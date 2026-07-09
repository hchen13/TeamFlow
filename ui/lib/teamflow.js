import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const cliPath = process.env.TEAMFLOW_CLI || "../scripts/teamflow.py";
const larkCliPath = process.env.LARK_CLI || "lark-cli";
const workspace = process.env.TEAMFLOW_WORKSPACE || "..";

export async function getState() {
  return runJson(["inspect", "--workspace", workspace, "--json"]);
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

export async function startLarkBaseAuth() {
  let payload;
  try {
    const result = await execFileAsync(larkCliPath, ["auth", "login", "--domain", "base", "--no-wait", "--json"], {
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

export async function fetchLarkAppInfo(appId, appSecret, domain) {
  if (!appId || !appSecret) {
    return {};
  }
  const origin = domain === "larksuite" ? "https://open.larksuite.com" : "https://open.feishu.cn";
  const tokenResponse = await fetch(`${origin}/open-apis/auth/v3/tenant_access_token/internal`, {
    method: "POST",
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify({ app_id: appId, app_secret: appSecret })
  });
  const tokenPayload = await tokenResponse.json();
  const token = tokenPayload?.tenant_access_token;
  if (!token) {
    return {};
  }

  const lang = domain === "larksuite" ? "en_us" : "zh_cn";
  const appResponse = await fetch(`${origin}/open-apis/application/v6/applications/${encodeURIComponent(appId)}?lang=${lang}`, {
    headers: { authorization: `Bearer ${token}` }
  });
  const appPayload = await appResponse.json();
  const app = appPayload?.data?.app || {};
  return {
    name: app.app_name || appPayload?.data?.app_name || app.name || appPayload?.data?.name || "",
    avatarUrl: app.avatar_url || appPayload?.data?.avatar_url || ""
  };
}

export function workspaceArgs() {
  return ["--workspace", workspace];
}
