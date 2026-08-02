import { agentForMutation, agentMutationAllowed } from "./agent-runtime-rules";

// The only place that turns a submitted agent form into a command. Its dependencies are injected
// so the real decision path runs in tests, and nothing here reads a session, a runtime status, or
// an assignment revision from the form: those all come from workspace state and the bridge.
export async function planAgentMutation({ readState, readSnapshot }, command, formData) {
  const agent = agentForMutation(await readState(), field(formData, "agent_id"));
  if (!agent) {
    return { blocked: true, checkedSession: null };
  }
  if (!agentMutationAllowed(readSnapshot(), agent.session_id)) {
    return { blocked: true, checkedSession: agent.session_id };
  }
  const args = ["--agent-id", agent.id];
  if (command === "update-agent") {
    args.push("--session-id", field(formData, "session_id"));
  }
  args.push("--expected-revision", String(agent.assignment_revision));
  return { blocked: false, agent, checkedSession: agent.session_id, args };
}

function field(formData, name) {
  return String(formData.get(name) || "").trim();
}
