const MAX_TARGETS = 2500;

function coordinateRows(text) {
  return String(text ?? "")
    .replace(/^\uFEFF/, "")
    .split(/\r?\n/)
    .map((value, index) => ({ value: value.trim(), line: index + 1 }))
    .filter(({ value }) => value);
}

function parsePoint(value, line) {
  const columns = value.split(",").map((item) => item.trim());
  if (columns.length !== 2 || columns.some((item) => item === "")) {
    throw new Error(`第 ${line} 行应为 lon,lat 两列`);
  }
  const lon = Number(columns[0]);
  const lat = Number(columns[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    throw new Error(`第 ${line} 行经纬度必须是有限数值`);
  }
  if (lon < -180 || lon > 180) throw new Error(`第 ${line} 行经度超出 [-180, 180]`);
  if (lat < -90 || lat > 90) throw new Error(`第 ${line} 行纬度超出 [-90, 90]`);
  return {
    lon: normalizeCoordinate(lon),
    lat: normalizeCoordinate(lat),
  };
}

function normalizeCoordinate(value) {
  const rounded = Number(value.toFixed(4));
  return Object.is(rounded, -0) ? 0 : rounded;
}

function parsePoints(rows) {
  if (!rows.length) throw new Error("至少需要 1 个推测点");
  if (rows.length > MAX_TARGETS) throw new Error(`推测点不能超过 ${MAX_TARGETS} 个`);
  const seen = new Set();
  return rows.map(({ value, line }) => {
    const point = parsePoint(value, line);
    const key = `${point.lon},${point.lat}`;
    if (seen.has(key)) throw new Error(`第 ${line} 行坐标重复`);
    seen.add(key);
    return point;
  });
}

export function normalizeGridTarget(rowsValue, columnsValue) {
  const rows = Number(rowsValue);
  const columns = Number(columnsValue);
  if (!Number.isInteger(rows) || !Number.isInteger(columns)) {
    throw new Error("网格行数和列数必须是整数");
  }
  if (rows < 2 || rows > 50 || columns < 2 || columns > 50) {
    throw new Error("网格行数和列数必须在 2–50 之间");
  }
  if (rows * columns > MAX_TARGETS) throw new Error(`推测点不能超过 ${MAX_TARGETS} 个`);
  return { mode: "grid", rows, columns };
}

export function parseTargetCsv(text) {
  const rows = coordinateRows(text);
  if (!rows.length) throw new Error("CSV 文件为空");
  const header = rows.shift().value.split(",").map((value) => value.trim().toLowerCase().replace(/\s+/g, ""));
  const longitudeHeaders = new Set(["lon", "longitude"]);
  const latitudeHeaders = new Set(["lat", "latitude"]);
  if (header.length !== 2 || !longitudeHeaders.has(header[0]) || !latitudeHeaders.has(header[1])) {
    throw new Error("CSV 表头必须为 lon,lat 或 longitude,latitude");
  }
  return parsePoints(rows);
}

export function parseManualTargets(text) {
  return parsePoints(coordinateRows(text));
}
