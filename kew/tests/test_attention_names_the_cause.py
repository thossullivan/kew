"""An attention event names the hold's own cause, not its bucket's fixed hint.

Three holds from #2682 used to tell the operator the wrong thing: an empty
runner credential was told to map a container image, and a diff the
publication filter held on a protected path was told to restore GitHub access.
Each case is built as ledger rows and read through ``summarize`` and
``attention_events``, the same path ``kew epic status`` and the drive take.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, NOW, FakeFactsReader, FakeSource, _facts, _node

import kew.dispatch_core  # noqa: F401 - imported before dispatch_preflight, its cycle
import kew.sandbox
from kew.audit import Actor, emit
from kew.cli.epic import _stall_dominant
from kew.config import SandboxConfig
from kew.dispatch_preflight import _containment_hold_reason
from kew.epic_holds import admission_refused_reason_prefix
from kew.graph.build import build_graph
from kew.graph.driver.attention import AttentionEvent, attention_events
from kew.graph.driver.tick import StuckNode, summarize
from kew.run_store import RunRecord
from kew.runner_quota import (
    RUNNER_QUOTA_HELD_PREFIX,
    QuotaRejection,
    record_runner_quota_hold,
)
from kew.sandbox import (
    REFUSAL_CREDENTIAL_MISSING,
    REFUSAL_IMAGE_UNMAPPED,
    SandboxCredentialUndeclared,
    SandboxProxyUnparseable,
    SandboxRuntimeUnavailable,
    _require_containable_runner,
    _require_parseable_inherited_proxies,
    check_host_capacity,
)
from kew.waits import classify_wait

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

HEAD = "0123456789abcdef0123456789abcdef01234567"


def _events(store: Any) -> tuple[AttentionEvent, ...]:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_fx._config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        now=NOW,
    )
    return attention_events(EPIC, summary)


def _misdirected(event: AttentionEvent) -> bool:
    action = event.action.lower()
    return "image" in action or "github access" in action


def test_an_empty_credential_hold_names_the_credential_not_an_image(
    store: Any,
) -> None:
    refusal = SandboxRuntimeUnavailable(
        "Sandbox: no claude credential in the environment — containers cannot "
        "inherit CLI login. Run `claude setup-token` to dispatch sandboxed.",
        reason=REFUSAL_CREDENTIAL_MISSING,
    )
    store.hold_epic_node(EPIC, 1, _containment_hold_reason("1", refusal))

    [event] = _events(store)

    assert "credential" in event.action
    assert not _misdirected(event)


def test_an_unparseable_proxy_hold_names_the_proxy_setting_not_a_credential(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its message mentions credentials, but the fix is the proxy variable."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@[::1")
    with pytest.raises(SandboxProxyUnparseable) as refused:
        _require_parseable_inherited_proxies(["HTTPS_PROXY"])
    assert "credentials" in str(refused.value)
    store.hold_epic_node(EPIC, 1, _containment_hold_reason("1", refused.value))

    [event] = _events(store)

    assert "proxy_environment" in event.action
    assert "credential" not in event.action
    assert not _misdirected(event)


def test_an_undeclared_credential_hold_names_the_runner_declaration(
    store: Any,
) -> None:
    """The host environment cannot fix a runner that declares no credential."""
    config = SandboxConfig()
    config.images = {"lemonade": "kew-lemonade:latest"}
    with pytest.raises(SandboxCredentialUndeclared) as refused:
        _require_containable_runner(config, "lemonade", runtime="docker")
    store.hold_epic_node(EPIC, 1, _containment_hold_reason("1", refused.value))

    [event] = _events(store)

    assert "required_credentials" in event.action
    assert "host environment" not in event.action
    assert not _misdirected(event)


def test_only_an_unmapped_image_is_told_to_map_an_image() -> None:
    tokens = {
        value
        for name, value in vars(kew.sandbox).items()
        if name.startswith("REFUSAL_") and isinstance(value, str)
    }
    assert REFUSAL_IMAGE_UNMAPPED in tokens

    for token in tokens:
        refusal = SandboxRuntimeUnavailable("Sandbox: refused", reason=token)
        wait = classify_wait(_containment_hold_reason("1", refusal), held=True)
        assert wait is not None
        maps_image = "images.<runner>" in wait.action
        assert maps_image is (token == REFUSAL_IMAGE_UNMAPPED), token


def test_the_stall_summary_names_each_containment_cause_not_the_image(
    tmp_path: Path,
) -> None:
    """``kew epic status``'s dominant-cause line reads the same per-cause hint."""
    credential = SandboxRuntimeUnavailable(
        "Sandbox: no claude credential in the environment",
        reason=REFUSAL_CREDENTIAL_MISSING,
    )
    with pytest.raises(SandboxRuntimeUnavailable) as full:
        check_host_capacity(tmp_path, free_floor=2**62)
    waiting = (
        StuckNode("1", _containment_hold_reason("1", credential), ()),
        StuckNode("2", _containment_hold_reason("2", full.value), ()),
    )

    line = _stall_dominant((), waiting)

    assert line is not None
    assert line.startswith("2 held on containment — ")
    assert "credential" in line
    assert "free disk space" in line
    assert "images.<runner>" not in line


def test_an_unmapped_image_hold_still_names_the_image_mapping(store: Any) -> None:
    store.hold_epic_node(
        EPIC,
        1,
        "sandbox_containment: Sandbox: runner 'codex' has no container image "
        "mapped, so it cannot be contained",
    )

    [event] = _events(store)

    assert "images.<runner>" in event.action


def test_a_runner_quota_wait_sends_nobody_to_images_or_github(store: Any) -> None:
    """Case 2 (fixed by #2643): the lane waits out the provider's reset, so the
    node is a wait with the quota as its reason, and no event misdirects."""
    record_runner_quota_hold(
        store,
        QuotaRejection("claude-code", "seven_day", datetime.now(UTC) + timedelta(1)),
    )

    events = _events(store)

    assert not [e for e in events if _misdirected(e)]
    for event in events:
        assert event.reason.startswith(RUNNER_QUOTA_HELD_PREFIX)


def test_a_protected_path_hold_names_the_path_not_github_access(
    store: Any,
) -> None:
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_review_required",
        )
    )
    emit(
        store,
        "publication_deferred",
        actor=Actor("policy", "host-publication"),
        run_id="run-1",
        issue_num=1,
        item_id="1",
        payload={
            "branch": "agent/issue-1",
            "category": "publication_review_required",
            "step": "push",
            "error": "publication needs human review — .github/workflows/ci.yml: "
            "CI workflow code, which runs with repository secrets",
            "worktree_preserved": True,
        },
    )

    [event] = _events(store)

    assert ".github/workflows/ci.yml" in event.reason
    assert "protected path .github/workflows/ci.yml" in event.action
    assert not _misdirected(event)


def test_a_refused_head_hold_names_the_head(store: Any) -> None:
    store.hold_epic_node(
        EPIC,
        1,
        f"{admission_refused_reason_prefix(HEAD)}: remediation admission refused",
    )

    [event] = _events(store)

    assert HEAD[:12] in event.action
    assert "kew epic unhold" in event.action


def test_a_publication_hold_on_the_pr_text_names_no_protected_path(
    store: Any,
) -> None:
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_review_required",
        )
    )
    emit(
        store,
        "publication_deferred",
        actor=Actor("policy", "host-publication"),
        run_id="run-1",
        issue_num=1,
        item_id="1",
        payload={
            "category": "publication_review_required",
            "error": "publication needs human review — pr_body_too_long: 70000",
        },
    )

    [event] = _events(store)

    assert "protected path" not in event.reason
    assert "protected path" not in event.action
    assert not _misdirected(event)
