import { listRuns } from "@/lib/server-runs";
import { forbidden, isLocalRequest, localHeaders } from "@/lib/local-request";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  if (!isLocalRequest(request)) return forbidden();
  try {
    return Response.json({ runs: await listRuns() }, { headers: localHeaders });
  } catch {
    return Response.json({ error: "Run listing unavailable" }, { status: 503, headers: localHeaders });
  }
}
