#!/usr/bin/env bash
# Build a lock-derived test environment; never install project packages on the host.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
MODE=${1:-python}
if [[ $# -gt 0 ]]; then shift; fi
case "$MODE" in
  python|audit-python) ;;
  *) printf 'Usage: %s {python|audit-python} [test/audit arguments]\n' "$0" >&2; exit 2 ;;
esac
docker build -q -f image/Dockerfile.tests -t openttd-bench-tests:hardening . >&2
NAME="openttd-tests-${MODE}-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
NETWORK=none
COMMAND=(python -m pytest -p no:cacheprovider -q -ra)
if [[ "$MODE" == audit-python ]]; then
  NETWORK=bridge
  # The networked metadata audit imports stdlib only, without site packages/hooks.
  COMMAND=(python -I -S /source/scripts/check_dependencies.py)
fi
timeout --signal=TERM --kill-after=10s 360s docker run --rm --name "$NAME" \
  --network "$NETWORK" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 1g --cpus 2 --ulimit core=0 \
  --tmpfs /work:rw,nosuid,mode=1777,size=512m \
  -v "$ROOT:/source:ro" -w /source \
  -e PYTHONPATH=/source:/source/harnesses:/source/image \
  openttd-bench-tests:hardening "${COMMAND[@]}" "$@"
