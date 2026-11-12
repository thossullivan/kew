---
name: add-cli-command
description: Use when adding or changing a kew CLI command — Typer registration in cli.py, docs/cli pages, mkdocs nav, and CLI test placement
---

# Add a CLI Command

Code paths relative to `kew/`; docs live at REPO ROOT `docs/` (not kew/docs). Run commands from `kew/`.

## Pattern

- Register with `@app.command()` on a module-level function in `src/kew/cli.py` (`app` defined at line 88). Sub-command groups use `app.add_typer(...)` (see audit/loop/ingest/epic apps, cli.py:89–98).
- Params: `Annotated[type, typer.Option(...)]` / `typer.Argument(...)`. Model command: `review` (cli.py:1479): `load_config()` → `_preflight(...)` → `_refuse_if_fleet_stopped(config)` → work → `raise typer.Exit(rc)` on failure.

## Test first

`typer.testing.CliRunner` in `tests/test_cli.py`, or `tests/test_cli_<cmd>.py` for a large command:

```python
from unittest.mock import patch

from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig

runner = CliRunner()

@patch("kew.cli.load_config")
def test_cmd_dry_run(mock_config):
    mock_config.return_value = DispatchConfig()
    result = runner.invoke(app, ["<cmd>", "--dry-run"])
    assert result.exit_code == 0
```

## Manual registration surface (nothing is auto-generated — miss one and docs drift silently)

- [ ] `../docs/cli/<cmd>.md` — skeleton: `# kew <cmd>`, one-line description, a ```text usage block```, an **Example** bash block (copy ../docs/cli/resolve.md)
- [ ] `../mkdocs.yml`: add `- <cmd>: cli/<cmd>.md` under the CLI nav section
- [ ] `../docs/cli/index.md`: add a table row AND bump the hardcoded "N commands" count in the header

## Gates

`cli.py` is xenon-excluded today — do not use that as license for dense functions; keep command bodies thin and push logic into modules.

## Acceptance

```bash
uv run pytest tests/test_cli.py -v
uv run ruff check . && uv run ruff format --check .
uv run mypy src/
```

Then the remaining pre-commit gates — `CLAUDE.md` Conventions + Trap Gates (xenon, bandit, semgrep; becomes one `bash scripts/verify.sh` call once Epic 1 lands it).
