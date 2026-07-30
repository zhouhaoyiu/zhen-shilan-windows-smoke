const numeric = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

function valueOf(value) {
  const trimmed = value.trim();
  return trimmed && numeric.test(trimmed) ? Number(trimmed) : value;
}

export function parseConfig(text) {
  const config = {};

  for (const rawLine of String(text ?? "").replace(/^\uFEFF/, "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const separator = line.indexOf("=");
    if (separator < 1) continue;

    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (key) config[key] = valueOf(value);
  }

  return config;
}

function csvRows(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  const source = String(text ?? "").replace(/^\uFEFF/, "");

  for (let i = 0; i < source.length; i += 1) {
    const char = source[i];

    if (quoted) {
      if (char === '"' && source[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && source[i + 1] === "\n") i += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }

  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }

  return rows.filter((fields) => fields.some((field) => field.trim()));
}

export function parseCsv(text) {
  const rows = csvRows(text);
  if (!rows.length) return [];

  const columns = rows[0]
    .map((name, index) => ({ name: name.trim(), index }))
    .filter(({ name }) => name && !/^Unnamed(?::.*)?$/i.test(name));

  return rows.slice(1).map((fields) => Object.fromEntries(
    columns.map(({ name, index }) => [name, valueOf(fields[index] ?? "")]),
  ));
}

export function parseDat(text) {
  const rows = [];

  for (const rawLine of String(text ?? "").replace(/^\uFEFF/, "").split(/\r?\n/)) {
    const fields = rawLine.trim().split(/\s+/);
    if (fields.length < 2 || /^time$/i.test(fields[0])) continue;

    const Time = Number(fields[0]);
    const Acc = Number(fields[1]);
    if (Number.isFinite(Time) && Number.isFinite(Acc)) rows.push({ Time, Acc });
  }

  return rows;
}

function pathOf(file) {
  return String(file.webkitRelativePath || file.relativePath || file.path || file.name || "")
    .replace(/\\/g, "/")
    .replace(/^\.\//, "")
    .replace(/\/{2,}/g, "/");
}

function dirname(path) {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? "" : path.slice(0, separator);
}

function basename(path) {
  return path.slice(path.lastIndexOf("/") + 1);
}

export async function buildEvents(files) {
  const entries = Array.from(files ?? [], (file) => ({ file, path: pathOf(file) }))
    .filter(({ path }) => path)
    .sort((a, b) => a.path.localeCompare(b.path));

  const groups = new Map();
  for (const entry of entries) {
    if (basename(entry.path).toLowerCase() !== "config.txt") continue;
    const root = dirname(entry.path);
    if (!groups.has(root)) groups.set(root, { root, configFile: entry.file, entries: [] });
  }

  for (const entry of entries) {
    const group = [...groups.values()]
      .filter(({ root }) => !root || entry.path.startsWith(`${root}/`))
      .sort((a, b) => b.root.length - a.root.length)[0];
    if (group) group.entries.push(entry);
  }

  return Promise.all([...groups.values()]
    .sort((a, b) => a.root.localeCompare(b.root))
    .map(async ({ root, configFile, entries: eventEntries }) => {
      const eventFiles = {
        config: configFile,
        points: null,
        observed: null,
        inferred: null,
        waveforms: { EW: [], NS: [], UD: [] },
        images: { intensity: null, distance: null },
        otherPng: [],
      };

      for (const { file, path } of eventEntries) {
        const relative = root ? path.slice(root.length + 1) : path;
        const name = basename(relative);

        if (relative.toLowerCase() === "points.csv") eventFiles.points = file;
        else if (/^实测地震动.*\.csv$/i.test(name)) eventFiles.observed = file;
        else if (/^推测地震动.*\.csv$/i.test(name)) eventFiles.inferred = file;
        else {
          const waveform = /^IF_folder\/.*\.(EW|NS|UD)\.dat$/i.exec(relative);
          if (waveform) eventFiles.waveforms[waveform[1].toUpperCase()].push(file);
          else if (/^仪器地震烈度空间分布_.*\.png$/i.test(name)) eventFiles.images.intensity = file;
          else if (/^推测与实测地震动随距离分布_.*\.png$/i.test(name)) eventFiles.images.distance = file;
          else if (/\.png$/i.test(name)) eventFiles.otherPng.push(file);
        }
      }

      const configText = typeof configFile.text === "function" ? await configFile.text() : "";
      return {
        id: root,
        name: basename(root) || "event",
        root,
        config: parseConfig(configText),
        files: eventFiles,
      };
    }));
}
