# Contributing to kew

Thanks for your interest in kew — the open-source control plane for autonomous coding
agents. This guide covers how to get a development environment running and the conventions
we follow.

By contributing, you agree that your contributions are licensed under the project's
[Apache License 2.0](LICENSE).

## Contributing from outside

The public repository is [thossullivan/kew](https://github.com/thossullivan/kew).
Issues and pull requests are welcome.

- **The maintainer reviews and merges.** Every pull request is reviewed by the
  maintainer, who decides whether and when it merges.
- **History starts at 1.0.0.** The public repository begins with the 1.0.0
  release. Development history before it is not in the public repository, so an
  issue or pull request number cited in older comments and docs (`#1234`) may
  not exist here.
- **Run the gates before you open a pull request.** From `kew/`, run
  `bash scripts/verify.sh`; it mirrors every check that gates a pull request
  (see [Quality gates](#quality-gates)). CI runs the same checks on
  GitHub-hosted runners for each pull request to `main`: lint, format, type
  check, bandit and import contracts; the Python test suite; the frontend and
  site checks; the complexity gate; semgrep; the dependency audit and license
  check; the containment and agent-image checks; a check that keeps
  `.kew/memory/` changes in commits of their own; a secret scan; and the docs
  build gate.
- **Tests follow `AGENTS.md`.** Its "Writing tests" section is the rule set a
  test must meet: test observable behavior, patch only at process, network,
  clock or forge boundaries, and never weaken a test to make it pass.

## Development setup

kew is a Python 3.13+ project managed with [uv](https://docs.astral.sh/uv/). The package
lives in the `kew/` subdirectory.

```bash
git clone https://github.com/thossullivan/kew
cd kew/kew          # the package lives in the kew/ subdir
uv sync             # install deps into a managed virtualenv
```

## Quality gates

The simplest way to run every gate before opening a pull request is from the `kew/`
package directory:

```bash
bash scripts/verify.sh
```

This mirrors every PR-gating CI check (parity is enforced by `tests/test_verify_parity.py`).
To run a single gate in isolation, use the individual commands below (all from `kew/`):

Ordinary verification reuses `UV_CACHE_DIR` or uv's normal user cache.
The creator of an explicit `UV_CACHE_DIR` owns its cleanup.
Never delete entries or force pruning in a shared uv cache.

Use this recipe when one verification task needs a private cache:

<!-- private-uv-cache-recipe:start -->
```bash
private_uv_root="$(mktemp -d "${TMPDIR:-/tmp}/kew-verify.XXXXXX")" || exit
cleanup_private_uv() {
  private_uv_status=$?
  trap - EXIT
  rm -rf -- "$private_uv_root"
  return "$private_uv_status"
}
trap cleanup_private_uv EXIT
UV_CACHE_DIR="$private_uv_root/uv-cache" bash scripts/verify.sh
```
<!-- private-uv-cache-recipe:end -->

The foreground command finishes before cleanup starts.
The trap preserves the command status and removes only its allocation.
Do not place reusable environments or preserved evidence inside that allocation.

```bash
uv run ruff check .                                                                 # lint
uv run ruff format --check .                                                        # format
uv run mypy src/                                                                    # type check (strict)
uv run lint-imports                                                                 # module-boundary contracts
uv run xenon --max-absolute B --max-modules B --max-average A \
  -e "$(paste -sd, .xenon-excludes)" src/                                           # complexity
uv run bandit -r src/ -c pyproject.toml                                             # security lint
uvx --with setuptools==84.2.1 semgrep==1.182.1 scan --config .semgrep/pinned.yml --error \
  --strict --timeout 120 --timeout-threshold 0 --max-target-bytes 0 \
  --exclude-rule semgrep.python.jinja2.security.audit.autoescape-disabled-false.incorrect-autoescape-disabled \
  src/                                                                               # SAST (pinned semgrep and ruleset; versions in scripts/tool-pins.env)
uv run pip-audit --skip-editable --desc                                             # dependency CVEs
uv run --with pip-licenses==5.5.5 pip-licenses --fail-on="GPL" --partial-match     # license gate (version in scripts/tool-pins.env)
gitleaks detect --source .. --verbose --redact                                      # secret scan (install: brew install gitleaks)
export PYTEST_XDIST_AUTO_NUM_WORKERS=6                                              # same worker cap as CI (verify.sh sets it)
uv run pytest -n auto -m "not serial" --cov=src/kew                                 # tests, parallel phase
uv run pytest -m serial --cov=src/kew --cov-append --cov-fail-under=85             # timing-sensitive tests, one process + floor
```

The parallel phase can also be split across machines. Adding `--shard=K/N`
(`kew/tests/support/shard.py`) to it runs the K-th of N groups, balanced by the
durations recorded in `kew/tests/.test_durations.json`. The N runs together
select exactly what the unsharded phase does — the split is a partition, pinned
on the real collection by `kew/tests/test_shard.py` — so a verify host can run
them on N VMs at once and merge the results. Only that host passes the flag:
the commands above are the whole suite, and `kew/tests/test_serial_phase.py`
refuses a documented phase that narrows, which is why there is no copy-paste
line for it here. Refresh the recording when the balance drifts
(`kew/tests/test_shard.py` fails when it has);
`kew/scripts/refresh_test_durations.py`'s docstring has the two commands. A
stale recording costs balance, never coverage.

The frontend gate (from `frontend/`):

```bash
npm ci
npm test -- --run
npm run check
```

Keep functions small and boundaries clean and these stay green.

## How we work

- **Test-driven development.** Write a failing test first, watch it fail, then implement the
  minimal code to pass. New features and bug fixes both need tests.
- **Commit subjects state behaviour.** A subject is one sentence that says what the
  project does after the change, for example "kew rename-repo moves a renamed
  repository's ledger rows to project.repo". No conventional-commit prefix. A pull request
  is squash-merged under its title, so the title follows the same rule.
- **Keep PRs focused.** One logical change per PR; it makes review and `git` history clean.
- **Update docs with behavior.** If you change a CLI command, config field, or security
  pattern, update the matching file under `docs/` (see `AGENTS.md` for the mapping).

## No silent clean defaults

**Silence on failure** is this project's recurring bug archetype: the system
reports a clean or absent state when it could not determine the state. The
convention below follows from it, and it is enforced mechanically.

The rule, and it is not "add logging":

> **No `except`-and-default path may return a clean state without recording why
> it defaulted** — a reason on the returned state, a warning line, or an audit
> fact. Never a bare clean default.

A *clean default* is a value indistinguishable from a real, healthy answer:
`None`, `False`, `0`, `""`, an empty list/dict/set. Returning one from an
exception handler tells the caller "I looked, and there was nothing" when the
truth is "I could not look". The same rule applies to a gate: **a check that
cannot run must fail, not pass** — a scan that could not read its target reports
an error, never zero findings.

`if result.returncode != 0: return None` is the same defect, but this gate does
not detect it. See `kew/src/kew/labels.py:67`. The gate also does not see `contextlib.suppress`, `except: pass` and `except: continue` (about 96 sites); #1764 lists them for a later pass.

### The house example

`_select_dispatch_issue` in `kew/src/kew/cli/__init__.py` is the shape to copy.
A failed GitHub query cannot mean "the queue is empty", so it does not say so:

```python
try:
    issue = select_next_issue(config=config, force_issue=force_issue)
except GhQueryError as exc:
    _error(
        "GitHub query failed — cannot tell if the queue is empty "
        f"(this is NOT an empty queue): {exc}"
    )
    return GH_QUERY_FAILURE_EXIT_CODE
```

Both halves matter: the operator gets a line naming the cause, and the return
value is distinct from the one a genuinely empty queue produces. Contrast the
counter-example in the same file — `kew status`'s per-label `_count` helper,
where `except ValueError: return 0` renders a failed `gh` call as a healthy
all-zero queue with exit 0.

Any of these satisfies the rule:

- a reason carried on the returned state (`QueueCount(value=None, reason=...)`),
- a warning/error line naming what failed and what it does *not* imply,
- an audit fact recording the degraded read,
- re-raising a typed error the caller must handle.

### The gate

`kew/tests/test_silence_on_failure.py` walks the AST of `src/kew/`. It flags
`except` handlers that return or assign clean defaults while recording nothing.
It runs with the normal test suite:

```bash
uv run pytest tests/ -k 'silence or fail_loud' -v
```

Known sites are listed in `kew/ratchets/silence_on_failure_baseline.txt` — a **debt
ledger, not an approval list**. The gate ratchets in both directions: a new site
fails until it is fixed (or added to the ledger deliberately, in a diff a
reviewer can see), and a site that is fixed or deleted must be removed from the
ledger, so the recorded corpus can only shrink.

The current ledger records 217 handlers in 200 functions. Burn-down work is
tracked in #1764.

## Test-quality ratchet

`kew/tests/test_test_quality_ratchet.py` runs `kew/scripts/test_quality_scan.py`
over every test file and compares, per file, the count of interaction-only
tests, patches of code the file is testing, source-text tests, skip/xfail
markers and imports from other `test_*.py` modules with
`kew/ratchets/test_quality.json`. A count above the ledger fails, naming the
file and the count; a count below it fails too, asking you to bank the fall
with `uv run python scripts/test_quality_scan.py --lower` (which only ever
lowers). A new test file named for a review round (`test_round*`,
`test_w42_round*`) or a PR/issue number is refused — name it for the
behaviour it covers.

`--initial` writes the first ledger from the tree as it stands, and refuses to
run while a ledger exists. It records the starting point; it is never the way
to make room for a rise.

Ratchet ledgers live in `kew/ratchets/`, not `tests/`: that directory is a
merge-gate path and an always-guarded loop path, so a change cannot widen its
own exceptions unseen.

## Repo-wide gates carry the `policy` mark

The silence gate above, the id sweep (`tests/test_id_sweep_gate.py`), the docs
truthfulness checks and every other test that *scans the tree* rather than
exercising one module must be marked:

```python
@pytest.mark.policy
def test_no_unguarded_int_coercion_on_item_ids() -> None: ...
```

A governed remediation round runs only the tests covering the files it changed,
so a scan gate — which no changed file imports — would never be selected. kew's
brief always adds `uv run pytest -m policy` to that set
(`[commands] test_policy_mark`), so a marked gate runs in every round; an
unmarked one is invisible until the pre-merge verification fails.
`tests/test_policy_mark_gate.py` finds scan-shaped tests by their AST and fails
on any that is neither marked nor listed in its `_EXEMPT` table with a reason.

## Reporting bugs and proposing features

- **Bugs / features:** open a [GitHub issue](https://github.com/thossullivan/kew/issues).
- **Questions / ideas:** start a
  [GitHub Discussion](https://github.com/thossullivan/kew/discussions).
- **Security vulnerabilities:** do **not** open a public issue — see
  [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you
are expected to uphold it.
