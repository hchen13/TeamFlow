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
// Revisions are only comparable within one bridge instance, so the tracker follows the newest
// epoch it has seen and retires the one it replaced. Arrival order decides which epoch is current;
// the epochs themselves are opaque identifiers and are never ordered against each other.
export function createRuntimeSequence() {
  return { epoch: null, revision: -1, retired: new Set() };
}

export function acceptRuntimeSequence(tracker, incoming) {
  // A payload from a bridge that predates the sequence carries no ordering claim, so it is applied
  // without moving the tracker.
  if (typeof incoming?.revision !== "number") {
    return true;
  }
  if (tracker.retired.has(incoming.epoch)) {
    return false;
  }
  if (tracker.epoch !== incoming.epoch) {
    if (tracker.epoch !== null) {
      tracker.retired.add(tracker.epoch);
    }
    tracker.epoch = incoming.epoch;
    tracker.revision = incoming.revision;
    return true;
  }
  if (incoming.revision < tracker.revision) {
    return false;
  }
  tracker.revision = incoming.revision;
  return true;
}
