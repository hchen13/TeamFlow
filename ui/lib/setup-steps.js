// The setup flow's two pure decisions. They live outside the client component and the
// server action so both can be exercised without a React renderer or a Next request.

export function initialLarkStep(step, hasIdentity) {
  if (step === "workflow" || step === "identity") {
    return step;
  }
  if (step === "board" && hasIdentity) {
    return "board";
  }
  // A workspace with no identity has never been set up, and the workflow step is where
  // it chooses its workflow and whether tasks are delivered through git. Landing on the
  // identity step skipped both and left the choice to whatever the defaults happened to be.
  return hasIdentity ? "board" : "workflow";
}

export function versionControlArgument(enabled) {
  // An unchecked checkbox sends no value at all, so absence is the disabled case.
  return enabled === "true" ? "--enable" : "--disable";
}
