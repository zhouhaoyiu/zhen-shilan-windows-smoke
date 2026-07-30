export const ATTENUATION_SOURCE_STYLES = Object.freeze({
  observed: Object.freeze({ label: "实测分箱", color: "#367c99", symbol: "diamond", log10Offset: -0.012 }),
  inferred: Object.freeze({ label: "推测分箱", color: "#dc642c", symbol: "circle", log10Offset: 0.012 }),
});

const number = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

export function attenuationBinSeries(rows, metric) {
  const grouped = { observed: [], inferred: [] };
  for (const raw of Array.isArray(rows) ? rows : []) {
    if (raw?.metric !== metric || raw.status !== "ok" || !(raw.source in grouped)) continue;
    const row = {
      source: raw.source,
      metric,
      binLeftKm: number(raw.bin_left_km),
      binRightKm: number(raw.bin_right_km),
      binCenterKm: number(raw.bin_center_km),
      n: number(raw.n),
      mean: number(raw.mean),
      median: number(raw.median),
      lower: number(raw.lower_1sigma),
      upper: number(raw.upper_1sigma),
      space: raw.space,
    };
    if (
      row.binLeftKm === null || row.binRightKm === null || row.binCenterKm === null
      || row.n === null || row.n < 3 || row.mean === null || row.median === null
      || row.lower === null || row.upper === null || row.binCenterKm <= 0
      || row.binRightKm <= row.binLeftKm || row.lower > row.upper
    ) continue;
    grouped[row.source].push(row);
  }
  return Object.entries(grouped).map(([source, values]) => ({
    source,
    ...ATTENUATION_SOURCE_STYLES[source],
    values: values.sort((left, right) => left.binCenterKm - right.binCenterKm),
  }));
}
