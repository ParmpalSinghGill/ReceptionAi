import { getAccessToken, needsRefresh, refreshAccessToken } from "@/lib/auth/token-store";

/**
 * RFC 9457 problem details. Every error the API returns has this shape,
 * including middleware denials (PHASE1-API-CONTRACT.md).
 */
export class ApiProblem extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly title: string,
    readonly detail: string | undefined,
    readonly requestId: string | undefined,
    readonly extensions: Record<string, unknown>,
  ) {
    super(title);
    this.name = "ApiProblem";
  }

  static async from(res: Response): Promise<ApiProblem> {
    let body: Record<string, unknown> = {};
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      /* a proxy returned HTML; fall through to the generic shape below */
    }
    const { title, code, detail, request_id, ...rest } = body;
    delete (rest as Record<string, unknown>).type;
    delete (rest as Record<string, unknown>).status;
    delete (rest as Record<string, unknown>).instance;
    return new ApiProblem(
      res.status,
      (code as string) ?? "unknown_error",
      (title as string) ?? res.statusText,
      detail as string | undefined,
      request_id as string | undefined,
      rest,
    );
  }
}

export interface ApiFetchInit extends RequestInit {
  idempotencyKey?: string;
  ifMatch?: string;
}

/**
 * The only thing in the app that talks to /api/v1.
 *
 * Note what is NOT here: a tenant id. The tenant comes from the bearer
 * token's `tid` claim, never a header or a path (contract §10) — the API
 * returns 400 tenant_header_not_permitted if you try.
 */
export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  if (needsRefresh()) await refreshAccessToken();

  const send = async (): Promise<Response> => {
    const headers = new Headers(init.headers);
    const token = getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
    if (init.ifMatch) headers.set("If-Match", init.ifMatch);
    return fetch(path, { ...init, headers, credentials: "same-origin" });
  };

  let res = await send();

  // One retry on 401. The token may have been revoked mid-flight (a
  // permission change bumps prm_v → token_stale) rather than merely expired,
  // and a single refresh distinguishes "stale token, recoverable" from
  // "session gone".
  if (res.status === 401) {
    const refreshed = await refreshAccessToken();
    if (refreshed) res = await send();
  }

  if (!res.ok) {
    const problem = await ApiProblem.from(res);
    // Events rather than direct navigation: this wrapper is called from React
    // Query's queryFn, from streaming reconnect loops, and from tests. Making
    // it call router.push would couple a transport utility to a router
    // instance it does not have. A listener in AppShell turns these into a
    // redirect and a modal respectively.
    if (problem.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("auth:expired"));
    }
    if (problem.code === "step_up_required" && typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("auth:step-up", { detail: { returnTo: location.pathname } }),
      );
    }
    throw problem;
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
