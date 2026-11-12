"""The review stop rule: accept late low/medium findings as residuals (#2112).

Some PRs ran 10-14 governed rounds on low and medium findings. After round
``[review].stop_rule_after_rounds`` a ``NEEDS_CHANGES`` verdict whose findings
are all within ``[review].stop_rule_max_severity`` — ``low`` only, or ``low``
and ``medium`` — is recorded as READY-with-residuals: the driver
files the findings as one GitHub issue, comments on the PR with the link, and
attests a ``review_residuals_accepted`` event. That event is what the overlay
and the merge gate read as READY for the round it names. Any finding above the
configured ceiling, a finding whose severity is missing or unknown, or a
recorded acceptance criterion the reviewer judged ``fail`` (#2291) keeps the
node in remediation (fail closed).

Acceptance binds to a round, head and sidecar digest, so a push voids it and the
next round is reviewed afresh. The ISSUE, though, is keyed on the findings
themselves: a later round that re-reports the same unfixed findings reuses the
OPEN issue already filed for them, and records itself on it, instead of filing a
duplicate every round (#2152).

The reviewer's own ``NEEDS_CHANGES`` round and verdict event are never
rewritten: the acceptance is a separate, driver-attested fact bound to the
round, head SHA and sidecar digest it accepted — including in the ledger, where
it lands in its own column beside the verdict as returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from kew import subprocess_utils
from kew.audit import Actor, emit
from kew.ids import GITHUB_ID_RE, item_ref
from kew.review_contract import DECLARATION_FINDING_KIND

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, ReviewRound, RunStore

_logger = logging.getLogger(__name__)

READY_WITH_RESIDUALS = "READY_WITH_RESIDUALS"
"""The stop rule's disposition for a round it accepts."""

RESIDUALS_ACCEPTED_EVENT = "review_residuals_accepted"
"""Audit event binding an accepted round to its filed residual issue."""

RESIDUAL_ISSUE_LABEL = "bug"
"""The label residual issues carry: GitHub's default bug label."""

_NEEDS_CHANGES = "NEEDS_CHANGES"
_RESIDUAL_SEVERITIES: dict[str, frozenset[str]] = {
    "low": frozenset({"low"}),
    "medium": frozenset({"low", "medium"}),
}
"""What each ``[review].stop_rule_max_severity`` ceiling accepts as residual."""
_DRIVER = Actor("policy", "epic-driver")
_ERROR_LIMIT = 200
_MATCH_PROBE_LIMIT = 5
"""How many candidate issues one match may ask GitHub about (bounded gh calls)."""


class _FindingLike(Protocol):
    @property
    def severity(self) -> str: ...
    @property
    def location(self) -> str: ...
    @property
    def summary(self) -> str: ...


@dataclass(frozen=True)
class ResidualMatch:
    """An open residual issue an earlier accepted round already filed.

    ``exact`` says the recorded findings are id-for-id this round's; a coarse
    match is the same residual re-reported with drifted lines or prose.
    """

    issue: int
    exact: bool


class ResidualFilingError(RuntimeError):
    """The residual issue could not be filed or its acceptance not recorded."""


class RoundMarkRetryError(ResidualFilingError):
    """The chain acceptance stands but the round's ledger mark did not land.

    Raised only when the mark write itself failed (a locked or unwritable
    ledger), never when no round matches the acceptance's binding: the round
    the mark belongs to is on record, so the next tick re-reads the acceptance
    and marks it. The caller therefore leaves the round standing — degrading or
    holding would replace or freeze the very round the retry needs (#2151).
    """


def stop_rule_decision(
    round_num: int,
    verdict: str,
    findings: Sequence[_FindingLike],
    *,
    after_rounds: int,
    max_severity: str,
    dropped_findings: int = 0,
    failing_criteria: Sequence[str] = (),
) -> str:
    """``READY_WITH_RESIDUALS`` when the stop rule accepts the round, else *verdict*.

    The rule fires only for a ``NEEDS_CHANGES`` verdict, on a round strictly
    after *after_rounds* (0 disables the rule), with at least one finding, every
    one of which is within *max_severity* — the ``[review]``
    ``stop_rule_max_severity`` ceiling: ``"low"`` accepts a round of lows only,
    ``"medium"`` accepts lows and mediums. Severity matching ignores case and
    surrounding whitespace; any other value, including a missing one, counts as
    high. So does a finding the sidecar parser dropped (*dropped_findings*):
    a finding with no severity never reaches *findings*, and its absence must
    not let the rest of the round through. An unreadable ceiling accepts
    nothing, the same way an unreadable severity does.

    *failing_criteria* are the recorded acceptance criteria the reviewer judged
    ``fail`` (#2291). ANY of them keeps the round in remediation, whatever the
    severities and the ceiling. A residual is a defect in HOW the work was done,
    which is what this rule exists to stop a PR circling on; a failed criterion
    is the work not doing what it was FOR — the "ready except for what it was
    asked to do" shape the verdict parser refuses for a direct READY, which must
    not be accepted here by another door. Nor is it residual-shaped: filing it
    as a low/medium tracker and merging ships an item that misses its own
    contract.

    A ``kind: "declaration"`` finding (#2872) is never residual either: it is
    answered by a **Behavior changes** line in the PR body, which accepting
    the round would never write — the merge's held-out gate would then fail
    on the very tests the reviewer asked to have declared.
    """
    if after_rounds <= 0 or round_num <= after_rounds:
        return verdict
    if verdict != _NEEDS_CHANGES or not findings or dropped_findings:
        return verdict
    residual = _residual_severities(max_severity)
    if residual is None:
        _logger.warning(
            "review-residuals: round %d stays in remediation — "
            "stop_rule_max_severity %r is not one of %s",
            round_num,
            max_severity,
            ", ".join(sorted(_RESIDUAL_SEVERITIES)),
        )
        return verdict
    if _never_residual(round_num, findings, failing_criteria):
        return verdict
    if all(_is_residual(finding, residual) for finding in findings):
        return READY_WITH_RESIDUALS
    return verdict


def _never_residual(
    round_num: int, findings: Sequence[_FindingLike], failing_criteria: Sequence[str]
) -> bool:
    """A failed recorded criterion, or a finding asking for a declaration:
    either keeps the round in remediation whatever the severities."""
    if failing_criteria:
        _logger.info(
            "review-residuals: round %d stays in remediation — recorded "
            "acceptance criteria %s failed, which no severity makes residual",
            round_num,
            ", ".join(failing_criteria),
        )
        return True
    if any(getattr(f, "kind", None) == DECLARATION_FINDING_KIND for f in findings):
        _logger.info(
            "review-residuals: round %d stays in remediation — a finding asks "
            "for a Behavior changes declaration, which no severity makes residual",
            round_num,
        )
        return True
    return False


def _residual_severities(max_severity: str) -> frozenset[str] | None:
    """The severities *max_severity* accepts, or None when it names no ceiling."""
    if not isinstance(max_severity, str):
        return None
    return _RESIDUAL_SEVERITIES.get(max_severity.strip().lower())


def _is_residual(finding: _FindingLike, residual: frozenset[str]) -> bool:
    severity = getattr(finding, "severity", None)
    return isinstance(severity, str) and severity.strip().lower() in residual


def _severity_phrase(max_severity: str) -> str:
    """How a ceiling reads in prose: "low", or "low or medium"."""
    residual = _residual_severities(max_severity)
    if residual is None:
        return f"within an unrecognised ceiling ({max_severity!r})"
    return " or ".join(sorted(residual, key=lambda s: s != "low"))


def finding_id(finding: _FindingLike) -> str:
    """A stable short id for one finding: its severity, location and summary."""
    material = "\0".join((finding.severity, finding.location, finding.summary))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def accepted_residuals(
    store: RunStore, item_id: str, latest: ReviewRound | None
) -> AuditEvent | None:
    """The driver's acceptance event for *latest*, or None.

    Matches on round, head SHA and sidecar digest, and requires a filed issue
    number, so an acceptance never carries over to a later round or head. A
    store without an audit trail has no acceptance to read (fail closed).
    """
    if latest is None or latest.verdict != _NEEDS_CHANGES:
        return None
    for event in _acceptance_events(store, item_id):
        if _binds(event, latest):
            return event
    return None


def acceptance_on_record(
    store: RunStore, item_id: str, latest: ReviewRound | None
) -> bool:
    """Whether *latest*'s acceptance is on record for BOTH of its readers (#2151).

    The chain event is the attested acceptance; ``accepted_verdict`` is the mark
    every ledger reader polls. A round carrying only one of the two is a
    half-written acceptance — ``mark_round_accepted`` failed after the chain
    event landed — and the merge gate must refuse it: merging there would squash
    a PR whose ledger still reads NEEDS_CHANGES, with ``kew-review`` published as
    failure and no merge-ready round on record. The review stage re-marks the
    round on a later tick (or holds), and the merge goes then.

    A pre-v59 acceptance has no mark either, and is marked by the same retry the
    tick after this build starts driving it.
    """
    if latest is None or latest.accepted_verdict is None:
        return False
    return accepted_residuals(store, item_id, latest) is not None


def unmarked_acceptance(
    store: RunStore, item_id: str, latest: ReviewRound | None
) -> int | None:
    """The residual issue of an acceptance attested for *latest* but NOT marked.

    The half-written state ``acceptance_on_record`` refuses — a crash between the
    chain event and the ledger mark, or any pre-v59 acceptance, which never had a
    mark. Returns the issue number the chain already records for the round, which
    is what the retry re-marks it with; None when there is nothing to repair (no
    acceptance, or one both readers already carry).
    """
    if latest is None or latest.accepted_verdict is not None:
        return None
    event = accepted_residuals(store, item_id, latest)
    payload = None if event is None else _driver_payload(event)
    return None if payload is None else int(payload["issue"])


def finding_match_key(finding: _FindingLike) -> str:
    """A coarser id for one finding: the file its location names.

    :func:`finding_id` binds the reviewer's exact prose and line, so the same
    unfixed finding re-reported a round later — shifted by a merge, reworded by
    a fresh reviewer run — hashes differently and reads as a new residual. That
    is how one finding collected an issue per round (#2152). This key keeps only
    the file, the part that does not drift, so the later round recognises the
    residual it already has an issue for. Two distinct residuals in one file
    therefore share an issue: the reusing round comments its own findings onto
    it, so nothing is lost, and one over-shared tracker beats a duplicate a
    human has to close every round.

    A location naming no file has no match key (empty string): it can only be
    matched exactly.
    """
    location = finding.location.strip().split(":", 1)[0].strip().strip("/")
    if not location or location in {".", ".."}:
        return ""
    material = location.lstrip("./").lower()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def matching_residual_issue(
    store: RunStore,
    item_id: str,
    findings: Sequence[_FindingLike],
    *,
    is_open: Callable[[int], bool] | None = None,
) -> ResidualMatch | None:
    """The OPEN residual issue a prior acceptance on *item_id* already filed.

    An acceptance binds to one round and head, so a push voids it, the node goes
    back through review, and the next round re-reports the same unfixed
    low/medium findings. Filing a second issue for them would orphan the first,
    so the dedupe key is the round's CONTENT: the recorded ``finding_ids`` when
    they match id-for-id, else an overlap on :func:`finding_match_key`, which
    survives the line and prose drift an exact match does not.

    *is_open* decides whether a candidate issue is still a live tracker; without
    it every candidate counts as open (the pure content match). Candidates are
    probed newest-first, exact matches before coarse ones, and at most
    ``_MATCH_PROBE_LIMIT`` of them — past that the round files a fresh issue
    rather than walking the whole history over the network.
    """
    wanted_ids = {finding_id(f) for f in findings}
    if not wanted_ids:
        return None
    wanted_keys = {k for k in (finding_match_key(f) for f in findings) if k}
    exact: list[tuple[int, bool]] = []
    coarse: list[tuple[int, bool]] = []
    for event in _acceptance_events(store, item_id):
        payload = _driver_payload(event)
        if payload is None:
            continue
        issue = int(payload["issue"])
        if _str_set(payload.get("finding_ids")) == wanted_ids:
            exact.append((issue, True))
        elif wanted_keys & _str_set(payload.get("finding_keys")):
            coarse.append((issue, False))
    return _first_open([*exact, *coarse], is_open=is_open)


def _str_set(recorded: Any) -> set[str]:
    """The recorded list of ids as a set; anything else has nothing to match."""
    if not isinstance(recorded, list):
        return set()
    return {value for value in recorded if isinstance(value, str)}


def _first_open(
    candidates: Sequence[tuple[int, bool]], *, is_open: Callable[[int], bool] | None
) -> ResidualMatch | None:
    """The first candidate issue that is still open, probing each issue once."""
    seen: set[int] = set()
    for issue, exact in candidates:
        if issue in seen:
            continue
        seen.add(issue)
        if is_open is None:
            return ResidualMatch(issue=issue, exact=exact)
        if len(seen) > _MATCH_PROBE_LIMIT:
            _logger.warning(
                "review-residuals: %d candidate issues probed without an open "
                "one — filing a fresh residual issue",
                _MATCH_PROBE_LIMIT,
            )
            return None
        if is_open(issue):
            return ResidualMatch(issue=issue, exact=exact)
    return None


def residual_issue_is_open(repo: str, issue: int) -> bool:
    """Whether residual issue *issue* is still an open tracker on GitHub.

    A state the driver cannot read is NOT open. Reusing a closed issue would
    park a live residual where nobody looks — the finding is then lost — while
    filing a second issue is the duplicate this match exists to avoid, which a
    human can close. The cheaper failure wins, and it is a loud one: the reason
    is logged.
    """
    try:
        result = subprocess_utils.run(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", "state"],
            capture=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — an unreadable state is not open
        _logger.warning("review-residuals: issue #%d state unreadable: %s", issue, exc)
        return False
    if result.returncode != 0:
        _logger.warning(
            "review-residuals: issue #%d state unreadable (rc=%d): %s",
            issue,
            result.returncode,
            (result.stderr or "").strip()[:_ERROR_LIMIT],
        )
        return False
    try:
        state = json.loads(result.stdout or "{}").get("state")
    except ValueError as exc:
        _logger.warning("review-residuals: issue #%d state unparseable: %s", issue, exc)
        return False
    if isinstance(state, str) and state.strip().upper() == "OPEN":
        return True
    _logger.info("review-residuals: issue #%d is not open (state=%r)", issue, state)
    return False


def _acceptance_events(store: RunStore, item_id: str) -> list[AuditEvent]:
    """Every acceptance event recorded for *item_id*.

    A store without an audit trail has no acceptance to read (fail closed).
    """
    list_events = getattr(store, "list_audit_events", None)
    if not callable(list_events):
        return []
    events: list[AuditEvent] = list_events(
        limit=None, event=RESIDUALS_ACCEPTED_EVENT, item_id=item_id
    )
    return events


def _driver_payload(event: AuditEvent) -> dict[str, Any] | None:
    """*event*'s payload, when the driver attested it with a filed issue number."""
    payload = event.payload
    if (event.actor_type, event.actor_id) != (_DRIVER.type, _DRIVER.id):
        return None
    if not isinstance(payload, dict):
        return None
    issue = payload.get("issue")
    if type(issue) is not int or issue <= 0:
        return None
    return payload


def _binds(event: AuditEvent, latest: ReviewRound) -> bool:
    payload = _driver_payload(event)
    if payload is None:
        return False
    return (
        payload.get("round") == latest.round
        and payload.get("head_sha") == latest.head_sha
        and payload.get("sidecar_digest") == latest.sidecar_digest
    )


def _cell(text: str) -> str:
    """One markdown table cell: no pipes or line breaks from reviewer prose."""
    return " ".join(text.replace("|", "\\|").split())


def residual_issue_body(
    *,
    item_id: str,
    pr_number: int | None,
    round_num: int,
    head_sha: str,
    sidecar_digest: str | None,
    after_rounds: int,
    max_severity: str,
    findings: Sequence[_FindingLike],
) -> str:
    """The residual issue body: one row per finding, plus where it came from."""
    pr_ref = f"PR #{pr_number}" if pr_number is not None else "the PR"
    lines = [
        f"<!-- kew-review-residuals item={item_id} round={round_num} -->",
        f"Residual findings from kew's adversarial review of {pr_ref} "
        f"(item {item_ref(item_id)}), round {round_num}, head `{head_sha[:12]}`.",
        "",
        f"This is a residual of the review stop rule: after round {after_rounds}, "
        f"a NEEDS_CHANGES verdict whose findings are all "
        f"{_severity_phrase(max_severity)} is accepted "
        "as READY-with-residuals. The PR proceeds to merge under its other gates; "
        "these findings were not fixed there and are tracked here instead.",
        "",
        f"Sidecar digest: `{sidecar_digest or 'none'}` (host-stored; source of truth).",
        "",
    ]
    lines.extend(_finding_table(findings))
    return "\n".join(lines) + "\n"


def _finding_table(findings: Sequence[_FindingLike]) -> list[str]:
    """The markdown table rows for *findings*, header included."""
    return [
        "| ID | Severity | Location | Summary |",
        "|---|---|---|---|",
        *(
            f"| `{finding_id(f)}` | {_cell(f.severity)} | `{_cell(f.location)}` "
            f"| {_cell(f.summary)} |"
            for f in findings
        ),
    ]


def comment_residual_round(
    repo: str,
    issue: int,
    *,
    item_id: str,
    pr_number: int | None,
    round_num: int,
    head_sha: str,
    sidecar_digest: str | None,
    findings: Sequence[_FindingLike],
    exact: bool,
) -> None:
    """Record a later accepted round on the residual issue it reuses (#2152).

    Without this the reused issue still reads as the one round that filed it,
    which is what made a second issue look like the only way to record the
    second round. Best-effort, like the PR comment: the acceptance is already on
    the chain, so a failed comment costs a round reference, not the dedupe.
    """
    body = "\n".join(
        [
            f"<!-- kew-review-residuals item={item_id} round={round_num} -->",
            _reuse_line(pr_number, round_num, head_sha, exact=exact),
            "",
            f"Sidecar digest: `{sidecar_digest or 'none'}` "
            "(host-stored; source of truth).",
            "",
            *_finding_table(findings),
        ]
    )
    _post_comment(repo, issue, body, surface=f"issue #{issue}")


def _reuse_line(
    pr_number: int | None, round_num: int, head_sha: str, *, exact: bool
) -> str:
    """The sentence naming the round that reused this issue, and why it did."""
    pr_ref = f"PR #{pr_number}" if pr_number is not None else "the PR"
    same = (
        "the same findings"
        if exact
        else "findings matching the ones tracked here (same files, drifted "
        "lines or wording)"
    )
    return (
        f"Round {round_num} of kew's adversarial review of {pr_ref} "
        f"(head `{head_sha[:12]}`) was also accepted as READY-with-residuals, "
        f"with {same}. They stay tracked on this issue rather than opening a "
        f"second one."
    )


def file_residual_issue(repo: str, *, title: str, body: str) -> int:
    """Create the residual issue with ``gh``; its number, or ResidualFilingError."""
    try:
        result = subprocess_utils.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
                "--label",
                RESIDUAL_ISSUE_LABEL,
            ],
            capture=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as the filing failure
        raise ResidualFilingError(f"gh issue create failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        first = detail[0] if detail else f"exit {result.returncode}"
        raise ResidualFilingError(f"gh issue create failed: {first[:_ERROR_LIMIT]}")
    lines = (result.stdout or "").strip().splitlines()
    tail = lines[-1].strip().rstrip("/").rsplit("/", 1)[-1] if lines else ""
    if not tail.isdecimal():
        raise ResidualFilingError(
            f"gh issue create printed no issue URL: {(result.stdout or '')[:80]!r}"
        )
    return int(tail)


def comment_residuals(
    repo: str,
    pr_number: int | None,
    *,
    issue: int,
    round_num: int,
    reused: bool = False,
) -> None:
    """Link the residual issue from the PR. Best-effort, like the verdict comment.

    ``reused`` names the case where this round's findings match an earlier
    accepted round's, so the comment points at the issue already filed for them
    rather than claiming a fresh filing. It does not claim they are identical:
    a match survives drifted lines and prose (#2152), and the issue's own
    comment carries this round's findings verbatim.
    """
    if pr_number is None:
        return
    tail = (
        f"Its residual findings stay tracked in #{issue}, the residual issue "
        f"an earlier accepted round already filed for them."
        if reused
        else f"Its residual findings are filed as #{issue}."
    )
    body = (
        f"kew review stop rule: round {round_num} was accepted as "
        f"READY-with-residuals. {tail}"
    )
    _post_comment(repo, pr_number, body, surface=f"PR #{pr_number}")


def _post_comment(repo: str, number: int, body: str, *, surface: str) -> None:
    """Post *body* on issue or PR *number*. Never raises: a display surface."""
    try:
        result = subprocess_utils.run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/{number}/comments",
                "-f",
                f"body={body}",
            ],
            capture=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — a display surface never fails the tick
        _logger.warning("review-residuals: could not comment on %s: %s", surface, exc)
        return
    if result.returncode != 0:
        _logger.warning(
            "review-residuals: %s comment failed (rc=%d): %s",
            surface,
            result.returncode,
            (result.stderr or "").strip()[:_ERROR_LIMIT],
        )


def _unrecorded_acceptance(issue: int, reused: bool, exc: Exception) -> str:
    """Why the acceptance is not durable — and what the issue needs from the operator.

    The two cases need opposite cleanups, so the message names the one that
    applies. A freshly filed issue is NOT on the chain (its only acceptance is
    the write that just failed), so the retry files a new one and this one must
    be closed or it is orphaned. A REUSED issue is already on the chain from an
    earlier accepted round, and the retry reuses it again: closing it would
    close a live residual tracker the node merges pointing at.
    """
    if reused:
        return (
            f"residual issue #{issue} is reused from an earlier accepted round and "
            f"stays on the chain, but this round's acceptance was not recorded: "
            f"{exc} — leave #{issue} OPEN; the retry reuses it"
        )
    return (
        f"residual issue #{issue} filed but its acceptance was not recorded: {exc} "
        f"— close #{issue} before you unhold; the retry files a new one"
    )


def _unmarked_round(issue: int, detail: str) -> str:
    """Why the acceptance is not visible to ledger readers — and the cleanup.

    The chain event is already written when this fires, so the retry finds the
    acceptance, reuses the issue and marks the round: closing the issue would
    close the live residual tracker (the same direction as a reused filing).
    """
    return (
        f"the accepted round could not be marked in the ledger: {detail} — the "
        f"chain acceptance stands, so leave #{issue} OPEN; the retry marks the "
        f"round"
    )


def mark_round_accepted(
    store: RunStore, item_id: str, latest: ReviewRound, *, issue: int
) -> None:
    """Record the acceptance on the round itself, where every reader polls (#2151).

    The chain event is the attested fact, but readers that poll recorded rounds
    only see ``verdict`` — which stays the reviewer's ``NEEDS_CHANGES``. Without
    this mark an accepted round reads as remediation to every one of them, which
    is how an accepted PR sat unmerged for hours while its watcher waited.

    Raises when the mark is not durable, so the node never merges on a state its
    readers disagree about: ``acceptance_on_record`` — the merge gate's reading —
    requires this mark as well as the chain event. A failed write raises
    ``RoundMarkRetryError`` (the round stands, so the next tick retries the
    mark); an acceptance no round matches raises ``ResidualFilingError``, which
    no retry can fix, so it takes the loud hold/degrade path.
    """
    try:
        marked = store.accept_review_round(
            item_id,
            round_num=latest.round,
            head_sha=latest.head_sha,
            sidecar_digest=latest.sidecar_digest,
            verdict=READY_WITH_RESIDUALS,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as the filing failure
        raise RoundMarkRetryError(
            f"the accepted round's ledger mark did not land: {exc} — the chain "
            f"acceptance for #{issue} stands, and so does the round it names"
        ) from exc
    if not marked:
        raise ResidualFilingError(
            _unmarked_round(
                issue,
                f"no round {latest.round} on head {latest.head_sha[:12]} is on "
                f"record for item {item_id}",
            )
        )


def record_acceptance(
    store: RunStore,
    *,
    item_id: str,
    repo: str,
    pr_number: int | None,
    round_num: int,
    head_sha: str,
    sidecar_digest: str | None,
    issue: int,
    after_rounds: int,
    findings: Sequence[_FindingLike],
    reused: bool = False,
) -> None:
    """Attest the acceptance; raises ResidualFilingError when it is not durable.

    ``reused`` says *issue* came from an earlier acceptance rather than this
    round's filing, which is what decides the failure message's cleanup
    direction (see ``_unrecorded_acceptance``).
    """
    try:
        emit(
            store,
            RESIDUALS_ACCEPTED_EVENT,
            actor=_DRIVER,
            issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
            item_id=item_id,
            repo=repo,
            payload={
                "pr": pr_number,
                "round": round_num,
                "head_sha": head_sha,
                "sidecar_digest": sidecar_digest,
                "issue": issue,
                "after_rounds": after_rounds,
                "finding_ids": [finding_id(f) for f in findings],
                "finding_keys": sorted(
                    {k for k in (finding_match_key(f) for f in findings) if k}
                ),
                "severities": [f.severity for f in findings],
            },
            dedup_key=f"{RESIDUALS_ACCEPTED_EVENT}:{item_id}:{round_num}:{head_sha}",
            strict=True,
        )
    except RuntimeError as exc:
        raise ResidualFilingError(_unrecorded_acceptance(issue, reused, exc)) from exc
