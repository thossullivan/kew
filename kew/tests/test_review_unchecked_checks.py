"""A READY whose reviewer could not run some checks waits for them (#3421).

The reviewer runs offline, with no browser, Docker or network, and a READY
needs one executed command. After #3419 every verdict names what it could not
check in ``not_checked``; this pins the one rule every merge path applies to
the sidecar of the round it merges H on — the latest READY, or an accepted
NEEDS_CHANGES:

- nothing listed, or a sidecar written before #3419 (no key): no refusal;
- ``full_suite``: no refusal where the merge gate is itself a ``kew verify``,
  held like Docker on the no-queue path;
- ``browser``: refused until a passing browser-checked verify of H is on
  record — while H's diff touches what ``kew verify``'s ui-check covers; on
  any other diff no verify ever records one, so it is held like Docker;
- ``docker``, ``network``, ``host``, ``other``: held under the gate hold
  reason, released for H alone by ``kew epic unhold``.

Real run store, real sidecar files, the real ``merge_pass``; only ``gh`` is
faked.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.unchecked_reviews import record_reviewed

from kew import subprocess_utils
from kew.audit import Actor, emit
from kew.epic_holds import (
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    holds_node_out_of_frontier,
    merge_gate_ack_key,
    merge_gate_hold_reason,
)
from kew.graph.driver.merge import merge_pass
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BROWSER_UNCHECKED,
    _unchecked_refusal,
    browser_checked,
    review_unchecked_kinds,
)
from kew.merge_verify import (
    VERIFY_EVENT,
    resolve_candidate,
    touches_ui_check,
    verify_answers_browser,
)
from kew.service.attention import wait_sentence

store = _fx.store

EPIC = "epic/1"
ITEM = "101"
PR = 501
HEAD = "a" * 40
NEXT_HEAD = "b" * 40
BASE_SHA = "c" * 40
_NO_KEY = object()


def _ready(
    tmp_path: Path,
    store: Any,
    not_checked: Any = _NO_KEY,
    *,
    head: str = HEAD,
    digest: str | None = None,
) -> None:
    """Record a READY round on *head* with its sidecar, as the review stage does."""
    verdict: dict[str, Any] = {"verdict": "READY"}
    if not_checked is not _NO_KEY:
        verdict["not_checked"] = not_checked
    sidecar = tmp_path / f"review-sidecar-{head[:8]}.json"
    sidecar.write_text(json.dumps(verdict))
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="READY",
        review_mode="full",
        sidecar_path=str(sidecar),
        sidecar_digest=digest or hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    )


def _entry(kind: str) -> dict[str, str]:
    return {"check": f"the {kind} check", "kind": kind, "reason": "sandbox"}


def _verified(
    store: Any,
    *,
    ref_sha: str = "d" * 40,
    ref_parents: list[str] | None = None,
    ui_check: bool = True,
    modules: list[str] | None = None,
    outcome: str = "passed",
) -> None:
    """A ``kew verify`` the verify host recorded."""
    payload: dict[str, Any] = {
        "ref_sha": ref_sha,
        "ref_parents": ref_parents if ref_parents is not None else [],
        "ui_check": ui_check,
        "ui_check_modules": modules,
        "outcome": outcome,
    }
    emit(store, VERIFY_EVENT, actor=Actor("policy", "verify-host"), payload=payload)


def _refusal(
    store: Any,
    *,
    head: str = HEAD,
    verify_gate: bool = True,
    ui_checkable: bool = True,
) -> Any:
    """The refusal for *head*; *ui_checkable* says its diff touches what
    ``kew verify``'s ui-check covers (a console change, by default)."""
    return _unchecked_refusal(
        store,
        provider="github",
        epic_ref=EPIC,
        item_id=ITEM,
        head_oid=head,
        verify_gate=verify_gate,
        ui_checkable=ui_checkable,
    )


def _holds(store: Any) -> list[str]:
    return [h.reason for h in store.list_epic_holds(EPIC) if h.item_id == ITEM]


def _unhold(store: Any) -> Any:
    """Exactly what `kew epic unhold` does to release a hold (cli/epic.py)."""
    return store.remove_epic_hold(
        EPIC,
        ITEM,
        receipt_key=merge_gate_ack_key(EPIC, ITEM),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )


@pytest.mark.parametrize("not_checked", [[], _NO_KEY], ids=["empty", "pre-r32a"])
def test_a_ready_that_names_nothing_unchecked_is_not_refused(
    tmp_path: Path, store: Any, not_checked: Any
) -> None:
    """A sidecar with no ``not_checked`` key was written before #3419, when
    no reviewer was asked: it reads as naming nothing, like an empty list."""
    _ready(tmp_path, store, not_checked)

    assert _refusal(store) is None
    assert _refusal(store, verify_gate=False) is None
    assert _holds(store) == []


def test_full_suite_adds_no_hold_where_the_gate_is_a_kew_verify(
    tmp_path: Path, store: Any
) -> None:
    _ready(tmp_path, store, [_entry("full_suite")])

    assert _refusal(store, verify_gate=True) is None
    assert _holds(store) == []


def test_full_suite_is_held_like_docker_where_no_verify_runs(
    tmp_path: Path, store: Any
) -> None:
    _ready(tmp_path, store, [_entry("full_suite")])

    refused = _refusal(store, verify_gate=False)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: full_suite")
    assert (refused.ok, refused.action, refused.reason) == (False, "held", reason)
    assert _holds(store) == [reason]


@pytest.mark.parametrize("kind", ["docker", "network", "host", "other"])
def test_a_person_check_holds_until_unhold_releases_that_exact_head(
    tmp_path: Path, store: Any, kind: str
) -> None:
    _ready(tmp_path, store, [_entry(kind), _entry("full_suite")])

    refused = _refusal(store)

    reason = merge_gate_hold_reason(HEAD, f"review sandbox could not run: {kind}")
    assert (refused.action, refused.reason) == ("held", reason)
    assert _holds(store) == [reason]
    assert holds_node_out_of_frontier(reason)

    assert _unhold(store) is not None
    assert _refusal(store) is None
    assert _holds(store) == []

    # A push moves the head: the receipt was for HEAD, so the hold is back.
    _ready(tmp_path, store, [_entry(kind)], head=NEXT_HEAD)
    again = _refusal(store, head=NEXT_HEAD)
    assert again.reason == merge_gate_hold_reason(
        NEXT_HEAD, f"review sandbox could not run: {kind}"
    )
    assert _holds(store) == [again.reason]


def test_every_person_kind_is_named_once_in_the_hold(
    tmp_path: Path, store: Any
) -> None:
    _ready(
        tmp_path,
        store,
        [_entry("network"), _entry("docker"), {**_entry("docker"), "kind": " Docker "}],
    )

    assert _refusal(store).reason == merge_gate_hold_reason(
        HEAD, "review sandbox could not run: docker, network"
    )


@pytest.mark.parametrize(
    "not_checked",
    [
        [{"check": "x", "reason": "y"}],
        [{"check": "x", "kind": "gpu", "reason": "y"}],
        ["docker"],
        {"kind": "docker"},
    ],
    ids=["no-kind", "invented-kind", "not-an-object", "not-a-list"],
)
def test_an_entry_kew_cannot_classify_waits_for_a_person(
    tmp_path: Path, store: Any, not_checked: Any
) -> None:
    _ready(tmp_path, store, not_checked)

    assert _refusal(store).reason == merge_gate_hold_reason(
        HEAD, "review sandbox could not run: other"
    )


def test_a_sidecar_off_its_digest_waits_for_a_person(
    tmp_path: Path, store: Any
) -> None:
    """Fail closed: a sidecar kew cannot stand behind may list anything."""
    _ready(tmp_path, store, [], digest="0" * 64)

    assert review_unchecked_kinds(store, ITEM, HEAD) == ("other",)
    assert _refusal(store).action == "held"


@pytest.mark.parametrize("accepted", [False, True], ids=["ready", "accepted"])
def test_a_missing_sidecar_waits_for_a_person_until_unhold(
    tmp_path: Path, store: Any, accepted: bool
) -> None:
    """A sidecar FILE that is gone leaves what its reviewer could not run
    unknown, not empty: it holds as ``other``, and unhold releases the head."""
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC,
        item_id=ITEM,
        head=HEAD,
        kinds=["docker", "network"],
        accepted=accepted,
    )
    Path(store.latest_review_round(ITEM).sidecar_path).unlink()

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: other")
    assert review_unchecked_kinds(store, ITEM, HEAD) == ("other",)
    assert _refusal(store).reason == reason
    assert _refusal(store, verify_gate=False).reason == reason
    assert _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _refusal(store) is None


@pytest.mark.parametrize("accepted", [False, True], ids=["ready", "accepted"])
def test_a_sidecar_rewritten_off_its_digest_waits_for_a_person(
    tmp_path: Path, store: Any, accepted: bool
) -> None:
    """A file that is there but no longer the bytes the round recorded may
    have been rewritten to drop what the reviewer could not run."""
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC,
        item_id=ITEM,
        head=HEAD,
        kinds=["docker"],
        accepted=accepted,
    )
    sidecar = Path(store.latest_review_round(ITEM).sidecar_path)
    # Rewritten in place to drop what the reviewer could not run.
    sidecar.write_text(json.dumps({"verdict": "READY", "not_checked": []}))

    assert review_unchecked_kinds(store, ITEM, HEAD) == ("other",)
    assert _refusal(store).reason == merge_gate_hold_reason(
        HEAD, "review sandbox could not run: other"
    )


@pytest.mark.parametrize("accepted", [False, True], ids=["ready", "accepted"])
def test_the_no_queue_path_holds_a_round_whose_sidecar_is_missing(
    tmp_path: Path, store: Any, accepted: bool
) -> None:
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC,
        item_id=ITEM,
        head=HEAD,
        kinds=["docker", "network"],
        accepted=accepted,
    )
    Path(store.latest_review_round(ITEM).sidecar_path).unlink()
    forge = _Forge()

    held = _merge_pass(store, forge)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: other")
    assert (held.action, held.reason) == ("held", reason)
    assert forge.merges == [] and _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _merge_pass(store, forge).action == "merged"
    assert len(forge.merges) == 1


def test_only_the_latest_ready_on_the_head_counts(tmp_path: Path, store: Any) -> None:
    _ready(tmp_path, store, [_entry("docker")])

    # The latest round is on another head: nothing on HEAD answers anything.
    _ready(tmp_path, store, [], head=NEXT_HEAD)

    assert review_unchecked_kinds(store, ITEM, NEXT_HEAD) == ()
    assert _refusal(store, head=NEXT_HEAD) is None


def test_a_browser_check_waits_for_a_browser_checked_verify_of_the_merge(
    tmp_path: Path, store: Any
) -> None:
    _ready(tmp_path, store, [_entry("browser")])

    refused = _refusal(store)

    assert (refused.action, refused.reason) == ("skip", MERGE_VERIFY_BROWSER_UNCHECKED)
    assert _holds(store) == [], "a browser wait is re-read, never a hold row"

    # A combined tree (main merged into HEAD) verified with the UI check.
    _verified(store, ref_parents=[HEAD, BASE_SHA], modules=["chat"])

    assert _refusal(store) is None


def test_a_browser_check_is_lifted_by_a_full_ui_check_of_the_head_itself(
    tmp_path: Path, store: Any
) -> None:
    """A verify of the head itself lifts it too: every module
    (``ui_check_modules`` None) ran there."""
    _ready(tmp_path, store, [_entry("browser")])
    _verified(store, ref_sha=HEAD)

    assert browser_checked(store, HEAD)
    assert _refusal(store) is None


@pytest.mark.parametrize(
    "verify",
    [
        {"ref_parents": [HEAD, BASE_SHA], "modules": []},
        {"ref_parents": [HEAD, BASE_SHA], "ui_check": False},
        {"ref_parents": [HEAD, BASE_SHA], "outcome": "failed"},
        {"ref_parents": [NEXT_HEAD, BASE_SHA], "modules": ["chat"]},
    ],
    ids=["no-module-ran", "no-ui-check", "failed", "another-head"],
)
def test_a_verify_that_checked_no_browser_on_the_head_does_not_lift_it(
    tmp_path: Path, store: Any, verify: dict[str, Any]
) -> None:
    _ready(tmp_path, store, [_entry("browser")])
    _verified(store, **verify)

    assert not browser_checked(store, HEAD)
    assert _refusal(store).reason == MERGE_VERIFY_BROWSER_UNCHECKED


def test_unhold_never_answers_the_browser_check(tmp_path: Path, store: Any) -> None:
    _ready(tmp_path, store, [_entry("browser"), _entry("docker")])

    assert _refusal(store).reason == merge_gate_hold_reason(
        HEAD, "review sandbox could not run: docker"
    )
    assert _unhold(store) is not None

    assert _refusal(store).reason == MERGE_VERIFY_BROWSER_UNCHECKED
    _verified(store, ref_parents=[BASE_SHA, HEAD], modules=["chat"])
    assert _refusal(store) is None


def test_a_browser_check_no_verify_of_the_diff_can_run_is_held_for_a_person(
    tmp_path: Path, store: Any
) -> None:
    """``kew verify`` runs ui-check only on a diff that touches ``frontend/``:
    on any other no verify ever records the browser check, so a wait would
    never end. It is held like Docker, named with it, and released by the
    same head receipt."""
    _ready(tmp_path, store, [_entry("docker"), _entry("browser")])

    refused = _refusal(store, ui_checkable=False)

    reason = merge_gate_hold_reason(
        HEAD, "review sandbox could not run: browser, docker"
    )
    assert (refused.action, refused.reason) == ("held", reason)
    assert _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _refusal(store, ui_checkable=False) is None


def test_a_ui_check_on_record_answers_no_browser_check_it_does_not_cover(
    tmp_path: Path, store: Any
) -> None:
    """The console's ui-check passed for the head, but on a diff it does not
    answer (a ``www/`` page, say) it checked nothing the reviewer missed."""
    _ready(tmp_path, store, [_entry("browser")])
    _verified(store, ref_sha=HEAD)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: browser")
    assert _refusal(store, ui_checkable=False).reason == reason
    assert _refusal(store, ui_checkable=True) is None


@pytest.mark.parametrize(
    ("paths", "covered"),
    [
        (["frontend/src/lib/chat.ts"], True),
        (["frontend"], True),
        (["kew/src/kew/widget.py", "frontend/package.json"], True),
        (["kew/src/kew/widget.py"], False),
        (["frontend-old/app.ts", "www/frontend/app.ts"], False),
        ([], False),
    ],
)
def test_the_ui_check_rule_is_a_path_under_frontend(
    paths: list[str], covered: bool
) -> None:
    assert touches_ui_check(paths) is covered


def test_resolve_candidate_reads_the_ui_check_rule_off_both_sides_of_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization: a file moved out of ``frontend/`` still moved the
    console. ``resolve_candidate`` now reads :func:`touches_ui_check` off both
    sides of the diff, and gives the answer the ``-- frontend`` pathspec it
    replaced gave; read off the rename-detected ``changed_paths`` it would not."""
    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "frontend").mkdir()
    (repo / "frontend" / "chat.ts").write_text("export const chat = 1;\n" * 20)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "move"], check=True)
    (repo / "docs").mkdir()
    subprocess.run([*git, "mv", "frontend/chat.ts", "docs/chat.ts"], check=True)
    subprocess.run([*git, "commit", "-qm", "move"], check=True)
    found = shutil.which("git")
    assert found is not None, "git required"
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", found)

    candidate = resolve_candidate(repo, ref="move", base="main")

    assert candidate.frontend_changed and candidate.runs_ui_check


@pytest.mark.parametrize("verify_gate", [True, False], ids=["queue", "no-queue"])
def test_an_accepted_needs_changes_is_read_for_what_it_could_not_check(
    tmp_path: Path, store: Any, verify_gate: bool
) -> None:
    """Every merge path takes an accepted NEEDS_CHANGES (#2112) as READY: its
    reviewer ran in the same sandbox, so its ``not_checked`` holds the same."""
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC,
        item_id=ITEM,
        head=HEAD,
        kinds=["docker"],
        accepted=True,
    )

    assert review_unchecked_kinds(store, ITEM, HEAD) == ("docker",)
    assert _refusal(store, verify_gate=verify_gate).reason == merge_gate_hold_reason(
        HEAD, "review sandbox could not run: docker"
    )


def test_a_needs_changes_nobody_accepted_names_nothing_here(
    tmp_path: Path, store: Any
) -> None:
    """No merge path merges on it: the verdict rule refuses it upstream."""
    sidecar = tmp_path / "needs-changes.json"
    sidecar.write_text(
        json.dumps({"verdict": "NEEDS_CHANGES", "not_checked": [_entry("docker")]})
    )
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=HEAD,
        verdict="NEEDS_CHANGES",
        review_mode="full",
        sidecar_path=str(sidecar),
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    )

    assert review_unchecked_kinds(store, ITEM, HEAD) == ()


# ── the merge path that runs no merge queue ─────────────────────────────────


def _pr(head: str) -> dict[str, Any]:
    return {
        "number": PR,
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "headRefOid": head,
        "headRefName": "agent/issue-101",
        "title": "Add the widget",
        "latestReviews": [
            {
                "state": "APPROVED",
                "submittedAt": "2099-01-01T00:00:00Z",
                "author": {"login": "alice", "is_bot": False, "__typename": "User"},
            }
        ],
        "author": {"login": "agent"},
    }


class _Forge:
    """``gh`` over one open PR: records merges, closes the issue on request."""

    def __init__(self, head: str = HEAD) -> None:
        self.reader = _fx.FakeFactsReader(
            issue_facts_by_num={int(ITEM): _fx._facts(int(ITEM), pr_number=PR)},
            pr_facts_by_num={PR: _pr(head)},
            permissions_by_login={"alice": "write"},
        )
        self.merges: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            self.merges.append(cmd)
        if cmd[:3] == ["gh", "issue", "close"]:
            self.reader.issue_facts_by_num[int(ITEM)] = _fx._facts(
                int(ITEM), state="CLOSED", pr_number=PR
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _merge_pass(store: Any, forge: _Forge) -> Any:
    [verdict] = merge_pass(
        [ITEM],
        repo="o/r",
        base="main",
        epic_ref=EPIC,
        reader=forge.reader,
        approvers=["alice"],
        store=store,
        run=forge,
        blocking_review=True,
        gated_checks=[],
        merge_verify=None,
    )
    return verdict


def test_the_no_queue_path_merges_a_ready_that_names_nothing(
    tmp_path: Path, store: Any
) -> None:
    _ready(tmp_path, store, [])
    forge = _Forge()

    assert _merge_pass(store, forge).action == "merged"
    assert len(forge.merges) == 1


def test_the_no_queue_path_holds_full_suite_until_unhold(
    tmp_path: Path, store: Any
) -> None:
    """No ``kew verify`` runs before this path's ``gh pr merge``."""
    _ready(tmp_path, store, [_entry("full_suite")])
    forge = _Forge()

    held = _merge_pass(store, forge)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: full_suite")
    assert (held.action, held.reason) == ("held", reason)
    assert forge.merges == [] and _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _merge_pass(store, forge).action == "merged"
    assert len(forge.merges) == 1


def test_the_no_queue_path_holds_a_browser_check_for_a_person(
    tmp_path: Path, store: Any
) -> None:
    """This path reads no diff, so it cannot know a ``kew verify`` of the head
    would ever run ui-check (on a PR that touches nothing under ``frontend/``
    none does): a wait could never end. A person runs the browser check, and
    the head's receipt releases it."""
    _ready(tmp_path, store, [_entry("browser")])
    forge = _Forge()

    held = _merge_pass(store, forge)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: browser")
    assert (held.action, held.reason) == ("held", reason)
    assert forge.merges == [] and _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _merge_pass(store, forge).action == "merged"
    assert len(forge.merges) == 1


def test_the_no_queue_path_holds_a_browser_check_whatever_verify_is_on_record(
    tmp_path: Path, store: Any
) -> None:
    """It reads no diff, so it cannot know the console's ui-check covers the
    browser check its reviewer missed."""
    _ready(tmp_path, store, [_entry("browser")])
    _verified(store, ref_sha=HEAD, modules=["chat"])
    forge = _Forge()

    held = _merge_pass(store, forge)

    assert held.action == "held" and forge.merges == []
    assert _holds(store) == [
        merge_gate_hold_reason(HEAD, "review sandbox could not run: browser")
    ]


def test_the_no_queue_path_holds_an_accepted_needs_changes_naming_docker(
    tmp_path: Path, store: Any
) -> None:
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC,
        item_id=ITEM,
        head=HEAD,
        kinds=["docker"],
        accepted=True,
    )
    forge = _Forge()

    held = _merge_pass(store, forge)

    reason = merge_gate_hold_reason(HEAD, "review sandbox could not run: docker")
    assert (held.action, held.reason) == ("held", reason)
    assert forge.merges == [] and _holds(store) == [reason]

    assert _unhold(store) is not None
    assert _merge_pass(store, forge).action == "merged"


# ── what a ui-check verify answers ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("paths", "answered"),
    [
        (["frontend/src/lib/chat.ts"], True),
        (["frontend/src/lib/chat.ts", "kew/src/kew/widget.py"], True),
        (["frontend/src/lib/chat.ts", "www/index.html"], False),
        (["www/index.html"], False),
        (["kew/src/kew/widget.py"], False),
        (["frontend/src/lib/chat.ts", "www-old/index.html"], True),
    ],
)
def test_a_ui_check_verify_answers_the_console_and_never_www(
    paths: list[str], answered: bool
) -> None:
    """``kew verify`` runs the console's ui-check, never www's own
    ``npm run browser-check``."""
    assert verify_answers_browser(paths) is answered


# ── what a release answers ──────────────────────────────────────────────────


def test_a_release_answers_only_the_checks_its_reason_named(
    tmp_path: Path, store: Any
) -> None:
    """A re-review of the released head that adds a check nobody released is
    held again, under one reason naming everything; the next release covers
    it."""
    _ready(tmp_path, store, [_entry("docker")])
    assert _refusal(store).action == "held"
    assert _unhold(store) is not None
    assert _refusal(store) is None

    _ready(tmp_path, store, [_entry("docker"), _entry("network")])

    reason = merge_gate_hold_reason(
        HEAD, "review sandbox could not run: docker, network"
    )
    assert _refusal(store).reason == reason
    assert _holds(store) == [reason]
    assert _unhold(store) is not None
    assert _refusal(store) is None


def test_a_release_still_answers_a_re_review_that_names_fewer_checks(
    tmp_path: Path, store: Any
) -> None:
    _ready(tmp_path, store, [_entry("docker"), _entry("network")])
    assert _refusal(store).action == "held"
    assert _unhold(store) is not None

    _ready(tmp_path, store, [_entry("network")])

    assert _refusal(store) is None
    assert _holds(store) == []


# ── how a person reads the hold ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("named", "said"),
    [
        ("scripts/verify.sh", "the PR edits the merge gate's own definition"),
        (
            "review sandbox could not run: docker",
            "its review could not run some checks",
        ),
        (
            "unanswered test-integrity flags asserts_removed",
            "its review left test-integrity flags unanswered",
        ),
        (
            "scripts/verify.sh; review sandbox could not run: docker",
            "the PR edits the merge gate's own definition and its review could "
            "not run some checks",
        ),
    ],
    ids=["gate-path", "unrun-check", "integrity-flag", "both"],
)
def test_a_merge_gate_hold_is_said_as_what_its_reason_names(
    named: str, said: str
) -> None:
    sentence = wait_sentence("held", merge_gate_hold_reason(HEAD, named))

    assert sentence == f"{said}: {named}"
