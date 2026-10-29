// Shared server/browser limits. Fail closed instead of partially rendering a run.
export const LIMITS = Object.freeze({
  indexBytes: 256 * 1024,
  fileBytes: 8 * 1024 * 1024,
  runBytes: 16 * 1024 * 1024,
  discoveryBytes: 4 * 1024 * 1024,
  directoryEntries: 1024,
  runs: 256,
  trajectories: 256,
  steps: 10_000,
  nodes: 100_000,
  depth: 48,
  toolCalls: 128,
  results: 256,
});
export type JsonBudget = { nodes: number; trajectories: number; steps: number };
export const jsonBudget = (): JsonBudget => ({ nodes: LIMITS.nodes, trajectories: LIMITS.trajectories, steps: LIMITS.steps });
export const isRecord = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);

export function checkTextBytes(text: string, maxBytes: number = LIMITS.runBytes): void {
  // The code-unit check avoids encoding arbitrarily oversized input first.
  if (text.length > maxBytes || new TextEncoder().encode(text).byteLength > maxBytes)
    throw new Error("JSON byte limit exceeded");
}
export function parseJson(text: string): unknown {
  try { return JSON.parse(text); }
  catch { throw new Error("Invalid JSON"); } // Do not echo source fragments from JSON.parse.
}
export function checkJsonBudget(value: unknown, budget = jsonBudget()): void {
  // Iterative preflight BEFORE schema walks, recursive presentation or stringify.
  const pending: { value: unknown; depth: number }[] = [];
  function push(value: unknown, depth: number) {
    if (--budget.nodes < 0) throw new Error("JSON node budget exceeded");
    if (depth > LIMITS.depth) throw new Error("JSON depth limit exceeded");
    pending.push({ value, depth });
  }
  push(value, 0);
  while (pending.length) {
    const { value, depth } = pending.pop()!;
    if (Array.isArray(value)) {
      if (value.length > budget.nodes) throw new Error("JSON node budget exceeded");
      for (const item of value) push(item, depth + 1);
    } else if (isRecord(value)) {
      // No Object.entries allocation on a potentially very wide object.
      for (const key in value) if (Object.hasOwn(value, key)) push(value[key], depth + 1);
    }
  }
}
