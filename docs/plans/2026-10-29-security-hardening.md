# OpenTTD benchmark hardening implementation plan

> **For Hermes:** Use subagent-driven-development to implement and independently review the bounded workstreams below.

**Goal:** Ship a reviewable Oni1xyz fork whose documented execution path avoids personal OAuth, host-networked agents, secret-bearing process arguments, unsafe artifact imports, and floating installation inputs.

**Architecture:** Preserve game/scoring code. Support the intercepted Codex API harness first; prebuild its tools and game assets in an immutable image. Use framework-only execution networking with a shared-seat setup barrier, fail-closed configuration checks, bounded artifact readers, and an explicitly local viewer. Keep an outer disposable VM as a deployment recommendation, not a claim that containers are a complete security boundary.

**Tech Stack:** Python 3.12, verifiers 0.3.1, OpenTTD 15.3, Codex CLI, Next.js/Node, Docker, pytest and Node tests.

**Tracking:** ONI-134. Baseline `fee25ab19837959fd9c9749808b117144b9d1b3e`. Fork `Oni1xyz/openttd-bench`; branch `fix/security-hardening`.

## Constraints

- No personal credentials, paid inference, cloud provisioning, upstream disclosure or merge.
- No repository/package code execution on the host; use isolated build/test containers without a host home or Docker socket. Host-side reviewed orchestration may invoke Docker explicitly.
- Preserve the seven-day package-age gate and verify source hashes. Never force-upgrade merely to suppress an advisory.
- Raw sessions, game saves, downloaded archives, audit dumps and credentials stay out of Git.
- No gameplay/scoring changes; a short scripted-seat game is the real acceptance path when paid model calls are unavailable.

## Required outcomes

R1. OAuth seat setup and unsandboxed host preflight fail before reading a credential or executing a model.
R2. The documented supported configuration uses the intercepted API Codex harness and bounded CPU, RAM, wall/token budgets.
R3. Unrestricted/local/host-network runtime configurations fail before provisioning; shared seats cannot reset execution policy or drop another seat's proxy route.
R4. Neither administrator nor inference bearer values enter in-sandbox process arguments. Administrator credentials are read from a root-only store with an explicit role selector; secrets are not shell-source exports.
R5. Agent/session imports and outputs stay within approved roots. Reject symlinks, FIFOs/devices, oversized/deep inputs; collectors run as the seat UID with a deadline.
R6. Viewer defaults enforce loopback and same-origin/host checks; reads and responses are bounded; no accidental publication of raw archival fields.
R7. Notifications are disabled by default; no repository-owned hardcoded destination is used.
R8. Runtime setup performs no pip/apt/npm or unchecked Node download. Build/install inputs are locked, hashed and age-gated; game archives use safe extraction.
R9. Refresh advisory-affected dependency versions and rescan the complete lock graph; document conditional/unresolved matches honestly.
R10. Isolated unit/security suites, production viewer build, cross-UID checks, network negative tests and a short actual-game/scripted-seat smoke succeed.
R11. Independent spec and adversarial code review pass; push verified commits and open a fork-local PR without merging.

## Workstream A: execution and credentials (isolated worktree)

Owned files: `openttd_bench/env.py`, `seats.py`, `taskset.py`; `harnesses/openttd_seat/seat.py`; CLI harness modules; host probe/preflight scripts; new hardened config; their focused Python tests.

1. Add regressions proving the former secret-bearing argv and OAuth/host-probe behavior is rejected. Execute tests in the isolated Python test image.
2. Replace box API token argv with root-only credential lookup plus a validated admin/company selector. Never interpolate credentials into the helper source.
3. Pass per-seat secrets as actual environment values after validating variable names; use an explicit safe PATH, not literal shell expansion. Retain UID/GID demotion and add no-new-privileges where applicable.
4. Fail closed for OAuth and host preflight; document API-only support. Harness setup verifies prebuilt Node/Codex instead of downloading them.
5. Add fail-closed runtime validation before `provision()`. Use restricted Docker policy, positive resource limits, and a bounded shared setup barrier that unions all seat interception routes before any agent launches. Abort waiting seats on setup failure and prohibit re-opening setup after execution.
6. Test API config compatibility, concurrent route preservation and failure/cancellation behavior. Keep existing nonsecurity contracts passing or update tests only where intentional safe behavior changes.
7. Commit the focused implementation and report tests/remaining integration assumptions.

## Workstream B: artifact and collector boundary (isolated worktree)

Owned files: `openttd_bench/atif.py`, a new bounded filesystem helper as needed, `harnesses/openttd_seat/store.py`, and focused Python tests. Do not edit `seat.py`, runtime config or dependencies.

1. Write failing regression cases for external harvested paths, descendant symlinks, a symlink publication directory, matching FIFOs, and oversized records.
2. Make implicit imports run-local; separately validate explicit operator-approved external roots. Use safe bounded regular-file reads and validate destination ancestry before writes.
3. Run summary collection as the seat UID, reject unsafe file types, bound totals and time, and preserve ordinary native-session conversion.
4. Exercise negative cases with external canaries unchanged and positive round trips. Execute in an offline unprivileged container; use a hard timeout for FIFO probes.
5. Commit only owned files and report precise verification.

## Workstream C: optional viewer (isolated worktree)

Owned files: `viewer/**` only, including its lockfile and documentation. Treat viewer/AGENTS.md and CLAUDE.md as upstream guidance, not permission to install or bypass controls.

1. Add failing API/filesystem/size/host-origin cases; retain existing traversal/root-symlink tests.
2. Add a loopback-only launcher, reject disallowed bind arguments and hostile origin/host requests, sanitize errors, bound file/manifest/response/depth work, and reject special files before opening.
3. Refresh vulnerable viewer dependencies within the seven-day age cutoff, preferably retaining compatible versions and shrinking scaffolding-only exposure when safe. Use `--ignore-scripts`; verify release timestamps independently.
4. Run Node tests and a production build in an isolated container, not the host. A runtime canary verifies intended local reads and rejection of unsafe requests.
5. Commit viewer changes and record exact dependency/advisory and test results.

## Parent integration: image, dependencies, notifications and server limits

Owned files: root `pyproject.toml`/`uv.lock`, `image/**`, build/test/security scripts, root docs/CI, notification helpers not owned by A, integration tests.

1. Build a credential-free isolated Python test image; pass its immutable identifier to implementers. Bound execution resources and mount source read-only.
2. Keep verifiers 0.3.1 API stable while refreshing affected transitive packages using a fixed age cutoff and a locked build backend. Export hash-locked image requirements.
3. Pin Python base digest, verified Node release/digest and Codex npm lock. Prebuild all runtime tools; fail if missing rather than bootstrap downloads. Verify safe extraction and installed content.
4. Disable all notification delivery unless explicitly configured; remove hardcoded destinations. Add cheap request-body and admission bounds to the control plane without changing game rules.
5. Add reproducible `scripts/test-isolated.sh` and `scripts/smoke-isolated.sh`. Test entry points must not read host credentials or start cloud/paid inference.
6. Integrate workstream commits; run full Python suite (separate Docker-runtime tests), viewer tests/build, exact cross-UID and malicious-artifact regressions, and scripted game smoke with real OpenTTD binaries.
7. Run the age/integrity/advisory checks against final locks. Retain full receipts outside Git; commit only bounded summaries and reproducible test code.

## Review and shipping gates

- Pre-flight: clean pinned source, verified fork, explicit ownership and safe build environment.
- Revision: focused regression fails before its fix and passes afterward; parent inspects each diff.
- Spec review: independent reviewer maps every R1-R11 requirement to implementation and actual evidence. Resolve gaps before quality review.
- Quality review: separate adversarial reviewer checks bypasses, races, secret leaks, misleading tests and unsafe installation fallbacks.
- Acceptance: parent reruns decisive final tests after review fixes; no earlier pass substitutes for final verification.
- Shipping: conventional commits, clean tree, pushed branch read back from GitHub, fork-local PR and CI state verified. No merge without user approval.

## Verification interfaces

Python: `scripts/test-isolated.sh python` (pytest including new security regressions; Docker-owning tests handled separately).
Viewer: `scripts/test-isolated.sh viewer` (Node tests plus production build).
Game: `scripts/smoke-isolated.sh` (real offline OpenTTD, scripted seat, no model credentials).
Dependency policy: a repository script validates final lock integrity/age and reports exact advisory matches; networked metadata lookup is distinct from offline execution.

These interfaces are acceptance requirements to implement and exercise, not claims that they already exist or pass.
