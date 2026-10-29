import { readRun } from "@/lib/server-runs";
import { forbidden, isLocalRequest, localHeaders } from "@/lib/local-request";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  if (!isLocalRequest(request)) return forbidden();
  try {
    const rel = new URL(request.url).searchParams.get("path");
    if (!rel) return Response.json({ error: "path is required" }, { status: 400, headers: localHeaders });
    return new Response(await readRun(rel), { headers: { ...localHeaders, "Content-Type": "application/json; charset=utf-8" } });
  } catch {
    return Response.json({ error: "Run unavailable or exceeds viewer limits" }, { status: 404, headers: localHeaders });
  }
}
