import { NextResponse, type NextRequest } from "next/server";

const API = process.env.API_ORIGIN;

export async function POST(req: NextRequest) {
  if (API) {
    try {
      await fetch(`${API}/api/v1/auth/logout`, {
        method: "POST",
        headers: { cookie: `rai_refresh=${req.cookies.get("rai_refresh")?.value ?? ""}` },
      });
    } catch {
      // Revoking server-side is best-effort. Clearing the cookies below is
      // not: the user asked to leave, so the browser must forget the session
      // even if the API is unreachable.
    }
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.delete("rai_refresh");
  res.cookies.delete("rai_csrf");
  return res;
}
