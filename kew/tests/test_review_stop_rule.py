"""#2112 — the review stop rule: late low/medium findings become residuals.

After round ``[review].stop_rule_after_rounds`` a NEEDS_CHANGES verdict whose
findings are all within ``[review].stop_rule_max_severity`` (#2349 — lows only,
or lows and mediums) is accepted as READY-with-residuals: one issue is filed for
the round, the PR is commented, the acceptance is attested, and the node goes to
the merge gate as a READY would. Severities above the ceiling, and unknown ones,
keep remediating; a filing failure holds (blocking) or degrades
(advisory); a re-tick never files twice; 0 disables the rule.

The filing runs inside this item's review reservation, so overlapping driver
ticks cannot double-file, and the issue is keyed on the findings themselves, so a
push that voids the acceptance does not orphan the issue it filed (PR #2122).
That key matches past the reviewer's exact prose and line numbers, and only onto
issues still open, so a residual re-reported round after round collects one
issue carrying every round that reused it rather than one issue per round
(#2152).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from support.review_stream import stream_verdict
from test_driver_review_merge import _FakeReader, _make_run, _pr

from kew.config import (
    ConfigError,
    DispatchConfig,
    LoggingConfig,
    ReviewConfig,
    load_config,
)
from kew.epic_holds import (
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    ROUNDS_EXHAUSTED_HOLD_REASON,
    merge_gate_ack_key,
    merge_gate_hold_reason,
)
from kew.graph.domain import NodeStatus
from kew.graph.driver import review_stage
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.merge import (
    REVIEW_SKIP_HEAD_MOVED,
    REVIEW_SKIP_NOT_READY,
    merge_pass,
)
from kew.graph.driver.overlay import DriverStatus, overlay_status
from kew.graph.driver.review_stage import drive_review_stage
from kew.graph.driver.tick import _review_overlay_for
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_audit import VERDICT_DEGRADED
from kew.review_contract import CollectedSidecar, Finding, ParsedVerdict
from kew.review_delta import REVIEW_MODE_MERGE_VERIFY
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_residuals import (
    READY_WITH_RESIDUALS,
    RESIDUALS_ACCEPTED_EVENT,
    ResidualFilingError,
    ResidualMatch,
    accepted_residuals,
    comment_residual_round,
    file_residual_issue,
    finding_id,
    finding_match_key,
    matching_residual_issue,
    residual_issue_body,
    residual_issue_is_open,
    stop_rule_decision,
)
from kew.run_store import ReviewRound, SqliteRunStore
from kew.subprocess_utils import run

EPIC = "epic:x"
ITEM = "42"
PR = 7
ISSUE = 901


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review-stage tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _finding(severity: str) -> Finding:
    return Finding(
        severity=severity,
        location="code.py:1",
        summary=f"{severity} thing",
        evidence_command="uv run pytest -q",
        evidence_output_digest="sha256:cafe",
    )


# --- the pure decision ------------------------------------------------------


@pytest.mark.parametrize(
    ("round_num", "severities", "expected"),
    [
        (7, ["low"], READY_WITH_RESIDUALS),
        (7, ["medium", "low"], READY_WITH_RESIDUALS),
        (7, [" Medium ", "LOW"], READY_WITH_RESIDUALS),
        (6, ["low"], "NEEDS_CHANGES"),  # "after round 6": 6 itself stays
        (1, ["low"], "NEEDS_CHANGES"),
        (12, ["low"], READY_WITH_RESIDUALS),
        (9, ["medium", "high"], "NEEDS_CHANGES"),
        (9, ["critical"], "NEEDS_CHANGES"),
        (9, ["low", "informational"], "NEEDS_CHANGES"),  # unknown → high
        (9, ["low", ""], "NEEDS_CHANGES"),  # missing → high
        (9, ["nit"], "NEEDS_CHANGES"),
        (9, [], "NEEDS_CHANGES"),  # NEEDS_CHANGES with no findings stays
    ],
)
def test_stop_rule_decision(round_num, severities, expected):
    findings = [_finding(s) for s in severities]
    decision = stop_rule_decision(
        round_num, "NEEDS_CHANGES", findings, after_rounds=6, max_severity="medium"
    )
    assert decision == expected


def test_stop_rule_decision_non_string_severity_fails_closed():
    @dataclass(frozen=True)
    class _Odd:
        severity: Any
        location: str = "a.py:1"
        summary: str = ""

    assert (
        stop_rule_decision(
            9, "NEEDS_CHANGES", [_Odd(None)], after_rounds=6, max_severity="medium"
        )
        == "NEEDS_CHANGES"
    )


@pytest.mark.parametrize("verdict", ["READY", "DEGRADED", "UNPARSEABLE"])
def test_stop_rule_decision_leaves_other_verdicts(verdict):
    assert (
        stop_rule_decision(
            9, verdict, [_finding("low")], after_rounds=6, max_severity="medium"
        )
        == verdict
    )


def test_stop_rule_decision_dropped_finding_fails_closed():
    # The parser drops a finding with no severity; the survivors are all low,
    # but the round must not pass on the finding it never saw.
    assert (
        stop_rule_decision(
            9,
            "NEEDS_CHANGES",
            [_finding("low")],
            after_rounds=6,
            max_severity="medium",
            dropped_findings=1,
        )
        == "NEEDS_CHANGES"
    )


def test_stop_rule_decision_zero_disables():
    assert (
        stop_rule_decision(
            50,
            "NEEDS_CHANGES",
            [_finding("low")],
            after_rounds=0,
            max_severity="medium",
        )
        == "NEEDS_CHANGES"
    )


def test_config_default_and_validation():
    assert ReviewConfig().stop_rule_after_rounds == 6
    assert ReviewConfig(stop_rule_after_rounds=0).stop_rule_after_rounds == 0
    with pytest.raises(ValidationError):
        ReviewConfig(stop_rule_after_rounds=-1)


# --- the configurable severity ceiling (#2349) ------------------------------


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        (["low"], READY_WITH_RESIDUALS),
        (["low", "LOW "], READY_WITH_RESIDUALS),
        (["medium"], "NEEDS_CHANGES"),
        (["low", "medium"], "NEEDS_CHANGES"),
        (["low", "high"], "NEEDS_CHANGES"),
    ],
)
def test_stop_rule_decision_low_ceiling_refuses_mediums(severities, expected):
    findings = [_finding(s) for s in severities]
    assert (
        stop_rule_decision(
            7, "NEEDS_CHANGES", findings, after_rounds=6, max_severity="low"
        )
        == expected
    )


@pytest.mark.parametrize("ceiling", ["low", "medium"])
def test_stop_rule_decision_failed_criterion_blocks_under_every_ceiling(ceiling):
    assert (
        stop_rule_decision(
            7,
            "NEEDS_CHANGES",
            [_finding("low")],
            after_rounds=6,
            max_severity=ceiling,
            failing_criteria=["AC1"],
        )
        == "NEEDS_CHANGES"
    )


@pytest.mark.parametrize("ceiling", ["low", "medium"])
@pytest.mark.parametrize("round_num", [1, 5, 6])
def test_stop_rule_decision_at_or_before_threshold_never_fires(ceiling, round_num):
    assert (
        stop_rule_decision(
            round_num,
            "NEEDS_CHANGES",
            [_finding("low")],
            after_rounds=6,
            max_severity=ceiling,
        )
        == "NEEDS_CHANGES"
    )


def test_stop_rule_decision_unreadable_ceiling_fails_closed(caplog):
    # Config validation refuses anything else at load; if one reaches the rule
    # anyway it accepts nothing, and says so rather than falling back silently.
    with caplog.at_level(logging.WARNING, logger="kew.review_residuals"):
        assert (
            stop_rule_decision(
                7, "NEEDS_CHANGES", [_finding("low")], after_rounds=6, max_severity="hi"
            )
            == "NEEDS_CHANGES"
        )
    assert "stop_rule_max_severity" in caplog.text


def test_config_severity_ceiling_default_and_validation():
    assert ReviewConfig().stop_rule_max_severity == "medium"
    assert ReviewConfig(stop_rule_max_severity="low").stop_rule_max_severity == "low"
    assert ReviewConfig(stop_rule_max_severity=" Medium ").stop_rule_max_severity == (
        "medium"
    )
    for bad in ("high", "critical", "", "none", 2):
        with pytest.raises(ValidationError):
            ReviewConfig(stop_rule_max_severity=bad)


def test_unknown_severity_ceiling_is_a_config_error_at_load(tmp_path):
    path = tmp_path / "kew.toml"
    path.write_text('[review]\nstop_rule_max_severity = "high"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "stop_rule_max_severity" in str(exc.value)


def _repo_review_config():
    """The ``[review]`` block this repo actually runs under, from the file.

    Read from the committed ``kew.toml`` rather than the environment, so the
    rule under test is the one the repo ships.
    """
    raw = tomllib.loads((Path(__file__).parents[2] / "kew.toml").read_text())
    return DispatchConfig.model_validate(raw).review


def test_repo_config_stop_rule_threshold_is_reachable_under_its_own_cap():
    """The repo's threshold must sit below its own ``max_rounds``.

    The rule fires strictly AFTER ``stop_rule_after_rounds`` and counts EVERY
    recorded round, while the driver HELDs ``review_rounds_exhausted`` once
    ``max_rounds`` REMEDIATION rounds are recorded. A threshold at or above the
    cap therefore does not behave one way: with no merge-queue round the two
    counts agree, the hold lands first, and releasing it restarts round
    numbering at 1, so the threshold round never comes round (the stop-rule
    config, ceiling included, is dead weight) — but a merge-queue round bumps
    the round number without spending a cycle, and then the same threshold
    fires one round PAST the cap and merges the round the cap was there to
    hold. Both arrangements are covered end to end below. kew.toml carried
    ``stop_rule_after_rounds = 3`` beside ``max_rounds = 2`` on the first cut of
    #2349; this is the invariant that catches that drift.
    """
    review = _repo_review_config()
    assert review.stop_rule_after_rounds > 0, "the rule is disabled, not configured"
    assert review.stop_rule_after_rounds < review.max_rounds


def test_repo_config_accepts_only_lows_from_round_two():
    # The repo's own kew.toml (#2349): the rule fires strictly after round 1,
    # and only on a round of lows.
    config = DispatchConfig(review=_repo_review_config())
    assert config.review.stop_rule_after_rounds == 1
    assert config.review.stop_rule_max_severity == "low"
    for round_num, expected in ((1, "NEEDS_CHANGES"), (2, READY_WITH_RESIDUALS)):
        assert (
            stop_rule_decision(
                round_num,
                "NEEDS_CHANGES",
                [_finding("low")],
                after_rounds=config.review.stop_rule_after_rounds,
                max_severity=config.review.stop_rule_max_severity,
            )
            == expected
        )
    assert (
        stop_rule_decision(
            2,
            "NEEDS_CHANGES",
            [_finding("medium")],
            after_rounds=config.review.stop_rule_after_rounds,
            max_severity=config.review.stop_rule_max_severity,
        )
        == "NEEDS_CHANGES"
    )


def _round(round_num: int) -> ReviewRound:
    return ReviewRound(
        epic_ref=EPIC,
        item_id=ITEM,
        round=round_num,
        head_sha="a" * 40,
        verdict="NEEDS_CHANGES",
        sidecar_digest="f" * 64,
        recorded_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("ceiling", "accepts_medium"), [("low", False), ("medium", True)]
)
def test_review_stage_decides_on_the_configured_ceiling(
    tmp_path, ceiling, accepts_medium
):
    # The stage's own re-decision reads the config, not a hard-coded ceiling.
    config = _config(tmp_path, max_severity=ceiling)
    lows = ParsedVerdict(verdict="NEEDS_CHANGES", findings=[_finding("low")])
    with_medium = ParsedVerdict(
        verdict="NEEDS_CHANGES", findings=[_finding("low"), _finding("medium")]
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:

        def accepts(parsed: ParsedVerdict) -> bool:
            # A head that touches no merge gate path (#3422).
            return review_stage._rule_accepts(
                _round(7), parsed, config, store=store, gate_paths_touched=lambda: False
            )

        assert accepts(lows)
        assert accepts(with_medium) is accepts_medium


@pytest.mark.parametrize(
    ("ceiling", "phrase"),
    [("low", "all low is accepted"), ("medium", "all low or medium is accepted")],
)
def test_residual_issue_body_names_the_configured_ceiling(ceiling, phrase):
    body = residual_issue_body(
        item_id=ITEM,
        pr_number=PR,
        round_num=7,
        head_sha="a" * 40,
        sidecar_digest=None,
        after_rounds=6,
        max_severity=ceiling,
        findings=[_finding("low")],
    )
    assert phrase in body


# --- the issue body and the gh wrapper --------------------------------------


def test_residual_issue_body_lists_each_finding():
    findings = [
        _finding("medium"),
        Finding("low", "b.py:9", "pipe | and\nnewline", "c", "d"),
    ]
    body = residual_issue_body(
        item_id=ITEM,
        pr_number=PR,
        round_num=7,
        head_sha="a" * 40,
        sidecar_digest="f" * 64,
        after_rounds=6,
        max_severity="medium",
        findings=findings,
    )
    assert "PR #7" in body and "item #42" in body
    assert "residual of the review stop rule" in body
    assert "f" * 64 in body
    assert f"| `{finding_id(findings[0])}` | medium | `code.py:1` |" in body
    assert "| low | `b.py:9` | pipe \\| and newline |" in body


@dataclass
class _Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def test_file_residual_issue_uses_bug_label_and_parses_number(monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return _Proc(stdout="https://github.com/o/r/issues/901\n")

    monkeypatch.setattr("kew.review_residuals.subprocess_utils.run", fake)
    assert file_residual_issue("o/r", title="t", body="b") == ISSUE
    [cmd] = calls
    assert cmd[:3] == ["gh", "issue", "create"]
    assert cmd[cmd.index("--label") + 1] == "bug"
    assert cmd[cmd.index("--repo") + 1] == "o/r"


@pytest.mark.parametrize(
    "proc",
    [
        _Proc(returncode=1, stderr="could not add label: 'bug' not found"),
        _Proc(stdout="not a url"),
        _Proc(stdout=""),
    ],
)
def test_file_residual_issue_failures_raise(monkeypatch, proc):
    monkeypatch.setattr(
        "kew.review_residuals.subprocess_utils.run", lambda cmd, **kw: proc
    )
    with pytest.raises(ResidualFilingError):
        file_residual_issue("o/r", title="t", body="b")


# --- the driver ---------------------------------------------------------------


def _commit(repo: Path, text: str) -> str:
    """One more commit on the checked-out branch; its sha."""
    (repo / "code.py").write_text(text)
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", text], cwd=repo, check=True)
    return run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _config(
    tmp_path: Path,
    *,
    after: int = 6,
    max_rounds: int = 12,
    blocking: bool = True,
    max_severity: str = "medium",
) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=blocking,
            max_rounds=max_rounds,
            stop_rule_after_rounds=after,
            stop_rule_max_severity=max_severity,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(head_oid: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=None,
    )


def _raw_finding(severity: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "location": "code.py:1",
        "summary": f"{severity} thing",
        "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:cafe"},
    }


def _launcher(*severities: str | None):
    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        findings = [_raw_finding(s or "") for s in severities]
        for raw, severity in zip(findings, severities, strict=True):
            if severity is None:
                del raw["severity"]
        sidecar = {"verdict": "NEEDS_CHANGES", "findings": findings}
        return stream_verdict(ctx, sidecar)

    return _launch


def _drifted_launcher(*specs: tuple[str, str]):
    """A reviewer reporting ``(severity, location)`` pairs, prose and all.

    The summary carries the location, so the same finding re-reported at a
    shifted line hashes to a different :func:`finding_id` — the drift that made
    round 9 file a duplicate of round 8's residual issue (#2152).
    """

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        findings = [
            {
                "severity": severity,
                "location": location,
                "summary": f"{severity} thing at {location}",
                "evidence": {
                    "command": "uv run pytest -q",
                    "output_digest": "sha256:cafe",
                },
            }
            for severity, location in specs
        ]
        sidecar = {"verdict": "NEEDS_CHANGES", "findings": findings}
        return stream_verdict(ctx, sidecar)

    return _launch


def _prior_rounds(store: SqliteRunStore, count: int) -> None:
    """Rounds 1..count, each a NEEDS_CHANGES on an older head."""
    for n in range(1, count + 1):
        store.record_review_round(
            EPIC, ITEM, head_sha=f"{n:040x}", verdict="NEEDS_CHANGES"
        )


def _standing_round(
    store: SqliteRunStore, tmp_path: Path, head: str, *severities: str
) -> None:
    """A recorded NEEDS_CHANGES round on *head* with a real stored sidecar."""
    data = json.dumps(
        {"verdict": "NEEDS_CHANGES", "findings": [_raw_finding(s) for s in severities]}
    ).encode()
    path = tmp_path / f"sidecar-{store.review_round_count(ITEM) + 1}.json"
    path.write_bytes(data)
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="NEEDS_CHANGES",
        sidecar_digest=hashlib.sha256(data).hexdigest(),
        sidecar_path=str(path),
    )


@dataclass
class _GitHub:
    issues: list[tuple[str, str, str]]
    comments: list[tuple[str, int | None, int, int, bool]]
    statuses: list[tuple[str, str, str]]
    issue_comments: list[tuple[int, int, bool, str]] = field(default_factory=list)
    closed: set[int] = field(default_factory=set)
    fail: bool = False


@pytest.fixture
def github(monkeypatch) -> _GitHub:
    gh = _GitHub(issues=[], comments=[], statuses=[])

    def file_issue(repo: str, *, title: str, body: str) -> int:
        if gh.fail:
            raise ResidualFilingError("gh issue create failed: HTTP 502")
        gh.issues.append((repo, title, body))
        return ISSUE + len(gh.issues) - 1

    def comment(repo, pr_number, *, issue, round_num, reused=False):
        gh.comments.append((repo, pr_number, issue, round_num, reused))

    def post(repo, sha, *, state, description):
        gh.statuses.append((sha, state, description))

    def is_open(repo: str, issue: int) -> bool:
        return issue not in gh.closed

    def comment_issue(repo, issue, *, round_num, head_sha, exact, **kwargs):
        gh.issue_comments.append((issue, round_num, exact, head_sha))

    monkeypatch.setattr(review_stage, "file_residual_issue", file_issue)
    monkeypatch.setattr(review_stage, "comment_residuals", comment)
    monkeypatch.setattr(review_stage, "residual_issue_is_open", is_open)
    monkeypatch.setattr(review_stage, "comment_residual_round", comment_issue)
    monkeypatch.setattr(review_stage.review_status, "_post", post)
    return gh


def _drive(store, config, facts, *, status, launcher=None, remediator=None, repo):
    statuses = {ITEM: status}
    out = drive_review_stage(
        statuses,
        {ITEM: facts},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=launcher,
        remediator=remediator,
    )
    return out, statuses


def _overlay(store, config, facts, *, held_reason=None) -> DriverStatus:
    review = _review_overlay_for(
        ITEM,
        facts,
        observed_status=NodeStatus.IN_REVIEW,
        config=config,
        store=store,
        review_res_kinds={},
    )
    return overlay_status(
        facts, [], reserved=False, held_reason=held_reason, review=review
    )


def _merge(store, head: str, *, approved_at="2099-01-01T00:00:00Z", reader=None):
    reader = reader or _FakeReader(pr=_pr(head_oid=head, approved_at=approved_at))
    calls, fake_run = _make_run(reader)
    [verdict] = merge_pass(
        [ITEM],
        repo="o/r",
        base="main",
        epic_ref=EPIC,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test"],
        run=fake_run,
        blocking_review=True,
    )
    return verdict, calls


def test_round7_low_medium_files_one_issue_and_proceeds_to_merge(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        out, statuses = _drive(
            store,
            config,
            facts,
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("medium", "low"),
            remediator=remediations.append,
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 7
        # The reviewer's verdict stays on record as given.
        assert latest.verdict == "NEEDS_CHANGES"
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        assert out.residuals == {ITEM: ISSUE}
        assert out.held == [] and remediations == []

        [(issue_repo, title, body)] = github.issues
        assert issue_repo == "o/r"
        assert "PR #7" in title and "round 7" in title
        assert "medium thing" in body and "low thing" in body
        assert latest.sidecar_digest in body
        assert github.comments == [("o/r", PR, ISSUE, 7, False)]
        assert github.statuses[-1][:2] == (head, "success")
        assert f"#{ISSUE}" in github.statuses[-1][2]

        [event] = store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)
        assert event.payload is not None
        assert event.payload["issue"] == ISSUE
        assert event.payload["round"] == 7
        assert event.payload["head_sha"] == head
        assert event.payload["sidecar_digest"] == latest.sidecar_digest
        assert len(event.payload["finding_ids"]) == 2
        # Both findings sit in one file, so one match key covers them (#2152).
        assert event.payload["finding_keys"] == [finding_match_key(_finding("low"))]
        assert (event.actor_type, event.actor_id) == ("policy", "epic-driver")

        # Next tick: the overlay reads READY (IN_REVIEW), and the merge gate
        # merges on a fresh approval exactly as it would a READY.
        assert _overlay(store, config, facts) == DriverStatus.IN_REVIEW
        verdict, calls = _merge(store, head)
        assert verdict.action == "merged"
        assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_acceptance_is_recorded_on_the_round_every_reader_polls(tmp_path, github):
    """#2151: the ledger, not just the chain and the PR comment, shows merge-ready.

    PR #2096 round 9 sat accepted and unmerged for ~3 hours because a watcher
    reading ``review_rounds.verdict`` saw NEEDS_CHANGES. The acceptance is now a
    fact on the round itself, so a query for merge-ready rounds returns it —
    while the reviewer's own verdict stays exactly as returned.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low", "medium"),
            repo=repo,
        )
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert latest.verdict == "NEEDS_CHANGES"
        assert latest.accepted_verdict == READY_WITH_RESIDUALS
        assert latest.effective_verdict == READY_WITH_RESIDUALS
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert (ready.round, ready.head_sha) == (7, head)


def test_a_ledger_acceptance_that_cannot_be_written_holds(
    tmp_path, github, monkeypatch, caplog
):
    """The chain fact alone is the state #2151 calls invisible, so a round the
    ledger cannot mark is not merged on: it holds, names the cleanup for an
    issue that stays on the chain, and the retry marks the round."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        accept = store.accept_review_round
        monkeypatch.setattr(store, "accept_review_round", lambda *a, **k: False)
        with caplog.at_level(logging.ERROR, logger="kew.graph.driver.review_stage"):
            out, _ = _drive(
                store,
                config,
                _facts(head),
                status=DriverStatus.ADVERSARIAL_REVIEW,
                launcher=_launcher("low"),
                repo=repo,
            )
        assert out.held == [ITEM]
        assert f"leave #{ISSUE} OPEN" in _unfiled_hold_log(caplog)
        assert store.merge_ready_review_rounds(ITEM) == []
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == "review_residuals_unfiled"

        # Unhold: the acceptance is already on the chain, so the retry files
        # nothing new and marks the round it could not mark before.
        monkeypatch.setattr(store, "accept_review_round", accept)
        store.remove_epic_hold(EPIC, ITEM)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert ready.round == 7


def test_a_ledger_acceptance_that_cannot_be_written_blocks_the_merge_gate(
    tmp_path, github
):
    """The hold must actually stop the merge, not just label the node HELD.

    The chain acceptance is already written when the mark fails, so a merge gate
    reading the chain alone merged the PR on the very tick that held the node —
    with ``kew-review`` published as failure and no merge-ready round on record.
    The gate reads both halves of the acceptance, so the half-written one waits.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(store, "accept_review_round", lambda *a, **k: False)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.held == [ITEM]
        monkeypatch.undo()
        # The acceptance IS on the chain; only the round's mark is missing.
        assert accepted_residuals(store, ITEM, store.latest_review_round(ITEM))
        assert github.statuses[-1][:2] == (head, "failure")
        verdict, calls = _merge(store, head)
        assert (verdict.action, verdict.reason) == ("skip", REVIEW_SKIP_NOT_READY)
        assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)

        # Unheld and re-driven, the retry marks the round and the merge goes.
        store.remove_epic_hold(EPIC, ITEM)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        verdict, calls = _merge(store, head)
        assert verdict.action == "merged"
        assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


@pytest.mark.parametrize("blocking", [True, False])
def test_a_failed_mark_write_leaves_the_round_for_the_next_tick(
    tmp_path, github, monkeypatch, caplog, blocking
):
    """A locked ledger costs one retry, never a fresh reviewer round (#2151).

    The mark is the one write the driver can redo by itself: the chain acceptance
    stands and the round it belongs to is still the standing one. Degrading
    (advisory) would make DEGRADED the latest round and buy the retry back with a
    full reviewer round on the same head; holding (blocking) would park it on an
    operator. So nothing moves, the merge gate holds the node meanwhile, and the
    next tick marks the round the first one accepted.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=blocking)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        accept = store.accept_review_round

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "accept_review_round", locked)
        with caplog.at_level(logging.ERROR, logger="kew.graph.driver.review_stage"):
            out, statuses = _drive(
                store,
                config,
                _facts(head),
                status=DriverStatus.ADVERSARIAL_REVIEW,
                launcher=_launcher("low"),
                repo=repo,
            )
        assert "the next tick retries the mark" in caplog.text
        # Nothing moved: no hold, no DEGRADED round, no verdict published.
        assert out.held == [] and store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        # The round stands as the reviewer returned it: nothing claims it was
        # accepted, on any surface.
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"} and out.residuals == {}
        assert github.statuses[-1][:2] == (head, "failure")
        assert store.review_round_count(ITEM) == 7
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.round, latest.verdict) == (7, "NEEDS_CHANGES")
        assert latest.accepted_verdict is None
        assert store.merge_ready_review_rounds(ITEM) == []
        # Nothing reads as merge-ready while the two records disagree.
        assert _merge(store, head)[0].action == "skip"

        # The next tick re-reads the same acceptance and marks the same round —
        # no second issue, no reviewer round (``launcher`` would raise if one ran).
        monkeypatch.setattr(store, "accept_review_round", accept)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            repo=repo,
        )
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        assert store.review_round_count(ITEM) == 7
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert (ready.round, ready.accepted_verdict) == (7, READY_WITH_RESIDUALS)


def _half_written_acceptance(store, config, repo, head, github) -> None:
    """Drive one accepting tick whose ledger mark fails: chain event, no mark.

    The state a crash between the two writes leaves — and, byte for byte, the
    state every pre-v59 accepted round is in after the upgrade: an attested
    acceptance the ledger does not carry.
    """
    _prior_rounds(store, 6)
    # Its own patcher: undoing the shared one would also undo the ``github``
    # fixture's seams, and this helper's callers assert on what they record.
    locked = pytest.MonkeyPatch()
    locked.setattr(
        store,
        "accept_review_round",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    _drive(
        store,
        config,
        _facts(head),
        status=DriverStatus.ADVERSARIAL_REVIEW,
        launcher=_launcher("low"),
        repo=repo,
    )
    locked.undo()
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.accepted_verdict is None
    assert accepted_residuals(store, ITEM, latest) is not None
    assert github.statuses[-1][:2] == (head, "failure")


def test_a_half_written_acceptance_keeps_the_node_in_the_review_lifecycle(
    tmp_path, github
):
    """The overlay must read the acceptance the MERGE GATE reads (#2151).

    Deriving it from the chain event alone placed a half-written acceptance at
    READY — which the overlay reports as IN_REVIEW, a status the review stage
    does not drive — while the gate refused the node for the missing mark. So
    nothing retried the mark and nothing merged: the node wedged permanently and
    silently, and so did every pre-v59 accepted-but-unmerged node after the
    upgrade. Both readings are now ``acceptance_on_record``, so the node stays in
    the lifecycle until the mark lands.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _half_written_acceptance(store, config, repo, head, github)
        # Not merge-ready, so not IN_REVIEW: the node is still the review
        # stage's to drive, and that is what retries the mark.
        assert _overlay(store, config, facts) == DriverStatus.REMEDIATING
        assert _merge(store, head)[0].reason == REVIEW_SKIP_NOT_READY

        # Driven from the status the overlay itself computed — not a hand-picked
        # one — the retry marks the round, files no second issue and dispatches
        # no remediation.
        out, _ = _drive(
            store,
            config,
            facts,
            status=_overlay(store, config, facts),
            remediator=remediations.append,
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert (remediations, out.unwired, out.held) == ([], [], [])
        assert len(github.issues) == 1
        assert github.statuses[-1][:2] == (head, "success")

        # Both halves on record: READY to the overlay, mergeable to the gate.
        assert _overlay(store, config, facts) == DriverStatus.IN_REVIEW
        verdict, calls = _merge(store, head)
        assert verdict.action == "merged"
        assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_an_attested_acceptance_is_marked_without_its_sidecar(tmp_path, github):
    """The retry belongs to the attested acceptance, not to the sidecar (#2151).

    A pre-v59 round is unmarked with a sidecar long since rotated away, and the
    mark can fail on a tick whose sidecar is deleted before the next one. Re-
    deciding the stop rule against a sidecar the driver can no longer read would
    refuse to mark either — remediating (or converging to a review hold) a round
    already accepted on the chain. So the acceptance is still marked, and no
    review round is re-run.

    The merge, though, cannot read what that round's reviewer could not run
    (#3421): the sidecar that listed it is gone, and it may have named Docker.
    It is held as ``merge_gate_touched`` naming ``other``, and a person's
    release of that exact head lets it merge.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _half_written_acceptance(store, config, repo, head, github)
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.sidecar_path
        Path(latest.sidecar_path).unlink()
        # The rule cannot be re-decided, but the acceptance is already attested.
        assert review_stage._standing_verdict(latest) is None

        out, statuses = _drive(
            store,
            config,
            facts,
            status=_overlay(store, config, facts),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert statuses[ITEM] != DriverStatus.HELD and store.list_epic_holds(EPIC) == []
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert (ready.round, ready.accepted_verdict) == (7, READY_WITH_RESIDUALS)

        held, calls = _merge(store, head)
        reason = merge_gate_hold_reason(head, "review sandbox could not run: other")
        assert (held.action, held.reason) == ("held", reason)
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [reason]
        assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
        assert store.latest_review_round(ITEM).round == 7, "no round re-run"

        released = store.remove_epic_hold(
            EPIC,
            ITEM,
            receipt_key=merge_gate_ack_key(EPIC, ITEM),
            receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
        )
        assert released is not None
        assert _merge(store, head)[0].action == "merged"


def _red_ci(head: str) -> _FakeReader:
    """An approved PR whose required checks are not green: the merge pass holds
    it ``ci_pending``, the sticky hold the overlay reports as HELD."""
    return _FakeReader(
        pr=_pr(
            head_oid=head,
            approved_at="2099-01-01T00:00:00Z",
            merge_state_status="BLOCKED",
        )
    )


def test_a_half_written_acceptance_under_a_hold_is_still_finished(tmp_path, github):
    """A HELD node's mark must be retried by something (#2151).

    A hold outranks the review lifecycle at overlay precedence 1, and the review
    stage drives ADVERSARIAL_REVIEW/REMEDIATING only — so an unmarked acceptance
    on a node also carrying (say) ``ci_pending`` had no retry at all: the merge
    gate refused it for the missing mark BEFORE ``_classify_open`` reached the CI
    check that releases the hold, and the stage skipped it for being HELD. The
    node wedged until an operator unheld it, where the same state merged on the
    build before this one. The stage now finishes the mark on a held node too:
    ledger write plus status, no issue filed, no agent dispatched, hold intact.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _half_written_acceptance(store, config, repo, head, github)
        # The hold the merge pass writes for an approved PR with red CI — the
        # state a pre-v59 acceptance is in after the upgrade, its mark absent
        # because the column did not exist when the round was accepted.
        store.hold_epic_node(EPIC, ITEM, "ci_pending")
        assert _overlay(store, config, facts, held_reason="ci_pending") is (
            DriverStatus.HELD
        )
        # The gate refuses before the CI branch, so nothing else in the merge
        # pass can clear the hold that keeps the stage away from the node.
        assert _merge(store, head, reader=_red_ci(head))[0].reason == (
            REVIEW_SKIP_NOT_READY
        )

        out, statuses = _drive(
            store,
            config,
            facts,
            status=DriverStatus.HELD,
            remediator=remediations.append,
            repo=repo,
        )
        # The one write it owes the node: the mark, and the round's status.
        assert out.residuals == {ITEM: ISSUE}
        assert (out.dispatched, remediations, out.held) == ([], [], [])
        assert len(github.issues) == 1
        assert github.statuses[-1][:2] == (head, "success")
        # The hold is untouched — it is CI's to clear, not the stage's.
        assert [h.reason for h in store.list_epic_holds(EPIC)] == ["ci_pending"]
        assert statuses[ITEM] is DriverStatus.HELD
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert (ready.round, ready.accepted_verdict) == (7, READY_WITH_RESIDUALS)

        # Both readers agree again, so the held node merges once CI goes green —
        # and the merge clears the hold, as it always did.
        verdict, calls = _merge(store, head)
        assert verdict.action == "merged"
        assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
        assert store.list_epic_holds(EPIC) == []


def test_the_held_retry_marks_nothing_that_was_not_accepted(tmp_path, github):
    """The held-node retry finishes an ATTESTED acceptance and nothing else.

    It runs outside the lifecycle, before the stop rule is re-decided for any
    node, so it must key on the chain event alone: a held node whose standing
    round was never accepted has no acceptance to finish, and marking one there
    would merge a NEEDS_CHANGES round nobody accepted.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _standing_round(store, tmp_path, head, "high")
        store.hold_epic_node(EPIC, ITEM, "ci_pending")

        out, statuses = _drive(
            store, config, _facts(head), status=DriverStatus.HELD, repo=repo
        )
        assert (out.residuals, out.verdicts, github.issues) == ({}, {}, [])
        assert statuses[ITEM] is DriverStatus.HELD
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.accepted_verdict is None
        assert store.merge_ready_review_rounds(ITEM) == []
        assert _merge(store, head, reader=_red_ci(head))[0].reason == (
            REVIEW_SKIP_NOT_READY
        )


def test_acceptance_keeps_the_other_merge_conditions(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        # CI human gate (#2033) still applies.
        reader = _FakeReader(pr=_pr(head_oid=head, approved_at="2099-01-01T00:00:00Z"))
        reader.head_checks = lambda sha, gated: ("awaiting_approval", "awaiting")  # type: ignore[method-assign]
        verdict, calls = _merge(store, head, reader=reader)
        assert verdict.action == "skip"
        assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)
        # A push after the acceptance voids it, as it voids a READY.
        moved = "b" * 40
        verdict, calls = _merge(store, moved)
        assert verdict.reason == REVIEW_SKIP_HEAD_MOVED
        assert _overlay(store, config, _facts(moved)) != DriverStatus.IN_REVIEW
        # And the approval must postdate the verdict.
        verdict, _ = _merge(store, head, approved_at="2000-01-01T00:00:00Z")
        assert verdict.action == "skip"


def test_round6_boundary_stays_in_remediation(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 5)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert store.latest_review_round(ITEM).round == 6  # type: ignore[union-attr]
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"}
        assert github.issues == []
        # The standing round 6 remediates on the next tick.
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert len(remediations) == 1 and out.remediating == [ITEM]
        assert github.issues == []
        assert _overlay(store, config, _facts(head)) == DriverStatus.REMEDIATING


@pytest.mark.parametrize(
    "severities",
    [("low", "high"), ("critical",), ("medium", "informational"), ("low", " ")],
)
def test_blocking_or_unknown_severity_at_round9_keeps_remediating(
    tmp_path, github, severities
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 8)
        _standing_round(store, tmp_path, head, *severities)
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert github.issues == []
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        # " " is dropped by the parser (no severity) and still fails closed.
        assert len(remediations) == 1 and out.remediating == [ITEM]
        assert statuses[ITEM] == DriverStatus.REMEDIATING


@pytest.mark.parametrize("second", ["nit", None])
def test_unknown_or_missing_severity_from_reviewer_fails_closed(
    tmp_path, github, second
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 7)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low", second),
            repo=repo,
        )
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"}
        assert github.issues == []
        assert _overlay(store, config, _facts(head)) == DriverStatus.REMEDIATING


def test_filing_failure_holds_and_does_not_merge(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    github.fail = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == "review_residuals_unfiled"
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        assert github.comments == []
        assert github.statuses[-1][:2] == (head, "failure")
        assert (
            overlay_status(
                _facts(head),
                [],
                reserved=False,
                held_reason=hold.reason,
                review=None,
            )
            == DriverStatus.HELD
        )
        verdict, calls = _merge(store, head)
        assert verdict.action == "skip"
        assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)

        # Unhold after fixing the cause: the next tick retries and files once.
        github.fail = False
        store.remove_epic_hold(EPIC, ITEM)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1


def _refuse_acceptance(store, monkeypatch):
    """Make the acceptance event write fail; returns the undo for that one patch.

    Not ``monkeypatch.undo()`` — that would also unwind the ``github`` fixture's
    patches, which the tick after the restore still needs.
    """
    append_once = store.append_audit_event_once

    def refuse(**kwargs):
        if kwargs["event"] == RESIDUALS_ACCEPTED_EVENT:
            raise OSError("disk full")
        return append_once(**kwargs)

    monkeypatch.setattr(store, "append_audit_event_once", refuse)
    return lambda: monkeypatch.setattr(store, "append_audit_event_once", append_once)


def _unfiled_hold_log(caplog) -> str:
    """The one HELD review_residuals_unfiled line, which names the cleanup."""
    [message] = [
        record.getMessage()
        for record in caplog.records
        if "review_residuals_unfiled" in record.getMessage()
    ]
    return message


def test_unrecorded_acceptance_holds_even_after_filing(
    tmp_path, github, monkeypatch, caplog
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _refuse_acceptance(store, monkeypatch)
        with caplog.at_level(logging.ERROR, logger="kew.graph.driver.review_stage"):
            out, _ = _drive(
                store,
                config,
                _facts(head),
                status=DriverStatus.ADVERSARIAL_REVIEW,
                launcher=_launcher("low"),
                repo=repo,
            )
        assert out.held == [ITEM]
        assert len(github.issues) == 1
        # This issue's only acceptance is the write that just failed, so nothing
        # on the chain names it: the operator closes it and the retry re-files.
        assert f"close #{ISSUE} before you unhold" in _unfiled_hold_log(caplog)
        verdict, _ = _merge(store, head)
        assert verdict.action == "skip"


def test_unrecorded_acceptance_on_a_reused_issue_says_to_leave_it_open(
    tmp_path, github, monkeypatch, caplog
):
    """A REUSED issue is on the chain, so the documented close would break it.

    The fresh-filing cleanup (close the issue, the retry files a new one) is the
    exact opposite of what a reused issue needs: an earlier accepted round has it
    on the chain and the retry reuses it again, so closing it would close the
    live residual tracker the node later merges pointing at. The hold diagnostic
    therefore names the cleanup that applies instead of assuming a fresh filing.
    """
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        repo, head1 = _make_repo(tmp_path)
        _prior_rounds(store, 6)
        out, _ = _drive(
            store,
            config,
            _facts(head1),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}

        # A push voids that acceptance. Round 8 re-reports the same finding, so
        # the filing reuses #ISSUE — but this round's acceptance cannot be written.
        head2 = _commit(repo, "x = 2\n")
        restore = _refuse_acceptance(store, monkeypatch)
        with caplog.at_level(logging.ERROR, logger="kew.graph.driver.review_stage"):
            out, _ = _drive(
                store,
                config,
                _facts(head2),
                status=DriverStatus.ADVERSARIAL_REVIEW,
                launcher=_launcher("low"),
                repo=repo,
            )
        assert out.held == [ITEM]
        assert len(github.issues) == 1
        message = _unfiled_hold_log(caplog)
        assert f"leave #{ISSUE} OPEN" in message
        assert "close" not in message.lower()
        # Round 7's acceptance still names it, and the retry reuses it: the
        # issue the operator was told to close is the live tracker.
        [kept] = store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)
        assert kept.payload is not None and kept.payload["issue"] == ISSUE
        restore()
        store.remove_epic_hold(EPIC, ITEM)
        out, _ = _drive(
            store,
            config,
            _facts(head2),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1


def test_retick_does_not_file_twice(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        # A re-tick that still sees the standing round (e.g. a stale overlay).
        for _ in range(2):
            out, _ = _drive(
                store,
                config,
                _facts(head),
                status=DriverStatus.REMEDIATING,
                remediator=lambda ctx: pytest.fail("must not remediate"),
                repo=repo,
            )
            assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        assert len(github.comments) == 1
        assert len(store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)) == 1


def test_standing_round_with_tampered_sidecar_stays_remediating(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 7)
        _standing_round(store, tmp_path, head, "high")
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.sidecar_path is not None
        # Rewrite the stored findings to low after the digest was attested.
        Path(latest.sidecar_path).write_text(
            json.dumps({"verdict": "NEEDS_CHANGES", "findings": [_raw_finding("low")]})
        )
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert github.issues == []
        assert len(remediations) == 1


def test_setting_zero_disables(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, after=0)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 9)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"}
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert github.issues == []
        assert len(remediations) == 1


def test_stop_rule_runs_before_the_round_cap(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, max_rounds=7)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _standing_round(store, tmp_path, head, "medium")
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.held == []
        assert out.residuals == {ITEM: ISSUE}
        assert statuses[ITEM] == DriverStatus.REMEDIATING


def test_repo_config_fires_on_the_last_round_before_its_cap(tmp_path, github):
    """This repo's committed stop rule reaches a round, end to end (#2349).

    The scenario is built from ``max_rounds``, not from the threshold: in a
    cycle of reviewer rounds only, round ``max_rounds`` is the LAST round
    reached before the driver HELDs `review_rounds_exhausted`, so if the rule
    does not decide that round it decides none. An all-low round there has to
    merge with its residuals filed, not hold — which is what a threshold raised
    to the cap does in this same reviewer-only cycle.
    """
    review = _repo_review_config()
    repo, head = _make_repo(tmp_path)
    config = _config(
        tmp_path,
        after=review.stop_rule_after_rounds,
        max_rounds=review.max_rounds,
        max_severity=review.stop_rule_max_severity,
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, review.max_rounds - 1)
        _standing_round(store, tmp_path, head, "low")
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.held == []
        assert out.residuals == {ITEM: ISSUE}


def test_a_threshold_at_the_round_cap_is_dead_in_a_reviewer_only_cycle(
    tmp_path, github
):
    """A cycle of reviewer rounds alone never reaches the threshold (#2349).

    Same all-low round, same cap, threshold moved up to the cap: the node HELDs
    `review_rounds_exhausted` with nothing filed. Releasing that hold restarts
    round numbering at 1, so the threshold round never comes round either.

    This is ONE arrangement, not the general case: round numbers count every
    recorded round while the cap counts only remediation cycles, so a cycle
    carrying a merge-queue round reaches the same threshold and accepts the
    round instead —
    :func:`test_a_merge_queue_round_carries_the_threshold_past_the_cap`. Both
    are why the repo's threshold has to stay BELOW `max_rounds`.
    """
    review = _repo_review_config()
    repo, head = _make_repo(tmp_path)
    config = _config(
        tmp_path,
        after=review.max_rounds,
        max_rounds=review.max_rounds,
        max_severity=review.stop_rule_max_severity,
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, review.max_rounds - 1)
        _standing_round(store, tmp_path, head, "low")
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        assert out.residuals == {}
        assert github.issues == []
        assert out.held == [ITEM]
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            ROUNDS_EXHAUSTED_HOLD_REASON
        ]


def _merge_queue_round(store: SqliteRunStore, head_sha: str) -> None:
    """A recorded merge-queue round: a round number, but not a cycle (#2148)."""
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head_sha,
        verdict="NEEDS_CHANGES",
        review_mode=REVIEW_MODE_MERGE_VERIFY,
    )


@pytest.mark.parametrize("after", [2, 0])
def test_a_merge_queue_round_carries_the_threshold_past_the_cap(
    tmp_path, github, after
):
    """``stop_rule_after_rounds == max_rounds`` still fires, one round late.

    The cycle is a reviewer round, a merge-queue round (#2148 — a round number
    the remediation cap deliberately does not spend), then the remediated
    reviewer round. That makes the standing round number 3 while only 2
    remediation cycles are spent, so a threshold of 2 — the cap itself — IS
    passed, and the stop rule runs before the cap: the all-low round merges with
    its residuals filed rather than HOLDing `review_rounds_exhausted`.

    ``after=0`` is the same cycle with the rule disabled, and pins that the cap
    is genuinely spent here: without the rule this exact arrangement HELDs. So
    the threshold at the cap is not inert — it converts that hold into a merge.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, after=after, max_rounds=2, max_severity="low")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 1)
        _merge_queue_round(store, f"{2:040x}")
        _standing_round(store, tmp_path, head, "low")
        assert store.review_round_count(ITEM) == 3
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=lambda ctx: None,
            repo=repo,
        )
        if after == 0:  # the rule disabled: the cap is what decides the round
            assert out.residuals == {}
            assert github.issues == []
            assert out.held == [ITEM]
            assert [h.reason for h in store.list_epic_holds(EPIC)] == [
                ROUNDS_EXHAUSTED_HOLD_REASON
            ]
            return
        assert out.held == []
        assert out.residuals == {ITEM: ISSUE}
        assert [title for _, title, _ in github.issues] == [
            f"Review residuals: PR #{PR} round 3"
        ]


def test_acceptance_does_not_carry_to_a_later_round(tmp_path, github):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        accepted = store.latest_review_round(ITEM)
        assert accepted_residuals(store, ITEM, accepted) is not None
        store.record_review_round(EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES")
        assert accepted_residuals(store, ITEM, store.latest_review_round(ITEM)) is None


# --- composition with delta re-review (#2111) --------------------------------


def _delta_cycle(tmp_path: Path, store: SqliteRunStore) -> tuple[Path, str]:
    """Round 1 full on c1, rounds 2-6 delta on c2; returns (repo, c3).

    No full round sits in the last two, so a delta READY next becomes
    NEEDS_FULL, and c2 is an ancestor of c3, so round 7 runs as a delta.
    """
    from kew.review_delta import REVIEW_MODE_DELTA, REVIEW_MODE_FULL

    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, c1 = _make_repo(tmp_path)
    heads = []
    for n in (2, 3):
        (repo / "code.py").write_text(f"x = {n}\n")
        run(["git", "add", "-A"], cwd=repo, check=True)
        run(["git", "commit", "-q", "-m", f"c{n}"], cwd=repo, check=True)
        heads.append(
            run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
        )
    c2, c3 = heads
    for n in range(1, 7):
        data = json.dumps(
            {"verdict": "NEEDS_CHANGES", "findings": [_raw_finding("high")]}
        ).encode()
        path = tmp_path / f"delta-seed-{n}.json"
        path.write_bytes(data)
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=c1 if n == 1 else c2,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(data).hexdigest(),
            sidecar_path=str(path),
            review_mode=REVIEW_MODE_FULL if n == 1 else REVIEW_MODE_DELTA,
        )
    return repo, c3


def test_stop_rule_composes_with_delta_rounds(tmp_path, github):
    from kew.review_delta import REVIEW_MODE_DELTA, VERDICT_NEEDS_FULL

    config = _config(tmp_path)
    # A delta round 7 returning NEEDS_CHANGES with only low findings is accepted.
    with SqliteRunStore(str(tmp_path / "a.db")) as store:
        repo, c3 = _delta_cycle(tmp_path / "a", store)
        out, _ = _drive(
            store,
            config,
            _facts(c3),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 7
        assert latest.review_mode == REVIEW_MODE_DELTA
        assert latest.verdict == "NEEDS_CHANGES"
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        assert _overlay(store, config, _facts(c3)) == DriverStatus.IN_REVIEW

    # A delta READY turned NEEDS_FULL at round 7 is never the stop rule's.
    with SqliteRunStore(str(tmp_path / "b.db")) as store:
        repo, c3 = _delta_cycle(tmp_path / "b", store)

        def ready(ctx: ReviewerLaunchContext) -> CollectedSidecar:
            return stream_verdict(
                ctx,
                {
                    "verdict": "READY",
                    "findings": [],
                    "acceptance_evidence": [
                        {"command": "uv run pytest -q", "output_digest": "sha256:e"}
                    ],
                },
            )

        out, _ = _drive(
            store,
            config,
            _facts(c3),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=ready,
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 7
        assert latest.review_mode == REVIEW_MODE_DELTA
        assert latest.verdict == VERDICT_NEEDS_FULL
        assert out.verdicts == {ITEM: VERDICT_NEEDS_FULL}
        assert out.residuals == {}
        assert len(github.issues) == 1  # only the first scenario's
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        assert accepted_residuals(store, ITEM, latest) is None
        assert _overlay(store, config, _facts(c3)) == DriverStatus.ADVERSARIAL_REVIEW
        verdict, _ = _merge(store, c3)
        assert verdict.action == "skip"


# --- PR #2122 review: filing is reserved, content-keyed, and blocking-aware ---


def test_two_overlapping_ticks_file_one_residual_issue(tmp_path, github, monkeypatch):
    """`gh issue create` is not idempotent, so the check-then-act is reserved.

    Two overlapping driver ticks used to each file an issue for the same round
    while the acceptance dedup key admitted only one — an orphan bug issue plus a
    PR comment naming an issue the chain does not.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    db = str(tmp_path / "d.db")
    filing = threading.Event()
    proceed = threading.Event()
    lock = threading.Lock()
    inner = review_stage.file_residual_issue

    def file_issue(issue_repo: str, *, title: str, body: str) -> int:
        with lock:
            issue = inner(issue_repo, title=title, body=body)
            first = len(github.issues) == 1
        if first:  # hold the first tick inside the filing, mid-critical-section
            filing.set()
            assert proceed.wait(timeout=30)
        return issue

    monkeypatch.setattr(review_stage, "file_residual_issue", file_issue)
    with SqliteRunStore(db) as seed:
        _prior_rounds(seed, 6)
        _standing_round(seed, tmp_path, head, "low")

    errors: list[BaseException] = []

    def tick() -> None:
        try:
            with SqliteRunStore(db) as store:
                _drive(
                    store,
                    config,
                    _facts(head),
                    status=DriverStatus.REMEDIATING,
                    remediator=lambda ctx: None,
                    repo=repo,
                )
        except BaseException as exc:  # noqa: BLE001 — re-asserted on the main thread
            errors.append(exc)

    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert filing.wait(timeout=30), "the first tick never reached the filing"
        # The overlapping tick, on its own handle to the same ledger.
        with SqliteRunStore(db) as other:
            out, _ = _drive(
                other,
                config,
                _facts(head),
                status=DriverStatus.REMEDIATING,
                remediator=lambda ctx: None,
                repo=repo,
            )
        assert out.residuals == {}
    finally:
        proceed.set()
        worker.join(timeout=30)
    assert errors == []
    assert len(github.issues) == 1
    assert len(github.comments) == 1
    with SqliteRunStore(db) as store:
        assert len(store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)) == 1
        # The winner released what it took; no reservation leaks past the tick.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_filing_defers_to_a_foreign_reservation(tmp_path, github):
    """A tick that built its due list before a peer reserved files nothing."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _standing_round(store, tmp_path, head, "low")
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert store.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, "peer-run")
        assert (
            review_stage._file_residuals_once(
                ITEM,
                _facts(head),
                latest,
                [_finding("low")],
                reserved=False,
                config=config,
                store=store,
                repo="o/r",
            )
            is None
        )
        assert github.issues == []
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        # The loser never releases a reservation it does not own (#975 class).
        [reservation] = store.list_epic_reservations(REVIEW_EPIC_REF)
        assert reservation.run_id == "peer-run"


def test_advisory_filing_failure_degrades_instead_of_holding(tmp_path, github):
    """Advisory review never turns a transient gh error into a durable hold.

    ``_converge`` and ``_handle_unparseable`` both attest DEGRADED and proceed
    when review is non-blocking; the stop rule's filing failure must too, or the
    advisory on-ramp acquires a hold only ``kew epic unhold`` clears.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False)
    github.fail = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.verdicts == {ITEM: VERDICT_DEGRADED}
        assert out.residuals == {}
        # No acceptance was recorded, so nothing reads as READY on this head.
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == VERDICT_DEGRADED
        sha, state, description = github.statuses[-1]
        assert (sha, state) == (head, "success")
        assert "DEGRADED (advisory)" in description
        # Not wedged: DEGRADED is neither READY nor NEEDS_CHANGES, so the next
        # tick reviews the head again instead of waiting on an operator.
        assert _overlay(store, config, _facts(head)) == DriverStatus.IN_REVIEW


def test_blocking_filing_failure_still_holds(tmp_path, github):
    """The blocking default is unchanged by the advisory branch."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    github.fail = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.held == [ITEM] and statuses[ITEM] == DriverStatus.HELD
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == "review_residuals_unfiled"


def test_push_after_acceptance_reuses_the_residual_issue(tmp_path, github):
    """A push voids the acceptance, not the findings — so not the issue either.

    Acceptance binds to a round and head, so the next round re-reports the same
    unfixed low/medium findings. Filing a second issue for them would orphan the
    first; ``finding_ids`` is the content key the acceptance already records.
    """
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        repo, head1 = _make_repo(tmp_path)
        _prior_rounds(store, 6)
        out, _ = _drive(
            store,
            config,
            _facts(head1),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert _overlay(store, config, _facts(head1)) == DriverStatus.IN_REVIEW

        # Someone pushes. The acceptance is void: the driver dispatches a fresh
        # reviewer round, so the overlay must say review — not remediating.
        head2 = _commit(repo, "x = 2\n")
        assert _overlay(store, config, _facts(head2)) == DriverStatus.ADVERSARIAL_REVIEW
        verdict, _ = _merge(store, head2)
        assert verdict.action == "skip"
        assert verdict.reason == REVIEW_SKIP_HEAD_MOVED

        # Round 8 re-reports the same finding: one issue, two bound acceptances.
        out, _ = _drive(
            store,
            config,
            _facts(head2),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 8
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        assert accepted_residuals(store, ITEM, latest) is not None
        assert len(store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)) == 2
        # The second PR comment says "still tracked", not "filed".
        assert github.comments[-1] == ("o/r", PR, ISSUE, 8, True)
        assert _overlay(store, config, _facts(head2)) == DriverStatus.IN_REVIEW
        verdict, _ = _merge(store, head2)
        assert verdict.action == "merged"

        # A residual somewhere ELSE is a different residual: a fresh issue.
        head3 = _commit(repo, "x = 3\n")
        out, _ = _drive(
            store,
            config,
            _facts(head3),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_drifted_launcher(("low", "other.py:4")),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE + 1}
        assert len(github.issues) == 2
        assert github.comments[-1] == ("o/r", PR, ISSUE + 1, 9, False)


def test_matching_residual_issue_matches_on_content(tmp_path, github):
    """The content key ignores round and head; a different residual misses it."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _prior_rounds(store, 6)
        _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_launcher("low"),
            repo=repo,
        )
        findings = [_finding("low")]
        assert matching_residual_issue(store, ITEM, findings) == ResidualMatch(
            issue=ISSUE, exact=True
        )
        assert matching_residual_issue(store, ITEM, []) is None
        assert matching_residual_issue(store, "99", findings) is None
        # Same file, changed prose or extra findings: the same residual issue,
        # matched past the exact id (#2152) — a different file is its own.
        grew = [*findings, _finding("medium")]
        assert matching_residual_issue(store, ITEM, grew) == ResidualMatch(
            issue=ISSUE, exact=False
        )
        elsewhere = [Finding("low", "other.py:3", "s", "c", "d")]
        assert matching_residual_issue(store, ITEM, elsewhere) is None


def test_a_reworded_finding_after_a_merge_reuses_the_open_issue(tmp_path, github):
    """#2152: the same residual re-reported is one issue, not one per round.

    Round 7 filed the issue; round 8, after a merge, re-raised the same finding
    at a shifted line with reworded prose. The exact finding-id key missed it and
    a duplicate was filed and closed by hand. The match now falls back to the
    finding's file, and the reused issue records the round that reused it.
    """
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        repo, head1 = _make_repo(tmp_path)
        _prior_rounds(store, 6)
        out, _ = _drive(
            store,
            config,
            _facts(head1),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_drifted_launcher(("low", "join.py:41")),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE}
        assert github.issue_comments == []

        head2 = _commit(repo, "x = 2\n")
        out, _ = _drive(
            store,
            config,
            _facts(head2),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_drifted_launcher(("low", "join.py:57")),
            repo=repo,
        )
        # One issue, two accepted rounds, and both rounds named on the issue.
        assert out.residuals == {ITEM: ISSUE}
        assert len(github.issues) == 1
        assert "round 7" in github.issues[0][2]
        assert github.issue_comments == [(ISSUE, 8, False, head2)]
        assert github.comments[-1] == ("o/r", PR, ISSUE, 8, True)
        assert len(store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT)) == 2
        verdict, _ = _merge(store, head2)
        assert verdict.action == "merged"


def test_a_closed_residual_issue_is_not_reused(tmp_path, github):
    """A residual tracker somebody closed cannot silently absorb a live one."""
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        repo, head1 = _make_repo(tmp_path)
        _prior_rounds(store, 6)
        _drive(
            store,
            config,
            _facts(head1),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_drifted_launcher(("low", "join.py:41")),
            repo=repo,
        )
        github.closed.add(ISSUE)

        head2 = _commit(repo, "x = 2\n")
        out, _ = _drive(
            store,
            config,
            _facts(head2),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_drifted_launcher(("low", "join.py:41")),
            repo=repo,
        )
        assert out.residuals == {ITEM: ISSUE + 1}
        assert len(github.issues) == 2
        assert github.issue_comments == []
        assert github.comments[-1] == ("o/r", PR, ISSUE + 1, 8, False)


@pytest.mark.parametrize(
    ("proc", "expected"),
    [
        (_Proc(stdout='{"state":"OPEN"}'), True),
        (_Proc(stdout='{"state":"open"}'), True),
        (_Proc(stdout='{"state":"CLOSED"}'), False),
        (_Proc(returncode=1, stderr="HTTP 502"), False),
        (_Proc(stdout="not json"), False),
        (_Proc(stdout="{}"), False),
    ],
)
def test_residual_issue_is_open_reads_gh_and_fails_closed(monkeypatch, proc, expected):
    """An undeterminable state is not open: a fresh issue beats a lost finding."""
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return proc

    monkeypatch.setattr("kew.review_residuals.subprocess_utils.run", fake)
    assert residual_issue_is_open("o/r", ISSUE) is expected
    [cmd] = calls
    assert cmd[:3] == ["gh", "issue", "view"]
    assert cmd[cmd.index("--repo") + 1] == "o/r"


def test_residual_issue_is_open_survives_a_gh_crash(monkeypatch, caplog):
    def boom(cmd, **kwargs):
        raise OSError("gh: not found")

    monkeypatch.setattr("kew.review_residuals.subprocess_utils.run", boom)
    with caplog.at_level(logging.WARNING):
        assert residual_issue_is_open("o/r", ISSUE) is False
    assert "gh: not found" in caplog.text


def test_comment_residual_round_names_the_round_and_is_best_effort(monkeypatch):
    """The reused issue gets the round; a failed comment never fails the tick."""
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return _Proc(returncode=1, stderr="HTTP 502")

    monkeypatch.setattr("kew.review_residuals.subprocess_utils.run", fake)
    comment_residual_round(
        "o/r",
        ISSUE,
        item_id=ITEM,
        pr_number=PR,
        round_num=8,
        head_sha="b" * 40,
        sidecar_digest="f" * 64,
        findings=[_finding("low")],
        exact=False,
    )
    [cmd] = calls
    body = cmd[-1]
    assert cmd[:4] == ["gh", "api", "-X", "POST"]
    assert f"repos/o/r/issues/{ISSUE}/comments" in cmd
    assert "Round 8" in body and "PR #7" in body
    assert "rather than opening a second one" in body
    assert f"| `{finding_id(_finding('low'))}` | low | `code.py:1` |" in body


# --- #2291: a failed acceptance criterion is never a residual ----------------


def _plan_with_one_criterion(
    store: SqliteRunStore, kind: str = "behavior"
) -> tuple[str, str]:
    """Record a one-criterion plan for ITEM; its ``(plan_sha, criteria_json)``."""
    from kew.triage.plan import canonical_plan_json, plan_sha
    from kew.triage.schema import AcceptanceCriterion, ItemPlan

    plan = ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC1", statement="the flag turns the thing off", kind=kind
            )
        ]
    )
    sha = plan_sha(plan)
    store.record_item_plan(
        ITEM,
        body_digest=hashlib.sha256(b"body").hexdigest(),
        source="extracted",
        plan_sha=sha,
        plan_json=canonical_plan_json(plan),
        criteria_count=1,
    )
    criteria_json = json.dumps(
        [c.model_dump(mode="json") for c in plan.acceptance_criteria],
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha, criteria_json


def _failed_criterion_sidecar() -> dict[str, Any]:
    """A low-severity NEEDS_CHANGES that also reports AC1 as ``fail``.

    Admissible to the parser: the failing criterion is named by an evidenced
    finding, and the verdict is NEEDS_CHANGES. Every severity is residual, so
    the stop rule's severity test alone would accept the round.
    """
    finding = _raw_finding("low") | {"criterion": "AC1"}
    return {
        "verdict": "NEEDS_CHANGES",
        "findings": [finding],
        "criteria_results": [
            {"id": "AC1", "result": "fail", "reason": "the flag does nothing"}
        ],
    }


def _criterion_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    assert ctx.criterion_ids == ("AC1",)
    return stream_verdict(ctx, _failed_criterion_sidecar())


def test_stop_rule_decision_failed_criterion_is_not_residual():
    # Every severity is residual and the round is past the cutoff, so only the
    # failed criterion can keep it in remediation.
    assert (
        stop_rule_decision(
            9,
            "NEEDS_CHANGES",
            [_finding("low"), _finding("medium")],
            after_rounds=6,
            max_severity="medium",
            failing_criteria=["AC1"],
        )
        == "NEEDS_CHANGES"
    )


def test_a_round_that_failed_a_recorded_criterion_is_not_accepted(tmp_path, github):
    """#2291: "ready except for what it was asked to do" is not a residual.

    The reviewer's own round says low — but it also says the item does not do
    what it was recorded as being for, which no severity makes acceptable.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _plan_with_one_criterion(store)
        _prior_rounds(store, 6)
        out, statuses = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_criterion_launcher,
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 7
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"}
        assert out.residuals == {} and github.issues == []
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        assert latest.accepted_verdict is None

        # And the next tick remediates it rather than accepting it off the
        # standing round, where the sidecar is re-parsed from persisted state.
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert github.issues == []
        assert len(remediations) == 1
        assert remediations[0].criterion_ids == ("AC1",)


def test_a_standing_round_is_re_parsed_against_its_recorded_criteria(tmp_path, github):
    """The standing path re-reads the sidecar; without the round's recorded ids
    every ``criteria_results`` entry is dropped and the failed criterion
    vanishes, so the rule would accept the round on severity alone."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _, criteria_json = _plan_with_one_criterion(store)
        _prior_rounds(store, 7)
        data = json.dumps(_failed_criterion_sidecar()).encode()
        path = tmp_path / "standing-sidecar.json"
        path.write_bytes(data)
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(data).hexdigest(),
            sidecar_path=str(path),
            plan_sha="deadbeef",
            criteria_json=criteria_json,
        )
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert out.residuals == {} and github.issues == []
        assert len(remediations) == 1


# --- #2732: a cited failing contract test is a failed criterion -------------


def _failed_contract_test_sidecar() -> dict[str, Any]:
    """A low-severity NEEDS_CHANGES whose reviewer calls AC1 ``pass`` while
    citing its contract test ``failed``.

    The parser records AC1 as ``fail`` on the contract test's word, and the
    evidenced finding names it, so the round is admissible. Only a parse that
    binds AC1 to its contract test sees the failure; read without that, the
    round is residual-only and the stop rule would accept it.
    """
    finding = _raw_finding("low") | {"criterion": "AC1"}
    return {
        "verdict": "NEEDS_CHANGES",
        "findings": [finding],
        "criteria_results": [
            {
                "id": "AC1",
                "result": "pass",
                "evidence": {"command": "uv run pytest", "output_digest": "x"},
                "contract_test": {"name": "test_ac1", "outcome": "failed"},
            }
        ],
    }


def _contract_test_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    assert ctx.contract_test_ids == ("AC1",)
    return stream_verdict(ctx, _failed_contract_test_sidecar())


def test_a_cited_failing_contract_test_blocks_residual_acceptance(tmp_path, github):
    """The round that cites its contract test failing stays in remediation —
    on the round it lands in, and on the next tick that re-parses the stored
    sidecar from the round's recorded criteria."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _plan_with_one_criterion(store, kind="test")
        _prior_rounds(store, 6)
        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.ADVERSARIAL_REVIEW,
            launcher=_contract_test_launcher,
            repo=repo,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 7
        assert out.verdicts == {ITEM: "NEEDS_CHANGES"}
        assert out.residuals == {} and github.issues == []
        assert latest.accepted_verdict is None

        out, _ = _drive(
            store,
            config,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            remediator=remediations.append,
            repo=repo,
        )
        assert out.residuals == {} and github.issues == []
        assert store.list_audit_events(event=RESIDUALS_ACCEPTED_EVENT) == []
        assert len(remediations) == 1
