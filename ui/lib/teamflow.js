import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const cliPath = process.env.TEAMFLOW_CLI || "../scripts/teamflow.py";
const larkCliPath = process.env.LARK_CLI || "lark-cli";
const workspace = process.env.TEAMFLOW_WORKSPACE || "..";
const larkUserScopes = "bitable:app docs:permission.member:auth docs:permission.member:create offline_access";

export async function getState() {
  return runJson(["inspect", "--workspace", workspace, "--json"]);
}

export async function getCodexState() {
  return runJson(["verify-agent", "--workspace", workspace]);
}

export function attachAgentHealth(agents, results) {
  const healthByAgent = new Map(results.map((health) => [health.agent_id, health]));
  return agents.map((agent) => ({ ...agent, health: healthByAgent.get(agent.id) || null }));
}

export async function runJson(args, env = {}) {
  const stdout = await run(args, env);
  return JSON.parse(stdout);
}

export async function run(args, env = {}) {
  try {
    const result = await execFileAsync(cliPath, args, {
      env: { ...process.env, ...env },
      maxBuffer: 1024 * 1024
    });
    return result.stdout.trim();
  } catch (error) {
    throw new Error(error.stderr?.trim() || error.stdout?.trim() || error.message);
  }
}

export async function startLarkUserAuthFlow() {
  let payload;
  try {
    const result = await execFileAsync(larkCliPath, ["auth", "login", "--scope", larkUserScopes, "--no-wait", "--json"], {
      env: process.env,
      maxBuffer: 1024 * 1024
    });
    payload = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(error.stderr?.trim() || error.stdout?.trim() || error.message);
  }

  if (!payload?.verification_url || !payload?.device_code) {
    throw new Error("lark-cli did not return an authorization URL");
  }

  const child = spawn(larkCliPath, ["auth", "login", "--device-code", payload.device_code], {
    detached: true,
    env: process.env,
    stdio: "ignore"
  });
  child.unref();

  return payload;
}

export function spawnTeamflow(args) {
  return spawn(cliPath, args, {
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"]
  });
}

export function workspaceArgs() {
  return ["--workspace", workspace];
}
