// The agent panel merges a polled snapshot and a streamed update into one runtime map. A snapshot
// is taken when its request reaches the server, so a stream event that landed while the response
// was in flight is newer and must survive it. The bridge revision decides which one wins.
//
// This module stays free of Node built-ins because the browser bundle imports it.
export function acceptsRuntimeRevision(applied, revision) {
  return typeof revision !== "number" || revision >= applied;
}
