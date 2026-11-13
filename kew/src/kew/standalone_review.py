"""One governed review round on a standalone PR (#2556).

The adversarial review stage runs inside the epic driver's tick, so a PR
dispatched on its own (``kew dispatch <n>``) had no kew path to a review
verdict: it was reviewed by hand, and the verdict reached neither the ledger's
review rounds nor the PR thread as a kew verdict. ``kew review --governed``
is that path. It runs the stage's own per-node action
(:func:`kew.graph.driver.review_stage.drive_review_node`) — the same launcher,
containment, sidecar parsing, round recording, round cap, stop rule and verdict
surfaces — with the item id read off the PR's head branch.

Everything the epic overlay decides before a tick reaches a node is checked
here instead, before any reviewer runs: the PR is open, its head branch is a
kew branch for a valid item, kew's recorded evidence says kew published that
exact head, the item is not held, no reviewer already holds it, no running
epic drives it (#2584; ``force`` overrides only this one), its review pool has
headroom for a reviewer round, and no verdict already stands on the head. Each
refusal is one sentence and exits 2.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.budget import (
    BudgetExceeded,
    check_review_budget,
    check_window_budget,
    window_budget_hold_reason,
)
from kew.command_outcome import (
    COULD_NOT_RUN_EXIT_CODE,
    FINDINGS_EXIT_CODE,
    SUCCESS_EXIT_CODE,
)
from kew.epic_holds import REVIEW_RETRYING_WAIT_PREFIX
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.review_stage import (
    Remediator,
    ReviewStageOutcome,
    drive_review_node,
)
from kew.ids import (
    GITHUB_ID_RE,
    InvalidItemIdError,
    branch_slug,
    item_ref,
    provider_from_config,
    validate_item_id,
)
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication_canonical import PublicationHeld
from kew.publication_credentials import (
    CredentialPublicationRefused,
    recorded_head_producer,
)
from kew.review_occupancy import reservation_holder
from kew.subprocess_utils import run as subprocess_run

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.review_dispatch import ReviewerLauncher
    from kew.run_store import ReviewRound, RunStore

STANDALONE_REVIEW_EPIC_REF = "standalone-review"
"""The epic ref a standalone PR's review rounds and holds are recorded under.

Rounds are keyed on the item, so a later epic that adopts the item reads the
same history. A hold this verb records is released with
``kew epic unhold standalone-review <item>``."""

_PR_FIELDS = (
    "number,state,headRefName,headRefOid,baseRefName,mergeStateStatus,"
    "reviewDecision,url"
)


@dataclass(frozen=True)
class PrUnreadable:
    """GitHub could not give the PR's facts, and why (never an empty PR)."""

    reason: str


PrReader = Callable[[int, str], "dict[str, Any] | PrUnreadable"]
LabelsReader = Callable[[str, str], "list[str] | PrUnreadable"]

EPIC_LABEL_PREFIX = "epic:"


@dataclass(frozen=True)
class StandaloneReviewResult:
    """What the verb prints and the exit code it returns."""

    exit_code: int
    message: str


def _first_line(exc: BaseException) -> str:
    stderr = getattr(exc, "stderr", None)
    text = stderr if isinstance(stderr, str) and stderr.strip() else str(exc)
    lines = text.strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else 'no message'}"


def read_pr(pr_number: int, repo: str) -> dict[str, Any] | PrUnreadable:
    """The PR facts the review binds to, or why GitHub could not give them."""
    try:
        result = subprocess_run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                _PR_FIELDS,
            ],
            check=True,
        )
        parsed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return PrUnreadable(_first_line(exc))
    if not isinstance(parsed, dict):
        return PrUnreadable(f"gh returned {type(parsed).__name__}, not a PR object")
    return parsed


def read_item_labels(item_id: str, repo: str) -> list[str] | PrUnreadable:
    """The item's tracker labels, or why GitHub could not give them.

    Only a GitHub issue carries the ``epic:*`` label convention; any other
    item reads as unlabelled.
    """
    if not GITHUB_ID_RE.fullmatch(item_id):
        return []
    try:
        result = subprocess_run(
            ["gh", "issue", "view", item_id, "--repo", repo, "--json", "labels"],
            check=True,
        )
        parsed = json.loads(result.stdout)
        return [str(label["name"]) for label in parsed["labels"]]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return PrUnreadable(_first_line(exc))
    except (KeyError, IndexError) as exc:
        return PrUnreadable(f"gh returned no labels list ({exc!r})")


def _refused(message: str) -> StandaloneReviewResult:
    return StandaloneReviewResult(COULD_NOT_RUN_EXIT_CODE, message)


class NotAKewBranchError(ValueError):
    """The head branch is not ``<prefix><item>``; the message says which rule."""


def _branch_item(branch: str, *, prefix: str, provider: str) -> str:
    """The item a kew branch names, when it is exactly ``<prefix><slug>``."""
    if not branch.startswith(prefix):
        raise NotAKewBranchError(f"it does not start with {prefix!r}")
    try:
        item_id = validate_item_id(branch.removeprefix(prefix).upper(), provider)
    except InvalidItemIdError as exc:
        raise NotAKewBranchError(f"its suffix is not an item id: {exc}") from exc
    canonical = f"{prefix}{branch_slug(item_id)}"
    if canonical != branch:
        raise NotAKewBranchError(f"the branch for {item_id} is {canonical!r}")
    return item_id


def _branch_refusal(
    branch: str, pr_number: int, config: DispatchConfig
) -> str | StandaloneReviewResult:
    """The item the head branch names, or the refusal saying why it names none."""
    prefix = config.branches.prefix
    try:
        return _branch_item(
            branch, prefix=prefix, provider=provider_from_config(config)
        )
    except NotAKewBranchError as exc:
        return _refused(
            f"PR #{pr_number}: head branch {branch!r} is not a kew branch "
            f"({prefix}<item>; {exc}), so there is no item to review it for."
        )


def _publication_refusal(
    store: RunStore, pr_number: int, *, branch: str, head: str
) -> str | None:
    """Why kew will not review this head, or None when kew published it."""
    try:
        producer = recorded_head_producer(store, branch=branch, head_sha=head)
    except (CredentialPublicationRefused, PublicationHeld) as exc:
        return (
            f"PR #{pr_number}: kew's publication evidence for {branch} at "
            f"{head[:12]} cannot be read as one claim ({exc}), so it is not "
            "reviewed."
        )
    if producer is not None:
        return None
    return (
        f"PR #{pr_number}: kew did not publish head {head[:12]} of {branch} "
        "(no publication attestation names it), so kew does not review it; "
        "bind a head published by hand with `kew publication attest`."
    )


def _standing_verdict(latest: ReviewRound | None, head: str) -> str | None:
    """A verdict already standing on this head, which a new round would only
    pay to repeat."""
    if latest is None or latest.head_sha != head:
        return None
    if latest.verdict == "READY":
        return "READY"
    return latest.accepted_verdict


def _preconditions(
    pr: dict[str, Any] | PrUnreadable,
    pr_number: int,
    *,
    config: DispatchConfig,
    store: RunStore,
) -> tuple[str, NodeFacts] | StandaloneReviewResult:
    """The item and facts to review, or the refusal that stops before a reviewer."""
    if not config.review.adversarial:
        return _refused(
            "governed review is off in this config; set [review] adversarial = true."
        )
    if isinstance(pr, PrUnreadable):
        return _refused(
            f"PR #{pr_number}: could not read the PR from GitHub ({pr.reason})."
        )
    if pr.get("state") != "OPEN":
        return _refused(f"PR #{pr_number} is {pr.get('state')}, not open.")
    branch = str(pr.get("headRefName") or "")
    head = str(pr.get("headRefOid") or "")
    item_id = _branch_refusal(branch, pr_number, config)
    if isinstance(item_id, StandaloneReviewResult):
        return item_id
    if not head:
        return _refused(f"PR #{pr_number} has no head commit to review.")
    refusal = _publication_refusal(store, pr_number, branch=branch, head=head)
    if refusal is not None:
        return _refused(refusal)
    return item_id, _facts(pr, pr_number, item_id, head)


def _item_refusal(
    store: RunStore, pr_number: int, item_id: str, head: str
) -> StandaloneReviewResult | None:
    """The ledger state that already owns this item's next review action."""
    # Any epic's hold, not only this verb's: a hold is somebody parking the
    # item, and an epic that holds it would not review it either.
    for hold in store.list_all_epic_holds():
        if hold.item_id == item_id:
            return _refused(
                f"PR #{pr_number}: {item_ref(item_id)} is held ({hold.reason}); "
                f"release it with `kew epic unhold {hold.epic_ref} {item_id}`."
            )
    if any(
        res.item_id == item_id for res in store.list_epic_reservations(REVIEW_EPIC_REF)
    ):
        return _refused(
            f"PR #{pr_number}: a reviewer or remediator for {item_ref(item_id)} "
            f"is already running: {reservation_holder(store, item_id)}."
        )
    latest = store.latest_review_round(item_id)
    standing = _standing_verdict(latest, head)
    if standing is not None:
        assert latest is not None  # narrowed by _standing_verdict
        return StandaloneReviewResult(
            SUCCESS_EXIT_CODE,
            f"PR #{pr_number}: {standing} already stands on {head[:12]} "
            f"(round {latest.round}); nothing to review.",
        )
    return None


def _epic_refusal(
    store: RunStore, pr_number: int, item_id: str, labels: list[str] | PrUnreadable
) -> StandaloneReviewResult | None:
    """A running epic that drives this item, which owns its review rounds.

    A standalone round between that epic's ticks would record the item's rounds
    under two epic refs. An epic is running while kew holds driver state for it
    that has not signalled complete. Fails closed: labels that cannot be read
    refuse, since the check cannot be made.
    """
    if isinstance(labels, PrUnreadable):
        return _refused(
            f"PR #{pr_number}: could not read {item_ref(item_id)}'s labels to "
            f"check epic membership ({labels.reason}); pass --force to review "
            "it anyway."
        )
    for label in labels:
        if not label.startswith(EPIC_LABEL_PREFIX):
            continue
        epic_ref = label.removeprefix(EPIC_LABEL_PREFIX)
        state = store.get_epic_driver_state(epic_ref)
        if state is not None and state.last_signal != TerminalSignal.COMPLETE:
            return _refused(
                f"PR #{pr_number}: {item_ref(item_id)} is driven by epic "
                f"{epic_ref!r}, whose review stage reviews it; let the epic "
                "review it, or pass --force to review it here anyway."
            )
    return None


def _budget_refusal(
    store: RunStore, pr_number: int, item_id: str, head: str, config: DispatchConfig
) -> StandaloneReviewResult | None:
    """A reviewer round the item's review pool or the windows cannot pay for.

    The launcher makes the same check, but only after the review stage has
    published a pending status and taken the reservation; this refuses before
    either. Findings standing on this head are remediated, not reviewed, and
    the remediation pool is the remediator's own check.
    """
    latest = store.latest_review_round(item_id)
    if (
        latest is not None
        and latest.verdict == "NEEDS_CHANGES"
        and latest.head_sha == head
    ):
        return None
    cap = config.review.max_review_usd
    try:
        check_review_budget(store, item_id, cap)
    except BudgetExceeded as exc:
        return _refused(
            f"PR #{pr_number}: {item_ref(item_id)}'s review pool "
            f"${cap:.2f} is spent (${exc.spent_usd:.2f}); raise "
            "[review].max_review_usd to review it again."
        )
    # The daily and weekly caps stop a reviewer too (#3205).
    try:
        check_window_budget(
            store, config.budget, reservation_usd=cap, count_admitting_run=True
        )
    except BudgetExceeded as exc:
        return _refused(f"PR #{pr_number}: {window_budget_hold_reason(exc)}.")
    return None


def _facts(pr: dict[str, Any], pr_number: int, item_id: str, head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=pr.get("url"),
        review_decision=pr.get("reviewDecision"),
        merge_state_status=pr.get("mergeStateStatus"),
        head_oid=head,
        pr_pushed_at=None,
        pr_state="OPEN",
        pr_link_source="branch",
    )


def _result(
    outcome: ReviewStageOutcome, pr_number: int, item_id: str, store: RunStore
) -> StandaloneReviewResult:
    """Read the one action the stage took into a line and an exit code."""
    verdict = outcome.verdicts.get(item_id)
    findings = outcome.findings.get(item_id, 0)
    if item_id in outcome.residuals:
        return StandaloneReviewResult(
            SUCCESS_EXIT_CODE,
            f"PR #{pr_number}: {findings} finding(s) accepted as residuals, "
            f"filed as #{outcome.residuals[item_id]}.",
        )
    if item_id in outcome.held:
        hold = next(
            (
                h.reason
                for h in store.list_epic_holds(STANDALONE_REVIEW_EPIC_REF)
                if h.item_id == item_id
            ),
            "held",
        )
        return StandaloneReviewResult(
            FINDINGS_EXIT_CODE, f"PR #{pr_number}: HELD — {hold}."
        )
    if verdict == "READY":
        return StandaloneReviewResult(SUCCESS_EXIT_CODE, f"PR #{pr_number}: READY.")
    if verdict == "NEEDS_CHANGES":
        return StandaloneReviewResult(
            FINDINGS_EXIT_CODE,
            f"PR #{pr_number}: NEEDS_CHANGES with {findings} finding(s); run "
            f"`kew review --governed {pr_number}` again to remediate them.",
        )
    if verdict is not None:
        return StandaloneReviewResult(
            FINDINGS_EXIT_CODE,
            f"PR #{pr_number}: {verdict}; run `kew review --governed "
            f"{pr_number}` again for the next round.",
        )
    if item_id in outcome.remediating:
        return StandaloneReviewResult(
            SUCCESS_EXIT_CODE,
            f"PR #{pr_number}: remediation dispatched; run `kew review --governed "
            f"{pr_number}` again once its commit lands to re-review.",
        )
    return _no_verdict(outcome, pr_number, item_id)


def _no_verdict(
    outcome: ReviewStageOutcome, pr_number: int, item_id: str
) -> StandaloneReviewResult:
    if item_id in outcome.refused:
        reason = outcome.refused[item_id]
        if reason.startswith(REVIEW_RETRYING_WAIT_PREFIX):
            # The drive re-runs the round on its next tick; here nothing does
            # until the operator asks again (#3186).
            reason += f"; run `kew review --governed {pr_number}` again to re-review"
    elif item_id in outcome.unwired:
        reason = (
            "the head no longer merges into the base, or no reviewer is wired; "
            f"resolve it (`kew resolve {pr_number}`) and run again"
        )
    else:
        reason = "no review ran (another reviewer took the slot, or the fleet is full)"
    return StandaloneReviewResult(
        FINDINGS_EXIT_CODE, f"PR #{pr_number}: no verdict — {reason}."
    )


def run_standalone_review(
    pr_number: int,
    *,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    reviewer_launcher: ReviewerLauncher | None,
    remediator: Remediator | None,
    base: str = "main",
    pr_reader: PrReader = read_pr,
    labels_reader: LabelsReader = read_item_labels,
    force: bool = False,
    now: datetime | None = None,
) -> StandaloneReviewResult:
    """Run one governed review action on PR *pr_number* (see the module doc).

    *force* overrides only the refusal of an item a running epic drives.
    """
    pr = pr_reader(pr_number, repo)
    checked = _preconditions(pr, pr_number, config=config, store=store)
    if isinstance(checked, StandaloneReviewResult):
        return checked
    item_id, facts = checked
    assert facts.head_oid is not None  # _preconditions refuses a headless PR
    assert not isinstance(pr, PrUnreadable)  # and an unreadable one
    refusal = _item_refusal(store, pr_number, item_id, facts.head_oid)
    if refusal is None and not force:
        refusal = _epic_refusal(store, pr_number, item_id, labels_reader(item_id, repo))
    if refusal is None:
        refusal = _budget_refusal(store, pr_number, item_id, facts.head_oid, config)
    if refusal is not None:
        return refusal
    outcome = drive_review_node(
        item_id,
        facts,
        epic_ref=STANDALONE_REVIEW_EPIC_REF,
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        # The reviewer diffs against the branch the PR merges into.
        base=str(pr.get("baseRefName") or base),
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        worker_cap=config.fleet_concurrency(),
        now=now,
    )
    return _result(outcome, pr_number, item_id, store)
