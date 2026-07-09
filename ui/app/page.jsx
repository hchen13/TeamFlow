import TeamFlowClient from "./teamflow-client";
import {
  configureLark,
  createLarkBoard,
  refreshLarkIdentity,
  registerAgent,
  removeLarkIdentity,
  selectWorkflow,
  setDefaultLarkIdentity,
  startLarkUserAuth,
  unregisterAgent
} from "../lib/actions";
import { getState } from "../lib/teamflow";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }) {
  const params = await searchParams;
  const state = await getState();
  const currentWorkflow = state.current_workflow || state.workflows[0];
  const currentRoles = state.roles.filter((role) => role.workflow_key === currentWorkflow?.key).sort(roleSort);

  return (
    <TeamFlowClient
      actions={{
        configureLark,
        createLarkBoard,
        refreshLarkIdentity,
        registerAgent,
        removeLarkIdentity,
        selectWorkflow,
        setDefaultLarkIdentity,
        startLarkUserAuth,
        unregisterAgent
      }}
      authExpires={textParam(params?.auth_expires)}
      authUrl={textParam(params?.auth_url)}
      currentRoles={currentRoles}
      initialLang={textParam(params?.lang)}
      initialStep={textParam(params?.step)}
      initialTab={textParam(params?.tab) || "lark"}
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
