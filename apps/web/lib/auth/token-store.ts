type Listener = (token: string | null) => void;

/**
 * The access token lives here and nowhere else.
 *
 * Not localStorage, not sessionStorage, not a cookie — 05 §A1 is explicit
 * ("Memory only — never localStorage"). Any XSS can read localStorage; the
 * same XSS can read this variable too, but only while the page is alive,
 * whereas localStorage survives the tab closing and hands an attacker a token
 * that outlives their injection.
 *
 * Module-scoped rather than React state because the fetch wrapper is called
 * from outside React (React Query's queryFn, the SSE reconnect loop) and must
 * not depend on a render having happened.
 */
let accessToken: string | null = null;
let expiresAt = 0;
let refreshInFlight: Promise<string | null> | null = null;
const listeners = new Set<Listener>();

export function setAccessToken(token: string | null, expiresInSeconds = 0): void {
  accessToken = token;
  expiresAt = token ? Date.now() + expiresInSeconds * 1000 : 0;
  listeners.forEach((l) => l(token));
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onTokenChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Refresh 60 seconds before expiry so an in-flight request never races it. */
export function needsRefresh(): boolean {
  return !accessToken || Date.now() > expiresAt - 60_000;
}

export async function refreshAccessToken(): Promise<string | null> {
  // Single-flight. Without this, a page that mounts eight queries at once
  // fires eight concurrent refreshes; 05 §A1.4 revokes the whole family on
  // refresh-token reuse, so the 2nd through 8th would look like an attack and
  // log the user out. This is not an optimization — the naive version is a
  // guaranteed logout on every cold page load.
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const res = await fetch("/api/auth/refresh", {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": readCsrfCookie() },
      });
      if (!res.ok) {
        setAccessToken(null);
        return null;
      }
      const body = (await res.json()) as { access_token: string; expires_in: number };
      setAccessToken(body.access_token, body.expires_in);
      return body.access_token;
    } catch {
      // A network failure is not an auth failure. Returning null lets the
      // caller surface a retryable error rather than logging the user out
      // because their wifi dropped for a second.
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

function readCsrfCookie(): string {
  if (typeof document === "undefined") return "";
  return (
    document.cookie
      .split("; ")
      .find((c) => c.startsWith("rai_csrf="))
      ?.split("=")[1] ?? ""
  );
}
