#!/usr/bin/env bash
# Build inputs are fetched only by Docker builds. The actual game smoke is offline.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
if [[ "${1:-}" == "--build" ]]; then
  docker build -f image/Dockerfile.tests -t openttd-bench-tests:hardening .
  docker build -f image/Dockerfile -t openttd-bench:hardening .
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--build]\n' "$0" >&2; exit 2
fi
docker image inspect openttd-bench-tests:hardening openttd-bench:hardening >/dev/null
docker build --network none -f image/Dockerfile.smoke -t openttd-bench-smoke:hardening .
NAME="openttd-smoke-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
# No host home, credentials, published ports, or Docker socket. Only source is bound RO.
# Writable disposable root is needed by init_game's user/client/world setup.
timeout --signal=TERM --kill-after=15s 600s docker run --rm --name "$NAME" \
  --network none --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN \
  --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL \
  --security-opt no-new-privileges --pids-limit 256 --memory 2g --cpus 2 --ulimit core=0 \
  --tmpfs /work:rw,nosuid,mode=1777,size=512m \
  -v "$ROOT:/source:ro" -w /source \
  -e PYTHONPATH=/source:/source/harnesses:/source/image -e TMPDIR=/work \
  -e OPENTTD_ISOLATED_SMOKE=1 \
  openttd-bench-smoke:hardening python -u tests/smoke_game.py
