"""Standalone stdlib collector and unprivileged-runtime regressions (synthetic stores)."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from openttd_seat import store


class FakeRuntime:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or SimpleNamespace(exit_code=0, stdout='{"store":"codex_cli","messages":1}', stderr="")
        self.error = error

    async def run(self, argv, env):
        self.calls.append((argv, env))
        if self.error:
            raise self.error
        return self.result


class SummaryRuntimeBoundaries(unittest.TestCase):
    def test_collector_is_demoted_isolated_and_time_bounded(self):
        runtime = FakeRuntime()
        summary = asyncio.run(store.codex_session_summary(runtime, "/home/agent1"))
        argv, env = runtime.calls[0]
        self.assertIn("setpriv", argv)
        self.assertIn("--reuid=agent1", argv)
        self.assertIn("--regid=agent1", argv)
        self.assertIn("--init-groups", argv)
        self.assertIn("--no-new-privs", argv)
        self.assertIn("timeout", argv)
        self.assertLess(argv.index("setpriv"), argv.index("timeout"))
        python = argv.index("python3")
        self.assertEqual(argv[python:python + 4], ["python3", "-I", "-S", "-c"])
        self.assertEqual(argv[-1], "/home/agent1")
        self.assertEqual(summary["messages"], 1)
        self.assertEqual(env, {})

    def test_invalid_home_never_runs_any_command(self):
        for home in ("/root", "/home/agent1/../agent2", "/home/agent01", "/home/agent1/", "/work/home/agent1", "/home/agent0"):
            with self.subTest(home=home):
                runtime = FakeRuntime()
                result = asyncio.run(store.codex_session_summary(runtime, home))
                self.assertIn("error", result)
                self.assertEqual(runtime.calls, [])
                self.assertNotIn(home, result["error"])

    def test_runtime_errors_do_not_reveal_paths_or_stderr(self):
        canary = "/private/canary/PRIVATE-CONTENT"
        failures = [FakeRuntime(SimpleNamespace(exit_code=1, stdout=canary, stderr=canary)),
                    FakeRuntime(error=OSError(canary)),
                    FakeRuntime(SimpleNamespace(exit_code=0, stdout=canary, stderr=""))]
        for runtime in failures:
            with self.subTest(runtime=runtime):
                result = asyncio.run(store.codex_session_summary(runtime, "/home/agent1"))
                self.assertIn("error", result)
                self.assertNotIn(canary, json.dumps(result))

    def test_runtime_timeout_is_bounded_and_sanitized(self):
        class Hanging:
            async def run(self, argv, env):
                await asyncio.sleep(60)
        with patch.object(store, "RUNTIME_TIMEOUT_SECONDS", 0.01):
            result = asyncio.run(store.codex_session_summary(Hanging(), "/home/agent1"))
        self.assertEqual(result, {"error": "Session summary unavailable"})


class StandaloneCollectors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/work")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def write(self, rel, records, jsonl=True):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n" if jsonl else json.dumps(records))
        return path

    def run_collect(self, script, kind, timeout=3, limits=None):
        # Load the exact embedded script without its CLI main, then use a fixture
        # home. The production CLI's /home/agentN + passwd/UID gate is tested below.
        code = "import sys,json; ns={'__name__':'collector_test'}; exec(sys.argv[1],ns); ns['_LIMITS'].update(json.loads(sys.argv[4])); print(json.dumps(ns['_collect'](sys.argv[2],sys.argv[3])))"
        return subprocess.run([sys.executable, "-I", "-S", "-c", code, script, str(self.home), kind, json.dumps(limits or {})],
                              capture_output=True, text=True, timeout=timeout)

    def test_normal_stores_preserve_summary_counts(self):
        self.write(".codex/sessions/2026/one.jsonl", [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 12, "output_tokens": 3}}}}])
        self.write(".claude/projects/project/one.jsonl", [
            {"type": "assistant", "message": {"usage": {"input_tokens": 12, "output_tokens": 3}, "content": [{"type": "tool_use", "name": "exec"}]}}])
        self.write(".local/share/opencode/storage/message/session/message.json", {"role": "assistant", "tokens": {"input": 12, "output": 3}}, False)
        self.write(".local/share/opencode/storage/part/session/message/part.json", {"type": "tool", "tool": "exec"}, False)
        for script, kind in ((store.CODEX_SUMMARY, "codex_cli"), (store.CLAUDE_SUMMARY, "claude_code"), (store.OPENCODE_SUMMARY, "opencode")):
            with self.subTest(kind=kind):
                result = self.run_collect(script, kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                out = json.loads(result.stdout)
                self.assertEqual(out["messages"], 1)
                self.assertEqual(out["assistant_messages"], 1)
                self.assertEqual(out["input_tokens"], 12)
                self.assertEqual(out["output_tokens"], 3)
                self.assertEqual(out["tools"], {"exec": 1})

    def test_symlinked_store_does_not_disclose_canary(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "one.jsonl").write_text(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "PRIVATE-CANARY"}}))
        (self.home / ".codex").mkdir()
        (self.home / ".codex/sessions").symlink_to(outside, target_is_directory=True)
        result = self.run_collect(store.CODEX_SUMMARY, "codex_cli")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVATE-CANARY", result.stdout + result.stderr)
        self.assertNotIn(str(outside), result.stdout + result.stderr)

    def test_fifo_and_deep_or_oversized_records_fail_boundedly(self):
        path = self.home / ".codex/sessions/one.jsonl"
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        result = self.run_collect(store.CODEX_SUMMARY, "codex_cli")
        self.assertNotEqual(result.returncode, 0)
        path.unlink()
        path.write_text('{"extra":' + "[" * 1500 + "0" + "]" * 1500 + "}")
        result = self.run_collect(store.CODEX_SUMMARY, "codex_cli")
        self.assertNotEqual(result.returncode, 0)
        with path.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024)
        result = self.run_collect(store.CODEX_SUMMARY, "codex_cli")
        self.assertNotEqual(result.returncode, 0)

    def test_all_stores_reject_leaf_symlinks_and_budget_exhaustion(self):
        cases = ((store.CODEX_SUMMARY, "codex_cli", ".codex/sessions/session.jsonl"),
                 (store.CLAUDE_SUMMARY, "claude_code", ".claude/projects/project/session.jsonl"),
                 (store.OPENCODE_SUMMARY, "opencode", ".local/share/opencode/storage/message/session/message.json"))
        for script, kind, rel in cases:
            with self.subTest(kind=kind):
                path = self.write(rel, [{"unused": "valid"}])
                for limits in ({"records": 0}, {"nodes": 1}, {"bytes": 1}, {"entries": 0}, {"files": 0}):
                    result = self.run_collect(script, kind, limits=limits)
                    self.assertNotEqual(result.returncode, 0, (kind, limits, result.stdout))
                path.unlink()
                canary = self.base / (kind + "-PRIVATE-CANARY")
                canary.write_text('{"role":"assistant","tokens":{"input":12345}}')
                path.symlink_to(canary)
                result = self.run_collect(script, kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(str(canary), result.stdout + result.stderr)

    def test_identity_gate_checks_passwd_group_home_and_actual_uid(self):
        namespace: dict[str, Any] = {"__name__": "collector_test"}
        exec(store.CODEX_SUMMARY, namespace)
        uid, gid = os.getuid(), os.getgid()
        account = SimpleNamespace(pw_name="agent1", pw_dir="/home/agent1", pw_uid=uid, pw_gid=gid)
        group = SimpleNamespace(gr_gid=gid)
        namespace["pwd"] = SimpleNamespace(getpwnam=lambda user: account)
        namespace["grp"] = SimpleNamespace(getgrnam=lambda user: group)
        directory = namespace["_directory"]
        # Only translate the test home; identity/ownership and no-follow opens
        # still execute with this container's real unprivileged UID.
        namespace["_directory"] = lambda root, parts: directory(root, tuple(str(self.home).split("/")[1:]))
        namespace["_validate_identity"]("/home/agent1")
        for field, bad in (("pw_uid", 0), ("pw_uid", uid + 1), ("pw_gid", gid + 1),
                           ("pw_dir", "/home/agent2"), ("pw_name", "agent2")):
            with self.subTest(field=field, bad=bad), patch.object(account, field, bad):
                with self.assertRaises(ValueError):
                    namespace["_validate_identity"]("/home/agent1")
        with patch.object(group, "gr_gid", gid + 1), self.assertRaises(ValueError):
            namespace["_validate_identity"]("/home/agent1")

    def test_cli_rejects_root_or_mismatched_identity_without_path_leaks(self):
        for script in (store.CODEX_SUMMARY, store.CLAUDE_SUMMARY, store.OPENCODE_SUMMARY):
            for home in ("/root/PRIVATE-CANARY", "/home/agent1"):
                result = subprocess.run([sys.executable, "-I", "-S", "-c", script, home], capture_output=True, text=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(home, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
