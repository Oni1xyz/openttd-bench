# Viewer verification receipt

Branch: `fix/viewer-hardening`, based on `304f008`. Verification used synthetic
fixtures only; no game execution, private traces, API credentials, public port,
or cloud provisioning. All project/dependency execution occurred in disposable
containers, not on the host.

## Results

- Node 24.21.0, npm 11.19.0; image ID below.
- Initial regression run: 19 tests, 3 passed / 16 failed. Confirmed an index FIFO
  hang in a timeout-killed child. A later ancestor-swap regression also failed
  before its fix. Existing ATIF/path tests were retained.
- Final `npm test`: **28 passed, 0 failed**, including isolated FIFO/leaf-swap
  tests with a 192 MiB child heap, 4-second timeout and SIGKILL fallback.
- Final `npm run build`: Next **16.4.1**, compiled successfully; TypeScript passed;
  **4/4** static pages generated. Routes: static `/` and `/_not-found`, dynamic
  `/api/runs` and `/api/traces`.
- Final `npm run test:runtime`: passed. Checked the actual loopback socket,
  successful local listing/trace read, **12** denied hostile requests, sanitized
  missing-path/root responses, and the served page.
- Final full-lock gate: **441 locations / 439 exact pairs / 421 names**;
  **439/439** registry release-age checks and OSV/npm queries, **6** bundled
  manifests checked. Fixed cutoff: `2026-10-22T00:00:00Z`.
- **Advisory gate remains exit 1**, not a waived/pass result: KaTeX 0.16.47,
  GHSA-238p-pmpm-9mq7 (low). `npm audit` reports seven low entries because it
  includes affected dependents; zero moderate/high/critical. See README for
  the incompatible upstream fix and release blocker.
- A malformed Lightning CSS 1.32.0 Linux x64 GNU SRI caused npm to omit the native
  optional dependency and broke the build. The token was not repaired. A scoped
  Lightning CSS 1.33.0 override passed the full age/artifact checks and real build.
- `git diff --check` passed. No node_modules, `.next`, downloaded archives, raw
  advisory responses, or trace artifacts are committed.

## Exact container workflow

These variables abbreviate the actual image, source, and disposable volume used.
The image is local/trusted; do not replace it with a mutable or unreviewed tag.

```bash
IMAGE=sha256:d6865dac67b5730d1a331eaab672a6b12ce691dfaf3f855c01e4b5e387308bd9
SRC=/home/lynx/projects/openttd-bench-worktrees/viewer/viewer
VOL=openttd-viewer-hardening-work

docker volume create "$VOL"
# The sole root step changes ownership only within the new disposable volume.
docker run --rm --network none --read-only --cap-drop ALL --cap-add CHOWN \
  --security-opt no-new-privileges --cpus 1 --memory 128m --pids-limit 32 \
  --user 0:0 --mount type=volume,src="$VOL",dst=/work,volume-nocopy \
  --entrypoint /bin/sh "$IMAGE" \
  -c 'mkdir -p /work/job; chown 1000:1000 /work/job'

# Install only into a copy; explicit include=dev overrides production image defaults.
docker run --rm --network bridge --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 3g --pids-limit 256 \
  --ulimit core=0 --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  --mount type=bind,src="$SRC",dst=/src,readonly \
  --mount type=volume,src="$VOL",dst=/work,volume-nocopy \
  --env HOME=/tmp/home --env npm_config_cache=/tmp/npm-cache \
  --env NEXT_TELEMETRY_DISABLED=1 --env NODE_ENV=development \
  --workdir /work/job --entrypoint /bin/sh "$IMAGE" \
  -c 'set -eu; mkdir -p /tmp/home; cp -R /src/. .; npm ci --ignore-scripts --include=dev --no-fund --no-audit'

# Real tests, production build, and HTTP canary without external networking.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 4g --pids-limit 256 \
  --ulimit core=0 --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --mount type=bind,src="$SRC",dst=/src,readonly \
  --mount type=volume,src="$VOL",dst=/work,volume-nocopy \
  --env HOME=/tmp/home --env NEXT_TELEMETRY_DISABLED=1 \
  --env NODE_OPTIONS=--max-old-space-size=2560 \
  --workdir /work/job --entrypoint /bin/sh "$IMAGE" \
  -c 'set -eu; mkdir -p /tmp/home; cp -R /src/. .; npm test; npm run build; npm run test:runtime'

# Independent full-lock gate needs only Node builtins, not installed dependencies.
# EXPECTED NONZERO while the documented KaTeX advisory remains unresolved.
docker run --rm --network bridge --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 2g --pids-limit 128 \
  --ulimit core=0 --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  --mount type=bind,src="$SRC",dst=/src,readonly \
  --workdir /src --entrypoint /bin/sh "$IMAGE" \
  -c 'node scripts/verify-dependencies.mjs'

# Remove only the disposable volume created for this verification.
docker volume rm "$VOL"
```

Lock resolution used the same isolated writable copy, before installing packages:
`npm install --package-lock-only --ignore-scripts --include=dev --before=2026-10-22T00:00:00Z --no-audit --no-fund`.
Only the reviewed lock output was exported; package code never had a writable
mount of the Git worktree. The advisory gate is intentionally separate from the
build canary: exercising the isolated artifact is not approval to deploy it.
