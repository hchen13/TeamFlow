import { spawnTeamflow, workspaceArgs } from "../../../../lib/teamflow";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request) {
  const body = await request.json().catch(() => ({}));
  const args = ["verify-lark-listener", ...workspaceArgs()];
  if (body.identity_id) {
    args.push("--identity-id", String(body.identity_id));
  }
  const child = spawnTeamflow(args);
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  request.signal.addEventListener("abort", () => child.kill(), { once: true });
  await new Promise((resolve) => child.on("close", resolve));
  try {
    return Response.json(JSON.parse(stdout));
  } catch {
    return Response.json({ error: stderr.trim() || stdout.trim() || "TeamFlow listener verification failed" }, { status: 500 });
  }
}
