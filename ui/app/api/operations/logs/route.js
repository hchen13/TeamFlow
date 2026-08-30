import { open, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const INITIAL_BYTES = 64 * 1024;
const MAX_READ_BYTES = 256 * 1024;
const INITIAL_LINES = 200;

export async function GET(request) {
  const encoder = new TextEncoder();
  const logPath = join(process.env.TEAMFLOW_HOME || join(homedir(), ".teamflow"), "daemon.log");
  let cleanup;

  const stream = new ReadableStream({
    start(controller) {
      let closed = false;
      let offset = 0;
      let carry = "";
      let reading = false;

      const send = (lines) => {
        if (!closed && lines.length) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ lines })}\n\n`));
        }
      };

      const readInitial = async () => {
        try {
          const snapshot = await stat(logPath);
          const start = Math.max(0, snapshot.size - INITIAL_BYTES);
          const text = await readRange(logPath, start, snapshot.size - start);
          const lines = text.split(/\r?\n/);
          if (start > 0) lines.shift();
          if (lines.at(-1) === "") lines.pop();
          offset = snapshot.size;
          send(lines.slice(-INITIAL_LINES));
        } catch (error) {
          if (error.code !== "ENOENT") send([`TeamFlow log unavailable: ${error.message}`]);
        }
      };

      const readUpdates = async () => {
        if (closed || reading) return;
        reading = true;
        try {
          const snapshot = await stat(logPath);
          if (snapshot.size < offset) {
            offset = 0;
            carry = "";
          }
          if (snapshot.size > offset) {
            const length = Math.min(snapshot.size - offset, MAX_READ_BYTES);
            const text = carry + await readRange(logPath, offset, length);
            offset += length;
            const parts = text.split(/\r?\n/);
            carry = parts.pop() || "";
            send(parts);
          }
        } catch (error) {
          if (error.code !== "ENOENT") send([`TeamFlow log unavailable: ${error.message}`]);
        } finally {
          reading = false;
        }
      };

      const poller = setInterval(readUpdates, 600);
      const heartbeat = setInterval(() => {
        if (!closed) controller.enqueue(encoder.encode(": keep-alive\n\n"));
      }, 15000);

      cleanup = () => {
        if (closed) return;
        closed = true;
        clearInterval(poller);
        clearInterval(heartbeat);
      };
      request.signal.addEventListener("abort", cleanup, { once: true });
      void readInitial();
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

async function readRange(path, start, length) {
  if (length <= 0) return "";
  const handle = await open(path, "r");
  try {
    const buffer = Buffer.alloc(length);
    const { bytesRead } = await handle.read(buffer, 0, length, start);
    return buffer.subarray(0, bytesRead).toString("utf8");
  } finally {
    await handle.close();
  }
}
