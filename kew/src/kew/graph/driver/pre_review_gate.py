"""The merge gate's static checks and ui-checks, run on a head before review (#3201).

A head that the merge gate would refuse for a ruff, mypy or bandit finding, or
whose browser checks break once current ``origin/<base>`` is merged in, used to
reach a paid adversarial reviewer first, and the defect surfaced only at merge.
This step runs those gates before any reviewer launches:

- ``[commands] static`` over the head;
- ``[commands] ui_check`` over a throwaway merge of the current base into the
  head (the merge candidate), for the ui-check modules the head touches
  (:func:`kew.ui_check_modules.touched_modules`).

Both run inside the approved image — ``[sandbox] check_image`` when one is set
(:func:`kew.loops.verification.resolve_check_image_admission`) — with no
credentials and no network
(:meth:`kew.loops.verification.CommandCondition.check_admitted_directory`),
never on the host: mypy loads the plugins the candidate's ``pyproject.toml``
names, so the static command executes candidate code. Without a container the
step does not run and the head goes to review as it did before.

**Host git never reads a tree a container wrote.** Every host git step — the
base fetch, the diff, the module selection, the merge that builds the candidate
— runs in a preparation clone no container ever mounts, before any container
starts, under the inert git environment
(:func:`kew.worktree.checkout_object_env`: no hooks, no fsmonitor, no replace
refs or grafts). Each container then gets its own clone of one commit, made
from that preparation clone, and host git never touches it again: a hook, an
fsmonitor or a replace ref the candidate plants there runs nowhere and moves
nothing. Paths a finding cites are checked against the commit's tree as git
listed it beforehand, never against the container-written directory.

**A failure counts against the head only when the base passes.** A command
that fails on the head is run once more, the same way, on the base commit. If
the base fails too, the image lacks what the command needs (the image has no
network, so dependencies must be prebuilt into it) or the base is red, and the
check is ``skipped``, never a finding. A command that exits 126 or 127 (not
executable, not found) is skipped without the control run.

That rule assumes the image serves the base and the head alike, which a
``[sandbox] check_image`` does not: its dependencies were frozen when it was
built. A head that changes a file it was built from can fail there for a
dependency it added while the base passes, and an agent with no network cannot
fix that. Those files are ``[sandbox] check_image_inputs`` (empty: every
dependency manifest and lock in the tree), counted only where ``[epic]
merge_gate_paths`` names them too, the rule the held-out half uses. In the check
image, a red result on such a head (not a missing tool) is ``skipped`` with that
reason, never a finding: the merge queue holds the head for a person, and the
merge verify, which installs the head's own lock, runs the gate. When the diff
that names those files cannot be taken (no merge base), the base decides as for
any other failure.

With ``[verify] held_out_before_review`` the step also runs the merge
verify's **held-out** phase (#3212) on the merge candidate, on the verify host
rather than in a container — its runs sync their own environment from the lock,
which the container's missing network cannot. That host is never this machine:
with ``[verify] host = "local"`` the half is skipped, because the run imports
the head's ``src`` before any review, and this machine's shell has the
operator's credentials in reach. It runs only for a change whose held-out
selection is a set of test files: a change that selects the whole base tree is
left to the merge verify, which runs it anyway. The base's own tests run against
the candidate and are judged against the PR body's **Behavior changes** section
by the merge's own rule (:func:`kew.merge_verify.run_held_out_check`), so a
failure no line declares is sent back in seconds, before a reviewer is paid,
instead of at the merge after the whole suite. The declaration it asks for is
the one the merge still requires; only the time it is asked for changes. A
failure counts against the head only when it is not main's: a test main's
health lists as red is never one, and every other failing test runs once more
on the base's own ``src``, and a test that fails there too is main's.

A failure is recorded as a NEEDS_CHANGES round carrying the output
(``review_mode`` :data:`REVIEW_MODE_PRE_REVIEW_GATE`), which the review stage
remediates; no reviewer launches on that head. A check that could not run (a
launch the runtime refused, a timeout, a tool the image lacks, a red base, a
verify host that was busy or down) proves nothing about the head and is never
recorded as a defect. It is recorded as a skip instead, per item and per head
with its reason (:func:`record_skips`, #3280): the tick's outcome, ``kew epic
status`` and the PR's verdict comment each say the gate did not check that
head, and why. A gate that ran and passed records nothing.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from functools import cache, partial
from itertools import chain
from pathlib import Path
from typing import Literal

from kew import work_dirs
from kew.config import DispatchConfig
from kew.credential_surface import redact_credentials
from kew.graph.driver.merge_queue import touched_gate_paths
from kew.graph.driver.overlay import DriverStatus
from kew.held_out_selection import FULL, SELECTED
from kew.hosts import HostError, HostTimeout
from kew.ids import item_ref
from kew.loops.verification import (
    CONTAINED_VERIFICATION_POSTURE,
    CommandCondition,
    ConditionResult,
    HostVerificationAdmission,
    resolve_check_image_admission,
)
from kew.merge_verify import (
    HELD_OUT_CHECK_LOCAL_REFUSAL,
    HELD_OUT_PROJECT,
    HeldOutReport,
    VerifyCandidate,
    VerifyRefused,
    behavior_change_lines,
    held_out_check_candidate,
    red_on_main,
    run_held_out_check,
)
from kew.public_log import public_line
from kew.review_contract import PRE_REVIEW_GATE_LOCATION, GateFinding
from kew.review_delta import REVIEW_MODE_PRE_REVIEW_GATE
from kew.run_store import RunStore
from kew.sandbox import DEFAULT_IMAGE_RUNNER
from kew.subprocess_utils import run
from kew.ui_check_modules import UI_CHECKS_DIR, touched_modules, uncovered_surfaces
from kew.worktree import (
    checkout_object_env,
    claim_checkout,
    clone_local_checkout,
    release_checkout_claim,
    resolve_base_commit,
)

_logger = logging.getLogger(__name__)

#: Rounds this step may record in one review cycle before the node is held. The
#: rounds sit outside ``[review] max_rounds`` (see
#: :data:`REVIEW_MODE_PRE_REVIEW_GATE`), so this is what bounds a head that
#: keeps failing the same gate however often it is remediated.
MAX_GATE_ROUNDS = 3

#: The step's budget per command. A full static gate set legitimately runs for
#: minutes; this is kew's constant, not operator input.
GATE_TIMEOUT_SECONDS = 1800.0

#: Files a finding may cite, at most: the brief scopes its tests to them.
_MAX_CITED_PATHS = 20

_SETTLED_META_PREFIX = "pre_review_gate_settled:"
_SKIPPED_META_PREFIX = "pre_review_gate_skipped:"
#: The most of one skip's reason kept: a red base's reason carries its output.
_MAX_SKIP_CHARS = 500
#: An IPv4 address in a skip's reason (a verify host's ssh error names one).
#: An address ending a sentence (``Last tried 10.0.0.7.``) is still one.
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
#: ``user@`` before a host already redacted to a marker: the public cleaner's
#: ``user@host`` rule wants a letter after ``@``, so a configured name or an
#: IPv4 address swapped out around it leaves the user behind.
_USER_AT_MARKER = re.compile(r"[\w.+-]+@<[\w-]+>")
#: How much text the public cleaner may hand back before the reason is
#: trimmed: its markers can lengthen it, and a cut must come after them.
_PUBLIC_CHARS = 1 << 16
#: Node statuses whose head is finished with: a skip on it is no longer news.
_FINISHED_STATUSES = frozenset({DriverStatus.DONE.value, DriverStatus.CANCELLED.value})

STATIC_GATE = "static checks"
UI_CHECK_GATE = "ui-checks on the merge candidate"
MERGE_GATE = "merges with the base"
HELD_OUT_GATE = "held-out base tests on the merge candidate"

#: The halves of the step, as their results are remembered per head.
STATIC_HALF = "static"
UI_HALF = "ui_check"
HELD_OUT_HALF = "held_out"
_HALVES = (STATIC_HALF, UI_HALF, HELD_OUT_HALF)

#: ``sh`` exit statuses for a command it could not execute (126) or find (127):
#: a tool the image does not carry, not a defect in the head.
_NOT_RUNNABLE_EXITS = frozenset({126, 127})

#: Why a failure in the check image is not the head's when the head changes
#: what that image was built from (see the module doc).
STALE_CHECK_IMAGE_REASON = "the head changes the check image's inputs"

#: The check image's inputs when ``[sandbox] check_image_inputs`` is empty:
#: every dependency manifest and lock in the tree, which is broad.
_DEFAULT_CHECK_IMAGE_INPUTS = (
    "**/pyproject.toml",
    "**/uv.lock",
    "**/uv.toml",
    "**/setup.py",
    "**/setup.cfg",
    "**/.python-version",
    "**/package*.json",
    "**/.npmrc",
    "**/.nvmrc",
)

#: Git modes of a regular file; a symlink (``120000``) or gitlink never counts.
_REGULAR_MODES = frozenset({"100644", "100755"})

#: The most of one ui-check module the host reads to find its ownership lines.
_MAX_MODULE_BYTES = 256 * 1024

#: ``path:line`` as ruff, mypy, bandit, xenon and semgrep print a finding.
_CITED = re.compile(r"(?<![\w./-])((?:[\w.()\[\]+@-]+/)*[\w.()\[\]+@-]+\.\w+):(\d+)")
_EXITED = re.compile(r"^command exited -?\d+: ")
#: The stream label a contained run's evidence puts before each stream.
_STREAM_LABEL = re.compile(r"^(?:stdout|stderr): ?")

#: The scratch refs the preparation clone names its commits by, so a container
#: clone made from it receives exactly those.
_HEAD_REF = "refs/heads/kew-gate-head"
_BASE_REF = "refs/heads/kew-gate-base"
_CANDIDATE_REF = "refs/heads/kew-gate-candidate"

HalfKind = Literal["passed", "failed", "skipped", "uncovered"]

#: How the step reads its halves together: the first kind any half has wins.
_PRECEDENCE: tuple[HalfKind, ...] = ("failed", "skipped", "uncovered")


@dataclass(frozen=True)
class GateOutcome:
    """What the step found: a pass, the failures, or why it did not run."""

    kind: HalfKind
    findings: tuple[GateFinding, ...] = ()
    detail: str = ""
    ran: tuple[str, ...] = field(default=())
    """The gates that actually ran, for the log line."""
    settled: tuple[str, ...] = ()
    """The halves whose result stands for this head (passed, or skipped for a
    reason the head cannot change), so a deferred review does not re-run them."""
    uncovered: tuple[str, ...] = ()
    """Changed console source files no ui-check module covers."""
    held_out: tuple[str, ...] = ()
    """The base tests the held-out half found failing undeclared, by node id."""
    skipped: tuple[tuple[str, str], ...] = ()
    """``(half, why)`` for each half that did not check the head (#3280)."""


@dataclass(frozen=True)
class _Half:
    """One half's result, before the step combines them."""

    name: str
    kind: HalfKind
    findings: tuple[GateFinding, ...] = ()
    detail: str = ""
    ran: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()
    held_out: tuple[str, ...] = ()
    settles: bool = True
    """False for a half that did not run for a reason the next try may not
    share (a busy verify host, a runtime that refused the launch)."""


@dataclass(frozen=True)
class _Prepared:
    """Every fact host git established, before any container started."""

    clone: Path
    base_sha: str
    head: str
    head_files: frozenset[str]
    modules: tuple[str, ...] = ()
    control_modules: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()
    candidate: str | None = None
    candidate_files: frozenset[str] = frozenset()
    conflicted: tuple[str, ...] = ()
    merge_error: str | None = None
    """Why the merge with the base did not run, when it failed with no
    conflict: no half that needs the candidate can run on this try."""


def gate_configured(config: DispatchConfig) -> bool:
    """Whether the operator configured any gate this step runs."""
    return bool(configured_halves(config))


def configured_halves(config: DispatchConfig) -> frozenset[str]:
    """The halves the operator configured a command for."""
    halves = set()
    if config.commands.static.strip():
        halves.add(STATIC_HALF)
    if config.commands.ui_check.strip():
        halves.add(UI_HALF)
    if config.verify.held_out_before_review:
        halves.add(HELD_OUT_HALF)
    return frozenset(halves)


def settled_key(item_id: str, half: str) -> str:
    """Meta key holding the last head *half* settled on for *item_id*."""
    return f"{_SETTLED_META_PREFIX}{half}:{item_id}"


def settled_halves(store: RunStore, item_id: str, head: str) -> frozenset[str]:
    """The halves whose result already stands for *head*."""
    return frozenset(
        half for half in _HALVES if store.get_meta(settled_key(item_id, half)) == head
    )


def record_settled(
    store: RunStore, item_id: str, head: str, halves: Collection[str]
) -> None:
    """Remember that *halves* settled on *head*, so they are not run again."""
    for half in halves:
        store.set_meta(settled_key(item_id, half), head)


def record_skips(
    store: RunStore,
    item_id: str,
    head: str,
    skipped: Iterable[tuple[str, str]],
    *,
    kept: Collection[str] = (),
    private: Mapping[str, str] | None = None,
) -> None:
    """Record why the gate did not check *head*, half by half (#3280).

    The record is per item and per head, so a deferred review of the same head
    and ``kew epic status`` read it after the tick is gone. Of an earlier
    record for *head*, only the halves in *kept* (settled, so not run again)
    stand; every other half's entry is replaced by this run's result. A
    *skipped* that is empty, on halves that all ran, clears the record.

    Each reason is redacted before it is kept (:func:`_public_reason`): the
    PR's verdict comment prints it, and a host error carries what the host
    said. *private* maps names the configuration knows (:func:`private_names`)
    to what stands in for them.
    """
    prior = _skip_entries(store, item_id, head)
    entries = {half: why for half, why in prior.items() if half in kept}
    entries.update((half, _public_reason(why, private or {})) for half, why in skipped)
    store.set_meta(
        _SKIPPED_META_PREFIX + item_id, json.dumps({"head": head, "skips": entries})
    )


def private_names(config: DispatchConfig) -> dict[str, str]:
    """The ``[hosts.gcp]`` names a host error can print, each mapped to what
    stands in for it in a public reason."""
    gcp = config.hosts.gcp
    names = {gcp.project: "<gcp-project>", gcp.zone: "<gcp-zone>"}
    names.update((name, "<gcp-instance>") for name in (gcp.instance, *gcp.instances))
    return {name: marker for name, marker in names.items() if name.strip()}


def _public_reason(why: str, private: Mapping[str, str]) -> str:
    """*why* as a public PR comment may carry it, one line and bounded.

    The configured *private* names go first, by exact match; then the public
    log cleaner (:func:`kew.public_log.public_line`: credentials, URL
    secrets, secret assignments, path ancestry, GCP hosts, projects and
    buckets, addresses, IPv6 and this machine's name); then IPv4 addresses;
    then the user of any ``user@`` left before a redacted host.
    """
    text = redact_credentials(why)
    for name in sorted(private, key=len, reverse=True):
        pattern = rf"(?<![\w.-]){re.escape(name)}(?![\w-])"
        text = re.sub(pattern, private[name], text)
    text = public_line(text, limit=_PUBLIC_CHARS)
    text = _IPV4.sub("<ip>", text)
    return _USER_AT_MARKER.sub("<address>", text)[:_MAX_SKIP_CHARS]


def gate_skip_reason(store: RunStore, item_id: str, head: str) -> str | None:
    """Why the gate did not check *head*, or None when nothing was skipped."""
    entries = _skip_entries(store, item_id, head)
    return "; ".join(entries.values()) or None


def gate_skip(store: RunStore, item_id: str) -> tuple[str, str] | None:
    """``(head, why)`` for the last head of *item_id* the gate skipped."""
    head = _skip_record(store, item_id).get("head")
    if not isinstance(head, str):
        return None
    reason = gate_skip_reason(store, item_id, head)
    return None if reason is None else (head, reason)


def gate_skip_lines(
    store: RunStore, item_ids: Iterable[str], *, statuses: Mapping[str, str]
) -> tuple[str, ...]:
    """One ``kew epic status`` line per item whose last skipped head the gate
    did not check, naming that head and why.

    An item *statuses* reads as done or cancelled gets none: its PR merged or
    closed, so the skip on its last head is history, not something to act on.
    """
    lines = []
    for item_id in sorted(item_ids):
        if statuses.get(item_id) in _FINISHED_STATUSES:
            continue
        skip = gate_skip(store, item_id)
        if skip is not None:
            head, why = skip
            lines.append(
                f"  pre-review gate skipped: {item_ref(item_id)} at {head[:12]}: {why}"
            )
    return tuple(lines)


def _skip_record(store: RunStore, item_id: str) -> dict[str, object]:
    raw = store.get_meta(_SKIPPED_META_PREFIX + item_id)
    try:
        record = json.loads(raw) if raw else {}
    except ValueError as exc:
        _logger.warning(
            "pre-review gate skip record for %s is unreadable, so whether the "
            "gate skipped its head is unknown: %s",
            item_ref(item_id),
            exc,
        )
        return {}
    return record if isinstance(record, dict) else {}


def _skip_entries(store: RunStore, item_id: str, head: str) -> dict[str, str]:
    record = _skip_record(store, item_id)
    skips = record.get("skips")
    if record.get("head") != head or not isinstance(skips, dict):
        return {}
    return {str(half): str(why) for half, why in skips.items() if why}


def gate_rounds(store: RunStore, item_id: str) -> int:
    """Rounds this step recorded in *item_id*'s current review cycle."""
    return sum(
        1
        for round_ in store.review_rounds_in_cycle(item_id)
        if round_.review_mode == REVIEW_MODE_PRE_REVIEW_GATE
    )


def first_failing_line(output: str) -> str:
    """The first line a failed command printed, for a hold reason."""
    for raw in _EXITED.sub("", output, count=1).splitlines():
        line = _STREAM_LABEL.sub("", raw).strip()
        if line:
            return line
    return ""


def run_pre_review_gate(
    config: DispatchConfig,
    *,
    repo_root: Path,
    base: str,
    head: str,
    settled: Collection[str] = (),
    pr_body: str | None = None,
    store: RunStore | None = None,
) -> GateOutcome:
    """Run the configured gates on *head* that have not settled on it yet.

    *pr_body* is the PR description whose **Behavior changes** section the
    held-out half judges failures against; *store* is where main's health is
    read, so a test red on main is never the head's.
    """
    results: list[_Half] = []
    halves = _runnable_halves(config, settled, results)
    contained = halves - {HELD_OUT_HALF}
    admission = _admission(config, contained, results)
    if admission is None:
        contained = frozenset()
    if not contained and HELD_OUT_HALF not in halves:
        return _combine(results)
    with ExitStack() as stack:
        dirs = _Dirs(stack)
        prepared = _prepare(
            dirs.new(),
            repo_root=repo_root,
            base=base,
            head=head,
            with_ui=UI_HALF in contained,
            with_merge=HELD_OUT_HALF in halves,
        )
        if admission is not None:
            stale = partial(_check_image_inputs_changed, config, prepared)
            runner = _Runner(prepared, dirs, admission, stale_image_inputs=stale)
            results += _contained_halves(config, runner, contained, base)
        if HELD_OUT_HALF in halves:
            main_red = partial(red_on_main, store, repo_root, base)
            results.append(_HeldOutRun(config, prepared, base, pr_body).half(main_red))
    return _combine(results)


def _runnable_halves(
    config: DispatchConfig, settled: Collection[str], results: list[_Half]
) -> frozenset[str]:
    """The configured halves still due on the head that may run at all.

    The held-out half never runs on the ``local`` host, whatever else is
    configured (see the module doc): it is skipped into *results* instead.
    """
    halves = configured_halves(config) - set(settled)
    if HELD_OUT_HALF in halves and config.verify.host == "local":
        detail = f"{HELD_OUT_GATE}: {HELD_OUT_CHECK_LOCAL_REFUSAL}"
        results.append(_Half(HELD_OUT_HALF, "skipped", detail=detail))
        return halves - {HELD_OUT_HALF}
    return halves


def _admission(
    config: DispatchConfig, contained: Collection[str], results: list[_Half]
) -> HostVerificationAdmission | None:
    """The container the contained halves run in, or None when there are none
    to run or no container to run them in (skipped into *results*)."""
    if not contained:
        return None
    admission = resolve_check_image_admission(
        config.sandbox, runner=config.agent.runner or DEFAULT_IMAGE_RUNNER
    )
    if admission.posture == CONTAINED_VERIFICATION_POSTURE:
        return admission
    # Never on the host, whatever the operator's containment opt-out says:
    # the static command executes the candidate's own plugins.
    refusal = admission.refusal or (
        f"no container to run the gates in (posture {admission.posture})"
    )
    results.append(_Half("contained", "skipped", detail=refusal, settles=False))
    return None


def _check_image_inputs_changed(
    config: DispatchConfig, prepared: _Prepared
) -> tuple[str, ...]:
    """The paths the head changes that ``[sandbox] check_image`` was built
    from (see the module doc); none without a check image, and none when the
    diff cannot be taken, so the base decides as for any failure.

    Only paths ``[epic] merge_gate_paths`` also names count: those are the
    heads the merge queue holds for a person, which the skip reason says.
    """
    if not config.sandbox.check_image.strip():
        return ()
    diff = run(
        [
            "git",
            "-C",
            str(prepared.clone),
            "diff",
            "--name-only",
            f"{prepared.base_sha}...{prepared.head}",
        ],
        env=checkout_object_env(),
        check=False,
    )
    if diff.returncode != 0:
        _logger.info(
            "pre-review gate: no check image input diff (%s); the base decides",
            (diff.stderr or "").strip(),
        )
        return ()
    gated = touched_gate_paths(diff.stdout.split(), config.epic.merge_gate_paths)
    inputs = config.sandbox.check_image_inputs or _DEFAULT_CHECK_IMAGE_INPUTS
    return touched_gate_paths(gated, inputs)


def _contained_halves(
    config: DispatchConfig, runner: _Runner, halves: Collection[str], base: str
) -> list[_Half]:
    """The static and ui-check halves, each in containers of its own."""
    results: list[_Half] = []
    if STATIC_HALF in halves:
        results.append(runner.static(config.commands.static.strip()))
    if UI_HALF in halves:
        results.append(runner.ui_check(config.commands.ui_check.strip(), base))
    return results


#: Why the held-out half sits out a change whose selection is not a set of
#: test files.
FULL_SELECTION_REASON = "the change selects the full suite; the merge verify runs it"
NO_TEST_FILE_REASON = "the change edits no base test file; there is nothing to run"


class _Unanswered(Exception):
    """A held-out run the host gave no answer to: a skip, never a finding."""

    def __init__(self, half: _Half) -> None:
        super().__init__(half.detail)
        self.half = half


class _HeldOutRun:
    """The merge verify's held-out phase on the merge candidate (#3212).

    Only a base test that failed, that no **Behavior changes** line declares,
    and that is not main's is a failure of the head. A run that broke, or
    never reported, proves nothing about it and is skipped, as is a head that
    does not merge with the base (the ui-check half names the conflict). A
    verify host that was busy or could not be reached is skipped without
    settling, so a deferred review tries again; a run that outran its ceiling
    is skipped and settled, since it would outrun it again.

    A head that edits a gate definition (``[epic] merge_gate_paths``: the lock,
    a ``conftest.py``, the pytest configuration) is skipped too. The merge
    verify runs such a head only after a person released its merge-gate hold,
    and this run, which installs the head's locked dependencies and imports its
    code on the verify host, must not run it before that.
    """

    def __init__(
        self,
        config: DispatchConfig,
        prepared: _Prepared,
        base: str,
        pr_body: str | None,
    ) -> None:
        self._config = config
        self._prepared = prepared
        self._base = base
        self._pr_body = pr_body

    def half(self, main_red: Callable[[], tuple[str, ...]]) -> _Half:
        """The half's result; *main_red* lists the tests main's health has red."""
        refused = self._refusal()
        if refused is not None:
            return refused
        prepared = self._prepared
        assert prepared.candidate is not None  # _refusal checked it
        candidate = held_out_check_candidate(
            prepared.clone, ref=prepared.candidate, base_sha=prepared.base_sha
        )
        if candidate.held_out.mode != SELECTED:
            reason = (
                FULL_SELECTION_REASON
                if candidate.held_out.mode == FULL
                else NO_TEST_FILE_REASON
            )
            return _Half(HELD_OUT_HALF, "skipped", detail=f"{HELD_OUT_GATE}: {reason}")
        try:
            report = self._run(candidate, self._pr_body)
            if not report.undeclared:
                return self._clean(report)
            return self._judged(report, main_red())
        except _Unanswered as unanswered:
            return unanswered.half

    def _refusal(self) -> _Half | None:
        """Why the half does not run on this head at all, if it does not."""
        prepared = self._prepared
        if prepared.merge_error is not None:
            detail = f"{HELD_OUT_GATE}: {prepared.merge_error}"
            return _Half(HELD_OUT_HALF, "skipped", detail=detail, settles=False)
        if prepared.candidate is None:
            detail = f"{HELD_OUT_GATE}: the head does not merge with {self._base}"
            return _Half(HELD_OUT_HALF, "skipped", detail=detail)
        changed = _git(
            prepared.clone,
            "diff",
            "--name-only",
            f"{prepared.base_sha}...{prepared.head}",
        ).split()
        gated = touched_gate_paths(changed, self._config.epic.merge_gate_paths)
        if not gated:
            return None
        return _Half(
            HELD_OUT_HALF,
            "skipped",
            detail=f"{HELD_OUT_GATE}: the head edits gate definitions "
            f"({', '.join(gated[:_MAX_CITED_PATHS])}), which wait for a person",
        )

    def _run(self, candidate: VerifyCandidate, pr_body: str | None) -> HeldOutReport:
        """One run on the verify host; :class:`_Unanswered` when it gave none."""
        ceiling = self._config.verify.held_out_before_review_timeout_seconds
        try:
            report = run_held_out_check(
                self._config,
                repo_root=self._prepared.clone,
                candidate=candidate,
                pr_body=pr_body,
            )
        except HostTimeout as exc:
            detail = f"{HELD_OUT_GATE}: ran past its {ceiling}s ceiling: {exc}"
            raise _Unanswered(_Half(HELD_OUT_HALF, "skipped", detail=detail)) from exc
        except (VerifyRefused, HostError) as exc:
            detail = f"{HELD_OUT_GATE}: {exc}"
            half = _Half(HELD_OUT_HALF, "skipped", detail=detail, settles=False)
            raise _Unanswered(half) from exc
        if report.outcome == "not_run":
            detail = f"{HELD_OUT_GATE}: {report.error}"
            half = _Half(HELD_OUT_HALF, "skipped", detail=detail, settles=False)
            raise _Unanswered(half)
        return report

    @staticmethod
    def _clean(report: HeldOutReport) -> _Half:
        ran = (HELD_OUT_GATE,)
        if report.outcome == "failed":
            detail = f"{HELD_OUT_GATE}: {report.error}"
            return _Half(HELD_OUT_HALF, "skipped", detail=detail, ran=ran)
        return _Half(HELD_OUT_HALF, "passed", ran=ran)

    def _judged(self, report: HeldOutReport, main_red: tuple[str, ...]) -> _Half:
        """The undeclared failures that are the head's: not red on main by
        its health, and passing on the base's own ``src`` (the control run)."""
        rest = [node for node in report.undeclared if node not in main_red]
        on_base = self._red_on_base(rest)
        mains = tuple(
            node for node in report.undeclared if node in main_red or node in on_base
        )
        heads = tuple(node for node in report.undeclared if node not in mains)
        ran = (HELD_OUT_GATE,)
        detail = (
            f"{HELD_OUT_GATE}: red on main too: not the agent's: {', '.join(mains)}"
            if mains
            else ""
        )
        if not heads:
            return _Half(HELD_OUT_HALF, "skipped", detail=detail, ran=ran)
        head_report = replace(report, undeclared=heads)
        findings = _held_out_findings(
            head_report, self._prepared, self._base, self._pr_body
        )
        asked = _to_declare(head_report)[0]
        return _Half(
            HELD_OUT_HALF, "failed", findings, detail=detail, ran=ran, held_out=asked
        )

    def _red_on_base(self, nodes: Sequence[str]) -> tuple[str, ...]:
        """The *nodes* that fail on the base's own ``src`` too: the control run,
        on the same host, of just those tests.

        The run names each node's test *function*, never the case: a case the
        head's ``src`` parametrizes into being does not exist on the base, and
        pytest given a node id it cannot find runs nothing at all. A case the
        base does not collect is the head's by construction, so it stays a
        finding. A control run that reached no verdict — a run that broke, a
        report that does not count, runs that collected nothing — proves
        nothing about any node: :class:`_Unanswered`, never "the base passed".
        """
        if not nodes:
            return ()
        prepared = self._prepared
        functions = tuple(dict.fromkeys(_test_function(node) for node in nodes))
        control = held_out_check_candidate(
            prepared.clone,
            ref=prepared.base_sha,
            base_sha=prepared.base_sha,
            only=functions,
        )
        report = self._run(control, None)
        refusal = _verdictless_control(report)
        if refusal is not None:
            detail = (
                f"{HELD_OUT_GATE}: the control run on the base "
                f"{prepared.base_sha[:12]} reached no verdict ({refusal}), so "
                "whose failures these are is unknown"
            )
            half = _Half(HELD_OUT_HALF, "skipped", detail=detail, settles=False)
            raise _Unanswered(half)
        return tuple(node for node in nodes if _failed_in(node, report.failed))


def _test_function(node: str) -> str:
    """*node* without its parametrization: ``f.py::T::t[x]`` is ``f.py::T::t``."""
    test, bracket, _ = node.partition("[")
    return test if bracket and "::" in test and node.endswith("]") else node


def _failed_in(node: str, failed: Sequence[str]) -> bool:
    """Whether *failed* names *node*, or a collector it sits in (its file or
    class failed to collect on the base)."""
    return any(node == item or node.startswith(f"{item}::") for item in failed)


def _verdictless_control(report: HeldOutReport) -> str | None:
    """Why the control run's report is no verdict on the tests it was given,
    or ``None`` when it is one. Its only acceptable error is its own verdict
    that tests failed: with no PR body, every failure is undeclared."""
    if report.verdictless:
        return report.verdictless
    if report.exits and all(code == _NOTHING_COLLECTED for code in report.exits):
        return "no run collected a test"
    if report.outcome == "failed" and not report.undeclared:
        return report.error or "it failed without naming a test"
    return None


#: pytest's exit status for a run that collected nothing.
_NOTHING_COLLECTED = 5


#: What a held-out finding tells the remediation to do: the merge's own rule.
DECLARATION_RULE = (
    "Each base test above fails against this head merged with the base, and "
    "the merge verify fails on any such failure the PR body's **Behavior "
    "changes** section does not name. If the failure is a defect, fix the "
    "source so the base test passes; never edit the test to match. If the "
    "behavior change is intended, declare it: write one line per test in your "
    "Dispatch Report's **Behavior changes** section, `- <node id> — <why the "
    "old expectation no longer holds>`, the node id relative to the project "
    "directory as pytest prints it. A test file that failed to collect takes "
    "one line for the file, `- <file path> — <why>`: it declares the tests of "
    "that file that never ran, never one that ran and failed."
)


def _to_declare(report: HeldOutReport) -> tuple[tuple[str, ...], dict[str, int]]:
    """The ids to ask a line for, and each collapsed file's uncollected count.

    A test file whose collector failed is asked for once, for the file: one
    line for it declares every test of it the run never collected. A test of
    it that was collected and failed is still asked for by name, and an
    acceptance contract file is never collapsed — no line declares it.
    """
    broken = {
        node
        for node in report.undeclared
        if "::" not in node and not _in_contracts(node, report.contracts)
    }
    uncollected = set(report.uncollected)
    counts: dict[str, int] = dict.fromkeys(sorted(broken), 0)
    asked: list[str] = []
    for node in report.undeclared:
        path = node.split("::", 1)[0]
        if node in uncollected and node != path and path in broken:
            counts[path] += 1
        else:
            asked.append(node)
    return tuple(asked), {path: n for path, n in counts.items() if n}


def _in_contracts(node: str, contracts: Sequence[str]) -> bool:
    return any(node == c or node.startswith(f"{c}::") for c in contracts)


def _undeclared_lines(report: HeldOutReport) -> str:
    """The undeclared failures as the finding lists them, a file that failed
    to collect named once with how many of its tests never ran."""
    asked, counts = _to_declare(report)
    return "\n".join(
        f"- {node} failed to collect; {counts[node]} of its tests never ran; one "
        "line for the file declares them"
        if node in counts
        else f"- {node}"
        for node in asked
    )


def _held_out_findings(
    report: HeldOutReport, prepared: _Prepared, base: str, pr_body: str | None
) -> tuple[GateFinding, ...]:
    """One finding per base test file with an undeclared failure."""
    ids = _undeclared_lines(report)
    declared = "\n".join(behavior_change_lines(pr_body)).strip() or "(none)"
    output = (
        f"Undeclared failures:\n{ids}\n\n"
        f"The PR body's Behavior changes section now:\n{declared}\n\n"
        f"{DECLARATION_RULE}"
    )
    summary = (
        f"{HELD_OUT_GATE} ({base} at {prepared.base_sha[:12]} merged into head "
        f"{prepared.head[:12]}): {len(report.undeclared)} base test(s) fail and "
        f"no Behavior changes line declares them.\n{output}"
    )
    files = sorted({node.split("::", 1)[0] for node in report.undeclared})
    locations = [
        f"{HELD_OUT_PROJECT}/{path}:1"
        for path in files
        if f"{HELD_OUT_PROJECT}/{path}" in prepared.candidate_files
    ] or [f"{PRE_REVIEW_GATE_LOCATION}:{HELD_OUT_GATE}"]
    return tuple(
        GateFinding(
            location=location,
            summary=summary,
            command="kew verify's held-out phase",
            output=output,
            check=HELD_OUT_GATE,
        )
        for location in locations[:_MAX_CITED_PATHS]
    )


class _Dirs:
    """Claimed throwaway directories, each removed when the step ends."""

    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack

    def new(self) -> Path:
        directory = work_dirs.make_work_dir("kew-pre-review-")
        claim_checkout(directory)
        self._stack.callback(_discard, directory)
        return directory


def _discard(directory: Path) -> None:
    try:
        shutil.rmtree(directory, ignore_errors=True)
    finally:
        release_checkout_claim(directory)


def _git(checkout: Path, *args: str, check: bool = True) -> str:
    """Host git in a clone kew built, with no checkout-supplied program run."""
    return run(
        ["git", "-C", str(checkout), *args], env=checkout_object_env(), check=check
    ).stdout


def _tree_files(checkout: Path, commit: str) -> frozenset[str]:
    return frozenset(_git(checkout, "ls-tree", "-r", "--name-only", commit).split())


def _prepare(
    clone: Path,
    *,
    repo_root: Path,
    base: str,
    head: str,
    with_ui: bool,
    with_merge: bool = False,
) -> _Prepared:
    """Fetch, diff, select modules and build the candidate: all host git.

    The candidate is built when a ui-check module is touched, or always with
    *with_merge* (the held-out half runs on it).

    *clone* is never mounted into a container, so everything read here is what
    git wrote, not what candidate code wrote.
    """
    base_sha = resolve_base_commit(repo_root, base)
    clone_local_checkout(repo_root, clone, (base_sha, head))
    _git(clone, "checkout", "-q", "--detach", head)
    _git(clone, "update-ref", _HEAD_REF, head)
    _git(clone, "update-ref", _BASE_REF, base_sha)
    prepared = _Prepared(
        clone=clone,
        base_sha=base_sha,
        head=head,
        head_files=_tree_files(clone, head),
    )
    if not with_ui:
        return _with_candidate(prepared) if with_merge else prepared
    changed = _git(clone, "diff", "--name-only", f"{base_sha}...{head}").split()
    present = _git(
        clone, "diff", "--name-only", "--diff-filter=d", f"{base_sha}...{head}"
    ).split()
    sources = _module_sources(clone, head)
    modules = tuple(touched_modules(changed, sources=sources))
    prepared = replace(
        prepared,
        modules=modules,
        control_modules=_control_modules(clone, base_sha, modules),
        uncovered=tuple(uncovered_surfaces(present, sources=sources)),
    )
    if not modules and not with_merge:
        return prepared
    return _with_candidate(prepared)


def _control_modules(
    clone: Path, base_sha: str, modules: tuple[str, ...]
) -> tuple[str, ...]:
    """The modules the base control run uses: those of *modules* the base has,
    or, when the head adds every one of them, the first module the base has —
    enough to show whether the image can run a browser check at all."""
    on_base = sorted(_module_blobs(clone, base_sha))
    shared = tuple(name for name in modules if name in on_base)
    return shared or tuple(on_base[:1])


def _module_blobs(clone: Path, commit: str) -> dict[str, str]:
    """The ui-check modules *commit* holds, by name, as regular-file blobs.

    Read from git objects, never through the checkout's filesystem: a head can
    commit the modules directory, or one module, as a symlink to a host path,
    and every filesystem read would follow it. A directory that is not a tree,
    a symlink (mode ``120000``), a submodule and a non-``.mjs`` entry are all
    skipped.
    """
    kind = _git(clone, "cat-file", "-t", f"{commit}:{UI_CHECKS_DIR}", check=False)
    if kind.strip() != "tree":
        return {}
    listed = _git(clone, "ls-tree", "-z", f"{commit}:{UI_CHECKS_DIR}")
    blobs: dict[str, str] = {}
    for entry in filter(None, listed.split("\0")):
        meta, _, name = entry.partition("\t")
        mode, kind, oid = meta.split()
        if mode in _REGULAR_MODES and kind == "blob" and name.endswith(".mjs"):
            blobs[name.removesuffix(".mjs")] = oid
    return blobs


def _module_sources(clone: Path, commit: str) -> dict[str, str]:
    """The text of each module :func:`_module_blobs` names, up to
    :data:`_MAX_MODULE_BYTES` each: a larger one, or one that is not text, is
    left out."""
    sources: dict[str, str] = {}
    for name, oid in _module_blobs(clone, commit).items():
        if int(_git(clone, "cat-file", "-s", oid)) > _MAX_MODULE_BYTES:
            continue
        try:
            sources[name] = _git(clone, "cat-file", "blob", oid)
        except UnicodeDecodeError:
            continue
    return sources


def _with_candidate(prepared: _Prepared) -> _Prepared:
    """Merge the base into the head in the preparation clone, uncommitted to
    any branch: the merge commit lives in this discarded clone only."""
    clone = prepared.clone
    merge = run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=kew",
            "-c",
            "user.email=kew@localhost",
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            "kew pre-review merge candidate",
            prepared.base_sha,
        ],
        env=checkout_object_env(),
        check=False,
    )
    conflicted = _git(clone, "diff", "--name-only", "--diff-filter=U").split()
    if conflicted:
        return replace(prepared, conflicted=tuple(conflicted))
    if merge.returncode != 0:
        # Not a conflict, so nothing the head can be told to fix: the halves
        # that need the candidate sit this try out, and the static half, which
        # runs on the head alone, still runs.
        detail = (merge.stderr or merge.stdout or "").strip()
        return replace(
            prepared, merge_error=f"the merge with the base did not run: {detail}"
        )
    candidate = _git(clone, "rev-parse", "HEAD").strip()
    _git(clone, "update-ref", _CANDIDATE_REF, candidate)
    return replace(
        prepared, candidate=candidate, candidate_files=_tree_files(clone, candidate)
    )


def _clone_at(prepared: _Prepared, dirs: _Dirs, commit: str) -> Path:
    """A clone of one commit for one container, made from the preparation
    clone; host git finishes with it before the container starts."""
    target = dirs.new()
    run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-hardlinks",
            "--no-tags",
            "--no-checkout",
            str(prepared.clone),
            str(target),
        ],
        # The read environment refuses every transport; a local clone is one.
        env={**checkout_object_env(), "GIT_ALLOW_PROTOCOL": "file"},
    )
    _git(target, "checkout", "-q", "--detach", commit)
    return target


class _Runner:
    """Runs one command per container, each in a clone of its own."""

    def __init__(
        self,
        prepared: _Prepared,
        dirs: _Dirs,
        admission: HostVerificationAdmission,
        *,
        stale_image_inputs: Callable[[], tuple[str, ...]] = tuple,
    ) -> None:
        self._prepared = prepared
        self._dirs = dirs
        self._admission = admission
        # Taken only once a check fails, and at most once for both halves.
        self._stale_image_inputs = cache(stale_image_inputs)

    def _run(self, command: str, commit: str) -> ConditionResult:
        # The clone is writable: ruff and mypy write their caches beside the
        # code, and the clone is discarded unread after this one command.
        checkout = _clone_at(self._prepared, self._dirs, commit)
        return CommandCondition(
            command, timeout=GATE_TIMEOUT_SECONDS, needs_writes=True
        ).check_admitted_directory(checkout, self._admission)

    def _settle(
        self, gate: str, result: ConditionResult, control: Callable[[], str | None]
    ) -> tuple[HalfKind, str]:
        """Is *result* a failure of the head? The base decides (see module doc).

        *control* runs the base's side and returns None when the base passed,
        else why it did not.
        """
        # An output budget overflow is the command failing loudly (thousands
        # of type errors), not a launch problem: the base decides, as for any
        # failure. Skipping it would let a head buy a paid review by failing
        # hard enough.
        if (result.timed_out or result.unrunnable) and not result.output_overflowed:
            return "skipped", f"{gate}: {result.evidence}"
        if result.met:
            return "passed", ""
        if result.exit_code in _NOT_RUNNABLE_EXITS:
            return "skipped", (
                f"{gate}: the image cannot run the command (exit "
                f"{result.exit_code}): {result.evidence}"
            )
        stale = self._stale_image_inputs()
        if stale:
            named = ", ".join(stale[:_MAX_CITED_PATHS])
            return "skipped", (
                f"{gate}: {STALE_CHECK_IMAGE_REASON} ({named}), which it was "
                "built without; the head is held for a person at merge and "
                "the merge verify runs the gate"
            )
        refused = control()
        if refused is None:
            return "failed", ""
        return "skipped", (
            f"{gate} fail on the base {self._prepared.base_sha[:12]} as well "
            f"(a red base, or an image without what the command needs): {refused}"
        )

    def _base_passes(self, command: str) -> Callable[[], str | None]:
        def control() -> str | None:
            base = self._run(command, self._prepared.base_sha)
            return None if base.met else base.evidence

        return control

    def static(self, command: str) -> _Half:
        prepared = self._prepared
        result = self._run(command, prepared.head)
        kind, detail = self._settle(STATIC_GATE, result, self._base_passes(command))
        if kind != "failed":
            return _Half(STATIC_HALF, kind, detail=detail, ran=(STATIC_GATE,))
        findings = _findings(STATIC_GATE, command, result.evidence, prepared.head_files)
        return _Half(STATIC_HALF, "failed", tuple(findings), ran=(STATIC_GATE,))

    def ui_check(self, command: str, base: str) -> _Half:
        prepared = self._prepared
        uncovered = prepared.uncovered
        if prepared.conflicted:
            return _Half(
                UI_HALF,
                "failed",
                _conflict_findings(prepared, base),
                ran=(f"merge with {base}",),
                uncovered=uncovered,
            )
        if prepared.merge_error is not None:
            detail = f"{UI_CHECK_GATE}: {prepared.merge_error}"
            return _Half(UI_HALF, "skipped", detail=detail, settles=False)
        if prepared.candidate is None or not prepared.modules:
            return _Half(
                UI_HALF, "uncovered" if uncovered else "passed", uncovered=uncovered
            )
        executed = command.replace("{modules}", shlex.join(prepared.modules))
        result = self._run(executed, prepared.candidate)
        kind, detail = self._settle(UI_CHECK_GATE, result, self._ui_control(command))
        if kind == "passed" and uncovered:
            kind = "uncovered"
        if kind != "failed":
            return _Half(
                UI_HALF, kind, detail=detail, ran=(UI_CHECK_GATE,), uncovered=uncovered
            )
        return _Half(
            UI_HALF,
            "failed",
            _ui_findings(prepared, base, executed, result.evidence),
            ran=(UI_CHECK_GATE,),
            uncovered=uncovered,
        )

    def _ui_control(self, command: str) -> Callable[[], str | None]:
        modules = self._prepared.control_modules
        if not modules:
            return lambda: "the base has no ui-check module to run the command on"
        return self._base_passes(command.replace("{modules}", shlex.join(modules)))


def _conflict_findings(prepared: _Prepared, base: str) -> tuple[GateFinding, ...]:
    files = ", ".join(prepared.conflicted)
    summary = (
        f"head {prepared.head[:12]} does not merge with {base} "
        f"({prepared.base_sha[:12]}): conflicted files: {files}"
    )
    return tuple(
        GateFinding(
            location=f"{path}:1",
            summary=summary,
            command=f"git merge origin/{base}",
            output=files,
            check=MERGE_GATE,
        )
        for path in prepared.conflicted[:_MAX_CITED_PATHS]
    )


def _ui_findings(
    prepared: _Prepared, base: str, executed: str, evidence: str
) -> tuple[GateFinding, ...]:
    assert prepared.candidate is not None  # a ui-check ran on it
    module_paths = [f"{UI_CHECKS_DIR}/{name}.mjs:1" for name in prepared.modules]
    summary = (
        f"{UI_CHECK_GATE} ({base} at {prepared.base_sha[:12]} merged into head "
        f"{prepared.head[:12]}) fail `{executed}`: {evidence}"
    )
    return tuple(
        GateFinding(
            location=location,
            summary=summary,
            command=executed,
            output=evidence,
            check=UI_CHECK_GATE,
        )
        for location in _cited_paths(evidence, prepared.candidate_files) or module_paths
    )


def _combine(halves: list[_Half]) -> GateOutcome:
    """One outcome for the step: failed over skipped over uncovered over passed."""
    return GateOutcome(
        _kind(halves),
        tuple(chain.from_iterable(half.findings for half in halves)),
        detail="; ".join(filter(None, (half.detail for half in halves))),
        ran=tuple(chain.from_iterable(half.ran for half in halves)),
        settled=_settled(halves),
        uncovered=tuple(chain.from_iterable(half.uncovered for half in halves)),
        held_out=tuple(chain.from_iterable(half.held_out for half in halves)),
        skipped=tuple((h.name, h.detail) for h in halves if h.kind == "skipped"),
    )


def _kind(halves: list[_Half]) -> HalfKind:
    kinds = {half.kind for half in halves}
    return next((k for k in _PRECEDENCE if k in kinds), "passed")


def _settled(halves: list[_Half]) -> tuple[str, ...]:
    # A half that left a changed surface uncovered never stands for the head.
    return tuple(
        half.name
        for half in halves
        if half.kind in _SETTLES and half.settles and not half.uncovered
    )


#: The half kinds whose result stands for the head they ran on.
_SETTLES = frozenset({"passed", "skipped"})


def _findings(
    gate: str, command: str, evidence: str, files: frozenset[str]
) -> list[GateFinding]:
    """One finding per file the output cites, or one naming the gate."""
    summary = f"the merge gate's {gate} fail on this head: `{command}`: {evidence}"
    locations = _cited_paths(evidence, files) or [f"{PRE_REVIEW_GATE_LOCATION}:{gate}"]
    return [
        GateFinding(
            location=location,
            summary=summary,
            command=command,
            output=evidence,
            check=gate,
        )
        for location in locations
    ]


def _cited_paths(output: str, files: frozenset[str]) -> list[str]:
    """``path:line`` locations the output names that are files in the commit.

    *files* is the commit's tree as host git listed it before the container
    ran. A gate run from a subdirectory (``cd kew && ruff check``) prints paths
    relative to it, so a path not found at the root is looked for one level
    down. Repository-rooted, de-duplicated, in first-cited order.
    """
    tops = sorted({path.split("/", 1)[0] for path in files if "/" in path})
    seen: list[str] = []
    for match in _CITED.finditer(output):
        path = _in_tree(match.group(1), files, tops)
        location = f"{path}:{match.group(2)}" if path else None
        if location and location not in seen:
            seen.append(location)
        if len(seen) >= _MAX_CITED_PATHS:
            break
    return seen


def _in_tree(raw: str, files: frozenset[str], tops: list[str]) -> str | None:
    rel = raw.removeprefix("./")
    if rel.startswith("/") or ".." in rel.split("/"):
        return None
    if rel in files:
        return rel
    return next((f"{top}/{rel}" for top in tops if f"{top}/{rel}" in files), None)
