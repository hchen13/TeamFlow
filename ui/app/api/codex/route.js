import { getCodexBridge } from "../../../lib/codex-ipc";
import { attachAgentHealth, getCodexState, getState } from "../../../lib/teamflow";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request) {
  const bridge = getCodexBridge();
  const encoder = new TextEncoder();
  let cleanup;
  const stream = new ReadableStream({
    start(controller) {
      let closed = false;
      const send = (event) => {
        if (!closed) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        }
      };
      const unsubscribe = bridge.subscribe(send);
      const heartbeat = setInterval(() => {
        if (!closed) {
          controller.enqueue(encoder.encode(": keep-alive\n\n"));
        }
      }, 15000);
      send({ type: "snapshot", ...bridge.snapshot() });
      cleanup = () => {
        if (closed) {
          return;
        }
        closed = true;
        clearInterval(heartbeat);
        unsubscribe();
      };
      request.signal.addEventListener("abort", cleanup, { once: true });
    },
    cancel() {
      cleanup?.();
    }
  });

  return new Response(stream, {
    headers: {
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "Content-Type": "text/event-stream"
    }
  });
}

export async function POST() {
  let codexState;
  try {
    codexState = await getCodexState();
  } catch (error) {
    codexState = { sessions: [], session_error: error.message || String(error) };
  }
  const state = await getState();
  const bridge = getCodexBridge();
  bridge.track([
    ...(codexState.sessions || []).map((session) => session.session_id),
    ...(state.agents || []).map((agent) => agent.session_id)
  ]);
  return Response.json({
    agents: attachAgentHealth(state.agents || [], codexState.results || []),
    sessions: codexState.sessions || [],
    sessionError: Boolean(codexState.session_error),
    runtime: bridge.snapshot()
  });
}
