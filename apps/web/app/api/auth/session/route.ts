import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { getServerSession } from "@/lib/auth/server";

/** BFF: returns the current principal for a client that needs to re-read it
 *  without a full navigation (after a role change, for example). */
export async function GET() {
  const session = await getServerSession(await cookies());
  if (!session) {
    return NextResponse.json({ code: "unauthenticated", title: "Not signed in" }, { status: 401 });
  }
  return NextResponse.json(session);
}
