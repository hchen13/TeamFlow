function workspaceFromLine(line) {
  const namespace = String(line).match(/\[([^\]]+)\s+@[^\]]+\]/);
  if (namespace) return namespace[1].trim();

  const field = String(line).match(/\bworkspace=(?:"([^"]+)"|([^\s]+))/);
  return (field?.[1] || field?.[2] || "").trim() || null;
}

function visibleForWorkspace(line, aliases) {
  const workspace = workspaceFromLine(line);
  if (!workspace) return true;
  return aliases.some((alias) => alias && workspace === alias);
}

module.exports = { visibleForWorkspace, workspaceFromLine };
