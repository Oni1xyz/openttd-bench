/* ATIF-only reader and presentation helpers. No native/Prime parsing lives here. */
import type { DynamicToolUIPart, UIMessage } from "ai";
import { LIMITS, checkJsonBudget, checkTextBytes, isRecord, jsonBudget, parseJson, type JsonBudget } from "./limits";

export type Benchmark = {
  id?: string;
  ok?: boolean;
  stop_condition?: string | null;
  info?: Record<string, unknown>;
  rewards?: Record<string, { score?: number; value?: number; weight?: number } | number>;
  errors?: { type?: string; message?: string }[];
  timing?: { agent?: { start?: number; end?: number } };
  agent?: { name?: string; config?: { model?: string; harness?: { id?: string; user?: string }; runtime?: { type?: string } } };
  task?: { data?: { name?: string; idx?: number } };
};
export type AtifStep = {
  step_id: number;
  source: "system" | "user" | "agent";
  message: unknown;
  timestamp?: string;
  model_name?: string;
  reasoning_content?: string;
  is_copied_context?: boolean;
  tool_calls?: { tool_call_id: string; function_name: string; arguments: Record<string, unknown> }[];
  observation?: { results: { source_call_id?: string; content?: unknown; subagent_trajectory_ref?: { trajectory_id?: string; trajectory_path?: string }[] }[] };
  metrics?: { prompt_tokens?: number; completion_tokens?: number; cached_tokens?: number; extra?: {
    usage?: Record<string, number>;
    calls?: { usage?: { reasoning_tokens?: number }; time?: { start: number; end: number }; finish_reason?: string }[];
  } };
};
export type Trajectory = {
  schema_version: string;
  trajectory_id?: string;
  session_id?: string;
  agent: { name: string; version: string; model_name?: string };
  steps: AtifStep[];
  subagent_trajectories?: Trajectory[];
  extra?: { benchmark?: Benchmark; episode?: Omit<Episode, "traces">; parent_trajectory_id?: string; [key: string]: unknown };
};
export type Episode = { id: string; ok?: boolean; task?: Benchmark["task"]; traces: Trajectory[] };
export type ParsedRun = { name: string; episodes: Episode[] };
export const traceId = (t: Trajectory): string => t.trajectory_id ?? t.session_id ?? t.agent.name;
export const traceMeta = (t: Trajectory): Benchmark => t.extra?.benchmark ?? {};

function optionalStrings(value: Record<string, unknown>, keys: string[]): void {
  for (const key of keys) if (value[key] !== undefined && typeof value[key] !== "string") throw new Error("Invalid ATIF string");
}
function optionalArray(value: unknown, max: number): unknown[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new Error("Invalid ATIF collection");
  if (value.length > max) throw new Error("ATIF collection limit exceeded");
  return value;
}
export function validateTrajectories(value: unknown, budget: JsonBudget = jsonBudget(), allowArray = true): Trajectory[] {
  checkJsonBudget(value, budget);
  const roots = allowArray && Array.isArray(value) ? value : [value];
  if (!roots.length) throw new Error("No ATIF trajectories");
  if (roots.length > budget.trajectories) throw new Error("ATIF trajectory limit exceeded");
  const pending = [...roots];
  while (pending.length) {
    const t = pending.pop();
    if (--budget.trajectories < 0) throw new Error("ATIF trajectory limit exceeded");
    if (!isRecord(t) || typeof t.schema_version !== "string" || !/^ATIF-v1\.[0-8]$/.test(t.schema_version) ||
        !isRecord(t.agent) || typeof t.agent.name !== "string" || typeof t.agent.version !== "string" ||
        !Array.isArray(t.steps) || !t.steps.length) throw new Error("Expected an ATIF trajectory (convert native traces first)");
    optionalStrings(t, ["trajectory_id", "session_id"]);
    optionalStrings(t.agent, ["model_name"]);
    if (t.extra !== undefined) {
      if (!isRecord(t.extra)) throw new Error("Invalid ATIF metadata");
      for (const key of ["episode", "benchmark"]) if (t.extra[key] !== undefined && !isRecord(t.extra[key])) throw new Error("Invalid ATIF metadata");
      if (isRecord(t.extra.episode)) optionalStrings(t.extra.episode, ["id"]);
    }
    if (t.steps.length > budget.steps) throw new Error("ATIF step limit exceeded");
    budget.steps -= t.steps.length;
    for (const [i, step] of t.steps.entries()) {
      if (!isRecord(step) || step.step_id !== i + 1 || typeof step.source !== "string" || !["system", "user", "agent"].includes(step.source)) throw new Error("Invalid ATIF step");
      optionalStrings(step, ["reasoning_content", "timestamp", "model_name"]);
      if (step.is_copied_context !== undefined && typeof step.is_copied_context !== "boolean") throw new Error("Invalid ATIF context flag");
      const ids = new Set<string>();
      for (const call of optionalArray(step.tool_calls, LIMITS.toolCalls)) {
        if (!isRecord(call) || typeof call.tool_call_id !== "string" || typeof call.function_name !== "string" || !isRecord(call.arguments) || ids.has(call.tool_call_id))
          throw new Error("Invalid or duplicate ATIF tool call");
        ids.add(call.tool_call_id);
      }
      if (step.observation !== undefined) {
        if (!isRecord(step.observation) || !Array.isArray(step.observation.results)) throw new Error("Invalid ATIF observation");
        for (const result of optionalArray(step.observation.results, LIMITS.results)) {
          if (!isRecord(result)) throw new Error("Invalid ATIF observation");
          optionalStrings(result, ["source_call_id"]);
          for (const ref of optionalArray(result.subagent_trajectory_ref, LIMITS.trajectories)) {
            if (!isRecord(ref)) throw new Error("Invalid ATIF reference");
            optionalStrings(ref, ["trajectory_id", "trajectory_path"]);
          }
        }
      }
    }
    for (const child of optionalArray(t.subagent_trajectories, budget.trajectories)) pending.push(child);
  }
  return roots as Trajectory[];
}

export async function parseAtifFile(file: Pick<File, "size" | "name" | "text">): Promise<ParsedRun> {
  if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > LIMITS.runBytes) throw new Error("File byte limit exceeded");
  return parseAtif(await file.text(), file.name);
}
export function parseAtif(text: string, name = "trajectory.atif.json"): ParsedRun {
  checkTextBytes(text);
  const roots = validateTrajectories(parseJson(text));
  const episodes = new Map<string, Episode>();
  function append(t: Trajectory, ep: Episode, parent?: Trajectory): void {
    if (parent) {
      const meta = traceMeta(parent);
      t = { ...t, extra: { ...t.extra, parent_trajectory_id: traceId(parent), benchmark: {
        task: meta.task, info: { openttd_company: meta.info?.openttd_company, openttd_role: `↳ ${t.agent.name}` },
        ...t.extra?.benchmark,
      } } };
    }
    ep.traces.push(t);
    for (const child of t.subagent_trajectories ?? []) append(child, ep, t);
  }
  for (const t of roots) {
    const metadata = t.extra?.episode;
    const id = metadata?.id ?? t.session_id ?? traceId(t);
    let ep = episodes.get(id);
    if (!ep) { ep = { ...metadata, id, traces: [] }; episodes.set(id, ep); }
    append(t, ep);
  }
  return { name, episodes: [...episodes.values()] };
}

export type Outcome = { score: number; terms: Record<string, number>; values: Record<string, number> };
export function traceOutcome(t: Trajectory): Outcome | null {
  const o = traceMeta(t).info?.openttd_outcome as Outcome | undefined;
  return typeof o?.score === "number" ? o : null;
}
export function traceCompany(t: Trajectory): number | null {
  const c = traceMeta(t).info?.openttd_company;
  return typeof c === "number" ? c : null;
}
export function traceRole(t: Trajectory): string {
  return (traceMeta(t).info?.openttd_role as string) ?? traceMeta(t).agent?.name ?? t.agent.name;
}
export function finalMeasurements(t: Trajectory): Record<string, unknown> | null {
  const final = traceMeta(t).info?.openttd_final as { companies?: Record<string, Record<string, unknown>>; date?: { text?: string } } | undefined;
  const c = traceCompany(t);
  const mine = c !== null ? final?.companies?.[String(c)] : undefined;
  return mine ? { ...mine, final_date: final?.date?.text } : null;
}
export function rewardValue(t: Trajectory): number | null {
  let total = 0, weight = 0;
  for (const v of Object.values(traceMeta(t).rewards ?? {})) {
    if (typeof v === "number") { total += v; weight++; }
    else if (typeof (v.score ?? v.value) === "number") { total += (v.score ?? v.value)! * (v.weight ?? 1); weight += v.weight ?? 1; }
  }
  return weight ? total / weight : null;
}
export function agentSeconds(t: Trajectory): number | null {
  const span = traceMeta(t).timing?.agent;
  if (span?.start !== undefined && span.end !== undefined) return span.end - span.start;
  const times = t.steps.map(s => s.timestamp ? Date.parse(s.timestamp) : NaN).filter(Number.isFinite);
  return times.length > 1 ? (Math.max(...times) - Math.min(...times)) / 1000 : null;
}
export function sampledTurns(t: Trajectory): number {
  return t.steps.filter(s => s.source === "agent" && !s.is_copied_context).length;
}
export type TurnStats = { promptTokens?: number; completionTokens?: number; reasoningTokens?: number; seconds?: number; finish?: string };
export type ViewerMessage = UIMessage & { node: number; stats?: TurnStats; gameDate?: string; turn?: number };
export function contentText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(v => v?.type === "text" ? v.text : JSON.stringify(v)).join("\n");
  return JSON.stringify(value);
}
export function traceToMessages(t: Trajectory, showContext = false, limit: number = LIMITS.steps): ViewerMessage[] {
  if (!Number.isSafeInteger(limit) || limit < 0 || limit > LIMITS.steps) throw new Error("Invalid message limit");
  const messages: ViewerMessage[] = [];
  let turn = 0, lastDate: string | undefined;
  for (const s of t.steps) {
    // Stop BEFORE serializing message bodies or joining observations off-screen.
    if (messages.length >= limit) break;
    if (s.is_copied_context && !showContext) continue;
    const parts: ViewerMessage["parts"] = [];
    if (s.reasoning_content) parts.push({ type: "reasoning", text: s.reasoning_content, state: "done" });
    const message = contentText(s.message);
    if (message) parts.push({ type: "text", text: message });
    const results = s.observation?.results ?? [];
    const consumed = new Set<string>();
    const outputs = results.map(r => contentText(r.content));
    const byCall = new Map<string, string[]>();
    results.forEach((r, i) => {
      if (r.source_call_id === undefined) return;
      const group = byCall.get(r.source_call_id) ?? [];
      group.push(outputs[i]);
      byCall.set(r.source_call_id, group);
    });
    let date: string | undefined;
    for (const call of s.tool_calls ?? []) {
      const matched = byCall.get(call.tool_call_id) ?? [];
      consumed.add(call.tool_call_id);
      const output = matched.join("\n");
      const part: DynamicToolUIPart = matched.length
        ? { type: "dynamic-tool", toolName: call.function_name, toolCallId: call.tool_call_id, input: call.arguments, output, state: "output-available" }
        : { type: "dynamic-tool", toolName: call.function_name, toolCallId: call.tool_call_id, input: call.arguments, state: "input-available" };
      parts.push(part);
    }
    results.forEach((r, i) => {
      const output = outputs[i];
      for (const m of output.matchAll(/"text":\s*"(\d{4}-\d{2}-\d{2})"/g)) date = m[1];
      if (r.source_call_id === undefined || !consumed.has(r.source_call_id)) parts.push({ type: "text", text: output });
      if (r.subagent_trajectory_ref?.length) parts.push({ type: "text", text: `Subagents: ${r.subagent_trajectory_ref.map(ref => ref.trajectory_id ?? ref.trajectory_path).join(", ")} (select in sidebar)` });
    });
    if (!parts.length) continue;
    const calls = s.metrics?.extra?.calls ?? [];
    const usage = s.metrics?.extra?.usage;
    const stats = s.metrics ? { promptTokens: s.metrics.prompt_tokens, completionTokens: s.metrics.completion_tokens,
      reasoningTokens: usage?.reasoning_output_tokens ?? usage?.reasoning_tokens ?? calls.reduce((n, c) => n + (c.usage?.reasoning_tokens ?? 0), 0),
      seconds: calls.length ? calls.reduce((n, c) => n + (c.time ? c.time.end - c.time.start : 0), 0) : undefined,
      finish: calls.at(-1)?.finish_reason } : undefined;
    const gameDate = date && date !== lastDate ? date : undefined;
    if (date) lastDate = date;
    messages.push({ id: `s${s.step_id}`, node: s.step_id, role: s.source === "agent" || results.length ? "assistant" : s.source,
      parts, stats, gameDate, turn: s.source === "agent" ? ++turn : undefined } as ViewerMessage);
  }
  return messages;
}
export function formatMoney(value: unknown): string {
  if (typeof value !== "number") return "–";
  return `${value < 0 ? "-" : ""}$${Math.abs(Math.round(value)).toLocaleString()}`;
}
export function formatSeconds(value: number | null | undefined): string {
  if (value == null) return "–";
  return value < 90 ? `${value.toFixed(0)} s` : `${(value / 60).toFixed(1)} min`;
}
