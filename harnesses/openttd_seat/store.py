"""Bounded session-store summaries, executed as the seat rather than root.

The embedded scripts are self-contained stdlib Python: the game image does not
need openttd_seat installed. The host validates the home spelling, setpriv drops
privileges before Python starts, and the script verifies the actual passwd/group
mapping and UID before opening the seat's store. No exception text crosses back
into trace metadata. Descriptor-relative no-follow reads are not a sandbox against
a concurrent hostile owner renaming directories or changing regular-file data.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from verifiers.v1 import Runtime

SUMMARY_TIMEOUT_SECONDS = 15
RUNTIME_TIMEOUT_SECONDS = 20
MAX_SUMMARY_BYTES = 65536

# Keep this code self-contained: these exact strings run in the game image under
# python -I -S, with no repository or third-party modules on sys.path.
_SUMMARY_COMMON = r'''
import contextlib, grp, json, math, os, pwd, re, signal, stat, sys
from datetime import datetime

_LIMITS = {"file_bytes": 8 * 1024 * 1024, "bytes": 32 * 1024 * 1024,
           "files": 2048, "entries": 8192, "records": 20000, "nodes": 200000,
           "depth": 32, "tools": 128, "name": 128}

class Rejected(ValueError):
    pass

def _reject():
    raise Rejected("Session store rejected")

def _charge(budget, key, amount=1):
    budget[key] = budget.get(key, 0) + amount
    if budget[key] > _LIMITS[key]:
        _reject()

@contextlib.contextmanager
def _directory(root, parts):
    fd = os.dup(root)
    try:
        for part in parts:
            if not part or part in (".", "..") or "/" in part:
                _reject()
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                _reject()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            after = os.fstat(child)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(child)
                _reject()
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)

def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns

def _read(parent, name, budget):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > _LIMITS["file_bytes"]:
        _reject()
    _charge(budget, "bytes", before.st_size)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            _reject()
        chunks, remaining = [], before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != before.st_size or _identity(opened) != _identity(os.fstat(fd)):
            _reject()
        return data.decode("utf-8", errors="replace")
    finally:
        os.close(fd)

def _files(root, parts, suffix, budget):
    def visit(parent, depth):
        if depth > _LIMITS["depth"]:
            _reject()
        with _directory(root, parent) as fd:
            with os.scandir(fd) as iterator:
                entries = []
                for entry in iterator:
                    _charge(budget, "entries")
                    info = entry.stat(follow_symlinks=False)
                    directory = stat.S_ISDIR(info.st_mode)
                    if not directory and not stat.S_ISREG(info.st_mode):
                        _reject()
                    if not directory:
                        _charge(budget, "files")
                    entries.append((entry.name, directory))
            for name, directory in sorted(entries):
                if directory:
                    yield from visit(parent + (name,), depth + 1)
                elif name.endswith(suffix):
                    yield _read(fd, name, budget)
    try:
        with _directory(root, parts):
            pass
    except FileNotFoundError:
        return
    yield from visit(parts, 0)

def _decode(raw, budget):
    _charge(budget, "records")
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _LIMITS["depth"]:
                _reject()
        elif char in "]}":
            depth -= 1
    try:
        value = json.loads(raw)
    except ValueError:
        # Truncated final JSONL records are common after a program exits.
        return {}
    stack = [(iter((value,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        _charge(budget, "nodes")
        if depth > _LIMITS["depth"]:
            _reject()
        if isinstance(item, (dict, list)):
            if len(item) > _LIMITS["nodes"] - budget["nodes"]:
                _reject()
            children = (part for pair in item.items() for part in pair) if isinstance(item, dict) else iter(item)
            stack.append((children, depth + 1))
    return _dict(value)

def _dict(value):
    return value if isinstance(value, dict) else {}

def _number(value):
    if value is None:
        return 0
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**15:
        _reject()
    return value

def _ms(value):
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None

def _time(out, value):
    if value is not None:
        value = _number(value)
        out["first_ms"] = value if out["first_ms"] is None else min(out["first_ms"], value)
        out["last_ms"] = value if out["last_ms"] is None else max(out["last_ms"], value)

def _tool(out, name):
    name = name or "?"
    if not isinstance(name, str) or len(name) > _LIMITS["name"]:
        _reject()
    if name not in out["tools"] and len(out["tools"]) >= _LIMITS["tools"]:
        _reject()
    out["tool_calls"] += 1
    out["tools"][name] = out["tools"].get(name, 0) + 1

def _collect(home, kind):
    out = {"store": kind, "messages": 0, "assistant_messages": 0, "output_tokens": 0,
           "reasoning_tokens": 0, "input_tokens": 0, "cache_read_tokens": 0, "tool_calls": 0,
           "tools": {}, "first_ms": None, "last_ms": None}
    budget = {}
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with _directory(root, home.split("/")[1:]) as fd:
            if kind == "codex_cli":
                out["sessions"] = 0
                for raw in _files(fd, (".codex", "sessions"), ".jsonl", budget):
                    out["sessions"] += 1
                    total = {}
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        record = _decode(line, budget)
                        payload = _dict(record.get("payload"))
                        _time(out, _ms(record.get("timestamp")))
                        if record.get("type") == "event_msg" and payload.get("type") == "token_count":
                            total = _dict(_dict(payload.get("info")).get("total_token_usage")) or total
                        elif record.get("type") == "response_item":
                            event = payload.get("type")
                            if event == "message":
                                out["messages"] += 1
                                out["assistant_messages"] += payload.get("role") == "assistant"
                            elif event in ("function_call", "custom_tool_call", "local_shell_call"):
                                _tool(out, payload.get("name") or event)
                    for dest, src in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                                      ("cache_read_tokens", "cached_input_tokens"), ("reasoning_tokens", "reasoning_output_tokens")):
                        out[dest] += _number(total.get(src))
            elif kind == "claude_code":
                for raw in _files(fd, (".claude", "projects"), ".jsonl", budget):
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        record = _decode(line, budget)
                        if record.get("type") not in ("user", "assistant"):
                            continue
                        out["messages"] += 1
                        _time(out, _ms(record.get("timestamp")))
                        if record["type"] != "assistant":
                            continue
                        out["assistant_messages"] += 1
                        message = _dict(record.get("message"))
                        usage = _dict(message.get("usage"))
                        for dest, src in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                                          ("cache_read_tokens", "cache_read_input_tokens")):
                            out[dest] += _number(usage.get(src))
                        content = message.get("content") or []
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    _tool(out, block.get("name"))
            elif kind == "opencode":
                base = (".local", "share", "opencode", "storage")
                for raw in _files(fd, base + ("message",), ".json", budget):
                    message = _decode(raw, budget)
                    if not message:
                        continue
                    out["messages"] += 1
                    out["assistant_messages"] += message.get("role") == "assistant"
                    tokens = _dict(message.get("tokens"))
                    for dest, src in (("input_tokens", "input"), ("output_tokens", "output"), ("reasoning_tokens", "reasoning")):
                        out[dest] += _number(tokens.get(src))
                    out["cache_read_tokens"] += _number(_dict(tokens.get("cache")).get("read"))
                    _time(out, _dict(message.get("time")).get("created"))
                for raw in _files(fd, base + ("part",), ".json", budget):
                    part = _decode(raw, budget)
                    if part.get("type") == "tool":
                        _tool(out, part.get("tool"))
            else:
                _reject()
    except (OSError, ValueError, RecursionError):
        # No source paths, content, traceback, or platform exception text in errors.
        raise Rejected("Session store rejected") from None
    finally:
        os.close(root)
    return out

def _validate_identity(home):
    match = re.fullmatch(r"/home/(agent[1-9][0-9]*)", home)
    if not match:
        _reject()
    user = match.group(1)
    account = pwd.getpwnam(user)
    group = grp.getgrnam(user)
    if (account.pw_name != user or account.pw_dir != home or account.pw_uid <= 0 or account.pw_gid <= 0
            or os.getuid() != account.pw_uid or os.geteuid() != account.pw_uid
            or os.getgid() != account.pw_gid or os.getegid() != account.pw_gid or group.gr_gid != account.pw_gid):
        _reject()
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with _directory(root, ("home", user)) as fd:
            if os.fstat(fd).st_uid != account.pw_uid:
                _reject()
    finally:
        os.close(root)

def _main(kind):
    try:
        signal.alarm(10)
        home = sys.argv[1]
        _validate_identity(home)
        data = json.dumps(_collect(home, kind), allow_nan=False)
        if len(data.encode("utf-8")) > 65536:
            _reject()
        print(data)
    except Exception:
        print('{"error":"Session summary unavailable"}')
        sys.exit(1)
'''

OPENCODE_SUMMARY = _SUMMARY_COMMON + '\nif __name__ == "__main__":\n    _main("opencode")\n'
CLAUDE_SUMMARY = _SUMMARY_COMMON + '\nif __name__ == "__main__":\n    _main("claude_code")\n'
CODEX_SUMMARY = _SUMMARY_COMMON + '\nif __name__ == "__main__":\n    _main("codex_cli")\n'


async def _run_summary(runtime: Runtime, script: str, home: str) -> dict[str, Any]:
    error = {"error": "Session summary unavailable"}
    if not isinstance(home, str) or len(home) > 256 or not re.fullmatch(r"/home/agent[1-9][0-9]*", home):
        return error
    user = home.rsplit("/", 1)[1]
    # The timeout supervisor must share the seat UID too, so it can kill its
    # child without relying on a privileged root process retaining CAP_KILL.
    argv = ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--no-new-privs",
            "timeout", "--signal=KILL", f"{SUMMARY_TIMEOUT_SECONDS}s",
            "python3", "-I", "-S", "-c", script, home]
    try:
        result = await asyncio.wait_for(runtime.run(argv, {}), timeout=RUNTIME_TIMEOUT_SECONDS)
        if result.exit_code != 0 or len(result.stdout) > MAX_SUMMARY_BYTES:
            return error
        summary = json.loads(result.stdout)
        if not isinstance(summary, dict) or summary.get("store") not in ("codex_cli", "claude_code", "opencode"):
            return error
        return summary
    except Exception:
        return error


async def opencode_session_summary(runtime: Runtime, home: str) -> dict[str, Any]:
    return await _run_summary(runtime, OPENCODE_SUMMARY, home)


async def claude_session_summary(runtime: Runtime, home: str) -> dict[str, Any]:
    return await _run_summary(runtime, CLAUDE_SUMMARY, home)


async def codex_session_summary(runtime: Runtime, home: str) -> dict[str, Any]:
    return await _run_summary(runtime, CODEX_SUMMARY, home)
