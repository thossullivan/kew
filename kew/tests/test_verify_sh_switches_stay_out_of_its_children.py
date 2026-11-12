"""verify.sh's switches decide what verify.sh runs, and nothing it starts.

``kew verify`` starts the script as ``KEW_VERIFY_STAGE=suite bash
scripts/verify.sh``, which exports the switch to every child. The pytest
phases are children, and some tests run verify.sh again: a switch that
reached them made that nested run skip its own gates. The real script runs
here with its tools stubbed; the stubbed ``uv run pytest`` records the
environment it was given and, once, runs verify.sh itself.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from support.fake_gcloud import launchable
from support.verify_switches import VERIFY_SWITCHES, environ_without_switches

_KEW_DIR = Path(__file__).resolve().parent.parent
_VERIFY_SH = _KEW_DIR / "scripts" / "verify.sh"

#: Exit status of the stubbed xdist phase, so a run that reached it says so.
_XDIST_EXIT = 86

_UV_STUB = """#!/bin/sh
depth="${KEW_STUB_DEPTH:-0}"
echo "$depth uv $*" >> "$KEW_STUB_LOG"
case "$*" in
  *pytest*)
    if [ "$depth" = 0 ]; then
      env | grep -E '^(SWITCHES)=' >> "$KEW_STUB_ENV_LOG"
      if [ ! -e "$KEW_STUB_NESTED" ]; then
        : > "$KEW_STUB_NESTED"
        KEW_STUB_DEPTH=1 bash "$KEW_STUB_VERIFY_SH" >/dev/null 2>&1
        echo "$?" > "$KEW_STUB_NESTED"
      fi
    fi
    ;;
esac
case "$*" in *"-n auto"*) exit 86;; esac
exit 0
""".replace("SWITCHES", "|".join(VERIFY_SWITCHES))


@dataclass(frozen=True)
class _Seen:
    """What one stubbed verify.sh run did.

    ``log`` holds every stubbed ``uv`` call, prefixed ``0`` for the outer run
    and ``1`` for the nested one; ``child_env`` holds every switch a pytest
    the outer run started was given; ``nested`` is the nested run's status.
    """

    code: int
    stderr: str
    log: str
    child_env: str
    nested: str


def _run_verify_sh(tmp_path: Path, switches: dict[str, str]) -> _Seen:
    root = tmp_path if launchable(tmp_path) else Path(os.environ["HOME"])
    bin_dir = Path(root) / f"switch-stub-{os.getpid()}-{id(tmp_path)}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "uv").write_text(_UV_STUB, encoding="utf-8")
    for tool in ("uvx", "gitleaks", "npm"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for entry in bin_dir.iterdir():
        entry.chmod(0o755)
    log = tmp_path / "stub.log"
    env_log = tmp_path / "env.log"
    nested = tmp_path / "nested.status"
    for path in (log, env_log):
        path.write_text("", encoding="utf-8")
    environment = environ_without_switches()
    environment.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        KEW_STUB_LOG=str(log),
        KEW_STUB_ENV_LOG=str(env_log),
        KEW_STUB_NESTED=str(nested),
        KEW_STUB_VERIFY_SH=str(_VERIFY_SH),
    )
    environment.update(switches)
    completed = subprocess.run(
        ["bash", str(_VERIFY_SH)],
        cwd=_KEW_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return _Seen(
        code=completed.returncode,
        stderr=completed.stderr,
        log=log.read_text(encoding="utf-8"),
        child_env=env_log.read_text(encoding="utf-8"),
        nested=nested.read_text(encoding="utf-8").strip() if nested.exists() else "",
    )


@pytest.mark.parametrize(
    ("switches", "outer_code", "outer_skipped"),
    [
        (
            {
                "KEW_VERIFY_STAGE": "suite",
                "KEW_VERIFY_FRONTEND": "full",
                "KEW_VERIFY_IMAGE_SCAN": "skip",
            },
            _XDIST_EXIT,
            "0 uv run ruff check .",
        ),
        (
            {
                "KEW_VERIFY_STAGE": "gates",
                "KEW_VERIFY_SUITE": "selected",
                "KEW_VERIFY_FRONTEND": "full",
            },
            0,
            "0 uv run pytest -n auto",
        ),
    ],
    ids=["suite-stage", "gates-stage-selected-suite"],
)
def test_no_switch_reaches_a_pytest_phase_and_a_nested_run_runs_every_gate(
    tmp_path: Path, switches: dict[str, str], outer_code: int, outer_skipped: str
) -> None:
    seen = _run_verify_sh(tmp_path, switches)

    assert seen.code == outer_code, seen
    assert outer_skipped not in seen.log, "verify.sh ignored its own switch"
    assert seen.child_env == "", (
        f"a switch reached a pytest verify.sh started: {seen.child_env!r}"
    )
    assert seen.nested == str(_XDIST_EXIT), (
        f"the nested verify.sh did not reach the xdist phase: {seen.log}"
    )
    assert "1 uv run ruff check ." in seen.log, "the nested run skipped its gates"
    assert "1 uv run pytest tests/test_sandbox_containment.py -v" in seen.log
