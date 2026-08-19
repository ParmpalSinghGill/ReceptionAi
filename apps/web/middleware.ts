import { NextResponse, type NextRequest } from "next/server";

const PUBLIC = [/^\/login/, /^\/auth\//, /^\/forgot/, /^\/reset/, /^\/api\//];

/**
 * Layer 1 of 06 §8.3 — cheap, and wrong-by-default-safe.
 *
 * It redirects an unauthenticated visitor before the app runs. It does NOT
 * validate the token: the Edge runtime has no business verifying Ed25519
 * signatures against a JWKS it must fetch. It checks for the PRESENCE of a
 * refresh cookie and nothing more, which is exactly the right amount of work
 * for a layer whose only job is to avoid rendering a portal to a stranger.
 *
 * In demo mode (no API_ORIGIN) there is no cookie to find, so the check is
 * skipped — otherwise the portal would be permanently unreachable.
 */
export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (PUBLIC.some((p) => p.test(pathname))) return NextResponse.next();
  if (!process.env.API_ORIGIN) return NextResponse.next();

  if (!req.cookies.has("rai_refresh")) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    // Redirect-to-intended-destination: the path the user actually wanted
    // travels through the login round trip and is restored afterwards.
    url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|woff2)$).*)"],
};
