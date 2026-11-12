"""A round that rejects only undeclared test edits asks for a declaration (#2872).

On #2834 the reviewer agreed two edited tests were justified and rejected only
that no **Behavior changes** line named them. Two remediations then ended with
no commit and no declaration, and the drive re-ran them at the same head until
the inbox flagged the loop. These pin the three halves of the fix: the brief
names each test to declare, a no-change remediation after such a round holds
the node, and the lines a remediation's report declares reach the PR body.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from support.driver_fakes import EPIC
from support.held_out_log import report_line
from support.review_stream import capture_of, reviewer_stream, stream_verdict

from kew import test_integrity as integrity
from kew.behavior_declarations import (
    carry_declared_lines,
    load_declaration_request,
    record_flagged_tests,
    requested_lines,
    undeclared,
    with_declared_lines,
)
from kew.config import DispatchConfig, LoggingConfig, ProjectConfig, ReviewConfig
from kew.epic_holds import (
    DECLARATION_RECORD_FAILED_HOLD_PREFIX,
    HOLD_REASON_MAX_CHARS,
    PR_BODY_UNREADABLE_HOLD_PREFIX,
    TEST_DECLARATIONS_HOLD_PREFIX,
    declarations_missing_hold_reason,
    holds_node_out_of_frontier,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.merge_verify import declared_behavior_changes, held_out_report
from kew.pr_text import unescape_text, with_proposed_lines
from kew.prompt import build_prompt
from kew.review import _ReviewDispatch, build_review_prompt
from kew.review_contract import collect_stream_verdict
from kew.review_integrity import read_integrity_report
from kew.run_store import (
    REVIEW_REMEDIATION_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

ITEM = "2811"
HEAD = "c912990f"
PR = 2834
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
GCLOUD = (
    "tests/test_hosts_providers.py::TestGcloud::"
    "test_every_gcloud_call_runs_in_that_environment"
)
CEILING = (
    "tests/test_hosts_providers.py::TestGcloud::"
    "test_subprocess_runs_ceiling_returns_but_leaves_the_child"
)
REVIEWER_REASON = "justified by the new env contract, but the PR body names neither"


def _report(*node_ids: str) -> integrity.TestIntegrityReport:
    edits = [
        integrity.Evidence(
            "tests/test_hosts_providers.py", n, f"{node_id} is edited and ..."
        )
        for n, node_id in enumerate(node_ids, start=10)
    ]
    return integrity.with_undeclared_test_edits(integrity.TestIntegrityReport(), edits)


def _finding(
    summary: str,
    location: str = "tests/test_hosts_providers.py:10",
    *,
    declare: tuple[str, ...] = (),
) -> Any:
    """A sidecar finding; with *declare*, the reviewer files it as
    ``kind: "declaration"`` asking for exactly those node ids."""
    finding: dict[str, Any] = {
        "severity": "medium",
        "location": location,
        "summary": summary,
        "evidence": {"command": "uv run pytest ...", "output_digest": "2 failed"},
    }
    if declare:
        finding.update(kind="declaration", node_ids=list(declare))
    return finding


def _sidecar(
    tmp_path: Path,
    *findings: dict[str, Any],
    rejected: tuple[str, ...] = (integrity.FLAG_UNDECLARED_TEST_EDITS,),
    flagged: tuple[str, ...] = (GCLOUD, CEILING),
    name: str = "round-5",
) -> Path:
    """A stored NEEDS_CHANGES sidecar plus the flagged tests the host recorded."""
    path = tmp_path / "logs" / f"{name}.sidecar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": list(findings),
                "test_changes": [
                    {"flag": flag, "answer": "REJECTED", "reason": REVIEWER_REASON}
                    for flag in rejected
                ],
            }
        )
    )
    record_flagged_tests(path, _report(*flagged))
    return path


def _declaration_sidecar(
    tmp_path: Path,
    flagged: tuple[str, ...] = (GCLOUD, CEILING),
    name: str = "round-5",
) -> Path:
    return _sidecar(
        tmp_path,
        _finding(
            f"{' and '.join(flagged)} fail against the head and no Behavior "
            "changes line names them",
            declare=flagged,
        ),
        flagged=flagged,
        name=name,
    )


def test_a_declaration_only_round_briefs_each_node_id_and_asks_for_no_code(
    tmp_path: Path,
) -> None:
    request = load_declaration_request(str(_declaration_sidecar(tmp_path)))
    assert request is not None

    prompt = build_review_prompt(
        pr_number=PR,
        issue_number=int(ITEM),
        branch_name="agent/issue-2811",
        comments=[],
        config=DispatchConfig(),
        governed=True,
        declaration=request,
    )

    assert "## Declare these tests — no code change" in prompt
    assert f"- `{GCLOUD}`" in prompt
    assert f"- `{CEILING}`" in prompt
    assert "Tests to declare (2)" in prompt
    assert REVIEWER_REASON in prompt
    assert "**Behavior changes**" in prompt


@pytest.mark.parametrize(
    ("findings", "rejected"),
    [
        pytest.param(
            (_finding("the retry loop never backs off", "src/kew/hosts.py:40"),),
            (integrity.FLAG_UNDECLARED_TEST_EDITS,),
            id="a-code-finding-too",
        ),
        pytest.param(
            (_finding(f"{GCLOUD} is undeclared", declare=(GCLOUD,)),),
            (integrity.FLAG_UNDECLARED_TEST_EDITS, "asserts_removed"),
            id="another-flag-rejected",
        ),
        pytest.param(
            (_finding(f"{GCLOUD} is undeclared", declare=(GCLOUD,)),),
            (),
            id="nothing-rejected",
        ),
    ],
)
def test_a_round_that_asks_for_more_than_a_declaration_is_not_one(
    tmp_path: Path, findings: tuple[dict[str, Any], ...], rejected: tuple[str, ...]
) -> None:
    path = _sidecar(tmp_path, *findings, rejected=rejected)

    assert load_declaration_request(str(path)) is None


def test_a_round_with_no_recorded_flagged_tests_is_not_one(tmp_path: Path) -> None:
    path = _declaration_sidecar(tmp_path)
    Path(f"{path}.declarations").unlink()

    assert load_declaration_request(str(path)) is None


class _Forge:
    """The two PR-body calls kew makes: a REST read and a REST PATCH."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.patches = 0
        self.unreadable = False
        #: Someone else's edit, landing right after each of kew's writes.
        self.after_patch: Any = None

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["gh", "api"]
        assert f"repos/o/r/pulls/{PR}" in cmd
        if "PATCH" in cmd:
            self.patches += 1
            self.body = cmd[cmd.index("-f") + 1].removeprefix("body=")
            if self.after_patch is not None:
                self.body = self.after_patch(self.body)
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if self.unreadable:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="502")
        payload = json.dumps({"body": self.body})
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")


def _forge(body: str) -> Any:
    forge = _Forge(body)
    return forge, (
        patch("kew.review_integrity.run", forge),
        patch("kew.behavior_declarations.run", forge),
    )


def test_declared_lines_in_a_remediation_report_reach_the_pr_body(
    tmp_path: Path,
) -> None:
    request = load_declaration_request(str(_declaration_sidecar(tmp_path)))
    assert request is not None
    report = (
        "## Dispatch Report\n\n### What was done\n- declared the edits\n\n"
        "### Behavior changes\n"
        f"- {GCLOUD} — every gcloud call now runs in the configured env\n"
        f"- {CEILING} — the ceiling now kills the child\n"
        "- tests/test_unrelated.py::test_other — not asked for\n\n"
        "### Known gaps\n- none\n"
    )
    body = "Closes #2811\n\n**Behavior changes**\n\n- None\n\n**Testing**\n- ok\n"
    forge, patches = _forge(body)

    lines = requested_lines(request, report)
    with patches[0], patches[1]:
        assert carry_declared_lines("o/r", PR, lines)

    assert declared_behavior_changes(forge.body) == {GCLOUD, CEILING}
    assert forge.body.startswith("Closes #2811\n")
    assert "**Testing**\n- ok" in forge.body
    assert "test_other" not in forge.body

    with patches[0], patches[1]:
        assert carry_declared_lines("o/r", PR, lines)
    assert forge.patches == 1, "lines the body already declares are not re-added"


def test_a_body_with_no_behavior_changes_section_gets_one(tmp_path: Path) -> None:
    forge, patches = _forge("Closes #2811\n")

    with patches[0], patches[1]:
        assert carry_declared_lines("o/r", PR, [f"- {GCLOUD} — new env contract"])

    assert declared_behavior_changes(forge.body) == {GCLOUD}
    assert forge.body.startswith("Closes #2811\n")


def test_a_concurrent_edit_that_drops_the_lines_is_written_into_once_more(
    tmp_path: Path,
) -> None:
    """The second write goes into the latest body, keeping the other edit."""
    forge, patches = _forge("Closes #2811\n")
    edits = iter(["Closes #2811\n\nAlso fixes #9\n"])
    forge.after_patch = lambda body: next(edits, body)

    with patches[0], patches[1]:
        assert carry_declared_lines("o/r", PR, [f"- {GCLOUD} — new env contract"])

    assert forge.patches == 2
    assert declared_behavior_changes(forge.body) == {GCLOUD}
    assert "Also fixes #9" in forge.body


def test_a_write_the_body_never_confirms_is_not_reported_carried(
    tmp_path: Path,
) -> None:
    forge, patches = _forge("Closes #2811\n")
    forge.after_patch = lambda _: "Closes #2811\n"

    with patches[0], patches[1]:
        assert not carry_declared_lines("o/r", PR, [f"- {GCLOUD} — new env contract"])

    assert forge.patches == 2, "retried once, then left for the driver's hold"


def _facts(head: str = HEAD) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


def _drive(
    store: Any, tmp_path: Path, *, head: str = HEAD, max_rounds: int = 8
) -> tuple[Any, list[RemediationContext]]:
    calls: list[RemediationContext] = []
    facts = _facts(head)
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=next((h.reason for h in store.list_epic_holds(EPIC)), None),
        review=ReviewOverlay(
            enabled=True,
            reserved_kind=None,
            round_count=5,
            latest_verdict="NEEDS_CHANGES",
            latest_head_sha=head,
            current_head_sha=head,
        ),
    )
    outcome = drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(adversarial=True, blocking=True, max_rounds=max_rounds),
            logging=LoggingConfig(dir=str(tmp_path / "logs")),
        ),
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        remediator=calls.append,
        now=NOW,
    )
    return outcome, calls


def _remediation(run_id: str, *, started_at: datetime) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=PR,
        item_id=ITEM,
        branch="agent/issue-2811",
        status="success",
        exit_code=0,
        started_at=started_at,
        finished_at=started_at,
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
    )


def _round(
    store: Any,
    tmp_path: Path,
    flagged: tuple[str, ...] = (GCLOUD, CEILING),
    name: str = "round-5",
    *,
    head: str = HEAD,
) -> None:
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="NEEDS_CHANGES",
        sidecar_digest="d",
        sidecar_path=str(_declaration_sidecar(tmp_path, flagged, name)),
    )


def test_an_unreadable_body_after_a_remediation_waits_instead_of_holding(
    tmp_path: Path,
) -> None:
    """A transient read failure is unknown, not missing: no sticky hold."""
    forge, patches = _forge("Closes #2811\n")
    forge.unreadable = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated(store)

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert calls == [], "the remediation already ran at this head"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []

        # The next tick reads the body and finds the lines really are absent.
        forge.unreadable = False
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert calls == []
        assert outcome.held == [ITEM]


def test_a_no_change_remediation_after_a_declaration_round_holds_the_node(
    tmp_path: Path,
) -> None:
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded + timedelta(seconds=5)))

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert calls == [], "a second remediation would end the same way"
        assert outcome.held == [ITEM]
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{TEST_DECLARATIONS_HOLD_PREFIX}: 2 undeclared: ")
        assert holds_node_out_of_frontier(reason)


def test_the_first_remediation_of_a_declaration_round_still_runs(
    tmp_path: Path,
) -> None:
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        recorded = store.latest_review_round(ITEM).recorded_at
        # A remediation of an EARLIER round does not count against this one.
        store.save(_remediation("rem-0", started_at=recorded - timedelta(hours=1)))

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert [c.item_id for c in calls] == [ITEM]
        assert calls[0].declaration is not None
        assert calls[0].declaration.node_ids == (GCLOUD, CEILING)
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_body_that_declares_every_test_re_reviews_instead_of_remediating(
    tmp_path: Path,
) -> None:
    body = (
        "Closes #2811\n\n**Behavior changes**\n"
        f"- {GCLOUD} — new env contract\n- {CEILING} — ceiling kills the child\n"
    )
    forge, patches = _forge(body)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded + timedelta(seconds=5)))

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert calls == [], "the declaration answered the round; nothing to remediate"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


_DECLARED_BODY = (
    "Closes #2811\n\n**Behavior changes**\n"
    f"- {GCLOUD} — new env contract\n- {CEILING} — ceiling kills the child\n"
)


def _remediated(store: Any) -> None:
    recorded = store.latest_review_round(ITEM).recorded_at
    store.save(_remediation("rem-1", started_at=recorded + timedelta(seconds=5)))


def test_a_needs_full_round_at_the_head_does_not_block_the_re_review(
    tmp_path: Path,
) -> None:
    forge, patches = _forge(_DECLARED_BODY)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_FULL", review_mode="delta"
        )
        _round(store, tmp_path)
        _remediated(store)

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert calls == [], "the declaration answered the round; nothing to remediate"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_declaring_the_held_tests_and_unholding_re_reviews_a_second_request(
    tmp_path: Path,
) -> None:
    """A re-review that asks for one more test, held, then declared by a person."""
    forge, patches = _forge(
        f"Closes #2811\n\n**Behavior changes**\n- {GCLOUD} — new env contract\n"
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path, flagged=(GCLOUD,), name="round-4")
        _round(store, tmp_path)
        _remediated(store)

        with patches[0], patches[1]:
            outcome, _ = _drive(store, tmp_path)
        assert outcome.held == [ITEM]

        forge.body = _DECLARED_BODY
        store.clear_epic_hold_item(ITEM)
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert calls == []
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_re_review_that_rejects_declared_tests_again_is_remediated_normally(
    tmp_path: Path,
) -> None:
    """The body declares both tests and the re-review they earned rejects them
    anyway: a disagreement no declaration settles, so it is neither re-reviewed
    again nor held as "undeclared" — it is an ordinary round."""
    forge, patches = _forge(_DECLARED_BODY)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path, name="round-4")
        # The remediation of round-4; round-5 is the re-review it earned.
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded))
        _round(store, tmp_path)

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert [c.item_id for c in calls] == [ITEM]
        assert calls[0].declaration is None, "the ordinary brief, code allowed"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def _round_excusing(store: Any, tmp_path: Path, declared: tuple[str, ...]) -> None:
    """Round-4 as the reviewer launch records it: asking for both tests while
    the body already declares *declared*, which kew excuses off the finding."""
    sha = "c" * 40
    sidecar = {
        "verdict": "NEEDS_CHANGES",
        "findings": [_finding("neither test is declared", declare=(GCLOUD, CEILING))],
        "test_changes": [
            {
                "flag": integrity.FLAG_UNDECLARED_TEST_EDITS,
                "answer": "REJECTED",
                "reason": REVIEWER_REASON,
            }
        ],
    }
    collected = collect_stream_verdict(
        capture_of(reviewer_stream(sidecar, sha)),
        head_sha=sha,
        exit_code=0,
        dest_dir=tmp_path / "logs",
        run_id="round-4",
        declared_tests=declared,
    )
    assert collected.parsed.excused_tests == declared
    record_flagged_tests(collected.stored_path, _report(GCLOUD, CEILING))
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=HEAD,
        verdict="NEEDS_CHANGES",
        sidecar_digest=collected.digest,
        sidecar_path=str(collected.stored_path),
    )


def test_a_re_review_asking_again_for_a_test_excused_earlier_is_remediated_normally(
    tmp_path: Path,
) -> None:
    """Round-4 asked for both tests; the body declared CEILING, so its request
    named GCLOUD alone. Once GCLOUD is declared too, the re-review it earned
    asking for both again at the same head has spent that re-review: it is an
    ordinary round, never re-reviewed a second time (#3516)."""
    forge, patches = _forge(_DECLARED_BODY)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round_excusing(store, tmp_path, (CEILING,))
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded))
        _round(store, tmp_path)

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert [c.declaration for c in calls] == [None], "the ordinary brief"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_declaration_remediation_that_also_proposed_its_lines_is_remediated_again(
    tmp_path: Path,
) -> None:
    """A declaration round's remediation pushes nothing by design, and its
    report's lines reach the proposed block as well as **Behavior changes**.
    The re-review its declaration earned is #2872's, not the one no-push
    re-review a changed proposed block earns (#3475): when it rejects the
    declared tests again at the same head, the round is remediated normally,
    never held for no progress."""
    lines = [f"- {GCLOUD} — new env contract", f"- {CEILING} — ceiling kills the child"]
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path, name="round-4")
        with patches[0], patches[1]:
            _, calls = _drive(store, tmp_path)
        assert [c.declaration is not None for c in calls] == [True]

        # The remediation declared both tests and proposed them, at this head.
        forge.body = with_proposed_lines(_DECLARED_BODY, lines)
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded))
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert (calls, outcome.held) == ([], []), "the declaration is re-reviewed"

        # That re-review rejects the declared tests again at the same head.
        _round(store, tmp_path, name="round-5")
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert [c.declaration for c in calls] == [None]
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_declare_re_reject_unhold_and_the_node_makes_progress(
    tmp_path: Path,
) -> None:
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path, name="round-4")
        # Started at round-4's own moment, so it precedes the re-review below.
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded))
        with patches[0], patches[1]:
            outcome, _ = _drive(store, tmp_path)
        assert outcome.held == [ITEM], "the remediation declared nothing"

        # A person declares both tests and releases the node: re-reviewed.
        forge.body = _DECLARED_BODY
        store.clear_epic_hold_item(ITEM)
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert (calls, outcome.held) == ([], [])

        # That re-review rejects the declared tests again at the same head.
        _round(store, tmp_path, name="round-5")
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert [c.declaration for c in calls] == [None]
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_requested_lines_rejects_unrequested_id_on_same_line(tmp_path: Path) -> None:
    """A line naming a requested id AND another one would declare both: it is
    refused whole, and what is carried is rebuilt around the agent's reason,
    escaped so its markup and mentions render as text (#3187)."""
    request = load_declaration_request(str(_declaration_sidecar(tmp_path)))
    assert request is not None
    report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {GCLOUD} tests/test_auth.py::test_cross_tenant — changed contract\n"
        f"- `{CEILING}` — the ceiling <b>now</b> **kills** [the child](x) @someone\n"
    )

    lines = requested_lines(request, report)

    reason = "the ceiling <b>now</b> **kills** [the child](x) @someone"
    assert lines == [
        f"- {CEILING} — the ceiling &#60;b&#62;now&#60;/b&#62; "
        "&#42;&#42;kills&#42;&#42; [the child](x) &#64;someone"
    ]
    assert unescape_text(lines[0]) == f"- {CEILING} — {reason}"
    body = with_declared_lines("Closes #2811\n", lines)
    assert declared_behavior_changes(body) == {CEILING}


def test_one_rejected_flag_requests_only_the_tests_a_finding_evidences(
    tmp_path: Path,
) -> None:
    """Two edits are undeclared, but only GCLOUD's base copy fails: CEILING is
    never requested, so it cannot become a waiver nobody evidenced."""
    path = _sidecar(
        tmp_path,
        _finding(f"{GCLOUD} fails against the head, undeclared.", declare=(GCLOUD,)),
    )
    request = load_declaration_request(str(path))
    assert request is not None
    assert request.node_ids == (GCLOUD,)

    report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {GCLOUD} — every gcloud call now runs in the configured env\n"
        f"- {CEILING} — the ceiling now kills the child\n"
    )
    forge, patches = _forge("Closes #2811\n")
    with patches[0], patches[1]:
        assert carry_declared_lines("o/r", PR, requested_lines(request, report))

    assert declared_behavior_changes(forge.body) == {GCLOUD}


@pytest.mark.parametrize(
    "finding",
    [
        pytest.param(
            _finding(f"{GCLOUD} fails against the head and no line names it"),
            id="prose-without-the-kind",
        ),
        pytest.param(
            _finding(
                f"{GCLOUD} and test_x fail",
                declare=(GCLOUD, "tests/test_hosts_providers.py::test_x"),
            ),
            id="declares-an-unflagged-test",
        ),
        pytest.param(
            {**_finding(f"{GCLOUD} is undeclared"), "kind": "declaration"},
            id="the-kind-without-node-ids",
        ),
        pytest.param(
            {**_finding("undeclared", declare=(GCLOUD,)), "kind": "Declaration"},
            id="another-kind",
        ),
        pytest.param(
            {**_finding("undeclared", declare=(GCLOUD,)), "node_ids": [GCLOUD, 7]},
            id="a-malformed-node-id",
        ),
    ],
)
def test_a_finding_that_cannot_be_mapped_onto_flagged_tests_is_not_one(
    tmp_path: Path, finding: dict[str, Any]
) -> None:
    assert load_declaration_request(str(_sidecar(tmp_path, finding))) is None


def test_code_finding_mentioning_flagged_test_is_not_declaration_only(
    tmp_path: Path,
) -> None:
    path = _sidecar(
        tmp_path,
        _finding(f"{GCLOUD} is undeclared", declare=(GCLOUD,)),
        _finding(
            f"the retry never backs off, which is why {GCLOUD} fails",
            "src/kew/retry.py:88",
        ),
    )

    assert load_declaration_request(str(path)) is None


_SAME_FILE_DEFECT = _finding(
    f"_tenant() in this file returns the caller's own tenant, so {GCLOUD}'s "
    "cross-tenant assertions are vacuous",
    "tests/test_hosts_providers.py:40",
)


def test_a_same_file_defect_beside_a_declaration_is_remediated_as_code(
    tmp_path: Path,
) -> None:
    """A defect sitting in the flagged test's own file, naming that test, is
    still a defect: only the reviewer's ``kind`` makes a finding a declaration,
    so the round gets the ordinary brief and the defect is not lost."""
    path = _sidecar(
        tmp_path,
        _finding(f"{GCLOUD} is undeclared", declare=(GCLOUD,)),
        _SAME_FILE_DEFECT,
    )
    assert load_declaration_request(str(path)) is None

    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=HEAD,
            verdict="NEEDS_CHANGES",
            sidecar_digest="d",
            sidecar_path=str(path),
        )
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

    assert [c.declaration for c in calls] == [None]
    assert outcome.held == []


def test_a_rejected_flag_whose_tests_the_host_could_not_name_is_not_one(
    tmp_path: Path,
) -> None:
    path = _sidecar(
        tmp_path,
        _finding(f"{GCLOUD} is undeclared", declare=(GCLOUD,)),
        rejected=(integrity.FLAG_UNDECLARED_TEST_EDITS, integrity.FLAG_TESTS_REMOVED),
    )

    assert load_declaration_request(str(path)) is None, "the removal is unnamed"


def _terminal_report_evidence(
    tmp_path: Path, message: str, name: str = "remediation.log"
) -> Path:
    """The run's log, with the evidence the real stream capture derives from a
    claude terminal ``result`` event carrying *message*."""
    log = tmp_path / name
    log.write_text("")
    derivation = StreamDerivation()
    derivation.observe(
        json.dumps({"type": "result", "subtype": "success", "result": message})
    )
    write_derived_evidence(log, derivation.evidence())
    return log


def test_prompt_compliant_terminal_declaration_reaches_pr_body(
    tmp_path: Path,
) -> None:
    request = load_declaration_request(str(_declaration_sidecar(tmp_path)))
    assert request is not None
    prompt = build_review_prompt(
        pr_number=PR,
        issue_number=int(ITEM),
        branch_name="agent/issue-2811",
        comments=[],
        config=DispatchConfig(),
        governed=True,
        declaration=request,
    )
    # The agent fills in exactly the structure the brief prescribes.
    template = prompt.split("```\n", 2)[1]
    example = "- <node id> — <why the old expectation no longer holds>\n"
    assert template.startswith("## Dispatch Report\n") and example in template
    message = "Done.\n\n" + template.replace(
        example,
        f"- {GCLOUD} — every gcloud call now runs in the configured env\n"
        f"- {CEILING} — the ceiling now kills the child\n",
    )
    review = _ReviewDispatch(
        pr_number=PR,
        config=DispatchConfig(project=ProjectConfig(repo="o/r")),
        author_filter=None,
        store=None,
        force_budget=False,
        item_id=ITEM,
        findings=None,
        declaration=request,
    )
    review.result = DispatchResult(
        exit_code=0,
        issue_num=PR,
        log_file=_terminal_report_evidence(tmp_path, message),
    )
    forge, patches = _forge("Closes #2811\n")

    with patches[0], patches[1]:
        review._carry_declarations()

    assert declared_behavior_changes(forge.body) == {GCLOUD, CEILING}


_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_AUTH_BASE = (
    "class TestAuth:\n"
    "    def test_allows_owner(self):\n        assert True\n\n"
    "    def test_denies_cross_tenant(self):\n        assert False is False\n"
)
_AUTH_HEAD = "class TestAuth:\n    def test_allows_owner(self):\n        assert True\n"
REMOVED = "tests/test_auth.py::TestAuth::test_denies_cross_tenant"


def _git(repo: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)


def _removal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main", ".")
    (repo / "tests" / "test_auth.py").write_text(_AUTH_BASE)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "head")
    (repo / "tests" / "test_auth.py").write_text(_AUTH_HEAD)
    _git(repo, "commit", "--quiet", "-am", "drop a test")
    return repo


def test_a_removed_class_test_is_declared_by_the_id_pytest_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the removal in a diff to a held-out report the declaration passes."""
    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")
    report = read_integrity_report(_removal_repo(tmp_path), "main", "head")
    assert report is not None
    assert report.removed_test_ids == (REMOVED,)
    sidecar = tmp_path / "logs" / "round-1.sidecar.json"
    sidecar.parent.mkdir()
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    _finding(
                        "the removal is undeclared",
                        "tests/test_auth.py:5",
                        declare=(REMOVED,),
                    )
                ],
                "test_changes": [
                    {
                        "flag": integrity.FLAG_TESTS_REMOVED,
                        "answer": "REJECTED",
                        "reason": "removed with the cross-tenant check",
                    }
                ],
            }
        )
    )
    record_flagged_tests(sidecar, report)
    request = load_declaration_request(str(sidecar))
    assert request is not None and request.node_ids == (REMOVED,)

    agent_report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {REMOVED} — tenants are now isolated by the gateway\n"
    )
    body = with_declared_lines("Closes #1\n", requested_lines(request, agent_report))
    log = (
        f"{report_line(REMOVED)}kew-held-out: exit 1 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )

    held_out = held_out_report(log, runs=2, declared=declared_behavior_changes(body))
    assert held_out.outcome == "passed"


def test_the_hold_reason_never_cuts_a_node_id() -> None:
    reason = declarations_missing_hold_reason((GCLOUD, CEILING))

    assert len(reason) <= HOLD_REASON_MAX_CHARS
    assert GCLOUD in reason, "the first id is named whole"
    assert reason.endswith(" +1 more")
    assert holds_node_out_of_frontier(reason)


def _old_rounds(store: Any, count: int) -> None:
    """*count* earlier NEEDS_CHANGES rounds at heads since moved past."""
    for n in range(count):
        store.record_review_round(
            EPIC, ITEM, head_sha=f"old{n}", verdict="NEEDS_CHANGES"
        )


def test_a_late_declaration_round_in_the_ceiling_is_never_accepted_as_residual(
    tmp_path: Path,
) -> None:
    """Round 7 is past the stop rule's six, and its one medium finding is
    within the ceiling — but accepting it would never write the line the
    merge's held-out gate reads, so the declaration brief runs instead."""
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _old_rounds(store, 6)
        sidecar = _declaration_sidecar(tmp_path)
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=HEAD,
            verdict="NEEDS_CHANGES",
            # The stop rule re-reads only a sidecar matching its digest.
            sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            sidecar_path=str(sidecar),
        )
        assert store.latest_review_round(ITEM).round == 7

        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        assert outcome.residuals == {}
        assert [c.declaration.node_ids for c in calls if c.declaration] == [
            (GCLOUD, CEILING)
        ]
        assert forge.patches == 0


def test_the_stop_rule_keeps_a_declaration_finding_in_remediation() -> None:
    from kew.review_contract import Finding
    from kew.review_residuals import READY_WITH_RESIDUALS, stop_rule_decision

    low = Finding("low", "tests/t.py:1", "a nit", "pytest", "ok")
    declaration = Finding(
        "low",
        "tests/t.py:1",
        "undeclared",
        "pytest",
        "1 failed",
        kind="declaration",
        node_ids=(GCLOUD,),
    )
    decide = partial(stop_rule_decision, after_rounds=6, max_severity="medium")

    assert decide(7, "NEEDS_CHANGES", [low]) == READY_WITH_RESIDUALS
    assert decide(7, "NEEDS_CHANGES", [low, declaration]) == "NEEDS_CHANGES"


CASE = f"{GCLOUD}[fast]"


def test_one_parametrized_case_does_not_answer_a_whole_function_request(
    tmp_path: Path,
) -> None:
    """The merge excuses only ``[fast]`` for a ``[fast]`` line, so that line
    cannot settle a request for the whole function."""
    request = load_declaration_request(
        str(_declaration_sidecar(tmp_path, flagged=(GCLOUD,)))
    )
    assert request is not None and request.node_ids == (GCLOUD,)
    report = f"## Dispatch Report\n\n### Behavior changes\n- {CASE} — fast mode\n"

    assert requested_lines(request, report) == []
    body = f"Closes #2811\n\n**Behavior changes**\n- {CASE} — fast mode\n"
    assert undeclared(request, body) == (GCLOUD,)


def test_a_requested_case_is_carried_under_its_own_id_and_excuses_only_itself(
    tmp_path: Path,
) -> None:
    path = _sidecar(
        tmp_path,
        _finding(f"{CASE} fails against the head, undeclared", declare=(CASE,)),
        flagged=(GCLOUD,),
    )
    request = load_declaration_request(str(path))
    assert request is not None and request.node_ids == (CASE,)
    report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {GCLOUD} — every gcloud call now runs in the configured env\n"
    )

    lines = requested_lines(request, report)

    assert lines == [f"- {CASE} — every gcloud call now runs in the configured env"]
    body = with_declared_lines("Closes #2811\n", lines)
    assert undeclared(request, body) == ()
    log = (
        f"{report_line(CASE)}kew-held-out: exit 1 seconds 1\n"
        f"{report_line(f'{GCLOUD}[slow]')}kew-held-out: exit 1 seconds 1\n"
    )
    held_out = held_out_report(log, runs=2, declared=declared_behavior_changes(body))
    assert held_out.undeclared == (f"{GCLOUD}[slow]",)


def test_a_body_that_never_reads_is_held_after_five_passes_and_released_on_a_read(
    tmp_path: Path,
) -> None:
    forge, patches = _forge("Closes #2811\n")
    forge.unreadable = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated(store)

        for _ in range(4):
            with patches[0], patches[1]:
                outcome, calls = _drive(store, tmp_path)
            assert (calls, outcome.held) == ([], [])
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert (calls, outcome.held) == ([], [ITEM])
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{PR_BODY_UNREADABLE_HOLD_PREFIX}: PR #{PR} ")
        assert "clears itself" in reason
        assert holds_node_out_of_frontier(reason)

        # Still unreadable: the hold stands, nothing is dispatched.
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert calls == []
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [reason]

        # The first read that succeeds clears it; the body declares both
        # tests, so the head is re-reviewed rather than held again.
        forge.unreadable = False
        forge.body = _DECLARED_BODY
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)
        assert (calls, outcome.held) == ([], [])
        assert store.list_epic_holds(EPIC) == []


def test_a_read_between_failures_starts_the_count_over(tmp_path: Path) -> None:
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated(store)
        for readable in (False, False, False, False, True):
            forge.unreadable = not readable
            with patches[0], patches[1]:
                outcome, _ = _drive(store, tmp_path)
        # The read found the lines missing: the declaration hold, released by
        # a person below without changing the body.
        assert outcome.held == [ITEM]
        store.clear_epic_hold_item(ITEM)

        forge.unreadable = True
        for _ in range(4):
            with patches[0], patches[1]:
                outcome, _ = _drive(store, tmp_path)
            assert outcome.held == [], "four failures after a read are not five"


def _widget_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "project"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main", ".")
    (repo / "tests" / "test_widget.py").write_text(
        "def test_widget():\n    assert widget() == 3\n"
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    (repo / "tests" / "test_widget.py").write_text(
        "def test_widget():\n    assert widget() == 4\n"
    )
    _git(repo, "commit", "--quiet", "-am", "edit the expectation")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, head


WIDGET = "tests/test_widget.py::test_widget"


def _widget_launcher(*, fail_record: bool) -> Any:
    def launch(ctx: Any) -> Any:
        sidecar = stream_verdict(
            ctx,
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    _finding(
                        f"{WIDGET} fails against the head, undeclared",
                        "tests/test_widget.py:2",
                        declare=(WIDGET,),
                    )
                ],
                "test_changes": [
                    {
                        "flag": integrity.FLAG_UNDECLARED_TEST_EDITS,
                        "answer": "REJECTED",
                        "reason": REVIEWER_REASON,
                    }
                ],
            },
        )
        if fail_record:
            # The record's own path is taken: the write between the stored
            # sidecar and the round fails as a full or read-only disk would.
            Path(f"{sidecar.stored_path}.declarations").mkdir()
        return sidecar

    return launch


def _review_tick(
    store: Any, tmp_path: Path, repo: Path, head: str, launcher: Any
) -> Any:
    facts = replace(_facts(), head_oid=head)
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=next((h.reason for h in store.list_epic_holds(EPIC)), None),
        review=ReviewOverlay(
            enabled=True,
            reserved_kind=None,
            round_count=0,
            latest_verdict=None,
            latest_head_sha=None,
            current_head_sha=head,
        ),
    )
    return drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(
                adversarial=True,
                blocking=True,
                max_rounds=8,
                publish_status=False,
                publish_comment=False,
            ),
            logging=LoggingConfig(dir=str(tmp_path / "logs")),
        ),
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=launcher,
        remediator=lambda _: None,
        now=NOW,
    )


def test_a_record_write_failure_after_the_sidecar_holds_and_records_no_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flagged-test record and the round land together or not at all: a
    round standing without its record would be remediated at the same head
    with the ordinary brief — the loop this issue ends."""
    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")
    repo, head = _widget_repo(tmp_path)
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with patches[0], patches[1]:
            outcome = _review_tick(
                store, tmp_path, repo, head, _widget_launcher(fail_record=True)
            )

        assert outcome.held == [ITEM]
        assert store.latest_review_round(ITEM) is None
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{DECLARATION_RECORD_FAILED_HOLD_PREFIX}: ")
        assert "IsADirectoryError" in reason
        assert holds_node_out_of_frontier(reason)

        # A person fixes the log directory and releases the node: with no
        # round at the head, it is reviewed again and the record lands.
        shutil.rmtree(tmp_path / "logs")
        store.clear_epic_hold_item(ITEM)
        with patches[0], patches[1]:
            outcome = _review_tick(
                store, tmp_path, repo, head, _widget_launcher(fail_record=False)
            )

        assert outcome.held == []
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "NEEDS_CHANGES"
        request = load_declaration_request(latest.sidecar_path)
        assert request is not None and request.node_ids == (WIDGET,)


SERVE_TIMEOUT = (
    "tests/test_serve_api_dashboard.py::TestDashboardReadsUnderLoad::"
    "test_a_slow_epic_reader_names_its_source_and_still_serves_the_rest"
)


def test_the_dashboard_serves_every_id_the_held_node_must_declare(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The reason is capped and ends ``+N more``; the served row is not (#2884)."""
    from kew.serve.app import create_app

    flagged = (GCLOUD, CEILING, SERVE_TIMEOUT)
    db = str(tmp_path / "d.db")
    forge, patches = _forge("Closes #2811\n")
    with SqliteRunStore(db) as store:
        _round(store, tmp_path, flagged)
        _remediated(store)
        with patches[0], patches[1]:
            outcome, _calls = _drive(store, tmp_path)
        assert outcome.held == [ITEM]
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
    assert reason.endswith(" more"), "the reason alone cannot name them all"

    app = create_app(
        lambda: SqliteRunStore(db),
        repo_dir=tmp_path,
        expose=False,
        config=DispatchConfig(project=ProjectConfig(repo="")),
    )
    body = inbox_client(app, loopback_peer=True).get("/api/dashboard").json()

    (row,) = [r for r in body["rows"] if r["item_id"] == ITEM]
    assert row["reason"] == reason
    assert sorted(row["node_ids"]) == sorted(flagged)


def _dashboard_row(db: str, tmp_path: Path, inbox_client: Any) -> dict[str, Any]:
    from kew.serve.app import create_app

    app = create_app(
        lambda: SqliteRunStore(db),
        repo_dir=tmp_path,
        expose=False,
        config=DispatchConfig(project=ProjectConfig(repo="")),
    )
    body = inbox_client(app, loopback_peer=True).get("/api/dashboard").json()
    (row,) = [r for r in body["rows"] if r["item_id"] == ITEM]
    return row


def test_a_re_hold_whose_reason_reads_the_same_serves_its_own_ids(
    tmp_path: Path, inbox_client: Any
) -> None:
    """Same count, same visible id: only the hold's own list tells them apart."""
    first = (GCLOUD, CEILING, SERVE_TIMEOUT)
    second = (GCLOUD, f"{CEILING}_again", f"{SERVE_TIMEOUT}_again")
    reason = declarations_missing_hold_reason(first)
    assert reason == declarations_missing_hold_reason(second)
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        store.hold_epic_node_item(EPIC, ITEM, reason, node_ids=first)
        assert store.remove_epic_hold(EPIC, ITEM) is not None
        store.hold_epic_node_item(EPIC, ITEM, reason, node_ids=second)

    assert _dashboard_row(db, tmp_path, inbox_client)["node_ids"] == list(second)


def test_a_hold_with_no_stored_list_serves_no_ids(
    tmp_path: Path, inbox_client: Any
) -> None:
    """A released list never resurfaces on a later hold written without one."""
    flagged = (GCLOUD, CEILING, SERVE_TIMEOUT)
    reason = declarations_missing_hold_reason(flagged)
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        _round(store, tmp_path, flagged)
        store.hold_epic_node_item(EPIC, ITEM, reason, node_ids=flagged)
        store.clear_epic_hold_item(ITEM)
        store.hold_epic_node_item(EPIC, ITEM, reason)

    row = _dashboard_row(db, tmp_path, inbox_client)
    assert row["reason"] == reason
    assert row["node_ids"] == []


def test_another_writer_s_hold_over_a_declaration_hold_drops_its_ids(
    tmp_path: Path,
) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.hold_epic_node_item(
            EPIC, ITEM, declarations_missing_hold_reason((GCLOUD,)), node_ids=(GCLOUD,)
        )
        assert store.epic_hold_node_ids(ITEM) == (GCLOUD,)
        assert store.hold_epic_node_unless(
            EPIC, ITEM, "review_rounds_exhausted", keep=lambda _r: False
        )
        assert store.epic_hold_node_ids(ITEM) == ()


def test_the_brief_says_only_the_reviewer_accepts_proposed_lines() -> None:
    """The implementer is told its lines are proposals the reviewer rules on,
    and that kew writes only the ones the reviewer accepts (#3475) — never that
    kew copies them into the body on its own."""
    prompt = " ".join(
        build_prompt(
            issue_num=int(ITEM),
            issue_title="Fix widget",
            issue_body="Body",
            complexity="medium",
            config=DispatchConfig(),
            has_coderabbit=False,
            sandboxed=True,
        ).split()
    )
    assert (
        "kew's reviewer rules on each line in its verdict, and kew adds the lines "
        "it accepts to the PR's **Behavior changes** section"
    ) in prompt
    assert "kew copies that line from this report" not in prompt


WIDGET_SPEC = "frontend/src/lib/monitor/widget.test.ts"


def _brief_through_remediator(
    store: Any, ctx: RemediationContext, tmp_path: Path
) -> str:
    """The prompt the production remediator hands the agent for *ctx*.

    ``sandbox_remediator`` and ``dispatch_review`` run for real; only their
    boundaries are faked: ``gh pr view``, the GitHub reachability probe, the
    container route, the review checkout and the agent launch. The launch is
    stopped at ``run_tracked_agent``, the process boundary, where the prompt is
    read."""
    from kew.graph.driver.review_launcher import sandbox_remediator

    prompts: list[str] = []

    def launch(**kwargs: Any) -> DispatchResult:
        prompts.append(kwargs["prompt"])
        raise RuntimeError("stopped at the process boundary")

    def gh(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["gh", "pr", "view"], argv
        meta = {
            "number": PR,
            "headRefName": "agent/issue-2811",
            "headRefOid": HEAD,
            "baseRefName": "main",
            "body": f"Closes #{ITEM}",
            "title": "t",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(meta), "")

    checkout = SimpleNamespace(
        path=tmp_path, head_sha=HEAD, detached=False, conflict=None
    )
    containment = SimpleNamespace(
        posture="host", image=None, contained=False, reason=""
    )
    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    with (
        patch("kew.review.subprocess_run", gh),
        patch("kew.review._check_github_connectivity", return_value=True),
        patch("kew.review.resolve_inference_transport", return_value=None),
        patch("kew.review.resolve_remediation_containment", return_value=containment),
        patch("kew.review.materialize_review_checkout", return_value=checkout),
        patch("kew.review.attest_flow_posture"),
        patch("kew.review.discard_checkout"),
        patch("kew.review.run_tracked_agent", launch),
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None),
    ):
        sandbox_remediator(config, store, epic_ref=EPIC)(ctx)
    [prompt] = prompts
    return prompt


def test_a_declaration_only_brief_names_each_test_and_requires_a_line_for_it(
    tmp_path: Path,
) -> None:
    """A round whose every finding asks only for a Behavior changes line, but
    that no flagged-test record makes a declaration round (a frontend test
    file has no node id the host flagged), used to get the ordinary brief: its
    remediation ended with no Dispatch Report and proposed nothing (#3493).
    The brief it receives now names each requested test and makes one
    proposed line per test, with a reason, the whole job."""
    sidecar = _sidecar(
        tmp_path,
        _finding(
            f"{GCLOUD} fails against the head: a declaration line is the whole fix",
            declare=(GCLOUD,),
        ),
        _finding(
            f"{WIDGET_SPEC} changed and no Behavior changes line names it",
            "frontend/src/lib/monitor/widget.test.ts:4",
            declare=(WIDGET_SPEC,),
        ),
    )
    Path(f"{sidecar}.declarations").unlink()
    assert load_declaration_request(str(sidecar)) is None, "not a declaration round"
    forge, patches = _forge(
        f"Closes #2811\n\n**Behavior changes**\n- {CEILING} — ceiling kills the child\n"
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=HEAD,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            sidecar_path=str(sidecar),
        )
        with patches[0], patches[1]:
            outcome, calls = _drive(store, tmp_path)

        [ctx] = calls
        prompt = _brief_through_remediator(store, ctx, tmp_path)

    assert outcome.remediating == [ITEM]
    assert ctx.declaration is None, "kew carries nothing into Behavior changes"
    flat = " ".join(prompt.split())
    assert "## Propose a Behavior changes line for each test — no code change" in (
        prompt
    )
    assert "Tests that need a line (2):" in prompt
    assert f"- `{GCLOUD}`" in prompt
    assert f"- `{WIDGET_SPEC}`" in prompt
    assert "a declaration line is the whole fix" in prompt, "the reviewer's reason"
    assert "Your whole job is one line per test below, each with a reason" in flat
    assert "### Behavior changes\n- <node id> — <why" in prompt
    assert (
        "The run fails without them: a run that ends with no Dispatch Report, "
        "or with no line for a test below, proposes nothing"
    ) in flat
    assert "## Declare these tests" not in prompt


def test_a_round_whose_requested_tests_the_body_already_declares_gets_no_proposal_brief(
    tmp_path: Path,
) -> None:
    """The body declares the test the finding asks about and the reviewer
    rejected it anyway: a disagreement no line settles, so the ordinary brief."""
    sidecar = _sidecar(tmp_path, _finding(f"{GCLOUD} undeclared", declare=(GCLOUD,)))
    Path(f"{sidecar}.declarations").unlink()
    forge, patches = _forge(
        f"Closes #2811\n\n**Behavior changes**\n- {GCLOUD} — new env contract\n"
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=HEAD,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            sidecar_path=str(sidecar),
        )
        with patches[0], patches[1]:
            _, calls = _drive(store, tmp_path)

    assert [(c.declaration, c.proposal) for c in calls] == [(None, None)]
