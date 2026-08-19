import { NextResponse, type NextRequest } from "next/server";

const API = process.env.API_ORIGIN;

/**
 * The BFF refresh route (06 §8.2). One of only four `app/api/*` routes, all
 * of which exist for the cookie↔token exchange — never as a proxy for data.
 */
export async function POST(req: NextRequest) {
  // Double-submit CSRF check before anything touches the upstream.
  const cookieToken = req.cookies.get("rai_csrf")?.value;
  const headerToken = req.headers.get("X-CSRF-Token");
  if (!API) {
    // Demo mode: no upstream to exchange against. A short-lived fake token
    // keeps apiFetch's contract intact for any call site that reaches it.
    return NextResponse.json({ access_token: "demo", expires_in: 600 });
  }
  if (!cookieToken || cookieToken !== headerToken) {
    return NextResponse.json(
      { code: "csrf_mismatch", title: "CSRF token mismatch" },
      { status: 403 },
    );
  }

  const upstream = await fetch(`${API}/api/v1/auth/refresh`, {
    method: "POST",
    headers: {
      // Forward ONLY the refresh cookie. Forwarding the whole Cookie header
      // would hand the API every unrelated cookie on the origin, including
      // any set by a marketing script — a needless widening of what the API
      // sees.
      cookie: `rai_refresh=${req.cookies.get("rai_refresh")?.value ?? ""}`,
      "user-agent": req.headers.get("user-agent") ?? "",
      "x-forwarded-for": req.headers.get("x-forwarded-for") ?? "",
    },
  });

  if (!upstream.ok) {
    const res = NextResponse.json(
      { code: "refresh_failed", title: "Session expired" },
      { status: 401 },
    );
    res.cookies.delete("rai_refresh");
    res.cookies.delete("rai_csrf");
    return res;
  }

  const body = (await upstream.json()) as {
    access_token: string;
    refresh_token: string;
    expires_in: number;
  };

  // The access token goes in the JSON body, to be held in memory. Only the
  // rotated refresh token becomes a cookie. Putting the access token in a
  // cookie would make it an ambient credential and reopen CSRF on every
  // guarded route — precisely what 05's memory-only rule prevents.
  const res = NextResponse.json({
    access_token: body.access_token,
    expires_in: body.expires_in,
  });
  res.cookies.set("rai_refresh", body.refresh_token, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/api/v1/auth",
    maxAge: 30 * 24 * 3600,
  });
  return res;
}
