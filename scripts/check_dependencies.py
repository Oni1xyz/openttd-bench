#!/usr/bin/env python3
"""Audit Python/baked-agent locks; audit the viewer separately with npm run audit:lock."""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

MAX_REPLY = 8 * 1024 * 1024

def get_json(url: str, payload=None, *, max_reply=MAX_REPLY):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "openttd-bench-lock-audit/1", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                raw = response.read(max_reply + 1)
            if len(raw) > max_reply:
                raise ValueError("registry response exceeds byte limit")
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("registry retry exhausted")

def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def check_age(value: str, cutoff: datetime):
    if instant(value) > cutoff:
        raise ValueError(f"release/artifact newer than cutoff: {value}")

def inventory(root: Path, python_only=True):
    if not python_only:
        raise ValueError("use viewer npm run audit:lock for bundled viewer dependencies")
    lock = tomllib.loads((root / "uv.lock").read_text())
    records = {}
    for p in lock["package"]:
        source = p.get("source", {})
        if "editable" in source:
            if p["name"] != "openttd-bench" or source["editable"] != ".":
                raise ValueError("unexpected editable dependency")
            continue
        if source != {"registry": "https://pypi.org/simple"}:
            raise ValueError(f"unapproved Python source for {p['name']}")
        key = ("PyPI", p["name"], p["version"])
        records[key] = {"key": key, "artifacts": [*p.get("wheels", []), *([p["sdist"]] if p.get("sdist") else [])]}
    npm_locks = [root / "image/agent/package-lock.json"]
    for file in npm_locks:
        if not file.exists():
            raise ValueError(f"missing npm lock: {file}")
        lock = json.loads(file.read_text())
        for location, p in lock["packages"].items():
            if not location:
                continue
            if p.get("link"):
                raise ValueError("unapproved linked npm dependency")
            name = p.get("name") or location.rsplit("node_modules/", 1)[-1]
            key = ("npm", name, p["version"])
            records.setdefault(key, {"key": key, "artifacts": []})["artifacts"].append(p)
    return sorted(records.values(), key=lambda p: p["key"])

def verify_record(record, cutoff, npm_meta=None):
    ecosystem, name, version = record["key"]
    quoted = urllib.parse.quote(name, safe="")
    if ecosystem == "PyPI":
        meta = get_json(f"https://pypi.org/pypi/{quoted}/{urllib.parse.quote(version, safe='')}/json")
        by_url = {a["url"]: a for a in meta["urls"]}
        if not record["artifacts"]:
            raise ValueError("Python lock has no hashed artifacts")
        for artifact in record["artifacts"]:
            known = by_url.get(artifact["url"])
            if known is None or artifact["hash"] != "sha256:" + known["digests"]["sha256"]:
                raise ValueError("Python artifact URL/hash disagrees with registry")
            if known.get("yanked"):
                raise ValueError("yanked Python artifact")
            check_age(known["upload_time_iso_8601"], cutoff)
    else:
        meta = npm_meta if npm_meta is not None else get_json(f"https://registry.npmjs.org/{quoted}", max_reply=64 * 1024 * 1024)
        release = meta["versions"][version]
        check_age(meta["time"][version], cutoff)
        for artifact in record["artifacts"]:
            if artifact.get("resolved") != release["dist"]["tarball"]:
                raise ValueError("npm resolved URL disagrees with registry")
            integrity = artifact.get("integrity", "")
            if not integrity.startswith("sha512-") or integrity != release["dist"].get("integrity"):
                raise ValueError("npm integrity missing or disagrees with registry")
            if len(base64.b64decode(integrity[7:], validate=True)) != 64:
                raise ValueError("invalid SHA-512 integrity")
    return {"ecosystem": ecosystem, "name": name, "version": version, "artifacts": len(record["artifacts"])}

def audit(root: Path, python_only=True, metadata=True):
    config = tomllib.loads((root / "pyproject.toml").read_text())
    cutoff = instant(config["tool"]["uv"]["exclude-newer"])
    now = datetime.now(timezone.utc)
    if (now - cutoff).total_seconds() < 7 * 86400:
        raise ValueError("review cutoff does not preserve minimum seven-day age")
    packages = inventory(root, python_only)
    errors = []
    checked = []
    if metadata:
        groups = {}
        for p in packages:
            groups.setdefault(p["key"][:2], []).append(p)
        def one(group):
            good, bad = [], []
            try:
                ecosystem, name, _ = group[0]["key"]
                # One full npm packument per name (e.g. all Codex platform aliases).
                meta = get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}", max_reply=64 * 1024 * 1024) if ecosystem == "npm" else None
                for p in group:
                    try:
                        good.append(verify_record(p, cutoff, meta))
                    except Exception as exc:
                        bad.append({"package": p["key"], "error": str(exc)})
            except Exception as exc:
                bad.extend({"package": p["key"], "error": str(exc)} for p in group)
            return good, bad
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for good, bad in pool.map(one, groups.values()):
                checked.extend(good)
                errors.extend(bad)
    matches = []
    queries = [{"package": {"ecosystem": p["key"][0], "name": p["key"][1]}, "version": p["key"][2]} for p in packages]
    query_count = 0
    for start in range(0, len(queries), 100):
        batch = queries[start:start + 100]
        response = get_json("https://api.osv.dev/v1/querybatch", {"queries": batch})
        results = response["results"]
        if len(results) != len(batch):
            raise ValueError("OSV returned incomplete coverage")
        for query, result in zip(batch, results):
            if result.get("next_page_token"):
                raise ValueError("OSV pagination requires explicit follow-up")
            if result.get("vulns"):
                matches.append({"query": query, "advisories": sorted({v["id"] for v in result["vulns"]})})
        query_count += len(batch)
    assert query_count == len(packages)
    files = [root / "uv.lock", root / "pyproject.toml", root / "image/agent/package-lock.json"]
    return {"scope": "python_and_baked_agent", "cutoff": cutoff.isoformat(), "checked_at": now.isoformat(), "package_versions": len(packages),
            "registry_packages_verified": len(checked), "registry_errors": errors,
            "osv_queries_verified": query_count, "affected_package_versions": len(matches), "matches": matches,
            "metadata_check_enabled": metadata,
            "source_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python-only", action="store_true", default=True, help="Compatibility flag; Python and baked agent is now the default scope")
    parser.add_argument("--advisories-only", action="store_true", help="Diagnostic only: does not attest registry age/integrity")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.root, args.python_only, not args.advisories_only)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    return 1 if result["registry_errors"] or result["matches"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
