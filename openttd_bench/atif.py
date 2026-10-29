"""Lossless Prime/Codex -> ATIF-v1.8 conversion (stdlib only).

Normalized steps are a view, not the archival record: extra.source retains every
original field/record, including copied context, encrypted reasoning and unknown
events. Prime parent graphs and Codex subagent sessions are never flattened away.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openttd_bench.safe_files import BoundaryError, Budget, InvalidJSON, Limits, SafeRoot, check_value, decode_json, decode_records

VERSION = "ATIF-v1.8"


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text(v.get("text", v)) if isinstance(v, dict) else text(v) for v in value)
    if isinstance(value, dict) and value.get("type") == "encrypted_content":
        return "[Encrypted content retained in extra.source]"
    return json.dumps(value, ensure_ascii=False)


def arguments(value: Any) -> dict:
    if isinstance(value, str):
        try:
            # Embedded tool JSON is another input boundary, not trusted structure.
            decoded = decode_json(value, Budget())
        except InvalidJSON:
            return {"raw": value}
    else:
        decoded = value
    return decoded if isinstance(decoded, dict) else {"raw": decoded}


def timestamp(value: Any) -> str | None:
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return value


def step(source: str, message: Any = "", **kwargs: Any) -> dict:
    return {"step_id": 0, "source": source, "message": text(message),
            **{k: v for k, v in kwargs.items() if v is not None}}


def finish(doc: dict) -> dict:
    if not doc["steps"]:
        doc["steps"] = [step("system", "No conversation records captured.", extra={"synthetic": True})]
    for i, s in enumerate(doc["steps"], 1):
        s["step_id"] = i
    metrics = [s["metrics"] for s in doc["steps"] if s.get("metrics") and not s.get("is_copied_context")]
    doc["final_metrics"] = {"total_steps": len(doc["steps"])}
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        values = [m[key] for m in metrics if key in m]
        if values:
            doc["final_metrics"]["total_" + key] = sum(values)
    return doc


def usage_metrics(usage: dict) -> dict:
    result = {"extra": {"usage": usage}}
    for dest, keys in {"prompt_tokens": ("prompt_tokens", "input_tokens"),
                       "completion_tokens": ("completion_tokens", "output_tokens"),
                       "cached_tokens": ("cached_input_tokens",)}.items():
        for key in keys:
            if key in usage:
                result[dest] = usage[key]
                break
    return result


def tool_call(call: dict) -> dict:
    fn = call.get("function") or call
    return {"tool_call_id": call["id"], "function_name": fn.get("name", "tool"),
            "arguments": arguments(fn.get("arguments", {}))}


def add_result(s: dict, content: Any, call_id: str | None = None, **extra: Any) -> dict:
    result = {"content": text(content)}
    if call_id is not None:
        result["source_call_id"] = call_id
    if extra:
        result["extra"] = extra
    s.setdefault("observation", {"results": []})["results"].append(result)
    return result


def prime_trajectory(trace: dict, episode: dict | None = None, *, limits: Limits | None = None) -> dict:
    """Partition explicitly identifiable subagents; preserve other context roots in-place.

    Prime has no thread IDs. Only an exact Agent prompt match identifies a child;
    unrelated/restarted/compacted roots are not guessed to be subagents.
    """
    limits = limits or Limits()
    check_value(trace, Budget(limits))
    if len(trace.get("nodes", [])) > limits.graph_nodes or len(trace.get("calls", [])) > limits.graph_nodes:
        raise BoundaryError("Prime graph limit exceeded")
    work = 0
    cfg = trace.get("agent", {}).get("config", {})
    harness = cfg.get("harness", {})
    tid = trace["id"]
    doc = {"schema_version": VERSION, "trajectory_id": tid, "session_id": tid,
           "agent": {"name": harness.get("id", "prime"), "version": harness.get("version", "unknown"),
                     "model_name": cfg.get("model")}, "steps": [],
           "extra": {"benchmark": {k: v for k, v in trace.items() if k not in ("nodes", "calls")},
                     "episode": episode or {"id": tid},
                     "source": {"format": "prime", "trace": trace}}}
    nodes = trace.get("nodes", [])
    roots = {}
    for i, n in enumerate(nodes):
        parent = n.get("parent")
        if parent is not None and parent not in roots:
            raise ValueError(f"{tid}: invalid parent {parent} at node {i}")
        roots[i] = roots[parent] if parent is not None else i
    child_roots: dict[int, tuple[int, str, str]] = {}
    for i, n in enumerate(nodes):
        if not n.get("sampled"):
            continue
        for call in n["message"].get("tool_calls") or []:
            fn = call.get("function") or call
            if fn.get("name") not in ("Agent", "Task"):
                continue
            args = arguments(fn.get("arguments"))
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                continue
            work += len(nodes)
            if work > limits.graph_work:
                raise BoundaryError("Prime graph work limit exceeded")
            matches = {roots[j] for j, m in enumerate(nodes) if j > i and roots[j] != roots[i]
                       and m["message"]["role"] == "user" and prompt in text(m["message"].get("content"))}
            if len(matches) == 1:
                child_roots[next(iter(matches))] = (i, call["id"], args.get("description", "subagent"))
    docs = {r: {"schema_version": VERSION, "trajectory_id": f"{tid}-branch-{r}", "session_id": tid,
                "agent": {**doc["agent"], "name": name}, "steps": [],
                "extra": {"parent_trajectory_id": tid, "prime_root_node": r}}
            for r, (_, _, name) in child_roots.items()}
    calls = defaultdict(list)
    failed_calls = []
    for index, c in enumerate(trace.get("calls", [])):
        if c.get("node") is None:
            # A transport failure may produce no assistant node. Keep the exact
            # record and surface it without inventing a node/session association.
            if not c.get("error"):
                raise ValueError(f"{tid}: call {index} has neither a node nor an error")
            failed_calls.append((index, c))
        else:
            calls[c["node"]].append(c)
    by_node: dict[int, dict] = {}
    call_steps: dict[tuple[int, str], dict] = {}
    for i, n in enumerate(nodes):
        m = n["message"]
        role = m["role"]
        owner = docs.get(roots[i], doc)
        common = {"timestamp": timestamp(n.get("timestamp")),
                  "extra": {"prime_node": i, "prime_parent": n.get("parent"), "prime_root": roots[i]}}
        if role == "tool":
            cid = m.get("tool_call_id")
            # Resolve within this context graph, never across independent subagents.
            s = call_steps.get((roots[i], cid))
            if s is None:
                s = step("system", **common)
                owner["steps"].append(s)
                add_result(s, m.get("content"), original_call_id=cid, prime_node=i)
            else:
                add_result(s, m.get("content"), cid, prime_node=i, timestamp=n.get("timestamp"))
            by_node[i] = s
            continue
        s = step("agent" if role == "assistant" else "user" if role == "user" else "system",
                 m.get("content"), **common)
        if role == "assistant":
            s["is_copied_context"] = not n.get("sampled", False)
            if m.get("reasoning_content"):
                s["reasoning_content"] = text(m["reasoning_content"])
            if m.get("tool_calls"):
                s["tool_calls"] = [tool_call(c) for c in m["tool_calls"]]
                for c in s["tool_calls"]:
                    call_steps[(roots[i], c["tool_call_id"])] = s
            if calls[i]:
                cs = calls[i]
                s["llm_call_count"] = len(cs)
                s["model_name"] = cs[-1].get("model", cfg.get("model"))
                ms = [usage_metrics(c.get("usage", {})) for c in cs]
                s["metrics"] = {k: sum(m.get(k, 0) for m in ms) for k in
                                ("prompt_tokens", "completion_tokens", "cached_tokens") if any(k in m for m in ms)}
                s["metrics"]["extra"] = {"calls": cs}
        owner["steps"].append(s)
        by_node[i] = s
    # Proxy graphs often attach a result to the harness's re-sent assistant,
    # rather than to its sampled original (sometimes across a new context root).
    # Surface those observations on the sampled call too, without crossing agents.
    for owner in [doc, *docs.values()]:
        observations = defaultdict(list)
        for s in owner["steps"]:
            for result in s.get("observation", {}).get("results", []):
                cid = result.get("source_call_id")
                if cid:
                    observations[cid].append(result)
        for s in owner["steps"]:
            if s["source"] != "agent" or s.get("is_copied_context"):
                continue
            for c in s.get("tool_calls", []):
                existing = s.get("observation", {}).get("results", [])
                work += len(existing) + len(observations[c["tool_call_id"]])
                if work > limits.graph_work:
                    raise BoundaryError("Prime graph work limit exceeded")
                if not any(r.get("source_call_id") == c["tool_call_id"] for r in existing):
                    seen_content = set()
                    for result in observations[c["tool_call_id"]]:
                        if result["content"] not in seen_content:
                            s.setdefault("observation", {"results": []})["results"].append(dict(result))
                            seen_content.add(result["content"])
    for r, (parent_node, cid, _) in reversed(list(child_roots.items())):
        parent_doc = docs.get(roots[parent_node], doc)
        child = finish(docs[r])
        child["extra"]["parent_trajectory_id"] = parent_doc["trajectory_id"]
        parent_doc.setdefault("subagent_trajectories", []).append(child)
        s = by_node[parent_node]
        results = s.get("observation", {}).get("results", [])
        result = next((v for v in results if v.get("source_call_id") == cid), None)
        if result is None:
            result = add_result(s, "", cid)
        result.setdefault("subagent_trajectory_ref", []).append({"trajectory_id": child["trajectory_id"]})
    for index, c in failed_calls:
        doc["steps"].append(step(
            "system", f"Model request failed before an assistant node was recorded: {text(c['error'])}",
            timestamp=timestamp(c.get("time", {}).get("start")),
            llm_call_count=1, model_name=c.get("model"),
            metrics=usage_metrics(c["usage"]) if c.get("usage") else None,
            extra={"prime_call_index": index, "unattached_failed_call": c},
        ))
    return finish(doc)


def codex_trajectory(records: list[dict], *, limits: Limits | None = None) -> dict:
    limits = limits or Limits()
    if len(records) > limits.records:
        raise BoundaryError("Artifact records limit exceeded")
    check_value(records, Budget(limits))
    meta = next(r["payload"] for r in records if r.get("type") == "session_meta")
    tid = meta["id"]
    origin = meta.get("source", {})
    spawn = origin.get("subagent", {}).get("thread_spawn", {}) if isinstance(origin, dict) and isinstance(origin.get("subagent"), dict) else {}
    parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
    doc = {"schema_version": VERSION, "trajectory_id": tid, "session_id": meta.get("session_id", tid),
           "agent": {"name": meta.get("agent_nickname") or "codex-cli", "version": meta.get("cli_version", "unknown")},
           "steps": [], "extra": {"parent_trajectory_id": parent,
                                     "source": {"format": "codex", "records": records}}}
    steps = doc["steps"]
    if meta.get("base_instructions"):
        steps.append(step("system", meta["base_instructions"].get("text", "")))
    call_steps = {}
    last_agent = None
    model = None
    usage_records = any(r.get("type") == "token_usage_record" for r in records)
    seen_usage = set()
    for i, r in enumerate(records):
        p = r.get("payload", {})
        kind = p.get("type")
        common = {"timestamp": r.get("timestamp"), "extra": {"codex_record": i}}
        copied = r.get("ordinal", i) < meta.get("subagent_history_start_ordinal", -1)
        if r["type"] == "turn_context":
            model = p.get("model", model)
            if model:
                doc["agent"]["model_name"] = model
        elif r["type"] == "response_item":
            if kind in ("function_call_output", "custom_tool_call_output"):
                cid = p.get("call_id")
                s = call_steps.get(cid)
                if s is None:
                    s = step("system", **common)
                    steps.append(s)
                    add_result(s, p.get("output"), original_call_id=cid, codex_record=i)
                else:
                    add_result(s, p.get("output"), cid, codex_record=i, timestamp=r.get("timestamp"))
                continue
            if kind in ("message", "agent_message"):
                role = p.get("role", "user")
                s = step("agent" if role == "assistant" else "user" if role == "user" else "system", p.get("content"), **common)
            elif kind == "reasoning":
                s = step("agent", reasoning_content=text(p.get("summary") or p.get("content")), **common)
                if p.get("encrypted_content"):
                    s["extra"]["encrypted_reasoning"] = True
                    if not s.get("reasoning_content"):
                        s["message"] = "[Encrypted reasoning retained in extra.source]"
            elif kind in ("function_call", "custom_tool_call"):
                s = step("agent", tool_calls=[{"tool_call_id": p["call_id"], "function_name": p["name"],
                         "arguments": arguments(p.get("arguments", p.get("input", {})))}], **common)
                call_steps[p["call_id"]] = s
            else:
                s = step("system", text(p), **common)
            if copied:
                s["is_copied_context"] = True
            if s["source"] == "agent":
                last_agent = s
                if model:
                    s["model_name"] = model
            steps.append(s)
        elif r["type"] == "compacted":
            steps.append(step("system", p.get("message", "Context compacted"), **common))
        elif r["type"] == "token_usage_record" or (not usage_records and r["type"] == "event_msg" and kind == "token_count"):
            u = p.get("usage") if usage_records else (p.get("info") or {}).get("last_token_usage")
            identity = p.get("response_id") if usage_records else json.dumps((p.get("info") or {}).get("total_token_usage"), sort_keys=True)
            if u and last_agent is not None and (identity is None or identity not in seen_usage):
                seen_usage.add(identity)
                m = usage_metrics(u)
                if "metrics" in last_agent:
                    old = last_agent["metrics"]
                    for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                        if k in old:
                            m[k] = m.get(k, 0) + old[k]
                last_agent["metrics"] = m
    return finish(doc)


def _load_codex(root: SafeRoot, directory: Path, budget: Budget) -> tuple[list, list]:
    sessions, artifacts = [], []
    for file, is_dir in root.walk(budget, directory):
        if is_dir or file.suffix != ".jsonl":
            continue
        data = root.read(file, budget)
        if file.name == "codex-exec.jsonl":
            try:
                artifacts.append({"path": str(root.path / file), "content": data.decode("utf-8")})
            except UnicodeError:
                raise BoundaryError("Invalid artifact encoding") from None
        else:
            records = [r for _, r in decode_records(data, budget)]
            sessions.append((str(root.path / file), hashlib.sha256(data).hexdigest(), records))
    return sessions, artifacts


def codex_forest(directory: Path, *, limits: Limits | None = None) -> list[dict]:
    """Read an explicit operator-approved tree; only its root may be a symlink."""
    budget = Budget(limits)
    with SafeRoot(directory) as root:
        return _codex_forest(_load_codex(root, Path("."), budget), budget.limits)


def _codex_forest(loaded: tuple[list, list], limits: Limits) -> list[dict]:
    sessions, artifacts = loaded
    docs = {}
    for path, digest, records in sessions:
        if not records or not any(r.get("type") == "session_meta" for r in records):
            continue
        doc = codex_trajectory(records, limits=limits)
        doc["extra"]["source"].update(path=path, sha256=digest)
        tid = doc["trajectory_id"]
        if tid in docs:
            # Crash-safe snapshots can duplicate the final harvest. Only accept prefixes.
            old = docs[tid]["extra"]["source"]["records"]
            if old == records[:len(old)]:
                docs[tid] = doc
            elif records != old[:len(records)]:
                raise ValueError(f"Conflicting Codex snapshots for {tid}")
        else:
            docs[tid] = doc
    roots = []
    work = 0
    for tid, doc in docs.items():
        parent = doc["extra"]["parent_trajectory_id"]
        if parent in docs:
            # Reject cycles, which would otherwise vanish from the output forest.
            seen = {tid}
            cursor = parent
            while cursor in docs:
                if cursor in seen:
                    raise ValueError(f"Codex parent cycle: {tid}")
                seen.add(cursor)
                if len(seen) > limits.depth:
                    raise BoundaryError("Codex graph depth limit exceeded")
                cursor = docs[cursor]["extra"]["parent_trajectory_id"]
            docs[parent].setdefault("subagent_trajectories", []).append(doc)
            meta = next(r["payload"] for r in doc["extra"]["source"]["records"] if r["type"] == "session_meta")
            agent_path = meta.get("agent_path")
            for s in docs[parent]["steps"]:
                work += 1
                if work > limits.graph_work:
                    raise BoundaryError("Codex graph work limit exceeded")
                if not any(c["function_name"] == "spawn_agent" for c in s.get("tool_calls", [])):
                    continue
                for result in s.get("observation", {}).get("results", []):
                    output = arguments(result.get("content"))
                    if output.get("agent_id") == tid or (agent_path and output.get("task_name") == agent_path):
                        result.setdefault("subagent_trajectory_ref", []).append({"trajectory_id": tid})
        else:
            roots.append(doc)
    if roots and artifacts:
        roots[0]["extra"]["source"]["artifacts"] = artifacts
    return roots


def resolve_harvest(path: str, run: Path) -> Path:
    """Treat trace paths only as run-local hints, never as filesystem authority.

    The sole implicit root is RUN/raw/codex. For relocation, a historical prefix
    ending in raw/codex is discarded, even if that old absolute path still exists.
    No cwd, sibling archived run, or basename-only external fallback is consulted.
    Symlink checks happen through the selected run descriptor, not Path.resolve().
    """
    if not isinstance(path, str) or not path or len(path) > 4096 or "\\" in path or "\x00" in path:
        raise BoundaryError("Unsafe harvested_to path")
    parts = path.split("/")
    if path.startswith("/"):
        parts = parts[1:]
    if any(p in ("", ".", "..") for p in parts):
        raise BoundaryError("Unsafe harvested_to path")
    markers = [i for i in range(len(parts) - 1) if parts[i:i + 2] == ["raw", "codex"]]
    if len(markers) != 1:
        raise BoundaryError("harvested_to must identify RUN/raw/codex")
    tail = parts[markers[0] + 2:]
    if not tail or not re.fullmatch(r"agent[1-9][0-9]*(?:-[A-Za-z0-9_.-]+)?", tail[0]):
        raise BoundaryError("harvested_to must identify a seat directory")
    return run / "raw" / "codex" / Path(*tail)


def _seat_name(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"agent[1-9][0-9]*", value):
        raise BoundaryError("Missing/ambiguous Codex sessions: valid seat_user required")
    return value


def _seat_matches(name: str, seat: str) -> bool:
    return name == seat or name.startswith(seat + "-")


def _select_codex(trace: dict, root: SafeRoot, approved: list[SafeRoot], trees: dict) -> tuple[SafeRoot, Path] | None:
    info = trace.get("info", {})
    session = info.get("harness_session", {})
    harvest = session.get("harvested_to")
    is_codex = trace.get("agent", {}).get("config", {}).get("harness", {}).get("id") in ("codex-cli", "codex_cli")
    if harvest is None and not is_codex:
        return None
    if harvest is not None:
        directory = resolve_harvest(harvest, root.path).relative_to(root.path)
        if "seat_user" in info and not _seat_matches(directory.parts[2], _seat_name(info["seat_user"])):
            raise BoundaryError("Codex harvest seat mismatch")
        if not root.is_dir(directory):
            raise BoundaryError("Missing/ambiguous Codex sessions")
        return root, directory
    seat = _seat_name(info.get("seat_user"))
    candidates = {}
    # Only direct seat children of an approved root can be inferred. Never walk
    # arbitrary trees by basename or use an empty seat to select all histories.
    for base in [root, *approved]:
        prefix = Path("raw/codex") if base is root else Path(".")
        if base is not root and _seat_matches(base.path.name, seat):
            candidates[base.path] = (base, Path("."))
        for relative, is_dir in trees[base.path]:
            if is_dir and relative.parent == prefix and _seat_matches(relative.name, seat):
                candidates[base.path / relative] = (base, relative)
    if len(candidates) != 1:
        raise BoundaryError("Missing/ambiguous Codex sessions; supply an explicit seat directory")
    return next(iter(candidates.values()))


def _encode(doc: dict, limits: Limits, remaining: int) -> bytes:
    # Preflight the expanded graph and stream encoding under a shared output cap.
    check_value(doc, Budget(replace(limits, depth=limits.depth * 4, nodes=limits.nodes * 8,
                                   total_bytes=limits.output_bytes)))
    chunks = []
    size = 1
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False).iterencode(doc):
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > remaining:
            raise BoundaryError("Artifact output bytes limit exceeded")
        chunks.append(encoded)
    return b"".join(chunks) + b"\n"


def convert_run(run: Path, codex_dirs: list[Path] | None = None, *, limits: Limits | None = None) -> dict:
    """Convert only bounded regular inputs, then prevalidate and publish manifest-last.

    `run` and `codex_dirs` are explicit operator selections, so their root symlinks
    may be canonicalized. Untrusted harvested_to paths cannot add approved roots.
    Descendant symlinks/special files are rejected, including publication targets.
    """
    budget = Budget(limits)
    limits = budget.limits
    with ExitStack() as stack:
        root = stack.enter_context(SafeRoot(run))
        root.validate_target("trajectories/index.json")
        approved = []
        roots = {root.path}
        for path in codex_dirs or []:
            selected = stack.enter_context(SafeRoot(path))
            if selected.path not in roots:
                roots.add(selected.path)
                approved.append(selected)
        trees = {base.path: base.walk(budget, exclude=("trajectories",) if base is root else ())
                 for base in [root, *approved]}
        jobs, loaded, consumed = [], {}, set()
        # Complete byte/record/node/depth preflight of all selected inputs before
        # invoking either converter. Failures cannot partially publish a run.
        for file, is_dir in trees[root.path]:
            if is_dir or file.name != "traces.jsonl":
                continue
            for line_number, record in decode_records(root.read(file, budget), budget):
                episode = {k: v for k, v in record.items() if k != "traces"} if "traces" in record else {"id": record["id"]}
                traces = record.get("traces", [record])
                if not isinstance(traces, list) or len(traces) > limits.graph_nodes:
                    raise BoundaryError("Invalid or oversized trace collection")
                for trace in traces:
                    if not isinstance(trace, dict):
                        raise BoundaryError("Trace must be an object")
                    selected = _select_codex(trace, root, approved, trees)
                    key = None
                    if selected:
                        base, directory = selected
                        key = base.path / directory
                        if key in consumed:
                            raise BoundaryError("Codex session tree reused across traces")
                        if key not in loaded:
                            loaded[key] = _load_codex(base, directory, budget)
                        consumed.add(key)
                    jobs.append((trace, episode, file, line_number, key))
        standalone = []
        for base in approved:
            if any(base.path == p or base.path in p.parents for p in consumed):
                continue
            if base.path not in loaded:
                loaded[base.path] = _load_codex(base, Path("."), budget)
            standalone.append(base.path)
        documents = []
        for trace, episode, file, line_number, key in jobs:
            doc = prime_trajectory(trace, episode, limits=limits)
            doc["extra"]["source"].update(path=str(file), line=line_number)
            if key is not None:
                native = _codex_forest(loaded[key], limits)
                if not native:
                    raise BoundaryError("Missing/ambiguous Codex sessions")
                if len(native) == 1:
                    native[0]["extra"].update(benchmark=doc["extra"]["benchmark"], episode=episode,
                                              prime_source=doc["extra"]["source"])
                    doc = native[0]
                else:
                    doc["subagent_trajectories"] = native
                    doc["notes"] = "Multiple native root sessions for this seat; retained independently."
            documents.append(doc)
        for key in standalone:
            documents.extend(_codex_forest(loaded[key], limits))
        if not documents:
            raise ValueError("No trajectories found in selected run")
        entries, outputs = [], []
        remaining = limits.output_bytes
        for doc in documents:
            data = _encode(doc, limits, remaining)
            remaining -= len(data)
            digest = hashlib.sha256(data).hexdigest()
            filename = f"{digest}.atif.json"
            ep = doc.get("extra", {}).get("episode", {"id": doc["session_id"]})
            entries.append({"file": filename, "trajectory_id": doc["trajectory_id"], "episode_id": ep["id"],
                            "sha256": digest, "size": len(data)})
            outputs.append((Path("trajectories") / filename, data))
        manifest = {"format": "openttd-atif-index-v1", "run": root.path.name, "trajectories": entries}
        outputs.append((Path("trajectories/index.json"), _encode(manifest, limits, remaining)))
        # Validate ALL targets before mkdir or the first write, retaining the old
        # manifest and external canaries if any target is an unsafe descendant.
        for path, _ in outputs:
            root.validate_target(path)
        root.mkdir("trajectories")
        for path, data in outputs:
            root.atomic_write(path, data)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run directory; writes RUN/trajectories")
    parser.add_argument("--codex-dir", type=Path, action="append", default=[],
                        help="explicitly approve another Codex tree (repeatable; root symlink allowed, descendants forbidden)")
    args = parser.parse_args()
    manifest = convert_run(args.run, args.codex_dir)
    print(f"{args.run}/trajectories: {len(manifest['trajectories'])} root trajectories")


if __name__ == "__main__":
    main()
