#!/usr/bin/env python3
"""Watch one model series for new recorded 429s; notify ntfy and exit to wake Pi.

Re-arm after each alert. A durable cursor/dedup journal prevents repeat alerts;
all four planned runs are watched, including logs created by later stages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from .watch_run import post
except ImportError:  # direct script invocation
    from watch_run import post

RATE_LIMIT = re.compile(
    r"\b(?:upstream|HTTP(?:/\d(?:\.\d)?)?|status(?:_code)?[\"']?\s*[:=]?)\s*429\b"
    r"|\b429\s+Too Many Requests\b|\brate_limit_exceeded\b|\brate_limited\b",
    re.IGNORECASE,
)
REQUEST_ID = re.compile(r'(?:inference_id|request_id)["\s:]+([A-Za-z0-9_-]+)|request id:\s*([A-Za-z0-9_-]+)', re.I)
TERMINAL = {"complete", "complete_with_issues", "complete_with_caveat", "cancelled", "cancelled_before_launch", "failed"}


def scan_file(path: Path, state: dict, label: str) -> list[dict]:
    key = str(path)
    offset = state["offsets"].get(key, 0)
    if path.stat().st_size < offset:
        offset = 0
    events = []
    with path.open("rb") as file:
        file.seek(offset)
        while True:
            start = file.tell()
            raw = file.readline()
            if not raw or not raw.endswith(b"\n"):
                break  # Do not consume a partially written record.
            state["offsets"][key] = file.tell()
            text = raw.decode(errors="replace")
            if not RATE_LIMIT.search(text):
                continue
            ids = [a or b for a, b in REQUEST_ID.findall(text)]
            fingerprint = label + ":" + (ids[0] if ids else hashlib.sha256((key + str(start) + text).encode()).hexdigest())
            if fingerprint in state["seen"]:
                continue
            state["seen"].append(fingerprint)
            events.append({"event": "rate_limit_429", "detected_unix": time.time(), "label": label,
                           "file": key, "byte_offset": start, "request_ids": ids,
                           "fingerprint": fingerprint})
    return events


def persist(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("series", type=Path)
    parser.add_argument("--topic", default=os.environ.get("OPENTTD_NOTIFY_TOPIC", ""))
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    cursor = args.series.with_name(args.series.stem + "-429-watch.json")
    journal = args.series.with_name(args.series.stem + "-429-alerts.jsonl")
    state = json.loads(cursor.read_text()) if cursor.exists() else {"offsets": {}, "seen": []}
    print("429 watcher armed for all series stages; scan interval 5s; durable dedup enabled.", flush=True)
    while True:
        plan = json.loads(args.series.read_text())
        events = []
        for item in plan["runs"].values():
            root = Path(item["out"])
            files = [root / "eval.log", root / "preflight.log", root / "codex-preflight.json"]
            files += list((root / "raw/codex").glob("*/codex-exec.jsonl"))
            files += list((root / "live/codex").glob("agent*-errors.jsonl"))
            for path in files:
                if path.exists():
                    events += scan_file(path, state, item["label"])
        persist(cursor, state)
        if events:
            with journal.open("a") as file:
                for event in events:
                    file.write(json.dumps(event) + "\n")
                    print(json.dumps(event), flush=True)
            body = "\n".join(f"{e['label']}: HTTP 429; request IDs {', '.join(e['request_ids']) or 'not recorded'}. Source: {e['file']}" for e in events)
            post(args.topic, "OpenTTD 429 alert - agent check requested", body)
            print(json.dumps({"event": "attention_required", "count": len(events), "action": "Inspect recovery/retirement, then re-arm this watcher using the same series path."}), flush=True)
            return
        if plan["status"] in TERMINAL:
            print(json.dumps({"event": "series_finished", "status": plan["status"], "new_429s": 0}), flush=True)
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
