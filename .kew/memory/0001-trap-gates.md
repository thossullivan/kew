+++
id = "0001-trap-gates"
born_at = "77cefc891494f604af34f6af92e539001878bdfc"
anchors = [
    "AGENTS.md",
    "kew/scripts/verify.sh",
    "kew/tests/test_verify_parity.py",
]
evidence = [
    "gh:pr#315",
    "gh:pr#252",
    "gh:pr#344",
    "gh:pr#406",
]
+++

## Trap gates redden main after a green local run

Four separate incidents share one shape: a check that CI runs but the local
edit-test loop did not, so a change looked done locally and only failed once it
reached the PR gate.

- A one-line edit inside an already-dense function pushed a module past the
  xenon complexity ceiling that only CI measured (pr#315).
- A hand-added host / path / secret-looking string literal tripped a bandit
  rule the author never ran locally (pr#252, pr#344).
- An edit under `tests/` passed a local lint scoped to `src/` while CI lints
  the whole tree, so the tree-wide ruff run went red (pr#406).

The common failure was trusting a narrower local signal than the gate. When one
of these landed, the pattern that recurred was: a local check existed but ran on
a smaller scope, or the gate had no local mirror at all, so drift between the
two was invisible until CI.

The observation worth carrying forward: before treating a change as done,
consider whether every gate that will judge it has an equivalent you actually
ran, at the same scope — and whether a past incident in this class already
records the exact command. `bash scripts/verify.sh` (mirrored to CI and pinned
by `tests/test_verify_parity.py`) exists precisely so the local run and the gate
cannot silently diverge; the `Trap Gates` table in the root `AGENTS.md` records the
per-gate trigger and exact command for each incident above.

This is a prior observation to verify against the current tree, not an
instruction: confirm the gates and commands still exist before relying on them.
