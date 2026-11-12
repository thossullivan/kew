"""#2362: a remediation whose publication is deferred must not loop.

The incident, on PR #2352: the remediation merged main into the branch, so the
publication diff carried main's OWN edits to `.github/workflows/ci.yml` — the
checkout was cut from a head that predated them. The protected-path check
flagged those, publication was held for a person, the run row recorded
`success` anyway, and the driver read the same standing NEEDS_CHANGES verdict
against the same unmoved head on every tick and dispatched another remediation.
Three runs, ~$44, nothing held, and `kew epic watch` emitted nothing.

Three defects, in the order the lifecycle hits them:

1. the protected-path check has to judge the remediation's OWN change — a path
   whose bytes are already the merged base's was not authored here;
2. a deferred publication records `awaiting_publication`, never `success`;
3. the next tick HOLDs the node under `publication_held` instead of
   dispatching, and the attention stream `kew epic watch` reads emits it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support.driver_fakes import EPIC, FakeFactsReader, FakeSource, _node

from kew import subprocess_utils, worktree
from kew.config import DispatchConfig, EpicConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import (
    HOLD_REASON_MAX_CHARS,
    PUBLICATION_HELD_HOLD_PREFIX,
    deferred_publication_key,
    holds_node_out_of_frontier,
    publication_held_hold_reason,
    safe_hold_reason,
)
from kew.graph.build import build_graph
from kew.graph.driver import tick as tick_mod
from kew.graph.driver.attention import AttentionKind, attention_events
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.publication_canonical import (
    CanonicalIdentity,
    PublicationHeld,
    publish_checkout,
)
from kew.publication_filter import ManifestEntry, ReviewRequired, filter_manifest
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, RunRecord, SqliteRunStore

ITEM = "2280"
HEAD = "c0ffee11"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WORKFLOW = ".github/workflows/ci.yml"
FIXTURE = "tests/fixtures/sample.json"
_IDENTITY = CanonicalIdentity(name="kew", email="kew@example.invalid")


# ---------------------------------------------------------------------------
# Defect 1: the protected-path check judges the remediation's own change
# ---------------------------------------------------------------------------


def _entry(path: str, digest: str) -> ManifestEntry:
    return ManifestEntry(path=path, mode="100644", sha256=digest)


def _filter(entries, **overrides):
    kwargs: dict[str, Any] = {
        "deletions": (),
        "existing_modes": {},
        "destination_paths": (),
    }
    kwargs.update(overrides)
    return filter_manifest(entries, **kwargs)


def test_a_workflow_write_the_merged_base_already_holds_is_not_flagged() -> None:
    verdict = _filter(
        [_entry(WORKFLOW, "a" * 64), _entry(FIXTURE, "b" * 64)],
        merged_base_digest={WORKFLOW: "a" * 64}.get,
    )

    assert verdict.flags == ()
    assert not verdict.review_required
    assert len(verdict.approved()) == 2


def test_a_workflow_write_the_merged_base_does_not_hold_still_flags() -> None:
    """The exemption is content equality, never the path's name: an agent-edited
    workflow stops for a person even when the base has one at that path."""
    verdict = _filter(
        [_entry(WORKFLOW, "c" * 64)], merged_base_digest={WORKFLOW: "a" * 64}.get
    )

    assert verdict.review_reasons() == (
        f"{WORKFLOW}: CI workflow code, which runs with repository secrets",
    )
    with pytest.raises(ReviewRequired):
        verdict.approved()


def test_an_entry_without_a_digest_is_never_exempt() -> None:
    """An unknown answer has to read like "different". A manifest entry with no
    digest would otherwise compare equal to a base that answers nothing."""
    verdict = _filter(
        [ManifestEntry(path=WORKFLOW, mode="100644")],
        merged_base_digest=lambda _path: None,
    )

    assert verdict.review_required


def test_a_removal_is_flagged_even_where_the_merged_base_has_nothing() -> None:
    """ "The base does not have it either" and "nobody could answer" are the same
    reply from that reader, so a removal keeps its stop for a person."""
    verdict = _filter(
        [], deletions=(".gitattributes",), merged_base_digest=lambda _path: None
    )

    assert verdict.review_reasons() == (
        ".gitattributes: removing it can activate an inherited attribute driver",
    )


# ---------------------------------------------------------------------------
# ...end to end, through a real canonical publication
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def merged_scene(tmp_path: Path) -> dict[str, Any]:
    """The #2352 shape: a PR branch cut BEFORE main moved a workflow file.

    ``head`` is the branch head the remediation checkout was cut at and the
    base the publication applies to; ``main`` carries the workflow edit that
    landed after it. The checkout holds the merge result — main's workflow
    bytes, verbatim — plus the remediation's own fixture edit.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    for path, body in ((WORKFLOW, "name: ci\njobs: {}\n"), (FIXTURE, '{"v": 1}\n')):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    head = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{_BRANCH}")

    # main moves on: the workflow edit the remediation's publication must not
    # be asked about (it is already merged upstream).
    merged_workflow = "name: ci\njobs: {}\n# hardened\n"
    (seed / WORKFLOW).write_text(merged_workflow)
    _git(seed, "commit", "-qam", "harden ci")
    _git(seed, "push", "-q", "origin", "main")
    main = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", _BRANCH, str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    (checkout / WORKFLOW).write_text(merged_workflow)
    (checkout / FIXTURE).write_text('{"v": 2}\n')
    return {
        "origin": origin,
        "checkout": checkout,
        "head": head,
        "main": main,
        "merged_workflow": merged_workflow,
    }


_BRANCH = "agent/issue-2280"


def _publish(scene: dict[str, Any], scratch: Path, **overrides: Any) -> Any:
    head = str(scene["head"])
    kwargs: dict[str, Any] = {
        "remote": str(scene["origin"]),
        "branch": _BRANCH,
        "parents": (head,),
        "base_sha": head,
        "message": "fix: address review feedback on PR #2352",
        "identity": _IDENTITY,
        "force_with_lease": f"{_BRANCH}:{head}",
        "output_guard": None,
        "scratch": scratch,
    }
    kwargs.update(overrides)
    return publish_checkout(Path(scene["checkout"]), **kwargs)


def _remote_text(origin: Path, revision: str, path: str) -> str:
    return _git(origin, "show", f"{revision}:{path}")


def test_a_base_merge_workflow_edit_publishes_without_review(
    merged_scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC1. The remediation's own change is one fixture file; the workflow in
    its diff is main's, byte for byte. Nothing here needs a person.

    The merged base is the commit's declared second parent, the only shape the
    remediation publisher passes it in (#2632)."""
    main = str(merged_scene["main"])
    published = _publish(
        merged_scene,
        tmp_path / "scratch",
        parents=(str(merged_scene["head"]), main),
        merged_base_sha=main,
    )

    origin = Path(merged_scene["origin"])
    assert published.commit_sha == _git(origin, "rev-parse", f"refs/heads/{_BRANCH}")
    assert sorted(published.changed_paths) == [WORKFLOW, FIXTURE]
    assert _remote_text(origin, _BRANCH, FIXTURE) == '{"v": 2}'
    assert _remote_text(origin, _BRANCH, WORKFLOW) == str(
        merged_scene["merged_workflow"]
    ).rstrip("\n")


def test_the_same_publication_is_held_without_the_merged_base(
    merged_scene: dict[str, Any], tmp_path: Path
) -> None:
    """The control for the test above: nothing else about the scene changed, so
    the exemption is what let it through — and its absence still defers."""
    with pytest.raises(PublicationHeld) as raised:
        _publish(merged_scene, tmp_path / "scratch")

    assert any(WORKFLOW in flag for flag in raised.value.flags)
    assert _git(Path(merged_scene["origin"]), "rev-parse", f"refs/heads/{_BRANCH}") == (
        str(merged_scene["head"])
    )


def test_an_agent_authored_workflow_edit_is_still_deferred(
    merged_scene: dict[str, Any], tmp_path: Path
) -> None:
    """The gate is not disabled by naming a merged base: a workflow the base
    does NOT hold these bytes for still stops the publication."""
    (Path(merged_scene["checkout"]) / WORKFLOW).write_text("name: ci\non: [push]\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(
            merged_scene,
            tmp_path / "scratch",
            merged_base_sha=str(merged_scene["main"]),
        )

    assert any(WORKFLOW in flag for flag in raised.value.flags)


def test_an_unresolvable_merged_base_defers_rather_than_failing(
    merged_scene: dict[str, Any], tmp_path: Path
) -> None:
    """Best effort, both ways. A base the host cannot fetch takes away the
    exemption — never the publication, and never the human stop."""
    with pytest.raises(PublicationHeld):
        _publish(merged_scene, tmp_path / "scratch", merged_base_sha="f" * 40)


# ---------------------------------------------------------------------------
# Defect 2: a deferred publication is not a success
# ---------------------------------------------------------------------------


def _remediation_row(run_id: str, *, status: str = "success") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=2352,
        item_id=ITEM,
        branch=_BRANCH,
        status=status,
        exit_code=0,
        started_at=NOW,
        finished_at=NOW,
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
        estimated_cost_usd=14.0,
    )


def test_a_deferred_remediation_publication_records_awaiting_publication(
    tmp_path: Path,
) -> None:
    """AC2, first half. The run row said `success` for all three of #2352's
    deferred runs, which is what the driver's guard now reads."""
    from kew.publication import PublicationError
    from kew.review import dispatch_review
    from kew.review_checkout import ReviewCheckout
    from kew.review_contract import Finding
    from kew.tracking import DispatchResult

    checkout_dir = tmp_path / "pr-2352"
    checkout_dir.mkdir()
    checkout = ReviewCheckout(
        path=checkout_dir,
        head_sha=HEAD,
        branch=_BRANCH,
        detached=False,
        impl_worktree=None,
        conflict=None,
    )
    held = PublicationHeld((f"{WORKFLOW}: CI workflow code",))
    deferral = PublicationError("push", held)
    # The canonical path re-raises a deferral wrapped for its callers, exactly
    # as `worktree._publish_existing_pr_branch_canonical` does.
    deferral.__cause__ = held

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(_remediation_row("rem-1"))
        with (
            patch("kew.review._check_github_connectivity", return_value=True),
            patch(
                "kew.review._get_pr_metadata",
                return_value={
                    "number": 2352,
                    "headRefName": _BRANCH,
                    "baseRefName": "main",
                    "body": "Closes #2280",
                    "title": "Fix the widget",
                },
            ),
            patch("kew.review.materialize_review_checkout", return_value=checkout),
            # The credential gate is not what this test is about; the governed
            # remediation already has its producer evidence in production.
            patch("kew.review.ensure_publication_credentials"),
            patch(
                "kew.review.run_tracked_agent",
                return_value=DispatchResult(
                    exit_code=0,
                    issue_num=2352,
                    log_file=tmp_path / "run.log",
                    run_id="rem-1",
                ),
            ),
            patch("kew.review.publish_existing_pr_branch", side_effect=deferral),
            patch("kew.review.publication_remote_sha", return_value=HEAD),
            patch("kew.review.discard_checkout") as discarded,
        ):
            outcome = dispatch_review(
                pr_number=2352,
                config=DispatchConfig(
                    logging=LoggingConfig(dir=str(tmp_path / "logs"))
                ),
                store=store,
                item_id=ITEM,
                findings=[
                    Finding(
                        severity="high",
                        location=f"{FIXTURE}:1",
                        summary="the fixture is stale",
                        evidence_command="uv run pytest -q",
                        evidence_output_digest="sha256:cafe",
                    )
                ],
            )

        assert outcome.exit_code == 1
        row = store.get("rem-1")
        assert row is not None
        assert row.status == "awaiting_publication", (
            "a remediation that published nothing is not a success: the driver "
            "re-dispatched three of them off this very field"
        )
        assert row.error_category == "publication_held"
        evidence = json.loads(store.get_meta(deferred_publication_key("rem-1")) or "{}")
        assert evidence["head"] == HEAD
        assert evidence["checkout"] == str(checkout_dir)

    discarded.assert_not_called()
    assert checkout_dir.is_dir(), "the preserved work is the whole point of the hold"


# ---------------------------------------------------------------------------
# Defect 3: the next tick holds instead of dispatching
# ---------------------------------------------------------------------------


def _facts(*, head_oid: str = HEAD) -> NodeFacts:
    return NodeFacts(
        issue_num=2280,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=2352,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=None,
    )


def _needs_changes_overlay(head: str = HEAD) -> ReviewOverlay:
    return ReviewOverlay(
        enabled=True,
        reserved_kind=None,
        round_count=4,
        latest_verdict="NEEDS_CHANGES",
        latest_head_sha=head,
        current_head_sha=head,
    )


def _preserved(tmp_path: Path, name: str = "pr-2352") -> Path:
    """A preserved checkout that is really on disk, as a live deferral's is.

    Its presence is load-bearing: the driver reads an absent one as the
    operator's discard, so a fixture that invents a path would exercise the
    discard branch while claiming to test the hold.
    """
    path = tmp_path / "preserved" / name
    path.mkdir(parents=True)
    return path


def _held_reason(checkout: str | Path) -> str:
    """The hold reason a live deferral earns, as an operator surface echoes it.

    Composed through the production builder rather than spelled out here: the
    reason rides the operator-facing cap (``safe_hold_reason``), so whether a
    path this long survives it whole is decided by the operator's TMPDIR. Spelt
    out, these assertions passed on the Linux verify VM and failed on a Mac
    with a 120-char scratch path (#2436).
    :func:`test_the_hold_reason_keeps_the_checkout_a_person_must_open` pins
    what the cap may drop from it, which is the part that matters.
    """
    return publication_held_hold_reason(str(checkout))


def _seed_deferral(
    store: Any,
    *,
    run_id: str = "rem-1",
    head: str = HEAD,
    checkout: str | Path,
) -> None:
    store.save(_remediation_row(run_id, status="awaiting_publication"))
    store.set_meta(
        deferred_publication_key(run_id),
        json.dumps({"head": head, "checkout": str(checkout), "pr": 2352}),
    )


def _drive(store: Any, tmp_path: Path, *, head: str = HEAD) -> tuple[Any, list[Any]]:
    """One review-stage pass over the node, with the remediator recording calls."""
    calls: list[RemediationContext] = []
    facts = _facts(head_oid=head)
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=next((h.reason for h in store.list_epic_holds(EPIC)), None),
        review=_needs_changes_overlay(head),
    )
    outcome = drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(adversarial=True, blocking=True, max_rounds=8),
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


def test_the_tick_after_a_deferral_holds_and_dispatches_nothing(
    tmp_path: Path,
) -> None:
    """AC2, second half. #2352 dispatched a fresh remediation here, three times,
    each one moving the previous preserved checkout aside."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        checkout = _preserved(tmp_path)
        _seed_deferral(store, checkout=checkout)

        outcome, calls = _drive(store, tmp_path)

        assert calls == [], "the round's work is preserved, not missing"
        assert outcome.held == [ITEM]
        assert outcome.remediating == []
        reasons = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reasons == [_held_reason(checkout)]
        assert holds_node_out_of_frontier(reasons[0]), (
            "an unmatched reason leaves the node dispatchable, which is the "
            "loop this hold exists to stop"
        )


def test_the_hold_reason_keeps_the_checkout_a_person_must_open(
    tmp_path: Path,
) -> None:
    """#2436: the cap may shorten this reason, never un-name its checkout.

    The operator-facing cap used to cut the END of a long line, so a deferral
    preserved under a long scratch path was held under
    ``publication_held: /long/scratch/...deferral0...`` — every echoed surface
    dropped the one directory the documented recovery tells a person to open
    (and five tests here, which spelt the full path out, failed with it).
    """
    deep = tmp_path / ("scratch-" + "x" * 90) / "preserved" / "pr-2352"

    reason = publication_held_hold_reason(str(deep))

    assert len(str(deep)) > HOLD_REASON_MAX_CHARS, "this path must pass the cap"
    assert len(reason) <= HOLD_REASON_MAX_CHARS, "an echoed line stays one line"
    assert reason.startswith(f"{PUBLICATION_HELD_HOLD_PREFIX}: "), (
        "the prefix routes the hold out of the frontier, so it is never cut"
    )
    assert holds_node_out_of_frontier(reason)
    assert reason.endswith("/preserved/pr-2352"), (
        "the checkout a person has to open survives the cap"
    )
    assert safe_hold_reason(reason) == reason, (
        "every surface re-flattens the stored reason, so a capped one is stable"
    )


def test_a_deferral_against_an_older_head_does_not_hold_the_new_round(
    tmp_path: Path,
) -> None:
    """The release path. An operator who publishes the preserved checkout moves
    the head; the stale row must not wedge the round that follows."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha="beef2222", verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        _seed_deferral(store, head=HEAD, checkout=_preserved(tmp_path))

        outcome, calls = _drive(store, tmp_path, head="beef2222")

        assert [c.item_id for c in calls] == [ITEM]
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_deferral_with_no_recorded_checkout_still_holds(tmp_path: Path) -> None:
    """Fail closed on the money. A row with no evidence is still a remediation
    that published nothing, so it stops the node — it just cannot name a path."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        store.save(_remediation_row("rem-1", status="awaiting_publication"))

        _outcome, calls = _drive(store, tmp_path)

        assert calls == []
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            f"{PUBLICATION_HELD_HOLD_PREFIX}: preserved checkout unknown"
        ]


def test_a_successful_remediation_round_is_unaffected(tmp_path: Path) -> None:
    """The guard reads `awaiting_publication`, not "a remediation ran": a round
    whose remediation published normally still remediates the next verdict."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        store.save(_remediation_row("rem-1", status="success"))

        _outcome, calls = _drive(store, tmp_path)

        assert [c.item_id for c in calls] == [ITEM]
        assert store.list_epic_holds(EPIC) == []


def test_discarding_the_preserved_checkout_lets_the_unhold_stick(
    tmp_path: Path,
) -> None:
    """The documented discard recovery, end to end (review round 19).

    `docs/cli/epic.md`, `docs/how-it-works.md`, `waits.py` and the console's
    hold reasons all print "publish or discard the preserved checkout, then
    kew epic unhold". Publishing moves the head, which the test above covers;
    a DISCARD moves nothing and the run row stays `awaiting_publication`
    forever — so keyed on the row alone the hold was re-derived on the very
    next tick and the node was wedged for good.
    """
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        checkout = _preserved(tmp_path)
        _seed_deferral(store, checkout=checkout)
        _drive(store, tmp_path)
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            _held_reason(checkout)
        ]

        # The operator discards the preserved work, then `kew epic unhold`
        # (`remove_epic_hold` is the primitive that verb calls).
        shutil.rmtree(checkout)
        store.remove_epic_hold(EPIC, ITEM)

        outcome, calls = _drive(store, tmp_path)

        assert [c.item_id for c in calls] == [ITEM], (
            "nothing is preserved to clobber any more, so the standing "
            "findings are the node's own work again"
        )
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == [], "the release has to be durable"


def test_a_bare_unhold_with_the_checkout_still_on_disk_is_re_held(
    tmp_path: Path,
) -> None:
    """The other half of the contract, which the fix must not trade away: a
    release that dealt with nothing is re-derived, like a base-integration
    conflict's. The preserved work is still there and a remediation dispatched
    over it would move it aside — #2362's loop, one tick slower."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        checkout = _preserved(tmp_path)
        _seed_deferral(store, checkout=checkout)
        _drive(store, tmp_path)
        store.remove_epic_hold(EPIC, ITEM)

        outcome, calls = _drive(store, tmp_path)

        assert calls == []
        assert outcome.held == [ITEM]
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            _held_reason(checkout)
        ]


def test_a_relative_recorded_checkout_is_never_read_as_discarded(
    tmp_path: Path,
) -> None:
    """A recorded path that is not absolute resolves against whatever directory
    the tick runs in, so its absence proves nothing. It holds."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        _seed_deferral(store, checkout="preserved/pr-2352")

        outcome, calls = _drive(store, tmp_path)

        assert calls == []
        assert outcome.held == [ITEM]
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            f"{PUBLICATION_HELD_HOLD_PREFIX}: preserved/pr-2352"
        ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses an unreadable parent")
def test_an_unreadable_checkout_is_not_read_as_discarded(tmp_path: Path) -> None:
    """Absent and unreadable are different answers. A parent the driver may not
    traverse says nothing about whether the work is still there, and guessing
    "discarded" would dispatch a remediation over it."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        checkout = _preserved(tmp_path)
        _seed_deferral(store, checkout=checkout)
        checkout.parent.chmod(0o000)
        try:
            outcome, calls = _drive(store, tmp_path)
        finally:
            checkout.parent.chmod(0o755)

        assert calls == []
        assert outcome.held == [ITEM]
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            _held_reason(checkout)
        ]


def _summary(store: Any) -> Any:
    """`kew epic status` / `kew epic watch`'s read-only snapshot of the node."""
    deps = FakeSource(nodes=[_node(ITEM)], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(issue_facts_by_num={int(ITEM): _facts()})
    return tick_mod.summarize(
        EPIC,
        graph,
        config=DispatchConfig(
            review=ReviewConfig(adversarial=True, blocking=True, max_rounds=8),
            epic=EpicConfig(stall_days=3),
        ),
        store=store,
        facts=facts,
        now=NOW,
    )


def test_watch_emits_publication_held_for_the_deferred_round(tmp_path: Path) -> None:
    """AC2, the operator surface. `kew epic watch` emitted NOTHING for three
    runs; it now emits one `publication_held` event naming the checkout."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        checkout = _preserved(tmp_path)
        _seed_deferral(store, checkout=checkout)
        _drive(store, tmp_path)

        summary = _summary(store)

    assert summary.held == (ITEM,)
    events = attention_events(EPIC, summary)
    assert [(e.kind, e.item) for e in events] == [
        (AttentionKind.PUBLICATION_HELD, ITEM)
    ]
    assert events[0].reason == _held_reason(checkout)
    assert events[0].reason.endswith(f"preserved/{checkout.name}"), (
        "the event names the checkout to open, whatever the cap dropped in front"
    )
    assert events[0].action == (
        "review the preserved checkout its reason names, then kew epic unhold"
    )


def test_a_remediating_node_carrying_the_hold_reads_held() -> None:
    """The hold's whole job, stated once against the overlay: a REMEDIATING
    node carrying it reads HELD rather than due."""
    status = overlay_status(
        _facts(),
        [],
        reserved=False,
        held_reason=f"{PUBLICATION_HELD_HOLD_PREFIX}: /w/pr-2352",
        review=_needs_changes_overlay(),
    )

    assert status is DriverStatus.HELD
