import "server-only";

import type { Me } from "@/lib/api/types";
import { MOCK_ME } from "@/lib/mock/data";

/**
 * Resolves the session on the SERVER, in the (app) layout, before any HTML is
 * streamed — so an unauthenticated visitor never receives portal markup
 * (06 §7.3, layer 2 of §8.3's three).
 *
 * In the real build this reads the HttpOnly refresh cookie, exchanges it
 * against `POST /api/v1/auth/refresh`, and calls `GET /api/v1/me` with the
 * resulting access token. The exchange happens here rather than in the client
 * so the token never has to be in client JS before first paint.
 *
 * When API_ORIGIN is unset the app runs in demo mode against the seeded
 * Northgate Dental tenant: Phase 1's skeleton does not yet serve most of the
 * portal, and a portal that cannot be opened cannot be reviewed.
 */
export async function getServerSession(
  cookieStore: { get(name: string): { value: string } | undefined },
): Promise<Me | null> {
  const apiOrigin = process.env.API_ORIGIN;

  if (!apiOrigin) return MOCK_ME;

  const refresh = cookieStore.get("rai_refresh")?.value;
  if (!refresh) return null;

  try {
    const tokenRes = await fetch(`${apiOrigin}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { cookie: `rai_refresh=${refresh}` },
      cache: "no-store",
    });
    if (!tokenRes.ok) return null;

    const { access_token } = (await tokenRes.json()) as { access_token: string };

    const meRes = await fetch(`${apiOrigin}/api/v1/me`, {
      headers: { authorization: `Bearer ${access_token}` },
      cache: "no-store",
    });
    if (!meRes.ok) return null;

    return (await meRes.json()) as Me;
  } catch {
    // A network failure talking to the API is not "logged out". Returning
    // null sends the user to /login, which is the safe direction: the
    // alternative is rendering a shell whose every query will 401.
    return null;
  }
}
