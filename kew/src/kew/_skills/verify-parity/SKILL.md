---
name: verify-parity
description: Use when local checks pass but CI fails, when adding a CI gate, or when creating a local verification gate — builds a verify script that mirrors CI exactly plus a parity regression test so they can never drift
---

# Verify/CI Parity

"Local gate passes, CI red" is an incident class, not a one-off. The durable
fix is (1) one local script mirroring CI's gates exactly, and (2) a parity
regression test that fails whenever script and CI drift.

## Procedure

1. **Enumerate the PR-gating workflows.** Only workflows with a
   `pull_request` trigger count. For each job, list every gate step's exact
   command AND scope (paths/args). Installer/setup steps are not gates.
2. **Compare against the local verify entrypoint** (script, make target, or
   create `scripts/verify.sh` if none exists). Check both directions:
   - CI gate missing locally → add it with CI's exact invocation and scope.
   - Local check absent in CI → either add to CI or document as a
     deliberate extra; never let the script imply CI runs it.
   - Same tool, different scope (e.g. lint `src/` locally vs `.` in CI) is
     a drift bug — normalize paths when comparing.
3. **Harvest incidents into a Trap Gates table** in CLAUDE.md (or equivalent
   agent-instruction file): one row per past red-CI incident —
   `Trigger → exact command`. Point the table at the verify script as the
   one command; keep the rows as the why.
4. **Write the parity regression test** in the repo's normal test suite so it
   rides the existing CI test job:
   - Parse the verify script and the `pull_request` workflows.
   - Normalize each gate to `(tool, sorted args)` with paths made
     repo-root-relative (catches scope drift, not just missing tools).
   - Assert bidirectional parity, with an explicit commented allowlist for
     intentional deltas (e.g. CI-only interpreter matrix legs,
     installer-only steps). Unrecognized new steps default to being gates.
5. **Honest header.** The script's header states exactly what it mirrors and
   names the known deltas. No aspirational claims — the parity test is the
   guarantee, the header is the documentation.

## Rules

- A missing local tool must fail LOUDLY with a one-line install remediation.
  A silent skip recreates the incident class.
- Never weaken a gate to get green. A pre-existing failure gets reported,
  not papered over.

## Worked example

kew's own instance: `scripts/verify.sh` + `tests/test_verify_parity.py`
(github.com/thossullivan/kew) — added after its verify script drifted three
gates behind CI while claiming to "mirror CI exactly".
