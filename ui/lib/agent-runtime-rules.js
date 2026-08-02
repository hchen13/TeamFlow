// Shared runtime rules for the agent panel and the server actions. It stays free of Node built-ins
// because the browser bundle imports it, and both sides read the same predicates so a fix on one
// cannot drift away from the other.

// Only a positively reported not-working session may be switched or removed. systemError says the
// session failed, not that it stopped, and checking/unconfirmed/missing cannot rule a turn out, so
// every one of them fails closed.
const MUTABLE_RUNTIME_STATUSES = new Set(["idle", "notLoaded"]);

export function runtimeStatusAllowsMutation(status) {
  return MUTABLE_RUNTIME_STATUSES.has(status);
}

// Only the agent id selects what is acted on. The session to check is read back from workspace
// state, so a submitted session cannot point the check at a different session than the command.
export function agentForMutation(state, agentId) {
  return (state?.agents || []).find((agent) => agent.id === agentId) || null;
}

export function agentMutationAllowed(snapshot, sessionId) {
  const runtime = (snapshot?.sessions || []).find((session) => session.threadId === sessionId);
  return Boolean(sessionId) && runtimeStatusAllowsMutation(runtime?.status);
}

// The panel merges a polled snapshot with the event stream. A snapshot is taken when its request
// reaches the server, so a response that returned late must not roll back newer stream events.
// Revisions are only comparable within one bridge instance: a rebuilt bridge restarts its counter,
// so a new epoch is always accepted and restarts the sequence.
export function acceptsRuntimeSequence(applied, incoming) {
  if (typeof incoming?.revision !== "number") {
    return true;
  }
  if (!applied || applied.epoch !== incoming.epoch) {
    return true;
  }
  return incoming.revision >= applied.revision;
}
