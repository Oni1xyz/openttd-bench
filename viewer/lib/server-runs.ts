import { constants, promises as fs, type Stats } from "node:fs";
import path from "node:path";
import { LIMITS, checkJsonBudget, isRecord, jsonBudget, parseJson } from "./limits";
import { validateTrajectories, type Trajectory } from "./trace";

export function outputsDir(): string {
  return path.resolve(/* turbopackIgnore: true */ process.env.OUTPUTS_DIR ?? path.join(process.cwd(), "..", "outputs"));
}
export type RunEntry = { path: string; name: string; group: string; mtime: number; size: number; episodes: number };
type IndexItem = { file: string; episode_id: string; size: number };
type ByteBudget = { remaining: number };
const sameFile = (a: Stats, b: Stats) => a.dev === b.dev && a.ino === b.ino;

async function checkedDirectory(root: string, rel: string): Promise<string> {
  const parts = rel.split("/");
  if (parts.length !== 3 || !parts[0] || parts[0].length > 255 || parts[0].startsWith(".") || parts[0] === "archived" ||
      /[\\\x00-\x1f\x7f]/.test(rel) || parts[1] !== "trajectories" || parts[2] !== "index.json")
    throw new Error("Invalid ATIF run path");
  let current = root;
  for (const part of parts.slice(0, 2)) {
    current = path.join(current, part);
    const stat = await fs.lstat(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("Invalid run directory");
  }
  return current;
}

async function readRegular(file: string, maxBytes: number, budget: ByteBudget) {
  const before = await fs.lstat(file);
  if (!before.isFile() || before.isSymbolicLink()) throw new Error("Expected a regular file");
  if (before.size > maxBytes || before.size > budget.remaining) throw new Error("File byte budget exceeded");
  // lstat alone races: NONBLOCK avoids FIFO hangs, NOFOLLOW refuses leaf links,
  // and fstat verifies the opened descriptor rather than trusting the old path.
  if (constants.O_NOFOLLOW === undefined || constants.O_NONBLOCK === undefined) throw new Error("Safe file reads unavailable");
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || !sameFile(before, stat)) throw new Error("File changed during read");
    if (stat.size > maxBytes || stat.size > budget.remaining) throw new Error("File byte budget exceeded");
    const bytes = Buffer.alloc(stat.size + 1);
    let count = 0;
    while (count < bytes.length) {
      const { bytesRead } = await handle.read(bytes, count, bytes.length - count, count);
      if (!bytesRead) break;
      count += bytesRead;
      budget.remaining -= bytesRead; // Charge even if a later race check rejects the file.
      if (budget.remaining < 0) throw new Error("File byte budget exceeded");
    }
    const after = await handle.stat();
    const named = await fs.lstat(file);
    if (!named.isFile() || !sameFile(stat, named)) throw new Error("File changed during read");
    if (count !== stat.size || after.size !== stat.size || after.mtimeMs !== stat.mtimeMs || after.ctimeMs !== stat.ctimeMs)
      throw new Error("File changed during read");
    return { text: bytes.subarray(0, count).toString("utf8"), stat };
  } finally { await handle.close(); }
}

async function readIndex(dir: string, budget: ByteBudget) {
  const { text, stat } = await readRegular(path.join(dir, "index.json"), LIMITS.indexBytes, budget);
  const value = parseJson(text);
  checkJsonBudget(value);
  if (!isRecord(value) || value.format !== "openttd-atif-index-v1" || !Array.isArray(value.trajectories)) throw new Error("Invalid ATIF index");
  if (value.trajectories.length > LIMITS.trajectories) throw new Error("Manifest entry limit exceeded");
  const items = new Map<string, IndexItem>();
  for (const item of value.trajectories) {
    if (!isRecord(item) || typeof item.file !== "string" || !/^[a-f0-9]{64}\.atif\.json$/.test(item.file) ||
        typeof item.episode_id !== "string" || item.episode_id.length > 1024 ||
        typeof item.size !== "number" || !Number.isSafeInteger(item.size) || item.size < 0)
      throw new Error("Invalid ATIF index entry");
    if (!items.has(item.file)) items.set(item.file, item as IndexItem);
  }
  return { items: [...items.values()], stat };
}

export async function listRuns(): Promise<RunEntry[]> {
  // The operator-selected root may itself be a symlink; descendants may not.
  const root = await fs.realpath(/* turbopackIgnore: true */ outputsDir());
  const runs: RunEntry[] = [];
  const budget = { remaining: LIMITS.discoveryBytes };
  let entries = 0;
  // opendir streams names: readdir would allocate the whole directory first.
  for await (const entry of await fs.opendir(/* turbopackIgnore: true */ root)) {
    if (++entries > LIMITS.directoryEntries || runs.length >= LIMITS.runs || budget.remaining < LIMITS.indexBytes) break;
    if (!entry.isDirectory() || entry.name === "archived" || entry.name.startsWith(".")) continue;
    const rel = `${entry.name}/trajectories/index.json`;
    try {
      const dir = await checkedDirectory(root, rel);
      const directoryStat = await fs.lstat(dir);
      const { items, stat } = await readIndex(dir, budget);
      await checkedDirectory(root, rel);
      if (!sameFile(directoryStat, await fs.lstat(dir))) continue;
      const size = items.reduce((sum, item) => sum + item.size, 0);
      if (!Number.isSafeInteger(size)) continue;
      runs.push({ path: rel, name: entry.name, group: "", mtime: stat.mtimeMs, size, episodes: new Set(items.map(t => t.episode_id)).size });
    } catch { /* Invalid/missing runs are skipped; do not log private paths/content. */ }
  }
  return runs.sort((a, b) => b.mtime - a.mtime);
}

function presentation(t: Trajectory): Trajectory {
  // Called only after a bounded iterative preflight, including all subagents.
  const extra = { ...t.extra };
  delete extra.source;
  delete extra.prime_source;
  return { ...t, extra, ...(t.subagent_trajectories ? { subagent_trajectories: t.subagent_trajectories.map(presentation) } : {}) };
}
export async function readRun(rel: string): Promise<string> {
  const root = await fs.realpath(/* turbopackIgnore: true */ outputsDir());
  const dir = await checkedDirectory(root, rel);
  const directoryStat = await fs.lstat(dir);
  const bytes = { remaining: LIMITS.runBytes };
  const budget = jsonBudget();
  const responseBudget = jsonBudget();
  const { items } = await readIndex(dir, bytes);
  const chunks: string[] = [];
  let responseBytes = 2;
  for (const item of items) {
    await checkedDirectory(root, rel);
    if (!sameFile(directoryStat, await fs.lstat(dir))) throw new Error("Run changed during read");
    const { text } = await readRegular(path.join(dir, item.file), LIMITS.fileBytes, bytes);
    const value = parseJson(text);
    const [trajectory] = validateTrajectories(value, budget, false);
    const presented = presentation(trajectory);
    // Account for presentation-added nodes and the response's enclosing array.
    checkJsonBudget([presented], responseBudget);
    const chunk = JSON.stringify(presented);
    responseBytes += Buffer.byteLength(chunk) + (chunks.length ? 1 : 0);
    if (responseBytes > LIMITS.runBytes) throw new Error("Response byte budget exceeded");
    chunks.push(chunk);
  }
  await checkedDirectory(root, rel);
  if (!sameFile(directoryStat, await fs.lstat(dir))) throw new Error("Run changed during read");
  return `[${chunks.join(",")}]`;
}
