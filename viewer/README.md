# OpenTTD bench trajectory viewer

An ATIF-only Next.js app using AI SDK messages and AI Elements. Convert native
Codex/Prime traces first; see the repository README's **ATIF trajectories**.
No personal API key, provider login, or cloud service is needed to view local runs.

## Local use

Use Node 24 in a reviewed, isolated environment. Install from the committed lock,
with lifecycle scripts disabled (also enforced by the project `.npmrc` default):

```sh
cd viewer
npm ci --ignore-scripts --include=dev
npm test
npm run build
OUTPUTS_DIR=/path/to/outputs npm start
# Or: OUTPUTS_DIR=/path/to/outputs npm run dev
# Default OUTPUTS_DIR is ../outputs; default URL is http://127.0.0.1:3000
# Optional: npm start -- --port 3100 --hostname ::1
```

`dev` and `start` use a strict launcher: only `--port`/`-p` and
`--hostname`/`-H` are accepted, once each. The bind address must be `127.0.0.1`,
`::1`, or `localhost` (normalized to `127.0.0.1`). Host environment variables
cannot override the explicit bind. Non-loopback and ambiguous extra flags fail.
Do not bypass this launcher with `next start -H 0.0.0.0`.

The read APIs require an exact loopback Host and, if present, a matching Origin.
Cross-site/same-site browser fetch metadata is rejected; forwarded headers do
not authorize a request. Responses are no-store, same-origin, and nosniff; listing
responses omit the absolute output root and errors do not echo paths or JSON.

**These header checks are not authentication.** A publicly rebound or proxied
service can receive forged Host/Origin headers from non-browser clients. Do not
publish the viewer with a tunnel, port-forward, `tailscale serve`, or a public
reverse proxy. Remote use needs a separately reviewed authenticated access layer
and network isolation, not an added hostname allowlist. Local users/processes can
read the viewer. Prefer curated immutable copies of runs, not live agent-owned
output directories. Path checks and descriptor checks are not an OS sandbox
against a concurrently hostile directory owner or hostile filesystem.

## Display and limits

- Discovers `outputs/<run>/trajectories/index.json`; ignores hidden/archived runs,
  raw traces, and descendant symlinks. An operator-selected root symlink is allowed.
- Only manifest-named SHA-shaped `.atif.json` files are read. Duplicate filenames
  count once (first entry wins). Both the index and trajectories must be regular
  files: lstat, `O_NOFOLLOW | O_NONBLOCK`, descriptor identity/type/size checks,
  bounded reads, and directory/leaf rechecks reject FIFOs, links and detected swaps.
- Drop a standalone `.atif.json` or JSON array of trajectories. Its size is checked
  **before** `File.text()`, then UTF-8 bytes and iterative JSON budgets are checked
  before schema validation, recursive subagent handling, or presentation.
- Episodes, seats, subagents, outcomes, game state, errors, reasoning, tool inputs,
  all observations, and usage remain visible. Copied context is optional. Messages
  are converted and rendered 100 at a time; off-screen message bodies are not
  precomputed before a UI slice. Matching observations uses a per-step lookup.
- Only archival `extra.source`/`extra.prime_source` copies are omitted from API
  presentation. **This is not secret redaction**: prompts, observations, metadata
  and paths inside trace content can still be sensitive. On-disk records are unchanged.

Limits are shared in `lib/limits.ts` and are not disabled by environment variables:

| Boundary | Limit |
| --- | --- |
| Index file | 256 KiB |
| Individual server trajectory file | 8 MiB |
| Aggregate run input (including index), response, or dropped file | 16 MiB each |
| Manifest entries, and aggregate trajectories including subagents | 256 each |
| Aggregate steps / JSON nodes / JSON depth | 10,000 / 100,000 / 48 |
| Tool calls / observations per step | 128 / 256 |
| Discovery | 1,024 directory entries, 256 runs, 4 MiB index reads |

Over-budget runs are rejected, not partially presented. Discovery returns a
bounded filesystem-order subset sorted by modification time; it is **not** a
complete catalog of a root exceeding its budgets. Invalid manifests are skipped.

## Dependency review and unresolved gate

The lock pins all direct versions and uses scoped overrides for sharp, DOMPurify,
source-map-js, undici, and Lightning CSS. The unused `shadcn` scaffolding CLI and
seven unused direct packages were removed; checked-in components remain. The
few needed Base UI CSS state variants are local instead of importing CLI CSS.
Zod 4.6.7 satisfies AI SDK's peer range without the old Zod 3 advisory.

Resolution used npm 11.19.0 with:

```sh
npm install --package-lock-only --ignore-scripts --include=dev \
  --before=2026-10-22T00:00:00Z --no-audit --no-fund
npm run audit:lock
npm audit --json --include=dev --ignore-scripts
```

`audit:lock` independently verifies every exact version's official npm release
time against the fixed **2026-10-22T00:00:00Z** cutoff, checks SRI syntax and registry
artifact metadata, verifies bundled package manifests inside their hash-checked
parent tarball, and queries OSV and npm's bulk advisory endpoint for the full lock
(including development and optional/other-platform packages). It fails on missing
coverage, malformed integrity, young versions, or **any** advisory match. npm's
`--before` aids resolution; it is not the independent age gate. No unrecognized
`min-release-age` npm option is used. Future updates need a newly reviewed cutoff.
Registry agreement is not an independent provenance/signature verification.

Review result (2026-10-29): **441 locations, 439 unique name/version pairs, 421
names; 439/439 registry age checks and OSV/npm queries; six bundled manifests
verified.** Next 16.4.1, sharp 0.35.5, DOMPurify 3.4.17, source-map-js 1.2.2,
undici 7.30.1 and Zod 4.6.7 no longer match the audited advisories. Lightning CSS
1.33.0 is a targeted compatible 1.x override: the 1.32.0 Linux x64 GNU package's
registry/lock SRI was malformed and npm omitted its required native binary. Its
integrity was **not repaired or bypassed**; the replacement passes installation,
age/SRI checks, and the production build.

**Unresolved — not waived:** `katex@0.16.47`,
[GHSA-238p-pmpm-9mq7](https://github.com/advisories/GHSA-238p-pmpm-9mq7), low severity.
OSV/npm identify one underlying advisory; `npm audit` reports seven low entries
including affected dependents, zero moderate/high/critical. The fix is 0.18.2,
outside the `^0.16` ranges required by the current math/diagram renderers, including
age-eligible releases of their plugins. No supported compatible renderer upgrade
was found under the cutoff. Math/diagram display is retained rather than forcing
an out-of-range KaTeX or silently removing features. The advisory requires existing
prototype pollution/attacker-controlled option prototypes; exploitability here was
not established. **Both advisory commands exit 1.** An advisory-clean release
remains blocked until a compatible upstream fix or an explicitly approved renderer
replacement/removal is reviewed. Do not interpret the build/test pass as clearance.

## Isolated verification

All dependency resolution, installation, tests, and builds for this change ran
inside disposable Docker containers using the existing trusted image
`sha256:d6865dac67b5730d1a331eaab672a6b12ce691dfaf3f855c01e4b5e387308bd9`
(Node 24.21.0, npm 11.19.0), with an explicit `/bin/sh` entrypoint. No credentials,
host home, Docker socket, host networking, or host-writable source was mounted.

Reproduce with a disposable writable volume **containing a copy** of `viewer/`:

- Mount source read-only at `/src`; copy it to `/work/job`. Keep node_modules,
  caches, generated Next files, and fixtures only inside the disposable volume.
- Run resolution, `audit:lock`, and `npm ci --ignore-scripts --include=dev` in a
  networked container. Explicit `--include=dev` matters if the base image sets
  `NODE_ENV=production`. Never export node_modules or generated artifacts.
- Run `npm test && npm run build && npm run test:runtime` in a fresh
  `--network none` container using that volume. No host port publication is needed.
- Use `--read-only --cap-drop ALL --security-opt no-new-privileges --cpus 2
  --memory 4g --pids-limit 256 --ulimit core=0`, a bounded `/tmp` tmpfs, and
  `NEXT_TELEMETRY_DISABLED=1`. Use an empty HOME and container-local npm cache.
- The Linux production canary creates only synthetic runs, checks the actual
  loopback listening socket, exercises successful local reads, denies hostile
  Host/Origin requests, and checks sanitized missing-root/path errors. It does not
  test game execution, real traces, a public deployment, or authentication.
- Delete the disposable containers/volume afterward. Only reviewed manifests/lock
  files may be exported from dependency resolution into the source worktree.
