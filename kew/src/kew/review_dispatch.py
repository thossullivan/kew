"""Governed adversarial-reviewer dispatch (Tasks 6, 7).

The driver invokes :func:`dispatch_reviewer` between "agent opened a PR" and the
approval surface. It stands up a fresh-context sandboxed reviewer against a
RECORDED head SHA under the ``review`` pseudo-epic, with its own review-pool
budget preflight (separate from the implementation cap, F5) and head-ref context
masking (F12/N5).

Two concurrency-sensitive idioms are reused verbatim so the reviewer never
corrupts node status (the ``concurrent-state-change`` checklist applies — this
is reservation + refusal code):

- The atomic reservation idiom (#939): the reviewer holds a reservation parked
  under the ``review`` pseudo-epic (NOT the node's item — overlay precedence
  derives IN_PROGRESS from any item reservation kind-blind), released on every
  terminal path matched by ``run_id`` (#972 semantics).
- The budget-refusal semantics of ``dispatch_core._refuse_budget`` /
  ``_record_budget_refusal_row``: a review-pool refusal writes a terminal
  ``refused`` RunRecord keyed to the reservation ``run_id``, holds the node with
  a ``review_budget`` reason (blocking) so the overlay routes it to HELD, and
  emits the audit event — never a silent dead corpse the reconciler misreads.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, NoReturn

from kew import audit, work_dirs, worktree
from kew.audit import resolve_actor
from kew.behavior_declarations import record_flagged_tests
from kew.budget import (
    REVIEW_POOL_KEY,
    WINDOW_LIMITS,
    BudgetExceeded,
    check_review_budget,
    check_window_budget,
    review_budget_hold_reason,
    window_budget_hold_reason,
)
from kew.config import DispatchConfig
from kew.graph.driver.merge_queue import touched_gate_paths
from kew.ids import branch_slug
from kew.pr_text import proposed_section_lines
from kew.prompt_addendum import ProfileAddendum, ProfileAddendumRefusedError
from kew.provenance import orchestrator_provenance
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_contract import (
    CollectedSidecar,
    criteria_contract_in_force,
    resolve_reviewer_prompt,
)
from kew.review_criteria import (
    ReviewPlan,
    UnjudgedCriteria,
    review_plan_section,
    unjudged_criteria,
)
from kew.review_delta import (
    REVIEW_DIFF_OPTIONS,
    REVIEW_MODE_DELTA,
    REVIEW_MODE_FULL,
    REVIEW_PREV_HEAD_REF,
    ReviewMode,
    ReviewModePlan,
    delta_section,
    full_fallback_section,
    full_on_gate_paths,
)
from kew.review_frontend import plan_frontend_review
from kew.review_integrity import (
    declared_reasons,
    integrity_section,
    read_integrity_report,
)
from kew.review_stream import VERDICT_BEGIN, VERDICT_END
from kew.run_report_sink import IntegrityDiff, export_published_integrity
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    ItemPlanRow,
    RunRecord,
    RunStore,
)
from kew.subprocess_utils import run
from kew.test_integrity import TestIntegrityReport
from kew.worktree import release_checkout_claim, resolve_base_commit

# ``REVIEW_EPIC_REF`` (imported above) is the pseudo-epic all reviewer/
# remediation reservations park under (N2), so an item-blind overlay precedence
# never derives the node IN_PROGRESS from a live reviewer. Re-exported here for
# this module's callers/tests (delegation-pseudo-epic precedent).
__all__ = ["REVIEW_EPIC_REF", "dispatch_reviewer", "mask_head_ref_context"]

_logger = logging.getLogger(__name__)

_REVIEW_BUDGET_REFUSAL_CATEGORY = "review_budget_refused"
_REVIEW_BASE_REF = "refs/kew/review/base"
_REVIEW_HEAD_REF = "refs/kew/review/head"
_REVIEW_DIFF_HEADING = "\n\n## Authoritative PR diff boundary\n\n"
#: Rendered between an operator addendum and the reviewer artifact, so every
#: review rule — the rubric and each host section after it — follows the host
#: text that overrides the addendum (#3024, #3088).
_REVIEW_HOST_PRECEDENCE = (
    "The review rules below take precedence over the operator instructions above."
)
_REVIEW_DIFF_SUFFIX = (
    f"""Review the PR with this exact host-controlled command:

git diff {REVIEW_DIFF_OPTIONS} {_REVIEW_BASE_REF}...{_REVIEW_HEAD_REF} --

"""
    "`git status` and working-tree diffs include host masking artifacts and "
    "do not define PR scope.\n"
    "Only the pinned base-to-head diff above defines the change under review.\n"
)

#: The prompt-profile role whose operator addendum the reviewer runs (#3024).
REVIEWER_PROFILE_ROLE = "adversarial_review"

_REVIEW_RUNTIME_GUIDANCE = """

## Review runtime and budget boundary

This contained review is read-only and makes no network requests of its own.
Before you started, the host installed the checkout's locked dependencies into
`/work/kew/.venv` inside this same image and checked that `pytest` and `kew`
import there from that environment's own interpreter. Run acceptance from
`/work/kew` with `UV_PROJECT_ENVIRONMENT=/work/kew/.venv uv run --offline --no-sync
--no-python-downloads pytest ...` (a targeted subset; the variable pins uv to
that environment whatever the tree's own uv settings say), the same prefix for
`ruff check .`, or the project's own scripts. If `/work/kew/.venv` is absent,
provisioning failed: say so in `acceptance_reason` and do not install or
download anything. Do not spend review time attempting `gh`, GitHub API calls,
dependency downloads, or credential/network stubs: those steps are not
permitted in this environment.

Run every command in the FOREGROUND, bounded and targeted.
Do not start background tasks, and do not start anything you would then have to
poll, wait on, or kill: when a background task finishes it re-invokes you, and
the extra result event that re-invocation emits lands after your verdict. The
host accepts exactly one result event per review, so a second one makes the
round unparseable and the whole review is lost — as completely as if you had
never produced a verdict. The full test suite is the host's own gate, not yours;
run the subset that evidences this diff.

The host review cap for this run is ${max_review_usd:.2f}. As spend or your
remaining review budget approaches that cap, stop investigating and end with
your verdict block immediately, with the evidence you have. A run that ends
without a verdict block is the worst outcome. If no runnable acceptance command
exists, emit READY only with `acceptance_executed: false` and a specific
`acceptance_reason`; this is degraded-confidence, not an absent verdict.
"""

# Runner-auto-loaded surfaces the head ref must never leak into the reviewer's
# context (F12/N5): their content is written by the code under review, and a
# runner injects them as instructions. Masked at materialization with the
# base-ref copy — the sandbox binds exactly one mount, so overlay-mounting is
# unavailable.
#
# Claude Code auto-loads a ``CLAUDE.md``/``CLAUDE.local.md`` from EVERY directory
# it works in (not just the repo root), reads ``.mcp.json`` and ``.claude/`` for
# tool/config, and the OpenCode runner (a supported reviewer runner) auto-loads
# ``AGENTS.md`` — all at any depth. So masking walks the whole checkout tree and
# masks every occurrence, not only the root: a nested ``src/CLAUDE.md`` planted
# by the PR under review is exactly the cheap steering vector this closes
# (#979-F1).
_HEAD_AUTOLOAD_FILES = frozenset(
    {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json"}
)
_HEAD_AUTOLOAD_DIRS = frozenset({".claude"})


def _collect_autoload_rels(checkout_dir: Path) -> list[str]:
    """Every relative path under *checkout_dir* (at ANY depth) a runner would
    auto-load: files named in ``_HEAD_AUTOLOAD_FILES`` and directories named in
    ``_HEAD_AUTOLOAD_DIRS``. ``.git`` and the interior of a matched ``.claude``
    are never descended into (the whole ``.claude`` is masked as a unit)."""
    rels: list[str] = []
    for dirpath, dirnames, filenames in os.walk(checkout_dir):
        rel_dir = Path(dirpath).relative_to(checkout_dir)
        if ".git" in dirnames:
            dirnames.remove(".git")  # the reviewer's clone .git is not head content
        for name in list(dirnames):
            if name in _HEAD_AUTOLOAD_DIRS:
                rels.append(str(rel_dir / name))
                dirnames.remove(name)  # mask wholesale; don't walk inside
        for name in filenames:
            if name in _HEAD_AUTOLOAD_FILES:
                rels.append(str(rel_dir / name))
    return rels


DispatchOutcomeKind = Literal[
    "dispatched", "budget_refused", "reservation_lost", "fleet_full"
]


@dataclass(frozen=True)
class PromptProvenance:
    """The prompt a reviewer ran under (#3024), as a crash verdict attests it.

    A round that produced no usable verdict still ran a prompt: the base
    artifact, the text it received and the operator addendum it carried. All
    None when no reviewer was launched, or the dispatch raised before the
    prompt was known.
    """

    prompt_artifact_sha: str | None = None
    effective_prompt_sha: str | None = None
    prompt_profile_version_id: int | None = None
    base_ref: str | None = None
    """The ref the base artifact was read from (#3088), None when unknown."""
    base_commit: str | None = None
    """The commit whose tree's artifact bytes hash to ``prompt_artifact_sha``
    (#3088): a deployed copy is read out of this commit, never a working tree."""

    def artifact_only(self) -> PromptProvenance:
        """The base artifact's half alone: what a reviewer that never started
        was governed by, with no received text and no profile."""
        return PromptProvenance(
            prompt_artifact_sha=self.prompt_artifact_sha,
            base_ref=self.base_ref,
            base_commit=self.base_commit,
        )


NO_PROVENANCE = PromptProvenance()


class ReviewerLaunchError(Exception):
    """A dispatch that raised once the reviewer's prompt was known (#3024).

    The launcher, or the bookkeeping after it, can raise after a reviewer ran
    under a known prompt; the crash verdict still names that prompt, so the
    raise carries it. The original error is the ``__cause__``.
    """

    def __init__(self, provenance: PromptProvenance) -> None:
        super().__init__("reviewer launch failed after its prompt was assembled")
        self.provenance = provenance


@dataclass(frozen=True)
class ReviewerDispatch:
    """Result of a reviewer dispatch attempt.

    ``head_sha`` is the sha the worktree was cut at and the sha bound into the
    verdict attestation — "did the merged code survive review" is answerable from
    the chain because this is the same sha the merge attestation records (F1).
    """

    outcome: DispatchOutcomeKind
    run_id: str
    item_id: str
    head_sha: str
    prompt_artifact_sha: str | None = None
    """sha256 of the BASE reviewer artifact (#3024), as ``resolve_reviewer_prompt``
    returned it: the addendum and the host sections never move it."""
    effective_prompt_sha: str | None = None
    """sha256 of the text the reviewer received (#3024): the attested prompt
    plus the host-owned verdict channel (:func:`reviewer_received_prompt`)."""
    prompt_profile_version_id: int | None = None
    """The ``adversarial_review`` addendum the prompt carried (#3024), None
    when the reviewer ran the bare base prompt."""
    base_ref: str | None = None
    """The ref ``prompt_artifact_sha``'s artifact was read from (#3088)."""
    base_commit: str | None = None
    """The commit that ref resolved to, whose artifact bytes hash to
    ``prompt_artifact_sha`` (#3088)."""
    worktree: Path | None = None
    sidecar: CollectedSidecar | None = None
    review_mode: ReviewMode = REVIEW_MODE_FULL
    """Whether the round ran as a full or a delta review (#2111)."""
    plan_sha: str | None = None
    """The recorded ``item_plans`` plan whose criteria this round's prompt
    carried (#2291). None when the item has no recorded plan, or its plan states
    no criteria: the round was judged against nothing stated, and the recorded
    round has to read that way rather than naming a plan it never saw."""
    criteria_json: str | None = None
    """Those criteria as JSON, exactly as the prompt carried them."""
    omitted_criterion_ids: tuple[str, ...] = ()
    """Recorded criteria the prompt could not carry, so they are outside this
    round's contract (#2291). The verdict comment names them: without that, its
    criteria table reads as the whole plan the sha above names."""
    unjudged_criteria: UnjudgedCriteria | None = None
    """The item's recorded plan when this round could carry NO criteria at all
    (#2291) — a stale deployed reviewer artifact. ``plan_sha`` stays None
    because nothing was judged; this keeps "kew skipped the contract" from
    reading, on the verdict comment, as "the issue stated no criteria"."""
    gate_paths: tuple[str, ...] = ()
    """The merge gate paths (``[epic] merge_gate_paths``) the reviewed diff
    changes (#3422), read off the pinned refs before the reviewer launched —
    never from the checkout after it, which the reviewer could write to. Any
    at all means a READY needs a second, independent full round on the head."""
    declaration_record_error: str | None = None
    """Why the round's flagged-test record (#2872) could not be written beside
    its sidecar, or None when it was (or there was nothing to record). The
    review stage must not record a NEEDS_CHANGES round without it."""
    pr_body: str | None = None
    """The PR body as read for this round, None when it was not read. Its
    proposed **Behavior changes** lines are the ones the reviewer was shown and
    ruled on (#3475), so the accepted lines are rebuilt from this copy."""

    @property
    def provenance(self) -> PromptProvenance:
        """The prompt this round's reviewer ran under (#3024)."""
        return PromptProvenance(
            prompt_artifact_sha=self.prompt_artifact_sha,
            effective_prompt_sha=self.effective_prompt_sha,
            prompt_profile_version_id=self.prompt_profile_version_id,
            base_ref=self.base_ref,
            base_commit=self.base_commit,
        )


def _stage_base_context(
    checkout_dir: Path, base_ref: str, rels: list[str], staged_tree: Path
) -> dict[str, bool]:
    """Classify and stage every replacement before checkout mutation."""
    base_present: dict[str, bool] = {}
    for rel in rels:
        probe = run(
            ["git", "cat-file", "-e", f"{base_ref}:{rel}"],
            cwd=checkout_dir,
            check=False,
        )
        base_present[rel] = probe.returncode == 0

    staged_tree.mkdir()
    git_env = {**os.environ, "GIT_INDEX_FILE": str(staged_tree.parent / "index")}
    run(["git", "read-tree", base_ref], cwd=checkout_dir, env=git_env)
    run(
        ["git", "checkout-index", "--all", f"--prefix={staged_tree}/"],
        cwd=checkout_dir,
        env=git_env,
    )
    for rel, present in base_present.items():
        staged = staged_tree / rel
        if present and not (staged.exists() or staged.is_symlink()):
            raise RuntimeError(f"failed to stage base context path: {rel}")
    return base_present


def _replace_with_staged_base(target: Path, staged: Path, present: bool) -> None:
    """Remove one head path and install its already-staged base copy."""
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    if not present:
        return
    if staged.is_dir() and not staged.is_symlink():
        shutil.copytree(staged, target, symlinks=True)
    elif staged.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.readlink(staged))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, target)


def mask_head_ref_context(checkout_dir: Path, base_ref: str) -> list[str]:
    """Atomically stage then mask every runner-auto-loaded head-ref surface."""

    run(["git", "cat-file", "-e", f"{base_ref}^{{commit}}"], cwd=checkout_dir)
    rels = _collect_autoload_rels(checkout_dir)
    with work_dirs.work_tempdir("kew-review-mask-") as stage_name:
        staged_tree = Path(stage_name) / "tree"
        base_present = _stage_base_context(checkout_dir, base_ref, rels, staged_tree)
        for rel in rels:
            _replace_with_staged_base(
                checkout_dir / rel, staged_tree / rel, base_present[rel]
            )
    return rels


def _pin_reviewer_ref(
    checkout_dir: Path, source_ref: str, pinned_ref: str, *, repo_root: Path
) -> str:
    """Resolve *source_ref*, fetching through the shared helper when necessary."""
    local = run(
        ["git", "cat-file", "-e", f"{source_ref}^{{commit}}"],
        cwd=checkout_dir,
        check=False,
    )
    resolved_ref = source_ref
    if local.returncode != 0:
        worktree.fetch_missing_head(checkout_dir, source_ref, repo_root=repo_root)
        resolved_ref = "FETCH_HEAD"
    sha = run(
        ["git", "rev-parse", f"{resolved_ref}^{{commit}}"],
        cwd=checkout_dir,
    ).stdout.strip()
    run(["git", "update-ref", pinned_ref, sha], cwd=checkout_dir)
    return sha


def materialize_reviewer_worktree(
    repo_root: Path, head_sha: str, base_ref: str
) -> Path:
    """Clone *repo_root* into a temp checkout, detach at the RECORDED *head_sha*,
    and mask the head-ref auto-load surface with the base ref (Task 6 + 7).

    The shared ``worktree`` primitive bootstraps both the base and recorded head.
    It uses locally resolved commits first and fetches only missing commits.
    The local-clone/upstream setup (#1002) therefore remains in one place.
    Detaching at the exact recorded sha means a
    post-dispatch force-push cannot move what the reviewer sees. The one
    genuinely reviewer-specific step stays here: masking the head-ref auto-load
    surface (F12/N5) before the caller launches the sandbox, on this single
    bind-mount checkout. On any failure we own the temp checkout — remove it so
    nothing leaks; a successful return hands a live checkout to the caller.
    """

    checkout_dir = work_dirs.make_work_dir("kew-review-")
    worktree.claim_checkout(checkout_dir)
    try:
        pinned_base, pinned_head = worktree.clone_local_checkout(
            repo_root, checkout_dir, (base_ref, head_sha)
        )
        run(["git", "update-ref", _REVIEW_BASE_REF, pinned_base], cwd=checkout_dir)
        run(["git", "update-ref", _REVIEW_HEAD_REF, pinned_head], cwd=checkout_dir)
        run(["git", "checkout", "--detach", pinned_head], cwd=checkout_dir, check=True)
        mask_head_ref_context(checkout_dir, _REVIEW_BASE_REF)
    except BaseException:
        try:
            shutil.rmtree(checkout_dir, ignore_errors=True)
        finally:
            release_checkout_claim(checkout_dir)
        raise
    return checkout_dir


def _is_ancestor(checkout_dir: Path, ancestor: str, descendant: str) -> bool:
    """Whether *ancestor* is an ancestor of *descendant*; a git error is no."""
    probe = run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=checkout_dir,
        check=False,
    )
    return probe.returncode == 0


def read_gate_paths(checkout_dir: Path, config: DispatchConfig) -> tuple[str, ...]:
    """The merge gate paths ``base...head`` changes in the pinned checkout (#3422).

    ``--no-renames`` lists both sides of a rename, so a gate file moved out of
    a gate directory still counts. ``--ignore-submodules=none`` lists a moved
    submodule pointer that the head's ``.gitmodules`` marks ``ignore = all``.
    A git failure raises: an unread diff must not read as "touches no gate
    path".
    """
    diff = run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--ignore-submodules=none",
            "-z",
            f"{_REVIEW_BASE_REF}...{_REVIEW_HEAD_REF}",
        ],
        cwd=checkout_dir,
        check=True,
    )
    changed = [path for path in diff.stdout.split("\0") if path]
    return touched_gate_paths(changed, config.epic.merge_gate_paths)


def head_gate_paths(
    repo_root: Path, head_sha: str, base_ref: str, config: DispatchConfig
) -> tuple[str, ...]:
    """The merge gate paths *head_sha* changes, read as a reviewer dispatch
    reads them (#3422): off the pinned refs of a fresh reviewer checkout,
    removed before this returns. For a decision on a tick where no reviewer
    ran. A failure raises, as :func:`read_gate_paths` does.
    """
    if not config.epic.merge_gate_paths:
        return ()
    checkout = materialize_reviewer_worktree(
        repo_root, head_sha, resolve_base_commit(repo_root, base_ref)
    )
    try:
        return read_gate_paths(checkout, config)
    finally:
        try:
            shutil.rmtree(checkout, ignore_errors=True)
        finally:
            release_checkout_claim(checkout)


def resolve_review_mode(
    checkout_dir: Path, plan: ReviewModePlan | None, *, repo_root: Path
) -> tuple[ReviewMode, str]:
    """Settle the round's mode in the materialized checkout (#2111).

    A delta candidate becomes a delta round only when its previous head can be
    pinned as ``refs/kew/review/prev-head`` and is an ancestor of the head
    under review. Every other case is a full review, and a round 2+ fallback
    says why in the returned prompt section. Returns ``(mode, section)``.
    """
    if plan is None or plan.candidate is None:
        reason = None if plan is None else plan.full_reason
        return REVIEW_MODE_FULL, full_fallback_section(reason) if reason else ""
    prev_head = plan.candidate.prev_head_sha
    try:
        _pin_reviewer_ref(
            checkout_dir, prev_head, REVIEW_PREV_HEAD_REF, repo_root=repo_root
        )
    except Exception:  # any fetch/pin failure means a full review
        _logger.warning("review: previous head %s unavailable; full review", prev_head)
        return REVIEW_MODE_FULL, full_fallback_section(
            f"the previous reviewed head {prev_head} is not available "
            "(force-push or rebase)"
        )
    if not _is_ancestor(checkout_dir, REVIEW_PREV_HEAD_REF, _REVIEW_HEAD_REF):
        run(
            ["git", "update-ref", "-d", REVIEW_PREV_HEAD_REF],
            cwd=checkout_dir,
            check=False,
        )
        return REVIEW_MODE_FULL, full_fallback_section(
            f"the previous reviewed head {prev_head} is not an ancestor of the "
            "head under review (force-push or rebase)"
        )
    return REVIEW_MODE_DELTA, delta_section(
        plan.candidate, base_ref=_REVIEW_BASE_REF, head_ref=_REVIEW_HEAD_REF
    )


# A launcher seam so the orchestration is testable without a container: the
# default wiring lands in the driver-integration wave (Group C).
ReviewerLauncher = Callable[["ReviewerLaunchContext"], CollectedSidecar]


@dataclass(frozen=True)
class ReviewerLaunchContext:
    """Everything a launcher needs to run the fresh-context reviewer."""

    worktree: Path
    prompt: str
    prompt_artifact_sha: str
    """sha256 of the BASE artifact *prompt* was assembled from (#3024)."""
    run_id: str
    item_id: str
    head_sha: str
    log_dir: Path
    criterion_ids: tuple[str, ...] = ()
    """Ids of the recorded criteria *prompt* carries (#2291), in plan order.

    The launcher hands them to ``collect_stream_verdict`` so the parser holds
    the verdict to exactly the ids the reviewer was shown. Empty means no plan
    section was rendered and the criterion rules do not apply."""
    contract_test_ids: tuple[str, ...] = ()
    """The ``kind = test`` ids among ``criterion_ids`` (#2732), whose results
    the parser binds to the acceptance-contract test outcome the verdict cites."""
    integrity_flags: tuple[str, ...] = ()
    """The test-integrity flags *prompt* asked the reviewer to answer (#2486).

    The launcher hands them to ``collect_stream_verdict`` so a READY that
    leaves one unanswered or REJECTED is refused. Empty means a clean report,
    no report, or a deployed prompt that predates the contract."""
    declared_tests: tuple[str, ...] = ()
    """The tests the PR body's **Behavior changes** section declares, as
    *prompt* shows them (#3516). The launcher hands them to
    ``collect_stream_verdict`` so a declaration finding asking only for them is
    dropped, as the prompt told the reviewer it would be."""
    frontend_lock_sha256: str | None = None
    """sha256 of the BASE ``frontend/package-lock.json`` when the PR leaves it
    unchanged (#2559): the launcher mounts the cached ``node_modules`` built
    from it read-only. None means no mount, and the prompt already says why."""
    prompt_profile_version_id: int | None = None
    """The approved reviewer profile version *prompt* carries (#3024), from the
    same build that rendered it, so the run row names the version the reviewer
    received. None means no active profile."""
    base_ref: str | None = None
    """The ref the base artifact was read from (#3088)."""
    base_commit: str | None = None
    """The commit whose artifact bytes hash to ``prompt_artifact_sha`` (#3088)."""


STALE_ARTIFACT_CRITERIA_REASON = (
    "the reviewer prompt deployed on this repo's base ref predates the "
    "recorded-criteria contract, so this round's reviewer was never given them "
    "(sync the pinned artifact to restore it)"
)
"""Why a round with a recorded plan carried no criteria section (#2291).

Host-authored and surfaced on the verdict comment, not just the driver log:
"the artifact is stale" and "the issue stated no criteria" produce the same
empty round, and only one of them is a reason to hold the merge.
"""


@dataclass(frozen=True)
class _PromptBuild:
    """One round's prompt and what it did — or could not — carry.

    ``plan`` and ``unjudged`` are mutually exclusive: either the criteria
    reached the reviewer or they did not, and at most one of those is a fact
    about this round. The accessors below are what "no plan section" means for
    each round fact, resolved once here so the dispatch path reads them rather
    than re-deciding it per field.
    """

    prompt: str
    artifact_sha: str
    """sha256 of the BASE artifact this prompt was assembled from (#3024): the
    round's ``prompt_artifact_sha``, which no addendum or host section moves."""
    plan: ReviewPlan | None
    unjudged: UnjudgedCriteria | None = None
    contracts_in_force: bool = True
    """Whether the loaded prompt is the pinned one, which states the criteria
    and ``test_changes`` contracts the parser enforces (#2291, #2486)."""
    profile_version_id: int | None = None
    """The operator addendum's version row id (#3024), None for none."""
    base_ref: str | None = None
    """The ref the artifact was read from (#3088); the launch sets it."""
    base_commit: str | None = None
    """The commit the artifact was read out of (#3088); the launch sets it."""

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        """The ids the prompt showed — none, when it showed no criteria."""
        return () if self.plan is None else self.plan.criterion_ids

    @property
    def contract_test_ids(self) -> tuple[str, ...]:
        """The shown ids a contract test proves — none, with no criteria."""
        return () if self.plan is None else self.plan.contract_test_ids

    @property
    def plan_sha(self) -> str | None:
        """The plan the round was judged against, None when it was nothing."""
        return None if self.plan is None else self.plan.plan_sha

    @property
    def criteria_json(self) -> str | None:
        """Those criteria as the prompt carried them."""
        return None if self.plan is None else self.plan.criteria_json

    @property
    def omitted_criterion_ids(self) -> tuple[str, ...]:
        """Recorded ids the prompt could not carry, so outside the contract."""
        return () if self.plan is None else self.plan.omitted_criterion_ids


def operator_addendum_section(role: str, addendum: ProfileAddendum) -> str:
    """*addendum* rendered as the one operator block BEFORE the reviewer rubric.

    The block opens the prompt and ends with the host-owned
    :data:`_REVIEW_HOST_PRECEDENCE` sentence, so the rubric and every host
    section rendered after it take precedence (#3088): an addendum adds to the
    reviewed rules, it never overrides them.
    """
    return (
        f"## Operator instructions — {role} profile v{addendum.version}\n\n"
        "The operator approved these instructions for this role. They add to "
        "the review rules below.\n\n"
        f"{addendum.body.strip()}\n\n"
        f"{_REVIEW_HOST_PRECEDENCE}\n\n"
    )


def reviewer_prompt_head(
    artifact_text: str,
    artifact_sha: str,
    plan_row: ItemPlanRow | None,
    *,
    item_id: str,
    max_review_usd: float,
    addendum: ProfileAddendum | None = None,
) -> _PromptBuild:
    """The launch-time half of the reviewer prompt, from its inputs alone.

    The operator's active ``adversarial_review`` *addendum* when there is one
    (#3024) and the host precedence sentence, then the loaded artifact, then
    the host-owned diff boundary and runtime guidance, then *plan_row*'s
    recorded acceptance criteria (#2291). The addendum renders before the
    whole rubric so no rubric rule is left above it to override (#3088).
    *artifact_sha* is the BASE artifact's sha: an addendum never switches the
    criteria contract off. Pure:
    the caller reads the artifact and the ledger row. Shared with the prompt
    catalog's preview (#3018), so the preview is this assembly, not a copy.

    A deployed artifact older than the one the manifest pins carries no
    criteria section (``criteria_contract_in_force``): its reviewer was never
    told the rules the parser would hold it to, and enforcing them anyway would
    lose every round on a planned item to the crash path. The round still
    records no ``plan_sha`` — nothing was judged — but the ledger's plan comes
    back as ``unjudged`` so the verdict comment reports a skipped contract
    instead of asserting the item recorded none.
    """
    prompt = artifact_text
    profile_version_id = None
    if addendum is not None:
        prompt = operator_addendum_section(REVIEWER_PROFILE_ROLE, addendum) + prompt
        profile_version_id = addendum.version_id
    prompt += _REVIEW_DIFF_HEADING + _REVIEW_DIFF_SUFFIX
    prompt += _REVIEW_RUNTIME_GUIDANCE.format(max_review_usd=max_review_usd)
    if not criteria_contract_in_force(artifact_sha):
        _logger.warning(
            "review: %s — the deployed reviewer prompt (sha %s) predates the "
            "recorded-criteria contract, so this round is judged against no "
            "recorded criteria; sync the artifact to restore it",
            item_id,
            artifact_sha[:12],
        )
        return _PromptBuild(
            prompt=prompt,
            artifact_sha=artifact_sha,
            plan=None,
            unjudged=unjudged_criteria(plan_row, reason=STALE_ARTIFACT_CRITERIA_REASON),
            contracts_in_force=False,
            profile_version_id=profile_version_id,
        )
    plan = review_plan_section(plan_row)
    return _PromptBuild(
        prompt=prompt if plan is None else prompt + plan.section,
        artifact_sha=artifact_sha,
        plan=plan,
        profile_version_id=profile_version_id,
    )


def assemble_reviewer_prompt(
    build: _PromptBuild,
    *,
    mode_section: str,
    frontend_section: str,
    integrity: TestIntegrityReport | None,
    proposed: Sequence[str] = (),
    declared: Mapping[str, str] | Sequence[str] = (),
) -> str:
    """The whole reviewer prompt: *build* plus the round's checkout sections.

    The delta/full section (#2111), the frontend dependency section (#2559)
    and the test-integrity report (#2486), in that order, the last with the PR
    body's *proposed* Behavior changes lines to rule on (#3475) and the tests
    its **Behavior changes** section already *declared*, with each line's
    reason (#3516). Pure, and shared with the prompt catalog's preview (#3018).
    """
    prompt = build.prompt + mode_section + frontend_section
    return prompt + integrity_section(
        integrity,
        required=build.contracts_in_force,
        proposed=proposed,
        declared=declared,
    )


def reviewer_received_prompt(prompt: str, head_sha: str) -> str:
    """*prompt* plus the host-owned verdict channel: what the reviewer reads.

    The launcher hands exactly this text to the runner, and the round's
    ``effective_prompt_sha`` (#3024) is computed over it.
    """
    return (
        f"{prompt}\n\n"
        "## Verdict channel\n\n"
        "End your run with ONE final message that contains your verdict JSON "
        "between these two delimiter lines, and nothing else between them:\n\n"
        f"{VERDICT_BEGIN}\n"
        '{"verdict": "...", "head_sha": "...", ...}\n'
        f"{VERDICT_END}\n\n"
        f'Set "head_sha" to exactly {head_sha}. kew reads the verdict only '
        "from your final message as the runner reports it. It reads no file, "
        "so do not write the verdict to disk, and write each delimiter exactly "
        "once in that message.\n\n"
        "Stage scratch files under /work when possible; captured command "
        "output belongs there too. /tmp is container-local but may be denied by "
        "a contained permission profile.\n"
    )


def _reviewer_prompt(
    store: RunStore,
    *,
    item_id: str,
    repo_root: Path,
    base_ref: str,
    base_commit: str,
    max_review_usd: float,
) -> _PromptBuild:
    """The reviewer's prompt as of launch, plus the recorded plan it carries.

    The pinned artifact (the deployed copy *base_commit*'s tree carries — the
    commit the reviewer checkout's base is cloned at — read out of Git and
    never off the operator's working tree, #3087) and the item's
    recorded plan read from the LEDGER row — never from the head ref, which
    the branch under review can rewrite — assembled by
    :func:`reviewer_prompt_head`. The round's checkout sections are appended
    by the caller inside its cleanup guard (:func:`assemble_reviewer_prompt`),
    and the effective sha is computed over all of it, so the attestation covers
    the criteria this round was actually judged against.

    The operator addendum (#3024) is read from the host store, never from the
    checkout under review: a PR cannot change the prompt that judges it. It is
    read through :func:`~kew.service.prompt_profiles.launch_addendum`, so a
    body that no longer matches its approved digest, or that opens a Markdown
    block, is refused before any checkout or reviewer exists: a
    :class:`ReviewerLaunchError` naming the base artifact that governed the
    attempt, caused by the :class:`~kew.prompt_addendum.ProfileAddendumRefusedError`.
    """
    from kew.service.prompt_profiles import launch_addendum

    loaded = resolve_reviewer_prompt(repo_root, base_ref, base_commit=base_commit)
    prompt, artifact_sha = loaded.text, loaded.sha256
    _logger.info(
        "review: %s reviewer artifact %s (sha %s) from %s at %s",
        item_id,
        loaded.deployed or "packaged default",
        artifact_sha[:12],
        base_ref,
        loaded.base_commit or "unresolved",
    )
    try:
        addendum = launch_addendum(store, REVIEWER_PROFILE_ROLE)
    except ProfileAddendumRefusedError as exc:
        raise ReviewerLaunchError(
            PromptProvenance(
                prompt_artifact_sha=artifact_sha,
                base_ref=loaded.base_ref,
                base_commit=loaded.base_commit,
            )
        ) from exc
    build = reviewer_prompt_head(
        prompt,
        artifact_sha,
        store.current_item_plan(item_id),
        item_id=item_id,
        max_review_usd=max_review_usd,
        addendum=addendum,
    )
    return replace(build, base_ref=loaded.base_ref, base_commit=loaded.base_commit)


def _refuse_review_budget(
    store: RunStore,
    config: DispatchConfig,
    *,
    node_epic_ref: str,
    item_id: str,
    issue_num: int,
    run_id: str,
    exc: BudgetExceeded,
    blocking: bool,
    now: datetime,
) -> None:
    """Mirror ``dispatch_core._refuse_budget`` for the review pool and the
    daily and weekly caps (#3205), each held under its own reason.

    Order matters (rollback-before-best-effort-IO, concurrent-state-change): the
    reservation is released generation-matched (#972) and the terminal refusal
    row is written FIRST, so the reconciler can never misread the freed slot as a
    silent corpse. In blocking mode the node is HELD with a ``review_budget``
    reason the overlay routes (Task 4); the audit event records the refusal
    either way. Spend never comes from a sidecar — this path has no sidecar.
    """
    store.release_epic_node_item(item_id, run_id=run_id)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            status="refused",
            exit_code=1,
            started_at=now,
            finished_at=now,
            error_category=_REVIEW_BUDGET_REFUSAL_CATEGORY,
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            repo=config.project.repo or None,
        )
    )
    if blocking:
        reason = (
            window_budget_hold_reason(exc)
            if exc.which_limit in WINDOW_LIMITS
            else review_budget_hold_reason(
                limit_usd=exc.limit_usd,
                spent_usd=exc.spent_usd,
                config_key=REVIEW_POOL_KEY,
            )
        )
        store.hold_epic_node(node_epic_ref, item_id, reason)
    audit.emit(
        store,
        "dispatch_completed",
        actor=resolve_actor(),
        issue_num=issue_num,
        item_id=item_id,
        repo=config.project.repo or None,
        payload={
            **orchestrator_provenance(),
            "complexity": "",
            "runner": "",
            "model": "",
            "item_id": item_id,
            "reported_model": None,
            "exit_code": 1,
            "duration_seconds": 0.0,
            "cost_usd": 0.0,
            "tokens": 0,
            "attempt_number": 0,
            "error_category": _REVIEW_BUDGET_REFUSAL_CATEGORY,
            "run_kind": ADVERSARIAL_REVIEW_RUN_KIND,
        },
    )


def _record_flagged_tests(
    sidecar: CollectedSidecar | None, integrity: TestIntegrityReport | None
) -> str | None:
    if sidecar is None or integrity is None:
        return None
    return record_flagged_tests(sidecar.stored_path, integrity)


def _reraise_with_provenance(
    exc: BaseException, provenance: PromptProvenance | None
) -> NoReturn:
    """Re-raise *exc*, as a :class:`ReviewerLaunchError` once the prompt was
    known (#3024); an interrupt or a pre-prompt raise propagates unchanged."""
    if provenance is not None and isinstance(exc, Exception):
        raise ReviewerLaunchError(provenance) from exc
    raise exc


def _admit_refused_or_reraise(
    exc: BaseException,
    provenance: PromptProvenance | None,
    refuse: Callable[..., None],
) -> None:
    """Refuse the round when the launcher's admit transaction refused the
    reviewer's row on a daily or weekly cap (#3205) — a concurrent launch took
    the window's headroom after the preflight — and re-raise anything else."""
    if not isinstance(exc, BudgetExceeded):
        _reraise_with_provenance(exc, provenance)
    refuse(exc=exc)


def _materialize_or_attest(
    repo_root: Path, head_sha: str, base_commit: str, provenance: PromptProvenance
) -> Path:
    """:func:`materialize_reviewer_worktree`, whose raise names the base
    artifact that governed the attempt (#3068); it cleans up after itself."""
    try:
        return materialize_reviewer_worktree(repo_root, head_sha, base_commit)
    except BaseException as exc:
        _reraise_with_provenance(exc, provenance)


def dispatch_reviewer(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    issue_num: int,
    head_sha: str,
    repo_root: Path,
    base_ref: str,
    max_review_usd: float,
    run_id: str,
    node_epic_ref: str,
    blocking: bool = True,
    worker_cap: int = 0,
    runtime_cap_usd: float = 0.0,
    cycle_start: datetime | None = None,
    launcher: ReviewerLauncher | None = None,
    now: datetime | None = None,
    mode_plan: ReviewModePlan | None = None,
    reserved: bool = False,
    pr_body: str | None = None,
) -> ReviewerDispatch:
    """Dispatch a fresh-context sandboxed reviewer against *head_sha* (Task 6).

    Order: atomic reservation under the ``review`` pseudo-epic (#939) →
    review-pool budget preflight (F5) → worktree materialization at the recorded
    sha with head-ref masking → fresh-context launch. A lost/full reservation or
    a budget refusal returns without launching; a refusal additionally holds the
    node (blocking) and attests the refusal.

    ``reserved`` says the caller already holds the node's review reservation
    under the fleet cap — a detached review-stage child (#2459) — so step 1 is
    skipped; the caller releases what it took.

    ``pr_body`` is the PR description as read for this round; with it, an
    existing test the diff edits and its Behavior changes section does not
    name is flagged before READY rather than at the merge's held-out gate
    (#2830). ``None`` (not read) skips that one check.
    """
    now = now or datetime.now(UTC)

    # 1. Atomic reservation under the review pseudo-epic (#939 idiom): count +
    #    insert fold into one BEGIN IMMEDIATE, so a live reviewer mid-startup is
    #    never double-dispatched and never derives node status.
    reserve = (
        "reserved"
        if reserved
        else store.reserve_epic_node_item_under_cap(
            REVIEW_EPIC_REF, item_id, run_id, worker_cap
        )
    )
    if reserve == "fleet_full":
        return ReviewerDispatch("fleet_full", run_id, item_id, head_sha)
    if reserve == "lost_claim":
        return ReviewerDispatch("reservation_lost", run_id, item_id, head_sha)

    # 2. Review-pool and window budget preflight — the reservation is held, so
    #    a refusal must release it (mirrored refusal semantics). The windows
    #    count this launch at its ceiling; the launcher checks them again in
    #    the transaction that admits the reviewer's row (#3205).
    refuse = partial(
        _refuse_review_budget,
        store,
        config,
        node_epic_ref=node_epic_ref,
        item_id=item_id,
        issue_num=issue_num,
        run_id=run_id,
        blocking=blocking,
        now=now,
    )
    try:
        check_review_budget(
            store,
            item_id,
            max_review_usd,
            since=cycle_start,
            now=now,
            pending_reservation_usd=runtime_cap_usd,
        )
        check_window_budget(
            store,
            config.budget,
            reservation_usd=max_review_usd,
            now=now,
            count_admitting_run=True,
        )
    except BudgetExceeded as exc:
        refuse(exc=exc)
        return ReviewerDispatch("budget_refused", run_id, item_id, head_sha)

    # 3. Materialize the worktree at the recorded sha + mask head-ref context.
    #    The prompt (artifact + host sections + the recorded criteria, #2291) is
    #    assembled first, before any checkout exists, so a raise there leaks
    #    nothing; materialize_reviewer_worktree owns its own cleanup. The base
    #    resolves ONCE, by the checkout's own rule (origin for a named ref), so
    #    the prompt is read off the very commit the reviewer is diffed against.
    base_commit = resolve_base_commit(repo_root, base_ref)
    build = _reviewer_prompt(
        store,
        item_id=item_id,
        repo_root=repo_root,
        base_ref=base_ref,
        base_commit=base_commit,
        max_review_usd=max_review_usd,
    )
    # The base artifact is known from here on, so every later pre-launch raise
    # (the checkout, the mode/frontend/integrity reads) still names it (#3068);
    # no reviewer has received a prompt yet, so nothing else is attested.
    provenance = PromptProvenance(
        prompt_artifact_sha=build.artifact_sha,
        base_ref=build.base_ref,
        base_commit=build.base_commit,
    )
    worktree = _materialize_or_attest(repo_root, head_sha, base_commit, provenance)

    # 4. Fresh-context launch (seam; real container wiring lands in Group C).
    #    A launcher raise must not leak the materialized checkout — remove it and
    #    re-raise. The happy path returns the live worktree (cleaned post-verdict).
    #    The delta/full decision (#2111) needs both heads in the checkout, so it
    #    runs here, inside the cleanup guard; the effective sha covers its section.
    sidecar: CollectedSidecar | None = None
    record_error: str | None = None
    try:
        # Before the launch: the reviewer can write to this checkout (#3422).
        # Before the mode too: a gate-path head is never reviewed as a delta.
        gate_paths = read_gate_paths(worktree, config)
        review_mode, mode_section = resolve_review_mode(
            worktree, full_on_gate_paths(mode_plan, gate_paths), repo_root=repo_root
        )
        # The frontend dependency decision (#2559) is read off the pinned refs
        # here, so the attested prompt carries it; the launcher only mounts.
        frontend = plan_frontend_review(worktree, _REVIEW_BASE_REF, _REVIEW_HEAD_REF)
        # The test-integrity report (#2486) over the same pinned refs; its
        # flags are what the parser holds a READY to answering.
        integrity = read_integrity_report(
            worktree, _REVIEW_BASE_REF, _REVIEW_HEAD_REF, pr_body=pr_body
        )
        # The run that published this head gets the same counts in its report
        # (#2494): this is the first kew-trusted checkout holding its diff.
        export_published_integrity(
            config,
            store,
            item_id=item_id,
            head_sha=head_sha,
            diff=IntegrityDiff(worktree, _REVIEW_BASE_REF, _REVIEW_HEAD_REF),
        )
        integrity_flags = (
            integrity.flags
            if integrity is not None and build.contracts_in_force
            else ()
        )
        reasons = declared_reasons(pr_body)
        declared = tuple(reasons)
        prompt = assemble_reviewer_prompt(
            build,
            mode_section=mode_section,
            frontend_section=frontend.section,
            integrity=integrity,
            proposed=proposed_section_lines(pr_body),
            declared=reasons,
        )
        # Known before the launch, so a raise from the launcher or the
        # bookkeeping after it still attests the prompt the reviewer ran under.
        provenance = PromptProvenance(
            prompt_artifact_sha=build.artifact_sha,
            effective_prompt_sha=hashlib.sha256(
                reviewer_received_prompt(prompt, head_sha).encode()
            ).hexdigest(),
            prompt_profile_version_id=build.profile_version_id,
            base_ref=build.base_ref,
            base_commit=build.base_commit,
        )
        if launcher is not None:
            log_dir = Path(config.logging.dir).resolve()
            ctx = ReviewerLaunchContext(
                worktree=worktree,
                prompt=prompt,
                prompt_artifact_sha=build.artifact_sha,
                run_id=run_id,
                item_id=item_id,
                head_sha=head_sha,
                log_dir=log_dir,
                criterion_ids=build.criterion_ids,
                contract_test_ids=build.contract_test_ids,
                integrity_flags=integrity_flags,
                declared_tests=declared,
                frontend_lock_sha256=frontend.lock_sha256,
                prompt_profile_version_id=build.profile_version_id,
                base_ref=build.base_ref,
                base_commit=build.base_commit,
            )
            sidecar = launcher(ctx)
            # The remediation leg reads which tests to declare from here on a
            # later tick (#2872); a record that did not land keeps the round
            # from being recorded at all.
            record_error = _record_flagged_tests(sidecar, integrity)
    except BaseException as exc:
        try:
            shutil.rmtree(worktree, ignore_errors=True)
        finally:
            release_checkout_claim(worktree)
        _admit_refused_or_reraise(exc, provenance, refuse)
        return ReviewerDispatch("budget_refused", run_id, item_id, head_sha)

    return ReviewerDispatch(
        outcome="dispatched",
        run_id=run_id,
        item_id=item_id,
        head_sha=head_sha,
        prompt_artifact_sha=provenance.prompt_artifact_sha,
        effective_prompt_sha=provenance.effective_prompt_sha,
        prompt_profile_version_id=provenance.prompt_profile_version_id,
        base_ref=provenance.base_ref,
        base_commit=provenance.base_commit,
        worktree=worktree,
        sidecar=sidecar,
        review_mode=review_mode,
        plan_sha=build.plan_sha,
        criteria_json=build.criteria_json,
        omitted_criterion_ids=build.omitted_criterion_ids,
        unjudged_criteria=build.unjudged,
        gate_paths=gate_paths,
        declaration_record_error=record_error,
        pr_body=pr_body,
    )
