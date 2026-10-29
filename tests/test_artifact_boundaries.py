"""Synthetic filesystem regressions; run only inside the restricted test container."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openttd_bench import atif


def native(secret="ordinary task", tid="native"):
    return [{"type": "session_meta", "payload": {"id": tid}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": secret}}]


def dump(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


class ArtifactBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/work")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.run_dir = self.base / "run"
        self.run_dir.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def trace(self, harvest=None, seat="agent1", codex=True):
        trace = {"id": "seat-trace", "nodes": [], "info": {"seat_user": seat}}
        if codex:
            trace["agent"] = {"config": {"harness": {"id": "codex-cli"}}}
        if harvest is not None:
            trace["info"]["harness_session"] = {"harvested_to": harvest}
        dump(self.run_dir / "eval" / "traces.jsonl", [trace])
        return trace

    def test_external_harvest_never_discloses_canary(self):
        dump(self.outside / "private.jsonl", native("PRIVATE-EXTERNAL-CANARY"))
        self.trace(str(self.outside))
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)
        self.assertFalse((self.run_dir / "trajectories").exists())

    def test_seat_traversal_and_missing_seat_never_select_other_history(self):
        dump(self.outside / "private.jsonl", native("PRIVATE-EXTERNAL-CANARY"))
        dump(self.run_dir / "raw/codex/agent2/session.jsonl", native())
        for seat in ("../../../outside", str(self.outside), "", None, "agent1/../agent2"):
            with self.subTest(seat=seat):
                self.trace(seat=seat)
                with self.assertRaises(ValueError):
                    atif.convert_run(self.run_dir)

    def test_relative_harvest_traversal_is_rejected_even_if_target_exists(self):
        dump(self.outside / "private.jsonl", native())
        self.trace("raw/codex/../../../outside")
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)

    def test_output_directory_symlink_preserves_external_manifest_and_canary(self):
        self.trace(codex=False)
        (self.outside / "index.json").write_text("old manifest")
        (self.outside / "canary").write_text("untouched")
        (self.run_dir / "trajectories").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)
        self.assertEqual((self.outside / "index.json").read_text(), "old manifest")
        self.assertEqual((self.outside / "canary").read_text(), "untouched")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["canary", "index.json"])

    def test_output_leaf_symlink_preserves_manifest(self):
        self.trace(codex=False)
        manifest = atif.convert_run(self.run_dir)
        dest = self.run_dir / "trajectories"
        old_index = (dest / "index.json").read_bytes()
        canary = self.outside / "canary"
        canary.write_text("untouched")
        output = dest / manifest["trajectories"][0]["file"]
        output.unlink()
        output.symlink_to(canary)
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)
        self.assertEqual((dest / "index.json").read_bytes(), old_index)
        self.assertEqual(canary.read_text(), "untouched")
        self.assertTrue(output.is_symlink())

    def test_manifest_symlink_rejected_before_any_publication(self):
        self.trace(codex=False)
        dest = self.run_dir / "trajectories"
        dest.mkdir()
        canary = self.outside / "index.json"
        canary.write_text("old manifest")
        (dest / "index.json").symlink_to(canary)
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)
        self.assertEqual(canary.read_text(), "old manifest")
        self.assertEqual([p.name for p in dest.iterdir()], ["index.json"])

    def test_descendant_source_and_harvest_symlinks_are_rejected(self):
        dump(self.outside / "session.jsonl", native("PRIVATE-EXTERNAL-CANARY"))
        raw = self.run_dir / "raw/codex"
        raw.mkdir(parents=True)
        (raw / "agent1").symlink_to(self.outside, target_is_directory=True)
        self.trace("raw/codex/agent1")
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)
        with self.assertRaises(ValueError):
            atif.codex_forest(raw)
        (raw / "agent1").unlink()
        (raw / "session.jsonl").symlink_to(self.outside / "session.jsonl")
        with self.assertRaises(ValueError):
            atif.codex_forest(raw)

    def test_fifo_fails_without_blocking(self):
        os.mkfifo(self.run_dir / "traces.jsonl")
        # A timeout makes the pre-fix blocking open a bounded failing test.
        # Bypass the heavy benchmark package initializer, not the actual stdlib
        # converter. The timeout must measure the read, not dependency imports.
        code = "import sys,types; from pathlib import Path; p=types.ModuleType('openttd_bench'); p.__path__=[sys.argv[2]]; sys.modules['openttd_bench']=p; from openttd_bench.atif import convert_run;\ntry: convert_run(Path(sys.argv[1]))\nexcept ValueError: sys.exit(0)\nsys.exit(3)"
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(self.run_dir), str(Path(atif.__file__).parent)], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_deep_input_is_rejected_as_a_boundary_error(self):
        (self.run_dir / "traces.jsonl").write_text('{"id":"deep","extra":' + "[" * 1500 + "0" + "]" * 1500 + "}\n")
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir)

    def test_file_total_record_and_node_budgets(self):
        from openttd_bench.safe_files import Limits
        self.trace(codex=False)
        for limits in (Limits(file_bytes=10), Limits(total_bytes=10), Limits(records=0), Limits(nodes=5)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                atif.convert_run(self.run_dir, limits=limits)
        self.assertFalse((self.run_dir / "trajectories").exists())

    def test_oversized_sparse_file_is_not_read(self):
        from openttd_bench.safe_files import Limits
        with (self.run_dir / "traces.jsonl").open("wb") as stream:
            stream.truncate(32 * 1024 * 1024)
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir, limits=Limits(file_bytes=1024))

    def test_aggregate_limits_fail_before_either_converter(self):
        from openttd_bench.safe_files import Limits
        dump(self.run_dir / "a/traces.jsonl", [{"id": "a", "nodes": []}])
        dump(self.run_dir / "b/traces.jsonl", [{"id": "b", "nodes": []}])
        one_size = (self.run_dir / "a/traces.jsonl").stat().st_size
        for limits in (Limits(total_bytes=one_size + 1), Limits(records=1), Limits(nodes=7),
                       Limits(entries=1), Limits(files=1), Limits(depth=0)):
            with self.subTest(limits=limits), patch.object(atif, "prime_trajectory") as prime:
                with self.assertRaises(ValueError):
                    atif.convert_run(self.run_dir, limits=limits)
                prime.assert_not_called()
        self.assertFalse((self.run_dir / "trajectories").exists())

    def test_safe_open_detects_preopen_fifo_symlink_and_identity_swaps(self):
        # Exercise the open/fstat guard itself, after the lstat has seen a regular
        # file. Each attack runs in a killed-on-timeout stdlib-only subprocess.
        code = '''import sys, types, os
from pathlib import Path
p = types.ModuleType("openttd_bench")
p.__path__ = [sys.argv[2]]
sys.modules["openttd_bench"] = p
from openttd_bench.safe_files import SafeRoot, Budget, BoundaryError
root_path, attack = Path(sys.argv[1]), sys.argv[3]
real_open = os.open
with SafeRoot(root_path) as root:
    def swapped(name, flags, *args, **kwargs):
        if name == "traces.jsonl":
            parent = kwargs["dir_fd"]
            os.unlink(name, dir_fd=parent)
            if attack == "fifo":
                os.mkfifo(name, dir_fd=parent)
            elif attack == "symlink":
                os.symlink("../outside/canary", name, dir_fd=parent)
            else:
                (root_path / name).write_text("replacement")
        return real_open(name, flags, *args, **kwargs)
    os.open = swapped
    try:
        root.read("traces.jsonl", Budget())
    except BoundaryError:
        sys.exit(0)
sys.exit(3)
'''
        canary = self.outside / "canary"
        canary.write_text("PRIVATE-CANARY")
        for attack in ("fifo", "symlink", "regular"):
            path = self.run_dir / "traces.jsonl"
            path.unlink(missing_ok=True)
            path.write_text("original")
            result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(self.run_dir),
                                     str(Path(atif.__file__).parent), attack], capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(canary.read_text(), "PRIVATE-CANARY")

    def test_existing_external_historical_path_only_uses_relocated_local_data(self):
        external = self.outside / "old-run/raw/codex/agent1-123"
        dump(external / "private.jsonl", native("PRIVATE-EXTERNAL-CANARY", "external"))
        dump(self.run_dir / "raw/codex/agent1-123/session.jsonl", native("local task", "local"))
        self.trace(str(external))
        manifest = atif.convert_run(self.run_dir)
        output = (self.run_dir / "trajectories" / manifest["trajectories"][0]["file"]).read_text()
        self.assertNotIn("PRIVATE-EXTERNAL-CANARY", output)
        self.assertEqual(json.loads(output)["trajectory_id"], "local")

    def test_output_budget_failure_preserves_previous_manifest(self):
        from openttd_bench.safe_files import Limits
        self.trace(codex=False)
        atif.convert_run(self.run_dir)
        manifest = self.run_dir / "trajectories/index.json"
        original = manifest.read_bytes()
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir, limits=Limits(output_bytes=10))
        self.assertEqual(manifest.read_bytes(), original)

    def test_native_roundtrip_and_run_local_relocation(self):
        records = native()
        directory = self.run_dir / "raw/codex/agent1-123"
        dump(directory / "sessions/one.jsonl", records)
        for harvest in ("raw/codex/agent1-123", str(directory), "/old/outputs/run/raw/codex/agent1-123"):
            with self.subTest(harvest=harvest):
                self.trace(harvest)
                manifest = atif.convert_run(self.run_dir)
                doc = json.loads((self.run_dir / "trajectories" / manifest["trajectories"][0]["file"]).read_text())
                self.assertEqual(doc["extra"]["source"]["records"], records)
                self.assertEqual(doc["trajectory_id"], "native")
                self.assertTrue(doc["extra"]["source"]["path"].startswith(str(directory)))

    def test_explicit_operator_root_symlink_is_allowed_but_not_descendants(self):
        dump(self.outside / "one.jsonl", native())
        approved = self.base / "approved"
        approved.symlink_to(self.outside, target_is_directory=True)
        manifest = atif.convert_run(self.run_dir, [approved])
        self.assertEqual(len(manifest["trajectories"]), 1)
        (self.outside / "linked.jsonl").symlink_to(self.outside / "one.jsonl")
        with self.assertRaises(ValueError):
            atif.convert_run(self.run_dir, [approved])

    def test_run_root_symlink_is_explicitly_supported(self):
        self.trace(codex=False)
        alias = self.base / "selected-run"
        alias.symlink_to(self.run_dir, target_is_directory=True)
        self.assertEqual(atif.convert_run(alias), atif.convert_run(self.run_dir))


if __name__ == "__main__":
    unittest.main()
