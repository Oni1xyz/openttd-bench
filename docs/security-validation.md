# Security hardening validation checkpoint

This is a partial checkpoint, NOT a release-readiness or security-certification claim.
The game/scoring definitions have not been changed. No real model credentials,
provider requests, paid VMs, upstream messages, or upstream PRs were used.

## Parent-verified execution

- `scripts/test-isolated.sh python`: 156 passed, 2 skipped, 38 subtests passed.
  The skips are a missing historical HogEx baseline archive and the framework's
  Docker-owning integration test (the test container deliberately has no daemon socket).
- `scripts/test-isolated.sh audit-python`: 124 exact Python/baked-Codex package/version
  combinations, 124 registry integrity/age checks, 124 OSV queries, zero advisory
  matches. Cutoff: `2026-10-22T00:00:00Z`. This excludes the viewer, system packages,
  and Node/OpenTTD binaries; it is not a whole-image vulnerability scan.
- `scripts/smoke-isolated.sh`: real OpenTTD 15.3, `free_for_all-s101`, four companies,
  a test-only one-year horizon. Three scripted seats retired immediately; the remaining
  seat actually ran as UID 1000, built a rail depot at (40,40), re-read its ownership,
  advanced the game clock, retired, and reached 1951-01-01. The unchanged grader ran
  against a fresh final accounting snapshot. Score zero is expected for this deliberately
  minimal construction-only smoke; it is not a model performance result.
- Smoke elapsed time: 187.88 seconds. Both private-file access attempts were denied;
  three invalid/unauthorized HTTP requests were rejected. Prebuilt tools reported
  `codex-cli 0.162.1` and Node `v22.23.4`. No inference calls occurred.

The game smoke has no network, published ports, host home, credentials or Docker socket.
It needs a writable disposable container root and scoped UID/file-management/signal
capabilities to exercise the real root bootstrap and subsequent unprivileged seat.
It is bounded to 2 CPUs, 2 GiB RAM, 256 PIDs, and a 600-second external deadline.
Unit/security tests instead run with a read-only root/source, no capabilities and no
network. The metadata-only audit uses network access but isolated stdlib Python with
site-package/startup hooks disabled.

## Observed compatibility issue

The first smoke used the upstream `two_towns_rail` scenario with seed 7 and failed
with `FoundTown failed: ERR_SITE_UNSUITABLE` at (108,128). No terrain/scenario/scoring
fix was attempted. The passing smoke uses the existing free-for-all seed 101 instead.
Do not infer that every historical scenario/configuration has been exercised.

## Outstanding gates at this checkpoint

- Runtime/credential workstream integration and its actual process/network acceptance.
- Viewer follow-up for the known KaTeX advisory, then parent rerun of its production
  build, request canary and complete bundled-dependency audit.
- Independent spec and adversarial quality review, followed by final regression runs.
- A fork-local PR and explicit CI status. Nothing is merged.

Raw execution logs and synthetic game artifacts are not committed. Re-run the scripts
for fresh evidence; these checkpoint counts must not be presented as final acceptance
for later commits.
