"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { getCodexBridge } from "./codex-ipc";
import { run, startLarkUserAuthFlow, workspaceArgs } from "./teamflow";

const messages = {
  zh: {
    agentRegistered: "Agent 已注册，状态已检查",
    agentRemoved: "Agent 已移除",
    agentUpdated: "Agent Session 已切换",
    agentBusy: "Agent 正在工作，完成后才能切换或移除。",
    authGenerated: "授权链接已生成，打开后完成确认",
    boardCreated: "多维表格已创建",
    boardNotFound: "找不到这个多维表格。它可能已被删除，或当前身份无权访问。请恢复原表并检查权限；也可以粘贴新的多维表格链接，或在下方选择身份创建新表。",
    boardSaved: "多维表格链接已保存，正在验证身份访问",
    boardAccessGranted: "身份已添加为协作者，访问状态已重新验证",
    identityRemoved: "身份已删除",
    identityRefreshed: "应用名称已刷新",
    identitySaved: "飞书身份已保存",
    invalidBoardUrl: "请输入有效的飞书多维表格链接。",
    sessionAlreadyAssigned: "该 Session 已分配给此角色的另一个 Agent。",
    userAuthExpired: "用户授权已过期，请重新授权后再检查。",
    userIdentityRefreshed: "用户身份已刷新",
    userIdentityVerified: "用户身份已连接",
    workflowUpdated: "Workflow 已更新"
  },
  en: {
    agentRegistered: "Agent registered and checked",
    agentRemoved: "Agent removed",
    agentUpdated: "Agent session updated",
    agentBusy: "This agent is working. Wait until it finishes before switching or removing it.",
    authGenerated: "Authorization link generated. Open it to confirm.",
    boardCreated: "Bitable created",
    boardNotFound: "This Bitable could not be found. It may have been deleted, or the current identity may not have access. Restore it and check access, paste a new Bitable link, or choose an identity to create one below.",
    boardSaved: "Bitable link saved. Identity access is being verified",
    boardAccessGranted: "The identity was added as a collaborator and verified again",
    identityRemoved: "Identity removed",
    identityRefreshed: "App name refreshed",
    identitySaved: "Lark identity saved",
    invalidBoardUrl: "Enter a valid Lark Bitable link.",
    sessionAlreadyAssigned: "This session is already assigned to another agent in the role.",
    userAuthExpired: "User authorization has expired. Authorize again, then check the status.",
    userIdentityRefreshed: "User identity refreshed",
    userIdentityVerified: "User identity connected",
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
  const boardUrl = field(formData, "board_url");
  add(args, "--url", boardUrl);
  let target;
  try {
    await run(args);
    revalidatePath("/");
    target = redirectTarget("lark", lang, messages[lang].boardSaved, false, "board");
  } catch (error) {
    revalidatePath("/");
    target = redirectTarget("lark", lang, localizedError(error, lang), true, "board", "", { board_url: boardUrl });
  }
  redirect(target);
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
      auth_mode: "user",
      message: messages[lang].authGenerated,
      step: "identity"
    });
    target = `/?${params.toString()}`;
  } catch (error) {
    target = redirectTarget("lark", lang, localizedError(error, lang), true, "identity", "user");
  }
  redirect(target);
}

export async function verifyLarkUserIdentity(formData) {
  const lang = language(formData);
  const args = ["verify-lark-user-identity", ...workspaceArgs()];
  const message = field(formData, "intent") === "refresh" ? "userIdentityRefreshed" : "userIdentityVerified";
  await finish(args, {}, "lark", message, lang, "identity", "user");
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
  add(args, "--identity-id", field(formData, "identity_id"));
  add(args, "--domain", field(formData, "lark_domain") || "feishu");
  add(args, "--name", field(formData, "board_name"));
  await finish(args, {}, "lark", "boardCreated", lang, "board");
}

export async function grantLarkBoardAccess(formData) {
  const lang = language(formData);
  const args = ["grant-lark-board-access", ...workspaceArgs()];
  add(args, "--identity-id", field(formData, "identity_id"));
  await finish(args, {}, "lark", "boardAccessGranted", lang, "board");
}

export async function registerAgent(formData) {
  const args = ["register-agent", ...workspaceArgs()];
  const lang = language(formData);
  add(args, "--workflow", field(formData, "workflow"));
  add(args, "--role", field(formData, "role"));
  add(args, "--harness-type", field(formData, "harness_type"));
  add(args, "--session-id", field(formData, "session_id"));
  add(args, "--display-name", field(formData, "display_name"));
  await finish(args, {}, "agent", "agentRegistered", lang);
}

export async function unregisterAgent(formData) {
  const args = ["unregister-agent", ...workspaceArgs()];
  const lang = language(formData);
  blockActiveAgent(formData, lang);
  add(args, "--agent-id", field(formData, "agent_id"));
  await finish(args, {}, "agent", "agentRemoved", lang);
}

export async function updateAgent(formData) {
  const args = ["update-agent", ...workspaceArgs()];
  const lang = language(formData);
  blockActiveAgent(formData, lang);
  add(args, "--agent-id", field(formData, "agent_id"));
  add(args, "--session-id", field(formData, "session_id"));
  await finish(args, {}, "agent", "agentUpdated", lang);
}

export async function selectWorkflow(formData) {
  const args = ["select-workflow", ...workspaceArgs()];
  const lang = language(formData);
  const tab = field(formData, "tab") === "lark" ? "lark" : "agent";
  add(args, "--workflow", field(formData, "workflow"));
  await finish(args, {}, tab, "workflowUpdated", lang, field(formData, "step"));
}

async function finish(args, env, tab, okMessage, lang, step = "", authMode = "") {
  let target;
  try {
    await run(args, env);
    revalidatePath("/");
    target = redirectTarget(tab, lang, messages[lang][okMessage], false, step, authMode);
  } catch (error) {
    target = redirectTarget(tab, lang, localizedError(error, lang), true, step, authMode);
  }
  redirect(target);
}

function field(formData, name) {
  return String(formData.get(name) || "").trim();
}

function language(formData) {
  return field(formData, "lang") === "en" ? "en" : "zh";
}

function localizedError(error, lang) {
  const message = error.message || String(error);
  if (/"code"\s*:\s*131005\b|API error:\s*\[131005\]\s*not found/i.test(message)) {
    return messages[lang].boardNotFound;
  }
  if (/valid Feishu\/Lark Bitable URL/i.test(message)) {
    return messages[lang].invalidBoardUrl;
  }
  if (/session is already registered for the role/i.test(message)) {
    return messages[lang].sessionAlreadyAssigned;
  }
  return /user token has expired|authorization expired/i.test(message) ? messages[lang].userAuthExpired : message;
}

function blockActiveAgent(formData, lang) {
  const sessionId = field(formData, "current_session_id");
  const active = field(formData, "runtime_status") === "active"
    || getCodexBridge().snapshot().sessions.some((session) => session.threadId === sessionId && session.status === "active");
  if (active) {
    redirect(redirectTarget("agent", lang, messages[lang].agentBusy, true));
  }
}

function redirectTarget(tab, lang, message, error = false, step = "", authMode = "", extra = {}) {
  const params = new URLSearchParams({ tab, lang, message, ...extra });
  if (error) {
    params.set("error", "1");
  }
  if (step) {
    params.set("step", step);
  }
  if (authMode) {
    params.set("auth_mode", authMode);
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
