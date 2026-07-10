"use server";

import { redirect } from "next/navigation";
import { run, startLarkUserAuthFlow, workspaceArgs } from "./teamflow";

const messages = {
  zh: {
    agentRegistered: "Agent 已注册",
    agentRemoved: "Agent 已移除",
    authGenerated: "授权链接已生成，打开后完成确认",
    boardCreated: "多维表格已创建",
    boardSaved: "多维表格已保存",
    identityDefaulted: "默认身份已更新",
    identityRemoved: "身份已删除",
    identityRefreshed: "应用名称已刷新",
    identitySaved: "飞书身份已保存",
    workflowUpdated: "Workflow 已更新"
  },
  en: {
    agentRegistered: "Agent registered",
    agentRemoved: "Agent removed",
    authGenerated: "Authorization link generated. Open it to confirm.",
    boardCreated: "Bitable created",
    boardSaved: "Bitable saved",
    identityDefaulted: "Default identity updated",
    identityRemoved: "Identity removed",
    identityRefreshed: "App name refreshed",
    identitySaved: "Lark identity saved",
    workflowUpdated: "Workflow updated"
  }
};

export async function configureLarkIdentity(formData) {
  const args = ["configure-lark-identity", ...workspaceArgs()];
  const env = {};
  const lang = language(formData);
  const appId = field(formData, "app_id");
  const appSecret = field(formData, "app_secret");
  add(args, "--app-id", appId);
  add(args, "--domain", field(formData, "lark_domain") || "feishu");
  addSecret(args, env, "--app-secret-env", "TEAMFLOW_UI_APP_SECRET", appSecret);
  await finish(args, env, "lark", "identitySaved", lang, "identity");
}

export async function configureLarkBoard(formData) {
  const args = ["configure-lark-board", ...workspaceArgs()];
  const lang = language(formData);
  add(args, "--url", field(formData, "board_url"));
  await finish(args, {}, "lark", "boardSaved", lang, "board");
}

export async function startLarkUserAuth(formData) {
  let target;
  const lang = language(formData);
  try {
    const auth = await startLarkUserAuthFlow();
    const params = new URLSearchParams({
      tab: "lark",
      lang,
      auth_url: auth.verification_url,
      auth_expires: String(auth.expires_in || 600),
      message: messages[lang].authGenerated,
      step: "identity"
    });
    target = `/?${params.toString()}`;
  } catch (error) {
    target = redirectTarget("lark", lang, error.message, true, "identity");
  }
  redirect(target);
}

export async function refreshLarkIdentity(formData) {
  const lang = language(formData);
  const args = ["refresh-lark-identity", ...workspaceArgs()];
  add(args, "--identity-id", field(formData, "identity_id"));
  add(args, "--domain", field(formData, "lark_domain") || "feishu");
  await finish(args, {}, "lark", "identityRefreshed", lang, "identity");
}

export async function removeLarkIdentity(formData) {
  const lang = language(formData);
  const args = ["remove-lark-identity", ...workspaceArgs()];
  add(args, "--identity-id", field(formData, "identity_id"));
  await finish(args, {}, "lark", "identityRemoved", lang, "identity");
}

export async function createLarkBoard(formData) {
  const lang = language(formData);
  const args = ["create-lark-board", ...workspaceArgs()];
  add(args, "--domain", field(formData, "lark_domain") || "feishu");
  add(args, "--name", field(formData, "board_name"));
  await finish(args, {}, "lark", "boardCreated", lang, "board");
}

export async function setDefaultLarkIdentity(formData) {
  const lang = language(formData);
  const args = ["set-default-lark-identity", ...workspaceArgs()];
  add(args, "--identity-id", field(formData, "identity_id"));
  await finish(args, {}, "lark", "identityDefaulted", lang, "identity");
}

export async function registerAgent(formData) {
  const args = ["register-agent", ...workspaceArgs()];
  const lang = language(formData);
  add(args, "--workflow", field(formData, "workflow"));
  add(args, "--role", field(formData, "role"));
  add(args, "--harness-type", field(formData, "harness_type"));
  add(args, "--session-id", field(formData, "session_id"));
  add(args, "--display-name", field(formData, "display_name"));
  if (formData.get("replace_role") === "on") {
    args.push("--replace-role");
  }
  await finish(args, {}, "agent", "agentRegistered", lang);
}

export async function unregisterAgent(formData) {
  const args = ["unregister-agent", ...workspaceArgs()];
  const lang = language(formData);
  add(args, "--agent-id", field(formData, "agent_id"));
  await finish(args, {}, "agent", "agentRemoved", lang);
}

export async function selectWorkflow(formData) {
  const args = ["select-workflow", ...workspaceArgs()];
  const lang = language(formData);
  const tab = field(formData, "tab") === "lark" ? "lark" : "agent";
  add(args, "--workflow", field(formData, "workflow"));
  await finish(args, {}, tab, "workflowUpdated", lang, field(formData, "step"));
}

async function finish(args, env, tab, okMessage, lang, step = "") {
  let target;
  try {
    await run(args, env);
    target = redirectTarget(tab, lang, messages[lang][okMessage], false, step);
  } catch (error) {
    target = redirectTarget(tab, lang, error.message, true, step);
  }
  redirect(target);
}

function field(formData, name) {
  return String(formData.get(name) || "").trim();
}

function language(formData) {
  return field(formData, "lang") === "en" ? "en" : "zh";
}

function redirectTarget(tab, lang, message, error = false, step = "") {
  const params = new URLSearchParams({ tab, lang, message });
  if (error) {
    params.set("error", "1");
  }
  if (step) {
    params.set("step", step);
  }
  return `/?${params.toString()}`;
}

function add(args, flag, value) {
  if (value) {
    args.push(flag, value);
  }
}

function addSecret(args, env, flag, name, value) {
  if (value) {
    env[name] = value;
    args.push(flag, name);
  }
}
