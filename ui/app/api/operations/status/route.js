import { run, runJson } from "../../../../lib/teamflow";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

let restartInFlight = null;

export async function GET() {
  return Response.json(await readDaemonStatus(), {
    headers: { "Cache-Control": "no-store" }
  });
}

export async function POST(request) {
  if (!isSameOrigin(request)) {
    return Response.json({ error: "Cross-origin daemon operations are not allowed." }, { status: 403 });
  }

  if (!restartInFlight) {
    restartInFlight = restartDaemon().finally(() => {
      restartInFlight = null;
    });
  }

  try {
    return Response.json(await restartInFlight, {
      headers: { "Cache-Control": "no-store" }
    });
  } catch (error) {
    return Response.json({ error: error.message || String(error) }, { status: 500 });
  }
}

async function restartDaemon() {
  await run(["daemon", "stop"]);
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const status = await readDaemonStatus();
    if (!status.running) {
      return runJson(["daemon", "start"]);
    }
    await sleep(150);
  }
  throw new Error("TeamFlow daemon did not stop within 10 seconds.");
}

async function readDaemonStatus() {
  try {
    return await runJson(["daemon", "status"]);
  } catch (error) {
    try {
      return JSON.parse(error.message);
    } catch {
      return {
        running: false,
        healthy: false,
        ready: false,
        error: error.message || String(error)
      };
    }
  }
}

function isSameOrigin(request) {
  const origin = request.headers.get("origin");
  return Boolean(origin) && origin === new URL(request.url).origin;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
