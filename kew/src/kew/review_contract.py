"""Governed-review contract: pinned prompt resolution + verdict parsing.

Pure, host-side pieces of the adversarial-review stage that carry no
sandbox or driver dependencies, so they test in isolation:

- ``resolve_reviewer_prompt`` resolves the sha256-pinned reviewer prompt
  artifact (Task 5). The dispatch template interpolates the artifact from the
  BASE ref of the reviewed repo — "tuning" means editing a pinned artifact
  through a PR — so the loader prefers the base-ref deployed copy, read out of
  the Git ref's tree and never off the operator's working tree (#3087), and
  falls back to the packaged default. The returned sha is bound
  into the verdict attestation (E). A deployed copy older than the pinned one
  still loads, but it does not carry the recorded-criteria contract
  (``criteria_contract_in_force``, #2291): its reviewer was never told those
  rules, so they are not enforced against it.
- ``parse_verdict_sidecar`` enforces evidence symmetry (Task 8, F10/F11) over a
  broker-collected sidecar: evidence-free findings are dropped, and degenerate
  shapes (all-findings-dropped NEEDS_CHANGES, or READY without acceptance
  evidence and without an explicit unavailable reason) collapse to
  ``unparseable`` — the crash path — never a silent READY. Evidence is the
  reviewer's self-reported command string and output digest. kew checks that
  both are present. It does not run the command or recompute the digest.
  An explicit
  ``acceptance_executed: false`` is retained as degraded-confidence evidence.
  kew never trusts a self-reported ``spend_usd`` from the sidecar.
  Every reviewer-authored string leaves the parser credential-redacted (#1920):
  the reviewer holds a provider credential, and parsed text reaches the PR
  comment, the remediation prompt and the audit payload. Redaction is by
  credential value and key shape only: the text is prose about code, and
  field parsing would blank phrases such as ``token: is echoed``.
- ``collect_stream_verdict`` takes the verdict from the reviewer's own typed
  terminal result event as the host read it from the runner's stream
  (:mod:`kew.review_stream`, #2021), never from a file in the reviewed
  checkout, and binds it to the head SHA the host recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from kew.credential_surface import redact_credential_values as _redact
from kew.merge_verify import declaration_excuses
from kew.review_stream import VERDICT_BYTE_CAP
from kew.sandbox import MAX_LINE_BYTES
from kew.skills_catalog import _PKG_DIR, Artifact, load_manifest
from kew.subprocess_utils import ensure_tool, run, run_bytes

_logger = logging.getLogger(__name__)

_GIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

REVIEWER_BASE_REF = "main"
"""The base ref a review round is pinned to when the project names none: the
epic driver reviews every PR against ``branches.base or "main"``."""


def reviewer_base_ref(configured: str | None) -> str:
    """The base ref a review launch reads its prompt from, given the project's
    ``branches.base`` — so a preview names the ref a launch would."""
    return configured or REVIEWER_BASE_REF


REVIEWER_PROMPT_ARTIFACT = "adversarial-reviewer"
"""Manifest ``name`` of the pinned reviewer prompt artifact."""


def reviewer_prompt_artifact() -> Artifact:
    """The manifest entry for the pinned reviewer prompt.

    Raises ``KeyError`` if the artifact is missing from the manifest (a build
    error the catalog integrity test would already have caught).
    """
    for artifact in load_manifest():
        if artifact.name == REVIEWER_PROMPT_ARTIFACT:
            return artifact
    raise KeyError(REVIEWER_PROMPT_ARTIFACT)


@dataclass(frozen=True)
class ReviewerPromptText:
    """The reviewer prompt that loaded, and which copy it was."""

    text: str
    sha256: str
    deployed: str | None
    """The deployed copy's path in the base ref's tree (the artifact's
    ``target``), or None when the packaged default loaded."""
    base_ref: str | None = None
    """The Git ref the deployed copy was looked for in; None without a root."""
    base_commit: str | None = None
    """The commit *base_ref* resolved to when it was read; None when it did
    not resolve (no repository, no such ref), so the packaged default loaded."""


class ReviewerPromptReadError(RuntimeError):
    """The base ref lists a deployed reviewer artifact that could not be read."""


def _has_origin(root: Path) -> bool:
    """Whether *root* names an ``origin`` remote."""
    probe = run(["git", "-C", str(root), "remote", "get-url", "origin"], check=False)
    return probe.returncode == 0 and bool(probe.stdout.strip())


def _base_commit(root: Path, ref: str) -> str | None:
    """The commit *ref* names in *root*; None without git, a repo or the ref.

    A named ref resolves on ORIGIN whenever there is one, as the reviewer
    checkout's base does (``worktree.resolve_base_commit``): the operator's
    local branch of that name can carry unpushed commits, or be stale, or not
    exist. Read paths never fetch, so this is origin's copy as last fetched
    (``refs/remotes/origin/<ref>``); the launch fetches it and passes the commit.

    ``ensure_tool`` so a read path (the prompt preview) degrades to the packaged
    default rather than exiting where preflight has not run."""
    if ensure_tool("git") is None:
        return None
    named = ref if _GIT_OID.fullmatch(ref) or not _has_origin(root) else None
    probe = run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{named or f'refs/remotes/origin/{ref}'}^{{commit}}",
        ],
        check=False,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


_REGULAR_FILE_MODES = frozenset({"100644", "100755"})
"""Tree-entry modes of a regular file; a symlink (``120000``) is not one."""


def _deployed_blob(root: Path, commit: str, target: str) -> str | None:
    """The object id of the regular file at *target* in *commit*'s tree.

    *root* is the repository's top, the root every review launch passes
    (``policy.repo_root``), so *target* names the same path for the launch and
    every preview of it. None when the tree lists nothing there, or nothing
    that is a regular file: a symlink's blob is its target's path, not a
    prompt, so it deploys nothing, nor does a tree or a submodule. A listing
    Git fails, or one that does not name *target*, raises
    :class:`ReviewerPromptReadError`: what the base ref deploys is unknown."""
    listed = run(
        ["git", "-C", str(root), "ls-tree", "-z", commit, "--", target], check=False
    )
    if listed.returncode == 0 and not listed.stdout:
        return None
    entry, _, path = listed.stdout.removesuffix("\0").partition("\t")
    mode, kind, oid = (entry.split() + ["", "", ""])[:3]
    if listed.returncode != 0 or path != target or not _GIT_OID.fullmatch(oid):
        raise ReviewerPromptReadError(
            f"{target} at {commit}: the tree listing failed: "
            + (listed.stderr or listed.stdout).strip()
        )
    if kind != "blob" or mode not in _REGULAR_FILE_MODES:
        _logger.warning(
            "reviewer prompt: %s at %s is a %s entry (mode %s), not a regular "
            "file; the packaged default loads",
            target,
            commit,
            kind,
            mode,
        )
        return None
    return oid


def _deployed_bytes(root: Path, commit: str, target: str) -> bytes | None:
    """The regular file at *target* in *commit*'s tree, or None when it has none.

    The bytes are those of the one object the tree listing named, so the path
    checked and the path read cannot differ (``git show <commit>:<path>``
    resolves from the repository's top, ``ls-tree`` from the directory Git
    runs in)."""
    oid = _deployed_blob(root, commit, target)
    if oid is None:
        return None
    shown = run_bytes(["git", "-C", str(root), "cat-file", "blob", oid])
    if shown.returncode != 0:
        raise ReviewerPromptReadError(
            f"{target} at {commit}: "
            + shown.stderr.decode("utf-8", errors="replace").strip()
        )
    return shown.stdout


def resolve_reviewer_prompt(
    base_ref_root: Path | None = None,
    base_ref: str = REVIEWER_BASE_REF,
    *,
    base_commit: str | None = None,
) -> ReviewerPromptText:
    """Resolve the reviewer prompt text, its sha256, and which copy won.

    *base_ref_root* is the top of the operator's repository and *base_ref* the
    Git ref the review is pinned to. When that ref's tree carries a deployed
    copy at the artifact's target path, that base-ref copy wins
    (tuning-through-PR: the version in force is whatever the reviewed repo
    shipped on its base branch). It is read out of the ref's tree — the blob
    ``ls-tree`` lists there, by ``cat-file`` — never from the working tree,
    where an uncommitted edit or a checked-out feature branch would otherwise
    govern every review (#3087). Otherwise the packaged default ships; a
    listing or a read Git fails raises :class:`ReviewerPromptReadError`. The sha
    is computed over the exact bytes returned so the attestation binds the
    prompt version that actually reviewed the code.

    *base_commit* is the commit the launch already pinned *base_ref* to — the
    one its reviewer checkout is cloned at — so the prompt and the reviewed
    base are one commit. Without it *base_ref* resolves by :func:`_base_commit`.
    """
    artifact = reviewer_prompt_artifact()
    commit = base_commit
    if commit is None and base_ref_root is not None:
        commit = _base_commit(base_ref_root, base_ref)
    data = None
    if base_ref_root is not None and commit is not None:
        data = _deployed_bytes(base_ref_root, commit, artifact.target)
    shipped = None if data is None else artifact.target
    if data is None:
        data = (_PKG_DIR / artifact.source).read_bytes()
    return ReviewerPromptText(
        data.decode("utf-8"),
        hashlib.sha256(data).hexdigest(),
        shipped,
        base_ref=None if base_ref_root is None else base_ref,
        base_commit=commit,
    )


def read_checkout_reviewer_prompt(root: Path | None = None) -> ReviewerPromptText:
    """The reviewer prompt as the CHECKOUT at *root* holds it, uncommitted
    bytes included, or the packaged default when it deploys none.

    Not the review launch's read — that is :func:`resolve_reviewer_prompt`, off
    the base ref (#3087). This is the dispatch self-check's: it quotes the
    operator's own checkout, so it starts no process to render.
    """
    artifact = reviewer_prompt_artifact()
    deployed = None if root is None else root / artifact.target
    if deployed is not None and deployed.is_file():
        data = deployed.read_bytes()
        shipped: str | None = artifact.target
    else:
        data = (_PKG_DIR / artifact.source).read_bytes()
        shipped = None
    return ReviewerPromptText(
        data.decode("utf-8"), hashlib.sha256(data).hexdigest(), shipped
    )


def load_reviewer_prompt(base_ref_root: Path | None = None) -> tuple[str, str]:
    """:func:`read_checkout_reviewer_prompt` as ``(text, sha256)``."""
    loaded = read_checkout_reviewer_prompt(base_ref_root)
    return loaded.text, loaded.sha256


def criteria_contract_in_force(prompt_sha256: str) -> bool:
    """Whether the reviewer prompt that loaded states the criteria contract (#2291).

    The criterion rules below — cover every recorded id, link each ``fail`` to
    an evidenced finding, never carry one on a READY — are rules the PROMPT
    states. ``load_reviewer_prompt`` loads whatever the reviewed repo's base ref
    deployed, unvalidated, and ``previous_sha256`` deliberately accepts an older
    copy as unmodified: a consumer repo still shipping the pre-#2291 artifact
    would report no ``criteria_results`` at all, and every round on an item with
    a recorded plan would be lost to the crash path over a rule its reviewer was
    never told.

    So the criteria section is rendered, and the rules are enforced, only
    against the artifact the manifest currently pins. A stale deployed copy
    reviews exactly as it did before #2291 — judged against no recorded
    criteria, which is a weaker round, not a broken one, and it is fixed by the
    ordinary ``kew doctor``/sync that updates the artifact.
    """
    return prompt_sha256 == reviewer_prompt_artifact().sha256


# --- Task 8: verdict sidecar + evidence-symmetry validation (F10, F11) ---

Verdict = Literal["READY", "NEEDS_CHANGES"]
_ACCEPTED_VERDICTS: frozenset[str] = frozenset({"READY", "NEEDS_CHANGES"})


@dataclass(frozen=True)
class Finding:
    """One evidenced NEEDS_CHANGES finding that survived validation."""

    severity: str
    location: str
    summary: str
    evidence_command: str
    evidence_output_digest: str
    criterion: str | None = None
    """The recorded acceptance criterion this finding fails (#2291), when the
    reviewer named one. A ``fail`` criterion result is admissible only when some
    kept finding names it, so this is what ties the two halves of the verdict
    together — and what the remediation brief lists first."""
    kind: str | None = None
    """:data:`DECLARATION_FINDING_KIND` when the reviewer files this finding as
    asking only for **Behavior changes** lines (#2872); None for every other
    finding, which is remediated as a defect."""
    node_ids: tuple[str, ...] = ()
    """The exact test node ids a declaration finding asks to declare — the
    reviewer's own structured answer, never read off its prose."""


DECLARATION_FINDING_KIND = "declaration"
"""``kind`` of a finding that a **Behavior changes** line settles (#2872)."""


@dataclass(frozen=True)
class DeclarationRuling:
    """The reviewer's ruling on one of the implementer's proposed **Behavior
    changes** lines (#3475): the PR body's **Proposed behavior changes**."""

    node_id: str
    """The test the proposed line names, as the line names it."""
    accepted: bool
    reason: str
    """Why, in the reviewer's words. Never blank: a ruling without one is
    dropped, and a dropped acceptance declares nothing."""
    not_needed: bool = False
    """A rejection whose ``ground`` is exactly :data:`NOT_NEEDED_GROUND`
    (#3500): the test needs no declaration — its base copy passes against the
    head, or the file only adds cases. kew drops that proposal instead of
    filing a finding. Every other rejection is "wrong" and blocks."""


_RULING_ANSWERS = {"accepted": True, "rejected": False}

NOT_NEEDED_GROUND = "not_needed"
"""``ground`` of a rejection that no declaration is needed for (#3500)."""


def _ruling_rank(ruling: DeclarationRuling) -> int:
    """How strongly *ruling* blocks: accepted, then not needed, then wrong."""
    if ruling.accepted:
        return 0
    return 1 if ruling.not_needed else 2


CriterionOutcome = Literal["pass", "fail", "unverifiable"]
"""How the reviewer judged one recorded criterion.

``pass`` — met, with executed evidence. ``fail`` — not met, and some
NEEDS_CHANGES finding names it. ``unverifiable`` — the reviewer could not
settle it in the contained environment, which is an answer and not a gap.
"""

_UNVERIFIABLE: CriterionOutcome = "unverifiable"
_PASS: CriterionOutcome = "pass"
_FAIL: CriterionOutcome = "fail"
_CRITERION_OUTCOMES: dict[str, CriterionOutcome] = {
    _PASS: _PASS,
    _FAIL: _FAIL,
    _UNVERIFIABLE: _UNVERIFIABLE,
}
"""The three accepted ``result`` strings, mapped to themselves.

A lookup rather than a set so the narrowing is the type system's, not a cast:
anything not a key resolves to :data:`_UNVERIFIABLE`.
"""


@dataclass(frozen=True)
class CriterionResult:
    """One recorded criterion as the reviewer judged it (#2291)."""

    id: str
    result: CriterionOutcome
    evidence_command: str = ""
    evidence_output_digest: str = ""
    reason: str = ""
    contract_test_outcome: str = ""
    """The executed outcome of the criterion's acceptance-contract test the
    reviewer cited (#2732): ``passed``, ``failed``, or empty when it cited none
    or the criterion has no contract test."""


def contract_test_name(criterion_id: str) -> str:
    """The acceptance-contract test that proves a ``kind = test`` criterion.

    The contract holds one test per such criterion, named ``test_ac<k>`` after
    its ``AC<k>`` id (#2726), so the name is derived, never read off the branch.
    """
    return f"test_{criterion_id.strip().lower()}"


_CONTRACT_PASSED = "passed"
_CONTRACT_FAILED = "failed"
_CONTRACT_OUTCOMES = frozenset({_CONTRACT_PASSED, _CONTRACT_FAILED})


def _cited_contract_outcome(raw: Any, criterion_id: str) -> str:
    """The contract-test outcome *raw* cites for *criterion_id*, or ``""``.

    A citation counts only when it names THE contract test for this criterion
    (``test_ac<k>``) and an executed outcome. Any other name, a missing one, or
    an outcome that is not a run's pass or fail (skipped, error, prose) cites
    nothing — and nothing is not a passing run.
    """
    if not isinstance(raw, dict):
        return ""
    name = raw.get("name")
    outcome = raw.get("outcome")
    if not isinstance(name, str) or name.strip() != contract_test_name(criterion_id):
        return ""
    if not isinstance(outcome, str) or outcome.strip() not in _CONTRACT_OUTCOMES:
        return ""
    return outcome.strip()


def _contract_bound(outcome: CriterionOutcome, cited: str) -> CriterionOutcome:
    """*outcome* for a ``kind = test`` criterion, bound to its contract test.

    The contract test is the criterion's proof (#2732): a cited failing run
    makes it ``fail`` whatever the reviewer called it, and a ``pass`` with no
    cited passing run is ``unverifiable`` — never a pass kew has no executed
    contract test behind.
    """
    if cited == _CONTRACT_FAILED:
        return _FAIL
    if outcome == _PASS and cited != _CONTRACT_PASSED:
        return _UNVERIFIABLE
    return outcome


@dataclass(frozen=True)
class CoverageEntry:
    """One thing a READY reviewer checked and cleared (#3419).

    ``how`` is ``executed`` (it ran something, and ``evidence_command`` and
    ``evidence_output_digest`` say what) or ``read`` (it read the code, and the
    evidence fields are empty).
    """

    target: str
    tried: str
    how: str
    evidence_command: str = ""
    evidence_output_digest: str = ""


@dataclass(frozen=True)
class NotCheckedEntry:
    """One check the change needs that the review sandbox could not run (#3419)."""

    check: str
    kind: str
    reason: str


@dataclass(frozen=True)
class ParsedVerdict:
    """Outcome of parsing a broker-collected verdict sidecar.

    ``unparseable`` is the crash path (retry → HELD): an unparseable sidecar is
    never guessed into a READY. ``dropped_findings`` records how many findings
    were discarded for missing evidence, so the driver can log the reason a
    NEEDS_CHANGES collapsed to unparseable. READY can carry an explicit
    ``acceptance_executed=False`` with a reason when the contained environment
    cannot run acceptance.
    """

    verdict: Verdict | None
    findings: list[Finding] = field(default_factory=list)
    acceptance_evidence: list[dict[str, str]] = field(default_factory=list)
    acceptance_executed: bool | None = None
    acceptance_reason: str | None = None
    dropped_findings: int = 0
    unparseable_reason: str | None = None
    criteria_results: list[CriterionResult] = field(default_factory=list)
    """The per-criterion judgements, in the recorded plan's own id order (#2291).
    Empty when the round was handed no criteria — which is not the same as a
    reviewer who was handed some and reported nothing, because that is
    unparseable."""
    coverage: list[CoverageEntry] = field(default_factory=list)
    """What a READY reviewer checked and cleared (#3419). Filled only when the
    round was held to the coverage contract (``require_coverage``)."""
    not_checked: list[NotCheckedEntry] = field(default_factory=list)
    """What the reviewer could not check in its sandbox (#3419). Filled only
    when the round was held to the coverage contract; may be empty."""
    dropped_not_checked: int = 0
    """How many malformed ``not_checked`` entries were dropped rather than
    voiding the verdict (#3419), so the comment can say so instead of reading
    as "none reported"."""
    coverage_required: bool = False
    """Whether the round was held to the coverage contract, so an empty
    ``not_checked`` means "nothing unchecked", not "never asked" (#3419)."""
    declarations: list[DeclarationRuling] = field(default_factory=list)
    """The reviewer's ruling on each proposed **Behavior changes** line it
    judged (#3475). kew writes the accepted ones into the PR body; each
    "wrong" rejected one is also a finding, so it never rides on a READY, and
    each "not needed" one is dropped from the proposed block (#3500)."""
    excused_tests: tuple[str, ...] = ()
    """The tests kew took off this round's declaration findings because the PR
    body's **Behavior changes** section already declares them (#3516). The
    verdict comment names them, and :func:`collect_stream_verdict` records the
    declared set beside the stored sidecar so a re-parse sees what this one
    did (:func:`stored_declared_tests`)."""

    @property
    def unparseable(self) -> bool:
        return self.verdict is None

    @property
    def failing_criteria(self) -> list[str]:
        """The ids the reviewer judged ``fail``, in plan order."""
        return [r.id for r in self.criteria_results if r.result == _FAIL]


def _has_evidence(raw: Any) -> tuple[str, str] | None:
    """Return ``(command, output_digest)`` iff *raw* carries evidence.

    Evidence is self-reported: a non-empty command string and a non-empty
    output digest, both required (F11). kew does not run the command or
    recompute the digest. Anything else (missing key, wrong type, blank
    string) → dropped.
    """
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    digest = raw.get("output_digest")
    if not isinstance(command, str) or not command.strip():
        return None
    if not isinstance(digest, str) or not digest.strip():
        return None
    return _redact(command.strip()), _redact(digest.strip())


def _valid_finding(raw: Any, plan_ids: frozenset[str] = frozenset()) -> Finding | None:
    """A finding survives only with severity, ``file:line`` location, and
    self-reported evidence (command string and output digest). Missing any →
    dropped (returns ``None``).

    A ``criterion`` survives only when *plan_ids* states it (#2291). The id
    reaches the verdict comment and the remediation brief as host-trusted text,
    and it is read back to decide which criteria a remediation must satisfy, so
    it has to be one of the ids kew itself recorded — never a label a reviewer
    invented. Anything else is dropped and the finding keeps the rest."""
    if not isinstance(raw, dict):
        return None
    severity = raw.get("severity")
    location = raw.get("location")
    summary = raw.get("summary", "")
    if not isinstance(severity, str) or not severity.strip():
        return None
    # ``file:line`` — a bare filename with no line is not actionable evidence.
    # A send-back's ``operator-send-back@<head>`` names the head it answers.
    if not isinstance(location, str) or not (
        ":" in location or is_operator_send_back_location(location)
    ):
        return None
    evidence = _has_evidence(raw.get("evidence"))
    if evidence is None:
        return None
    node_ids = _declared_node_ids(raw)
    return Finding(
        severity=_redact(severity.strip()),
        location=_redact(location.strip()),
        summary=_redact(summary) if isinstance(summary, str) else "",
        evidence_command=evidence[0],
        evidence_output_digest=evidence[1],
        criterion=_named_criterion(raw, plan_ids),
        kind=DECLARATION_FINDING_KIND if node_ids else None,
        node_ids=node_ids,
    )


def _named_criterion(raw: Mapping[str, Any], plan_ids: frozenset[str]) -> str | None:
    criterion = raw.get("criterion")
    named = criterion.strip() if isinstance(criterion, str) else ""
    return named if named in plan_ids else None


def _declared_node_ids(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """The node ids a ``kind: "declaration"`` finding asks to declare (#2872).

    Empty — and the finding an ordinary defect — unless ``kind`` is exactly
    :data:`DECLARATION_FINDING_KIND` and ``node_ids`` is a non-empty list of
    non-empty strings. Any malformed shape is doubt, and doubt is remediated.
    """
    ids = raw.get("node_ids")
    if raw.get("kind") != DECLARATION_FINDING_KIND or not isinstance(ids, list):
        return ()
    kept = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
    if not kept or len(kept) != len(ids):
        return ()
    return tuple(dict.fromkeys(kept))


def _ruling(raw: Any) -> DeclarationRuling | None:
    """One ``declarations`` entry, or None when it names no test, gives an
    answer other than accepted or rejected, or gives no reason."""
    if not isinstance(raw, dict):
        return None
    node_id, reason = raw.get("node_id"), raw.get("reason")
    answer = _RULING_ANSWERS.get(_keyword(raw.get("answer")) or "")
    if answer is None or not isinstance(node_id, str) or not node_id.strip():
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    not_needed = not answer and _keyword(raw.get("ground")) == NOT_NEEDED_GROUND
    return DeclarationRuling(
        node_id.strip(), answer, _redact(reason.strip()), not_needed=not_needed
    )


def _parse_rulings(raw: Any) -> list[DeclarationRuling]:
    """The well-formed rulings, one per test. A test ruled on twice keeps the
    ruling that blocks most (:func:`_ruling_rank`): doubt is never a
    declaration, and a "not needed" never clears a "wrong"."""
    rulings: dict[str, DeclarationRuling] = {}
    for entry in raw if isinstance(raw, list) else []:
        ruling = _ruling(entry)
        if ruling is None:
            continue
        held = rulings.get(ruling.node_id)
        if held is None or _ruling_rank(ruling) > _ruling_rank(held):
            rulings[ruling.node_id] = ruling
    return list(rulings.values())


def _rejection_finding(ruling: DeclarationRuling) -> Finding:
    """The finding a "wrong" rejected proposal is (#3475): it names the test
    and carries the reviewer's reason, and it is an ordinary finding — the line
    was refused, so another line for the same test settles nothing by itself.
    A "not needed" rejection is no finding (#3500): no code change clears it,
    and the proposal it drops declared nothing."""
    return Finding(
        severity="medium",
        location=ruling.node_id,
        summary=(
            f"Proposed Behavior changes line for {ruling.node_id} rejected: "
            f"{ruling.reason}"
        ),
        evidence_command=f"reviewer ruling on the proposed line for {ruling.node_id}",
        evidence_output_digest=hashlib.sha256(ruling.reason.encode()).hexdigest(),
    )


def _without_declared(
    findings: list[Finding], declared: frozenset[str]
) -> tuple[list[Finding], tuple[str, ...]]:
    """*findings* less what the PR body already declares, and the tests that
    came off (#3516).

    A ``kind: "declaration"`` finding asks only for **Behavior changes** lines,
    so the tests the body declares — by the held-out gate's own rule,
    :func:`declaration_excuses` — come off its ``node_ids``, and a finding
    left asking for none is dropped. One that names a ``criterion`` is a
    defect against the plan, not only a request for lines, and stands as
    written, as does every finding of another kind.
    """
    kept: list[Finding] = []
    excused: list[str] = []
    for finding in findings:
        if finding.kind != DECLARATION_FINDING_KIND or finding.criterion:
            kept.append(finding)
            continue
        ids = tuple(i for i in finding.node_ids if not declaration_excuses(i, declared))
        excused += [i for i in finding.node_ids if i not in ids]
        if ids:
            kept.append(replace(finding, node_ids=ids))
    return kept, tuple(dict.fromkeys(excused))


def _rejection_findings(rulings: list[DeclarationRuling]) -> list[Finding]:
    """One finding per "wrong" rejection; a "not needed" one files none."""
    return [
        _rejection_finding(r) for r in rulings if not r.accepted and not r.not_needed
    ]


def _unparseable(reason: str, dropped: int = 0) -> ParsedVerdict:
    return ParsedVerdict(
        verdict=None, dropped_findings=dropped, unparseable_reason=reason
    )


def _load_sidecar_dict(raw: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    """Decode *raw* to a JSON object, or ``None`` (the crash path) if it is not
    valid JSON or not an object."""
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _partition_findings(
    raw_findings: Any, plan_ids: frozenset[str] = frozenset()
) -> tuple[list[Finding], int]:
    """Split a raw findings list into (evidenced, dropped-count)."""
    findings_list = raw_findings if isinstance(raw_findings, list) else []
    kept: list[Finding] = []
    dropped = 0
    for entry in findings_list:
        valid = _valid_finding(entry, plan_ids)
        if valid is None:
            dropped += 1
        else:
            kept.append(valid)
    return kept, dropped


def _claimed_outcome(raw_result: Any) -> CriterionOutcome:
    """The reviewer's ``result`` as one of the three states, else ``unverifiable``."""
    # ``isinstance`` first: a reviewer can put any JSON value here, and an
    # unhashable one (a list, an object) raises on a dict lookup.
    if not isinstance(raw_result, str):
        return _UNVERIFIABLE
    return _CRITERION_OUTCOMES.get(raw_result, _UNVERIFIABLE)


def _criterion_result(
    raw: Any, plan_ids: frozenset[str], contract_ids: frozenset[str] = frozenset()
) -> CriterionResult | None:
    """One ``criteria_results`` entry, or None when its id is not in the plan.

    An id the recorded plan does not state is DROPPED: the plan is the
    contract, and a reviewer cannot extend it by reporting against an id kew
    never recorded. Everything else resolves toward ``unverifiable``, which is
    the floor of the three states — a result outside the enum, and a ``pass``
    with no executed evidence behind it, both become ``unverifiable`` rather
    than a pass kew has no evidence for.

    A criterion in *contract_ids* (``kind = test``) is further bound to its
    acceptance-contract test outcome — see :func:`_contract_bound`.
    """
    if not isinstance(raw, dict):
        return None
    cid = raw.get("id")
    if not isinstance(cid, str) or cid.strip() not in plan_ids:
        return None
    outcome = _claimed_outcome(raw.get("result"))
    evidence = _has_evidence(raw.get("evidence"))
    if outcome == _PASS and evidence is None:
        outcome = _UNVERIFIABLE
    cited = ""
    if cid.strip() in contract_ids:
        cited = _cited_contract_outcome(raw.get("contract_test"), cid)
        outcome = _contract_bound(outcome, cited)
    reason = raw.get("reason")
    return CriterionResult(
        id=cid.strip(),
        result=outcome,
        evidence_command="" if evidence is None else evidence[0],
        evidence_output_digest="" if evidence is None else evidence[1],
        reason=_redact(reason.strip()) if isinstance(reason, str) else "",
        contract_test_outcome=cited,
    )


def _uncovered_refusal(
    results: list[CriterionResult], plan_ids: tuple[str, ...]
) -> str | None:
    """A verdict that leaves a recorded criterion unjudged is inadmissible."""
    judged = {r.id for r in results}
    missing = [cid for cid in plan_ids if cid not in judged]
    if not missing:
        return None
    return "verdict criteria_results do not cover recorded plan criteria " + ", ".join(
        missing
    )


def _failing_refusal(
    failed: list[str], verdict: str, kept: list[Finding]
) -> str | None:
    """Why a verdict's ``fail`` results are inadmissible, or None.

    Two refusals: a verdict other than NEEDS_CHANGES carrying a failing
    criterion — the forged-READY shape, "ready except for what it was asked to
    do" — and a ``fail`` with no evidenced finding naming it, which is a defect
    asserted without evidence, exactly what F11 drops elsewhere.
    """
    if not failed:
        return None
    if verdict != "NEEDS_CHANGES":
        return (
            f"{verdict} cannot carry a failing acceptance criterion ("
            + ", ".join(failed)
            + ")"
        )
    named = {f.criterion for f in kept}
    unmatched = [cid for cid in failed if cid not in named]
    if not unmatched:
        return None
    return (
        "each failing acceptance criterion needs an evidenced finding "
        'carrying its "criterion"; none names ' + ", ".join(unmatched)
    )


def _criteria_refusal(
    results: list[CriterionResult],
    plan_ids: tuple[str, ...],
    verdict: str,
    kept: list[Finding],
) -> str | None:
    """Why these criterion results are inadmissible, or None — the crash path
    rather than a verdict kew cannot stand behind."""
    return _uncovered_refusal(results, plan_ids) or _failing_refusal(
        [r.id for r in results if r.result == _FAIL], verdict, kept
    )


def _parse_criteria(
    data: dict[str, Any],
    plan_ids: tuple[str, ...],
    verdict: str,
    kept: list[Finding],
    contract_ids: frozenset[str] = frozenset(),
) -> tuple[list[CriterionResult], str | None]:
    """The admissible criterion results and the refusal they earn, if any.

    A no-op when the round was handed no criteria: the rules apply ONLY when a
    plan section was rendered, so a node with no recorded plan behaves exactly
    as it did before #2291. Results come back in the plan's own id order, and a
    duplicated id keeps its first entry.
    """
    known = frozenset(plan_ids)
    if not known:
        return [], None
    raw = data.get("criteria_results")
    by_id: dict[str, CriterionResult] = {}
    for entry in raw if isinstance(raw, list) else []:
        result = _criterion_result(entry, known, contract_ids)
        if result is not None:
            by_id.setdefault(result.id, result)
    results = [by_id[cid] for cid in plan_ids if cid in by_id]
    return results, _criteria_refusal(results, plan_ids, verdict, kept)


def _acceptance_entries(data: dict[str, Any]) -> list[dict[str, str]]:
    raw_accept = data.get("acceptance_evidence")
    accept_list = raw_accept if isinstance(raw_accept, list) else []
    return [ev for entry in accept_list if (ev := _accept_evidence(entry))]


def _parse_unexecuted_acceptance(
    data: dict[str, Any],
    acceptance: list[dict[str, str]],
    dropped: int,
    criteria: list[CriterionResult],
) -> ParsedVerdict:
    reason = data.get("acceptance_reason")
    if not isinstance(reason, str) or not reason.strip():
        return _unparseable(
            "READY with acceptance_executed=false requires acceptance_reason"
        )
    if acceptance:
        return _unparseable(
            "READY cannot carry executed evidence with acceptance_executed=false"
        )
    return ParsedVerdict(
        verdict="READY",
        acceptance_executed=False,
        acceptance_reason=_redact(reason.strip()),
        dropped_findings=dropped,
        criteria_results=criteria,
    )


def _parse_ready_acceptance(
    data: dict[str, Any], dropped: int, criteria: list[CriterionResult]
) -> ParsedVerdict:
    """Parse READY acceptance, including an explicit unavailable result."""
    acceptance = _acceptance_entries(data)
    raw_executed = data.get("acceptance_executed")
    if raw_executed is False:
        return _parse_unexecuted_acceptance(data, acceptance, dropped, criteria)
    if raw_executed is not None and raw_executed is not True:
        return _unparseable("acceptance_executed must be a boolean")
    if not acceptance:
        return _unparseable("READY without executed acceptance evidence")
    return ParsedVerdict(
        verdict="READY",
        acceptance_evidence=acceptance,
        acceptance_executed=True,
        dropped_findings=dropped,
        criteria_results=criteria,
    )


TEST_CHANGE_ACCEPTED = "ACCEPTED"
TEST_CHANGE_REJECTED = "REJECTED"


def unanswered_test_flags(data: Mapping[str, Any], flags: Sequence[str]) -> list[str]:
    """The test-integrity flags a verdict does not mark ACCEPTED with a reason (#2486).

    *flags* are the flag names of the round's test-integrity report
    (:mod:`kew.test_integrity`). The verdict's ``test_changes`` section — its
    **Test changes** — answers each one with ``ACCEPTED`` and the reason (the
    issue line, or the PR's **Behavior changes** line) that justifies it, or
    ``REJECTED``. A flag with no entry, a REJECTED one, and an ACCEPTED one with
    a blank reason all come back here, in *flags* order. A duplicated flag keeps
    its first entry; an entry for a flag the report did not raise is ignored.
    """
    raw = data.get("test_changes")
    answers: dict[str, tuple[Any, Any]] = {}
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("flag"), str):
            answers.setdefault(
                entry["flag"].strip(), (entry.get("answer"), entry.get("reason"))
            )
    return [flag for flag in flags if not _accepted(answers.get(flag))]


def _accepted(answer: tuple[Any, Any] | None) -> bool:
    if answer is None:
        return False
    verdict, reason = answer
    return (
        verdict == TEST_CHANGE_ACCEPTED
        and isinstance(reason, str)
        and bool(reason.strip())
    )


def _test_changes_refusal(
    data: dict[str, Any], flags: Sequence[str], verdict: str
) -> str | None:
    """Why a READY leaves a test-integrity flag unanswered or REJECTED, or None.

    Only READY is held to it: a NEEDS_CHANGES already sends the PR back, and a
    round whose report raised no flag has nothing to answer.
    """
    if verdict != "READY" or not flags:
        return None
    open_flags = unanswered_test_flags(data, flags)
    if not open_flags:
        return None
    return (
        "READY must mark every test-integrity flag ACCEPTED with a reason in "
        "test_changes; unanswered or REJECTED: " + ", ".join(open_flags)
    )


#: The ``not_checked`` kinds the reviewer prompt names (#3419). A kind the
#: reviewer NAMES outside this set makes the verdict unparseable: the comment
#: groups by it, and an invented kind is a reviewer writing outside the
#: contract it was given. Case and surrounding space are not invention.
NOT_CHECKED_KINDS: frozenset[str] = frozenset(
    {"browser", "full_suite", "docker", "network", "host", "other"}
)
_COVERAGE_HOW: frozenset[str] = frozenset({"executed", "read"})
#: The longest coverage or not-checked text field kept, in characters. The
#: lists reach the PR comment, so one reviewer string cannot flood it.
COVERAGE_TEXT_CAP = 300
#: A target a reader can open: ``path:line``, ``path:line-line``,
#: ``path:line:col``, ``path:L42`` / ``path:L42-L50``, or a pytest node id
#: (``path::name[::name...]``, with an optional ``[param]``).
_COVERAGE_TARGET = re.compile(
    r"^[^\s:]+(?::(?:L?\d+(?:-L?\d+)?|\d+:\d+)"
    r"|::[^\s:\[\]]+(?:::[^\s:\[\]]+)*(?:\[[^\n]+\])?)$"
)


def _clip(text: str) -> str:
    """*text* cut to :data:`COVERAGE_TEXT_CAP` characters, marked when cut."""
    if len(text) > COVERAGE_TEXT_CAP:
        return text[: COVERAGE_TEXT_CAP - 1] + "…"
    return text


def _coverage_text(raw: Any) -> str:
    """*raw* as a redacted, clipped, stripped string; ``""`` when not a string."""
    if not isinstance(raw, str):
        return ""
    return _clip(_redact(raw.strip()))


def _keyword(raw: Any) -> str | None:
    """A reviewer-written keyword (``how``, ``kind``) trimmed and lowercased."""
    return raw.strip().lower() if isinstance(raw, str) else None


def _coverage_target(raw: Any) -> str | None:
    """*raw* as a target a reader can open (:data:`_COVERAGE_TARGET`), or None."""
    if not isinstance(raw, str) or not _COVERAGE_TARGET.match(raw.strip()):
        return None
    return _coverage_text(raw)


def _coverage_how(raw: Any) -> str | None:
    how = _keyword(raw)
    return how if how in _COVERAGE_HOW else None


def _coverage_entry(raw: Any) -> CoverageEntry | None:
    """A coverage entry survives with an openable target, what was tried, a
    known ``how``, and evidence when ``how`` is ``executed``; else dropped.
    The evidence is clipped like every other coverage field: it reaches the PR
    comment too."""
    if not isinstance(raw, dict):
        return None
    target = _coverage_target(raw.get("target"))
    how = _coverage_how(raw.get("how"))
    tried = _coverage_text(raw.get("tried"))
    if target is None or how is None or not tried:
        return None
    if how == "read":
        return CoverageEntry(target=target, tried=tried, how=how)
    evidence = _has_evidence(raw.get("evidence"))
    if evidence is None:
        return None
    command, digest = evidence
    return CoverageEntry(
        target=target,
        tried=tried,
        how=how,
        evidence_command=_clip(command),
        evidence_output_digest=_clip(digest),
    )


def _named_kind(raw: Any) -> str | None:
    """The kind a not-checked entry names, normalized; None when it names none."""
    kind = _keyword(raw.get("kind")) if isinstance(raw, dict) else None
    return kind or None


def _not_checked_entry(raw: Any) -> NotCheckedEntry | None:
    """A not-checked entry with a check, a known kind and a reason, or None."""
    kind = _named_kind(raw)
    if kind is None or kind not in NOT_CHECKED_KINDS:
        return None
    check = _coverage_text(raw.get("check"))
    reason = _coverage_text(raw.get("reason"))
    if not check or not reason:
        return None
    return NotCheckedEntry(check=check, kind=kind, reason=reason)


def _parse_not_checked(
    data: dict[str, Any],
) -> tuple[list[NotCheckedEntry], int, str | None]:
    """The admitted entries, how many malformed ones were dropped, and a refusal.

    Only a NAMED kind outside :data:`NOT_CHECKED_KINDS` refuses the verdict
    (#3419). Any other malformed entry — no check, no reason, no kind, not an
    object — is dropped and counted, so one slip cannot void a NEEDS_CHANGES
    whose findings are evidenced and hold the node for a human.
    """
    raw = data.get("not_checked")
    if not isinstance(raw, list):
        return [], 0, "every verdict must carry a not_checked list (it may be empty)"
    named = (_named_kind(entry) for entry in raw)
    if any(kind is not None and kind not in NOT_CHECKED_KINDS for kind in named):
        return (
            [],
            0,
            (
                "every not_checked kind must be one of "
                + ", ".join(sorted(NOT_CHECKED_KINDS))
            ),
        )
    kept = [entry for item in raw if (entry := _not_checked_entry(item))]
    dropped = len(raw) - len(kept)
    if dropped:
        _logger.warning(
            "review verdict: dropped %d malformed not_checked entr%s "
            "(no check, reason or kind)",
            dropped,
            "y" if dropped == 1 else "ies",
        )
    return kept, dropped, None


def _parse_ready_coverage(
    data: dict[str, Any],
) -> tuple[list[CoverageEntry], str | None]:
    raw = data.get("coverage")
    if not isinstance(raw, list) or not raw:
        return [], "READY must carry a non-empty coverage list"
    kept = [entry for item in raw if (entry := _coverage_entry(item))]
    if not kept:
        return (
            [],
            "READY coverage entries were all dropped (target, tried, how, evidence)",
        )
    ran_acceptance = data.get("acceptance_executed") is not False
    if ran_acceptance and not any(entry.how == "executed" for entry in kept):
        return [], (
            "READY that executed acceptance must carry an executed coverage "
            "entry with evidence"
        )
    return kept, None


@dataclass(frozen=True)
class _CoverageLists:
    """The coverage contract's parsed lists, and why they are inadmissible."""

    coverage: list[CoverageEntry] = field(default_factory=list)
    not_checked: list[NotCheckedEntry] = field(default_factory=list)
    dropped_not_checked: int = 0
    refusal: str | None = None


def _coverage_refusal(
    data: dict[str, Any], verdict: str, require_coverage: bool
) -> _CoverageLists:
    """The coverage and not-checked lists, and why they are inadmissible (#3419).

    A no-op unless *require_coverage*: only a reviewer that was shown the
    coverage contract is held to it, so stored sidecars and older prompts
    parse as they always did. ``coverage`` is READY-only; ``not_checked`` is
    required on every verdict and may be empty.
    """
    if not require_coverage:
        return _CoverageLists()
    not_checked, dropped, refusal = _parse_not_checked(data)
    if refusal is not None or verdict != "READY":
        return _CoverageLists([], not_checked, dropped, refusal)
    coverage, refusal = _parse_ready_coverage(data)
    return _CoverageLists(coverage, not_checked, dropped, refusal)


def parse_verdict_sidecar(
    raw: str | bytes | dict[str, Any],
    *,
    plan_criterion_ids: Sequence[str] = (),
    integrity_flags: Sequence[str] = (),
    contract_test_ids: Sequence[str] = (),
    require_coverage: bool = False,
    declared_tests: Sequence[str] = (),
) -> ParsedVerdict:
    """Parse a reviewer verdict sidecar with evidence symmetry (F10/F11).

    Rules, each collapsing to the crash path (``unparseable``) rather than a
    silent READY:

    - Non-JSON / non-object / unknown verdict → unparseable.
    - Evidence-free NEEDS_CHANGES findings are dropped; a NEEDS_CHANGES whose
      findings are ALL dropped → unparseable (never converted to READY).
    - READY without executed acceptance evidence or an explicit unavailable
      acceptance reason → unparseable.
    - A self-reported ``spend_usd`` (or any cost field) is ignored — kew's spend
      figures come from RunRecords, never the sidecar (F10).

    *plan_criterion_ids* are the ids of the recorded plan the dispatch rendered
    into the reviewer's prompt (#2291), in plan order. Given them, four more
    rules apply — a verdict that leaves one unjudged is unparseable, an id
    outside the plan is dropped, a ``pass`` with no evidence becomes
    ``unverifiable``, a READY carrying a ``fail`` is unparseable, and each
    ``fail`` needs an evidenced finding naming it. Empty (no plan section was
    rendered) means none of them apply and parsing is unchanged: a round judged
    against nothing stated must not be failed for saying nothing about it.

    *integrity_flags* are the test-integrity flags the dispatch rendered into
    the prompt (#2486). Given them, a READY whose ``test_changes`` leaves one
    unanswered or REJECTED is unparseable — never a READY. Empty (a clean
    report, or no report shown) adds no requirement.

    *contract_test_ids* are the ``kind = test`` ids among *plan_criterion_ids*
    (#2732). Each one's result is bound to the acceptance-contract test outcome
    its entry cites: a ``pass`` citing no passing run is ``unverifiable``, and a
    cited ``failed`` is ``fail`` — which then needs an evidenced finding and
    keeps the round out of READY and residual acceptance like any other fail.

    The ``declarations`` section is the reviewer's ruling on each proposed
    **Behavior changes** line (#3475), kept in ``declarations``. A rejected
    proposal is also a finding naming its test and the reason, so a verdict
    that rejects one is NEEDS_CHANGES whatever it said — unless its ``ground``
    is ``not_needed`` (#3500): that proposal is dropped, never a finding.

    *require_coverage* holds the verdict to the coverage contract (#3419): every
    verdict carries a ``not_checked`` list, and a READY a non-empty ``coverage``
    list — with an evidenced ``executed`` entry when acceptance ran. Only the
    launch of the pinned prompt sets it; a re-parse of a stored sidecar keeps
    the default and parses as it always did.

    *declared_tests* are the tests the PR body's **Behavior changes** section
    declares, as the prompt showed them (#3516). A declaration finding asking
    only for them is dropped (:func:`_without_declared`), and a NEEDS_CHANGES
    left with no finding, and none dropped as malformed, is judged as the READY
    it then is, under every READY rule above. One that lacks what a READY needs
    parses as the reviewer wrote it, findings and all, and its remediation is
    briefed on them as on any NEEDS_CHANGES round. A re-parse of a stored
    sidecar passes the set :func:`collect_stream_verdict` recorded beside it
    (:func:`stored_declared_tests`), so every reader sees the same findings.
    """
    parsed = _parse_verdict(
        raw,
        plan_criterion_ids=plan_criterion_ids,
        integrity_flags=integrity_flags,
        contract_test_ids=contract_test_ids,
        require_coverage=require_coverage,
        declared=frozenset(declared_tests),
    )
    if parsed.unparseable and declared_tests:
        return _parse_verdict(
            raw,
            plan_criterion_ids=plan_criterion_ids,
            integrity_flags=integrity_flags,
            contract_test_ids=contract_test_ids,
            require_coverage=require_coverage,
            declared=frozenset(),
        )
    return parsed


def _ruled_verdict(
    verdict: str,
    kept: list[Finding],
    rejected: list[Finding],
    *,
    excused: bool,
    dropped: int,
) -> tuple[str, list[Finding]]:
    """The verdict and findings once the declaration rulings and the declared
    tests are applied."""
    if rejected:
        # A rejected proposal is a finding, so the verdict cannot be READY.
        return "NEEDS_CHANGES", [*kept, *rejected]
    if excused and not kept and not dropped:
        # Every objection asked for lines the body already carries (#3516). A
        # finding dropped as malformed is an objection nobody can read, so the
        # round stays as the reviewer wrote it.
        return "READY", kept
    return verdict, kept


def _parse_verdict(
    raw: str | bytes | dict[str, Any],
    *,
    plan_criterion_ids: Sequence[str],
    integrity_flags: Sequence[str],
    contract_test_ids: Sequence[str],
    require_coverage: bool,
    declared: frozenset[str],
) -> ParsedVerdict:
    """:func:`parse_verdict_sidecar` against one declared set."""
    data = _load_sidecar_dict(raw)
    if data is None:
        return _unparseable("sidecar is not a JSON object")

    verdict = data.get("verdict")
    if verdict not in _ACCEPTED_VERDICTS:
        return _unparseable(f"unknown verdict {verdict!r}")

    plan_ids = tuple(plan_criterion_ids)
    kept, dropped = _partition_findings(data.get("findings"), frozenset(plan_ids))
    kept, excused = _without_declared(kept, declared)
    rulings = _parse_rulings(data.get("declarations"))
    verdict, kept = _ruled_verdict(
        str(verdict),
        kept,
        _rejection_findings(rulings),
        excused=bool(excused),
        dropped=dropped,
    )
    criteria, refusal = _parse_criteria(
        data, plan_ids, str(verdict), kept, frozenset(contract_test_ids)
    )
    refusal = refusal or _test_changes_refusal(data, integrity_flags, str(verdict))
    lists = _coverage_refusal(data, str(verdict), require_coverage)
    refusal = refusal or lists.refusal
    if refusal is not None:
        return _unparseable(refusal, dropped=dropped)
    parsed = _parse_admitted(data, str(verdict), kept, dropped, criteria)
    if parsed.unparseable:
        return parsed
    return replace(
        parsed,
        coverage=lists.coverage,
        not_checked=lists.not_checked,
        dropped_not_checked=lists.dropped_not_checked,
        coverage_required=require_coverage,
        declarations=rulings,
        excused_tests=excused,
    )


def _parse_admitted(
    data: dict[str, Any],
    verdict: str,
    kept: list[Finding],
    dropped: int,
    criteria: list[CriterionResult],
) -> ParsedVerdict:
    """The verdict once every section-level refusal has passed."""
    if verdict == "NEEDS_CHANGES":
        if not kept:
            return _unparseable(
                "NEEDS_CHANGES with no evidenced findings", dropped=dropped
            )
        return ParsedVerdict(
            verdict="NEEDS_CHANGES",
            findings=kept,
            dropped_findings=dropped,
            criteria_results=criteria,
        )

    # Older evidence-bearing sidecars infer true; contained reviewers can
    # explicitly report unavailable acceptance as degraded confidence.
    return _parse_ready_acceptance(data, dropped, criteria)


def _accept_evidence(raw: Any) -> dict[str, str] | None:
    evidence = _has_evidence(raw)
    if evidence is None:
        return None
    return {"command": evidence[0], "output_digest": evidence[1]}


@dataclass(frozen=True)
class CollectedSidecar:
    """A reviewer verdict record stored on the host (report-broker #759 pattern).

    The attested ``digest`` is computed over kew's *stored* copy of the verdict
    block the reviewer's own result event carried — never over a file in the
    reviewed checkout (which the reviewed code can write, #2021) and never
    over a GitHub comment (F10).
    """

    stored_path: Path
    digest: str
    parsed: ParsedVerdict
    failure_layer: str | None = None
    error_category: str | None = None


#: ``error_category`` of a reviewer run a provider usage limit ended (#2219).
#: NOT a verdict and NOT a hold: the reviewer was cut off mid-run, so there is no
#: verdict to judge and nothing a human can fix — the round is retried once the
#: limit resets (see ``review_stage``).
RATE_LIMITED_CATEGORY = "rate_limited"

#: The lane that failed for a rate-limited round — the provider, not any layer
#: the host could repair.
RATE_LIMITED_LAYER = "rate_limit"

#: The lane that failed for a reviewer that ran and whose stream carried no
#: admissible verdict.
STREAM_LAYER = "stream"

#: ``error_category`` of a reviewer run that exited 0 with more than one result
#: event, none of them carrying a different verdict (#3174): the runner started
#: a second turn. A runner fault, retried once per node and head before it holds.
MULTIPLE_RESULTS_CATEGORY = "review_multiple_results"

#: ``error_category`` of a reviewer run that failed and left no verdict
#: (#3174). It holds under its own name with the exit and what the stream
#: lacked, never as a bare ``review_unparseable``, and is not retried.
FAILED_NO_VERDICT_CATEGORY = "review_failed_no_verdict"


def _rate_limited_round(capture: Mapping[str, Any] | None) -> bool:
    """Did a provider usage limit end this reviewer run? (#2219)

    A REJECTED rate-limit notice in the stream is necessary and the reviewer
    reaching the end of its own judging is disqualifying — nothing in between.
    Rejected and nothing weaker: the runner emits a ``rate_limit_event``
    whenever its rate-limit info changes, so a long run that never came near a
    limit still carries ``allowed``/``allowed_warning`` notices, and counting
    those reclassified every ordinary reviewer failure as a provider limit
    (``review_stream._record_rate_limit``).

    A ``result`` event reporting no error is the reviewer reaching that end: it
    ran to completion however many rejections it survived, so a missing or
    malformed verdict block there is a verdict defect and keeps the unparseable
    path a human reads.

    Everything else is the limit ending the run: an error result, and — the
    shape a limit that kills the reviewer MID-STREAM leaves — no result event at
    all, where ``is_error`` was never recorded. Requiring ``is_error is True``
    read that absence as a clean finish and held the node on
    ``review_unparseable``, which is the needs-human hold this exists to remove.

    Fail-closed either way: this decides only whether a FAILED round is retried
    or held, never whether one is admitted. The stream is untrusted output, so a
    planted limit notice can at most turn a hold into a retry — and the retry is
    bounded, holding the node under its own reason once the wait has outlasted
    any real limit (``review_stage._MAX_RATE_LIMIT_WAIT``).
    """
    if capture is None:
        return False
    seen = capture.get("rate_limit_rejections")
    if not isinstance(seen, int) or isinstance(seen, bool) or seen < 1:
        return False
    return capture.get("is_error") is not False


def _stream_refusal(capture: Mapping[str, Any] | None, exit_code: int) -> str | None:
    """Why the reviewer's stream carries no admissible verdict, or None.

    Every refusal is the crash path: a non-zero exit, a stream without exactly
    one terminal result event, an error result, an oversize message, and a
    final message without exactly one verdict block all fail the round. A
    failed run's refusal names both its exit and what its stream lacked, so the
    hold says what went wrong rather than only that the run failed (#3174).
    """
    stream: str | None
    if capture is None:
        stream = "the reviewer stream produced no result capture"
    else:
        stream = _result_event_refusal(capture) or _block_refusal(capture)
    if exit_code != 0:
        return (
            f"reviewer exited {exit_code}; {stream or 'a failed run yields no verdict'}"
        )
    return stream


def _result_event_refusal(capture: Mapping[str, Any]) -> str | None:
    """Exactly one successful result event, and no later non-system event."""
    split = capture.get("split_lines")
    if split != 0:
        return (
            f"the reviewer stream carries {split} split lines: a line longer than "
            f"the host reader's {MAX_LINE_BYTES}-byte limit, or one glued onto "
            "a newline-free write; no result event can be trusted"
        )
    unparsed = capture.get("unparsed_lines")
    if unparsed != 0:
        # A genuine reviewer stream is typed JSON events only. A line that does
        # not parse may be the agent's own result glued onto a newline-free
        # write, so nothing after it, or before it, can be trusted as final.
        return (
            f"the reviewer stream carries {unparsed} lines that do not parse as "
            "events; no result event can be trusted"
        )
    count = capture.get("result_events")
    if count != 1:
        return (
            f"the reviewer stream carries {count} result events; "
            "exactly one is required"
        )
    if capture.get("terminal_is_result") is not True:
        return (
            "the reviewer's result event is not the stream's final event: a "
            "later line is a non-system event or does not parse as one"
        )
    if capture.get("is_error") is not False or capture.get("subtype") != "success":
        return "the reviewer's result event reports an error"
    return None


def _block_refusal(capture: Mapping[str, Any]) -> str | None:
    """The result text fits the cap and carries exactly one verdict block."""
    if capture.get("oversize") is True:
        return f"the reviewer's final result exceeds the {VERDICT_BYTE_CAP}-byte cap"
    block_error = capture.get("block_error")
    if isinstance(block_error, str) and block_error:
        return block_error
    if not isinstance(capture.get("verdict_block"), str):
        return "the reviewer's final result carries no verdict block"
    return None


def _bind_head_sha(parsed: ParsedVerdict, data: bytes, head_sha: str) -> ParsedVerdict:
    """Fail a verdict that names any head other than the one the host recorded.

    The attested head is always the host's recorded sha; the reviewer's
    ``head_sha`` only checks that it reviewed that checkout. Missing counts as
    a mismatch.
    """
    if parsed.unparseable:
        return parsed
    claimed = _load_sidecar_dict(data)
    named = None if claimed is None else claimed.get("head_sha")
    if named != head_sha:
        return _unparseable(
            f"verdict head_sha {_redact(str(named))!r} does not match the "
            f"recorded head {head_sha}"
        )
    return parsed


def collect_stream_verdict(
    capture: Mapping[str, Any] | None,
    *,
    head_sha: str,
    exit_code: int,
    dest_dir: Path,
    run_id: str,
    plan_criterion_ids: Sequence[str] = (),
    integrity_flags: Sequence[str] = (),
    contract_test_ids: Sequence[str] = (),
    require_coverage: bool = False,
    declared_tests: Sequence[str] = (),
) -> CollectedSidecar:
    """Store, digest and parse the verdict the reviewer's stream carried.

    *capture* is the host-side record of the runner's typed terminal result
    event (:mod:`kew.review_stream`); nothing in the reviewed checkout is read.
    The host stores the verdict block at ``review-sidecar-<run_id>.json`` and
    the attested digest is over that stored copy. A refused stream stores a
    zero-byte record and takes the crash path under its reason, so a failed
    round is never admitted as READY.

    *plan_criterion_ids* are the recorded criteria this round's prompt carried
    (#2291); the dispatch passes them through its launch context so the parser
    holds the verdict to the same ids the reviewer was shown. *integrity_flags*
    travel the same way (#2486): the flags this round's prompt asked it to answer,
    and so do *contract_test_ids* (#2732): its ``kind = test`` criterion ids.
    *require_coverage* (#3419) is whether this round's prompt was the pinned
    one that states the coverage contract. *declared_tests* (#3516) are the
    tests this round's prompt listed as already declared.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    refusal = _stream_refusal(capture, exit_code)
    block = capture.get("verdict_block") if capture is not None else None
    data = block.encode("utf-8") if refusal is None and isinstance(block, str) else b""
    stored.write_bytes(data)
    digest = hashlib.sha256(stored.read_bytes()).hexdigest()
    if refusal is None:

        def parse(declared: Sequence[str]) -> ParsedVerdict:
            return parse_verdict_sidecar(
                data,
                plan_criterion_ids=plan_criterion_ids,
                integrity_flags=integrity_flags,
                contract_test_ids=contract_test_ids,
                require_coverage=require_coverage,
                declared_tests=declared,
            )

        parsed = parse(declared_tests)
        if not _record_declared(stored, parsed.excused_tests and declared_tests):
            # Unrecorded, a re-parse would see findings this parse dropped.
            parsed = parse(())
        parsed = _bind_head_sha(parsed, data, head_sha)
        return CollectedSidecar(stored_path=stored, digest=digest, parsed=parsed)
    failure_layer, error_category = _refused_round_category(capture, exit_code)
    return CollectedSidecar(
        stored_path=stored,
        digest=digest,
        parsed=_unparseable(refusal),
        failure_layer=failure_layer,
        error_category=error_category,
    )


_DECLARED_SUFFIX = ".declared.json"


def _declared_record(sidecar_path: str | Path) -> Path:
    return Path(f"{sidecar_path}{_DECLARED_SUFFIX}")


def _record_declared(stored: Path, declared: Sequence[str]) -> bool:
    """Store the declared set a parse of *stored* excused tests against (#3516).

    Written whole or not at all, and only when *declared* is not empty; a
    record left by an earlier write of the same path is removed otherwise.
    False when it could not be written: the caller then parses as though
    nothing were declared, so the round and every later re-parse agree.
    """
    path = _declared_record(stored)
    staged = path.with_name(f"{path.name}.tmp")
    try:
        if not declared:
            path.unlink(missing_ok=True)
            return True
        staged.write_text(json.dumps(sorted(declared)))
        staged.replace(path)
    except OSError as exc:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        _logger.error("review: declared tests not recorded for %s: %s", path, exc)
        return False
    return True


def stored_declared_tests(sidecar_path: str | Path | None) -> tuple[str, ...]:
    """The declared set :func:`collect_stream_verdict` parsed *sidecar_path*
    against (#3516), for a re-parse to pass as ``declared_tests``; empty when
    none was recorded or the record is unreadable — the sidecar then parses
    as the reviewer wrote it, as the round itself did."""
    if not sidecar_path:
        return ()
    path = _declared_record(sidecar_path)
    try:
        data = json.loads(path.read_text()) if path.is_file() else []
    except (OSError, ValueError) as exc:
        _logger.warning("review: declared-test record %s unreadable: %s", path, exc)
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(i for i in data if isinstance(i, str))


def criterion_ids(criteria_json: str | None) -> tuple[str, ...]:
    """The ids a recorded round's ``criteria_json`` states, in its own order.

    The reverse of what :func:`review_plan_section` stored, for a later tick
    that re-reads a round's sidecar from persisted state — the remediation leg.
    Re-parsing those findings without the ids would silently drop every
    ``criterion`` the reviewer named, so the brief that is supposed to list the
    failing criteria first would list none.

    An unreadable column yields no ids and says so in the log: the brief then
    degrades to its pre-#2291 shape, which is the right outcome — the round's
    findings are still remediated — but it is a host-side defect, not the
    "this round had no criteria" that an empty/absent column legitimately
    means, so the two must not be indistinguishable in the operator's log.
    """
    if not criteria_json:
        return ()
    try:
        entries = json.loads(criteria_json)
    except ValueError:
        _logger.warning(
            "review: a round's recorded criteria_json is not valid JSON — the "
            "remediation brief for it names no failing criteria"
        )
        return ()
    if not isinstance(entries, list):
        _logger.warning(
            "review: a round's recorded criteria_json is not a JSON list — the "
            "remediation brief for it names no failing criteria"
        )
        return ()
    return tuple(
        entry["id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )


def contract_test_ids(criteria_json: str | None) -> tuple[str, ...]:
    """The ``kind = test`` ids a recorded round's ``criteria_json`` states (#2732).

    The companion of :func:`criterion_ids` for a tick that re-parses a stored
    sidecar: the stop rule must read a cited failing contract test as the
    ``fail`` the original parse recorded, or a round that missed its own
    contract reads as residual-only. An unreadable column yields none, and
    says so: the same column yields no criterion ids either, so the re-parse
    holds the round to no criteria at all.
    """
    if not criteria_json:
        return ()
    try:
        entries = json.loads(criteria_json)
    except ValueError:
        _logger.warning(
            "review: a round's recorded criteria_json is not valid JSON — no "
            "criterion of it is bound to an acceptance-contract test"
        )
        return ()
    if not isinstance(entries, list):
        return ()
    return tuple(
        entry["id"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry.get("kind") == "test"
    )


def _refused_round_category(
    capture: Mapping[str, Any] | None, exit_code: int
) -> tuple[str | None, str | None]:
    """The ``(failure_layer, error_category)`` a refused stream earns.

    A round a usage limit cut off names its category, so the stage can retry it
    instead of holding the node on a verdict that was never written. A failed
    run and a successful run that spoke more than once each name themselves
    (#3174); every other refusal keeps the bare crash path.
    """
    if _rate_limited_round(capture):
        return RATE_LIMITED_LAYER, RATE_LIMITED_CATEGORY
    if exit_code != 0:
        return STREAM_LAYER, FAILED_NO_VERDICT_CATEGORY
    if _repeated_result_round(capture):
        return STREAM_LAYER, MULTIPLE_RESULTS_CATEGORY
    return None, None


def _repeated_result_round(capture: Mapping[str, Any] | None) -> bool:
    """Did a clean stream carry more than one result, none of them disagreeing?

    The runner starting a second turn after its first ``result`` is a runner
    fault, not a verdict defect. Results carrying two DIFFERENT verdict blocks
    are not that fault: one of them may be forged into the agent's stdout, so
    that stream keeps the refusal it always had and is never retried.
    """
    if capture is None:
        return False
    count = capture.get("result_events")
    distinct = capture.get("distinct_verdict_blocks")
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        return False
    if not isinstance(distinct, int) or isinstance(distinct, bool) or distinct > 1:
        return False
    return capture.get("split_lines") == 0 and capture.get("unparsed_lines") == 0


def credential_refusal_sidecar(
    dest_dir: Path, run_id: str, runner_name: str
) -> CollectedSidecar:
    """Create a deterministic host-owned refusal without reading a worktree."""
    reason = f"review runner {runner_name!r} declares no approved container credential"
    data = json.dumps(
        {"host_refusal": "runner_has_no_approved_container_credential"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    stored.write_bytes(data)
    return CollectedSidecar(
        stored_path=stored,
        digest=hashlib.sha256(data).hexdigest(),
        parsed=_unparseable(reason),
        failure_layer="credential",
        error_category="credential_missing",
    )


def infrastructure_failure_sidecar(
    dest_dir: Path,
    run_id: str,
    *,
    failure_layer: str,
    error_category: str,
    reason: str,
) -> CollectedSidecar:
    """Create a host-owned crash sidecar with its failing layer attached.

    Infrastructure failures are not reviewer verdicts, but they still need a
    durable object for the normal DEGRADED/HELD path to consume. Keeping the
    layer on the collected object also lets the attested verdict explain which
    part of the lane failed without trusting reviewer-written JSON.
    """
    data = json.dumps(
        {
            "error_category": error_category,
            "failure_layer": failure_layer,
            "host_failure": reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    stored.write_bytes(data)
    return CollectedSidecar(
        stored_path=stored,
        digest=hashlib.sha256(data).hexdigest(),
        parsed=_unparseable(reason),
        failure_layer=failure_layer,
        error_category=error_category,
    )


MERGE_VERIFY_LOCATION = "merge-verify"
"""``location`` prefix of a merge-queue finding (see below)."""


def merge_verify_failure_sidecar(
    dest_dir: Path,
    run_id: str,
    *,
    base: str,
    head_sha: str,
    command: str,
    evidence: str,
) -> CollectedSidecar:
    """Author the NEEDS_CHANGES sidecar for a failed combined-tree verify (#2148).

    The serial merge queue merges *base* into a READY head and runs the project's
    verify command on the combined tree. A red result is not a reviewer verdict,
    but it IS a defect in that head that a remediation can fix — so it is
    recorded as the same evidenced NEEDS_CHANGES round a reviewer would write,
    and the review stage remediates it with the failing gate named in the brief.

    Severity is ``high`` deliberately: the residual stop rule accepts only
    low/medium findings, and a red merge verification must never be accepted as
    READY-with-residuals and merged anyway.

    ``location`` is ``merge-verify:<base>``. The contract wants a
    ``<file>:<line>``-shaped location, and what failed here is the integration
    of the head with *base*, not one line of the diff; the failing gate is named
    in ``summary``. ``evidence.output_digest`` is the sha256 of the stored
    evidence text — self-reported, like every other sidecar digest (F11).
    """
    data = json.dumps(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [
                {
                    "severity": "high",
                    "location": f"{MERGE_VERIFY_LOCATION}:{base}",
                    "summary": (
                        f"the combined tree ({base} merged into head "
                        f"{head_sha[:12]}) fails `{command}`: {evidence}"
                    ),
                    "evidence": {
                        "command": command,
                        "output_digest": hashlib.sha256(
                            evidence.encode("utf-8", "replace")
                        ).hexdigest(),
                    },
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    stored.write_bytes(data)
    return CollectedSidecar(
        stored_path=stored,
        digest=hashlib.sha256(data).hexdigest(),
        parsed=parse_verdict_sidecar(data),
    )


OPERATOR_SEND_BACK_LOCATION = "operator-send-back"
"""``location`` prefix of an operator send-back finding (see below)."""


def is_operator_send_back_location(location: str) -> bool:
    """Whether *location* is a send-back's ``operator-send-back@<head>``."""
    return location.startswith(f"{OPERATOR_SEND_BACK_LOCATION}@")


def is_host_location(location: str) -> bool:
    """Whether *location* is one kew writes itself rather than a ``file:line``.

    A merge verify's ``merge-verify:<base>`` and a send-back's
    ``operator-send-back@<head>`` name a base or a head, never a line: a base
    called ``2026`` must not be read back as line 2026 of a file ``merge-verify``.
    """
    return location.startswith(f"{MERGE_VERIFY_LOCATION}:") or (
        is_operator_send_back_location(location)
    )


def operator_send_back_sidecar(
    dest_dir: Path, run_id: str, *, head_sha: str, actor: str, reason: str
) -> CollectedSidecar:
    """Author the NEEDS_CHANGES sidecar for an operator send-back (#3070).

    ``kew epic send-back`` records an operator's blockers against a head kew's
    own reviewer passed. Like a red merge verification it is not a reviewer
    verdict, but it is the round the drive's remediation must answer, so it is
    written in the same evidenced shape: one ``high`` finding (the stop rule
    never accepts it as a residual) whose summary is the operator's reason.
    ``evidence.output_digest`` is the sha256 of that reason as written.

    ``location`` is ``operator-send-back@<head>``: the ``@`` keeps a head whose
    first characters are all digits from being read back as a line number.
    """
    data = json.dumps(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [
                {
                    "severity": "high",
                    "location": f"{OPERATOR_SEND_BACK_LOCATION}@{head_sha}",
                    "summary": f"operator {actor} sent this head back: {reason}",
                    "evidence": {
                        "command": "kew epic send-back",
                        "output_digest": hashlib.sha256(
                            reason.encode("utf-8", "replace")
                        ).hexdigest(),
                    },
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    stored.write_bytes(data)
    return CollectedSidecar(
        stored_path=stored,
        digest=hashlib.sha256(data).hexdigest(),
        parsed=parse_verdict_sidecar(data),
    )


PRE_REVIEW_GATE_LOCATION = "pre-review-gate"
"""``location`` prefix of a pre-review gate finding that cites no file."""


@dataclass(frozen=True)
class GateFinding:
    """One failure the pre-review gate saw, as a finding records it (#3201)."""

    location: str
    """``<file>:<line>`` when the output cites a file in the head, else
    ``pre-review-gate:<gate>``."""
    summary: str
    command: str
    output: str
    check: str = ""
    """Which of the step's checks failed (the static checks, the ui-checks,
    the merge with the base), for the hold its exhausted bound earns."""


def pre_review_gate_sidecar(
    dest_dir: Path, run_id: str, *, findings: Sequence[GateFinding]
) -> CollectedSidecar:
    """Author the NEEDS_CHANGES sidecar for a head the pre-review gate failed.

    A static check or a ui-check that fails before review is not a reviewer
    verdict, but it is a defect the merge gate would refuse, so it is recorded
    in the same evidenced shape a merge-queue failure is: ``high`` findings (the
    stop rule never accepts one as a residual) whose summaries carry the
    output, so the remediation brief shows the agent what failed.
    """
    data = json.dumps(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [
                {
                    "severity": "high",
                    "location": finding.location,
                    "summary": finding.summary,
                    "evidence": {
                        "command": finding.command,
                        "output_digest": hashlib.sha256(
                            finding.output.encode("utf-8", "replace")
                        ).hexdigest(),
                    },
                }
                for finding in findings
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"review-sidecar-{run_id}.json"
    stored.write_bytes(data)
    return CollectedSidecar(
        stored_path=stored,
        digest=hashlib.sha256(data).hexdigest(),
        parsed=parse_verdict_sidecar(data),
    )
