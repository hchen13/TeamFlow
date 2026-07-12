import { spawnTeamflow, workspaceArgs } from "../../../../lib/teamflow";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request) {
  const body = await request.json().catch(() => ({}));
  const args = ["verify-lark-board", ...workspaceArgs(), "--stream"];
  if (body.identity_id) {
    args.push("--identity-id", String(body.identity_id));
  }
  const child = spawnTeamflow(args);
  const encoder = new TextEncoder();
  let stderr = "";
  let closed = false;
  const stream = new ReadableStream({
    start(controller) {
      child.stdout.on("data", (chunk) => controller.enqueue(chunk));
      child.stderr.on("data", (chunk) => {
        stderr += chunk.toString();
      });
      child.on("close", (code) => {
        if (closed) {
          return;
        }
        closed = true;
        if (code) {
          controller.enqueue(encoder.encode(`${JSON.stringify({ type: "verification_error", error: stderr.trim() || `TeamFlow exited with ${code}` })}\n`));
        }
        controller.close();
      });
      request.signal.addEventListener("abort", () => child.kill(), { once: true });
    },
    cancel() {
      closed = true;
      child.kill();
    }
  });
  return new Response(stream, {
    headers: {
      "Cache-Control": "no-cache, no-transform",
      "Content-Type": "application/x-ndjson; charset=utf-8"
    }
  });
}
