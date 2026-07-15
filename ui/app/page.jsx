import TeamFlowClient from "./teamflow-client";
import {
  configureLarkBoard,
  configureLarkIdentity,
  createLarkBoard,
  grantLarkBoardAccess,
  refreshLarkIdentity,
  registerAgent,
  removeLarkIdentity,
  selectWorkflow,
  startLarkUserAuth,
  unregisterAgent,
  updateAgent,
  verifyLarkUserIdentity
} from "../lib/actions";
import { attachAgentHealth, getCodexState, getState } from "../lib/teamflow";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }) {
  const params = await searchParams;
  const initialTab = textParam(params?.tab) || "lark";
  let codexSessions = [];
  let codexSessionError = false;
  let agentHealth = [];
  if (initialTab === "agent") {
    try {
      const codexState = await getCodexState();
      codexSessions = codexState.sessions || [];
      codexSessionError = Boolean(codexState.session_error);
      agentHealth = codexState.results || [];
    } catch {
      codexSessionError = true;
    }
  }
  const state = await getState();
  state.agents = attachAgentHealth(state.agents || [], agentHealth);
  const currentWorkflow = state.current_workflow || state.workflows[0];
  const currentRoles = state.roles.filter((role) => role.workflow_key === currentWorkflow?.key).sort(roleSort);

  return (
    <TeamFlowClient
      actions={{
        configureLarkBoard,
        configureLarkIdentity,
        createLarkBoard,
        grantLarkBoardAccess,
        refreshLarkIdentity,
        registerAgent,
        removeLarkIdentity,
        selectWorkflow,
        startLarkUserAuth,
        unregisterAgent,
        updateAgent,
        verifyLarkUserIdentity
      }}
      authExpires={textParam(params?.auth_expires)}
      boardUrlDraft={textParam(params?.board_url)}
      codexSessionError={codexSessionError}
      codexSessions={codexSessions}
      initialAuthMode={textParam(params?.auth_mode)}
      authUrl={textParam(params?.auth_url)}
      currentRoles={currentRoles}
      initialLang={textParam(params?.lang)}
      initialStep={textParam(params?.step)}
      initialTab={initialTab}
      message={textParam(params?.message)}
      error={textParam(params?.error)}
      state={state}
    />
  );
}

function textParam(value) {
  if (Array.isArray(value)) {
    return value[0] || "";
  }
  return value || "";
}

function roleSort(a, b) {
  const rank = { pm: 0, qa: 1, tl: 2, design: 3 };
  return (rank[a.role_key] ?? 99) - (rank[b.role_key] ?? 99) || a.role_key.localeCompare(b.role_key);
}
