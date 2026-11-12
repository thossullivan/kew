"""The mutation report runs after the merge, off the verify's critical path (#2817).

A gating ``kew verify`` runs no mutation phase and reports ``mutation=deferred``;
the report is its own job (:func:`run_mutation_report`) against the merged
commit and its diff from the main it was verified on, recorded as its own
event. The host is the real local provider with only the job's execution
stubbed — what the job *is* is asserted on the command it was handed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.held_out_log import opened

from kew.config import DispatchConfig, VerifyConfig
from kew.hosts import Job, LocalProvider, host_lock_dir
from kew.hosts.lock import host_lock
from kew.merge_verify import (
    MUTATION_CAP_SECONDS,
    MUTATION_EVENT,
    VERIFY_EVENT,
    mutation_comment,
    run_mutation_report,
    run_verify,
    summary_line,
)

store = _fx.store

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)

_SURVIVORS_LOG = "\n".join(
    [
        "kew-mutation: selected 3 mutants in 1 files against 1 test files",
        "kew-mutation: survived src/kew/widget.py:12 kew.widget.x__mutmut_4 '-' -> '+'",
        "kew-mutation: survived src/kew/widget.py:14 kew.widget.x__mutmut_7 "
        "'<' -> '<='",
        "kew-mutation: done exit=0 capped=no cap=600s seconds=42 selected=3 "
        "killed=1 survived=2 not_checked=0 other=0",
    ]
)


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _project(root: Path) -> tuple[Path, str, str]:
    """A repo whose HEAD is one commit ahead of ``main``: (repo, base, head)."""
    repo = root / "repo"
    (repo / "kew" / "src" / "kew").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main", ".")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--quiet", "-b", "candidate")
    (repo / "kew" / "src" / "kew" / "widget.py").write_text("x = 1\n", "utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "candidate")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(host="local")
    return config


def _host_answers(
    monkeypatch: pytest.MonkeyPatch, log: str, shipped: dict[str, Any]
) -> None:
    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        shipped["command"] = job.command
        shipped["name"] = job.name
        shipped["head"] = _git(self.checkout, "rev-parse", "HEAD")
        return 0, log

    monkeypatch.setattr(LocalProvider, "_execute", _execute)


def test_a_gating_verify_runs_no_mutation_phase_and_reports_it_deferred(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _head = _project(tmp_path)
    shipped: dict[str, Any] = {}
    # Even a log that carries mutation lines is not read as the phase's report.
    log = "gate passed\n" + _HELD_OUT_PASSED + _SURVIVORS_LOG
    _host_answers(monkeypatch, log, shipped)

    candidate, result = run_verify(
        _config(tmp_path), store=store, ref="HEAD", repo_root=repo, host="local"
    )

    assert "mutmut-sampler" not in shipped["command"]
    assert result.outcome == "passed"
    assert result.mutation is not None
    assert result.mutation.outcome == "deferred"
    assert summary_line(candidate, result).endswith(" mutation=deferred")
    (event,) = store.list_audit_events(limit=None, event=VERIFY_EVENT)
    assert event.payload["mutation"]["outcome"] == "deferred"
    assert event.payload["mutation"]["survivors"] == []


def test_the_post_merge_job_records_survivors_per_file_line_and_mutation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, merged = _project(tmp_path)
    shipped: dict[str, Any] = {}
    _host_answers(monkeypatch, _SURVIVORS_LOG, shipped)

    report = run_mutation_report(
        _config(tmp_path),
        store=store,
        merged_sha=merged,
        base_sha=base,
        pr_number=7,
        repo_root=repo,
    )

    # Its own job, on a clone of the merged commit, scoped to the merged diff.
    assert shipped["name"] == "mutation-report"
    assert shipped["head"] == merged
    assert f"--diff-scope {base} --cap {MUTATION_CAP_SECONDS}" in shipped["command"]
    assert "verify.sh" not in shipped["command"]
    assert report is not None and report.outcome == "completed"
    (event,) = store.list_audit_events(limit=None, event=MUTATION_EVENT)
    payload = event.payload
    assert (payload["pr"], payload["ref_sha"], payload["base_sha"]) == (
        7,
        merged,
        base,
    )
    assert [
        (s["path"], s["line"], s["mutation"]) for s in payload["mutation"]["survivors"]
    ] == [
        ("src/kew/widget.py", 12, "'-' -> '+'"),
        ("src/kew/widget.py", 14, "'<' -> '<='"),
    ]
    body = mutation_comment(merged, report)
    assert "survived=2/3" in body
    assert "| `src/kew/widget.py` | 12 | `'-' -> '+'` |" in body
    assert "| `src/kew/widget.py` | 14 | `'<' -> '<='` |" in body


def test_a_report_job_yields_a_host_another_job_holds(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, merged = _project(tmp_path)
    shipped: dict[str, Any] = {}
    _host_answers(monkeypatch, _SURVIVORS_LOG, shipped)

    with host_lock(host_lock_dir(), "local"):
        report = run_mutation_report(
            _config(tmp_path),
            store=store,
            merged_sha=merged,
            base_sha=base,
            pr_number=7,
            repo_root=repo,
        )

    assert report is None
    assert shipped == {}
    assert store.list_audit_events(limit=None, event=MUTATION_EVENT) == []
