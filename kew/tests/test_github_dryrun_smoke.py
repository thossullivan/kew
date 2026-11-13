"""Group A9 exit gate — byte-identical GitHub dry-run smoke vs origin/main.

Slice A′ threads an opaque ``item_id`` through the dispatch surfaces so Linear
ids survive. The invariant the sweep MUST preserve: a GitHub-numeric id slugs
and renders to *exactly* what the pre-``item_id`` code produced — the driver's
GitHub path is unchanged to the byte. These goldens are the observable
dry-run outputs on origin/main (HEAD is at origin/main as of this slice); the
test locks them so the deferred Task-8 consumer sweep — or any future edit —
cannot silently perturb the GitHub surface.

Three dry-run surfaces, per the plan (``test_resolve`` / ``test_scheduler`` /
``test_epic_plan_run`` fixtures): the prompt/branch render, the scheduler
``[DRY RUN]`` line, and the resolve dry-run report.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest

from kew.config import DispatchConfig, ParallelConfig
from kew.ids import branch_slug, closes_marker, item_ref
from kew.prompt import build_prompt, build_template_context
from kew.resolve import dispatch_resolve
from kew.scheduler import DispatchScheduler

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The H7 untrusted-content sentinel is intentionally random per render, so it is
# orthogonal to the item_id threading invariant these goldens lock. Normalize it
# to a constant before comparing legacy vs threaded renders.
_SENTINEL = re.compile(r"<<<KEW-UNTRUSTED-[0-9a-f]{32}>>>")


def _plain(text: str) -> str:
    """Drop any ANSI styling typer may emit so line matches are byte-stable."""
    return _ANSI.sub("", text)


def _norm_sentinel(text: str) -> str:
    """Replace the per-render random sentinel with a fixed placeholder."""
    return _SENTINEL.sub("<<<KEW-UNTRUSTED-SENTINEL>>>", text)


def _ctx(item_id: str | None) -> dict[str, Any]:
    return build_template_context(
        issue_num=42,
        issue_title="Add a widget",
        issue_body="Body text.",
        complexity="medium",
        config=DispatchConfig(),
        has_coderabbit=False,
        item_id=item_id,
        provider="github",
    )


def test_id_rendering_helpers_are_stable() -> None:
    # The GitHub-numeric render — the exact strings origin/main produced.
    assert branch_slug("42") == "42"
    assert item_ref("42") == "#42"
    assert closes_marker("42") == "Closes #42"


def _ctx_norm(item_id: str | None) -> dict[str, Any]:
    ctx = _ctx(item_id)
    # Neutralize the per-render random sentinel (H7): it varies regardless of
    # item_id, so it is not part of the threading invariant under test.
    ctx["sentinel"] = "<<<KEW-UNTRUSTED-SENTINEL>>>"
    ctx["issue_body_fenced"] = _norm_sentinel(ctx["issue_body_fenced"])
    return ctx


def test_template_context_github_id_is_byte_identical_to_legacy() -> None:
    # Threading item_id="42" must yield the identical context dict to the
    # legacy (item_id=None) path — the whole render, not just the branch.
    assert _ctx_norm("42") == _ctx_norm(None)

    ctx = _ctx("42")
    config = DispatchConfig()
    assert ctx["branch_name"] == f"{config.branches.prefix}42"
    assert ctx["branch_name"] == "agent/issue-42"
    assert ctx["commands"]["commit_prefix"] == "feat(#42): "


def test_full_prompt_render_github_id_is_byte_identical_to_legacy() -> None:
    kwargs: dict[str, Any] = dict(
        issue_num=42,
        issue_title="Add a widget",
        issue_body="Body text.",
        complexity="medium",
        config=DispatchConfig(),
        has_coderabbit=False,
    )
    legacy = build_prompt(**kwargs)
    swept = build_prompt(**kwargs, item_id="42", provider="github")
    # byte-for-byte identical prompt, modulo the per-render random sentinel (H7)
    assert _norm_sentinel(swept) == _norm_sentinel(legacy)


def test_scheduler_dry_run_line_is_byte_identical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
    scheduler = DispatchScheduler(config)
    issue: dict[str, Any] = {
        "number": 42,
        "title": "Add a widget",
        "labels": [],
        "body": "",
    }

    handled = scheduler._handle_preview(issue, show_prompt=False, dry_run=True)

    assert handled is True
    err = _plain(capsys.readouterr().err)
    assert "[DRY RUN] Would dispatch: #42 — Add a widget" in err


@patch("kew.resolve._check_github_connectivity", return_value=True)
@patch("kew.resolve._get_pr_metadata")
def test_resolve_dry_run_report_is_byte_identical(
    mock_meta: Any, _mock_conn: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_meta.return_value = {
        "headRefName": "agent/issue-42",
        "baseRefName": "main",
        "title": "feat: x",
        "body": "",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
    }

    rc = dispatch_resolve(pr_number=42, config=DispatchConfig(), dry_run=True)

    assert getattr(rc, "kind", None) == "executed"
    assert rc.exit_code == 0
    err = _plain(capsys.readouterr().err)
    assert "PR #42 has mergeable state: CONFLICTING" in err
    assert "Branch: agent/issue-42 → main" in err
    assert "Would dispatch agent to resolve conflicts." in err
