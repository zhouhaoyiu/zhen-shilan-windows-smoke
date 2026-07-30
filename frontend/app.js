import { buildEvents, parseCsv, parseDat } from "./data.js";
import { normalizeGridTarget, parseManualTargets, parseTargetCsv } from "./target-config.js";
import { groupArtifacts, spatialArtifactLabel } from "./artifact-layout.js";
import { attenuationBinSeries } from "./attenuation-bins.js";

const d3 = window.d3;

const viewMeta = {
  map: ["空间分布 / Spatial field", "实测台站与推测场点"],
  attenuation: ["距离衰减 / Distance", "地震动随震源距变化"],
  waveforms: ["三分量波形 / Waveform", "加速度时程"],
  artifacts: ["成果图件 / Output", "流程生成图件"],
  ml: ["残差修正 / Validation", "残差修正与严格留出验证"],
};

const metricMeta = {
  PGA: { label: "PGA", unit: "cm/s²", log: true },
  PGV: { label: "PGV", unit: "cm/s", log: true },
  PSA03: { label: "PSA 0.3 s", unit: "cm/s²", log: true },
  PSA10: { label: "PSA 1.0 s", unit: "cm/s²", log: true },
  PSA30: { label: "PSA 3.0 s", unit: "cm/s²", log: true },
  intensity: { label: "仪器地震烈度", unit: "", log: false },
};

const componentColors = { EW: "#dc642c", NS: "#367c99", UD: "#205a55" };

// FIXED_STRONG_MOTION_SCALE_START
export const FIXED_SCALE_COLORS = Object.freeze([
  "#DEE5FF", "#AEDAFF", "#8DF4FF", "#7CFFBB", "#D3FF30", "#FFD700",
  "#FF9D00", "#FF1800", "#CD0000", "#820000", "#740000",
]);

export const FIXED_SCALE_LEVELS = Object.freeze([
  "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "10+",
]);

export const FIXED_SCALE_THRESHOLDS = Object.freeze({
  acceleration: Object.freeze([2.57, 5.28, 10.8, 22.2, 45.6, 93.6, 194, 401, 830, 1720]),
  velocity: Object.freeze([0.177, 0.381, 0.819, 1.76, 3.8, 8.17, 17.6, 37.8, 81.4, 175]),
  intensity: Object.freeze([2, 3, 4, 5, 6, 7, 8, 9, 10, 11]),
});

function fixedScaleNumber(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function fixedMetricScaleKind(metric) {
  if (metric === "PGV") return "velocity";
  if (metric === "intensity") return "intensity";
  if (["PGA", "PSA03", "PSA10", "PSA30"].includes(metric)) return "acceleration";
  return null;
}

export function fixedMetricClassIndex(metric, value) {
  const kind = fixedMetricScaleKind(metric);
  const number = fixedScaleNumber(value);
  if (!kind || number === null || number < 0) return -1;
  const thresholds = FIXED_SCALE_THRESHOLDS[kind];
  const nextBoundary = thresholds.findIndex((threshold) => number < threshold);
  return nextBoundary === -1 ? thresholds.length : nextBoundary;
}

export function fixedMetricColor(metric, value) {
  const index = fixedMetricClassIndex(metric, value);
  return index < 0 ? null : FIXED_SCALE_COLORS[index];
}

function interpolateHexColor(start, end, amount) {
  const channels = (color) => [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16));
  const startChannels = channels(start);
  const endChannels = channels(end);
  return `#${startChannels.map((channel, index) => Math.round(channel + (endChannels[index] - channel) * amount)
    .toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}

export function fixedMetricContinuousColor(metric, value) {
  const kind = fixedMetricScaleKind(metric);
  const number = fixedScaleNumber(value);
  if (!kind || number === null || number < 0) return null;
  const thresholds = FIXED_SCALE_THRESHOLDS[kind];
  const lowerAnchor = kind === "intensity"
    ? thresholds[0] - (thresholds[1] - thresholds[0])
    : thresholds[0] ** 2 / thresholds[1];
  if (number <= lowerAnchor) return FIXED_SCALE_COLORS[0];
  const transform = kind === "intensity" ? (entry) => entry : (entry) => Math.log(entry);
  const anchors = [lowerAnchor, ...thresholds].map(transform);
  const transformed = transform(number);
  if (transformed >= anchors.at(-1)) return FIXED_SCALE_COLORS.at(-1);
  const upper = anchors.findIndex((anchor) => transformed < anchor);
  const lower = Math.max(0, upper - 1);
  const span = anchors[upper] - anchors[lower];
  const amount = span > 0 ? (transformed - anchors[lower]) / span : 0;
  return interpolateHexColor(FIXED_SCALE_COLORS[lower], FIXED_SCALE_COLORS[upper], amount);
}

export function fixedMetricLegendLabels(metric) {
  const kind = fixedMetricScaleKind(metric);
  if (!kind) return [];
  const thresholds = FIXED_SCALE_THRESHOLDS[kind];
  return [
    `<${thresholds[0]}`,
    ...thresholds.slice(0, -1).map(String),
    `≥${thresholds.at(-1)}`,
  ];
}

export function fixedMetricIntervalLabels(metric) {
  const kind = fixedMetricScaleKind(metric);
  if (!kind) return [];
  const thresholds = FIXED_SCALE_THRESHOLDS[kind];
  return FIXED_SCALE_LEVELS.map((_, index) => {
    if (index === 0) return `<${thresholds[0]}`;
    if (index === thresholds.length) return `≥${thresholds.at(-1)}`;
    return `${thresholds[index - 1]}–<${thresholds[index]}`;
  });
}
// FIXED_STRONG_MOTION_SCALE_END

const mapColors = {
  ocean: "#EEF6F8",
  land: "#F7FAF8",
  boundary: "#6F8580",
  boundaryHalo: "#FFFFFF",
  label: "#4F6764",
  labelHalo: "#F7FAF8",
};
const administrativeLayerStyles = {
  "admin-province": { color: "#6F8580", width: 0.9, opacity: 0.96, labelSize: 8.2, priority: 0 },
  "admin-city": { color: "#91A6A1", width: 0.62, opacity: 0.88, labelSize: 7.2, priority: 1 },
  "admin-county": { color: "#B9C8C4", width: 0.42, opacity: 0.78, labelSize: 6.4, priority: 2 },
};
const administrativeLayerOrder = ["admin-county", "admin-city", "admin-province"];
const rawDataFolders = new Set(["eidata", "hndata", "eldata"]);

const state = {
  current: null,
  descriptors: [],
  catalogEvents: [],
  boundary: null,
  boundaryCache: new Map(),
  basemap: null,
  basemapCache: new Map(),
  defaultBoundaryUrl: "./demo/japan-prefectures.geojson",
  selectedPoint: null,
  activeView: "map",
  objectUrls: [],
  waveformToken: 0,
  catalogRequestToken: 0,
  mlPilot: null,
  mlPilotPromise: null,
  residualResearch: null,
  residualResearchPromise: null,
  strictKnetHoldout: null,
  strictKnetHoldoutPromise: null,
  unifiedStep3: null,
  unifiedStep3Promise: null,
  importSelection: null,
  importServiceReady: false,
  importActive: false,
  targetConfig: null,
  targetCsvText: null,
  targetCsvError: null,
  watchTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function finite(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstFinite(...values) {
  for (const value of values) {
    const number = finite(value);
    if (number !== null) return number;
  }
  return null;
}

function numberText(value, digits = 1) {
  const number = finite(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function byteText(value) {
  let bytes = finite(value);
  if (bytes === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit += 1;
  }
  return `${bytes.toLocaleString("zh-CN", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}

function elapsedText(value) {
  const seconds = finite(value);
  if (seconds === null || seconds < 0) return null;
  const rounded = Math.floor(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  if (hours) return `${hours}时${minutes}分${remainder}秒`;
  if (minutes) return `${minutes}分${remainder}秒`;
  return `${remainder}秒`;
}

function dateTimeText(value) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // The plain static server returns HTML for missing API routes.
  }
  if (!response.ok) throw new Error(payload?.error || `本机服务请求失败（HTTP ${response.status}）`);
  return payload || {};
}

function coordinateText(lon, lat) {
  const longitude = finite(lon);
  const latitude = finite(lat);
  if (longitude === null || latitude === null) return "—";
  return `${longitude.toFixed(3)}°E · ${latitude.toFixed(3)}°N`;
}

function normalizePoint(row, kind) {
  return {
    ...row,
    kind,
    station: String(row.station ?? row.Station ?? "").trim(),
    lon: firstFinite(row.lon, row.longitude, row.station_longitude_deg),
    lat: firstFinite(row.lat, row.latitude, row.station_latitude_deg),
    R: firstFinite(row.R, row.dis, row.hyp_dis, row.source_distance_km),
    azi: firstFinite(row.azi, row.azimuth),
    PGA: firstFinite(row.PGA, row.pga, row.pga_gal),
    PGV: firstFinite(row.PGV, row.pgv),
    PSA03: firstFinite(row.PSA03, row.psa03, row.psa0p3),
    PSA10: firstFinite(row.PSA10, row.psa10, row.psa1p0),
    PSA30: firstFinite(row.PSA30, row.psa30, row.psa3p0),
    intensity: firstFinite(row.intensity, row.Intensity),
  };
}

function folderTimestamp(name) {
  const match = String(name).match(/(\d{14})/);
  if (!match) return "结果目录未提供";
  const t = match[1];
  return `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)} ${t.slice(8, 10)}:${t.slice(10, 12)}:${t.slice(12, 14)}`;
}

function titleFromDescriptor(descriptor) {
  const observed = descriptor.files.observed?.name || "";
  const inferred = descriptor.files.inferred?.name || "";
  const outputName = observed || inferred;
  const extracted = outputName
    .replace(/^(实测|推测)地震动_?/, "")
    .replace(/\.csv$/i, "")
    .trim();
  return extracted || descriptor.name.replace(/_infer$/i, "");
}

function makeWaveformGroups(files) {
  const groups = new Map();
  for (const component of ["EW", "NS", "UD"]) {
    for (const file of files.waveforms[component] || []) {
      const stem = file.name.replace(/\.(EW|NS|UD)\.dat$/i, "");
      if (!groups.has(stem)) groups.set(stem, { key: stem, label: stem, files: {}, kind: "inferred" });
      groups.get(stem).files[component] = file;
    }
  }
  return [...groups.values()]
    .filter((group) => ["EW", "NS", "UD"].some((component) => group.files[component]))
    .sort((a, b) => a.label.localeCompare(b.label));
}

async function fromDescriptor(descriptor) {
  const readRows = async (file) => (file ? parseCsv(await file.text()) : []);
  const [observedRows, inferredRows, pointRows] = await Promise.all([
    readRows(descriptor.files.observed),
    readRows(descriptor.files.inferred),
    readRows(descriptor.files.points),
  ]);

  const observed = observedRows.map((row) => normalizePoint(row, "observed"));
  const inferred = inferredRows.map((row) => normalizePoint(row, "inferred"));
  const pointsByStation = new Map(pointRows.map((row) => [String(row.station), row]));

  for (const point of observed) {
    const station = pointsByStation.get(point.station);
    if (!station) continue;
    point.lon ??= finite(station.lon);
    point.lat ??= finite(station.lat);
    point.R ??= finite(station.hyp_dis);
  }

  const artifacts = [];
  const artifactEntries = [
    [descriptor.files.images.intensity, "仪器地震烈度空间分布"],
    [descriptor.files.images.distance, "推测与实测地震动随距离分布"],
    ...(descriptor.files.otherPng || []).map((file) => [file, file.name]),
  ];
  for (const [file, label] of artifactEntries) {
    if (!file) continue;
    const url = URL.createObjectURL(file);
    state.objectUrls.push(url);
    artifacts.push({ label, url, filename: file.name });
  }

  const config = descriptor.config || {};
  const title = titleFromDescriptor(descriptor);
  return {
    id: descriptor.id,
    title,
    demoOnly: false,
    eventId: descriptor.name,
    originTime: folderTimestamp(descriptor.name),
    config,
    observed,
    inferred,
    waveforms: makeWaveformGroups(descriptor.files),
    artifacts,
    resultPackage: null,
    summary: null,
    sourceLabel: "ZSL 结果目录",
    sourceDetail: "本地处理输出",
    badges: ["本地结果", ...(observed.length ? ["实测"] : []), ...(inferred.length ? ["推测"] : [])],
    integrityNote: inferred.length
      ? "实测与推测分层显示。推测流程含随机相位项，同一输入重复运行可能产生不同结果。"
      : "目录中没有推测 CSV；界面只显示已读取到的实测记录。",
    provenance: `结果目录：${descriptor.name}`,
  };
}

function normalizeDemo(raw) {
  const config = raw.config || raw.event || {};
  const observed = (raw.observed || raw.stations || []).map((row) => normalizePoint(row, "observed"));
  const inferred = (raw.inferred || []).map((row) => normalizePoint(row, "inferred"));
  const waveforms = (raw.waveforms || []).map((waveform, index) => ({
    ...waveform,
    key: waveform.station || waveform.key || `waveform-${index + 1}`,
    label: waveform.label || waveform.station || `波形 ${index + 1}`,
    kind: waveform.kind || "observed",
    samplingRate: firstFinite(waveform.samplingRate, waveform.sampling_rate_hz) || 100,
  }));
  const artifacts = (raw.artifacts || [])
    .filter((artifact) => artifact?.url)
    .map((artifact, index) => ({
      label: artifact.label || artifact.filename || `结果图 ${index + 1}`,
      filename: artifact.filename || `figure-${index + 1}.png`,
      url: artifact.url,
      kind: artifact.kind || null,
      metric: artifact.metric || null,
    }));

  return {
    id: raw.eventId || "knet-demo",
    title: raw.title || "2000 年鸟取县西部地震",
    demoOnly: raw.demoOnly !== false,
    eventId: raw.eventId || "0010061330",
    originTime: raw.originTime || raw.origin_time || config.origin_time || raw.provenance?.event?.originTime || "2000-10-06 13:30:00 JST",
    config,
    observed,
    inferred,
    distanceBinStatistics: Array.isArray(raw.distanceBinStatistics) ? raw.distanceBinStatistics : [],
    waveforms,
    artifacts,
    resultPackage: raw.resultPackage || null,
    resultFiles: raw.resultFiles || null,
    summary: raw.summary || null,
    sourceLabel: raw.sourceLabel || "NIED K-NET + ZSL",
    sourceDetail: raw.sourceDetail || "K-NET 实测 + 本项目推测",
    // badges: raw.badges || ["K-NET 输入", "ZSL 推测结果", `${inferred.length} 场点`],
    badges: [],
    integrityNote: raw.integrityNote || `K-NET 实测记录生成 ${inferred.length} 个推测场点；推测结果是一次随机相位实现。`,
    provenance: publicProvenance(
      raw.provenance?.summary
        || raw.provenance?.source
        || `NIED K-NET · event ${raw.eventId || "0010061330"} · ${observed.length} stations`,
    ),
    provenanceDetail: raw.provenance || {},
    boundaryUrl: raw.boundaryUrl || null,
    basemapUrl: raw.basemapUrl || null,
    basemap: raw.basemap || null,
  };
}

function publicProvenance(value) {
  return String(value || "")
    .replace(/\s*·?\s*seeds?\b.*$/i, "")
    .trim();
}

function revokeObjectUrls() {
  for (const url of state.objectUrls) URL.revokeObjectURL(url);
  state.objectUrls = [];
}

function showLoading(message) {
  $("#loading").hidden = false;
  $("#loading span:last-child").textContent = message;
  $("#dashboard").hidden = true;
  $("#error-state").hidden = true;
}

function showError(title, message) {
  $("#loading").hidden = true;
  $("#dashboard").hidden = true;
  $("#error-state").hidden = false;
  $("#error-title").textContent = title;
  $("#error-message").textContent = message;
}

async function loadDemo(preferredEventId = null) {
  showLoading("正在装载地震事件目录…");
  revokeObjectUrls();
  state.catalogRequestToken += 1;
  try {
    const [catalogResponse, batchResponse] = await Promise.all([
      fetch("./demo/events/catalog.json", { cache: "no-store" }),
      fetch("./demo/batch/catalog.json", { cache: "no-store" }),
    ]);
    state.descriptors = [];
    const catalogs = [];
    if (catalogResponse.ok) catalogs.push(await catalogResponse.json());
    if (batchResponse.ok) catalogs.unshift(await batchResponse.json());
    const events = catalogs.flatMap((catalog) => Array.isArray(catalog.events) ? catalog.events : []);
    if (events.length) {
      state.catalogEvents = events;
      const switcher = $("#event-switcher");
      switcher.replaceChildren(...events.map((event, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        const time = String(event.originTime || "").slice(0, 16);
        const magnitudeType = event.magnitudeType || (/^\d{10}$/.test(String(event.eventId)) ? "Mj" : "M");
        const title = event.title ? ` · ${event.title}` : "";
        const targetCount = finite(event.candidateCount);
        const targetLabel = targetCount === null ? "" : ` · ${numberText(targetCount, 0)} 点`;
        option.textContent = `${time}${title} · ${magnitudeType} ${numberText(event.magnitude, 1)} · ${event.stationCount} 站${targetLabel}`;
        return option;
      }));
      $("#event-switcher-wrap").hidden = events.length < 2;
      const defaultEventId = catalogs.find((catalog) => catalog.defaultEventId)?.defaultEventId;
      const preferredIndex = preferredEventId === null
        ? -1
        : events.findIndex((event) => String(event.eventId) === String(preferredEventId));
      const defaultIndex = preferredIndex >= 0
        ? preferredIndex
        : Math.max(0, events.findIndex((event) => event.eventId === defaultEventId));
      await selectCatalogEvent(defaultIndex);
      return;
    }

    const eventResponse = await fetch("./demo/knet-demo.json", { cache: "no-store" });
    if (!eventResponse.ok) throw new Error("演示资产未找到");
    state.catalogEvents = [];
    $("#event-switcher-wrap").hidden = true;
    const event = normalizeDemo(await eventResponse.json());
    [state.boundary, state.basemap] = await Promise.all([
      loadBoundary(event.boundaryUrl || state.defaultBoundaryUrl),
      loadBasemap(event.basemapUrl),
    ]);
    await setCurrent(event);
  } catch (error) {
    showError(
      "地震项目结果未能载入",
      `${error.message}。请在项目目录运行 “python local_archive_server.py --port 8000”，再打开 http://127.0.0.1:8000。`,
    );
  }
}

async function loadBoundary(url) {
  if (!url) return null;
  if (!state.boundaryCache.has(url)) {
    state.boundaryCache.set(url, fetch(url).then(async (response) => {
      if (!response.ok) throw new Error(`底图 HTTP ${response.status}`);
      return response.json();
    }));
  }
  try {
    return await state.boundaryCache.get(url);
  } catch (error) {
    state.boundaryCache.delete(url);
    console.warn(`底图 ${url} 读取失败`, error);
    return null;
  }
}

async function loadBasemap(url) {
  if (!url) return null;
  if (!state.basemapCache.has(url)) {
    while (state.basemapCache.size >= 3) {
      state.basemapCache.delete(state.basemapCache.keys().next().value);
    }
    state.basemapCache.set(url, fetch(url).then(async (response) => {
      if (!response.ok) throw new Error(`行政区划底图 HTTP ${response.status}`);
      const payload = await response.json();
      if (payload?.type !== "FeatureCollection" || !Array.isArray(payload.features)) {
        throw new Error("行政区划底图不是 GeoJSON FeatureCollection");
      }
      return payload;
    }));
  }
  try {
    return await state.basemapCache.get(url);
  } catch (error) {
    state.basemapCache.delete(url);
    console.warn(`行政区划底图 ${url} 读取失败`, error);
    return null;
  }
}

function setImportStatus(message, isError = false) {
  const status = $("#import-status");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

function refreshImportStartButton() {
  $("#import-start-button").disabled = !state.importServiceReady
    || !state.importSelection
    || !state.targetConfig
    || state.importActive;
}

function setTargetControlsDisabled(disabled) {
  $$("#target-config input, #target-config select, #target-config textarea")
    .forEach((control) => { control.disabled = disabled; });
}

function updateTargetConfig() {
  const mode = $("#target-mode").value;
  $$('[data-target-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.targetPanel !== mode;
  });
  const status = $("#target-config-status");
  try {
    if (mode === "grid") {
      state.targetConfig = normalizeGridTarget($("#target-grid-rows").value, $("#target-grid-columns").value);
      status.textContent = `${(state.targetConfig.rows * state.targetConfig.columns).toLocaleString("zh-CN")} 个规则网格推测点`;
    } else if (mode === "csv") {
      if (state.targetCsvError) throw state.targetCsvError;
      if (state.targetCsvText === null) throw new Error("请选择经纬度 CSV 文件");
      const points = parseTargetCsv(state.targetCsvText);
      state.targetConfig = { mode: "custom", source: "csv", points };
      status.textContent = `${points.length.toLocaleString("zh-CN")} 个 CSV 推测点`;
    } else if (mode === "manual") {
      const points = parseManualTargets($("#target-manual-input").value);
      state.targetConfig = { mode: "custom", source: "manual", points };
      status.textContent = `${points.length.toLocaleString("zh-CN")} 个手动推测点`;
    } else {
      throw new Error("未知的推测点生成方式");
    }
    status.classList.remove("is-error");
  } catch (error) {
    state.targetConfig = null;
    status.textContent = error.message;
    status.classList.add("is-error");
  }
  refreshImportStartButton();
}

function inspectRawFolder(fileList) {
  const entries = Array.from(fileList || [], (file) => ({
    file,
    path: String(file.webkitRelativePath || file.name || "")
      .replace(/\\/g, "/")
      .replace(/^\/+/, "")
      .replace(/\/{2,}/g, "/"),
  })).filter(({ path }) => path);
  if (!entries.length) throw new Error("所选目录中没有文件");

  const roots = new Set(entries.map(({ path }) => path.split("/")[0]));
  if (roots.size !== 1) throw new Error("一次只能选择一个事件的外层目录");
  const rootName = [...roots][0];
  const files = entries
    .filter(({ path }) => path.toLowerCase().endsWith(".dat"))
    .sort((a, b) => a.path.localeCompare(b.path));
  if (!files.length) throw new Error("所选目录中没有 .dat 原始记录");
  if (!files.some(({ path }) => path.split("/").slice(1, -1).some((part) => rawDataFolders.has(part.toLowerCase())))) {
    throw new Error("请选择 EIdata、HNdata 或 ElData 的外层事件目录");
  }
  const uniquePaths = new Set(files.map(({ path }) => path));
  if (uniquePaths.size !== files.length) throw new Error("所选目录中存在重复文件路径");
  return {
    rootName,
    files,
    ignoredFiles: entries.length - files.length,
    totalBytes: d3.sum(files, ({ file }) => file.size),
  };
}

function renderImportSelection(selection) {
  $("#import-selection").hidden = false;
  $("#import-root-name").textContent = selection.rootName;
  $("#import-file-count").textContent = `${selection.files.length.toLocaleString("zh-CN")} 个`;
  $("#import-file-size").textContent = byteText(selection.totalBytes);
  const upload = $("#upload-progress");
  upload.max = Math.max(1, selection.files.length);
  upload.value = 0;
  $("#upload-progress-note").textContent = "等待开始";
  $("#process-progress").value = 0;
  $("#process-progress-note").textContent = "等待上传";
  const ignored = selection.ignoredFiles ? `；已忽略 ${selection.ignoredFiles} 个非 DAT 文件` : "";
  setImportStatus(`目录预检通过${ignored}。服务端仍会核验头段、三分量完整性与台站几何。`);
}

async function checkImportService() {
  state.importServiceReady = false;
  $("#import-choose-button").disabled = true;
  $("#import-start-button").disabled = true;
  $("#import-service-note").textContent = "正在检查本机处理服务…";
  try {
    const health = await apiJson("/api/health");
    if (health.restartRequired) {
      $("#import-service-note").textContent =
        "本机处理代码已更新，当前服务仍是旧版本。请关闭原启动窗口并重新运行启动脚本。";
      setImportStatus("服务重启前不会接收或处理新事件，避免生成旧版图件。", true);
      return;
    }
    state.importServiceReady = health.status === "ok";
    const mapMode = health.administrativeBasemapConfigured
      ? "全国省、市、县三级行政区划底图已配置"
      : health.tiandituConfigured
        ? "天地图总包已配置，但未找到行政区划数据，将使用边界回退"
        : "未配置行政区划底图，将使用边界回退";
    $("#import-service-note").textContent = `本机处理服务已连接；${mapMode}。`;
    $("#import-choose-button").disabled = false;
    refreshImportStartButton();
    if (!state.importSelection) {
      $("#import-selection").hidden = true;
      setImportStatus("请选择一个外层事件目录，其中至少包含 EIdata、HNdata 或 ElData。");
    }
  } catch (error) {
    $("#import-service-note").textContent = "本机处理服务不可用。请使用 Windows 启动脚本打开本网站。";
    setImportStatus(error.message, true);
  }
}

async function openImportDialog() {
  const dialog = $("#import-dialog");
  if (!dialog.open) dialog.showModal();
  if (!state.importActive) await checkImportService();
}

function chooseRawFolder(fileList) {
  try {
    state.importSelection = inspectRawFolder(fileList);
    renderImportSelection(state.importSelection);
    refreshImportStartButton();
  } catch (error) {
    state.importSelection = null;
    $("#import-selection").hidden = true;
    refreshImportStartButton();
    setImportStatus(error.message, true);
  }
}

function renderImportJob(job) {
  const progress = job.progress && typeof job.progress === "object" ? job.progress : null;
  const progressNode = $("#process-progress");
  progressNode.max = 100;
  if (progress) {
    const percent = Math.max(0, Math.min(100, finite(progress.percent) ?? 0));
    progressNode.value = percent;
    const parts = [];
    const stageIndex = finite(progress.stageIndex);
    const stageCount = finite(progress.stageCount);
    if (stageIndex !== null && stageCount !== null) parts.push(`阶段 ${stageIndex}/${stageCount}`);
    if (progress.label) parts.push(String(progress.label));
    const completed = finite(progress.completed);
    const total = finite(progress.total);
    if (completed !== null && total !== null) {
      const unit = progress.unit ? ` ${progress.unit}` : "";
      parts.push(`${numberText(completed, 0)}/${numberText(total, 0)}${unit}`);
    }
    const accepted = finite(progress.accepted);
    const skipped = finite(progress.skipped);
    if (accepted !== null || skipped !== null) {
      parts.push(`通过 ${numberText(accepted, 0)} · 跳过 ${numberText(skipped, 0)}`);
    }
    parts.push(`${numberText(percent, 1)}%`);
    const elapsed = elapsedText(progress.elapsedSeconds);
    if (elapsed) parts.push(`已用 ${elapsed}`);
    $("#process-progress-note").textContent = parts.join(" · ");
  } else {
    const fallback = { queued: 5, inspecting: 15, processing: 45, success: 100, failed: 100 }[job.status] || 0;
    progressNode.value = fallback;
    $("#process-progress-note").textContent = {
      queued: "等待计算",
      inspecting: "输入质控",
      processing: "正在计算",
      success: "已归档",
      failed: "处理失败",
    }[job.status] || job.status || "等待处理";
  }
  setImportStatus(job.error || progress?.message || job.message || "正在处理…", job.status === "failed");
}

async function waitForImportJob(jobId) {
  while (true) {
    const job = await apiJson(`/api/imports/${encodeURIComponent(jobId)}`);
    renderImportJob(job);
    if (["success", "failed"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

async function uploadImportFiles(jobId, selection) {
  let nextIndex = 0;
  let uploadedFiles = 0;
  let uploadedBytes = 0;
  let firstError = null;
  const progress = $("#upload-progress");
  progress.max = Math.max(1, selection.files.length);

  async function worker() {
    while (!firstError) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= selection.files.length) return;
      const row = selection.files[index];
      try {
        await apiJson(`/api/imports/${encodeURIComponent(jobId)}/file?path=${encodeURIComponent(row.path)}`, {
          method: "PUT",
          body: row.file,
        });
        uploadedFiles += 1;
        uploadedBytes += row.file.size;
        progress.value = uploadedFiles;
        $("#upload-progress-note").textContent = `${uploadedFiles}/${selection.files.length} · ${byteText(uploadedBytes)}/${byteText(selection.totalBytes)}`;
      } catch (error) {
        firstError = error;
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(4, selection.files.length) }, () => worker()));
  if (firstError) throw firstError;
}

async function startRawImport() {
  const selection = state.importSelection;
  const targetConfig = state.targetConfig;
  if (!selection || !targetConfig || !state.importServiceReady || state.importActive) return;
  state.importActive = true;
  $("#import-choose-button").disabled = true;
  setTargetControlsDisabled(true);
  refreshImportStartButton();
  $("#upload-progress").value = 0;
  $("#upload-progress-note").textContent = "正在建立任务";
  $("#process-progress").value = 0;
  $("#process-progress-note").textContent = "等待上传";
  setImportStatus("正在建立导入任务…");
  try {
    const job = await apiJson("/api/imports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rootName: selection.rootName,
        files: selection.files.map(({ file, path }) => ({
          path,
          size: file.size,
          lastModified: file.lastModified || 0,
        })),
      }),
    });
    setImportStatus(`正在上传 ${selection.files.length.toLocaleString("zh-CN")} 个原始记录…`);
    await uploadImportFiles(job.jobId, selection);
    $("#upload-progress-note").textContent = `${selection.files.length}/${selection.files.length} · 上传完成`;
    const queued = await apiJson(`/api/imports/${encodeURIComponent(job.jobId)}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targetConfig }),
    });
    renderImportJob(queued);
    const completed = await waitForImportJob(job.jobId);
    if (completed.status !== "success") throw new Error(completed.error || completed.message || "处理失败");
    const eventId = completed.eventIds?.[0];
    setImportStatus("处理完成，正在打开归档结果…");
    state.importSelection = null;
    $("#import-selection").hidden = true;
    await loadDemo(eventId || null);
    if ($("#import-dialog").open) $("#import-dialog").close();
  } catch (error) {
    setImportStatus(error.message, true);
  } finally {
    state.importActive = false;
    $("#import-choose-button").disabled = !state.importServiceReady;
    setTargetControlsDisabled(false);
    refreshImportStartButton();
  }
}

function renderArchives(records) {
  const list = $("#archive-list");
  list.replaceChildren();
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "archive-empty";
    empty.textContent = "还没有导入归档。";
    list.append(empty);
    return;
  }

  for (const record of records) {
    const row = document.createElement("article");
    row.className = "archive-row";
    const content = document.createElement("div");
    const status = document.createElement("span");
    const succeeded = record.status === "success";
    status.className = `archive-status ${succeeded ? "is-success" : "is-failed"}`;
    status.textContent = succeeded ? "成功" : "失败";
    const title = document.createElement("strong");
    title.textContent = record.title || record.rootName || record.eventId || "未命名事件";
    const meta = document.createElement("small");
    const details = [
      dateTimeText(record.completedAt || record.createdAt),
      record.inputFiles === undefined ? null : `${Number(record.inputFiles).toLocaleString("zh-CN")} 个文件`,
      record.inputBytes === undefined ? null : byteText(record.inputBytes),
      succeeded ? `${numberText(record.observedStations, 0)} 站 · ${numberText(record.inferredPoints, 0)} 推测点` : null,
    ].filter(Boolean);
    meta.textContent = details.join(" · ");
    content.append(status, title, meta);
    if (!succeeded && record.error) {
      const error = document.createElement("p");
      error.textContent = record.error;
      content.append(error);
    }
    row.append(content);
    if (succeeded && record.eventId) {
      const open = document.createElement("button");
      open.className = "button button-quiet";
      open.type = "button";
      open.textContent = "打开成果";
      open.addEventListener("click", async () => {
        $("#archive-dialog").close();
        await loadDemo(record.eventId);
      });
      row.append(open);
    }
    list.append(row);
  }
}

async function refreshArchives() {
  const list = $("#archive-list");
  const loading = document.createElement("p");
  loading.className = "archive-empty";
  loading.textContent = "正在读取归档…";
  list.replaceChildren(loading);
  try {
    const payload = await apiJson("/api/archives");
    renderArchives(Array.isArray(payload.archives) ? payload.archives : []);
  } catch (error) {
    loading.className = "archive-empty is-error";
    loading.textContent = error.message;
  }
}

async function openArchiveDialog() {
  const dialog = $("#archive-dialog");
  if (!dialog.open) dialog.showModal();
  await refreshArchives();
}

function setWatchFeedback(message, isError = false) {
  const feedback = $("#watch-feedback");
  feedback.textContent = message;
  feedback.classList.toggle("is-error", isError);
}

function renderWatchMessages(messages) {
  const list = $("#watch-message-list");
  list.replaceChildren();
  const recent = Array.isArray(messages) ? messages.slice(-8).reverse() : [];
  if (!recent.length) {
    const item = document.createElement("li");
    item.textContent = "暂无监控消息";
    list.append(item);
    return;
  }
  for (const row of recent) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = row.time || "";
    time.textContent = dateTimeText(row.time);
    const message = document.createElement("span");
    message.textContent = row.message || "—";
    item.append(time, message);
    list.append(item);
  }
}

function renderWatchQueue(queue, counts = {}) {
  const list = $("#watch-job-list");
  list.replaceChildren();
  const rows = Array.isArray(queue) ? queue : [];
  $("#watch-queue-summary").textContent = [
    `等待 ${numberText(counts.waiting, 0)}`,
    `处理中 ${numberText(counts.processing, 0)}`,
    `完成 ${numberText(counts.success, 0)}`,
    `失败 ${numberText(counts.failed, 0)}`,
  ].join(" · ");
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "watch-job-empty";
    empty.textContent = "当前没有等待或处理中的监控事件。";
    list.append(empty);
    return;
  }

  const statusLabels = {
    uploading: "归档输入",
    stabilizing: "等待稳定",
    waiting: "排队中",
    queued: "排队中",
    inspecting: "输入质控",
    processing: "处理中",
    success: "已完成",
    failed: "失败",
  };
  for (const job of rows) {
    const progress = job.progress || {};
    const percent = Math.max(0, Math.min(100, finite(progress.percent) ?? 0));
    const row = document.createElement("article");
    row.className = `watch-job is-${statusLabels[job.status] ? job.status : "queued"}`;

    const heading = document.createElement("div");
    heading.className = "watch-job-head";
    const title = document.createElement("strong");
    title.textContent = job.event || "未命名事件";
    const badge = document.createElement("span");
    badge.className = "watch-job-status";
    badge.textContent = statusLabels[job.status] || "排队中";
    heading.append(title, badge);

    const meta = document.createElement("small");
    const details = [];
    if (progress.label) details.push(progress.label);
    if (finite(progress.stageIndex) !== null && finite(progress.stageCount) !== null) {
      details.push(`阶段 ${progress.stageIndex}/${progress.stageCount}`);
    }
    if (finite(progress.completed) !== null && finite(progress.total) !== null) {
      details.push(`${numberText(progress.completed, 0)}/${numberText(progress.total, 0)} ${progress.unit || "项"}`);
    }
    const elapsed = elapsedText(progress.elapsedSeconds);
    if (elapsed) details.push(`已用 ${elapsed}`);
    meta.textContent = details.join(" · ") || dateTimeText(job.updatedAt || job.createdAt);

    const bar = document.createElement("progress");
    bar.max = 100;
    bar.value = percent;
    bar.setAttribute("aria-label", `${title.textContent}：${percent}%`);

    const note = document.createElement("div");
    note.className = "watch-job-note";
    const message = document.createElement("span");
    message.textContent = job.error || progress.message || job.message || "等待处理";
    const value = document.createElement("b");
    value.textContent = `${numberText(percent, 1)}%`;
    note.append(message, value);
    row.append(heading, meta, bar, note);
    list.append(row);
  }
}

function renderWatchStatus(watch, syncForm = false) {
  const running = watch.status === "running";
  $("#watch-state").textContent = running ? "监控中" : "已停止";
  $("#watch-state").classList.toggle("is-running", running);
  $("#watch-detected").textContent = numberText(watch.detectedEvents, 0);
  $("#watch-pending").textContent = numberText(watch.queueCounts?.waiting ?? watch.pendingEvents, 0);
  $("#watch-last-event").textContent = watch.lastEvent || "—";
  $("#watch-last-scan").textContent = watch.lastScanAt ? dateTimeText(watch.lastScanAt) : "—";
  renderWatchMessages(watch.messages);
  renderWatchQueue(watch.queue, watch.queueCounts);

  const path = $("#watch-path");
  const stable = $("#watch-stable-seconds");
  const existing = $("#watch-process-existing");
  if (syncForm) {
    if (watch.root) path.value = watch.root;
    if (finite(watch.stableSeconds) !== null) stable.value = String(watch.stableSeconds);
    existing.checked = Boolean(watch.processExisting);
  }
  path.disabled = running;
  stable.disabled = running;
  existing.disabled = running;
  $("#watch-browse-button").disabled = running;
  $("#watch-start-button").disabled = running;
  $("#watch-stop-button").disabled = !running;
  if (watch.error) setWatchFeedback(watch.error, true);
  else if (running) setWatchFeedback(`正在监控 ${watch.root}；完成结果会自动进入归档记录。`);
  else setWatchFeedback("监控已停止。默认只处理启动后新增或发生变化的事件目录。");
}

async function refreshWatchStatus(syncForm = false) {
  try {
    renderWatchStatus(await apiJson("/api/watch"), syncForm);
  } catch (error) {
    $("#watch-start-button").disabled = true;
    $("#watch-stop-button").disabled = true;
    setWatchFeedback(error.message, true);
  }
}

function stopWatchPolling() {
  if (state.watchTimer !== null) clearInterval(state.watchTimer);
  state.watchTimer = null;
}

function startWatchPolling() {
  stopWatchPolling();
  state.watchTimer = setInterval(() => {
    if ($("#watch-dialog").open) refreshWatchStatus();
  }, 3000);
}

async function openWatchDialog() {
  const dialog = $("#watch-dialog");
  if (!dialog.open) dialog.showModal();
  await refreshWatchStatus(true);
  startWatchPolling();
}

async function chooseWatchFolder() {
  const path = $("#watch-path");
  const button = $("#watch-browse-button");
  button.disabled = true;
  setWatchFeedback("正在打开本机文件夹选择器…");
  try {
    const selection = await apiJson("/api/folders/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initialPath: path.value.trim() || null }),
    });
    if (selection.path) {
      path.value = selection.path;
      path.setCustomValidity("");
      setWatchFeedback(`已选择监控目录：${selection.path}`);
    } else {
      setWatchFeedback("未选择目录。");
    }
  } catch (error) {
    setWatchFeedback(error.message, true);
  } finally {
    button.disabled = path.disabled;
  }
}

async function startFolderWatch() {
  const path = $("#watch-path");
  const stable = $("#watch-stable-seconds");
  path.setCustomValidity(path.value.trim() ? "" : "请先选择本机监控根目录");
  if (!path.reportValidity() || !stable.reportValidity()) return;
  $("#watch-start-button").disabled = true;
  setWatchFeedback("正在启动文件夹监控…");
  try {
    const watch = await apiJson("/api/watch/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: path.value.trim(),
        stableSeconds: stable.valueAsNumber,
        processExisting: $("#watch-process-existing").checked,
      }),
    });
    renderWatchStatus(watch, true);
  } catch (error) {
    $("#watch-start-button").disabled = false;
    setWatchFeedback(error.message, true);
  }
}

async function stopFolderWatch() {
  $("#watch-stop-button").disabled = true;
  setWatchFeedback("正在停止文件夹监控…");
  try {
    const watch = await apiJson("/api/watch/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    renderWatchStatus(watch, true);
  } catch (error) {
    $("#watch-stop-button").disabled = false;
    setWatchFeedback(error.message, true);
  }
}

async function openFiles(fileList) {
  showLoading("正在检查结果目录…");
  revokeObjectUrls();
  state.catalogRequestToken += 1;
  try {
    const descriptors = await buildEvents(fileList);
    if (!descriptors.length) throw new Error("没有找到 config.txt；请选择单个 *_infer 结果目录或包含这些目录的上级文件夹");
    state.descriptors = descriptors;
    state.catalogEvents = [];
    const switcher = $("#event-switcher");
    switcher.replaceChildren(...descriptors.map((descriptor, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = descriptor.name;
      return option;
    }));
    $("#event-switcher-wrap").hidden = descriptors.length < 2;
    await selectDescriptor(0);
  } catch (error) {
    showError("结果目录无法使用", error.message);
  }
}

async function selectCatalogEvent(index) {
  const entry = state.catalogEvents[index];
  if (!entry) return;
  const requestToken = ++state.catalogRequestToken;
  showLoading(`正在读取 ${entry.eventId} 的项目结果…`);
  $("#event-switcher").value = String(index);
  try {
    const response = await fetch(entry.url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const event = normalizeDemo(await response.json());
    const [boundary, basemap] = await Promise.all([
      loadBoundary(event.boundaryUrl || entry.boundaryUrl || state.defaultBoundaryUrl),
      loadBasemap(event.basemapUrl || entry.basemapUrl),
    ]);
    if (requestToken !== state.catalogRequestToken) return;
    state.boundary = boundary;
    state.basemap = basemap;
    await setCurrent(event);
  } catch (error) {
    if (requestToken !== state.catalogRequestToken) return;
    showError("事件结果未能载入", `${entry.eventId} 的结果文件不存在或无法读取（${error.message}）。`);
  }
}

async function selectDescriptor(index) {
  const descriptor = state.descriptors[index];
  if (!descriptor) return;
  state.catalogRequestToken += 1;
  showLoading(`正在读取 ${descriptor.name}…`);
  state.boundary = null;
  state.basemap = null;
  $("#event-switcher").value = String(index);
  await setCurrent(await fromDescriptor(descriptor));
}

async function setCurrent(event) {
  state.waveformToken += 1;
  state.current = event;
  state.selectedPoint = event.inferred.length
    ? event.inferred.reduce((best, point) => (finite(point.intensity) > finite(best.intensity) ? point : best))
    : event.observed[0] || null;
  $("#map-metric").value = event.inferred.length ? "intensity" : "PGA";
  $("#loading").hidden = true;
  $("#error-state").hidden = true;
  $("#dashboard").hidden = false;
  renderEventText();
  setupWaveformSelect();
  switchView(state.activeView);
}

function renderEventText() {
  const event = state.current;
  if (!event) return;
  const config = event.config;
  const lon = firstFinite(config.e_lon, config.source_longitude_deg, config.longitude);
  const lat = firstFinite(config.e_lat, config.source_latitude_deg, config.latitude);

  $("#event-title").textContent = event.title;
  $("#source-badges").replaceChildren(...event.badges.map((label, index) => {
    const badge = document.createElement("span");
    badge.className = `badge${index === 1 ? " badge-accent" : ""}`;
    badge.textContent = label;
    return badge;
  }));
  $("#fact-time").textContent = event.originTime;
  const place = eventPlaceFromTitle(event.title);
  $("#fact-location").textContent = place
    ? `${place} · ${coordinateText(lon, lat)}`
    : coordinateText(lon, lat);

  $("#metric-observed").textContent = numberText(event.observed.length, 0);
  $("#metric-inferred").textContent = event.inferred.length ? numberText(event.inferred.length, 0) : "未计算";
  const gridColumns = firstFinite(config.number_horizontal);
  const gridRows = firstFinite(config.number_vertical);
  const candidatePoints = firstFinite(
    event.summary?.candidateTargetPoints,
    event.summary?.candidateGridPoints,
  );
  const skippedPoints = firstFinite(event.summary?.skippedPoints);
  $("#metric-inferred-note").textContent = event.inferred.length
    ? (candidatePoints
      ? `${numberText(event.inferred.length, 0)} / ${numberText(candidatePoints, 0)} 通过 · ${numberText(skippedPoints, 0)} 跳过`
      : (gridColumns && gridRows ? `${numberText(gridColumns, 0)} × ${numberText(gridRows, 0)} 网格` : `${numberText(event.inferred.length, 0)} 个场点`))
    : "no inferred output";

  const maxPga = event.inferred.length
    ? event.inferred.reduce((best, point) => (finite(point.PGA) > finite(best.PGA) ? point : best))
    : null;
  const maxIntensity = event.inferred.length
    ? event.inferred.reduce((best, point) => (finite(point.intensity) > finite(best.intensity) ? point : best))
    : null;
  $("#metric-max-pga").textContent = maxPga ? numberText(maxPga.PGA, 2) : "未计算";
  $("#metric-max-pga-note").textContent = maxPga
    ? `cm/s² · ${coordinateText(maxPga.lon, maxPga.lat)}`
    : "no inferred output";
  $("#metric-max-intensity").textContent = maxIntensity ? numberText(maxIntensity.intensity, 1) : "未计算";
  $("#metric-max-intensity-note").textContent = maxIntensity
    ? coordinateText(maxIntensity.lon, maxIntensity.lat)
    : "no inferred output";

  const download = $("#result-download");
  download.hidden = !event.resultPackage?.url;
  if (event.resultPackage?.url) {
    download.href = event.resultPackage.url;
    download.download = event.resultPackage.filename || "project-result.zip";
    const megabytes = finite(event.resultPackage.bytes) / 1024 / 1024;
    const datCount = finite(event.provenanceDetail?.outputs?.datFileCount);
    download.textContent = Number.isFinite(megabytes)
      ? `下载完整结果 · ${megabytes.toFixed(1)} MB`
      : "下载完整结果";
    download.title = [
      event.resultPackage.sha256 ? `SHA-256 ${event.resultPackage.sha256}` : "",
      datCount ? `包含 ${numberText(datCount, 0)} 个推测时程 DAT` : "",
    ].filter(Boolean).join(" · ");
  }

  const hasBoth = event.observed.length && event.inferred.length;
  $("#workspace-title").textContent = hasBoth ? "实测台站与推测场点" : event.observed.length ? "实测台站" : "推测场点";
  $("#load-status").textContent = `${event.observed.length + event.inferred.length} 个空间记录已就绪`;
  $("#footer-provenance").textContent = event.provenance;
}

function switchView(view) {
  if (!viewMeta[view] || (view !== "ml" && !state.current)) return;
  state.activeView = view;
  $$(".nav-item").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $$(".view").forEach((section) => {
    const active = section.id === `view-${view}`;
    section.hidden = !active;
    section.classList.toggle("is-active", active);
  });
  $("#workspace-eyebrow").textContent = viewMeta[view][0];
  $("#workspace-title").textContent = view === "map" && state.current
    ? (state.current.observed.length && state.current.inferred.length ? "实测台站与推测场点" : state.current.observed.length ? "实测台站" : "推测场点")
    : viewMeta[view][1];
  $("#event-metrics").hidden = view === "ml";
  if (view === "ml") {
    $("#load-status").textContent = "正在读取遮蔽台站试验";
    void renderMlPilot();
    return;
  }
  $("#load-status").textContent = `${state.current.observed.length + state.current.inferred.length} 个空间记录已就绪`;

  if (view === "map") renderMap();
  if (view === "attenuation") renderAttenuation();
  if (view === "waveforms") loadSelectedWaveform();
  if (view === "artifacts") renderArtifacts();
}

function pilotMetric(metrics, model) {
  const row = metrics.find((metric) => metric.model === model);
  if (!row || finite(row.mae_log10) === null) throw new Error(`缺少 ${model} 指标`);
  return row;
}

function pilotMetricWhere(metrics, predicate, label) {
  const row = metrics.find(predicate);
  if (!row || finite(row.mae_log10) === null) throw new Error(`缺少 ${label} 指标`);
  return row;
}

function fetchPilotSummary(path, label) {
  return fetch(path).then(async (response) => {
    if (!response.ok) throw new Error(`${label}摘要未找到`);
    return response.json();
  });
}

function resultNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  return finite(value);
}

function countText(value) {
  const number = resultNumber(value);
  return number === null ? "—" : Math.round(number).toLocaleString("zh-CN");
}

function maeText(metric) {
  return Number(metric.mae_log10).toFixed(3);
}

function reductionText(reference, candidate) {
  const baseline = finite(reference.mae_log10);
  const corrected = finite(candidate.mae_log10);
  if (baseline === null || corrected === null || baseline === 0) return "—";
  return (100 * (baseline - corrected) / baseline).toFixed(1);
}

function confidenceIntervalText(interval) {
  if (!Array.isArray(interval) || interval.length !== 2 || interval.some((value) => resultNumber(value) === null)) return "未记录";
  return `${Number(interval[0]).toFixed(3)} 至 ${Number(interval[1]).toFixed(3)}`;
}

function maximumIfComplete(values) {
  const numbers = values.map(resultNumber);
  return numbers.every((value) => value !== null) ? Math.max(...numbers) : null;
}

function bestValidationCandidate(candidates, usesJshis) {
  return candidates
    .filter((candidate) => Number(candidate.uses_jshis) === Number(usesJshis) && resultNumber(candidate.validation_mae_log10) !== null)
    .reduce((best, candidate) => !best || candidate.validation_mae_log10 < best.validation_mae_log10 ? candidate : best, null);
}

function comparisonMetric(metrics, modelId) {
  const row = (metrics || []).find((metric) => metric.model_id === modelId);
  if (!row || finite(row.mae_log10) === null) throw new Error(`统一比较缺少 ${modelId} 指标`);
  return row;
}

function renderUnifiedStep3(summary) {
  const cohort = summary.cohort || {};
  const coverage = summary.zslCoverage || {};
  const known = summary.evaluations?.knownStation2006 || {};
  const strictResult = summary.evaluations?.strictEventStation2006 || {};
  const knownCurrent = comparisonMetric(known.pairedMetrics, "current");
  const knownZsl = comparisonMetric(known.pairedMetrics, "zsl");
  const strictCurrent = comparisonMetric(strictResult.pairedMetrics, "current");
  const strictZsl = comparisonMetric(strictResult.pairedMetrics, "zsl");
  const rate = finite(coverage.rate);
  const fullZslMae = finite(coverage.processedMetrics?.mae_log10);
  const knownCommonRate = finite(known.commonSubset?.coverage);
  const strictCommonRate = finite(strictResult.commonSubset?.coverage);

  $("#unified-step3-size").textContent = `${countText(cohort.records)} / ${countText(cohort.events)}`;
  $("#unified-step3-size-note").textContent = `${countText(cohort.stations)} 个台站；100 Hz ${countText(cohort.samplingRateCounts?.["100"])}，200 Hz ${countText(cohort.samplingRateCounts?.["200"])}`;
  $("#unified-zsl-coverage").textContent = `${countText(coverage.processed)} / ${countText(coverage.eligible)}`;
  $("#unified-zsl-coverage-note").textContent = `覆盖率 ${rate === null ? "—" : `${(100 * rate).toFixed(1)}%`}；可计算目标 MAE ${fullZslMae === null ? "—" : fullZslMae.toFixed(3)}；其余记录保留 QC 原因`;
  $("#unified-known-mae").textContent = `${maeText(knownCurrent)} / ${maeText(knownZsl)}`;
  $("#unified-known-note").textContent = `${countText(known.commonSubset?.records)} / ${countText(known.eligible?.records)}（${knownCommonRate === null ? "—" : `${(100 * knownCommonRate).toFixed(1)}%`}）几何可计算目标`;
  $("#unified-strict-mae").textContent = `${maeText(strictCurrent)} / ${maeText(strictZsl)}`;
  $("#unified-strict-note").textContent = `${countText(strictResult.commonSubset?.records)} / ${countText(strictResult.eligible?.records)}（${strictCommonRate === null ? "—" : `${(100 * strictCommonRate).toFixed(1)}%`}）几何可计算目标`;

  $("#unified-step3-boundary").textContent = `全部 ${countText(cohort.records)} 条记录使用同一目标版本 ${summary.target?.version || "—"}。当前模型的全部目标指标与 ZSL 可计算共同子集分开报告；ZSL-5 只在目标位于遮蔽上下文凸包内且有恰好 5 个自然邻站时给出预测。根目录 Step2.1 使用“最多 5 个”邻站，因此这里明确标为带项目 QC 的泄漏控制适配。`;

  const knownAudit = known.pairedBootstrapCurrentMinusZsl || {};
  const strictAudit = strictResult.pairedBootstrapCurrentMinusZsl || {};
  const knownDifference = finite(knownAudit.meanDifferenceLog10);
  const strictDifference = finite(strictAudit.recordMeanDifferenceLog10);
  const strictGeometry = strictResult.geometrySelectionDiagnostic || {};
  const acceptedDistance = finite(strictGeometry.processed?.medianSourceDistanceKm);
  const skippedDistance = finite(strictGeometry.skipped?.medianSourceDistanceKm);
  const acceptedContext = finite(strictGeometry.processed?.medianContextStations);
  const skippedContext = finite(strictGeometry.skipped?.medianContextStations);
  $("#unified-step3-audit").textContent = `已知台站共同子集：当前模型减 ZSL 的事件宏观 MAE 差为 ${knownDifference === null ? "—" : knownDifference.toFixed(3)} log10，95% 区间 ${confidenceIntervalText(knownAudit.ci95)}。严格事件×台站共同子集：记录平均差 ${strictDifference === null ? "—" : strictDifference.toFixed(3)}，双向 bootstrap 95% 区间 ${confidenceIntervalText([strictAudit.ci95LowLog10, strictAudit.ci95HighLog10])}。严格共同子集存在几何选择：通过 / 跳过记录的震源距中位数为 ${acceptedDistance === null ? "—" : acceptedDistance.toFixed(1)} / ${skippedDistance === null ? "—" : skippedDistance.toFixed(1)} km，上下文台站中位数为 ${acceptedContext === null ? "—" : acceptedContext.toFixed(0)} / ${skippedContext === null ? "—" : skippedContext.toFixed(0)}。负值表示当前模型误差更低。`;
  $("#unified-step3-caption").textContent = `${countText(coverage.processed)} 条全量 ZSL 预测；2006 已知台站共同目标 ${countText(known.commonSubset?.records)} 条，严格共同目标 ${countText(strictResult.commonSubset?.records)} 条`;
}

function renderResidualResearch(summary) {
  const data = summary.data || {};
  const metrics = summary.metrics || [];
  const fixed = pilotMetric(metrics, "fixed attenuation");
  const site = pilotMetric(metrics, "site history + local residual");
  const selected = pilotMetricWhere(metrics, (metric) => String(metric.model).startsWith("validation-selected"), "validation-selected model");
  const selection = summary.selection || {};

  $("#residual-research-size").textContent = `${countText(data.testRecords2006)} / ${countText(data.testEvents2006)}`;
  const historyCoverage = resultNumber(data.stationHistoryCoverage2006);
  $("#residual-research-size-note").textContent = `${countText(data.testStations2006)} 个台站；历史覆盖 ${historyCoverage === null ? "—" : `${(100 * historyCoverage).toFixed(1)}%`}`;
  $("#residual-research-fixed").textContent = maeText(fixed);
  $("#residual-research-site").textContent = maeText(site);
  $("#residual-research-site-note").textContent = `较固定衰减下降 ${reductionText(fixed, site)}%`;
  $("#residual-research-selected").textContent = maeText(selected);
  $("#residual-research-selected-note").textContent = `较固定衰减下降 ${reductionText(fixed, selected)}%`;

  const selectionWindow = selection.selectionYears
    ? "场地参数使用 2002–2005 年滚动验证，ML 与集成权重由 2005 年选定"
    : "选参窗口未记录";
  $("#residual-research-boundary").textContent = `目录覆盖 ${countText(data.records)} 条记录、${countText(data.events)} 个事件和 ${countText(data.stations)} 个台站；${selectionWindow}。2006 结果已经用于本轮方法迭代与解释，只作为回溯开发评估，不是确认性测试。`;

  const audit = summary.bootstrapSelectedMinusPreviousHgb || {};
  const meanDifference = resultNumber(audit.meanDifferenceLog10);
  $("#residual-research-audit").textContent = `验证选定模型相对上一版滚动历史 HGB 的事件块 bootstrap 平均 MAE 差为 ${meanDifference === null ? "—" : meanDifference.toFixed(3)} log10，95% 区间为 ${confidenceIntervalText(audit.ci95)}；${countText(audit.eventsImproved)}/${countText(audit.events)} 个事件的 MAE 降低。`;

  const jshis = summary.rootCauses?.jshisAblation || {};
  const retainedRaw = jshis.retainedBySelectedFeatureSet;
  const selectedFeatureSet = String(selection.hgb?.feature_set || "");
  const retained = typeof retainedRaw === "boolean" ? retainedRaw : selectedFeatureSet.includes("jshis") ? true : null;
  const retainedText = retained === true ? "验证后保留" : retained === false ? "验证后未保留" : "保留状态未记录";
  const withJshis = resultNumber(jshis.bestWithJshisValidationMae);
  const withoutJshis = resultNumber(jshis.bestWithoutJshisValidationMae);
  const validationText = withJshis === null || withoutJshis === null ? "未记录" : `${withJshis.toFixed(4)} / ${withoutJshis.toFixed(4)}`;
  const coverage = resultNumber(data.jshisCoverage2006);
  $("#residual-research-jshis").textContent = `J-SHIS：${retainedText}；验证 MAE（有 / 无 J-SHIS）为 ${validationText}，2006 目标覆盖率为 ${coverage === null ? "—" : `${(100 * coverage).toFixed(1)}%`}。`;
  $("#residual-research-caption").textContent = `2006 回溯开发评估：${countText(data.testRecords2006)} 个目标、${countText(data.testEvents2006)} 个事件、${countText(data.testStations2006)} 个台站（以具备历史记录者为主）`;
}

function renderStrictKnetHoldout(summary) {
  const data = summary.data || {};
  const metrics = summary.testMetrics || [];
  const fixed = pilotMetric(metrics, "fixed attenuation");
  const local = pilotMetric(metrics, "local residual");
  const selected = pilotMetricWhere(metrics, (metric) => String(metric.model).startsWith("validation-selected"), "validation-selected model");

  $("#strict-holdout-size").textContent = `${countText(data.testRows2006)} / ${countText(data.testEvents2006)}`;
  $("#strict-holdout-size-note").textContent = `${countText(data.testStations2006)} 个全新目标台站`;
  $("#strict-holdout-fixed").textContent = maeText(fixed);
  $("#strict-holdout-local").textContent = maeText(local);
  $("#strict-holdout-local-note").textContent = `较固定衰减下降 ${reductionText(fixed, local)}%`;
  $("#strict-holdout-selected").textContent = maeText(selected);
  $("#strict-holdout-selected-note").textContent = `较固定衰减下降 ${reductionText(fixed, selected)}%`;

  $("#strict-holdout-boundary").textContent = `训练集为 2001–2004 年 ${countText(data.trainingRows2001To2004)} 个目标，2005 年 ${countText(data.validationRows2005)} 个目标仅用于选择，2006 年 ${countText(data.testRows2006)} 个目标用于回溯严格开发评估。测试事件和目标台站均不与训练或验证重叠，目标台站的经验历史完全排除；该年份此前已被项目查看，不作为确认性测试。`;

  const overlap = summary.split?.groupOverlap || {};
  const eventOverlap = maximumIfComplete([
    overlap.train_validation_event_overlap,
    overlap.train_test_event_overlap,
    overlap.validation_test_event_overlap,
  ]);
  const stationOverlap = maximumIfComplete([
    overlap.train_validation_target_station_overlap,
    overlap.train_test_target_station_overlap,
    overlap.validation_test_target_station_overlap,
  ]);
  const contextOverlap = maximumIfComplete([
    overlap.validation_target_vs_context_overlap,
    overlap.test_target_vs_context_overlap,
  ]);
  const targetHistoryRows = resultNumber(overlap.target_history_nonzero_rows);
  $("#strict-holdout-overlap").textContent = `${countText(eventOverlap)} / ${countText(stationOverlap)}`;
  $("#strict-holdout-overlap-note").textContent = `训练、验证、测试之间的最大事件重叠为 ${countText(eventOverlap)}，最大目标台站重叠为 ${countText(stationOverlap)}；目标台站与同事件上下文台站重叠为 ${countText(contextOverlap)}，目标历史非零行数为 ${countText(targetHistoryRows)}。`;

  const audit = summary.twoWayBootstrapValidationSelectedMinusFixed || {};
  const probability = resultNumber(audit.bootstrapProbabilityImproved);
  const interval = [audit.ci95LowLog10, audit.ci95HighLog10];
  const localAudit = summary.twoWayBootstrapValidationSelectedMinusAnalyticLocal || {};
  const localInterval = [localAudit.ci95LowLog10, localAudit.ci95HighLog10];
  $("#strict-holdout-audit").textContent = `验证选定模型相对固定衰减的事件×台站双向 bootstrap MAE 差 95% 区间为 ${confidenceIntervalText(interval)} log10，改进概率为 ${probability === null ? "—" : `${(100 * probability).toFixed(1)}%`}；${countText(audit.eventsImproved)}/${countText(audit.eventsEvaluated)} 个事件、${countText(audit.stationsImproved)}/${countText(audit.stationsEvaluated)} 个台站改善。相对解析局部残差的 95% 区间为 ${confidenceIntervalText(localInterval)} log10。`;

  const candidates = summary.validationCandidates || [];
  const bestWithJshis = bestValidationCandidate(candidates, true);
  const bestWithoutJshis = bestValidationCandidate(candidates, false);
  const retainedRaw = summary.jshis?.retainedByValidation;
  const retainedText = retainedRaw === true ? "经 2005 验证后保留" : retainedRaw === false ? "未通过 2005 验证，未保留" : "验证保留状态未记录";
  const validationText = bestWithJshis && bestWithoutJshis
    ? `${Number(bestWithJshis.validation_mae_log10).toFixed(4)} / ${Number(bestWithoutJshis.validation_mae_log10).toFixed(4)}`
    : "未记录";
  const coverage = resultNumber(data.testJshisCoverage);
  const localHigh = resultNumber(localAudit.ci95HighLog10);
  const transferConclusion = localHigh === null
    ? "复杂模型相对解析局部残差的稳定性未记录。"
    : localHigh >= 0
      ? "验证选中的复杂模型在 2006 回溯集未稳定超过解析局部残差。"
      : "验证选中的复杂模型在 2006 回溯集中保持了对解析局部残差的优势。";
  $("#strict-holdout-jshis").textContent = `J-SHIS：${retainedText}；2005 最佳验证 MAE（有 / 无 J-SHIS）为 ${validationText}，2006 严格留出目标覆盖率为 ${coverage === null ? "—" : `${(100 * coverage).toFixed(1)}%`}。${transferConclusion}`;
  $("#strict-holdout-caption").textContent = `2006 事件×台站零重叠评估：${countText(data.testRows2006)} 个目标、${countText(data.testEvents2006)} 个事件、${countText(data.testStations2006)} 个全新目标台站`;
}

async function renderMlPilot() {
  const status = $("#ml-pilot-status");
  status.textContent = "正在读取试验结果…";
  try {
    if (!state.mlPilotPromise) {
      state.mlPilotPromise = fetchPilotSummary("./demo/ml_pilot/summary.json", "24 事件 Step3 ");
    }
    if (!state.residualResearchPromise) {
      state.residualResearchPromise = fetchPilotSummary("./demo/ml_pilot/residual_research/summary.json", "2006 回溯开发评估");
    }
    if (!state.strictKnetHoldoutPromise) {
      state.strictKnetHoldoutPromise = fetchPilotSummary("./demo/ml_pilot/strict_knet_holdout/summary.json", "事件×台站零重叠评估");
    }
    if (!state.unifiedStep3Promise) {
      state.unifiedStep3Promise = fetchPilotSummary("./demo/ml_pilot/unified_step3_comparison/comparison_summary.json", "统一 Step3 全量比较");
    }
    [state.mlPilot, state.residualResearch, state.strictKnetHoldout, state.unifiedStep3] = await Promise.all([
      state.mlPilotPromise,
      state.residualResearchPromise,
      state.strictKnetHoldoutPromise,
      state.unifiedStep3Promise,
    ]);
    const pilot = state.mlPilot;
    const data = pilot.data || {};
    const group = pilot.groupEventCrossValidation || {};
    const metrics = group.metrics || [];
    const zsl = pilotMetric(metrics, "ZSL");
    const ridge = pilotMetric(metrics, "ZSL + nested-CV Ridge residual");
    const broad = pilotMetric(metrics, "broad-neighbour spatial residual");
    const site = pilotMetric(metrics, "broad spatial + historical site");
    const ridgeGain = 100 * (zsl.mae_log10 - ridge.mae_log10) / zsl.mae_log10;
    const broadGain = 100 * (zsl.mae_log10 - broad.mae_log10) / zsl.mae_log10;
    const siteGain = 100 * (zsl.mae_log10 - site.mae_log10) / zsl.mae_log10;
    const accepted = finite(data.processedStations) ?? 0;
    const candidates = finite(data.candidateStations) ?? 0;
    const eventCount = finite(data.events) ?? 0;

    renderResidualResearch(state.residualResearch);
    renderStrictKnetHoldout(state.strictKnetHoldout);
    renderUnifiedStep3(state.unifiedStep3);

    $("#ml-zsl-mae").textContent = Number(zsl.mae_log10).toFixed(3);
    $("#ml-ridge-mae").textContent = Number(ridge.mae_log10).toFixed(3);
    $("#ml-ridge-note").textContent = `较 ZSL 下降 ${ridgeGain.toFixed(1)}%`;
    $("#ml-broad-mae").textContent = Number(broad.mae_log10).toFixed(3);
    $("#ml-broad-note").textContent = `较 ZSL 下降 ${broadGain.toFixed(1)}%`;
    $("#ml-site-mae").textContent = Number(site.mae_log10).toFixed(3);
    $("#ml-site-note").textContent = `较 ZSL 下降 ${siteGain.toFixed(1)}%`;

    const audit = group.audit || {};
    const ridgeAudit = audit.modelMinusZslMae?.ridgeResidual || {};
    const broadAudit = audit.modelMinusZslMae?.broadSpatialResidual || {};
    const siteAudit = audit.modelMinusZslMae?.historicalSite;
    const eventsImproved = audit.eventsImprovedByModel || {};
    $("#zsl-comparison-boundary").textContent = `24 个事件共有 ${candidates.toLocaleString("zh-CN")} 个候选目标；ZSL 凸包与四邻站质控后，${accepted.toLocaleString("zh-CN")} 个目标进入同样本比较（覆盖率 ${candidates ? (100 * accepted / candidates).toFixed(1) : "—"}%）。每个目标先从相位拟合、FAS 插值和邻站选择中完整遮蔽；ZSL 采用可复现的 Step2/3 适配器、10 次固定随机相位实现的 log10 中位数。`;
    $("#ml-audit").textContent = `事件互斥的 ${audit.outerFolds || 5} 折验证：ZSL + 残差回归在 ${eventsImproved.ridgeResidual ?? "—"}/${audit.eventsEvaluated ?? eventCount} 个事件降低 MAE，事件块 bootstrap 的模型减 ZSL 95% 区间为 ${confidenceIntervalText(ridgeAudit.ci95)}；新坐标空间模型为 ${eventsImproved.broadSpatialResidual ?? "—"}/${audit.eventsEvaluated ?? eventCount} 个事件、区间 ${confidenceIntervalText(broadAudit.ci95)}；历史台站模型为 ${eventsImproved.historicalSite ?? "—"}/${audit.eventsEvaluated ?? eventCount} 个事件、区间 ${confidenceIntervalText(siteAudit?.ci95)} log10。`;

    const temporalMetrics = pilot.metrics || [];
    const temporalZsl = pilotMetric(temporalMetrics, "ZSL");
    const temporalBroad = pilotMetric(temporalMetrics, "broad-neighbour spatial residual");
    const temporalSite = pilotMetric(temporalMetrics, "broad spatial + historical site");
    const temporalCount = finite(data.heldEventTestStations) ?? 0;
    const temporalBroadGain = 100 * (temporalZsl.mae_log10 - temporalBroad.mae_log10) / temporalZsl.mae_log10;
    const temporalSiteGain = 100 * (temporalZsl.mae_log10 - temporalSite.mae_log10) / temporalZsl.mae_log10;
    const temporalBroadAudit = pilot.bootstrap?.modelMinusZslMae?.broadSpatialResidual || {};
    const temporalSiteAudit = pilot.bootstrap?.modelMinusZslMae?.historicalSite || {};
    $("#zsl-temporal-audit").textContent = `2005–2006 年时间外对照含 ${temporalCount} 个目标、3 个事件：ZSL MAE ${Number(temporalZsl.mae_log10).toFixed(3)}；新坐标空间模型 ${Number(temporalBroad.mae_log10).toFixed(3)}（下降 ${temporalBroadGain.toFixed(1)}%，95% 区间 ${confidenceIntervalText(temporalBroadAudit.ci95)}，区间跨 0）；历史台站模型 ${Number(temporalSite.mae_log10).toFixed(3)}（下降 ${temporalSiteGain.toFixed(1)}%，95% 区间 ${confidenceIntervalText(temporalSiteAudit.ci95)}）。后者只适用于已有历史记录的 K-NET 台站。`;
    $("#ml-boundary").textContent = "全部实验现已统一使用逐记录原生采样率重算的 Step3 三分量矢量 PGA。证据 01 给出 18,264 条全队列 ZSL 覆盖及同目标比较；证据 01A 是 24 事件的早期敏感性复验；证据 02 在 2006 年 1,464 个已知台站目标上检验残差机制；证据 03 在 201 个事件×台站严格留出目标上检验迁移。跨实验比较须限定为相同目标集合。所有卡片均在运行时读取对应 summary.json。";
    status.textContent = "统一 Step3、ZSL 对照与两组全量证据已载入";
    $("#load-status").textContent = `ZSL 对照、严格留出与回溯开发已就绪`;
  } catch (error) {
    state.mlPilotPromise = null;
    state.residualResearchPromise = null;
    state.strictKnetHoldoutPromise = null;
    state.unifiedStep3Promise = null;
    status.textContent = "试验结果读取失败";
    $("#ml-boundary").textContent = `${error.message}。请确认统一比较与三个试验目录的 summary.json 已生成。`;
    $("#load-status").textContent = "残差验证结果不可用";
  }
}

function eventFeatureCollection() {
  const event = state.current;
  const points = [...event.observed, ...event.inferred]
    .filter((point) => point.lon !== null && point.lat !== null)
    .map((point) => ({ type: "Feature", geometry: { type: "Point", coordinates: [point.lon, point.lat] } }));
  const lon = firstFinite(event.config.e_lon, event.config.source_longitude_deg, event.config.longitude);
  const lat = firstFinite(event.config.e_lat, event.config.source_latitude_deg, event.config.latitude);
  if (lon !== null && lat !== null) points.push({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] } });
  return { type: "FeatureCollection", features: points };
}

function eventExtentGeometry() {
  const coordinates = eventFeatureCollection().features.map((feature) => feature.geometry.coordinates);
  const lons = coordinates.map(([lon]) => lon);
  const lats = coordinates.map(([, lat]) => lat);
  const lonExtent = d3.extent(lons);
  const latExtent = d3.extent(lats);
  const lonPad = Math.max((lonExtent[1] - lonExtent[0]) * 0.16, 0.18);
  const latPad = Math.max((latExtent[1] - latExtent[0]) * 0.16, 0.18);
  const west = lonExtent[0] - lonPad;
  const east = lonExtent[1] + lonPad;
  const south = latExtent[0] - latPad;
  const north = latExtent[1] + latPad;
  return { type: "MultiPoint", coordinates: [[west, south], [east, north]] };
}

function metricScale(metric) {
  const kind = fixedMetricScaleKind(metric);
  const thresholds = kind ? FIXED_SCALE_THRESHOLDS[kind] : [];
  return {
    color: (value) => fixedMetricColor(metric, value) || "transparent",
    continuousColor: (value) => fixedMetricContinuousColor(metric, value) || "transparent",
    valid: (value) => fixedMetricClassIndex(metric, value) >= 0,
    domain: [0, Number.POSITIVE_INFINITY],
    thresholds,
  };
}

function createMapContext(selector, { zoomable = false } = {}) {
  const root = d3.select(selector);
  root.selectAll(":scope > .map-viewport").remove();
  root.on(".zoom", null).classed("is-panning", false);
  const node = root.node();
  const width = Math.max(320, node.clientWidth || 620);
  const height = node.clientHeight || 440;
  const inset = { left: 34, top: 27, right: 34, bottom: 31 };
  const projection = d3.geoMercator().fitExtent(
    [[inset.left, inset.top], [width - inset.right, height - inset.bottom]],
    eventExtentGeometry(),
  );
  root.attr("viewBox", `0 0 ${width} ${height}`)
    .attr("data-zoomable", zoomable ? "true" : null);
  const svg = root.append("g").attr("class", "map-viewport");
  return { root, svg, node, width, height, inset, projection, path: d3.geoPath(projection), zoomable };
}

function enableMapZoom(context, controlPrefix) {
  if (!context.zoomable) return;
  const zoom = d3.zoom()
    .scaleExtent([1, 8])
    .extent([[0, 0], [context.width, context.height]])
    .translateExtent([[0, 0], [context.width, context.height]])
    .on("start", () => context.root.classed("is-panning", true))
    .on("zoom", ({ transform }) => context.svg.attr("transform", transform))
    .on("end", () => context.root.classed("is-panning", false));

  context.root.call(zoom).call(zoom.transform, d3.zoomIdentity);
  const animate = (method, value) => context.root.transition().duration(160).call(method, value);
  $(`#${controlPrefix}-zoom-in`).onclick = () => animate(zoom.scaleBy, 1.5);
  $(`#${controlPrefix}-zoom-out`).onclick = () => animate(zoom.scaleBy, 1 / 1.5);
  $(`#${controlPrefix}-zoom-reset`).onclick = () => animate(zoom.transform, d3.zoomIdentity);
}

function drawMapFoundation(context) {
  const { svg, width, height, path } = context;
  svg.append("rect").attr("width", width).attr("height", height).attr("fill", mapColors.ocean);
  const boundaryFeatures = polygonFeatures(state.boundary?.features || []);
  if (boundaryFeatures.length) {
    svg.append("g")
      .selectAll("path")
      .data(boundaryFeatures)
      .join("path")
      .attr("d", path)
      .attr("fill", mapColors.land)
      .attr("stroke", "none");
  }
  const provinceFeatures = administrativeFeatures().filter((feature) => feature.properties?.layer === "admin-province");
  if (provinceFeatures.length) {
    svg.append("g")
      .attr("aria-label", "行政区陆地")
      .selectAll("path")
      .data(provinceFeatures)
      .join("path")
      .attr("d", path)
      .attr("fill", mapColors.land)
      .attr("stroke", "none");
  }
}

function polygonFeatures(features) {
  return features.filter((feature) => ["Polygon", "MultiPolygon"].includes(feature.geometry?.type));
}

function administrativeFeatures() {
  return polygonFeatures(state.basemap?.features || [])
    .filter((feature) => administrativeLayerStyles[feature.properties?.layer]);
}

function basemapProperty(feature, ...names) {
  const properties = feature?.properties || {};
  for (const name of names) {
    if (properties[name] !== undefined && properties[name] !== null) return String(properties[name]).trim();
    const found = Object.keys(properties).find((key) => key.toLowerCase() === name.toLowerCase());
    if (found && properties[found] !== null) return String(properties[found]).trim();
  }
  return "";
}

function featureName(feature) {
  return basemapProperty(feature, "name_ja", "name_zh", "name_local", "name", "XZNAME");
}

function drawMapReference(context) {
  const { svg, width, height, path, projection } = context;
  const boundaryFeatures = polygonFeatures(state.boundary?.features || []);
  if (boundaryFeatures.length) {
    for (const [color, opacity, strokeWidth] of [
      [mapColors.boundaryHalo, 0.7, 1.55],
      [mapColors.boundary, 0.96, 0.72],
    ]) {
      svg.append("g")
        .attr("aria-label", "世界或日本行政边界")
        .selectAll("path")
        .data(boundaryFeatures)
        .join("path")
        .attr("d", path)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-opacity", opacity)
        .attr("stroke-width", strokeWidth)
        .attr("vector-effect", "non-scaling-stroke");
    }
  }

  const administrative = administrativeFeatures();
  for (const layer of administrativeLayerOrder) {
    const rows = administrative.filter((feature) => feature.properties?.layer === layer);
    const style = administrativeLayerStyles[layer];
    if (!rows.length) continue;
    for (const [color, opacity, strokeWidth] of [
      [mapColors.boundaryHalo, 0.62, style.width + 0.9],
      [style.color, style.opacity, style.width],
    ]) {
      svg.append("g")
        .attr("aria-label", `${layer} 行政边界`)
        .selectAll("path")
        .data(rows)
        .join("path")
        .attr("d", path)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-opacity", opacity)
        .attr("stroke-width", strokeWidth)
        .attr("vector-effect", "non-scaling-stroke");
    }
  }

  const seenNames = new Set();
  const places = [
    ...boundaryFeatures.map((feature) => ({ feature, layer: "boundary" })),
    ...administrative.map((feature) => ({ feature, layer: feature.properties.layer })),
  ]
    .map((feature) => ({
      ...feature,
      name: featureName(feature.feature),
      style: administrativeLayerStyles[feature.layer] || { labelSize: 7.5, priority: 0 },
    }))
    .filter((place) => place.name && place.name !== "境界线")
    .map((place) => ({ ...place, point: projection(d3.geoCentroid(place.feature)) }))
    .filter((place) => place.point)
    .sort((a, b) => a.style.priority - b.style.priority)
    .filter((place) => {
      if (seenNames.has(place.name)) return false;
      seenNames.add(place.name);
      return place.point[0] > 28 && place.point[0] < width - 28 && place.point[1] > 22 && place.point[1] < height - 22;
    })
    .slice(0, 36);
  svg.append("g")
    .attr("aria-label", "行政区名称")
    .selectAll("text")
    .data(places)
    .join("text")
    .attr("x", ({ point }) => point[0])
    .attr("y", ({ point }) => point[1])
    .attr("text-anchor", "middle")
    .attr("fill", mapColors.label)
    .attr("fill-opacity", 0.78)
    .attr("font-size", ({ style }) => style.labelSize)
    .attr("paint-order", "stroke")
    .attr("stroke", mapColors.labelHalo)
    .attr("stroke-opacity", 0.95)
    .attr("stroke-width", 2.6)
    .text(({ name }) => name);
}

function drawEpicenter(context) {
  const event = state.current;
  const lon = firstFinite(event.config.e_lon, event.config.source_longitude_deg, event.config.longitude);
  const lat = firstFinite(event.config.e_lat, event.config.source_latitude_deg, event.config.latitude);
  if (lon === null || lat === null) return;
  context.svg.append("path")
    .attr("d", d3.symbol().type(d3.symbolStar).size(145)())
    .attr("transform", `translate(${context.projection([lon, lat])})`)
    .attr("fill", "#FFFFFF")
    .attr("stroke", "#153C3F")
    .attr("stroke-width", 1.5)
    .attr("aria-label", "震中");
}

function emptyMap(context, message) {
  context.svg.append("text")
    .attr("x", context.width / 2)
    .attr("y", context.height / 2)
    .attr("text-anchor", "middle")
    .attr("fill", "#72817f")
    .attr("font-size", 11)
    .text(message);
}

function renderScatterMap(context, points, metric, scale) {
  drawMapFoundation(context);
  drawMapReference(context);
  const tooltip = $("#map-tooltip");
  const frame = context.node.closest(".plot-frame");
  const marker = context.svg.append("g").attr("aria-label", "空间记录");

  function showTooltip(domEvent, point) {
    const meta = metricMeta[metric];
    const value = finite(point[metric]);
    tooltip.innerHTML = `<strong>${point.station || (point.kind === "observed" ? "实测台站" : "推测场点")}</strong><br>${meta.label}: ${numberText(value, metric === "intensity" ? 1 : 2)} ${meta.unit}<br>${coordinateText(point.lon, point.lat)}`;
    tooltip.hidden = false;
    const frameRect = frame.getBoundingClientRect();
    const tipRect = tooltip.getBoundingClientRect();
    const x = Math.max(8, Math.min(domEvent.clientX - frameRect.left + 12, frameRect.width - tipRect.width - 8));
    const y = Math.max(8, Math.min(domEvent.clientY - frameRect.top + 12, frameRect.height - tipRect.height - 8));
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${y}px`;
  }

  function selectPoint(domEvent, point) {
    if (domEvent.type === "keydown" && !["Enter", " "].includes(domEvent.key)) return;
    state.selectedPoint = point;
    renderSelection();
  }

  const inferred = points.filter((point) => point.kind === "inferred" && scale.valid(point[metric]));
  marker.selectAll("circle.inferred")
    .data(inferred)
    .join("circle")
    .attr("class", "inferred")
    .attr("cx", (point) => context.projection([point.lon, point.lat])[0])
    .attr("cy", (point) => context.projection([point.lon, point.lat])[1])
    .attr("r", 4.1)
    .attr("fill", (point) => scale.color(point[metric]))
    .attr("fill-opacity", 0.94)
    .attr("stroke", "#486764")
    .attr("stroke-width", 0.65)
    .attr("tabindex", 0)
    .attr("aria-label", (point) => `推测场点 ${coordinateText(point.lon, point.lat)}`)
    .on("pointerenter focus", showTooltip)
    .on("pointerleave blur", () => { tooltip.hidden = true; })
    .on("click keydown", selectPoint);

  const observed = points.filter((point) => point.kind === "observed" && scale.valid(point[metric]));
  const triangle = d3.symbol().type(d3.symbolTriangle).size(68);
  marker.selectAll("path.observed")
    .data(observed)
    .join("path")
    .attr("class", "observed")
    .attr("d", triangle)
    .attr("transform", (point) => `translate(${context.projection([point.lon, point.lat])})`)
    .attr("fill", (point) => scale.color(point[metric]))
    .attr("fill-opacity", 0.96)
    .attr("stroke", "#143F45")
    .attr("stroke-width", 1.1)
    .attr("tabindex", 0)
    .attr("aria-label", (point) => `实测台站 ${point.station || "未命名"}`)
    .on("pointerenter focus", showTooltip)
    .on("pointerleave blur", () => { tooltip.hidden = true; })
    .on("click keydown", selectPoint);
  drawEpicenter(context);
}

function interpolateSurface(context, points, metric) {
  const meta = metricMeta[metric];
  const samples = points
    .filter((point) => fixedMetricClassIndex(metric, point[metric]) >= 0 && (!meta.log || point[metric] > 0))
    .map((point) => {
      const [x, y] = context.projection([point.lon, point.lat]);
      return { x, y, value: point[metric], z: meta.log ? Math.log(point[metric]) : point[metric] };
    });
  if (samples.length < 3) return null;
  const delaunay = d3.Delaunay.from(samples, (point) => point.x, (point) => point.y);
  const triangles = delaunay.triangles;
  if (!triangles.length) return null;

  const x0 = context.inset.left;
  const y0 = context.inset.top;
  const spanX = context.width - context.inset.left - context.inset.right;
  const spanY = context.height - context.inset.top - context.inset.bottom;
  const cols = Math.max(140, Math.min(360, Math.round(spanX / 1.8)));
  const rows = Math.max(105, Math.min(270, Math.round(spanY / 1.8)));
  const cellWidth = spanX / cols;
  const cellHeight = spanY / rows;
  const values = new Float64Array(cols * rows);
  values.fill(Number.NaN);

  for (let row = 0; row < rows; row += 1) {
    const y = y0 + (row + 0.5) * cellHeight;
    for (let column = 0; column < cols; column += 1) {
      const x = x0 + (column + 0.5) * cellWidth;
      for (let index = 0; index < triangles.length; index += 3) {
        const a = samples[triangles[index]];
        const b = samples[triangles[index + 1]];
        const c = samples[triangles[index + 2]];
        const denominator = (b.y - c.y) * (a.x - c.x) + (c.x - b.x) * (a.y - c.y);
        if (Math.abs(denominator) < 1e-12) continue;
        const wa = ((b.y - c.y) * (x - c.x) + (c.x - b.x) * (y - c.y)) / denominator;
        const wb = ((c.y - a.y) * (x - c.x) + (a.x - c.x) * (y - c.y)) / denominator;
        const wc = 1 - wa - wb;
        if (wa >= -1e-7 && wb >= -1e-7 && wc >= -1e-7) {
          values[row * cols + column] = wa * a.z + wb * b.z + wc * c.z;
          break;
        }
      }
    }
  }
  const kernel = [1, 2, 1];
  let smoothed = values;
  for (let pass = 0; pass < 2; pass += 1) {
    const next = new Float64Array(smoothed.length);
    next.fill(Number.NaN);
    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < cols; column += 1) {
        const index = row * cols + column;
        if (!Number.isFinite(smoothed[index])) continue;
        let total = 0;
        let weight = 0;
        for (let dy = -1; dy <= 1; dy += 1) {
          for (let dx = -1; dx <= 1; dx += 1) {
            const sampleRow = row + dy;
            const sampleColumn = column + dx;
            if (sampleRow < 0 || sampleRow >= rows || sampleColumn < 0 || sampleColumn >= cols) continue;
            const sample = smoothed[sampleRow * cols + sampleColumn];
            if (!Number.isFinite(sample)) continue;
            const sampleWeight = kernel[dy + 1] * kernel[dx + 1];
            total += sample * sampleWeight;
            weight += sampleWeight;
          }
        }
        next[index] = total / weight;
      }
    }
    smoothed = next;
  }
  const displayValues = meta.log
    ? Float64Array.from(smoothed, (value) => Number.isFinite(value) ? Math.exp(value) : Number.NaN)
    : smoothed;
  return { values: displayValues, cols, rows, x0, y0, spanX, spanY, cellWidth, cellHeight };
}

function contourThresholds(scale) {
  return [0, ...scale.thresholds];
}

function featheredSurfaceAlpha(values, cols, rows, index, maximum = 184) {
  if (!Number.isFinite(values[index])) return 0;
  const row = Math.floor(index / cols);
  const column = index % cols;
  const feather = 4;
  let nearestGap = feather;
  for (let dy = -feather; dy <= feather; dy += 1) {
    for (let dx = -feather; dx <= feather; dx += 1) {
      const sampleRow = row + dy;
      const sampleColumn = column + dx;
      if (sampleRow < 0 || sampleRow >= rows || sampleColumn < 0 || sampleColumn >= cols
          || !Number.isFinite(values[sampleRow * cols + sampleColumn])) {
        nearestGap = Math.min(nearestGap, Math.hypot(dx, dy));
      }
    }
  }
  return Math.round(maximum * Math.min(1, nearestGap / feather));
}

function renderFieldMap(context, inferred, metric, scale) {
  drawMapFoundation(context);
  const surface = interpolateSurface(context, inferred, metric);
  if (!surface || !surface.values.some(Number.isFinite)) {
    drawMapReference(context);
    emptyMap(context, "有效推测场点不足，无法生成连续场");
    drawEpicenter(context);
    return;
  }

  const mode = $("#field-mode").value;
  if (mode === "overlay") {
    const canvas = document.createElement("canvas");
    canvas.width = surface.cols;
    canvas.height = surface.rows;
    const canvasContext = canvas.getContext("2d");
    const image = canvasContext.createImageData(surface.cols, surface.rows);
    for (let index = 0; index < surface.values.length; index += 1) {
      const value = surface.values[index];
      if (!Number.isFinite(value)) continue;
      const color = d3.rgb(scale.continuousColor(value));
      image.data[index * 4] = color.r;
      image.data[index * 4 + 1] = color.g;
      image.data[index * 4 + 2] = color.b;
      image.data[index * 4 + 3] = featheredSurfaceAlpha(surface.values, surface.cols, surface.rows, index);
    }
    canvasContext.putImageData(image, 0, 0);
    context.svg.append("image")
      .attr("x", surface.x0)
      .attr("y", surface.y0)
      .attr("width", surface.spanX)
      .attr("height", surface.spanY)
      .attr("preserveAspectRatio", "none")
      .attr("href", canvas.toDataURL("image/png"))
      .style("image-rendering", "auto")
      .attr("aria-label", `${metricMeta[metric].label} image overlay`);
  } else {
    const contours = d3.contours()
      .size([surface.cols, surface.rows])
      .smooth(true)
      .thresholds(contourThresholds(scale))(surface.values);
    const contourPath = d3.geoPath(d3.geoIdentity());
    context.svg.append("g")
      .attr("transform", `translate(${surface.x0},${surface.y0}) scale(${surface.cellWidth},${surface.cellHeight})`)
      .selectAll("path")
      .data(contours)
      .join("path")
      .attr("d", contourPath)
      .attr("fill", (contour) => scale.color(contour.value))
      .attr("fill-opacity", 0.72)
      .attr("stroke", "#65545D")
      .attr("stroke-opacity", 0.28)
      .attr("stroke-width", 0.45)
      .attr("vector-effect", "non-scaling-stroke");
  }
  drawMapReference(context);
  drawEpicenter(context);
}

function renderMap() {
  const event = state.current;
  const points = [...event.inferred, ...event.observed]
    .filter((point) => point.lon !== null && point.lat !== null);
  const scatterContext = createMapContext("#map-chart", { zoomable: true });
  const fieldContext = createMapContext("#field-chart", { zoomable: true });
  enableMapZoom(scatterContext, "map");
  enableMapZoom(fieldContext, "field");
  if (!points.length) {
    emptyMap(scatterContext, "CSV 中没有可用经纬度");
    emptyMap(fieldContext, "CSV 中没有可用经纬度");
    return;
  }
  const metric = $("#map-metric").value;
  const scale = metricScale(metric);
  renderScatterMap(scatterContext, points, metric, scale);
  renderFieldMap(fieldContext, event.inferred, metric, scale);
  renderSelection();
  renderMapLegend(metric, Boolean(event.observed.length), Boolean(event.inferred.length));
}

function renderSelection() {
  const point = state.selectedPoint;
  if (!point) return;
  $("#selection-name").textContent = point.station || (point.kind === "observed" ? "实测台站" : `${numberText(point.lon, 3)}°, ${numberText(point.lat, 3)}°`);
  const output = {
    kind: point.kind === "observed" ? "实测" : "推测",
    PGA: `${numberText(point.PGA, 2)} cm/s²`,
    PGV: `${numberText(point.PGV, 2)} cm/s`,
    PSA03: `${numberText(point.PSA03, 2)} cm/s²`,
    PSA10: `${numberText(point.PSA10, 2)} cm/s²`,
    PSA30: `${numberText(point.PSA30, 2)} cm/s²`,
    intensity: numberText(point.intensity, 1),
  };
  for (const value of $("#selection-values").querySelectorAll("[data-point-field]")) {
    value.textContent = output[value.dataset.pointField] ?? "—";
  }
  $("#selection-help").textContent = point.R === null
    ? coordinateText(point.lon, point.lat)
    : `震源距 ${numberText(point.R, 1)} km · ${coordinateText(point.lon, point.lat)}`;
}

function renderMapLegend(metric, hasObserved, hasInferred) {
  const legend = $("#map-legend");
  const meta = metricMeta[metric];
  legend.replaceChildren();

  const scaleLegend = document.createElement("section");
  scaleLegend.className = "fixed-scale-legend";
  scaleLegend.setAttribute("aria-label", `${meta.label}固定强震分级`);

  const heading = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = meta.label;
  const note = document.createElement("span");
  note.textContent = `${meta.unit ? `${meta.unit} · ` : ""}固定分级`;
  heading.append(title, note);

  const boundaryRow = document.createElement("div");
  boundaryRow.className = "fixed-scale-grid fixed-scale-boundaries";
  for (const label of fixedMetricLegendLabels(metric)) {
    const value = document.createElement("span");
    value.textContent = label;
    boundaryRow.append(value);
  }

  const colorRow = document.createElement("div");
  colorRow.className = "fixed-scale-grid fixed-scale-colors";
  const intervalLabels = fixedMetricIntervalLabels(metric);
  FIXED_SCALE_COLORS.forEach((color, index) => {
    const swatch = document.createElement("i");
    swatch.style.backgroundColor = color;
    swatch.setAttribute("aria-label", `${FIXED_SCALE_LEVELS[index]} 级，${intervalLabels[index]}`);
    colorRow.append(swatch);
  });

  const levelRow = document.createElement("div");
  levelRow.className = "fixed-scale-grid fixed-scale-levels";
  for (const level of FIXED_SCALE_LEVELS) {
    const value = document.createElement("span");
    value.textContent = level;
    levelRow.append(value);
  }

  scaleLegend.append(heading, boundaryRow, colorRow, levelRow);
  legend.append(scaleLegend);

  if (hasObserved) legend.insertAdjacentHTML("beforeend", '<span class="legend-item"><i class="legend-triangle"></i>实测台站</span>');
  if (hasInferred) legend.insertAdjacentHTML("beforeend", '<span class="legend-item"><i class="legend-circle"></i>推测场点</span>');
  legend.insertAdjacentHTML("beforeend", '<span class="legend-item"><i class="legend-star">★</i>震中</span>');
  legend.insertAdjacentHTML("beforeend", '<span class="legend-item"><i class="legend-transparent"></i>无数据透明</span>');
}

function safeLogDomain(values) {
  let [min, max] = d3.extent(values.filter((value) => value > 0));
  if (!Number.isFinite(min)) return [1, 10];
  if (min === max) { min *= 0.75; max *= 1.35; }
  return [min, max];
}

// ATTENUATION_DISTANCE_DOMAIN_START
export function boundedLogDistanceDomain(values, lowerLimit = 1, upperLimit = 1000) {
  const logs = values
    .map(Number)
    .filter((value) => Number.isFinite(value) && value > 0)
    .map((value) => Math.log10(Math.max(lowerLimit, Math.min(upperLimit, value))));
  if (!logs.length) return [lowerLimit, upperLimit];
  const minimum = Math.min(...logs);
  const maximum = Math.max(...logs);
  const center = (minimum + maximum) / 2;
  const span = Math.max(maximum - minimum, 0.3);
  const padding = Math.max(0.04, span * 0.12);
  return [
    Math.max(lowerLimit, 10 ** (center - span / 2 - padding)),
    Math.min(upperLimit, 10 ** (center + span / 2 + padding)),
  ];
}
// ATTENUATION_DISTANCE_DOMAIN_END

function attenuationDistanceDomain(rows) {
  const distances = [];
  for (const metric of ["PGA", "PGV", "PSA03", "PSA10", "PSA30", "intensity"]) {
    const meta = metricMeta[metric];
    for (const group of attenuationBinSeries(rows, metric)) {
      for (const row of group.values) {
        if (meta.log && ![row.mean, row.median, row.lower, row.upper].every((value) => value > 0)) continue;
        distances.push(row.binCenterKm * (10 ** group.log10Offset));
      }
    }
  }
  return boundedLogDistanceDomain(distances);
}

function renderAttenuation() {
  const container = $("#attenuation-charts");
  container.replaceChildren();
  const distanceDomain = attenuationDistanceDomain(state.current.distanceBinStatistics);
  for (const metric of ["PGA", "PGV", "PSA03", "PSA10", "PSA30", "intensity"]) {
    container.append(makeAttenuationChart(metric, distanceDomain));
  }
}

function makeAttenuationChart(metric, distanceDomain) {
  const meta = metricMeta[metric];
  const panel = document.createElement("article");
  panel.className = "chart-panel";
  panel.dataset.expandableChart = metric;
  panel.tabIndex = 0;
  panel.setAttribute("role", "button");
  panel.setAttribute("aria-label", `放大查看${meta.label}震源距分箱图`);
  panel.innerHTML = `<header><h4>${meta.label}</h4></header><svg role="img" aria-label="${meta.label} 按震源距分箱统计"></svg><div class="chart-tooltip attenuation-tooltip" role="status" hidden></div>`;
  const svg = d3.select(panel.querySelector("svg"));
  const tooltip = panel.querySelector(".attenuation-tooltip");
  const width = 390;
  const height = 320;
  const margin = { top: 38, right: 16, bottom: 42, left: 54 };
  svg.attr("viewBox", `0 0 ${width} ${height}`);
  const series = attenuationBinSeries(state.current.distanceBinStatistics, metric)
    .map((group) => ({
      ...group,
      values: group.values.filter((row) => (
        !meta.log || [row.mean, row.median, row.lower, row.upper].every((value) => value > 0)
      )),
    }));
  const bins = series.flatMap((group) => group.values);

  function positionTooltip(domEvent) {
    const panelRect = panel.getBoundingClientRect();
    const tipRect = tooltip.getBoundingClientRect();
    const x = Math.max(8, Math.min(domEvent.clientX - panelRect.left + 12, panelRect.width - tipRect.width - 8));
    const y = Math.max(8, Math.min(domEvent.clientY - panelRect.top + 12, panelRect.height - tipRect.height - 8));
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${y}px`;
  }

  function showBinTooltip(domEvent, row, group) {
    const unit = meta.unit ? ` ${meta.unit}` : "";
    tooltip.innerHTML = `<strong>${group.label}</strong><br>距离箱 ${numberText(row.binLeftKm, 2)}–${numberText(row.binRightKm, 2)} km<br>样本数 n = ${numberText(row.n, 0)}<br>均值 ${numberText(row.mean, 2)}${unit}<br>中位数 ${numberText(row.median, 2)}${unit}<br>±1σ 离散范围 ${numberText(row.lower, 2)}–${numberText(row.upper, 2)}${unit}`;
    tooltip.hidden = false;
    positionTooltip(domEvent);
  }

  if (!bins.length) {
    svg.append("text").attr("x", width / 2).attr("y", height / 2).attr("text-anchor", "middle").attr("fill", "#72817f").text("无可用距离分箱");
    return panel;
  }

  const x = d3.scaleLog().domain(distanceDomain).range([margin.left, width - margin.right]);
  const yValues = bins.flatMap((row) => [row.lower, row.mean, row.median, row.upper]);
  const y = meta.log
    ? d3.scaleLog().domain(safeLogDomain(yValues)).nice().range([height - margin.bottom, margin.top])
    : d3.scaleLinear().domain([
      Math.min(0, d3.min(yValues)),
      Math.max(12, d3.max(yValues) * 1.05),
    ]).nice().range([height - margin.bottom, margin.top]);

  svg.append("g").attr("class", "grid").attr("transform", `translate(${margin.left},0)`).call(d3.axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${height - margin.bottom})`).call(d3.axisBottom(x).ticks(5, "~g"));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${margin.left},0)`).call(d3.axisLeft(y).ticks(5, "~g"));
  svg.append("text").attr("x", (margin.left + width - margin.right) / 2).attr("y", height - 8).attr("text-anchor", "middle").attr("fill", "#72817f").attr("font-size", 9).text("震源距 (km)");

  for (const group of series) {
    if (!group.values.length) continue;
    const layer = svg.append("g").attr("class", `attenuation-bins ${group.source}`);
    const binX = (row) => x(row.binCenterKm * (10 ** group.log10Offset));
    layer.selectAll("line.bin-error").data(group.values).join("line")
      .attr("class", "bin-error")
      .attr("x1", binX).attr("x2", binX)
      .attr("y1", (row) => y(row.lower)).attr("y2", (row) => y(row.upper))
      .attr("stroke", group.color).attr("stroke-width", 1.25);
    for (const bound of ["lower", "upper"]) {
      layer.selectAll(`line.bin-cap-${bound}`).data(group.values).join("line")
        .attr("class", `bin-cap-${bound}`)
        .attr("x1", (row) => binX(row) - 3.5).attr("x2", (row) => binX(row) + 3.5)
        .attr("y1", (row) => y(row[bound])).attr("y2", (row) => y(row[bound]))
        .attr("stroke", group.color).attr("stroke-width", 1.25);
    }
    layer.selectAll("line.bin-median").data(group.values).join("line")
      .attr("class", "bin-median")
      .attr("x1", (row) => binX(row) - 5).attr("x2", (row) => binX(row) + 5)
      .attr("y1", (row) => y(row.median)).attr("y2", (row) => y(row.median))
      .attr("stroke", group.color).attr("stroke-width", 2.2);
    const symbol = d3.symbol()
      .type(group.symbol === "diamond" ? d3.symbolDiamond : d3.symbolCircle)
      .size(42);
    layer.selectAll("path.bin-mean").data(group.values).join("path")
      .attr("class", "bin-mean")
      .attr("d", symbol)
      .attr("transform", (row) => `translate(${binX(row)},${y(row.mean)})`)
      .attr("fill", "white").attr("stroke", group.color).attr("stroke-width", 1.5)
      .append("title")
      .text((row) => `${group.label} · n=${row.n} · 均值 ${numberText(row.mean, 2)} · 中位数 ${numberText(row.median, 2)} ${meta.unit}`);
    layer.selectAll("rect.bin-hit").data(group.values).join("rect")
      .attr("class", "bin-hit")
      .attr("x", (row) => binX(row) - 9)
      .attr("y", (row) => Math.min(y(row.lower), y(row.upper), y(row.mean)) - 9)
      .attr("width", 18)
      .attr("height", (row) => Math.max(18, Math.abs(y(row.lower) - y(row.upper)) + 18))
      .attr("fill", "transparent")
      .attr("pointer-events", "all")
      .attr("aria-label", (row) => `${group.label}，${numberText(row.binLeftKm, 2)} 至 ${numberText(row.binRightKm, 2)} 千米，样本数 ${numberText(row.n, 0)}`)
      .on("pointerenter", (domEvent, row) => showBinTooltip(domEvent, row, group))
      .on("pointermove", positionTooltip)
      .on("pointerleave", () => { tooltip.hidden = true; });
  }

  const legend = svg.append("g").attr("class", "attenuation-bin-legend").attr("transform", `translate(${margin.left},12)`);
  series.forEach((group, index) => {
    const item = legend.append("g").attr("transform", `translate(${index * 92},0)`);
    item.append("line").attr("x1", 0).attr("x2", 14).attr("y1", 0).attr("y2", 0).attr("stroke", group.color).attr("stroke-width", 2);
    item.append("text").attr("x", 18).attr("y", 3).attr("fill", "#526663").attr("font-size", 8.5).text(group.label);
  });
  return panel;
}

function setupWaveformSelect() {
  const select = $("#waveform-select");
  const waveforms = state.current.waveforms || [];
  select.replaceChildren(...waveforms.map((waveform, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = waveform.label || waveform.station || waveform.key;
    return option;
  }));
  select.disabled = !waveforms.length;
  if (!waveforms.length) {
    const option = document.createElement("option");
    option.textContent = "没有三分量 DAT";
    select.append(option);
  }
}

async function waveformData(waveform) {
  if (waveform.components) {
    const samplingRate = waveform.samplingRate || 100;
    const series = {};
    for (const component of ["EW", "NS", "UD"]) {
      const values = waveform.components[component] || [];
      series[component] = values.map((Acc, index) => ({ Time: index / samplingRate, Acc: Number(Acc) }));
    }
    return { ...waveform, samplingRate, series, unit: waveform.unit || "gal" };
  }

  const series = {};
  await Promise.all(["EW", "NS", "UD"].map(async (component) => {
    const file = waveform.files?.[component];
    series[component] = file ? parseDat(await file.text()) : [];
  }));
  const first = Object.values(series).find((values) => values.length > 1) || [];
  const dt = first.length > 1 ? first[1].Time - first[0].Time : null;
  return { ...waveform, samplingRate: dt > 0 ? 1 / dt : null, series, unit: "source DAT unit" };
}

async function loadSelectedWaveform() {
  const waveforms = state.current.waveforms || [];
  if (!waveforms.length) {
    renderWaveform(null);
    return;
  }
  const index = Number($("#waveform-select").value) || 0;
  const token = ++state.waveformToken;
  const waveform = await waveformData(waveforms[index]);
  if (token === state.waveformToken) renderWaveform(waveform);
}

function decimateSeries(series, maxPoints = 1800) {
  if (series.length <= maxPoints) return series;
  const bucket = Math.ceil(series.length / (maxPoints / 2));
  const output = [];
  for (let start = 0; start < series.length; start += bucket) {
    const slice = series.slice(start, start + bucket);
    let min = slice[0];
    let max = slice[0];
    for (const point of slice) {
      if (point.Acc < min.Acc) min = point;
      if (point.Acc > max.Acc) max = point;
    }
    if (min.Time < max.Time) output.push(min, max);
    else output.push(max, min);
  }
  return output;
}

function renderWaveform(waveform) {
  const svg = d3.select("#waveform-chart");
  svg.selectAll("*").remove();
  const node = svg.node();
  const width = Math.max(500, node.clientWidth || 900);
  const height = node.clientHeight || 500;
  svg.attr("viewBox", `0 0 ${width} ${height}`);
  if (!waveform) {
    svg.append("text").attr("x", width / 2).attr("y", height / 2).attr("text-anchor", "middle").attr("fill", "#72817f").text("当前数据没有可用三分量波形");
    $("#waveform-meta").textContent = "结果目录需包含 IF_folder/*.EW.dat、*.NS.dat 与 *.UD.dat。";
    return;
  }

  const margin = { top: 28, right: 24, bottom: 42, left: 70 };
  const gap = 23;
  const laneHeight = (height - margin.top - margin.bottom - gap * 2) / 3;
  const maxTime = d3.max(Object.values(waveform.series).flat(), (point) => point.Time) || 1;
  const x = d3.scaleLinear().domain([0, maxTime]).range([margin.left, width - margin.right]);

  ["EW", "NS", "UD"].forEach((component, lane) => {
    const series = waveform.series[component] || [];
    const top = margin.top + lane * (laneHeight + gap);
    const amplitude = d3.max(series, (point) => Math.abs(point.Acc)) || 1;
    const y = d3.scaleLinear().domain([-amplitude, amplitude]).range([top + laneHeight, top]);
    svg.append("rect").attr("x", margin.left).attr("y", top).attr("width", width - margin.left - margin.right).attr("height", laneHeight).attr("fill", lane % 2 ? "#fbfaf5" : "#f5f5ef");
    svg.append("line").attr("x1", margin.left).attr("x2", width - margin.right).attr("y1", y(0)).attr("y2", y(0)).attr("stroke", "#c5ccc7").attr("stroke-width", 0.7);
    svg.append("text").attr("x", margin.left - 15).attr("y", top + 15).attr("text-anchor", "end").attr("fill", componentColors[component]).attr("font-size", 11).attr("font-weight", 700).text(component);
    svg.append("text").attr("x", margin.left - 15).attr("y", top + 30).attr("text-anchor", "end").attr("fill", "#72817f").attr("font-size", 8).text(`±${numberText(amplitude, 1)}`);
    if (series.length) {
      const line = d3.line().x((point) => x(point.Time)).y((point) => y(point.Acc));
      svg.append("path").datum(decimateSeries(series)).attr("d", line).attr("fill", "none").attr("stroke", componentColors[component]).attr("stroke-width", 0.85).attr("vector-effect", "non-scaling-stroke");
    }
  });

  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${height - margin.bottom + 7})`).call(d3.axisBottom(x).ticks(Math.max(4, Math.floor(width / 140))));
  svg.append("text").attr("x", (margin.left + width - margin.right) / 2).attr("y", height - 6).attr("text-anchor", "middle").attr("fill", "#72817f").attr("font-size", 9).text("时间 (s)");

  const count = Math.max(...Object.values(waveform.series).map((series) => series.length));
  const samplingText = waveform.displayStride > 1
    ? `${numberText(waveform.samplingRate, 2)} Hz 显示采样 · 原始 ${numberText(waveform.sourceSamplingRate, 0)} Hz · 步长 ${waveform.displayStride}`
    : `${numberText(waveform.samplingRate, 2)} Hz`;
  $("#waveform-meta").replaceChildren(
    textSpan(waveform.label || waveform.station || waveform.key),
    textSpan(`${samplingText} · ${count.toLocaleString("zh-CN")} samples · ${waveform.unit}`),
  );
}

function textSpan(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

// EVENT_PLACE_FROM_TITLE_START
export function eventPlaceFromTitle(title) {
  return String(title || "")
    .replace(/^\d{8,14}/, "")
    .replace(/^\d{4}\s*[-年]\s*\d{1,2}\s*[-月]\s*\d{1,2}\s*(?:日)?\s*/, "")
    .replace(/^\d{4}\s*年\s*/, "")
    .replace(/\s*(?:M[swjl]?\s*)?\d+(?:\.\d+)?\s*(?:级)?地震.*$/i, "")
    .replace(/\s*地震\s*$/, "")
    .replace(/^K-NET\s*事件$/i, "")
    .trim();
}
// EVENT_PLACE_FROM_TITLE_END

function openFigureLightbox(title, content) {
  const dialog = $("#figure-lightbox");
  $("#figure-lightbox-title").textContent = title || "图件预览";
  $("#figure-lightbox-body").replaceChildren(content);
  if (!dialog.open) dialog.showModal();
}

function openArtifactLightbox(link) {
  const source = link.querySelector("img");
  if (!source) return;
  const image = document.createElement("img");
  image.src = link.href;
  image.alt = source.alt || "成果图件";
  const title = link.closest("figure")?.querySelector("figcaption")?.textContent || image.alt;
  openFigureLightbox(title, image);
}

function openChartLightbox(panel) {
  const source = panel.querySelector("svg");
  if (!source) return;
  const title = panel.querySelector("h4")?.textContent || "震源距分箱图";
  const chart = source.cloneNode(true);
  chart.removeAttribute("width");
  chart.removeAttribute("height");
  openFigureLightbox(title, chart);
}

function renderArtifacts() {
  const grid = $("#artifact-grid");
  const artifacts = state.current.artifacts || [];
  const resultFiles = state.current.resultFiles || {};
  grid.replaceChildren();
  if (!artifacts.length && !Object.keys(resultFiles).length) {
    const empty = document.createElement("div");
    empty.className = "empty-panel";
    const detail = "当前结果目录未找到成果 PNG；CSV 和 DAT 仍可在对应视图与完整结果包中查看。";
    empty.innerHTML = `<div><strong>当前事件没有流程图件</strong><span>${detail}</span></div>`;
    grid.append(empty);
    return;
  }

  const figureFor = (artifact, className = "") => {
    const figure = document.createElement("figure");
    figure.className = `artifact ${className}`.trim();
    const image = document.createElement("img");
    image.src = artifact.url;
    image.alt = artifact.label;
    image.loading = "lazy";
    const link = document.createElement("a");
    link.className = "artifact-link";
    link.href = artifact.url;
    link.append(image);
    const caption = document.createElement("figcaption");
    caption.textContent = artifact.label;
    figure.append(link, caption);
    return figure;
  };

  const sectionFor = (className, eyebrow, title) => {
    const section = document.createElement("section");
    section.className = `artifact-section ${className}`;
    const header = document.createElement("header");
    const index = document.createElement("span");
    index.textContent = eyebrow;
    const heading = document.createElement("h4");
    heading.textContent = title;
    header.append(index, heading);
    section.append(header);
    return section;
  };

  const groups = groupArtifacts(artifacts);
  const comparison = document.createElement("div");
  comparison.className = "artifact-comparison-layout";

  if (groups.attenuation.length) {
    const overview = sectionFor("artifact-overview", "SIX METRICS", "实测与推测随震源距变化");
    const figures = document.createElement("div");
    figures.className = "artifact-overview-grid";
    for (const artifact of groups.attenuation) figures.append(figureFor(artifact, "artifact-attenuation"));
    overview.append(figures);
    comparison.append(overview);
  }

  if (groups.spatial.length) {
    const maps = sectionFor("artifact-spatial", `${groups.spatial.length} MAP GROUPS`, "六指标实测—推测融合场");
    const figures = document.createElement("div");
    figures.className = "artifact-spatial-grid";
    for (const artifact of groups.spatial) {
      figures.append(figureFor({ ...artifact, label: spatialArtifactLabel(artifact) }, "artifact-spatial-map"));
    }
    maps.append(figures);
    comparison.append(maps);
  }
  if (comparison.childElementCount) grid.append(comparison);

  const secondary = [...groups.quality, ...groups.other];
  if (secondary.length) {
    const audit = sectionFor("artifact-audit", "QUALITY CONTROL", "候选点质控与辅助图件");
    const figures = document.createElement("div");
    figures.className = "artifact-audit-grid";
    for (const artifact of secondary) figures.append(figureFor(artifact));
    audit.append(figures);
    grid.append(audit);
  }
  const fileEntries = [
    [resultFiles.observedCsv, "实测地震动指标 CSV"],
    [resultFiles.inferredCsv, `${state.current.inferred.length} 个质控通过推测场点指标 CSV`],
    [resultFiles.distanceBinStatistics, "六指标按震源距分箱统计 CSV"],
    [
      resultFiles.qualityControlCsv,
      `${numberText(
        state.current.summary?.candidateTargetPoints
          ?? state.current.summary?.candidateGridPoints,
        0,
      )} 个候选目标点质量控制 CSV`,
    ],
    [resultFiles.runMetadata, "运行元数据与方法哈希 JSON"],
  ].filter(([url]) => url);
  if (fileEntries.length) {
    const files = document.createElement("section");
    files.className = "result-files";
    const heading = document.createElement("strong");
    const datCount = finite(state.current.provenanceDetail?.outputs?.datFileCount);
    heading.textContent = datCount
      ? `本事件的数值结果 · ${numberText(datCount, 0)} 个完整 DAT 已入 ZIP`
      : "本事件的数值结果";
    const links = document.createElement("div");
    for (const [url, label] of fileEntries) {
      const link = document.createElement("a");
      link.href = url;
      link.download = "";
      link.textContent = label;
      links.append(link);
    }
    files.append(heading, links);
    grid.append(files);
  }
}

function debounce(callback, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), wait);
  };
}

function bindEvents() {
  $("#folder-button").addEventListener("click", () => $("#folder-input").click());
  $("#error-open-button").addEventListener("click", () => $("#folder-input").click());
  $("#folder-input").addEventListener("change", (event) => {
    if (event.target.files.length) openFiles(event.target.files);
    event.target.value = "";
  });
  $("#raw-folder-button").addEventListener("click", openImportDialog);
  $("#import-choose-button").addEventListener("click", () => $("#raw-folder-input").click());
  $("#raw-folder-input").addEventListener("change", (event) => {
    if (event.target.files.length) chooseRawFolder(event.target.files);
    event.target.value = "";
  });
  $("#target-mode").addEventListener("change", updateTargetConfig);
  $("#target-grid-rows").addEventListener("input", updateTargetConfig);
  $("#target-grid-columns").addEventListener("input", updateTargetConfig);
  $("#target-manual-input").addEventListener("input", updateTargetConfig);
  $("#target-csv-input").addEventListener("change", async (event) => {
    const file = event.target.files?.[0] || null;
    state.targetCsvText = null;
    state.targetCsvError = null;
    $("#target-csv-name").textContent = file ? file.name : "尚未选择 CSV";
    if (file) {
      try {
        state.targetCsvText = await file.text();
      } catch (error) {
        state.targetCsvError = new Error(`无法读取 CSV：${error.message}`);
      }
    }
    updateTargetConfig();
    event.target.value = "";
  });
  $("#import-start-button").addEventListener("click", startRawImport);
  $("#watch-button").addEventListener("click", openWatchDialog);
  $("#watch-browse-button").addEventListener("click", chooseWatchFolder);
  $("#watch-start-button").addEventListener("click", startFolderWatch);
  $("#watch-stop-button").addEventListener("click", stopFolderWatch);
  $("#watch-dialog").addEventListener("close", stopWatchPolling);
  $("#archive-button").addEventListener("click", openArchiveDialog);
  $("#archive-refresh-button").addEventListener("click", refreshArchives);
  $("#demo-button").addEventListener("click", () => loadDemo());
  $("#event-switcher").addEventListener("change", (event) => {
    const index = Number(event.target.value);
    if (state.catalogEvents.length) selectCatalogEvent(index);
    else selectDescriptor(index);
  });
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$('[data-view-target]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
  $("#map-metric").addEventListener("change", renderMap);
  $("#field-mode").addEventListener("change", renderMap);
  $("#waveform-select").addEventListener("change", loadSelectedWaveform);
  document.addEventListener("click", (event) => {
    const artifactLink = event.target.closest(".artifact-link");
    if (artifactLink?.querySelector("img")) {
      event.preventDefault();
      openArtifactLightbox(artifactLink);
      return;
    }
    const chart = event.target.closest("[data-expandable-chart]");
    if (chart) openChartLightbox(chart);
  });
  document.addEventListener("keydown", (event) => {
    const chart = event.target.closest?.("[data-expandable-chart]");
    if (!chart || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    openChartLightbox(chart);
  });
  $("#figure-lightbox").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  window.addEventListener("resize", debounce(() => {
    if (!state.current) return;
    if (state.activeView === "map") renderMap();
    if (state.activeView === "waveforms") loadSelectedWaveform();
  }, 150));
  updateTargetConfig();
}

bindEvents();
loadDemo();
