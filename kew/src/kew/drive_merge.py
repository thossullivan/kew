"""The operator-signed standing merge rule, and ``kew drive``'s merge executor (#2618).

``[drive.merge]`` names what the drive may merge without a person: a READY PR
whose NET paths (the diff against the merge base, so what main changed since
the PR forked is not read as the PR's) avoid every gate class
(:data:`GATE_CLASSES`) and every protected publication class
(:func:`kew.publication_filter.protected_path_reason`), and that is not a UI
change unless a browser check of its own head is on record. Everything else is
left where it already is — a node awaiting a merge, on the needs-you set — and
the drive records why it declined (``drive_merge_declined``, once per head).

**The signature.** The rule names its signer, the operator's GitHub login. The
drive fingerprints the rule as it reads it and compares that with the last
``drive_merge_rule_signed`` event on the audit chain. A new or changed rule is
signed — recorded on the chain under the login ``gh`` runs as — only when that
login is the signer of record (the rule's own ``signer`` for a first rule, the
previous rule's signer for an edit, so a signer can hand over but nobody else
can take over). A rule changed under any other login is refused, and nothing
merges until the signer runs the drive or the rule is put back.

**The merge.** One at a time, and never two verifications at once: each runs
under the repo-wide merge-queue lock (:func:`merge_queue_lock`) that ``kew epic
merge`` and the driver's own queue take, held until the merge has landed. Every
fact :func:`kew.graph.driver.operator_merge.operator_merge` proves still has to
hold — membership, no hold, live work, the READY verdict bound to the head, the
open PR at that head, both CI gates, and the in-lease re-proof of all of them.
Its verifier here is :class:`_CombinedVerify`: fetch main and the PR head, merge
main into the head in a fresh worktree, classify the net paths, run ``kew
verify`` (on ``[verify] host``) over the merged commit, and re-read main. A main
that moved while the verification ran refuses, and the next round retries. The
merge is a squash carrying the PR's title and body, bound to the verified head.
The acceptance contract id and version must also match immediately before the
merge. An amendment during verification requires another verification.
Under ``[drive.merge] gate = "verify"`` (#2814) both CI gates are skipped: that
passing ``kew verify`` of the candidate is the evidence the tests ran.

**The mutation report** (#2817) is off the merge's critical path: the
gating ``kew verify`` reports ``mutation=deferred``, and each merge queues a
report job for the commit it verified, against the main it was verified on.
The queue is the store's, not the process's: the merge records
:data:`REPORT_QUEUED_EVENT`, and a report is owed until a
:data:`~kew.merge_verify.MUTATION_EVENT` or :data:`REPORT_DROPPED_EVENT` for
its commit says otherwise (:func:`pending_reports`), so a drive that restarts
between a merge and its report still runs it. The queue runs at the end of a
pass — after every merge the pass attempted — and only while it holds the
merge-queue lock: it yields, staying queued, while a gating verification holds
that lock or another job holds the host, and while a report runs, a gating
verification that arrives finds the queue busy — a wait it retries next round —
never a host it is refused as an ``incomplete`` verify
(:meth:`ReportQueue.run_owed`). Its survivors are recorded and posted to
the PR thread.

**A verify host that cannot be reached** (an expired cloud login, an instance
gone) pauses merges and nothing else: each merge pass probes ``[verify] host``
first (:func:`probe_verify_host`), and while the probe fails the pass merges
nothing — the ticks before it still dispatch and review. The first failed probe
of an outage sends one notification naming the fix; the first probe that
answers again resumes merges, with no operator action. The suite runs on the
operator machine instead only when ``[drive.merge] local_fallback`` says so.

TRUST BOUNDARY: the rule is operator config; its authority is the signer's
login, read from the forge, never a label or anything an agent can write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kew.audit import Actor, emit
from kew.config import DispatchConfig, DriveMergeConfig, EpicConfig
from kew.graph.driver.base_integration import DIRTY_MERGE_STATE
from kew.graph.driver.excluded_conflict import (
    excluded_by_rule,
    excluded_epics,
    hold_conflict,
    release_conflict,
)
from kew.graph.driver.facts import GitHubFactsReader, RunFn
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BASE_MOVED,
    MERGE_VERIFY_BASE_UNMERGEABLE,
    MERGE_VERIFY_BASE_UNREADABLE,
    MERGE_VERIFY_FAILED,
    MERGE_VERIFY_HEAD_MOVED,
    MERGE_VERIFY_LEASE_BUSY,
    MERGE_VERIFY_UNRUNNABLE,
    UNCHECKED_BROWSER,
    UNCHECKED_FULL_SUITE,
    MergeVerifyResult,
    acceptance_contract_versions,
    browser_checked,
    merge_queue_lock,
    merge_queue_lock_path,
    review_unchecked_kinds,
    touched_gate_paths,
)
from kew.graph.driver.operator_merge import OperatorMergeRefused, operator_merge
from kew.hosts import HostError, provider_named
from kew.ids import GITHUB_ID_RE
from kew.merge_verify import (
    MUTATION_EVENT,
    VerifyLeaseRefused,
    VerifyRefused,
    mutation_comment,
    run_mutation_report,
    run_verify,
    verify_answers_browser,
)
from kew.notifier import NotificationEvent, build_notifier
from kew.outcomes import ForgeIdentity, observe_forge_identity
from kew.publication_filter import protected_path_reason
from kew.run_store import RunStore
from kew.run_store_acceptance import AcceptanceContractRow
from kew.subprocess_utils import run as _default_run

_logger = logging.getLogger(__name__)

RULE_SIGNED_EVENT = "drive_merge_rule_signed"
HOST_UNREACHABLE_EVENT = "drive_verify_host_unreachable"
DECLINED_EVENT = "drive_merge_declined"
#: A merge whose mutation report is owed: the durable queue entry.
REPORT_QUEUED_EVENT = "mutation_report_queued"
#: A queued report that could not run and never will (a refusal repeats).
REPORT_DROPPED_EVENT = "mutation_report_dropped"
MERGE_TRIGGER = "drive"

#: Bumped whenever :data:`GATE_CLASSES` changes, so a signed rule never covers
#: a gate list its signer did not read.
RULE_VERSION = 2

#: What the drive never merges on its own: what the gates ARE, and what builds,
#: provisions or configures the machines they run on.
GATE_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflows", (".github/workflows/", ".github/actions/")),
    ("scripts", ("scripts/", "**/scripts/**")),
    ("docker", ("docker/", "**/docker/**", "**/Dockerfile*", "**/*.dockerfile")),
    ("docker", ("**/docker-compose*", "**/compose.y*ml", "**/.dockerignore")),
    ("provisioning", ("**/provision/**", "**/provisioning*")),
    ("lockfiles", ("**/uv.lock", "**/*.lock", "**/package-lock.json")),
    ("lockfiles", ("**/pnpm-lock.yaml", "**/npm-shrinkwrap.json")),
    ("pyproject", ("**/pyproject.toml",)),
    ("conftest", ("**/conftest.py",)),
    ("kew.toml", ("**/kew.toml",)),
    ("frontend/scripts", ("frontend/scripts/",)),
    # Everything the merge queue holds as a gate definition by default — the
    # scanner ignore lists, the lint/test/coverage configs, the docs build: a
    # PR editing what its own `kew verify` runs against is never merged
    # without a person (#2184, #2192). The configured `[epic]
    # merge_gate_paths` are declined on top of these, never instead of them.
    (
        "gate definitions",
        tuple(EpicConfig.model_fields["merge_gate_paths"].default),
    ),
)

#: ``MergeVerifyResult.reason`` prefix for a PR the rule does not cover.
DECLINED = "drive-merge-declined"

_VERDICT_READY = "READY"
_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")

#: Where the drive fetches main and the PR head. Private refs, never
#: ``FETCH_HEAD``: a tick fetching in the same checkout would rewrite that file
#: between this fetch and its read. Only the drive writes these, and only one
#: drive runs per project, under the queue lock.
_BASE_REF = "refs/kew/drive-merge/base"
_HEAD_REF = "refs/kew/drive-merge/head"
_UI_DECLINE = "is a UI change"
#: The ``not_checked`` kinds the drive's own gates answer: its ``kew verify``
#: runs the full suite (forced when the READY names it, whatever
#: ``[verify] suite`` says), and a browser check of the head answers a browser
#: one.
_VERIFY_RUNS_UNCHECKED = frozenset({UNCHECKED_BROWSER, UNCHECKED_FULL_SUITE})


# ── the rule and its signature ──────────────────────────────────────────────


def rule_fingerprint(rule: DriveMergeConfig) -> str:
    """The rule as signed: every field that changes what the drive merges."""
    body = {
        "version": RULE_VERSION,
        "signer": rule.signer.lower(),
        "ui_paths": sorted(rule.ui_paths),
        "gate_classes": [[name, list(globs)] for name, globs in GATE_CLASSES],
    }
    # Only when on, so turning it on is a change the signer must sign while
    # every rule signed before the option existed keeps its fingerprint.
    if rule.local_fallback:
        body["local_fallback"] = True
    # Likewise only when set: excluding an epic narrows what merges, and the
    # signer still signs it, since lifting it later widens the rule again.
    if rule.exclude_epics:
        body["exclude_epics"] = excluded_epics(rule)
    # Only when it trades GitHub's checks for the verify: a wider rule the
    # signer signs, while every rule signed before it keeps its fingerprint.
    if rule.gate != "github":
        body["gate"] = rule.gate
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class SignedRule:
    signer: str
    fingerprint: str
    signed_now: bool = False


@dataclass(frozen=True)
class RuleRefused:
    detail: str


def _last_signed(store: RunStore) -> tuple[str, str] | None:
    """The ``(signer, fingerprint)`` of the rule last signed on the chain."""
    events = store.list_audit_events(limit=1, event=RULE_SIGNED_EVENT)
    if not events or not events[0].payload:
        return None
    payload = events[0].payload
    return str(payload.get("signer") or ""), str(payload.get("fingerprint") or "")


def authorize_rule(
    store: RunStore,
    rule: DriveMergeConfig,
    identity: Callable[[], ForgeIdentity] = observe_forge_identity,
) -> SignedRule | RuleRefused:
    """The rule's signature, signing a new or changed rule when the login
    ``gh`` runs as is the signer of record; a refusal otherwise."""
    fingerprint = rule_fingerprint(rule)
    last = _last_signed(store)
    if last is not None and last[1] == fingerprint:
        return SignedRule(signer=last[0], fingerprint=fingerprint)
    required = rule.signer if last is None else last[0]
    changed = "is new" if last is None else "changed since it was last signed"
    who = identity()
    if not who.available:
        return RuleRefused(
            f"the standing merge rule {changed}, and the login that would sign "
            f"it could not be read ({who.detail}); nothing merges until it can"
        )
    if who.login.lower() != required.lower():
        return RuleRefused(
            f"the standing merge rule {changed} under {who.login}, but only "
            f"{required} may sign it; nothing merges until {required} runs the "
            "drive or the rule is put back"
        )
    emit(
        store,
        RULE_SIGNED_EVENT,
        actor=Actor(type="human", id=who.login),
        payload={
            "signer": rule.signer,
            "signed_by": who.login,
            "fingerprint": fingerprint,
            "previous_signer": None if last is None else last[0],
            "previous_fingerprint": None if last is None else last[1],
            "version": RULE_VERSION,
            "ui_paths": sorted(rule.ui_paths),
            "gate_classes": sorted({name for name, _ in GATE_CLASSES}),
            "exclude_epics": excluded_epics(rule),
            "gate": rule.gate,
        },
        strict=True,
    )
    return SignedRule(signer=rule.signer, fingerprint=fingerprint, signed_now=True)


# ── what the rule covers ────────────────────────────────────────────────────


def decline_reason(
    paths: Sequence[str],
    *,
    ui_paths: Sequence[str],
    browser_checked: bool,
    gate_paths: Sequence[str] = (),
    unchecked: Sequence[str] = (),
) -> str | None:
    """Why the rule does not cover a PR with these net *paths*, or None.

    *gate_paths* is the configured ``[epic] merge_gate_paths``: it can only add
    to :data:`GATE_CLASSES`, so emptying it never widens what the drive merges.
    *unchecked* is the ``not_checked`` kinds of the head's READY (#3421). The
    drive's own ``kew verify`` runs the full suite, and a browser check of the
    head lifts a browser entry like a UI path — while a ui-check verify of
    *paths* answers every browser check they need
    (:func:`verify_answers_browser`); otherwise the browser check is a
    person's. Any other kind is a check only a person can stand behind.
    """
    for name, globs in (*GATE_CLASSES, ("gate definitions", tuple(gate_paths))):
        touched = touched_gate_paths(paths, globs)
        if touched:
            return f"touches the {name} gate class ({_named(touched)})"
    protected = [(p, r) for p in paths if (r := protected_path_reason(p))]
    if protected:
        path, reason = protected[0]
        return f"touches a protected publication path ({path}: {reason})"
    return _unchecked_decline(
        paths, ui_paths=ui_paths, browser_checked=browser_checked, unchecked=unchecked
    )


def _unchecked_decline(
    paths: Sequence[str],
    *,
    ui_paths: Sequence[str],
    browser_checked: bool,
    unchecked: Sequence[str],
) -> str | None:
    """The declines a check nobody has run yet answers: a person's, or a
    browser check's — the UI paths' and the READY's own alike."""
    person = {k for k in unchecked if k not in _VERIFY_RUNS_UNCHECKED}
    if UNCHECKED_BROWSER in unchecked and not verify_answers_browser(paths):
        # No verify record answers it, however the UI check went.
        person.add(UNCHECKED_BROWSER)
    if person:
        return (
            "its READY review could not run the "
            f"{', '.join(sorted(person))} checks; a person merges"
        )
    if browser_checked:
        return None
    ui = touched_gate_paths(paths, ui_paths)
    if ui:
        return _ui_decline(_named(ui))
    if UNCHECKED_BROWSER in unchecked:
        return _ui_decline("its READY review could not run the browser check")
    return None


def _ui_decline(what: str) -> str:
    """The decline a later browser check of the head lifts (`_settled_decline`)."""
    return (
        f"{_UI_DECLINE} ({what}) with no browser check of its head on "
        "record — a passing `kew verify` of that head that ran the UI check"
    )


#: What a tracker lookup or an audit write raises when it cannot answer now;
#: an exclusion it leaves unrecorded is retried next round. Anything else is a
#: defect and surfaces.
_RETRIED_EXCLUSION_FAILURES = (
    OSError,
    RuntimeError,
    ValueError,
    subprocess.SubprocessError,
    sqlite3.Error,
)


def excluded_reason(epic: str, rule: DriveMergeConfig) -> str | None:
    """Why the rule leaves every PR of *epic* to a person, or None."""
    if excluded_by_rule(epic, rule):
        return (
            f"epic {epic} is excluded from the standing merge rule "
            "([drive.merge] exclude_epics); a person merges it"
        )
    return None


def _named(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:3])
    return shown if len(paths) <= 3 else f"{shown} and {len(paths) - 3} more"


@dataclass(frozen=True)
class Candidate:
    epic: str
    item_id: str
    head: str


#: Driver statuses a node never leaves: nothing of it is left to merge. Only
#: these skip a candidate; every other status (a label-derived ``blocked``,
#: a transient ``held``) still reaches ``operator_merge``'s live gates.
_FINISHED = frozenset({"done", "cancelled"})


def _finished_nodes(store: RunStore, epic: str) -> tuple[set[str], datetime] | None:
    """The ids the epic's latest journaled tick shows finished, and when it
    ran; None when the epic never ticked or the tick time does not parse."""
    tick = store.latest_epic_tick(epic)
    if tick is None:
        return None
    try:
        at = _aware(datetime.fromisoformat(tick.tick_ts))
    except (TypeError, ValueError):
        _logger.warning(
            "kew drive: epic %s tick time %r does not parse; finished nodes "
            "stay merge candidates this round",
            epic,
            tick.tick_ts,
        )
        return None
    nodes = tick.snapshot.get("nodes") if isinstance(tick.snapshot, dict) else None
    finished = {
        str(node.get("id"))
        for node in nodes or ()
        if isinstance(node, dict) and node.get("status") in _FINISHED
    }
    return finished, at


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def ready_candidates(store: RunStore, epic: str) -> list[Candidate]:
    """The epic's nodes whose latest review round is READY, with its head.

    A merged node keeps its last READY round, so a node the epic's latest
    tick shows finished (done, cancelled) is skipped: it has nothing to
    merge, and reading its PR every round spent the tracker's budget for
    nothing (#2742). A READY round recorded AFTER that tick is never hidden
    by it (a tie fails open), and any other status still goes to the live
    gates. A finished item reopened after that tick waits for the next
    journaled tick to read it live again: one round, not a wrong merge.
    """
    finished = _finished_nodes(store, epic)
    seen: dict[str, None] = {}
    for review in store.list_review_rounds_for_epic(epic):
        seen.setdefault(review.item_id)
    found = []
    for item_id in seen:
        latest = store.latest_review_round(item_id)
        if latest is None or latest.verdict != _VERDICT_READY:
            continue
        if (
            finished is not None
            and item_id in finished[0]
            and _aware(latest.recorded_at) < finished[1]
        ):
            continue
        found.append(Candidate(epic, item_id, latest.head_sha.lower()))
    return found


# ── the verify host ─────────────────────────────────────────────────────────


def probe_verify_host(config: DispatchConfig) -> str | None:
    """Why ``[verify] host`` cannot be reached right now — naming the fix — or
    None when it answers. Starts, claims and ships nothing."""
    try:
        provider = provider_named(config, config.verify.host)
    except HostError as exc:
        return str(exc)
    try:
        provider.probe()
    except HostError as exc:
        fix = provider.unreachable_fix
        return f"{exc}; {fix}" if fix else str(exc)
    return None


# ── the combined-tree verification ──────────────────────────────────────────


class _GitError(RuntimeError):
    pass


@dataclass
class _CombinedVerify:
    """The verifier :func:`operator_merge` runs inside: serial, fresh, current.

    ``declined`` carries the rule's refusal out to the executor, which records
    it; every other refusal is retried next round.
    """

    rule: DriveMergeConfig
    run: RunFn
    verify: Callable[..., Any]
    #: The host to verify on instead of ``[verify] host`` (the local fallback).
    host: str | None = None
    #: Re-asked after a verify that reached no verdict: only a host that is
    #: gone excuses it from being held until main moves.
    probe: Callable[[DispatchConfig], str | None] = probe_verify_host
    declined: str | None = None
    paths: tuple[str, ...] = ()
    #: The main a ``kew verify`` failed on: that head is not verified again
    #: until main moves (or the head does).
    failed_on: str | None = None
    #: The PR description, whose **Behavior changes** section may excuse a
    #: held-out base test failure: the same body the squash message carries.
    pr_body: str | None = None
    #: The node's current acceptance contract, whose file the verify runs as
    #: its contract commit carries it; ``None`` for a node without one.
    contract: AcceptanceContractRow | None = None
    #: The main and the merged commit a passing ``kew verify`` ran on: what
    #: the post-merge mutation report is run against.
    verified: tuple[str, str] | None = None
    #: The PR the verify is for. The drive holds no merge claim, so the verify
    #: records it as data: the ledger pairs the PR's verifies by it, across
    #: its heads (:mod:`kew.verify_ledger`).
    pr: int | None = None
    #: The head's READY names a ``full_suite`` its reviewer could not run, so
    #: the verify runs the full suite whatever ``[verify] suite`` says: the
    #: rule waives that entry only because this verify runs it (#3421).
    force_full: bool = False

    def _git(self, *args: str, cwd: Path, strip: bool = True) -> str:
        result = self.run(["git", "-C", str(cwd), *args], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise _GitError(f"git {args[0]} exited {result.returncode}: {detail}")
        out = result.stdout or ""
        return out.strip() if strip else out

    def _remote_base(self, repo_root: Path, base: str) -> str:
        out = self._git("ls-remote", "origin", f"refs/heads/{base}", cwd=repo_root)
        sha = out.split()[0] if out else ""
        if not _SHA_RE.match(sha):
            raise _GitError(f"origin has no {base} branch to read")
        return sha.lower()

    @contextmanager
    def __call__(
        self, config: DispatchConfig, store: RunStore, **kw: Any
    ) -> Iterator[MergeVerifyResult]:
        with merge_queue_lock(merge_queue_lock_path(config)) as lease:
            if not lease.acquired:
                yield MergeVerifyResult(False, "held", lease.reason)
                return
            with tempfile.TemporaryDirectory(prefix="kew-drive-merge-") as scratch:
                tree = Path(scratch) / "tree"
                try:
                    yield self._verify(config, store, tree=tree, **kw)
                finally:
                    self._drop_tree(kw["repo_root"], tree)

    def _drop_tree(self, repo_root: Path, tree: Path) -> None:
        if not tree.exists():
            return
        try:
            self._git("worktree", "remove", "--force", str(tree), cwd=repo_root)
        except _GitError as exc:
            _logger.warning("kew drive: could not remove %s: %s", tree, exc)

    def _verify(
        self,
        config: DispatchConfig,
        store: RunStore,
        *,
        tree: Path,
        repo_root: Path,
        base: str,
        item_id: str,
        pr_number: int,
        head_oid: str,
        **_: Any,
    ) -> MergeVerifyResult:
        try:
            base_sha, merged, candidate_tree = self._combine(
                repo_root, tree, base, pr_number, head_oid
            )
        except _MoveError as exc:
            return MergeVerifyResult(False, "skip", f"{exc.reason}: {exc}")
        except _GitError as exc:
            return MergeVerifyResult(False, "held", f"{MERGE_VERIFY_UNRUNNABLE}: {exc}")
        unchecked = review_unchecked_kinds(store, item_id, head_oid)
        self.declined = decline_reason(
            self.paths,
            ui_paths=self.rule.ui_paths,
            browser_checked=browser_checked(store, head_oid),
            gate_paths=config.epic.merge_gate_paths,
            unchecked=unchecked,
        )
        self.force_full = UNCHECKED_FULL_SUITE in unchecked
        if self.declined is not None:
            return MergeVerifyResult(False, "held", f"{DECLINED}: {self.declined}")
        return self._run_verify(
            config,
            store,
            repo_root,
            base,
            base_sha,
            merged,
            candidate_tree,
        )

    def _combine(
        self, repo_root: Path, tree: Path, base: str, pr_number: int, head: str
    ) -> tuple[str, str, str]:
        """Merge the fetched main into the PR head in *tree*; read net paths."""
        self._git(
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{base}:{_BASE_REF}",
            f"+refs/pull/{pr_number}/head:{_HEAD_REF}",
            cwd=repo_root,
        )
        base_sha = self._git("rev-parse", f"{_BASE_REF}^{{commit}}", cwd=repo_root)
        fetched = self._git("rev-parse", f"{_HEAD_REF}^{{commit}}", cwd=repo_root)
        if fetched.lower() != head:
            raise _MoveError(
                MERGE_VERIFY_HEAD_MOVED,
                f"PR #{pr_number} is at {fetched[:12]}, not {head[:12]}",
            )
        self._git("worktree", "add", "--detach", str(tree), head, cwd=repo_root)
        # NUL-separated: a quoted path ("scripts/\303\244.sh") would match no
        # gate glob, so an unusual name could carry a gate edit past the rule.
        names = self._git(
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base_sha}...{head}",
            cwd=tree,
            strip=False,
        )
        self.paths = tuple(sorted(p for p in names.split("\0") if p))
        identity = ("-c", "user.name=kew", "-c", "user.email=kew@invalid.local")
        try:
            self._git(*identity, "merge", "--no-ff", "--no-edit", base_sha, cwd=tree)
        except _GitError as exc:
            raise _MoveError(MERGE_VERIFY_BASE_UNMERGEABLE, str(exc)) from exc
        return (
            base_sha.lower(),
            self._git("rev-parse", "HEAD", cwd=tree),
            self._git("rev-parse", "HEAD^{tree}", cwd=tree).lower(),
        )

    def _host_went_away(self, config: DispatchConfig) -> bool:
        """Whether the host the verify ran on cannot be reached now."""
        if self.host is not None:
            return False  # the local fallback: the operator machine stays up
        return self.probe(config) is not None

    def _run_verify(
        self,
        config: DispatchConfig,
        store: RunStore,
        repo_root: Path,
        base: str,
        base_sha: str,
        merged: str,
        candidate_tree: str,
    ) -> MergeVerifyResult:
        where: dict[str, Any] = {} if self.host is None else {"host": self.host}
        if self.contract is not None:
            where["contract"] = self.contract
        if self.force_full:
            where["force_full"] = True
        try:
            _candidate, result = self.verify(
                config,
                store=store,
                ref=merged,
                repo_root=repo_root,
                pr_body=self.pr_body,
                base_sha=base_sha,
                pr=self.pr,
                **where,
            )
        except VerifyLeaseRefused as exc:
            # Another local verify has the machine (#3190): contention, never
            # remembered as a failure, retried next round.
            return MergeVerifyResult(False, "held", f"{MERGE_VERIFY_LEASE_BUSY}: {exc}")
        except VerifyRefused as exc:
            return MergeVerifyResult(False, "held", f"{MERGE_VERIFY_UNRUNNABLE}: {exc}")
        refused = self._verdict_refusal(config, result, base_sha)
        if refused is not None:
            return refused
        self.verified = (base_sha, merged)
        return self._base_still_current(repo_root, base, base_sha, candidate_tree)

    def _verdict_refusal(
        self, config: DispatchConfig, result: Any, base_sha: str
    ) -> MergeVerifyResult | None:
        """Why ``kew verify``'s result does not let the merge go on, or None."""
        if result.outcome == "incomplete" and self._host_went_away(config):
            # No verdict because the host went away says nothing about the
            # tree, so it is not remembered as a failure: the next round's
            # probe pauses merges until the host answers, then retries.
            return MergeVerifyResult(
                False,
                "held",
                f"{MERGE_VERIFY_UNRUNNABLE}: kew verify reached no verdict on "
                f"{result.host} ({result.error or 'no exit code'})",
            )
        if result.outcome != "passed":
            # A failure — or no verdict from a host that still answers (a
            # suite timeout, a kill), which the same tree would repeat — is
            # held until main moves rather than re-run every round.
            self.failed_on = base_sha
            code = result.exit_code
            detail = result.error or (
                "no exit code" if code is None else f"exit {code}"
            )
            red = getattr(result, "main_red_refusal", None)
            if callable(red) and red():
                detail = f"{detail}; {red()}"
            return MergeVerifyResult(
                False,
                "held",
                f"{MERGE_VERIFY_FAILED}: kew verify {result.outcome}"
                f" on {result.host} ({detail})",
            )
        return None

    def _base_still_current(
        self, repo_root: Path, base: str, base_sha: str, candidate_tree: str
    ) -> MergeVerifyResult:
        """VERIFIED while main is still the one verified; else why not."""
        try:
            now = self._remote_base(repo_root, base)
        except _GitError as exc:
            return MergeVerifyResult(
                False, "held", f"{MERGE_VERIFY_BASE_UNREADABLE}: {exc}"
            )
        if now != base_sha:
            return MergeVerifyResult(
                False,
                "skip",
                f"{MERGE_VERIFY_BASE_MOVED}: {base} moved from {base_sha[:12]} to "
                f"{now[:12]} while the verification ran; retried next round",
            )
        return MergeVerifyResult(
            True,
            base_sha=base_sha,
            tree_sha=candidate_tree,
            contracts=acceptance_contract_versions((self.contract,)),
        )


class _MoveError(_GitError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


# ── the executor ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingReport:
    """A merge whose mutation report has not run yet."""

    item_id: str
    pr_number: int
    #: The main the merge was verified on: the diff's base.
    base_sha: str
    #: The commit the merge was verified as: main merged into the PR head.
    merged_sha: str


#: How many of the newest queued reports :func:`pending_reports` reads.
_REPORT_WINDOW = 200


def _queued(event: Any) -> PendingReport | None:
    """The report a :data:`REPORT_QUEUED_EVENT` owes, or None if malformed."""
    payload = event.payload or {}
    pr, base_sha, merged_sha = (
        payload.get("pr"),
        payload.get("base_sha"),
        payload.get("ref_sha"),
    )
    if not isinstance(pr, int) or not isinstance(base_sha, str):
        return None
    if not isinstance(merged_sha, str) or not _SHA_RE.match(merged_sha):
        return None
    return PendingReport(event.item_id or "", pr, base_sha, merged_sha)


def pending_reports(store: RunStore) -> list[PendingReport]:
    """The queued mutation reports no event has answered yet, oldest first."""
    queued = store.list_audit_events(limit=_REPORT_WINDOW, event=REPORT_QUEUED_EVENT)
    if not queued:
        return []
    since = datetime.fromisoformat(min(event.timestamp for event in queued))
    answered = {
        (event.payload or {}).get("ref_sha")
        for name in (MUTATION_EVENT, REPORT_DROPPED_EVENT)
        for event in store.list_audit_events(limit=None, event=name, since=since)
    }
    pending: list[PendingReport] = []
    for event in reversed(queued):
        report = _queued(event)
        if report is None or report.merged_sha in answered:
            continue
        answered.add(report.merged_sha)
        pending.append(report)
    return pending


@dataclass
class DriveMerger:
    """Merge what the signed rule covers, one PR at a time, once per round."""

    config: DispatchConfig
    store: RunStore
    repo: str
    repo_root: Path
    #: Builds a fresh facts reader each round: one may serve a tick's batch.
    reader: Callable[[], GitHubFactsReader]
    provider: str
    base: str = "main"
    identity: Callable[[], ForgeIdentity] = observe_forge_identity
    run: RunFn = _default_run
    verify: Callable[..., Any] = run_verify
    #: The post-merge mutation report job (:func:`run_mutation_report`).
    report: Callable[..., Any] = run_mutation_report
    say: Callable[[str], None] = _logger.info
    probe: Callable[[DispatchConfig], str | None] = probe_verify_host
    #: Where the one outage notice goes; ``[alerting]`` when None.
    notify: Callable[[NotificationEvent], None] | None = None
    merged: list[Candidate] = field(default_factory=list)
    #: Why the verify host is unreachable, while it is: merges are paused.
    host_down: str | None = None
    _said: dict[str, str] = field(default_factory=dict)
    _declined: dict[str, str] = field(default_factory=dict)
    _excluded: dict[str, str] = field(default_factory=dict)
    _failed: dict[str, str] = field(default_factory=dict)

    def _report_once(self, key: str, message: str) -> None:
        """Say *message* the first time *key* reads it, so rounds stay quiet."""
        if self._said.get(key) != message:
            self._said[key] = message
            self.say(message)

    def __call__(self, epics: Sequence[str]) -> None:
        if not self.config.drive.merge.enabled:
            return
        self._merge_pass(epics)
        self._run_reports()

    def _merge_pass(self, epics: Sequence[str]) -> None:
        rule = self.config.drive.merge
        signed = authorize_rule(self.store, rule, self.identity)
        if isinstance(signed, RuleRefused):
            self._report_once("rule", f"kew drive: merging nothing — {signed.detail}.")
            return
        if signed.signed_now:
            self.say(f"kew drive: standing merge rule signed by {signed.signer}.")
        candidates = [c for epic in epics for c in ready_candidates(self.store, epic)]
        if not candidates:
            return
        reader = self.reader()
        eligible = self._eligible(signed, candidates, reader)
        if not eligible:
            return
        host = self._verify_host()
        if host is None:
            return
        for candidate in eligible:
            self._attempt(signed, candidate, reader, host)

    def _eligible(
        self,
        signed: SignedRule,
        candidates: Sequence[Candidate],
        reader: GitHubFactsReader,
    ) -> list[Candidate]:
        """The candidates the rule may merge; an excluded epic's are declined
        here, before any git fetch or verify, and never wake the host probe
        on their own. (The PR number is still read from the tracker.)"""
        eligible = []
        for candidate in candidates:
            reason = excluded_reason(candidate.epic, self.config.drive.merge)
            if reason is None:
                eligible.append(candidate)
            else:
                self._exclude(signed, candidate, reader, reason)
        return eligible

    def _exclude(
        self,
        signed: SignedRule,
        candidate: Candidate,
        reader: GitHubFactsReader,
        reason: str,
    ) -> None:
        """Decline an excluded epic's PR once per head, with *reason*.

        Remembered only once the decline is on the chain: a tracker that does
        not answer, a PR not linked yet, or an audit write that fails is
        retried next round rather than recorded wrong or never.
        """
        key = f"{candidate.item_id}@{candidate.head}"
        facts_key: Any = (
            int(candidate.item_id) if self.provider == "github" else candidate.item_id
        )
        try:
            pr_number = reader.issue_facts(facts_key).pr_number
            if pr_number is None:
                return
            self._read_conflict(candidate, pr_number)
            if self._excluded.get(key) == reason:
                return
            self._record_decline(
                signed, candidate, pr_number, reason, (), kind="excluded", strict=True
            )
        except _RETRIED_EXCLUSION_FAILURES as exc:
            self._report_once(
                key,
                f"kew drive: {candidate.item_id} is excluded; recording it "
                f"failed ({exc}); retried.",
            )
            return
        self._excluded[key] = reason
        self._report_once(key, f"kew drive: not merging {candidate.item_id}: {reason}.")

    def _read_conflict(self, candidate: Candidate, pr_number: int) -> None:
        """Hold an excluded epic's READY PR that stopped merging into the base.

        The merge pass never reaches an excluded epic, so nothing else reads
        whether its PR still merges, and a conflicting one kept reading
        merge-ready (#2862). Read every round, since the base moves under an
        unchanged head. A conflict at the reviewed head gets the merge pass's
        own ``ci_pending:dirty`` hold: the tick reports the node as conflicting,
        and its review stage runs the merge-main step (the resolve) exactly as
        for any other epic. Nothing here merges. A clean read releases that
        hold once the conflict is gone; an unreadable one changes nothing.

        The epic's own tick classifies a conflict it reads first, before its
        review stage (``excluded_conflict.hold_excluded_conflicts``); this
        read, after the round, catches one a merge in the same round caused.
        Both transitions are atomic and scoped to the conflict hold (#2869),
        so a detached resolver's ``base_integration_*`` hold is never replaced
        or erased.
        """
        pr = self._mergeability(pr_number)
        if pr is None:
            return
        key = f"{candidate.item_id}@{candidate.head}:conflict"
        mergeable = str(pr.get("mergeable") or "").upper()
        state = str(pr.get("mergeStateStatus") or "").upper()
        if mergeable == "CONFLICTING" or state == DIRTY_MERGE_STATE:
            if str(pr.get("headRefOid") or "").lower() == candidate.head:
                self._hold_conflict(candidate, pr_number, key)
        elif mergeable == "MERGEABLE" and release_conflict(
            self.store, candidate.epic, candidate.item_id
        ):
            self._said.pop(key, None)

    def _mergeability(self, pr_number: int) -> dict[str, Any] | None:
        """The PR's ``mergeable``, ``mergeStateStatus`` and head, or None."""
        result = self.run(
            ["gh", "pr", "view", str(pr_number), "--repo", self.repo]
            + ["--json", "mergeable,mergeStateStatus,headRefOid"],
            check=False,
        )
        try:
            pr = json.loads(result.stdout or "") if result.returncode == 0 else None
        except ValueError as exc:
            _logger.warning(
                "kew drive: PR #%s merge state is not JSON: %s", pr_number, exc
            )
            return None
        return pr if isinstance(pr, dict) else None

    def _hold_conflict(self, candidate: Candidate, pr_number: int, key: str) -> None:
        """Hold the node ``ci_pending:dirty`` (never over another writer's
        hold) and say once that its PR conflicts with the base."""
        if not hold_conflict(self.store, candidate.epic, candidate.item_id):
            return
        self._report_once(
            key,
            f"kew drive: {candidate.item_id}'s PR #{pr_number} conflicts with "
            f"{self.base} — not merge-ready; the epic's tick resolves it, and "
            "a person still merges it.",
        )

    def _verify_host(self) -> str | None:
        """The host this pass verifies on, or None while merges are paused.

        Probed every pass that has something to merge, so an outage ends on
        its own the round the host answers again.
        """
        host = self.config.verify.host
        down = self.probe(self.config)
        if down is None:
            if self.host_down is not None:
                self.host_down = None
                self.say(f"kew drive: verify host {host} answers again; merges resume.")
            return host
        if self.host_down is None:
            self._outage(host, down)
        self.host_down = down
        if self.config.drive.merge.local_fallback and host != "local":
            return "local"
        return None

    def _outage(self, host: str, detail: str) -> None:
        """Say once per outage that merges are paused, and how to resume them."""
        fallback = self.config.drive.merge.local_fallback and host != "local"
        paused = (
            "Merges verify on the operator machine instead (local_fallback)"
            if fallback
            else "Merges are paused — epics keep dispatching and reviewing — "
            "and resume on their own once the host answers"
        )
        message = (
            f"kew drive: verify host {host} cannot be reached ({detail}). {paused}."
        )
        self.say(message)
        emit(
            self.store,
            HOST_UNREACHABLE_EVENT,
            actor=Actor(type="policy", id="drive"),
            repo=self.repo,
            payload={"host": host, "detail": detail, "local_fallback": fallback},
        )
        notify = self.notify or build_notifier(self.config).notify
        try:
            notify(
                NotificationEvent(
                    event_type=HOST_UNREACHABLE_EVENT,
                    severity="warning",
                    message=message,
                    metadata={"host": host, "detail": detail},
                )
            )
        except (OSError, RuntimeError) as exc:
            _logger.warning("kew drive: outage notice not delivered: %s", exc)

    def _message(self, pr_number: int) -> tuple[str, str] | None:
        result = self.run(
            ["gh", "pr", "view", str(pr_number), "--repo", self.repo]
            + ["--json", "title,body"],
            check=False,
        )
        try:
            data = json.loads(result.stdout or "") if result.returncode == 0 else None
        except ValueError as exc:
            _logger.warning("kew drive: PR #%s view is not JSON: %s", pr_number, exc)
            data = None
        if not isinstance(data, dict) or not str(data.get("title") or "").strip():
            return None
        title = str(data["title"]).strip()
        return f"{title} (#{pr_number})", str(data.get("body") or "")

    def _settled_decline(self, key: str, head: str) -> bool:
        """A head the rule already declined, and still declines: its paths
        cannot change, so only a browser check recorded since can lift it."""
        reason = self._declined.get(key)
        if reason is None:
            return False
        return not (
            reason.startswith(_UI_DECLINE) and browser_checked(self.store, head)
        )

    def _failed_on_this_main(self, key: str, verifier: _CombinedVerify) -> bool:
        """A head whose verify failed on the main still current: a re-run would
        verify the same tree. A push (a new head) or a new main retries it."""
        failed = self._failed.get(key)
        if failed is None:
            return False
        try:
            return verifier._remote_base(self.repo_root, self.base) == failed
        except _GitError as exc:
            self._report_once(key, f"kew drive: cannot read {self.base}: {exc}")
            return True

    def _attempt(
        self,
        signed: SignedRule,
        candidate: Candidate,
        reader: GitHubFactsReader,
        host: str,
    ) -> None:
        key = f"{candidate.item_id}@{candidate.head}"
        verifier = _CombinedVerify(
            self.config.drive.merge,
            self.run,
            self.verify,
            host=None if host == self.config.verify.host else host,
            probe=self.probe,
        )
        if self._settled_decline(key, candidate.head):
            return
        if self._failed_on_this_main(key, verifier):
            return
        facts_key: Any = (
            int(candidate.item_id) if self.provider == "github" else candidate.item_id
        )
        pr_number = reader.issue_facts(facts_key).pr_number
        if pr_number is None:
            return
        message = self._message(pr_number)
        if message is None:
            self._report_once(
                key, f"kew drive: PR #{pr_number}: title unreadable; retried."
            )
            return
        verifier.pr_body = message[1]
        verifier.pr = pr_number
        verifier.contract = self.store.current_acceptance_contract(
            candidate.epic, candidate.item_id
        )
        try:
            outcome = operator_merge(
                self.config,
                self.store,
                epic_ref=candidate.epic,
                item_id=candidate.item_id,
                head=candidate.head,
                repo=self.repo,
                base=self.base,
                repo_root=self.repo_root,
                reader=reader,
                actor=Actor(type="policy", id=f"drive-merge-rule:{signed.signer}"),
                provider=self.provider,
                run=self.run,
                verify=verifier,
                message=message,
                trigger=MERGE_TRIGGER,
                require_merge_queue=False,
                checks=self.config.drive.merge.gate == "github",
            )
        except OperatorMergeRefused as exc:
            if verifier.declined is not None:
                self._declined[key] = verifier.declined
                self._record_decline(
                    signed, candidate, pr_number, verifier.declined, verifier.paths
                )
            if verifier.failed_on is not None:
                self._failed[key] = verifier.failed_on
            self._report_once(key, f"kew drive: not merging {candidate.item_id}: {exc}")
            return
        self.merged.append(candidate)
        self._queue_report(candidate, pr_number, verifier)
        self.say(
            f"kew drive: merged PR #{outcome.pr_number} for {candidate.item_id} "
            f"at {outcome.head[:12]} under {signed.signer}'s standing rule."
        )

    def _record_decline(
        self,
        signed: SignedRule,
        candidate: Candidate,
        pr_number: int,
        reason: str,
        paths: Sequence[str],
        *,
        kind: str = "",
        strict: bool = False,
    ) -> None:
        emit(
            self.store,
            DECLINED_EVENT,
            actor=Actor(type="policy", id=f"drive-merge-rule:{signed.signer}"),
            issue_num=(
                int(candidate.item_id)
                if GITHUB_ID_RE.fullmatch(candidate.item_id)
                else 0
            ),
            item_id=candidate.item_id,
            repo=self.repo,
            payload={
                "epic": candidate.epic,
                "pr": pr_number,
                "sha": candidate.head,
                "reason": reason,
                "paths": list(paths[:50]),
                "rule": signed.fingerprint,
            },
            # An exclusion is its own decline: a UI decline of the same head
            # recorded before the epic was excluded must not hide it.
            dedup_key=(
                f"{DECLINED_EVENT}:{candidate.item_id}:{candidate.head}"
                + (f":{kind}" if kind else "")
            ),
            strict=strict,
        )

    @property
    def reports(self) -> list[PendingReport]:
        """Merges whose mutation report is still to run, oldest first."""
        return pending_reports(self.store)

    def _report_queue(self) -> ReportQueue:
        return ReportQueue(
            self.config,
            self.store,
            repo=self.repo,
            repo_root=self.repo_root,
            report=self.report,
            run=self.run,
            say=self.say,
        )

    def _queue_report(
        self, candidate: Candidate, pr_number: int, verifier: _CombinedVerify
    ) -> None:
        """Queue the merged PR's mutation report, against what was verified."""
        if verifier.verified is None:
            return
        base_sha, merged_sha = verifier.verified
        self._report_queue().add(
            PendingReport(candidate.item_id, pr_number, base_sha, merged_sha)
        )

    def _run_reports(self) -> None:
        """Run the queued mutation reports unless the verify host is down.

        Called after the pass's merges, so a READY PR this pass verifies never
        waits behind a report (:meth:`ReportQueue.run_owed`).
        """
        if self.host_down is None:
            self._report_queue().run_owed()


@dataclass
class ReportQueue:
    """The post-merge mutation reports the store owes, and the job that runs them.

    Every merge path queues here — the standing rule and the project's merge
    queue (#2814) — so each merged PR is owed one report whichever merged it.
    """

    config: DispatchConfig
    store: RunStore
    repo: str
    repo_root: Path
    #: The post-merge mutation report job (:func:`run_mutation_report`).
    report: Callable[..., Any] = run_mutation_report
    run: RunFn = _default_run
    say: Callable[[str], None] = _logger.info

    def owed(self) -> list[PendingReport]:
        """Merges whose mutation report is still to run, oldest first."""
        return pending_reports(self.store)

    def add(self, pending: PendingReport) -> None:
        """Queue *pending*'s report: the store holds the queue."""
        self._record(REPORT_QUEUED_EVENT, pending, strict=True)

    def _record(
        self, event: str, pending: PendingReport, *, strict: bool = False, **extra: Any
    ) -> None:
        """Record *event* for *pending*'s commit."""
        emit(
            self.store,
            event,
            actor=Actor(type="policy", id="drive"),
            item_id=pending.item_id or None,
            repo=self.repo,
            payload={
                "pr": pending.pr_number,
                "ref_sha": pending.merged_sha,
                "base_sha": pending.base_sha,
                **extra,
            },
            dedup_key=f"{event}:{pending.merged_sha}",
            strict=strict,
        )

    def run_owed(self) -> None:
        """Run the queued mutation reports, yielding to any gating verify.

        Each report runs holding the merge-queue lock: a gating verification
        already holding it makes the report yield, and one arriving while the
        report runs is refused the queue — contention, which it retries —
        instead of reaching a host the report holds, where it would be refused
        as an incomplete verify and held until main moved.
        """
        ran: set[str] = set()
        while True:
            with merge_queue_lock(merge_queue_lock_path(self.config)) as lease:
                if not lease.acquired:
                    return  # a gating verification is under way: yield to it
                if not self._run_next(ran):
                    return

    def _run_next(self, ran: set[str]) -> bool:
        """Run the oldest owed report; False when none is owed or it yields.

        Read under the queue lock, so a report another drive ran meanwhile is
        already answered and is not run twice. *ran* is this pass's: a report
        whose answer the store failed to keep is not re-run until the next.
        """
        owed = (p for p in self.owed() if p.merged_sha not in ran)
        pending = next(owed, None)
        if pending is None:
            return False
        ran.add(pending.merged_sha)
        try:
            report = self.report(
                self.config,
                store=self.store,
                merged_sha=pending.merged_sha,
                base_sha=pending.base_sha,
                pr_number=pending.pr_number,
                repo_root=self.repo_root,
            )
        except VerifyRefused as exc:
            # A refusal (a commit kew cannot bundle, a host config it cannot
            # use) would refuse again: dropped, so the queue moves.
            self._record(REPORT_DROPPED_EVENT, pending, reason=str(exc))
            self.say(
                f"kew drive: mutation report for PR #{pending.pr_number} "
                f"could not run: {exc}"
            )
            return True
        if report is None:
            return False  # the host is held by another job: yield to it
        self._post(pending, report)
        return True

    def _post(self, pending: PendingReport, report: Any) -> None:
        """Post *report* to the merged PR's thread; it is already recorded."""
        result = self.run(
            ["gh", "pr", "comment", str(pending.pr_number), "--repo", self.repo]
            + ["--body", mutation_comment(pending.merged_sha, report)],
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            _logger.warning(
                "kew drive: mutation report not posted to PR #%s: %s",
                pending.pr_number,
                detail,
            )
