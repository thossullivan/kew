#!/usr/bin/env bash
# Local verification gate — mirrors the PR-gating CI (.github/workflows/ci.yml).
# Parity is enforced by tests/test_verify_parity.py: adding a gate to CI
# without mirroring it here fails the test suite.
#
# Known intentional deltas from CI (the ONLY ones — no aspirational claims):
#   * Python: CI runs pytest on 3.13 only; this script runs your active
#     interpreter only.
#   * gitleaks install: CI's `secrets` job installs gitleaks on the runner;
#     locally we fail loudly with a remediation line if it is missing —
#     a silent skip would recreate the "local pass, CI red" gap (#406/#315/
#     #252/#344 class) this script exists to close.
#   * npm install: CI's `frontend` job runs `npm ci`; locally we fail loudly
#     with a remediation line if npm is missing (same rationale as gitleaks).
#
# The frontend gate always runs (this script has no changed-path scoping);
# `npm ci` is part of the gate so a fresh checkout still gates correctly.
#
# Run from kew/: bash scripts/verify.sh
# The script preserves UV_CACHE_DIR. Its caller owns every explicit cache path.
set -euo pipefail
# Inherited shell state must not shadow the tools below: an exported function
# (BASH_FUNC_uv%%), a hashed path (`hash -p`), or a BASH_ENV file that bash
# sources into this script AND into every `bash scripts/*.sh` gate it starts.
# Aliases need no cleanup (a non-interactive bash does not expand them).
# tests/test_serial_phase.py proves each of these against the xdist phase.
unset BASH_ENV
hash -r
unset -f uv uvx gitleaks npm
# No silent skips (#2480): every pytest below fails on a skip the environment
# caused — a missing docker, git, bash, gitleaks — and names what was missing.
# Intended skips are declared once, in tests/support/required_tools.py. CI sets
# the same value workflow-wide; tests/test_verify_parity.py pins the pair.
export KEW_REQUIRE_TOOLS=1

# Staged merge verify (#3212). `kew verify` runs this gate in two jobs and
# judges the first before it starts the second: KEW_VERIFY_STAGE=gates runs
# the static gates and the containment test below and stops, and
# KEW_VERIFY_STAGE=suite runs everything after them. kew runs the held-out
# phase between the two, so a red static gate or an undeclared held-out
# failure never starts a pytest phase. Only those exact values split the gate:
# a typo runs all of it, and CI and a hand-run verify.sh set nothing and run
# every gate, in this order.
#
# Each switch is read ONCE, here, into a shell variable this script does not
# export, and the exported name is unset. An inline `KEW_VERIFY_STAGE=suite
# bash scripts/verify.sh` exports it to every child, so the pytest phases
# below would hand it to each test that runs verify.sh itself, and that nested
# run would skip its own gates. KEW_VERIFY_FRONTEND is read and dropped the
# same way. KEW_VERIFY_IMAGE_SCAN is dropped too, then set again under its own
# name as a plain shell variable: the scan gate below still reads it, but no
# child inherits it, so a nested run scans images unless told otherwise itself.
verify_stage="${KEW_VERIFY_STAGE:-all}"
verify_suite="${KEW_VERIFY_SUITE:-all}"
verify_frontend="${KEW_VERIFY_FRONTEND:-full}"
verify_image_scan="${KEW_VERIFY_IMAGE_SCAN:-run}"
unset KEW_VERIFY_STAGE KEW_VERIFY_SUITE KEW_VERIFY_FRONTEND KEW_VERIFY_IMAGE_SCAN
KEW_VERIFY_IMAGE_SCAN="$verify_image_scan"
if [ "$verify_stage" = "suite" ]; then
    echo "verify.sh: KEW_VERIFY_STAGE=suite — the static gates and the containment test ran in kew verify's gate job, so they are NOT run here" >&2
fi
# Phase markers (#3215): one `kew-phase: <name> start|end <unix-seconds>` line
# per phase, which kew verify's ledger reads each phase's seconds and outcome
# off (kew/src/kew/verify_ledger.py). A phase that fails prints no end line.
if [ "$verify_stage" != "suite" ]; then
echo "kew-phase: static start $(date +%s)"
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run lint-imports
# Lesson-channel guard (spec D8, Task 8) — mirrors ci.yml's `memory-channel`
# job. Fails if any commit ahead of the base mixes `.kew/memory/` (the untrusted
# lesson corpus) with ordinary work files. Base ref defaults to origin/main then
# main; CI pins it via MEMORY_GUARD_BASE. Parity: tests/test_verify_parity.py.
uv run python scripts/check_memory_channel.py
# Proactive pricing-completeness guard (#934) — mirrors ci.yml's lint job. The
# full `uv run pytest -n auto` below also covers it, but this standalone gate keeps
# the fast, explicit signature in parity with CI (tests/test_verify_parity.py).
uv run pytest tests/test_pricing_completeness.py
# API-surface drift gate (A3, #1438) — mirrors ci.yml's lint job. Fails if the
# committed openapi.json no longer matches the schema the app serves; the fix is
# `uv run python scripts/export_openapi.py`. tests/test_openapi_route_coverage.py
# then holds every route in that file to a doc row or an explicit allowlist
# entry, which is what makes the counted claims in docs/security.md falsifiable.
uv run python scripts/export_openapi.py --check
# Xenon 0.9.3 normally exits 1 for a violation, but keep the gate fail-closed
# if a wrapper prints its ERROR diagnostics and exits 0.  The command itself is
# kept byte-for-byte equivalent to ci.yml; parity is enforced by the tests.
xenon_log="${TMPDIR:-/tmp}/kew-xenon-$$.log"
trap 'rm -f "$xenon_log"' EXIT
set +e
uv run xenon --max-absolute B --max-modules B --max-average A \
    -e "$(paste -sd, .xenon-excludes)" src/ 2>"$xenon_log"
xenon_exit=$?
set -e
xenon_output=$(<"$xenon_log")
if [ -n "$xenon_output" ]; then
    printf '%s\n' "$xenon_output" >&2
fi
if grep -q 'ERROR:xenon:' "$xenon_log"; then
    echo "verify.sh: Xenon reported a complexity violation" >&2
    exit 1
fi
if [ "$xenon_exit" -ne 0 ]; then
    exit "$xenon_exit"
fi
uv run bandit -r src/ -c pyproject.toml
# semgrep runs at the one exact version scripts/tool-pins.env names, read the
# way ci.yml's `sast` job reads it (tests/test_verify_parity.py). Read, never
# sourced: this script sources no file (tests/test_serial_phase.py).
SEMGREP_VERSION="$(awk -F= '$1 == "SEMGREP_VERSION" { print $2 }' scripts/tool-pins.env)"
SETUPTOOLS_VERSION="$(awk -F= '$1 == "SETUPTOOLS_VERSION" { print $2 }' scripts/tool-pins.env)"
uvx --with setuptools=="${SETUPTOOLS_VERSION}" semgrep=="${SEMGREP_VERSION}" scan --config .semgrep/pinned.yml --error \
    --strict --timeout 120 --timeout-threshold 0 --max-target-bytes 0 \
    --exclude-rule semgrep.python.jinja2.security.audit.autoescape-disabled-false.incorrect-autoescape-disabled \
    src/
uv run pip-audit --skip-editable --desc
PIP_LICENSES_VERSION="$(awk -F= '$1 == "PIP_LICENSES_VERSION" { print $2 }' scripts/tool-pins.env)"
uv run --with pip-licenses=="${PIP_LICENSES_VERSION}" pip-licenses --fail-on="GPL" --partial-match
# CI's `secrets` job runs the gitleaks CLI from the repo root with
# `gitleaks detect --source . --verbose --redact` on a full-history checkout.
# Closest local equivalent: same command against the repo root (`..` from
# kew/); the repo-root .gitleaksignore is picked up automatically.
#
# The VERSION is part of the gate, not an implementation detail. 8.24.3 does not
# understand `.gitleaks.toml`'s `[[allowlists]]` syntax and discards it without
# warning, so it reports this repo's history as dirty while a newer build
# reports it clean — same commits, same config. Running a different version
# locally means verify.sh and CI are not the same check, which is the whole
# point of this script. CI pins the same number (ci.yml `secrets`), and
# tests/test_verify_parity.py pins that the two agree.
GITLEAKS_VERSION="8.30.0"
if ! command -v gitleaks >/dev/null 2>&1; then
    echo "verify.sh: gitleaks not found — install it: brew install gitleaks (no PyPI/uv package; see https://github.com/gitleaks/gitleaks/releases)" >&2
    exit 1
fi
if [ "$(gitleaks version)" != "$GITLEAKS_VERSION" ]; then
    echo "verify.sh: gitleaks $(gitleaks version) is not CI's pinned $GITLEAKS_VERSION — the secrets gate may disagree with CI (8.24.3 silently ignores .gitleaks.toml). Install the pinned build: brew upgrade gitleaks, or https://github.com/gitleaks/gitleaks/releases/tag/v${GITLEAKS_VERSION}" >&2
fi
gitleaks detect --source .. --verbose --redact
echo "kew-phase: static end $(date +%s)"
# Sandbox containment proof — CI's `containment` job. The docker-requiring
# escape/mount test (@_REQUIRES_DOCKER) skips when docker is absent, and under
# KEW_REQUIRE_TOOLS=1 (above) that skip fails here as it does in CI, whose
# hosted containment job also fails closed without a reachable Docker daemon.
# Deliberate parity stance: see CONTAINMENT_GATE_SIGNATURES in
# tests/test_verify_parity.py.
echo "kew-phase: containment start $(date +%s)"
uv run pytest tests/test_sandbox_containment.py -v
echo "kew-phase: containment end $(date +%s)"
fi
if [ "$verify_stage" = "gates" ]; then
    echo "verify.sh: KEW_VERIFY_STAGE=gates — the static gates and the containment test passed; the rest of the gate runs in kew verify's suite job" >&2
    exit 0
fi
# Coverage floor (#951) — mirrors ci.yml's `test` job exactly (same flags, same
# scope) so a governance path silently losing its tests fails the gate here too.
# The floor (85%) is set ~2pt below the measured baseline: honest, never
# aspirational. Keep this command byte-for-parity with ci.yml (enforced by
# tests/test_verify_parity.py).
# Two phases (parity with ci.yml): Textual pilot tests and the wall-clock
# deadline tests flake under CPU contention (#1772), so they run alone
# after the xdist phase (marker `serial`, see tests/conftest.py).
# Worker cap: `-n auto` reads logical CPUs (24 on the maintainer's workstation), and the
# same box hosts both self-hosted CI runners, so an uncapped local run plus a
# CI leg starves the deadline tests and the operator. Default to ci.yml's cap
# (tests/test_serial_phase.py pins both); a caller may LOWER it
# (PYTEST_XDIST_AUTO_NUM_WORKERS=2 bash scripts/verify.sh), never raise it.
export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-6}"
# pytest-xdist int()s the value and, on failure, WARNS and falls back to one
# worker per logical CPU: a typo would recreate the incident. It also has no
# upper bound (600 starts 600 workers). The override only LOWERS the cap:
# two CI legs (concurrency is per PR) plus one local run at 12 would take all
# 24 logical CPUs on the shared host. Raise the ceiling by editing this line
# and ci.yml's cap together (tests/test_serial_phase.py pins both).
if ! [[ "$PYTEST_XDIST_AUTO_NUM_WORKERS" =~ ^[1-6]$ ]]; then
    echo "verify.sh: PYTEST_XDIST_AUTO_NUM_WORKERS must be an integer from 1 to 6, got '$PYTEST_XDIST_AUTO_NUM_WORKERS'" >&2
    exit 1
fi
# Sharded verify (#2380). `kew verify --shards N` runs the parallel phase as N
# jobs on N hosts at once and everything else once, on the first of them;
# KEW_VERIFY_SUITE=elsewhere is how that host is told the two whole-suite
# phases below are running somewhere else. Only that exact value skips them, so
# a typo runs the whole gate rather than silently dropping the suite, and a
# hand-run verify.sh sets nothing and runs it. kew then runs the serial phase
# itself, without the floor: the floor reads ONE machine's joined coverage
# data and a sharded run has none, since each shard's data stays on its own
# host. That is the one gate a sharded verify does not discharge — it says so
# in its own output and in docs/cli/verify.md, and CI still applies the floor
# to the joined coverage on every PR.
#
# Tiered verify (#3202). With `[verify] suite = "tiered"`, kew computes on the
# operator side which test files a PR's change selects (kew.verify_selection)
# and tells this script KEW_VERIFY_SUITE=selected: the two whole-suite phases
# and the coverage floor are skipped here, exactly as for `elsewhere`, and the
# job runs the two phases itself on that file list, plus every `-m policy`
# test, after this script. The floor reads the whole suite's coverage, so it
# runs only on a full verify (the nightly, or a verify that fell back); kew
# says so in its own output and in docs/cli/verify.md. Only that exact value
# skips them: a typo runs the whole gate, and CI and a hand-run verify.sh set
# nothing and run it.
if [ "$verify_suite" = "elsewhere" ]; then
    echo "verify.sh: KEW_VERIFY_SUITE=elsewhere — the two pytest phases and the coverage floor are NOT run here (kew verify --shards)" >&2
fi
if [ "$verify_suite" = "selected" ]; then
    echo "verify.sh: KEW_VERIFY_SUITE=selected — the two pytest phases and the coverage floor are NOT run here; kew verify runs the selected test files and -m policy after this script ([verify] suite = \"tiered\")" >&2
fi
if [ "$verify_suite" != "elsewhere" ]; then
if [ "$verify_suite" != "selected" ]; then
echo "kew-phase: xdist start $(date +%s)"
uv run pytest -n auto -m "not serial" --cov=src/kew
echo "kew-phase: xdist end $(date +%s)"
echo "kew-phase: serial start $(date +%s)"
uv run pytest -m serial --cov=src/kew --cov-append --cov-fail-under=85
echo "kew-phase: serial end $(date +%s)"
fi
fi

# Agent-image CVE gate — CI's `agent-image-scan` job. Both sides call the same
# script so the parity signature cannot drift (same pattern as the docs and www
# gates). It builds every agent image and fails on any CRITICAL/HIGH with a fix
# available; the script refuses rather than skipping when docker or trivy is
# missing, because a gate that silently no-ops is worse than no gate.
# kew verify sets KEW_VERIFY_IMAGE_SCAN=skip (unexported above, so no child
# sees it) when the candidate's diff touches
# no image input (docker/, uv.lock, the image recipe), and says so in its
# verdict and its audit event; the nightly scans every image in its own job
# and files an issue per new CVE (#3195). CI and a hand-run verify.sh set
# nothing and scan.
if [ "${KEW_VERIFY_IMAGE_SCAN:-run}" = "skip" ]; then
    echo "verify.sh: KEW_VERIFY_IMAGE_SCAN=skip — no image input changed, so the agent-image scan is NOT run here (kew verify nightly scans every image)" >&2
fi
if [ "${KEW_VERIFY_IMAGE_SCAN:-run}" != "skip" ]; then
echo "kew-phase: image_scan start $(date +%s)"
bash ../scripts/scan-agent-image.sh
echo "kew-phase: image_scan end $(date +%s)"
fi

# Docs gate — CI's `docs.yml` build job. Both sides call the same script so the
# parity signature cannot drift (same pattern as the www gate below). It builds
# the docs with the pinned mkdocs line and asserts no internal working material
# reaches the built site (the PR #1407 leak class).
echo "kew-phase: docs start $(date +%s)"
bash ../scripts/check-docs.sh
echo "kew-phase: docs end $(date +%s)"

# Frontend gate — CI's `frontend` job. Mirrors
# `npm ci && npm test -- --run && npm run check` in frontend/ (svelte-check +
# vitest). Runs last and cds into frontend/ (the script exits after, so the
# cwd change does not leak).
if ! command -v npm >/dev/null 2>&1; then
    echo "verify.sh: npm not found — install Node.js: https://nodejs.org (or brew install node)" >&2
    exit 1
fi
#
# Scoped frontend verify (#3213). With `[verify] frontend = "scoped"`, kew
# computes on the operator side what of vitest and ui-check a PR's change owns
# (kew.verify_frontend) and tells this script KEW_VERIFY_FRONTEND=scoped, read
# once into verify_frontend at the top: the
# whole vitest run is skipped here, and the job runs `vitest related` on the
# changed files after this script. svelte-check and every other gate still run
# whole. Only that exact value skips it: a typo runs the whole gate, and CI and
# a hand-run verify.sh set nothing and run it.
#
# The vitest phase marks both branches: the full run, or under scoped only
# `npm ci`. The job's own `vitest related` run after this script is marked
# as part of its ui_check phase (kew/src/kew/merge_verify.py).
echo "kew-phase: vitest start $(date +%s)"
cd ../frontend
npm ci
if [ "$verify_frontend" = "scoped" ]; then
    echo "verify.sh: KEW_VERIFY_FRONTEND=scoped — the full vitest run is NOT run here; kew verify runs vitest related on the change after this script ([verify] frontend = \"scoped\")" >&2
fi
if [ "$verify_frontend" != "scoped" ]; then
npm test -- --run
fi
echo "kew-phase: vitest end $(date +%s)"
echo "kew-phase: svelte_check start $(date +%s)"
npm run check
echo "kew-phase: svelte_check end $(date +%s)"

# Launch-site gate — CI's `www` job. Mirrors `npm ci && npm run gates` in www/
# (token sync from frontend/src/app.css, svelte-check, and the build, which
# carries the no-dashes copy gate). The three gates live behind one npm script
# on both sides so the parity signature cannot depend on whether www/build
# happens to exist locally — see the note in ci.yml's `www` job.
echo "kew-phase: www start $(date +%s)"
cd ../www
npm ci
npm run gates
echo "kew-phase: www end $(date +%s)"
