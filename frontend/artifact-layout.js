export const SPATIAL_METRIC_ORDER = Object.freeze([
  "intensity",
  "PGA",
  "PGV",
  "PSA03",
  "PSA10",
  "PSA30",
]);

export const SPATIAL_METRIC_LABELS = Object.freeze({
  PGA: "PGA 峰值加速度",
  PGV: "PGV 峰值速度",
  PSA03: "PSA 0.3 s",
  PSA10: "PSA 1.0 s",
  PSA30: "PSA 3.0 s",
  intensity: "仪器地震烈度",
});

function artifactText(artifact) {
  return `${artifact?.filename || ""} ${artifact?.label || ""}`.toLowerCase();
}

export function inferArtifactMetric(artifact) {
  if (SPATIAL_METRIC_ORDER.includes(artifact?.metric)) return artifact.metric;
  const text = artifactText(artifact);
  if (/psa[\s_-]*0[._-]?3(?:[^0-9]|$)|0[._]3\s*s/.test(text)) return "PSA03";
  if (/psa[\s_-]*1(?:[._-]?0)?(?:[^0-9]|$)|1[._]0\s*s/.test(text)) return "PSA10";
  if (/psa[\s_-]*3(?:[._-]?0)?(?:[^0-9]|$)|3[._]0\s*s/.test(text)) return "PSA30";
  if (/(^|[^a-z])pgv([^a-z]|$)/.test(text)) return "PGV";
  if (/(^|[^a-z])pga([^a-z]|$)/.test(text)) return "PGA";
  if (/intensity|烈度/.test(text)) return "intensity";
  return null;
}

export function inferArtifactKind(artifact) {
  if (["attenuation", "spatial-map", "quality-control"].includes(artifact?.kind)) {
    return artifact.kind;
  }
  const text = artifactText(artifact);
  if (/attenuation|distance|随距离|衰减/.test(text)) return "attenuation";
  if (/quality|质控/.test(text)) return "quality-control";
  if (inferArtifactMetric(artifact) && /map|空间|分布/.test(text)) return "spatial-map";
  return "other";
}

export function groupArtifacts(artifacts = []) {
  const groups = { attenuation: [], spatial: [], quality: [], other: [] };
  for (const artifact of artifacts) {
    const normalized = {
      ...artifact,
      kind: inferArtifactKind(artifact),
      metric: inferArtifactMetric(artifact),
    };
    if (normalized.kind === "attenuation") groups.attenuation.push(normalized);
    else if (normalized.kind === "spatial-map") groups.spatial.push(normalized);
    else if (normalized.kind === "quality-control") groups.quality.push(normalized);
    else groups.other.push(normalized);
  }
  groups.spatial.sort((left, right) => {
    const leftIndex = SPATIAL_METRIC_ORDER.indexOf(left.metric);
    const rightIndex = SPATIAL_METRIC_ORDER.indexOf(right.metric);
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
  });
  return groups;
}

export function spatialArtifactLabel(artifact) {
  return SPATIAL_METRIC_LABELS[inferArtifactMetric(artifact)] || artifact?.label || "空间分布图";
}
