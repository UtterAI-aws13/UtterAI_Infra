-- Explicit opt-in: Application readiness propagation is an orchestration policy.
local hs = {status = "Healthy", message = "Child health propagation is not enabled"}
local labels = obj.metadata.labels or {}
if labels["gitops.utterai.io/propagate-health"] ~= "true" then
  return hs
end
hs.status = "Progressing"
hs.message = "Waiting for Child reconciliation"
if obj.status == nil or obj.status.sync == nil then
  return hs
end
if obj.status.sync.status ~= "Synced" then
  hs.message = "Child is not Synced"
  return hs
end
-- For pinned revisions, don't accept Healthy left over from the previous revision.
local revision = obj.spec.source and obj.spec.source.targetRevision or ""
if #revision == 40 then
  if obj.status.sync.revision ~= revision then
    hs.message = "Waiting for the requested Child revision"
    return hs
  end
end
if obj.status.health ~= nil then
  hs.status = obj.status.health.status or "Progressing"
  hs.message = obj.status.health.message or "Child health propagated"
end
return hs
