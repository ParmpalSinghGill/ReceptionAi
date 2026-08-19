import { NextResponse, type NextRequest } from "next/server";

const API = process.env.API_ORIGIN ?? process.env.API_DATA_ORIGIN;

/**
 * The BFF login route (06 §8.2).
 *
 * This was the missing fourth route. `logout`, `refresh` and `session` all
 * existed; the login page posted here and got Next's 404 HTML, so the portal
 * could never authenticate and every screen ran on mock data. A booking made
 * through the chat was in the database and absent from `/bookings`, which is
 * how the gap surfaced — a data bug on the face of it, an auth bug underneath.
 *
 * The split between the two credentials is the whole point of the route:
 *
 *   access token   returned in the JSON body, held in memory by the client
 *   refresh token  set as an HttpOnly cookie, never readable by script
 *
 * Putting the access token in a cookie would make it ambient — sent on every
 * request to the origin, including ones a page did not intend — which reopens
 * CSRF on every guarded route. 05's memory-only rule exists for that reason.
 */
export async function POST(req: NextRequest) {
  if (!API) {
    // Demo mode: no upstream to authenticate against. Mirrors `refresh`, so a
    // clean clone still renders every screen without a database behind it.
    return NextResponse.json({ access_token: "demo", expires_in: 600 });
  }

  let credentials: { email?: string; password?: string };
  try {
    credentials = await req.json();
  } catch {
    return NextResponse.json(
      { code: "invalid_request", title: "Expected a JSON body" },
      { status: 400 },
    );
  }

  const upstream = await fetch(`${API}/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      // Forwarded so the API can rate-limit and audit by real client, not by
      // this server. Without them every login appears to come from one IP.
      "user-agent": req.headers.get("user-agent") ?? "",
      "x-forwarded-for": req.headers.get("x-forwarded-for") ?? "",
    },
    body: JSON.stringify({
      email: credentials.email ?? "",
      password: credentials.password ?? "",
    }),
  });

  if (!upstream.ok) {
    // Deliberately does NOT distinguish "no such user" from "wrong password".
    // The difference is an account-enumeration oracle, and the upstream is
    // already careful not to leak it — undoing that here would be free for an
    // attacker and worth nothing to a legitimate user, who knows which of the
    // two they got wrong.
    return NextResponse.json(
      { code: "invalid_credentials", title: "Email or password is incorrect" },
      { status: 401 },
    );
  }

  const body = (await upstream.json()) as {
    access_token: string;
    refresh_token: string;
    expires_in: number;
  };

  const res = NextResponse.json({
    access_token: body.access_token,
    expires_in: body.expires_in,
  });

  // `secure` is relaxed on plain-HTTP localhost only. A Secure cookie is
  // silently dropped over http://, so a hardcoded `true` would make login
  // appear to succeed and then fail on the first refresh — the sort of failure
  // that reads as a session bug rather than a cookie policy.
  const isLocalhost = new URL(req.url).hostname === "localhost";
  res.cookies.set("rai_refresh", body.refresh_token, {
    httpOnly: true,
    secure: !isLocalhost,
    sameSite: "lax",
    // Scoped to the auth paths that use it. A refresh token sent on every
    // request to the origin is a refresh token in every access log.
    path: "/api/v1/auth",
    maxAge: 30 * 24 * 3600,
  });

  // Double-submit CSRF partner for the refresh route, which compares this
  // cookie against an `X-CSRF-Token` header. Readable by script on purpose —
  // its security comes from same-origin policy, not from being hidden.
  res.cookies.set("rai_csrf", crypto.randomUUID(), {
    httpOnly: false,
    secure: !isLocalhost,
    sameSite: "lax",
    path: "/",
    maxAge: 30 * 24 * 3600,
  });

  return res;
}
