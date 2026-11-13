import ast
import pathlib
import shutil
import subprocess

import pytest

FORBIDDEN = {
    "kew.run_store",
    "kew.orchestrator",
    "kew.audit",
    "kew.approval",
    "kew.fleet",
}


@pytest.mark.policy
def test_serve_does_not_import_forbidden_modules():
    serve_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "kew" / "serve"
    offenders = []
    for py in serve_dir.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]  # every alias, not just [0]
            else:
                continue
            for mod in mods:
                if any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN):
                    offenders.append((py.name, mod))
    assert offenders == [], f"kew.serve must not import {FORBIDDEN}: {offenders}"


def test_lint_imports_clean():
    """import-linter's declared contracts (pyproject.toml) must all pass.

    This is the same gate wired into CI and scripts/verify.sh; mirroring it in
    pytest makes a boundary break a red test too. The AST test above catches a
    *direct* serve import; this catches the full contract set (including the
    serve/service -> kew.graph JSON-only crossing). Skips — not fails — when the
    binary is absent, matching the guard-test pattern for optional binaries.
    """
    if shutil.which("lint-imports") is None:
        pytest.skip("lint-imports binary not available")
    kew_dir = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
        ["lint-imports"],
        cwd=kew_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "import-linter contract violated — kew.serve must read runs through "
        f"kew.service, not import run_store/orchestrator/audit directly.\n"
        f"{result.stdout}\n{result.stderr}"
    )
