# kew

The open-source control plane for autonomous coding agents.

## Tech Stack

Python 3.13+, Typer, Pydantic v2, Jinja2, Textual, Rich. Uses stdlib tomllib.
Package managed with uv. Tests with pytest. SQLite for run history (run_store.py).

## Conventions

- TDD: write failing test first, then implement
- ruff format + ruff check + mypy src/ + xenon + bandit + semgrep before committing
- Commit subjects are behaviour statements, not conventional commits: one
  sentence saying what the project does after the change (see `git log --oneline`).
  A pull request squash-merges under its title, so the title follows the same rule
- All subprocess calls go through subprocess_utils.py allowlist
- Run from `kew/` subdir: `uv run pytest tests/ -v`, `uv run kew tui`

## Trap Gates (one command: `bash scripts/verify.sh`, before committing)

Run `bash scripts/verify.sh` (from `kew/`) — it mirrors every PR-gating CI check.
Inside a kew sandbox (dispatch or remediation, 8 GB memory cap) do NOT run it,
the full pytest suite, semgrep or a bare `npm run ui-check`: run the tests for
the files you touched plus ruff, mypy and xenon, and for a frontend change
`npm run ui-check -- <module> ...` for the surfaces you touched. The merge verify
runs the full suite, semgrep and every browser check on the verify host (#2984).
`tests/test_verify_parity.py` enforces parity. CI pins pytest to Python 3.13.
The table below explains why each gate exists —
each trap has caused a red main; use the exact command when iterating on one gate.

| Trigger | Exact command |
|---|---|
| You edited inside an existing function — any inline edit to already-dense code (#315) | `uv run xenon --max-absolute B --max-modules B --max-average A -e "$(paste -sd, .xenon-excludes)" src/` |
| You added a host/URL/path/secret-looking string literal by hand (#252, #344) | `uv run bandit -r src/ -c pyproject.toml` |
| You touched anything under `tests/` or outside `src/` — CI lints the whole tree (#406) | `uv run ruff check . && uv run ruff format --check .` |
| You added a test that scans the tree instead of one module (#2174) — mark it `@pytest.mark.policy` so every unsandboxed run and the merge verify run it | `uv run pytest -m policy` |
| You need a semgrep suppression (#256) | `# nosemgrep: <rule-id>` inline ON the finding's call line — never the line above. Re-check (ruleset pinned to `.semgrep/pinned.yml`, not `--config auto` — see `.semgrep/README.md`; semgrep and setuptools at the versions `scripts/tool-pins.env` pins): `uvx --with setuptools==84.2.1 semgrep==1.182.1 scan --config .semgrep/pinned.yml --error --exclude-rule semgrep.python.jinja2.security.audit.autoescape-disabled-false.incorrect-autoescape-disabled src/` |

## Writing tests

- Test observable behavior: return values, files, store rows, HTTP responses,
  CLI output and exit codes.
- Do not assert only on internal calls (`assert_called*`, `call_args`) when an
  outcome can be checked.
- Do not patch functions defined in the module under test; patch at process,
  network, clock or forge boundaries.
- Do not assert on source text (`inspect.getsource`, regex over `src/`); write
  an AST lint under `-m policy` when the rule is architectural.
- Name tests and files for the behavior they pin, never for a review round or PR
  number. Shared helpers go in `tests/support/`, never imported from another
  `test_*.py`.
- Never weaken a test to pass: no removed asserts, no skip or xfail, no changed
  expected values, unless the issue declares the behavior change; then say so in
  the PR body under **Behavior changes**.
- Characterization tests that pin behavior before a refactor are deliberate; say
  so in their docstring.

## TUI (Textual) Patterns

The TUI dashboard lives in `src/kew/tui/`. Key patterns:

### Keybindings
- Use uppercase chars for shift bindings: `("J", "action")` not `("shift+j", "action")`
- ListView consumes `enter` — use `on_list_view_selected` event, not a screen binding
- Define `action_quit` directly on pushed screens — actions don't reliably bubble to App

### Scrolling
- Trackpad scroll goes to the terminal, not unfocused Textual widgets. Use keyboard shortcuts (Shift+J/K) to scroll secondary panes while keeping focus on the primary list.
- Proven scrollable pattern: `VerticalScroll` + `Static(height: auto)`. VerticalScroll handles scroll events, Static expands to content height.
- Don't nest scrollable containers — causes clipping and broken scroll regions
- Pass Rich renderables (`Text`, `Group`, `Markdown`) directly to `Static.update()`. Never render to ANSI strings — it breaks Textual's height measurement.

### Tests (pilot modules)
- Wait through `tests/support/tui_wait.py` (`wait_for`, `quiesce`, `settle`) — never a
  bare `await pilot.pause()`. The pilot's idle wait has no deadline and no idea what
  the assertion is about, so a fixed number of pauses fails under load (#1772, #2178).
- Calling one of those waits is also the switch out of the serial phase:
  `tests/conftest.py` marks every test in a pilot module `serial` unless the module
  really imports *and calls* a wait (the rule is `tests/support/serial_phase.py`, read
  off the syntax tree, so a docstring line or an unused import buys nothing), and
  `tests/test_serial_phase.py` refuses a deadline-free pump — under any binding name —
  in a module that claims it (#2342).
- Import `no_ambient_polls` from the same helper when a test sets `app.state` by hand;
  the app's 2s/30s interval polls otherwise replace that state mid-test.

### Performance
- Debounce expensive updates with `set_timer(0.15, callback)` + timer cancellation
- Cache ListView content by data snapshot — skip clear/rebuild when data unchanged
- Track widget identity with `id(widget)` to detect new screens (same data, different widget after push/pop)
- Use `_poll_local` (2s) for SQLite, `_poll_remote` (30s) for GitHub API

## Documentation Site

User-facing docs are published via MkDocs Material at **kew.dev/docs**. Source
files live in `docs/` at the repo root. Publication is **manual** — deploy with
`www/deploy.sh` from a CLEAN checkout at `main` (mkdocs builds untracked files,
which is how a never-committed page once went live). CI does not deploy: the
gh-pages mirror was unpublished on 2026-08-06 after internal working docs were
found published there, and `.github/workflows/docs.yml` is now a build gate
(`scripts/check-docs.sh`, mirrored in `scripts/verify.sh`) that fails if
anything under the `exclude_docs` list would reach a built site.

When changing code that affects user-facing behavior, update the corresponding docs:
- CLI commands (cli.py) → `docs/cli/<command>.md`
- Config fields (config.py) → `docs/configuration.md`
- Security patterns (security.py) → `docs/security.md`
- Dispatch flow changes → `docs/how-it-works.md`

Preview locally: `mkdocs serve` (from repo root)

Internal docs directory: `.kew/docs/` (gitignored). The audit, handoff and
plan-to-issues skills write there, never into `docs/`. Past audit reports, plans
and internal design notes live in the maintainers' private archive, not in this
repository; `scripts/check-docs.sh` refuses them in the public tree.
