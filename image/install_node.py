#!/usr/bin/env python3
"""Image-build-only installation of a pinned, hash-verified Node toolchain."""
import json
from pathlib import Path
import tarfile

from fetch_content import fetch

ROOT = Path(__file__).resolve().parent
spec = json.loads((ROOT / "toolchain.json").read_text())["node"]
archive = fetch(ROOT / "cache", {"node": spec})["node"]
target = Path("/var/tmp/vf-node")
target.mkdir(parents=True, exist_ok=True)
prefix = f"node-{spec['version']}-linux-x64/"
with tarfile.open(archive, "r:xz") as tar:
    for member in tar:
        if member.name == prefix.rstrip("/") and member.isdir():
            continue
        if not member.name.startswith(prefix):
            raise RuntimeError("unexpected Node archive root")
        member.name = member.name[len(prefix):]
        if member.name:
            tar.extract(member, target, filter="data")
if not (target / "bin/node").is_file():
    raise RuntimeError("Node archive has no executable")
