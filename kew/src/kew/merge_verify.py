"""``kew verify`` — one merge verification on a host, recorded as it happened.

The operator's drive ran a merge verify as two shell scripts: one built the
candidate and bundled it, the other ran the gate on a Compute Engine instance
and wrote a CSV row of timings. None of it was a kew command, so kew could not
attest that a merge had been verified at all. This module is that job, in kew.

What runs on the host (:func:`job_script`) is deliberately kew's own gate and
nothing of its own invention:

* ``umask 022``, so files the job writes are readable by the next job.
* the pinned agent images from ``[sandbox] images``, built **when missing** —
  a host that already has them rebuilds nothing.
* ``bash scripts/verify.sh``, the one local gate that mirrors PR-gating CI
  (``tests/test_verify_parity.py`` is what keeps the two the same). The
  private Switchyard layer inside it stays opt-in through
  ``KEW_SWITCHYARD_IMAGE``, and the opt-in is forwarded only to a host that
  can build from the private input at all
  (:attr:`~kew.hosts.provider.HostProvider.builds_private_images`) and only
  together with ``KEW_SWITCHYARD_SOURCE_ARCHIVE``, the path at which that
  host finds the input. The archive itself never leaves the operator machine;
  only the operator machine is ever told where it is.
* ``npm run ui-check`` when, and only when, ``frontend/`` differs from the
  base. A console change verified without a browser is the failure mode that
  rule exists for; a backend change does not pay for a browser run. It runs
  requiring a browser (:data:`UI_CHECK_COMMAND`): here, no Chromium fails the
  verification rather than skipping the checks.

The candidate goes in as a git bundle whose ``HEAD`` is the ref being verified,
built through a temporary bare repository that borrows the project's objects
(``objects/info/alternates``). That indirection buys two things: an arbitrary
ref — a sha, a tag, a branch kew is not standing on — becomes the bundle's
``HEAD``, which is what makes ``git clone`` of the bundle produce a checkout at
all; and the operator's repository is never written to, not even a temporary
ref.

Nothing the host returns is read as an instruction: the result is an exit code,
a duration and log text, and the audit event records exactly that. Only what
the *job* answered decides the outcome — a host kew could not release is
recorded beside whatever the job answered, never in place of it and never
dropped when the job answered nothing, and a job a signal killed is recorded as
having answered nothing at all, in whichever shape the kill reached kew
(:func:`killing_signal`).

``--shards N`` runs that same gate across N hosts instead of one: the parallel
pytest phase as N jobs, ``--shard=K/N`` each, and everything that runs once on
the first host of the pool beside its own shard (:func:`run_sharded`). The
verification passes only when every job does and the shards' selected counts
sum to one unsharded collection (:func:`count_check`), and every shard's run
reported every test it collected; a host that answered nothing fails it rather
than being skipped.

The verification runs in two jobs (#3212). The **gate job**
(:func:`job_script`) runs ``verify.sh``'s static gates and containment test
(:data:`STAGE_GATES`), then the held-out phase; kew judges it, and only a
clean one is followed by the **suite job** (:func:`suite_job_script`): the
pytest phases and every gate after them (:data:`STAGE_SUITE`), ``ui-check``
and the red-main phase. A red gate job, or an undeclared held-out failure,
ends the verification in minutes, with no pytest phase started and the phase
it stopped in named (:func:`stopped_phase`). A sharded verification runs the
gate job on the first host, and every host holds its suite jobs until kew has
judged it (:class:`GateOpening`). ``verify.sh`` run by hand or in CI sets no
stage and runs every gate, as before.

The **held-out** phase (#2485) runs the base branch's own
tests, which the candidate cannot have edited, run against the candidate's
``src`` in a temporary tree outside the checkout (:func:`held_out_lines`). A
base test that fails there fails the verification unless the PR body's
**Behavior changes** section names its node id with a reason
(:func:`declared_behavior_changes`, :func:`held_out_report`). A declared test
with no ``[...]`` part also declares every parametrization of it, so an id a
PR body cannot spell — one with spaces or quotes in it — is declared through
its test function (#2795); a declared test never covers another test, and a
declared file covers none of the tests in it. Only the base
versions of the test files the candidate changed are run, unless it changed
something they share (:mod:`kew.held_out_selection`, #2816).

A node with an **acceptance contract** (#2730) adds its contract file to that
held-out tree, taken from the commit the ledger recorded it at, never from the
head (:func:`read_contract`). A contract test that fails there fails the
verification, and no PR body can declare it away. The contract also gets a run
of its own, by path and with ``addopts`` cleared, which must pass at least one
of its tests (:func:`_contract_run_lines`): a contract never collected did not
pass.

Every held-out and contract run has plugin autoload off and loads only the
pinned plugins (:mod:`kew.pinned_pytest_plugins`), plus kew's own report plugin
(:mod:`kew.held_out_report_plugin`), whose JSON report — not the run's log — is
where its verdict is read from (#3194). Each takes the base's pytest
configuration, never the head's (:func:`held_out_config`), and must collect
every test the base's own collection of the same run does (:func:`_run_lines`).

The **mutation** report (#2495) is not part of the gate (#2817): it never
changed a verdict, and every merge waited on it. A gating verification reports
``mutation=deferred``; after the merge, :func:`run_mutation_report` runs mutmut
as its own job on the verify host — only the mutants that sit on the ``src/``
lines the merged commit changed against the base it was verified on, under
:data:`MUTATION_CAP_SECONDS` (``scripts/mutmut-sampler.py --diff-scope``) — and
records its survivors, file, line and mutation, as :data:`MUTATION_EVENT`.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

from kew import red_main_check, subprocess_utils, verify_executed, work_dirs
from kew.audit import emit, resolve_actor
from kew.config import DispatchConfig
from kew.held_out_selection import (
    FULL,
    NO_TEST_FILE_CHANGED,
    SELECTED,
    HeldOutSelection,
    select_held_out,
)
from kew.host_takeovers import record_lapsed_leases
from kew.hosts import (
    HostBusy,
    HostError,
    HostProvider,
    HostTimeout,
    Job,
    JobResult,
    provider_pool,
)
from kew.pinned_pytest_plugins import (
    AUTOLOAD_OFF,
    CHECKOUT_ENV,
    EXPECTED_ENV,
    LIST_COLLECTED_ENV,
    NAME_PASSES_ENV,
    NO_PINS,
    PLUGIN_PREFIX,
    REPORT_ENV,
    SETTINGS_ENV,
    TRUSTED_ENV,
    Pins,
    pins,
)
from kew.verify_frontend import FULL_FRONTEND, FrontendSelection, choose_frontend
from kew.verify_lease import VerifyLeaseBusy, hold_verify_lease
from kew.verify_ledger import (
    SETUP,
    ledger_payload,
    phase_line,
    resolve_after,
    write_log,
)
from kew.verify_suite import FULL_SUITE, SuiteRequest, SuiteSelection, choose_suite

if TYPE_CHECKING:
    from kew.run_store import RunStore
    from kew.run_store_acceptance import AcceptanceContractRow

logger = logging.getLogger(__name__)


class ClaimWatch(Protocol):
    """The merge claim a verify holds (:class:`kew.merge_claim.HeldClaim`)."""

    pr: int
    heads: tuple[str, ...]

    def lapsed(self) -> bool: ...


#: The audit event every verification writes — passed, failed or incomplete.
VERIFY_EVENT = "verify_completed"

#: The job name, as it appears in a :class:`~kew.hosts.provider.JobResult` and
#: in the host's lock diagnostics.
JOB_NAME = "merge-verify"

#: The opt-in switch for the private Switchyard image layer, read by
#: ``scripts/scan-agent-image.sh`` and ``tests/conftest.py``. Forwarded only to
#: a host that can build from the private input.
SWITCHYARD_IMAGE_ENV = "KEW_SWITCHYARD_IMAGE"

#: Where ``scripts/scan-agent-image.sh`` finds the pinned private input the
#: opt-in asks it to build from. A path on the operator machine, not the
#: archive: the bytes never move. The gate refuses with ``input is missing``
#: when the opt-in is set and this is not, so the two travel as a pair.
SWITCHYARD_ARCHIVE_ENV = "KEW_SWITCHYARD_SOURCE_ARCHIVE"

#: The base ``scripts/check_memory_channel.py`` classifies commits against.
#: Passed as the resolved base sha, because the bundle carries the candidate's
#: history and not the base *branch* the guard would otherwise look for.
MEMORY_GUARD_ENV = "MEMORY_GUARD_BASE"

#: The runners ``kew sandbox build`` can build an image for. A ``[sandbox]
#: images`` entry for any other runner is left alone rather than guessed at.
BUILDABLE_RUNNERS = ("claude-code", "codex", "opencode")

#: The ref name the candidate takes inside the bundle.
_BUNDLE_REF = "refs/heads/kew-verify"

#: What a POSIX shell adds to a signal number when a command it ran was killed
#: by one: ``$?`` reads ``128 + N``. What either host runs the gate under is a
#: *shell*, so this is the form a kill usually arrives in.
_SHELL_SIGNAL_BASE = 128

#: The highest signal number a shell can report that way. Past it a status is
#: just a status, and is read as the gate's own.
_MAX_SIGNAL = 64


class VerifyRefused(RuntimeError):
    """The verification could not be set up, so no job ran."""


class VerifyLeaseRefused(VerifyRefused):
    """Another local verify holds the lease (#3190): nothing is broken, the
    machine is busy, and the same verify goes once that one ends."""


@dataclass(frozen=True)
class TrustedContract:
    """A node's acceptance contract file, as its recorded commit carries it."""

    #: Repository-relative path of the file, under :data:`HELD_OUT_TESTS`.
    path: str

    #: The file's text at the contract commit, matching the ledger's sha256.
    text: str


@dataclass(frozen=True)
class BaseConfig:
    """The base's pytest configuration, as the held-out runs are given it."""

    #: The file it is written to in the held-out project directory, beside
    #: where the base keeps it, so its relative paths mean what they mean on
    #: the base; ``-c`` names it.
    name: str

    #: Its text, as the base commit carries it.
    text: str


#: A base with no pytest configuration of its own: an empty one, so pytest
#: never goes looking for the head's.
EMPTY_CONFIG = BaseConfig(name=".kew-held-out-base.ini", text="[pytest]\n")


@dataclass(frozen=True)
class VerifyCandidate:
    """What is being verified, and what it is being verified against."""

    #: The ref as the operator named it (``HEAD`` by default).
    ref: str

    #: The commit that ref resolves to.
    ref_sha: str

    #: The base ref kew found (``origin/main``, ``main``, …).
    base_ref: str

    #: The merge base of the candidate and the base ref.
    base_sha: str

    #: Whether ``frontend/`` differs between the base and the candidate.
    frontend_changed: bool

    #: The node's acceptance contract, which the held-out phase runs beside
    #: base's tests; ``None`` for a node without one.
    contract: TrustedContract | None = None

    #: More contracts run exactly as ``contract`` is — a merge-queue train
    #: verifies every entry's at once (#2814).
    contracts: tuple[TrustedContract, ...] = ()

    #: Which base test files the held-out phase runs (#2816): the whole tree
    #: unless :func:`resolve_candidate` read a narrower selection off the diff.
    held_out: HeldOutSelection = HeldOutSelection()

    #: The parents of ``ref_sha``, as git listed them (#3140). The drive and an
    #: operator's ad hoc merge verify a merge commit of the base and the PR
    #: head — the drive's has the head first, the ad hoc one (main checked out,
    #: the PR merged in) has it second — so the parent that is not
    #: ``base_sha`` is the head the event joins its item by.
    #: Empty when the parents could not be read.
    ref_parents: tuple[str, ...] = ()

    #: What the held-out and contract runs may load: the pytest plugins and
    #: packages the base's ``kew/uv.lock`` pins (:func:`held_out_pins`). The
    #: default pins nothing, so a run judged by it is refused.
    held_out_pins: Pins = NO_PINS

    #: The base's own pytest configuration, which every held-out and contract
    #: run is given by ``-c`` (:func:`held_out_config`). The default is an
    #: empty configuration.
    held_out_config: BaseConfig = EMPTY_CONFIG

    #: The Python the held-out runs ask uv for, as the base's ``kew/`` asks
    #: for it (:func:`held_out_python`); found by uv outside any project. A
    #: job that synced the checkout asks for its exact interpreter instead
    #: (:func:`main_python_lines`).
    held_out_python: str = "3"

    #: Whether the diff touches an input of the agent images
    #: (:func:`image_inputs_changed`). A candidate that touches none skips the
    #: image scan, and says so; the nightly scans every image in its own job.
    image_inputs_changed: bool = True

    #: The candidate's tree, which a squash merge of exactly what was verified
    #: carries too: how main health knows the candidate landed unchanged.
    ref_tree: str = ""

    #: ``pr`` for a candidate, ``nightly`` for the nightly's verify of main.
    kind: str = "pr"

    #: The failing node ids of a red main that reproduced alone, which this
    #: verification runs once more after its gate (:mod:`kew.red_main_check`):
    #: a landed PR clears that red only when each one passed here.
    red_tests: tuple[str, ...] = ()

    #: What the two pytest phases run (#3202): the full suite, unless
    #: ``[verify] suite = "tiered"`` chose the selection (:mod:`kew.verify_suite`).
    suite: SuiteSelection = FULL_SUITE

    #: Every path the candidate's diff against its base touches, which the
    #: verify ledger reads each phase's changed inputs off
    #: (:func:`kew.verify_ledger.changed_inputs`).
    changed_paths: tuple[str, ...] = ()

    #: What vitest and ``ui-check`` run (#3213): everything, unless
    #: ``[verify] frontend = "scoped"`` chose what the change owns
    #: (:mod:`kew.verify_frontend`).
    frontend: FrontendSelection = FULL_FRONTEND

    @property
    def runs_ui_check(self) -> bool:
        """Whether the job runs the browser ``ui-check``.

        A candidate runs it when its diff moved the console. The nightly
        verifies main against itself, so its diff is empty: it runs it always.
        """
        return self.frontend_changed or self.kind == "nightly"

    @property
    def scans_images(self) -> bool:
        """Whether this verification's job runs the agent-image scan."""
        return self.image_inputs_changed and self.kind != "nightly"

    @property
    def image_scan_reason(self) -> str:
        """Why the job runs the image scan, or why it does not."""
        if self.kind == "nightly":
            return "the nightly scans every image in its own job"
        if not self.image_inputs_changed:
            return "no image input changed (" + ", ".join(IMAGE_INPUTS) + ")"
        return "an image input changed"

    @property
    def held_contracts(self) -> tuple[TrustedContract, ...]:
        """Every acceptance contract the held-out phase runs."""
        first = () if self.contract is None else (self.contract,)
        return first + self.contracts


@dataclass(frozen=True)
class VerifyResult:
    """The host's answer, as data."""

    #: The provider that ran the job (``"local"``, ``"gcp"``).
    host: str

    #: The job's exit code, or ``None`` when the job never produced one.
    exit_code: int | None

    #: Wall-clock seconds, measured by kew around the job.
    seconds: float

    #: The job's combined output.
    log: str

    #: Why the verification is incomplete, or what went wrong around a job
    #: that still answered — a host kew could not release, say.
    error: str | None = None

    #: How the host classified the failure that ended the session, when it
    #: classified it at all (``host_connection_lost``,
    #: ``host_job_deadline_exceeded``). The category is what tells a *retryable
    #: interruption* — a session the network killed, which says nothing about
    #: the code under test — from a host that answered, and the reason text
    #: alone cannot be read for that by anything but a person.
    error_category: str | None = None

    #: One report per job when the verification was sharded across hosts, in
    #: dispatch order (shard 1..N, then the single-run job). Empty for an
    #: unsharded verification, which is one job and already reports itself.
    shards: tuple[ShardReport, ...] = ()

    #: What one unsharded collection selects, as the first host counted it.
    #: The number the shards' own counts are summed against; ``None`` outside
    #: a sharded verification.
    collected: int | None = None

    #: What the held-out phase answered, read off the log (:func:`apply_held_out`),
    #: or ``None`` when it was never read.
    held_out: HeldOutReport | None = None

    #: The first job's own log (:func:`job_script`), where the held-out section
    #: is read from (:func:`held_out_sections`), or ``None`` for a result no
    #: staged run produced. Nothing a later job prints is read as held-out's.
    gate_log: str | None = None

    #: What the mutation phase reported, read off the log (:func:`mutation_report`),
    #: or ``None`` when it was never read. Never part of the verdict.
    mutation: MutationReport | None = None

    #: The durable audit row for a verification that gates a merge.
    receipt: int | None = None

    #: Main's health when it was red as this PR verification ran
    #: (:class:`kew.main_health.MainHealth`), or ``None`` when it was not.
    main_red: Any = None

    #: What the red-main phase found for each of the candidate's
    #: :attr:`VerifyCandidate.red_tests`, or ``None`` with no evidence.
    red_outcomes: dict[str, str] | None = None

    def main_red_refusal(self) -> str | None:
        """Why this verdict refuses because main is red, or ``None``."""
        if self.main_red is None or self.outcome == "passed":
            return None
        issue = self.main_red.issue
        tracked = "" if issue is None else f" (issue #{issue})"
        return (
            f"main is red at {self.main_red.sha}{tracked}; every PR verify runs "
            "the full suite and must pass, so only a change that fixes main merges"
        )

    @property
    def outcome(self) -> str:
        """``passed``, ``failed`` or ``incomplete`` — never inferred later."""
        if self.exit_code is None:
            return "incomplete"
        return "passed" if self.exit_code == 0 else "failed"


def _git(repo_root: Path, *args: str, check: bool = True) -> str:
    """Run one read-only ``git`` command in *repo_root* and return its stdout."""
    try:
        completed = subprocess_utils.run(
            ["git", *args], cwd=repo_root, check=check, capture=True
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit {exc.returncode}"
        raise VerifyRefused(f"git {' '.join(args)}: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerifyRefused(f"git {' '.join(args)} could not run: {exc}") from exc
    return (completed.stdout or "").strip()


def _object_store(repo_root: Path) -> str:
    """Where this checkout's objects really live.

    A linked worktree's ``.git`` is a *file* naming the real git directory, so
    ``repo_root/.git/objects`` is no directory at all there and the alternates
    file written from it is refused ("unable to normalize alternate object
    path"). Asking git is the one reading that holds for either layout, and it
    answers with the *common* object store — the one holding the candidate.
    """
    return _git(
        repo_root, "rev-parse", "--path-format=absolute", "--git-path", "objects"
    )


def _resolve_base_ref(repo_root: Path, base: str) -> str:
    """The first of ``origin/<base>`` and ``<base>`` that resolves."""
    for candidate in (f"origin/{base}", base):
        resolved = _git(
            repo_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{candidate}^{{commit}}",
            check=False,
        )
        if resolved:
            return candidate
    raise VerifyRefused(
        f"neither origin/{base} nor {base} resolves in this checkout, so the "
        "candidate has no base to be verified against"
    )


#: The tree the browser ``ui-check`` checks. A candidate whose diff touches it
#: runs ``ui-check`` (:attr:`VerifyCandidate.runs_ui_check`); one that touches
#: nothing under it never does, ``--full`` or not.
UI_CHECK_ROOT = "frontend"


def touches_ui_check(paths: Iterable[str]) -> bool:
    """Whether a diff with these *paths* runs ``ui-check`` in ``kew verify``.

    The one rule :func:`resolve_candidate` sets ``frontend_changed`` by, and
    the merge paths read to know whether a verify of a head can ever record a
    browser check of it (#3421): a path at or under :data:`UI_CHECK_ROOT`, as
    git's ``-- frontend`` pathspec matches one. *paths* lists both sides of a
    rename (``git diff --no-renames``).
    """
    under = f"{UI_CHECK_ROOT}/"
    return any(path == UI_CHECK_ROOT or path.startswith(under) for path in paths)


#: Trees with a browser check of their own that ``kew verify`` never runs:
#: kew.dev's ``npm run browser-check`` under ``www/`` (the verify script runs
#: only its ``npm run gates``).
UNVERIFIED_BROWSER_ROOTS: tuple[str, ...] = ("www",)


def verify_answers_browser(paths: Iterable[str]) -> bool:
    """Whether a ui-check ``kew verify`` of a diff with these *paths* answers
    every browser check the diff needs (#3421).

    True only when the verify runs ``ui-check`` (:func:`touches_ui_check`) and
    the diff touches no tree whose browser check it never runs
    (:data:`UNVERIFIED_BROWSER_ROOTS`): a review that could not run the browser
    check of a diff touching ``www/`` is not answered by the console's.
    """
    listed = list(paths)
    unverified = any(
        path == root or path.startswith(f"{root}/")
        for path in listed
        for root in UNVERIFIED_BROWSER_ROOTS
    )
    return touches_ui_check(listed) and not unverified


def resolve_candidate(
    repo_root: Path,
    *,
    ref: str = "HEAD",
    base: str = "main",
    base_sha: str | None = None,
) -> VerifyCandidate:
    """Resolve *ref* and its base, and read whether ``frontend/`` moved.

    *base_sha*, when given, is the base itself — the main a caller fetched and
    built *ref* on — instead of whatever ``origin/<base>`` this checkout last
    fetched, which may lag the real main.
    """
    ref_sha = _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    base_ref = (
        _resolve_base_ref(repo_root, base)
        if base_sha is None
        else _git(repo_root, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
    )
    base_sha = _git(repo_root, "merge-base", base_ref, ref_sha)
    # Both sides of every rename, NUL-separated so no path is quoted: what the
    # ``-- frontend`` pathspec this replaced matched (a file moved out of
    # ``frontend/`` still moved the console).
    both_sides = _git(
        repo_root, "diff", "--no-renames", "--name-only", "-z", base_sha, ref_sha
    ).split("\0")
    paths = _git(repo_root, "diff", "--name-only", base_sha, ref_sha).splitlines()
    return VerifyCandidate(
        ref=ref,
        ref_sha=ref_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        frontend_changed=touches_ui_check(both_sides),
        image_inputs_changed=image_inputs_changed(paths),
        changed_paths=tuple(paths),
        ref_tree=_git(repo_root, "rev-parse", f"{ref_sha}^{{tree}}"),
        held_out=_held_out_selection(repo_root, base_sha, ref_sha),
        ref_parents=commit_parents(repo_root, ref_sha),
        held_out_pins=held_out_pins(repo_root, base_sha, ref_sha),
        held_out_config=held_out_config(repo_root, base_sha),
        held_out_python=held_out_python(repo_root, base_sha),
    )


def claim_heads(repo_root: Path, *, ref: str) -> tuple[str, ...]:
    """The PR heads a verify of *ref* may be testing, for its merge claim (#3198).

    *ref* itself and, when it is a merge, both its parents: an operator's ad
    hoc merge (main checked out, the PR merged in) verifies the PR head as its
    second parent, and the drive's merge of main into the head has it first.
    Every reading is claimed rather than one guessed at. A claim is keyed by
    the PR too, so claiming a commit that is not that PR's current head holds
    nothing back, while missing the current head would let a remediation move
    it under the verify.
    """
    ref_sha = _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    parents = commit_parents(repo_root, ref_sha)
    return (ref_sha, *parents) if len(parents) == 2 else (ref_sha,)


def held_out_python(repo_root: Path, base_sha: str) -> str:
    """The Python the base's ``kew/`` asks for: its ``.python-version``, else
    its ``requires-python``, else any Python 3.

    Read here, from the base, never from the head on the host: uv runs the
    file a ``.python-version`` names to learn what it is. Only a version or a
    version range is passed on; anything else asks for any Python 3.
    """
    pinned = _git(
        repo_root, "show", f"{base_sha}:{HELD_OUT_PROJECT}/.python-version", check=False
    )
    request = pinned.splitlines()[0].strip() if pinned else ""
    if not request:
        project = _git(
            repo_root,
            "show",
            f"{base_sha}:{HELD_OUT_PROJECT}/pyproject.toml",
            check=False,
        )
        try:
            declared = tomllib.loads(project).get("project", {}).get("requires-python")
        except tomllib.TOMLDecodeError as exc:
            raise VerifyRefused(
                f"the base's {HELD_OUT_PROJECT}/pyproject.toml does not parse: {exc}"
            ) from exc
        request = declared if isinstance(declared, str) else ""
    return request if request and _PYTHON_REQUEST.fullmatch(request) else "3"


#: Where pytest looks for configuration in a directory, first to last, and
#: the name kew writes each one's text under (``pytest.toml`` is read by its
#: name; any other ``.toml`` is read as a ``pyproject.toml``).
_CONFIG_FILES = (
    ("pytest.toml", ".pytest.toml"),
    (".pytest.toml", ".pytest.toml"),
    ("pytest.ini", ".kew-held-out-base.ini"),
    (".pytest.ini", ".kew-held-out-base.ini"),
    ("pyproject.toml", ".kew-held-out-base.toml"),
    ("tox.ini", ".kew-held-out-base.ini"),
    ("setup.cfg", ".kew-held-out-base.cfg"),
)

#: What marks pytest configuration in each kind of file.
_CONFIG_MARKS = {
    "pyproject.toml": re.compile(r"^\[tool\.pytest(?:\.ini_options)?\]", re.M),
    "tox.ini": re.compile(r"^\[pytest\]", re.M),
    "setup.cfg": re.compile(r"^\[tool:pytest\]", re.M),
}


def held_out_config(repo_root: Path, base_sha: str) -> BaseConfig:
    """The pytest configuration the base's ``kew/`` carries, read here.

    The first file pytest would take as the base's own, in pytest's order:
    the held-out and contract runs are given it by ``-c``, so the head's
    configuration — its ``addopts``, ``python_files``, ``testpaths`` or
    ``pythonpath``, in whatever file — never reaches a run of the base's
    tests. Read from the operator's checkout, as the pins are.
    """
    for source, name in _CONFIG_FILES:
        text = _git(
            repo_root, "show", f"{base_sha}:{HELD_OUT_PROJECT}/{source}", check=False
        )
        mark = _CONFIG_MARKS.get(source)
        if text and (mark is None or mark.search(text)):
            return BaseConfig(name=name, text=f"{text}\n")
    return EMPTY_CONFIG


def held_out_pins(repo_root: Path, base_sha: str, ref_sha: str) -> Pins:
    """The pins the base's ``kew/uv.lock`` sets, refused where the head's moves one.

    Read here, from the operator's own checkout, never on the host: the
    host's copy of the repository is the candidate's to rewrite while the main
    phase runs. A head without the lock moves every pin.
    """
    lock = f"{HELD_OUT_PROJECT}/uv.lock"
    base = _git(repo_root, "show", f"{base_sha}:{lock}", check=False)
    head = _git(repo_root, "show", f"{ref_sha}:{lock}", check=False)
    return pins(base or None, head, lock=lock)


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def commit_parents(repo_root: Path, sha: str) -> tuple[str, ...]:
    """*sha*'s parent commits, first parent first; ``()`` when unreadable.

    Read once, when the candidate is resolved, so the verify event says which
    heads the verified commit was made from. A list git would not give, or one
    that is not all full shas, is recorded as none rather than guessed at.
    """
    try:
        listed = _git(repo_root, "rev-list", "--parents", "-n", "1", sha)
    except VerifyRefused:
        logger.warning("could not read the parents of %s", sha[:12], exc_info=True)
        return ()
    words = listed.lower().split()
    if not words or not all(_FULL_SHA.fullmatch(word) for word in words):
        return ()
    return tuple(words[1:])


def _held_out_selection(
    repo_root: Path, base_sha: str, ref_sha: str
) -> HeldOutSelection:
    """The base test files the held-out phase runs for this candidate."""
    name_status = _git(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        "-M",
        base_sha,
        ref_sha,
        "--",
        HELD_OUT_PROJECT,
    )

    def reader(sha: str) -> Callable[[str], str | None]:
        def read(path: str) -> str | None:
            text = _git(repo_root, "show", f"{sha}:{path}", check=False)
            return text or None

        return read

    return select_held_out(
        name_status,
        project=HELD_OUT_PROJECT,
        tests=HELD_OUT_TESTS,
        read_base=reader(base_sha),
        read_head=reader(ref_sha),
        base_mentions=lambda modules: _base_mentions(repo_root, base_sha, modules),
    )


def _base_mentions(
    repo_root: Path, base_sha: str, modules: tuple[str, ...]
) -> list[str] | None:
    """The base's Python files under the test tree that name any of *modules*
    as a word, or ``None`` when ``git grep`` answered with an error — which
    selects the full tree, never fewer files."""
    patterns = [arg for module in modules for arg in ("-e", module)]
    try:
        completed = subprocess_utils.run(
            ["git", "grep", "-l", "-z", "-w", "-F", *patterns, base_sha, "--"]
            + [f"{HELD_OUT_TESTS}/*.py"],
            cwd=repo_root,
            check=False,
            capture=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerifyRefused(f"git grep could not run: {exc}") from exc
    if completed.returncode not in (0, 1):
        return None
    prefix = f"{base_sha}:"
    return [
        path.removeprefix(prefix)
        for path in (completed.stdout or "").split("\0")
        if path
    ]


def write_bundle(repo_root: Path, candidate: VerifyCandidate, workspace: Path) -> Path:
    """Bundle the candidate with its full history and *itself* as ``HEAD``.

    A bundle without a ``HEAD`` entry clones into an empty working tree, and
    ``git bundle create`` only writes one for a ref it is handed by name — so
    naming the candidate is not enough. The bundle is therefore written from a
    temporary bare repository that reads the project's objects through
    ``objects/info/alternates`` and whose ``HEAD`` is the candidate: the
    operator's repository is only ever read.
    """
    mirror = workspace / "candidate.git"
    _git(workspace, "init", "--quiet", "--bare", str(mirror))
    alternates = mirror / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(f"{_object_store(repo_root)}\n", encoding="utf-8")
    mirror_git = ("--git-dir", str(mirror))
    _git(workspace, *mirror_git, "update-ref", _BUNDLE_REF, candidate.ref_sha)
    _git(workspace, *mirror_git, "symbolic-ref", "HEAD", _BUNDLE_REF)
    bundle = workspace / "repo.bundle"
    _git(workspace, *mirror_git, "bundle", "create", str(bundle), "HEAD", _BUNDLE_REF)
    return bundle


def pinned_images(config: DispatchConfig) -> list[tuple[str, str]]:
    """``(runner, tag)`` for every pinned image ``kew sandbox build`` builds."""
    images = config.sandbox.images
    return [
        (runner, images[runner])
        for runner in BUILDABLE_RUNNERS
        if images.get(runner, "").strip()
    ]


#: The pinned-tool preflight, run on the host before anything expensive. It is
#: the gate's own check, in the gate's own script, in a mode that builds no
#: image and downloads no database — seconds. A host whose trivy is not the
#: pinned one used to fail at the *last* step of ``scripts/verify.sh``, after
#: the whole suite had passed: twelve minutes burned per attempt, every attempt
#: (#2462). The path is relative to ``kew/``, which is where the job script has
#: just ``cd``-ed, exactly as verify.sh's own call to the gate is.
#:
#: Once it passes it prints :data:`PREFLIGHT_PASSED_MARK` (the script runs
#: under ``set -e``, so a refused preflight never reaches the ``echo``): a job
#: whose log lacks it was refused before any gate ran (:func:`gate_ran`).
PREFLIGHT_PASSED_MARK = "KEW_VERIFY_PREFLIGHT passed"
PREFLIGHT_COMMAND = (
    f'bash ../scripts/scan-agent-image.sh --check-tools; echo "{PREFLIGHT_PASSED_MARK}"'
)

#: Where the provisioning recipe installs the headless shell ``ui-check``
#: drives (``PLAYWRIGHT_BROWSERS_PATH`` in ``hosts/provision/ubuntu-24.04.sh``,
#: which ``tests/test_hosts_provision.py`` refuses to let drift from this).
HOST_BROWSERS_PATH = "/opt/playwright-browsers"

#: ``ui-check`` as a merge verify runs it: told where a provisioned host keeps
#: its browser, and told that no browser is a failure. The recipe names that
#: directory only in ``/etc/profile.d``, which the job's non-login shell never
#: sources, so a freshly provisioned host found no Chromium and the runner's
#: own default — print ``skipped: no Chromium``, exit 0 — let a frontend change
#: pass a gate that made none of its checks (#2630). A value the job's shell
#: already has is kept, so an operator verifying locally with their own
#: browser directory is not overridden; the runner also searches the user's
#: Playwright cache after it.
UI_CHECK_COMMAND = (
    f'PLAYWRIGHT_BROWSERS_PATH="${{PLAYWRIGHT_BROWSERS_PATH:-{HOST_BROWSERS_PATH}}}" '
    "KEW_UI_CHECK_REQUIRE_CHROMIUM=1 npm run ui-check"
)


#: What the agent images are built from: a diff that touches none of these
#: cannot change what the image scan finds, so its verification skips the scan.
#: The npm overlays live in the Dockerfiles under ``docker/``.
IMAGE_INPUTS = (
    "docker/",
    "kew/src/kew/_docker/",
    "kew/src/kew/hosts/provision/trivy.pin",
    # Computes the inputs label every image the scan builds carries.
    "kew/src/kew/image_inputs.py",
    "scripts/scan-agent-image.sh",
    "uv.lock",
)

#: How the job tells ``scripts/verify.sh`` to skip the image scan.
IMAGE_SCAN_ENV = "KEW_VERIFY_IMAGE_SCAN"


def image_inputs_changed(paths: Iterable[str]) -> bool:
    """Whether any of *paths* (repo-relative) is an input of the agent images."""
    for path in paths:
        if path.rsplit("/", 1)[-1] == "uv.lock":
            return True
        if any(path.startswith(prefix) for prefix in IMAGE_INPUTS):
            return True
    return False


def _image_lines(images: list[tuple[str, str]]) -> list[str]:
    """Build each pinned image that is missing, on a host that has docker.

    A host without docker builds nothing: the gate's own container checks
    already refuse or skip on that host, and a `docker` that is absent must
    not be reported as a failed image build.
    """
    if not images:
        return []
    return [
        "if command -v docker >/dev/null 2>&1; then",
        *(
            f"    docker image inspect {shlex.quote(tag)} >/dev/null 2>&1 || "
            f"uv run kew sandbox build --runner {shlex.quote(runner)}"
            for runner, tag in images
        ),
        "fi",
    ]


#: What a broken verify environment is recorded as: the gate never judged the
#: candidate, because the virtualenv the suite ran in was missing files.
ENVIRONMENT_BROKEN = "verify_host_environment_broken"

#: The line the probe prints, followed by what it found broken.
ENVIRONMENT_BROKEN_MARK = "KEW_VERIFY_ENV broken:"

#: The exit code a job ends with when its environment is broken.
ENVIRONMENT_BROKEN_EXIT = 70

#: Modules the suite cannot run without, each by the distribution that ships
#: it. ``kew verify`` verifies any project, so the probe imports a module only
#: when the virtualenv has its distribution installed: a project that never
#: installs one has not lost it.
PROBE_MODULES: Mapping[str, str] = MappingProxyType(
    {
        "pytest": "pytest",
        "pydantic": "pydantic",
        "typer": "typer",
        "rich": "rich",
        "jinja2": "jinja2",
        "textual": "textual",
        "kew": "kew-cli",
    }
)

#: Where the probe keeps the ``site-packages`` digest taken after ``uv sync``:
#: inside the virtualenv, outside ``site-packages`` itself.
_PROBE_MANIFEST = ".venv/kew-site-manifest"

#: The probe, run by the virtualenv's own interpreter and never through
#: ``uv run``, which would re-sync and repair what it is meant to find. It
#: imports each :data:`PROBE_MODULES` entry whose distribution the
#: virtualenv's own metadata lists as installed (whether the module imports
#: never decides that), checks every file each installed
#: distribution's ``RECORD`` lists under ``site-packages`` is present, and
#: compares a digest of the ``site-packages`` file list (name and size) with
#: the one ``record`` took. #3171's verify lost files out of its virtualenv
#: while the suite ran and reported 311 test failures; this reports one
#: environment error instead.
_PROBE_SOURCE = r"""
import csv, hashlib, importlib, pathlib, sys, sysconfig
from importlib import metadata
mode, manifest = sys.argv[1], pathlib.Path(sys.argv[2])
site = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
problems = []
for spec in sys.argv[3:]:
    name, _, dist = spec.partition("=")
    try:
        metadata.distribution(dist)
    except metadata.PackageNotFoundError:
        continue
    try:
        importlib.import_module(name)
    except Exception as exc:
        problems.append(f"import {name}: {type(exc).__name__}: {exc}")
for record in sorted(site.glob("*.dist-info/RECORD")):
    with record.open(newline="", errors="replace") as rows:
        for row in csv.reader(rows):
            if not row or not row[0] or row[0].endswith(".pyc"):
                continue
            path = (site / row[0]).resolve()
            if path.is_relative_to(site) and not path.exists():
                problems.append(f"missing {row[0]} ({record.parent.name})")
digest = hashlib.sha256()
for path in sorted(site.rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
        digest.update(f"{path.relative_to(site)}\0{path.stat().st_size}\0".encode())
value = digest.hexdigest()
if mode == "record":
    manifest.write_text(value)
elif not manifest.is_file() or manifest.read_text().strip() != value:
    problems.append("site-packages changed since uv sync")
if problems:
    print("@MARK@", "; ".join(problems[:5]), flush=True)
    sys.exit(@EXIT@)
print("KEW_VERIFY_ENV ok", flush=True)
""".replace("@MARK@", ENVIRONMENT_BROKEN_MARK).replace(
    "@EXIT@", str(ENVIRONMENT_BROKEN_EXIT)
)


def environment_probe_lines(modules: Mapping[str, str] = PROBE_MODULES) -> list[str]:
    """A shell function, ``kew_env_probe record|check``, that probes the venv.

    *modules* maps each module to the distribution that ships it; a module
    whose distribution is not installed is skipped, one whose distribution is
    installed but which fails to import is a broken environment.

    ``record`` runs once right after ``uv sync`` and stores the digest;
    ``check`` runs when the suite fails and compares against it. Either one
    prints :data:`ENVIRONMENT_BROKEN_MARK` and returns
    :data:`ENVIRONMENT_BROKEN_EXIT` when the environment is broken, including
    when there is no virtualenv interpreter at all.
    """
    names = " ".join(
        shlex.quote(f"{module}={distribution}")
        for module, distribution in modules.items()
    )
    return [
        "kew_env_probe() {",
        "    if [ ! -x .venv/bin/python ]; then",
        f'        echo "{ENVIRONMENT_BROKEN_MARK} .venv/bin/python is missing"',
        f"        return {ENVIRONMENT_BROKEN_EXIT}",
        "    fi",
        f"    .venv/bin/python - \"$1\" {_PROBE_MANIFEST} {names} <<'KEW_ENV_PROBE'",
        *_PROBE_SOURCE.strip("\n").splitlines(),
        "KEW_ENV_PROBE",
        "}",
    ]


def environment_record_lines(sync: str = "uv sync") -> list[str]:
    """Sync the virtualenv and record its digest, when there is a project.

    A checkout with no ``pyproject.toml`` has no virtualenv to probe, so it
    syncs and records nothing, and :func:`probed_suite_lines` checks nothing.
    """
    return [
        "kew_env_probed=0",
        "if [ -f pyproject.toml ]; then",
        f"    {sync}",
        "    kew_env_probe record",
        "    kew_env_probed=1",
        "fi",
    ]


#: The shell variable the job keeps the checkout environment's interpreter
#: version in, ``X.Y.Z`` (:func:`main_python_lines`); empty when there is none.
MAIN_PYTHON_VAR = "kew_main_python"


def main_python_lines() -> list[str]:
    """Record the exact version of the interpreter ``uv sync`` built on.

    Run right after :func:`environment_record_lines`, from the synced
    ``.venv``, so the held-out, base-collection and contract runs ask uv for
    the interpreter the main phase runs on (:func:`_python_lines`), never
    another one the base's range also admits. Empty when no project was
    synced.

    The interpreter runs isolated, ``-I -S``, so neither a ``.pth`` file nor
    the environment can print into the answer. A synced environment whose own
    interpreter answers with no version, or with anything but digits and
    dots, is a broken environment (:data:`ENVIRONMENT_BROKEN_MARK`), as one
    with no interpreter is to :func:`environment_probe_lines`: falling back to
    the base's request would run the held-out phase on a Python the suite
    does not run on, and the request is only ever a version.
    """
    version = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    return [
        f'{MAIN_PYTHON_VAR}=""',
        'if [ "$kew_env_probed" = 1 ]; then',
        f'    {MAIN_PYTHON_VAR}="$(.venv/bin/python -I -S -c "{version}")" '
        f'|| {MAIN_PYTHON_VAR}=""',
        f'    case "${MAIN_PYTHON_VAR}" in',
        '        "" | *[!0-9.]*)',
        f'            echo "{ENVIRONMENT_BROKEN_MARK} .venv/bin/python did not '
        'report its version"',
        f"            exit {ENVIRONMENT_BROKEN_EXIT}",
        "            ;;",
        "    esac",
        "fi",
    ]


def probed_suite_lines(command: str) -> list[str]:
    """Probe, run *command*, and probe again if it fails.

    The probe right before the suite catches an environment that broke after
    ``uv sync`` — while images were built, say — before any test runs; the one
    after a failure catches files lost while the suite ran, and reports them
    instead of the test failures they caused.
    """
    return [
        '[ "$kew_env_probed" = 0 ] || kew_env_probe check',
        "verify_status=0",
        f'{command} || verify_status="$?"',
        'if [ "$verify_status" -ne 0 ]; then',
        '    [ "$kew_env_probed" = 0 ] || kew_env_probe check '
        f"|| exit {ENVIRONMENT_BROKEN_EXIT}",
        '    exit "$verify_status"',
        "fi",
    ]


def environment_broken(log: str) -> str | None:
    """What the probe found broken, read off the job's log, or ``None``."""
    for line in log.splitlines():
        if line.startswith(ENVIRONMENT_BROKEN_MARK):
            detail = line[len(ENVIRONMENT_BROKEN_MARK) :].strip()
            return f"{ENVIRONMENT_BROKEN}: {detail}"
    return None


#: What ``scripts/verify.sh`` reads to run only part of its gate (#3212):
#: :data:`STAGE_GATES` runs the static gates and the containment test and
#: stops, :data:`STAGE_SUITE` runs everything after them. Only these exact
#: values split it, so a typo runs the whole gate, and CI sets neither.
STAGE_ENV = "KEW_VERIFY_STAGE"
STAGE_GATES = "gates"
STAGE_SUITE = "suite"

#: The line each phase of a job opens with, so a log names the phase a red
#: stopped in (:func:`stopped_phase`).
PHASE_MARK = "kew-verify: phase"

#: The phases, in the order a verification runs them.
PHASE_GATES = "static-gates"
PHASE_HELD_OUT = "held-out"
PHASE_SUITE = "suite"
PHASE_UI_CHECK = "ui-check"

#: The name the suite job runs under; the gate job runs under :data:`JOB_NAME`.
SUITE_JOB_NAME = f"{JOB_NAME}-{PHASE_SUITE}"


def _phase(name: str) -> str:
    return f'echo "{PHASE_MARK} {name}"'


def stopped_phase(log: str) -> str | None:
    """The last phase *log* opened: the one a red job stopped in."""
    opened = [
        line[len(PHASE_MARK) :].strip()
        for line in log.splitlines()
        if line.startswith(f"{PHASE_MARK} ")
    ]
    return opened[-1] if opened else None


def job_script(candidate: VerifyCandidate, *, images: list[tuple[str, str]]) -> str:
    """The shell the host runs first: the cheap gates, then held-out (#3212).

    ``cd kew`` because the bundle's checkout root is the repository root and
    ``scripts/verify.sh`` runs from ``kew/``.

    The pinned-tool preflight goes first, before any image is built and long
    before the suite runs (:data:`PREFLIGHT_COMMAND`). The virtualenv probe
    records ``site-packages`` after ``uv sync``, checks it again right before
    the gate and once more if the gate fails (:func:`probed_suite_lines`),
    so a broken environment ends the job with :data:`ENVIRONMENT_BROKEN`
    rather than a page of failures.

    Then ``verify.sh``'s static gates and containment test
    (:data:`STAGE_GATES`), then the held-out phase. kew judges this job before
    it starts :func:`suite_job_script`: a red here, or a held-out failure no
    **Behavior changes** line declares, never starts a pytest phase. The
    held-out phase is this job's last: kew reads only the log after the
    phase's own opening line (:func:`held_out_section`), so nothing the
    candidate's code prints may follow it.
    """
    lines = ["set -eu", "umask 022", phase_line(SETUP, "start"), "cd kew"]
    lines += [PREFLIGHT_COMMAND]
    lines += [*environment_probe_lines(), *environment_record_lines()]
    lines += main_python_lines()
    lines += _image_lines(images)
    lines.append(_phase(PHASE_GATES))
    lines += probed_suite_lines(f"{STAGE_ENV}={STAGE_GATES} bash scripts/verify.sh")
    lines.append(_phase(PHASE_HELD_OUT))
    lines.append(phase_line("held_out", "start"))
    lines += held_out_lines(
        candidate, (HELD_OUT_PARALLEL, HELD_OUT_SERIAL), main_python=True
    )
    lines.append(phase_line("held_out", "end"))
    return "\n".join(lines) + "\n"


def suite_job_script(candidate: VerifyCandidate) -> str:
    """The shell the host runs once :func:`job_script` passed: the rest.

    The pytest phases, the image scan, the docs, frontend and www gates
    (:data:`STAGE_SUITE`), ``ui-check`` from ``frontend/`` when the console
    moved, and the red-main phase. It runs in the same checkout, so the
    environment is synced again rather than rebuilt. A selected verify
    (#3202) runs its two pytest phases on the selection and every ``policy``
    test here (:func:`suite_command`).
    """
    lines = ["set -eu", "umask 022", phase_line(SETUP, "start"), "cd kew"]
    lines += [*environment_probe_lines(), *environment_record_lines()]
    lines.append(_phase(PHASE_SUITE))
    lines += selected_run_lines(candidate.suite)
    gate = frontend_gate(candidate, suite_command(candidate.suite))
    lines += probed_suite_lines(gate)
    lines += _ui_check_lines(candidate)
    lines += red_main_check.phase_lines(candidate.red_tests)
    return "\n".join(lines) + "\n"


#: What tells verify.sh that the job runs vitest on the change itself (#3213):
#: verify.sh skips its whole ``npm test -- --run`` and nothing else, and the
#: job runs :data:`VITEST_RELATED` after it. Only this exact value skips it.
FRONTEND_ENV = "KEW_VERIFY_FRONTEND"
FRONTEND_SCOPED = "scoped"

#: vitest's own selection of the test files that import the files named after
#: it. A change whose files no test imports runs none, which is no failure.
VITEST_RELATED = "npx --no-install vitest related --run --passWithNoTests"

#: Makes a scoped ``ui-check`` run both builds anyway, as a full one does:
#: the build and the demo build stay whole (``scripts/ui-check-lib.mjs``).
UI_CHECK_BUILDS_FLAG = "--builds"


#: How the gate command starts verify.sh (:func:`suite_command`).
_VERIFY_SH = "bash scripts/verify.sh"


def frontend_gate(candidate: VerifyCandidate, command: str) -> str:
    """*command* with verify.sh told to skip its full vitest run, for a
    scoped verify only, and only when :func:`_ui_check_lines` runs, which runs
    vitest instead. The variable is set on the suite job's verify.sh command
    line, beside :data:`STAGE_ENV` and ``KEW_VERIFY_SUITE``, never exported:
    nothing after verify.sh (the selected pytest phases, the red-main check)
    sees it, and the gate job (:func:`job_script`) never sets it."""
    if not candidate.frontend.scoped or not candidate.runs_ui_check:
        return command
    told = f"{FRONTEND_ENV}={FRONTEND_SCOPED} {_VERIFY_SH}"
    return command.replace(_VERIFY_SH, told, 1)


def _ui_check_lines(candidate: VerifyCandidate) -> list[str]:
    """``ui-check`` when it runs (:attr:`VerifyCandidate.runs_ui_check`), back
    in ``kew/`` afterwards. A scoped verify runs ``vitest related`` first, on
    the changed files and every file-reading test (``vitest related`` runs a
    test file it is handed), and only the selected modules, with both builds."""
    if not candidate.runs_ui_check:
        return []
    frontend = candidate.frontend
    if not frontend.scoped:
        return [
            _phase(PHASE_UI_CHECK),
            phase_line("ui_check", "start"),
            "cd ../frontend",
            UI_CHECK_COMMAND,
            "cd ../kew",
            phase_line("ui_check", "end"),
        ]
    # The ledger's ui_check phase holds the scoped `vitest related` run too:
    # the job runs it inside its ui-check phase, so its seconds and a failure
    # of it are that phase's.
    lines = [_phase(PHASE_UI_CHECK), phase_line("ui_check", "start")]
    lines.append("cd ../frontend")
    named = dict.fromkeys((*frontend.related, *frontend.always))
    if named:
        files = " ".join(shlex.quote(path) for path in named)
        lines.append(f"{VITEST_RELATED} {files}")
    words = " ".join([UI_CHECK_BUILDS_FLAG, *frontend.modules])
    lines += [f"{UI_CHECK_COMMAND} -- {words}", "cd ../kew"]
    lines.append(phase_line("ui_check", "end"))
    return lines


def ui_check_modules(candidate: VerifyCandidate) -> list[str] | None:
    """The browser modules a scoped ``ui-check`` ran, possibly none, as the
    ``verify_completed`` event's ``ui_check_modules``; ``None`` when ui-check
    ran every module or did not run (``ui_check`` tells which). A scoped run
    of no module made the builds and checked nothing in a browser
    (``drive_merge.browser_checked``)."""
    if not candidate.runs_ui_check or not candidate.frontend.scoped:
        return None
    return list(candidate.frontend.modules)


def switchyard_variables(
    source: Mapping[str, str], *, builds_private_images: bool
) -> dict[str, str]:
    """The private-layer opt-in and the input it needs, or nothing at all.

    The opt-in alone is not an opt-in the gate can honour: with
    ``KEW_SWITCHYARD_IMAGE=1`` and no ``KEW_SWITCHYARD_SOURCE_ARCHIVE``,
    ``scripts/scan-agent-image.sh`` refuses with ``input is missing`` and the
    verification is attested as a *failed* verdict the candidate never earned.
    So the two are forwarded together or not at all: a host holding the input
    runs that layer, and a host without it skips that layer exactly as every
    machine without the input already does.

    Neither reaches a host that cannot build from the private input, whatever
    this machine's environment says — that is what keeps the archive's
    whereabouts on the operator machine.
    """
    if not builds_private_images or source.get(SWITCHYARD_IMAGE_ENV, "") != "1":
        return {}
    archive = source.get(SWITCHYARD_ARCHIVE_ENV, "").strip()
    if not archive:
        return {}
    return {SWITCHYARD_IMAGE_ENV: "1", SWITCHYARD_ARCHIVE_ENV: archive}


def job_environment(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    builds_private_images: bool,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The variables the job declares. No credential is among them."""
    source = os.environ if ambient is None else ambient
    environment = {MEMORY_GUARD_ENV: candidate.base_sha}
    if not candidate.scans_images:
        environment[IMAGE_SCAN_ENV] = "skip"
    environment.update(
        switchyard_variables(source, builds_private_images=builds_private_images)
    )
    if config.verify.deselect:
        environment["PYTEST_ADDOPTS"] = " ".join(
            f"--deselect {shlex.quote(node_id)}" for node_id in config.verify.deselect
        )
    return environment


def build_job(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    provider: HostProvider,
) -> Job:
    """The first job this host runs for this candidate (:func:`job_script`)."""
    ceiling = config.verify.timeout_seconds
    return Job(
        name=JOB_NAME,
        command=job_script(candidate, images=pinned_images(config)),
        environment=job_environment(
            candidate,
            config=config,
            builds_private_images=provider.builds_private_images,
        ),
        timeout_seconds=None if ceiling == 0 else float(ceiling),
    )


def build_suite_job(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    provider: HostProvider,
) -> Job:
    """The job that follows a clean :func:`build_job` (:func:`suite_job_script`)."""
    gate = build_job(candidate, config=config, provider=provider)
    return replace(gate, name=SUITE_JOB_NAME, command=suite_job_script(candidate))


def _release(provider: HostProvider) -> str | None:
    """Release the host, reporting a failure instead of raising it.

    A release that fails is a host problem — an instance that may keep running
    and billing — and never the gate's verdict. Letting it raise is how a
    verification that *passed* gets attested as ``incomplete`` with no exit
    code and no log, so the failure comes back as text to be recorded beside
    the result the job did produce.
    """
    try:
        provider.release()
    except HostError as exc:
        logger.warning("could not release the %s host: %s", provider.name, exc)
        return str(exc)
    return None


def _recorded(*reasons: str | None) -> str | None:
    """The reasons worth recording, in the order they happened."""
    return "; ".join(reason for reason in reasons if reason) or None


def _category(exc: HostError) -> str | None:
    """The category *exc* classifies itself as, if it classifies itself.

    Read off the exception rather than matched against its message: the host
    providers own which of their failures are retryable interruptions
    (:class:`kew.hosts.errors.HostInterrupted`), and a gate re-deriving that
    from wording would disagree with them the first time the wording changed.
    """
    category = getattr(exc, "error_category", None)
    return category if isinstance(category, str) and category else None


def killing_signal(exit_code: int) -> int | None:
    """The signal that killed the job, read off the status kew was handed.

    Two readings, because what either host runs the gate under is a *shell*,
    and a kill reaches kew through it one of two ways. Python reports a shell
    the kernel killed as ``-N``, which is the local host's own ``returncode``.
    A shell reports a *command* the kernel killed as ``128 + N`` in ``$?`` —
    which is what the gcp host's exit marker carries, and what the local
    host's ``sh`` propagates when the OOM killer reaches the gate rather than
    the shell above it. Reading only the first is how a killed gate on the
    remote host came back as a ``failed`` verdict with the signal's own status
    as the CLI's exit code.

    ``None`` for anything else: a status outside the signal range is the
    gate's own, and reading one as a kill would discard a real verdict.
    """
    if exit_code < 0:
        return -exit_code
    if _SHELL_SIGNAL_BASE < exit_code <= _SHELL_SIGNAL_BASE + _MAX_SIGNAL:
        return exit_code - _SHELL_SIGNAL_BASE
    return None


def _completed(result: JobResult, release_error: str | None) -> VerifyResult:
    """The host's answer as a verify result, with a signal read as no answer.

    A killed job is one the operating system stopped: the gate never ran to a
    verdict, so reporting it as ``failed`` with a number no drive script can
    read would attest a judgement nothing made. It is an *incomplete*
    verification, named as one, with the log the job did write kept.
    """
    exit_code: int | None = result.exit_code
    signal = killing_signal(result.exit_code)
    signal_error = None
    if signal is not None:
        signal_error = (
            f"the job was killed by signal {signal} without reporting a status"
        )
        exit_code = None
    broken = environment_broken(result.log) if exit_code else None
    return VerifyResult(
        host=result.host,
        exit_code=exit_code,
        seconds=result.seconds,
        log=result.log,
        error=_recorded(broken, signal_error, release_error),
        error_category=ENVIRONMENT_BROKEN if broken else None,
    )


def _run_on_host(provider: HostProvider, bundle: Path, job: Job) -> VerifyResult:
    """Ship, run and release — one claim over the whole session.

    Bringing the host up is inside the released scope, not above it.
    ``ensure_up()`` is the step that *starts* a machine, so a gcp instance kew
    had just started and then could not reach over ssh ended the verification
    right there: running, billing, no idle stop armed and no release so much
    as attempted, because the release only covered the job. Whatever ends the
    session now — the gate's verdict, a host kew could not bring up, a job it
    could not ship — the host is released once on the way out, and a release
    that fails is recorded beside the reason it was needed
    (:func:`_release`).

    Timed from the outside as well as the inside: a verification that never
    produced an exit code still took time, and recording that as zero would
    read as "it never started" for a job that ran for an hour and timed out.
    """
    started = time.monotonic()
    released: str | None = None
    try:
        with provider.claim():
            try:
                provider.ensure_up()
                provider.ship(bundle)
                result = provider.run(job)
            finally:
                released = _release(provider)
    except HostError as exc:
        # The release is named here too. A job that answered nothing can still
        # have left an instance running and billing, and recording only the
        # job's failure drops the one fact an operator has to act on.
        return VerifyResult(
            host=provider.name,
            exit_code=None,
            seconds=time.monotonic() - started,
            log="",
            error=_recorded(str(exc), released),
            error_category=_category(exc),
        )
    return _completed(result, released)


def _run_staged(
    provider: HostProvider,
    bundle: Path,
    gate: Job,
    suite: Job,
    judge: Callable[[VerifyResult], VerifyResult],
) -> VerifyResult:
    """The gate job, judged, then the suite job only behind a clean one (#3212).

    One claim over the whole session, as in :func:`_run_on_host`, so the suite
    runs in the checkout the gate job synced. *judge* reads the gate job's
    held-out section against the PR body: a red gate, or a held-out failure
    nobody declared, ends the session without starting a pytest phase, and the
    result names the phase it stopped in.
    """
    started = time.monotonic()
    released: str | None = None
    answered: list[VerifyResult] = []
    try:
        with provider.claim():
            try:
                provider.ensure_up()
                provider.ship(bundle)
                answered.append(_judged(provider.run(gate), judge))
                if answered[0].exit_code == 0:
                    answered.append(_completed(provider.run(suite), None))
            finally:
                released = _release(provider)
    except HostError as exc:
        answered.append(
            VerifyResult(
                host=provider.name,
                exit_code=None,
                seconds=time.monotonic() - started,
                log="",
                error=str(exc),
                error_category=_category(exc),
            )
        )
    return _staged_result(answered, released)


def _judged(
    answer: JobResult, judge: Callable[[VerifyResult], VerifyResult]
) -> VerifyResult:
    """The gate job's answer with its held-out section read, and, when it went
    red, the phase it stopped in named."""
    gated = judge(replace(_completed(answer, None), gate_log=answer.log))
    if gated.exit_code in (0, None):
        return gated
    phase = stopped_phase(gated.log) or "the gate job"
    return replace(
        gated,
        error=_recorded(
            gated.error, f"stopped in phase {phase}: no pytest phase was started"
        ),
    )


def _staged_result(answered: list[VerifyResult], released: str | None) -> VerifyResult:
    """The gate job's answer and the suite's, if it ran, as one verification."""
    gate, *rest = answered
    if not rest:
        return replace(gate, error=_recorded(gate.error, released))
    suite = rest[0]
    return replace(
        suite,
        seconds=gate.seconds + suite.seconds,
        log=gate.log + suite.log,
        error=_recorded(gate.error, suite.error, released),
        gate_log=gate.gate_log,
        held_out=gate.held_out,
    )


def _shard_payload(result: VerifyResult) -> dict[str, object]:
    """What a sharded verification adds to the event: every job, named.

    Nothing is summarised away. The acceptance asks for each shard's exit
    code, selected count and seconds, and an attestation that kept only the
    aggregate could not answer which host failed, or which one went silent.
    """
    if not result.shards:
        return {}
    return {
        "shards": sum(1 for report in result.shards if report.job != SINGLE_JOB),
        "collected": result.collected,
        "jobs": [
            {
                "job": report.job,
                "instance": report.instance,
                "exit_code": report.result.exit_code,
                "seconds": round(report.result.seconds, 3),
                "selected": report.selected,
                "executed": report.executed,
                "outcome": report.result.outcome,
                "error": report.result.error,
            }
            for report in result.shards
        ],
    }


def gate_ran(result: VerifyResult) -> bool:
    """Whether the job really ran its gates, so a failure is a verdict.

    Not when the job reached no exit (a kill, an OOM: :func:`killing_signal`),
    when the host's environment broke (:data:`ENVIRONMENT_BROKEN`), or when a
    job refused at the pinned-tool preflight (no :data:`PREFLIGHT_PASSED_MARK`
    in its log). Any other failure — a test, a conftest that will not import,
    the coverage floor, a static gate, the browser check — is a verdict.
    """
    if result.exit_code is None or result.error_category == ENVIRONMENT_BROKEN:
        return False
    # The single-run job runs the preflight; a sharded verify's shard jobs
    # run only their slice of the suite, so one job carrying it is enough.
    logs = [shard.result.log for shard in result.shards] or [result.log]
    return any(PREFLIGHT_PASSED_MARK in log for log in logs)


def _health_payload(
    candidate: VerifyCandidate, result: VerifyResult
) -> dict[str, object]:
    """What main health reads off the event (:mod:`kew.main_health`)."""
    from kew.main_health import failing_tests

    failing = failing_tests(result.log) if result.outcome == "failed" else ()
    return {
        # A selected verify is never main-health evidence (:mod:`kew.main_health`).
        "suite": candidate.suite.mode,
        "suite_selection": candidate.suite.payload(),
        "kind": candidate.kind,
        "ref_tree": candidate.ref_tree,
        "failing_tests": list(failing),
        "gate_ran": gate_ran(result),
        "image_scan": {
            "ran": candidate.scans_images,
            "reason": candidate.image_scan_reason,
        },
        "main_health": None if result.main_red is None else result.main_red.payload(),
        "red_tests": result.red_outcomes,
    }


def record_verify_event(
    store: RunStore | None,
    candidate: VerifyCandidate,
    result: VerifyResult,
    *,
    config: DispatchConfig,
    strict: bool = False,
    claim: ClaimWatch | None = None,
    pr_body: str | None = None,
    pr: int | None = None,
) -> int | None:
    """Record what was verified and return its durable row identifier.

    A failed or incomplete verification records the same event: the point of
    the attestation is that the gate ran at all, and a run only recorded when
    it passed would make the absence of a row unreadable.

    *claim* is the merge claim the verify held on a PR head (#3198); the event
    names it, and whether it lapsed before the verify finished — a verify whose
    claim lapsed may have tested a head the drive has since moved.

    The event carries what the verify ledger reads (:mod:`kew.verify_ledger`):
    each phase's seconds and outcome, the failing phase and its failing ids,
    a digest of *pr_body*'s **Behavior changes** section (``None`` without a
    body), and the path the log is kept at.

    *pr* is the PR the verify was for when it held no claim — the drive's
    merge pass — so the ledger pairs the PR's verifies across its heads.
    """
    payload: dict[str, object] = {
        "ref": candidate.ref,
        "ref_sha": candidate.ref_sha,
        "ref_parents": list(candidate.ref_parents),
        "base_ref": candidate.base_ref,
        "base_sha": candidate.base_sha,
        "host": result.host,
        "exit_code": result.exit_code,
        "seconds": round(result.seconds, 3),
        "outcome": result.outcome,
        "frontend_changed": candidate.frontend_changed,
        "ui_check": candidate.runs_ui_check,
        "ui_check_modules": ui_check_modules(candidate),
        "frontend_selection": candidate.frontend.payload(),
        "error": result.error,
        "error_category": result.error_category,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    payload.update(_health_payload(candidate, result))
    payload.update(_shard_payload(result))
    payload["held_out"] = _held_out_payload(result.held_out)
    # Selected is what the ledger bound; executed is what the job's own
    # contract run reported. A gate that failed first selected one and ran none.
    payload["contract_selected"] = (
        None if candidate.contract is None else candidate.contract.path
    )
    payload["contract_executed"] = contract_executed(candidate, result)
    payload["held_out_selection"] = {
        "mode": candidate.held_out.mode,
        "files": len(candidate.held_out.files),
    }
    payload["mutation"] = _mutation_payload(result.mutation)
    payload.update(ledger_payload(candidate, result, pr_body=pr_body))
    payload["pr"] = pr if claim is None else claim.pr
    payload["log_path"] = write_log(store, candidate.ref_sha, result.log)
    if claim is not None:
        payload["merge_claim"] = {
            "pr": claim.pr,
            "heads": list(claim.heads),
            "lapsed": claim.lapsed(),
        }
    return emit(
        store,
        VERIFY_EVENT,
        actor=resolve_actor(),
        repo=config.project.repo or None,
        payload=payload,
        strict=strict,
    )


def _with_verify_receipt(
    store: RunStore | None,
    candidate: VerifyCandidate,
    result: VerifyResult,
    *,
    config: DispatchConfig,
    required: bool,
    claim: ClaimWatch | None = None,
    pr_body: str | None = None,
    pr: int | None = None,
) -> VerifyResult:
    """Attach the audit row identifier, or refuse a required missing write."""
    try:
        receipt = record_verify_event(
            store,
            candidate,
            result,
            config=config,
            strict=required,
            claim=claim,
            pr_body=pr_body,
            pr=pr,
        )
    except RuntimeError as exc:
        raise VerifyRefused(f"verification produced no durable receipt: {exc}") from exc
    if required and receipt is None:
        raise VerifyRefused("verification produced no durable receipt")
    return replace(result, receipt=receipt)


def _sweep_work_root(store: RunStore | None, project: Path) -> None:
    """Let go of work trees past retention that no live row references.

    Run before each verification, which makes a tree of its own; a sweep
    that fails is logged and never refuses the verification.
    """
    try:
        removed = work_dirs.sweep_work_root(store, project=project)
    except OSError as exc:
        logger.warning("work root sweep failed: %s", exc)
        return
    if removed:
        logger.info("removed %d expired work tree(s)", len(removed))


def run_verify(
    config: DispatchConfig,
    *,
    store: RunStore | None,
    ref: str = "HEAD",
    host: str | None = None,
    shards: int = 1,
    repo_root: Path | None = None,
    pr_body: str | None = None,
    contract: AcceptanceContractRow | None = None,
    base_sha: str | None = None,
    contracts: Sequence[AcceptanceContractRow] = (),
    require_receipt: bool = False,
    kind: str = "pr",
    merge_claim: ClaimWatch | None = None,
    force_full: bool = False,
    now: datetime | None = None,
    pr: int | None = None,
    gh: Callable[..., subprocess.CompletedProcess[str]] = subprocess_utils.run,
) -> tuple[VerifyCandidate, VerifyResult]:
    """Verify *ref* on the named host, record the event, and report the result.

    *host* defaults to ``[verify] host``. *shards* above one runs the parallel
    pytest phase as that many jobs on that many hosts, with everything that
    runs once on the first of them (:func:`single_job_script`); the pool is
    ``[hosts.gcp] instances`` and a pool too short for the shards asked for is
    refused before any job.

    The event is written whatever the hosts answer; only a refusal before the
    jobs — an unknown ref, an unconfigured gcp instance, a bundle kew could
    not write — raises :class:`VerifyRefused`, because then there is nothing
    to attest.

    *pr_body* is the PR description whose **Behavior changes** section may
    excuse a held-out base test failure by node id; without one, none is
    excused (:func:`apply_held_out`).

    *contract* is the node's current acceptance contract from the ledger; its
    file joins the held-out tree as the contract commit carries it, and a
    contract test failure is never excused (:func:`read_contract`).
    *contracts* adds more, each run the same way: a merge-queue train's, one
    per PR it carries.

    *base_sha* pins the base the held-out phase and the event read, for a
    caller that fetched main itself (:func:`resolve_candidate`).

    *require_receipt* makes the event a required write. A merge gate refuses
    unless the ledger returns the durable audit row identifier.

    *kind* is ``pr`` for a candidate and ``nightly`` for the nightly's verify
    of main (:mod:`kew.main_health`). A PR verification reads main's health
    first: while main is red, its verdict names the red commit and its issue,
    and a failure is refused because of it (:meth:`VerifyResult.main_red_refusal`).

    *merge_claim* is the claim ``kew verify --pr`` holds on the PR head while
    this runs (#3198); the event records it and whether it lapsed.

    With ``[verify] suite = "tiered"`` the two pytest phases may run the
    change's selection instead of the full suite (:mod:`kew.verify_suite`);
    *force_full* (``kew verify --full``) runs the full suite whatever it says.
    *now* is the clock the selection's 26-hour window reads.

    *pr* is the PR the drive's merge pass verifies for, which holds no
    claim; the event records it so the PR's verifies pair across its heads
    (:mod:`kew.verify_ledger`). Once the event is recorded, the PR's previous
    red verify is resolved, and a flake files its ``flaky`` issues with *gh*
    — only when the verify is bound to a PR (*pr* or *merge_claim*); nothing
    that does can change this verify's result.
    """
    root = Path.cwd() if repo_root is None else Path(repo_root)
    candidate = resolve_candidate(
        root, ref=ref, base=config.branches.base or "main", base_sha=base_sha
    )
    candidate = replace(candidate, kind=kind)
    held = read_contracts(root, ([] if contract is None else [contract]) + [*contracts])
    if held:
        candidate = replace(candidate, contract=held[0], contracts=held[1:])
    try:
        providers = provider_pool(
            config, provider=host or config.verify.host, count=shards
        )
    except HostError as exc:
        raise VerifyRefused(str(exc)) from exc
    logger.info(
        "verifying %s (base %s) on %d %s host(s)",
        candidate.ref_sha[:12],
        candidate.base_sha[:12],
        len(providers),
        providers[0].name,
    )
    with contextlib.ExitStack() as leased:
        _take_local_verify_lease(leased, candidate, providers[0].name)
        # Main's health is read once the verify is sure to run: a verify the
        # lease refuses reads nothing.
        main_red = _red_main(store, root, candidate)
        request = _suite_request(
            candidate, root=root, shards=len(providers), force_full=force_full
        )
        candidate = replace(
            candidate,
            red_tests=_red_tests(main_red),
            suite=choose_suite(config.verify.suite, request, store=store, now=now),
            frontend=choose_frontend(
                config.verify.frontend, request, store=store, now=now
            ),
        )
        logger.info(
            "verifying %s: %s; %s",
            candidate.ref_sha[:12],
            candidate.suite.describe(),
            candidate.frontend.describe(),
        )
        result, refused = _verify_on_hosts(
            candidate,
            config=config,
            store=store,
            root=root,
            providers=providers,
            pr_body=pr_body,
            merge_claim=merge_claim,
            pr=pr,
        )
    if refused:
        return candidate, result
    result = replace(
        result, main_red=main_red, red_outcomes=_red_outcomes(candidate, result)
    )
    result = _with_verify_receipt(
        store,
        candidate,
        result,
        config=config,
        required=require_receipt,
        claim=merge_claim,
        pr_body=pr_body,
        pr=pr,
    )
    # The PR's previous verify, if red, is resolved now that this one is
    # recorded (:mod:`kew.verify_ledger`); a flake files its flaky issues.
    resolve_after(
        store, result.receipt, repo_root=root, repo=config.project.repo, gh=gh
    )
    return candidate, result


def held_out_job_script(candidate: VerifyCandidate) -> str:
    """The held-out phase alone, for a candidate before review (#3212).

    The same runs the gate job makes (:func:`held_out_lines`), with nothing
    of the candidate's own run first: the runs sync their own environment
    from the lock, outside the checkout. The contract is left to the merge.
    """
    lines = ["set -eu", "umask 022", "cd kew", _phase(PHASE_HELD_OUT)]
    lines += held_out_lines(
        candidate, (HELD_OUT_PARALLEL, HELD_OUT_SERIAL), run_contract=False
    )
    return "\n".join(lines) + "\n"


#: Why a held-out check before review never runs on the ``local`` host: it
#: imports the head's ``src`` before any person or reviewer read it, and this
#: machine's shell has the operator's credentials in reach.
HELD_OUT_CHECK_LOCAL_REFUSAL = (
    "the pre-review held-out check never runs head code on this machine; "
    "set [verify] host = gcp"
)


def held_out_check_candidate(
    repo_root: Path, *, ref: str, base_sha: str, only: Sequence[str] = ()
) -> VerifyCandidate:
    """The candidate a held-out check before review runs on (#3212).

    *ref* is a commit in *repo_root* built on *base_sha*. With *only*, the
    runs cover just those base node ids instead of the change's own selection:
    the control run on the base, which asks whether a failure is the head's.
    """
    candidate = resolve_candidate(repo_root, ref=ref, base_sha=base_sha)
    if not only:
        return candidate
    files = tuple(f"{HELD_OUT_PROJECT}/{held_out_node_id(node)}" for node in only)
    return replace(candidate, held_out=HeldOutSelection(SELECTED, files))


def run_held_out_check(
    config: DispatchConfig,
    *,
    repo_root: Path,
    candidate: VerifyCandidate,
    pr_body: str | None,
) -> HeldOutReport:
    """Run the held-out phase on *candidate* on the verify host, and judge it.

    *candidate* comes from :func:`held_out_check_candidate`: the pre-review
    step's throwaway merge of a head with the current base, or the base itself
    for the control run. The verdict is the merge verify's own
    (:func:`held_out_report`) against *pr_body*'s **Behavior changes**
    section, so a failure that is undeclared here is undeclared at the merge.
    Nothing is recorded: this run attests no merge. The job runs under
    ``[verify] held_out_before_review_timeout_seconds``, not the merge
    verify's ceiling.

    Raises :class:`VerifyRefused` on the ``local`` host
    (:data:`HELD_OUT_CHECK_LOCAL_REFUSAL`), and :class:`~kew.hosts.HostError`
    when the host was busy, could not be reached or outran the ceiling
    (:class:`~kew.hosts.HostTimeout`): none of those is an answer.
    """
    provider = provider_pool(config, provider=config.verify.host, count=1)[0]
    if provider.name == "local":
        raise VerifyRefused(HELD_OUT_CHECK_LOCAL_REFUSAL)
    job = replace(
        build_job(candidate, config=config, provider=provider),
        name=f"{JOB_NAME}-{PHASE_HELD_OUT}",
        command=held_out_job_script(candidate),
        timeout_seconds=float(config.verify.held_out_before_review_timeout_seconds),
    )
    with work_dirs.work_tempdir("kew-held-out-") as workspace:
        bundle = write_bundle(repo_root, candidate, Path(workspace))
        result = _completed(_held_out_session(provider, bundle, job), None)
    section = held_out_section(result.log) if result.exit_code == 0 else ""
    report = held_out_report(
        section, runs=HELD_OUT_RUNS, declared=declared_behavior_changes(pr_body)
    )
    if result.exit_code != 0 and report.outcome == "not_run":
        detail = result.error or f"the job exited {result.exit_code}"
        return replace(report, error=f"{report.error}: {detail}")
    return report


def _held_out_session(provider: HostProvider, bundle: Path, job: Job) -> JobResult:
    """Ship and run one held-out job under one claim, and release the host.

    Unlike :func:`_run_on_host`, a host that is busy, unreachable or outran
    the ceiling raises (:class:`~kew.hosts.HostError`): the caller must tell
    "the host did not answer" from "the head's tests did not report". Only
    the job outrunning its own ceiling raises :class:`~kew.hosts.HostTimeout`:
    a host that took too long to start or to take the bundle is a host that
    did not answer, and says so.
    """
    with provider.claim():
        try:
            try:
                provider.ensure_up()
                provider.ship(bundle)
            except HostTimeout as exc:
                raise HostError(
                    f"the verify host did not come up or take the bundle in time, "
                    f"so the job never started: {exc}"
                ) from exc
            return provider.run(job)
        finally:
            released = _release(provider)
            if released:
                logger.warning("held-out check: %s", released)


def red_on_main(store: RunStore | None, repo_root: Path, base: str) -> tuple[str, ...]:
    """The test ids main's health lists as failing while main is red (T2).

    A health that cannot be read is logged and lists nothing: the pre-review
    held-out check still runs its own control run on the base.
    """
    from kew.main_health import MainHealthError, read_main_health

    try:
        base_ref = _resolve_base_ref(repo_root, base)
        health = read_main_health(store, repo_root, base_ref=base_ref)
    except (MainHealthError, VerifyRefused) as exc:
        logger.warning("could not read main's health for the held-out check: %s", exc)
        return ()
    return health.tests if health.red else ()


def _suite_request(
    candidate: VerifyCandidate, *, root: Path, shards: int, force_full: bool
) -> SuiteRequest:
    """What :func:`choose_suite` and :func:`choose_frontend` read of *candidate*."""
    return SuiteRequest(
        repo_root=root,
        base_ref=candidate.base_ref,
        base_sha=candidate.base_sha,
        ref_sha=candidate.ref_sha,
        kind=candidate.kind,
        shards=shards,
        force_full=force_full,
    )


def _take_local_verify_lease(
    leased: contextlib.ExitStack[bool | None], candidate: VerifyCandidate, host: str
) -> None:
    """Hold the verify lease for a local job's whole run (#3190), so the drive
    launches no new agent work beside it; a gcp job loads no machine kew
    launches on, and takes none.

    A lease another local verify holds refuses this one before any job, at
    once: :class:`VerifyLeaseRefused`, which a hand ``kew verify`` reports as
    a verification that never ran and the drive's merge pass reads as
    contention, held without a hold row, and retries next round. Never a
    wait: the drive runs its verifies in its own process, and a wait behind a
    stuck verify would freeze it.
    """
    if host != "local":
        return
    try:
        leased.enter_context(
            hold_verify_lease(f"{candidate.ref} ({candidate.ref_sha[:12]})")
        )
    except VerifyLeaseBusy as exc:
        raise VerifyLeaseRefused(str(exc)) from exc


def _verify_on_hosts(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    store: RunStore | None,
    root: Path,
    providers: list[HostProvider],
    pr_body: str | None,
    merge_claim: ClaimWatch | None,
    pr: int | None = None,
) -> tuple[VerifyResult, bool]:
    """Run the jobs and the held-out phase; True says the hosts refused before
    any work, and that refusal is already recorded — with the PR and its body,
    so the PR's next verify resolves it (:mod:`kew.verify_ledger`)."""
    _sweep_work_root(store, root)
    try:
        record_lapsed_leases(store, providers, config=config)
    except HostError as exc:
        # Refused before any host work: nothing is leased, nothing ships.
        result = VerifyResult(
            host=providers[0].name, exit_code=None, seconds=0.0, log="", error=str(exc)
        )
        record_verify_event(
            store,
            candidate,
            result,
            config=config,
            claim=merge_claim,
            pr_body=pr_body,
            pr=pr,
        )
        return result, True
    judge = partial(
        apply_held_out,
        runs=HELD_OUT_RUNS,
        declared=declared_behavior_changes(pr_body),
        contracts=_contract_nodes(candidate),
    )
    with work_dirs.work_tempdir("kew-verify-") as workspace:
        bundle = write_bundle(root, candidate, Path(workspace))
        result = _staged_on_hosts(
            candidate, config=config, providers=providers, bundle=bundle, judge=judge
        )
    return replace(judge(result), mutation=DEFERRED), False


def _staged_on_hosts(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    providers: list[HostProvider],
    bundle: Path,
    judge: Callable[[VerifyResult], VerifyResult],
) -> VerifyResult:
    """The gate job on the first host, then the suite: there, or on the pool.

    A sharded verification ships to every host at once, and each holds its
    shard until the first host's gate job has been judged clean
    (:class:`GateOpening`), so a red gate or an undeclared held-out failure
    starts no shard at all.
    """
    first = providers[0]
    gate = build_job(candidate, config=config, provider=first)
    if len(providers) == 1:
        suite = build_suite_job(candidate, config=config, provider=first)
        return _run_staged(first, bundle, gate, suite, judge)
    return run_sharded(
        candidate,
        config=config,
        providers=providers,
        bundle=bundle,
        opening=GateOpening(gate, judge),
    )


def _red_outcomes(
    candidate: VerifyCandidate, result: VerifyResult
) -> dict[str, str] | None:
    """What the red-main phase found, read job by job (:mod:`kew.red_main_check`)."""
    logs = [shard.result.log for shard in result.shards] or [result.log]
    return red_main_check.outcomes(logs, candidate.red_tests)


def _red_tests(main_red: Any) -> tuple[str, ...]:
    """The failing ids a PR verification runs again while main is red.

    Only a red that reproduced alone can be cleared by a landing, so only its
    ids are worth the run.
    """
    from kew.main_health import REPRODUCED

    if main_red is None or main_red.reproduced != REPRODUCED:
        return ()
    return tuple(main_red.tests)


def _red_main(
    store: RunStore | None, repo_root: Path, candidate: VerifyCandidate
) -> Any:
    """Main's health while it is red, for a PR verification; else ``None``.

    A health that cannot be read is logged and blocks nothing: the
    verification still runs the full suite.
    """
    from kew.main_health import PR_KIND, MainHealthError, read_main_health

    if candidate.kind != PR_KIND:
        return None
    try:
        health = read_main_health(store, repo_root, base_ref=candidate.base_ref)
    except MainHealthError as exc:
        logger.warning("could not read main's health: %s", exc)
        return None
    return health if health.red else None


def run_host_job(
    config: DispatchConfig,
    *,
    store: RunStore | None,
    repo_root: Path,
    sha: str,
    name: str,
    script: str,
    timeout_seconds: float,
    ambient: Mapping[str, str] | None = None,
) -> VerifyResult:
    """Run one *script* at commit *sha* on ``[verify] host``: ship, run, release.

    What the nightly's bisect probes and image scan run (:mod:`kew.nightly`).
    Nothing is recorded here; the caller records what it reads off the result.
    The job carries the private-layer opt-in a merge verify would forward to
    this host (:func:`switchyard_variables`): PR verifies leave the image scan
    to the nightly, so the nightly's scan must cover that layer too.
    """
    candidate = VerifyCandidate(
        ref=sha, ref_sha=sha, base_ref=sha, base_sha=sha, frontend_changed=False
    )
    try:
        (provider,) = provider_pool(config, provider=config.verify.host, count=1)
        record_lapsed_leases(store, [provider], config=config)
    except HostError as exc:
        raise VerifyRefused(str(exc)) from exc
    job = Job(
        name=name,
        command=script,
        environment=switchyard_variables(
            os.environ if ambient is None else ambient,
            builds_private_images=provider.builds_private_images,
        ),
        timeout_seconds=timeout_seconds,
    )
    with work_dirs.work_tempdir(f"kew-{name}-") as workspace:
        bundle = write_bundle(repo_root, candidate, Path(workspace))
        return _run_on_host(provider, bundle, job)


@contextmanager
def terminate_unwinds() -> Iterator[None]:
    """Turn ``SIGTERM`` into an exit that unwinds, for as long as this is held.

    Python's default for ``SIGTERM`` ends the process where it stands, so the
    ``finally`` that releases a host — and drops its lease — never ran, and a
    verify killed mid-job left the instance leased to a dead process for
    hours (#2811). ``SIGINT`` already unwinds as ``KeyboardInterrupt``. Only
    the main thread can install a handler; anywhere else this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _exit)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@dataclass(frozen=True)
class ContractBinding:
    """Which node a verification answers to, and that node's current contract."""

    #: The work item the candidate is bound to; ``None`` when nothing binds it.
    item_id: str | None = None

    #: The epic the candidate's node belongs to; ``None`` when the item has no
    #: contract anywhere and no single epic lists it.
    epic_ref: str | None = None

    #: The node's current contract; ``None`` when the node has none.
    contract: AcceptanceContractRow | None = None

    def describe(self) -> str:
        """One line naming the contract that runs, or why none does."""
        if self.item_id is None:
            return "acceptance contract: none (no node is bound to this ref)"
        row = self.contract
        if row is None:
            node = f"item {self.item_id}"
            if self.epic_ref is not None:
                node = f"epic {self.epic_ref}, {node}"
            return f"acceptance contract: none ({node} has none)"
        return (
            f"acceptance contract: {row.path} v{row.version} "
            f"(epic {row.epic_ref}, item {row.item_id})"
        )


#: The ref prefixes a branch name is read off, local before remote.
_BRANCH_PREFIXES = ("refs/heads/", "refs/remotes/origin/")


def _bound_item(store: RunStore, repo_root: Path, ref: str) -> str | None:
    """The item whose run opened the branch *ref* names, if it names one."""
    full = _git(repo_root, "rev-parse", "--symbolic-full-name", ref, check=False)
    for prefix in _BRANCH_PREFIXES:
        if full.startswith(prefix):
            run = store.find_run_by_branch(full.removeprefix(prefix))
            if run is None:
                return None
            return run.item_id or (str(run.issue_num) if run.issue_num else None)
    return None


def _member_epics(store: RunStore, item: str) -> tuple[str, ...]:
    """The epics whose newest journaled tick lists *item* as a node of its own.

    Another epic's boundary copy of the node is ``external`` and does not count.
    """
    return tuple(
        sorted(
            epic
            for epic, tick in store.latest_epic_ticks().items()
            if any(
                isinstance(node, dict)
                and str(node.get("id")) == item
                and not node.get("external")
                for node in tick.snapshot.get("nodes") or ()
            )
        )
    )


def _candidate_epic(
    item: str,
    epic_ref: str | None,
    members: tuple[str, ...],
    contracted: Collection[str],
) -> str | None:
    """The epic the candidate's node belongs to, or a refusal.

    An explicit *epic_ref* must be one the ledger ties to *item*, by a node or
    a contract: a typo would otherwise bind the candidate to an epic with no
    contract and skip the real one. Without it, exactly one epic listing the
    item settles it; when none or several do and any epic holds a contract for
    the item, the candidate's epic cannot be told and the verify is refused.
    """
    if epic_ref is not None:
        if epic_ref not in members and epic_ref not in contracted:
            raise VerifyRefused(
                f"epic {epic_ref} has no node and no acceptance contract for "
                f"item {item}"
            )
        return epic_ref
    if len(members) == 1:
        return members[0]
    if not contracted:
        return None
    listed = ", ".join(members) if members else "no epic lists it"
    raise VerifyRefused(
        f"item {item} has an acceptance contract ({', '.join(sorted(contracted))}) "
        f"but its epic cannot be established ({listed}); name the epic with --epic"
    )


def resolve_contract(
    store: RunStore | None,
    repo_root: Path,
    *,
    ref: str = "HEAD",
    item_id: str | None = None,
    epic_ref: str | None = None,
) -> ContractBinding:
    """The candidate's node and its current acceptance contract, from the ledger.

    *item_id* binds the candidate explicitly; without it the branch *ref*
    names is joined back to the run that opened it. The node's epic is then
    established first (:func:`_candidate_epic`), and only that epic's contract
    runs: never another epic's, and never one picked out of several.
    """
    if store is None:
        return ContractBinding()
    item = item_id or _bound_item(store, repo_root, ref)
    if item is None:
        return ContractBinding()
    contracts = {
        row.epic_ref: row for row in store.current_acceptance_contracts_for_item(item)
    }
    epic = _candidate_epic(item, epic_ref, _member_epics(store, item), contracts)
    contract = None if epic is None else contracts.get(epic)
    return ContractBinding(item, epic, contract)


def contract_executed(candidate: VerifyCandidate, result: VerifyResult) -> bool:
    """Whether the candidate's contract run reported, by its own exit marker.

    A gate that failed before the held-out phase, or a host that never started
    the job, selected a contract and ran none. The marker counts only inside
    the held-out phase's own section (:func:`held_out_sections`).
    """
    return candidate.contract is not None and bool(
        _CONTRACT_EXIT.search(held_out_sections(result))
    )


def _contract_summary(candidate: VerifyCandidate, result: VerifyResult) -> str:
    """The contract's part of :func:`summary_line`: run, selected only, or none."""
    if candidate.contract is None:
        return " contract=none"
    key = "contract" if contract_executed(candidate, result) else "contract_not_run"
    return f" {key}={candidate.contract.path}"


def summary_line(candidate: VerifyCandidate, result: VerifyResult) -> str:
    """One line an operator (or a drive script) can read at a glance."""
    exit_code = "none" if result.exit_code is None else str(result.exit_code)
    ui_check = "true" if candidate.runs_ui_check else "false"
    shards = sum(1 for report in result.shards if report.job != SINGLE_JOB)
    sharding = "" if not shards else f" shards={shards} collected={result.collected}"
    held_out = "" if result.held_out is None else _held_out_summary(result.held_out)
    contract = _contract_summary(candidate, result)
    mutation = "" if result.mutation is None else _mutation_summary(result.mutation)
    # The new fields sit before ``mutation=``: readers take the line's end as
    # the mutation verdict.
    images = "" if candidate.scans_images else " image_scan=skipped"
    red = "" if result.main_red is None else f" main=red@{result.main_red.sha[:12]}"
    suite = _suite_summary(candidate.suite)
    return (
        f"{result.outcome}: host={result.host} ref={candidate.ref_sha[:12]} "
        f"base={candidate.base_sha[:12]} exit={exit_code} "
        f"seconds={result.seconds:.1f} ui_check={ui_check}{sharding}{held_out}"
        f"{contract}{images}{red}{suite}{_frontend_summary(candidate.frontend)}"
        f"{mutation}"
    )


def _frontend_summary(frontend: FrontendSelection) -> str:
    """``frontend=scoped modules=N`` for a scoped verify; one that fell back
    says ``frontend=full``; the default says nothing."""
    if frontend.scoped:
        return f" frontend=scoped modules={len(frontend.modules)}"
    return " frontend=full" if frontend.scoped_setting else ""


def _suite_summary(suite: SuiteSelection) -> str:
    """``suite=selected files=N`` for a selected verify; a tiered verify that
    fell back says ``suite=full``; the default says nothing."""
    if suite.selected:
        return f" suite=selected files={len(suite.files)}"
    return " suite=full" if suite.tiered else ""


# --- Sharded verification (N3) ----------------------------------------------
# The parallel pytest phase is the verify's wall clock on one VM, and #2344
# made it splittable: `--shard=K/N` deselects all but the K-th of N balanced
# groups. So a sharded verify is N jobs of that phase on N hosts, plus one job
# for everything that must run exactly once — the static gates, the serial
# phase and `ui-check`, on the first host of the pool, beside its own shard.
#
# Two rules make the split safe to attest, and both are checked here rather
# than trusted:
#
# * A shard that does not report is a **failed** verification, never a skipped
#   one. Nothing is inferred from a host's silence.
# * The shards' selected counts must sum to the unsharded selection's count,
#   which the first host measures with a collect-only pass. A partition that
#   can drop a test would otherwise go green having never run it, which is
#   worse than not sharding at all.
# * Every test a shard collected must be a test its run reported. Collection
#   and the run are two pytest processes, so a count of the first says nothing
#   about the second: a conftest hook can keep a test at --collect-only and
#   deselect it at run time (#2629). The run writes junit, and the shard checks
#   it against its collect log (:mod:`kew.verify_executed`) and reports the
#   number executed, which kew holds to the number selected.


#: The job name the single-run phases run under, as it appears in a report.
SINGLE_JOB = "single"

#: What verify.sh reads to learn that the two whole-suite pytest phases are
#: running somewhere else. Only this exact value skips them, so a typo runs
#: the whole gate rather than silently dropping the suite.
SUITE_ENV = "KEW_VERIFY_SUITE"
SUITE_ELSEWHERE = "elsewhere"

#: What tells verify.sh that the job runs the two phases on a selection
#: instead (#3202): verify.sh skips them, and the coverage floor with them,
#: and the job runs :func:`selected_run_lines` itself, with the file list kew
#: computed. Like :data:`SUITE_ELSEWHERE`, only this exact value skips them.
SUITE_SELECTED = "selected"

#: The shell function a selected verify runs its pytest phases through.
_SELECTED_RUN = "kew_selected_run"


def selected_run_lines(suite: SuiteSelection) -> list[str]:
    """Define :data:`_SELECTED_RUN` for a selected verify; nothing otherwise.

    One selected phase may hold no test at all — selected files with no
    serial test, say — and pytest exits 5 for that, which is no failure. Any
    other status is the phase's own. ``KEW_REQUIRE_TOOLS=1`` is verify.sh's
    own no-silent-skips switch (#2480), which a run outside it must set too.
    """
    if not suite.selected:
        return []
    return [
        f"{_SELECTED_RUN}() {{",
        "    kew_selected_status=0",
        '    KEW_REQUIRE_TOOLS=1 uv run pytest "$@" || kew_selected_status="$?"',
        '    [ "$kew_selected_status" -eq 5 ] || return "$kew_selected_status"',
        "}",
        _worker_cap(),
    ]


def suite_command(suite: SuiteSelection) -> str:
    """The gate the suite job runs: verify.sh's suite stage (:data:`STAGE_SUITE`),
    and for a selected verify, the two phases on the selected files and every
    ``policy`` test after it."""
    stage = f"{STAGE_ENV}={STAGE_SUITE}"
    if not suite.selected:
        return f"{stage} bash scripts/verify.sh"
    steps = [f"{stage} {SUITE_ENV}={SUITE_SELECTED} bash scripts/verify.sh"]
    if suite.files:
        files = " ".join(shlex.quote(path) for path in suite.files)
        steps += [
            phase_line("xdist", "start"),
            f'{_SELECTED_RUN} -n auto -m "not serial" {files}',
            phase_line("xdist", "end"),
            phase_line("serial", "start"),
            f"{_SELECTED_RUN} -m serial {files}",
            phase_line("serial", "end"),
        ]
    steps += [
        phase_line("policy", "start"),
        "KEW_REQUIRE_TOOLS=1 uv run pytest -m policy",
        phase_line("policy", "end"),
    ]
    return " && ".join(steps)


#: The xdist worker cap a shard runs under — ci.yml's and verify.sh's own
#: number, so a shard is the same parallel phase as the one it is a slice of
#: and an N=1 reading is comparable with an N=3 one. Pinned against both in
#: ``tests/test_verify_shards.py``; raise it in all three places or none.
XDIST_WORKERS_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
XDIST_WORKERS = 6

#: Where a job writes its collect-only pass's output: beside the checkout, not
#: in it, and never on stdout — the pass prints one line per selected test, so
#: the log that comes back would otherwise be megabytes of node ids.
_COLLECT_LOG = "../.kew-verify-collect.log"

#: How many of the collect log's lines are reported when it carries no count.
#: A collection error that reaches nobody is a verification nobody can debug.
_COLLECT_TAIL = 40

#: pytest's own collect-only summary, in both the shapes it is printed in:
#: ``"6300/18900 tests collected (12600 deselected) in 20.02s"`` when
#: something was deselected, ``"18900 tests collected in 20.02s"`` when
#: nothing was. The first number is the selection either way.
#: ``tests/test_verify_shards.py`` runs a real collect-only pass against this.
COLLECTED_PATTERN = re.compile(r"^(\d+)(?:/\d+)? tests collected", re.MULTILINE)

#: Where a shard's run writes its junit report: beside the collect log, and
#: removed before the run, so a report a previous job left on a reused host is
#: never read as this run's.
_JUNIT_REPORT = "../.kew-verify-junit.xml"

#: The heredoc delimiter the executed check's source is shipped inside.
_EXECUTED_DELIMITER = "KEW_EXECUTED_CHECK"

#: The executed check's own report line (:data:`kew.verify_executed.REPORT_FORMAT`).
EXECUTED_PATTERN = re.compile(
    r"^kew-verify: (\d+) of (\d+) collected tests executed$", re.MULTILINE
)


@dataclass(frozen=True)
class ShardReport:
    """One job's answer inside a sharded verification."""

    #: ``"1/3"`` for a shard, :data:`SINGLE_JOB` for the single-run phases.
    job: str

    #: The machine that answered — the instance name, for a gcp pool.
    instance: str

    #: What that host returned, in the same shape a whole verify returns.
    result: VerifyResult

    #: The tests this job's collect-only pass counted, or ``None`` when it
    #: reported none. A missing count fails the verification; it is never read
    #: as a shard that happened to select nothing.
    selected: int | None

    #: How many of the selected tests the shard's run reported, by its own
    #: junit, or ``None`` when no count came back. Always ``None`` for
    #: :data:`SINGLE_JOB`, whose collection is counted and never run as one.
    executed: int | None = None

    @property
    def line(self) -> str:
        """One line per shard, which is what the acceptance asks to see."""
        exit_code = (
            "none" if self.result.exit_code is None else str(self.result.exit_code)
        )
        selected = "none" if self.selected is None else str(self.selected)
        executed = ""
        if self.job != SINGLE_JOB:
            executed = " executed=" + (
                "none" if self.executed is None else str(self.executed)
            )
        return (
            f"  {self.job}: instance={self.instance} exit={exit_code} "
            f"selected={selected}{executed} seconds={self.result.seconds:.1f}"
        )


def read_selected(log: str) -> int | None:
    """The selection a job's collect-only pass counted, read off its log.

    The *first* match: the collect-only pass is each job's own first act, and
    what follows it is a run whose own collection reports in another shape.
    """
    match = COLLECTED_PATTERN.search(log)
    return None if match is None else int(match.group(1))


def read_executed(log: str) -> int | None:
    """How many collected tests a shard's run reported, read off its log.

    The *last* match: the check is the shard's last act, and the run before it
    prints whatever its tests print, so an earlier line is not the check's.
    """
    matches = EXECUTED_PATTERN.findall(log)
    return int(matches[-1][0]) if matches else None


def executed_lines(run: str) -> list[str]:
    """Run the shard's selection to junit, then check it ran what it collected.

    The check's source is the operator's kew (:mod:`kew.verify_executed`),
    shipped in the script, so the candidate's checkout cannot rewrite the code
    that checks it. A failing run still gets checked, and keeps its own status:
    the check only turns a green run red. The status is left in ``run_status``
    for the caller to exit with, after whatever else the job runs.
    """
    source = Path(verify_executed.__file__).read_text(encoding="utf-8")
    if _EXECUTED_DELIMITER in source:  # pragma: no cover - a source edit
        raise RuntimeError("the executed check's source contains its delimiter")
    return [
        f"rm -f {_JUNIT_REPORT}",
        "run_status=0",
        f"{run} --junitxml={_JUNIT_REPORT} || run_status=$?",
        f"uv run python -I - {_COLLECT_LOG} {_JUNIT_REPORT} "
        f"<<'{_EXECUTED_DELIMITER}' || [ \"$run_status\" -ne 0 ] || run_status=1",
        source.rstrip("\n"),
        _EXECUTED_DELIMITER,
    ]


def collect_lines(shard: str | None) -> list[str]:
    """Count this job's selection before it runs it, and fail if it cannot.

    The count is pytest's own, off a ``--collect-only`` pass of exactly the
    selection the job is about to run, so the union check compares what the
    shards really selected against what one unsharded collection really
    selects. Both failures — a pass that errored, a pass that printed no
    count — end the job with the tail of the log that explains them, because
    a shard whose count is unknown must not be read as a shard that passed.
    """
    select = "" if shard is None else f" --shard={shard}"
    rescue = f"{{ tail -n {_COLLECT_TAIL} {_COLLECT_LOG} >&2; exit 1; }}"
    return [
        f'uv run pytest --collect-only -q -m "not serial"{select} '
        f"> {_COLLECT_LOG} 2>&1 || {rescue}",
        f"grep -E '^[0-9]+(/[0-9]+)? tests collected' {_COLLECT_LOG} || {rescue}",
    ]


def _worker_cap() -> str:
    """The xdist cap, in the same shape verify.sh exports it: a floor, not a
    ceiling an inherited value can raise."""
    return f'export {XDIST_WORKERS_ENV}="${{{XDIST_WORKERS_ENV}:-{XDIST_WORKERS}}}"'


def shard_job_script(index: int, total: int, candidate: VerifyCandidate) -> str:
    """The parallel phase for one shard, and the count it is checked against.

    Only that phase: the serial phase and ``ui-check`` run once, in
    :func:`single_job_script`, on the first host of the pool, and the static
    gates and the held-out phase ran before any shard (:func:`job_script`).
    """
    spec = f"{index}/{total}"
    return (
        "\n".join(
            [
                "set -eu",
                "umask 022",
                phase_line(SETUP, "start"),
                "cd kew",
                _worker_cap(),
                *collect_lines(spec),
                phase_line("xdist", "start"),
                *executed_lines(
                    f'uv run pytest -n auto -m "not serial" --shard={spec}'
                ),
                # A red run leaves the phase open, which is what marks it red.
                f'[ "$run_status" -ne 0 ] || {phase_line("xdist", "end")}',
                'exit "$run_status"',
            ]
        )
        + "\n"
    )


def single_job_script(
    candidate: VerifyCandidate, *, images: list[tuple[str, str]]
) -> str:
    """Everything a sharded verification runs exactly once, on the first host.

    The gate after its static gates and containment test, which the gate job
    ran before any shard (:data:`STAGE_SUITE`), told that the suite is running
    elsewhere (:data:`SUITE_ENV`), then the serial phase, then ``ui-check``
    when the console moved. The serial phase is run here rather than left to verify.sh
    because the floor verify.sh applies with it reads one machine's joined
    coverage data, and a sharded run has none: the shards' data never leaves
    their own hosts. That floor is the one gate a sharded verify does not
    discharge, and kew says so in its own output and in docs/cli/verify.md.

    The unsharded collect-only pass runs first, because its count is the
    number the shards' counts are summed against — after the pinned-tool
    preflight, which is cheaper still (:data:`PREFLIGHT_COMMAND`).
    """
    lines = ["set -eu", "umask 022", phase_line(SETUP, "start"), "cd kew"]
    lines += [PREFLIGHT_COMMAND, *collect_lines(None)]
    lines += _image_lines(images)
    lines += [
        _phase(PHASE_SUITE),
        f"{STAGE_ENV}={STAGE_SUITE} "
        f"{SUITE_ENV}={SUITE_ELSEWHERE} bash scripts/verify.sh",
        phase_line("serial", "start"),
        "uv run pytest -m serial",
        phase_line("serial", "end"),
    ]
    lines += _ui_check_lines(candidate)
    lines += red_main_check.phase_lines(candidate.red_tests)
    return "\n".join(lines) + "\n"


#: What a report says when its host never reached the job at all. Rule 3: a
#: shard that does not report is a failed verification, never a skipped one,
#: so this is recorded as the reason rather than left blank.
_UNREPORTED = "the job never ran on this host, so it reported nothing"


def shard_jobs(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    provider: HostProvider,
    shards: int,
) -> list[tuple[str, Job]]:
    """The N shard jobs and the single-run job, labelled, in dispatch order.

    One environment for all of them: the same candidate, the same guard base,
    the same deselects. The hosts in a pool are one provider type, so what
    the private image layer is told
    (:attr:`~kew.hosts.provider.HostProvider.builds_private_images`) is the
    same answer on each.
    """
    ceiling = config.verify.timeout_seconds
    timeout = None if ceiling == 0 else float(ceiling)
    environment = job_environment(
        candidate,
        config=config,
        builds_private_images=provider.builds_private_images,
    )
    labelled = [
        (
            f"{index}/{shards}",
            Job(
                name=f"{JOB_NAME}-shard-{index}-of-{shards}",
                command=shard_job_script(index, shards, candidate),
                environment=environment,
                timeout_seconds=timeout,
            ),
        )
        for index in range(1, shards + 1)
    ]
    labelled.append(
        (
            SINGLE_JOB,
            Job(
                name=f"{JOB_NAME}-{SINGLE_JOB}",
                command=single_job_script(candidate, images=pinned_images(config)),
                environment=environment,
                timeout_seconds=timeout,
            ),
        )
    )
    return labelled


def shard_sessions(
    providers: list[HostProvider], labelled: list[tuple[str, Job]]
) -> list[tuple[HostProvider, list[tuple[str, Job]]]]:
    """Which host runs which jobs: one shard each, the single-run job first.

    The first host takes two jobs, and takes them in that order on purpose.
    One host runs one job at a time (that is kew's own lock), so the two queue
    there — and the shard goes first so every host starts its slice of the
    parallel phase at once, which is the wall clock the sharding exists to
    cut. The single-run job then uses the time the other hosts spend finishing
    their shards.
    """
    shards, single = labelled[:-1], labelled[-1]
    sessions = [[shard] for shard in shards]
    sessions[0].append(single)
    return list(zip(providers, sessions, strict=True))


def _session_reports(
    provider: HostProvider,
    jobs: list[tuple[str, Job]],
    results: list[JobResult],
    *,
    failure: str | None,
    category: str | None,
    released: str | None,
    seconds: float,
) -> list[ShardReport]:
    """One report per job this host was given, answered or not.

    The release failure belongs to the session rather than to a job, so it is
    recorded once, on the last report — the point at which the host was
    actually let go.
    """
    reports: list[ShardReport] = []
    last = len(jobs) - 1
    for position, (label, _job) in enumerate(jobs):
        trailing = released if position == last else None
        if position < len(results):
            result = _completed(results[position], trailing)
        else:
            result = VerifyResult(
                host=provider.name,
                exit_code=None,
                seconds=seconds if position == 0 else 0.0,
                log="",
                error=_recorded(failure or _UNREPORTED, trailing),
                error_category=category,
            )
        reports.append(
            ShardReport(
                job=label,
                instance=provider.address,
                result=result,
                selected=read_selected(result.log),
                executed=None if label == SINGLE_JOB else read_executed(result.log),
            )
        )
    return reports


#: The label the gate job runs under in the first host's session. It is
#: judged on its own (:class:`GateOpening`) and is never one of the reports.
GATE_JOB = "gate"


class GateOpening:
    """Where a sharded verification's suite jobs wait for the gate job (#3212).

    The first host's session runs the gate job first and hands its answer
    here; every session waits here before its first suite job, and runs it
    only when the judged gate job passed. A session that ends before the gate
    job answered closes it, so no host waits on a verdict that never comes.
    """

    def __init__(
        self,
        job: Job,
        judge: Callable[[VerifyResult], VerifyResult],
        *,
        grace: float | None = None,
    ):
        self.job = job
        self._judge = judge
        self._grace = GATE_WAIT_GRACE if grace is None else grace
        self._settled = threading.Event()
        self.result: VerifyResult | None = None

    def answer(self, answer: JobResult) -> None:
        self.result = _judged(answer, self._judge)
        self._settled.set()

    def close(self) -> None:
        self._settled.set()

    def opens(self) -> bool:
        """Wait for the verdict; True when the suite may start.

        The wait is bounded by the gate job's own ceiling and the time its
        host takes to come up and take the bundle (*grace*, by default
        :data:`GATE_WAIT_GRACE`):
        past it, no verdict is coming, and the session fails rather than hangs.
        An unbounded gate job (``timeout_seconds = 0``) waits unbounded, as
        the gate job itself does; the first host's session closes the opening
        however it ends.
        """
        ceiling = self.job.timeout_seconds
        bound = None if ceiling is None else ceiling + self._grace
        if not self._settled.wait(bound):
            raise HostTimeout(
                f"no verdict from the gate job within {bound:.0f}s, so no suite "
                "job ran on this host"
            )
        return self.result is not None and self.result.exit_code == 0


#: What a suite host waits for the gate job beyond its ceiling: the first host
#: brings itself up and takes the bundle before the gate job starts.
GATE_WAIT_GRACE = 900.0


def _run_jobs(
    provider: HostProvider,
    jobs: list[tuple[str, Job]],
    results: list[JobResult],
    opening: GateOpening | None,
) -> None:
    """Run *jobs* in order, each behind the gate job's verdict when there is
    one, appending each answer to *results* as it comes."""
    for label, job in jobs:
        if opening is not None and label == GATE_JOB:
            opening.answer(provider.run(job))
            continue
        if opening is not None and not opening.opens():
            return
        results.append(provider.run(job))


def _run_session(
    provider: HostProvider,
    bundle: Path,
    jobs: list[tuple[str, Job]],
    opening: GateOpening | None = None,
) -> list[ShardReport]:
    """Ship to one host once, run its jobs in order, release it once.

    The claim spans the whole session for the reason it does in the unsharded
    case (:func:`_run_on_host`): bringing the host up and shipping to it are
    writes another process must not land in the middle of. A job that answered
    before the host failed keeps its answer — the results collected so far are
    returned beside the failure, instead of the whole session being reduced to
    the reason its last job never ran.

    With an *opening*, each suite job waits for the gate job's verdict
    (:class:`GateOpening`), and the session that runs the gate job closes the
    opening as it ends, however it ends: a host that was busy before the
    claim, a release that raised, anything.
    """
    started = time.monotonic()
    released: str | None = None
    results: list[JobResult] = []
    failure: str | None = None
    category: str | None = None
    try:
        with provider.claim():
            try:
                provider.ensure_up()
                provider.ship(bundle)
                _run_jobs(provider, jobs, results, opening)
            finally:
                released = _release(provider)
    except HostError as exc:
        failure = str(exc)
        category = _category(exc)
    finally:
        if opening is not None and (GATE_JOB, opening.job) in jobs:
            opening.close()
    return _session_reports(
        provider,
        [(label, job) for label, job in jobs if label != GATE_JOB],
        results,
        failure=failure,
        category=category,
        released=released,
        seconds=time.monotonic() - started,
    )


def _collected_count(reports: list[ShardReport]) -> int | None:
    """What the single-run job's unsharded collection selected, if it said."""
    single = next((report for report in reports if report.job == SINGLE_JOB), None)
    return None if single is None else single.selected


def _unreported(reports: list[ShardReport]) -> list[str]:
    """Every job whose collect-only pass came back without a count."""
    return [report.job for report in reports if report.selected is None]


def _executed_error(shards: list[ShardReport]) -> str | None:
    """Why the shards' runs did not each report every test they collected.

    Collection and the run are separate pytest processes, so a selected count
    proves nothing about what ran (#2629): the run's own junit, checked on the
    host against the collect log, is the only evidence a test executed.
    """
    silent = [report.job for report in shards if report.executed is None]
    if silent:
        return (
            f"no executed-test count came back from {', '.join(silent)}, so kew "
            "cannot check that those shards ran the tests they collected"
        )
    short = [
        f"{report.job} ran {report.executed} of {report.selected}"
        for report in shards
        if report.executed != report.selected
    ]
    if short:
        return (
            f"a shard did not run every test it collected ({'; '.join(short)}): "
            "a test that is collected and never run must not read as passing"
        )
    return None


def count_check(reports: Iterable[ShardReport]) -> tuple[int | None, str | None]:
    """The unsharded count, and why the shards do not add up to it.

    The union check: `--shard=K/N` is only safe if the N groups partition the
    collection, so the sum of what the shards selected has to equal what one
    unsharded collection selects. A sharded run that silently dropped a group
    of tests would otherwise report green having never run them, which is a
    worse outcome than never sharding.

    A count that did not come back is named, not assumed: a shard whose
    collect-only pass reported nothing is not a shard that selected zero.
    Then each shard's executed count is held to its selected one
    (:func:`_executed_error`), because a selection that adds up is still only
    a collection.
    """
    rows = list(reports)
    shards = [report for report in rows if report.job != SINGLE_JOB]
    collected = _collected_count(rows)
    missing = _unreported(rows)
    if missing:
        return collected, (
            f"no test count came back from {', '.join(missing)}, so kew cannot "
            "check that the shards ran the whole selection between them"
        )
    total = sum(report.selected or 0 for report in shards)
    if collected is not None and total != collected:
        return collected, (
            f"the shards selected {total} tests between them and one unsharded "
            f"collection selects {collected}: the split is not a partition of "
            "the suite, so some tests ran twice or not at all"
        )
    return collected, _executed_error(shards)


def _sharded_exit(reports: list[ShardReport], count_error: str | None) -> int | None:
    """The verification's own status: every job's, plus the union check's.

    ``None`` the moment one job answered nothing — rule 3, so a host's silence
    cannot be read as a pass. Otherwise the first non-zero status any job
    returned, verbatim, exactly as the unsharded verify passes a gate's status
    through. A clean set of jobs whose counts do not add up still fails: the
    gate answered, and kew's own check did not.
    """
    if any(report.result.exit_code is None for report in reports):
        return None
    failed = [
        report.result.exit_code for report in reports if report.result.exit_code != 0
    ]
    if failed:
        return failed[0]
    return 1 if count_error else 0


def sharded_result(
    reports: list[ShardReport], *, host: str, seconds: float
) -> VerifyResult:
    """Every job's answer as one verification, with nothing summarised away."""
    collected, count_error = count_check(reports)
    log = "\n".join(
        f"--- {report.job} on {report.instance} ---\n{report.result.log}"
        for report in reports
    )
    error = _recorded(
        *(
            f"{report.job}: {report.result.error}"
            for report in reports
            if report.result.error
        ),
        count_error,
    )
    return VerifyResult(
        host=host,
        exit_code=_sharded_exit(reports, count_error),
        seconds=seconds,
        log=log,
        error=error,
        error_category=next(
            (
                report.result.error_category
                for report in reports
                if report.result.error_category
            ),
            None,
        ),
        shards=tuple(reports),
        collected=collected,
    )


#: The signals that unwind a verify, and so must reach the main thread.
_UNWINDING_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})


def _leave_signals_to_main() -> None:
    """Block the unwinding signals in a shard worker thread (#3121).

    A ``SIGTERM`` sent to the process lands on any thread that does not block
    it. Landing on a worker, it never woke the main thread out of its wait on
    the futures, so the unwind that interrupts the hosts waited out every job
    first. Blocked here, the kernel hands it to the main thread. A child the
    worker starts inherits the mask, so neither signal ends it: the unwind
    ends the hosts' sessions with ``SIGKILL`` (:meth:`HostProvider.interrupt`),
    and a timeout ends a call the same way, neither of which a mask holds off.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, _UNWINDING_SIGNALS)


def run_sharded(
    candidate: VerifyCandidate,
    *,
    config: DispatchConfig,
    providers: list[HostProvider],
    bundle: Path,
    opening: GateOpening | None = None,
) -> VerifyResult:
    """Run the shards and the single-run job at once, and read them together.

    Timed from the outside: the whole point of the node is the wall clock, and
    the sum of the jobs' own durations is not it.

    With an *opening*, the first host runs its gate job before anything else,
    and no host starts a suite job until kew judged it clean
    (:class:`GateOpening`); a gate job that went red is the answer.
    """
    labelled = shard_jobs(
        candidate, config=config, provider=providers[0], shards=len(providers)
    )
    sessions = _gated_sessions(shard_sessions(providers, labelled), opening)
    started = time.monotonic()
    pool = ThreadPoolExecutor(
        max_workers=len(sessions), initializer=_leave_signals_to_main
    )
    try:
        futures = [
            pool.submit(_run_session, provider, bundle, jobs, opening)
            for provider, jobs in sessions
        ]
        gathered = {
            report.job: report for future in futures for report in future.result()
        }
    except BaseException:
        # A SIGTERM (:func:`terminate_unwinds`) or ^C lands here, in the main
        # thread, while the workers wait on their jobs: end those jobs, so each
        # worker reaches its release now and the wait below is short (#2811).
        # A worker waiting on the gate job's verdict is let go the same way.
        _interrupt_pool(sessions, opening)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    seconds = time.monotonic() - started
    reports = [gathered[label] for label, _job in labelled]
    return _behind_gate(
        sharded_result(reports, host=providers[0].name, seconds=seconds),
        reports,
        opening,
    )


def _gated_sessions(
    sessions: list[tuple[HostProvider, list[tuple[str, Job]]]],
    opening: GateOpening | None,
) -> list[tuple[HostProvider, list[tuple[str, Job]]]]:
    """*sessions* with the gate job first on the first host, when there is one."""
    if opening is None:
        return sessions
    (first, jobs), *rest = sessions
    return [(first, [(GATE_JOB, opening.job), *jobs]), *rest]


def _interrupt_pool(
    sessions: list[tuple[HostProvider, list[tuple[str, Job]]]],
    opening: GateOpening | None,
) -> None:
    """End every host's job, and let go every worker waiting on the gate."""
    if opening is not None:
        opening.close()
    for provider, _jobs in sessions:
        provider.interrupt()


def _behind_gate(
    result: VerifyResult, reports: list[ShardReport], opening: GateOpening | None
) -> VerifyResult:
    """The pool's answer read behind the gate job's: the red gate job when it
    stopped the pool, the two together when it did not."""
    if opening is None or opening.result is None:
        return result
    if opening.result.exit_code != 0:
        return _stopped_pool(opening.result, reports)
    # The pool's wall clock already spans the gate job, which ran first in
    # one of its sessions: it is counted once.
    staged = _staged_result([opening.result, result], None)
    return replace(staged, seconds=result.seconds)


def _stopped_pool(gate: VerifyResult, reports: list[ShardReport]) -> VerifyResult:
    """The red gate job's answer, with what the pool's sessions still have to
    say: a host kew could not release is never dropped with the jobs it never
    ran."""
    skipped = f"{_UNREPORTED}; "
    left = [
        f"{report.job}: {report.result.error.removeprefix(skipped)}"
        for report in reports
        if report.result.error and report.result.error != _UNREPORTED
    ]
    return replace(gate, error=_recorded(gate.error, *left))


# --- Held-out base tests (#2485) --------------------------------------------
# Every gate above runs the candidate's own tests, which the implementer can
# edit. The base branch's tests were written before the implementer started
# and no PR can edit them, so they are the cheapest held-out check there is:
# run them against the candidate's src, and a failure has to be declared.
#
# The base tests go into a temporary clone OUTSIDE the checkout, never into it
# — the checkout is what every other phase of the job runs against, and a test
# tree swapped inside it would be the tree they ran. The clone shares the
# checkout's objects, sits at the candidate, and has its test tree replaced
# wholesale by base's: a base test the PR deleted comes back, and a test only
# the PR added is not there to be run.
#
# The host reports, the operator judges: the job prints what failed and how
# the run exited, and never fails on it, because only the PR body can say
# which failures are intended, and the PR body never leaves this machine.
#
# The job's log is the only thing a host sends back, and the candidate's own
# code prints into it first. So the phase opens with a line naming a nonce it
# draws only once the static gates have ended, tags every line it reports with
# it, and kew reads nothing but the tagged lines after the LAST such opening
# (:func:`held_out_section`). Each run's verdict inside it is the base64 of
# the JSON report kew's own plugin wrote to a file outside the checkout; the
# run's own output goes to a file of its own and is never read as a result.

#: The project directory and the test tree the held-out phase swaps for base's.
HELD_OUT_PROJECT = "kew"
HELD_OUT_TESTS = f"{HELD_OUT_PROJECT}/tests"

#: The interpreter a held-out run starts, from its project directory. ``uv
#: run`` there syncs a fresh environment from the lock, so the src under test
#: is the candidate's and nothing is borrowed from the checkout. It runs kew's
#: report plugin as the script, which starts pytest with itself as the first
#: plugin (:func:`_held_out_pytest`).
#:
#: ``--frozen`` installs exactly what ``uv.lock`` says and never re-locks, so a
#: ``[tool.uv.sources]`` the head adds to its ``pyproject.toml`` cannot swap a
#: pinned distribution while the lock stays the base's. ``--no-config`` reads
#: no ``uv.toml`` or ``[tool.uv]`` settings of the head's, and ``--python``
#: names the interpreter kew found itself (:func:`_python_lines`), so no
#: ``.python-version`` of the head's is ever read — uv runs what one names.
HELD_OUT_RUNNER = 'uv run --frozen --no-config --python "$held_out_python" python'

#: A Python request kew passes to ``uv python find``: a version or a range.
_PYTHON_REQUEST = re.compile(r"[0-9<>=!~., ]+")

#: Draws *n* random bytes as hex, on the host.
_RANDOM_HEX = "$(od -An -N{n} -tx1 /dev/urandom | tr -d ' \\n')"

#: The suite's two selections, which the held-out phase repeats: the
#: parallel phase and the serial one.
HELD_OUT_PARALLEL = '-n auto -m "not serial"'
HELD_OUT_SERIAL = "-m serial"

#: What every held-out run adds: name each failure and error in the summary,
#: and keep going past a base module the candidate no longer lets import.
_HELD_OUT_REPORTING = "-rfE --continue-on-collection-errors"

#: The prefix of every line the held-out phase reports in the job log.
_HELD_OUT_MARK = "kew-held-out:"


#: Where the base's own collection of a run reports what it collected.
_EXPECTED = '"$held_out_root/expected.json"'


def _held_out_pytest(
    candidate: VerifyCandidate,
    args: str,
    *,
    name_passes: bool = False,
    collect: bool = False,
) -> str:
    """The command a held-out run is, with only kew's plugins in it.

    Autoload is off, so no ``pytest11`` entry point loads of its own accord.
    kew's report plugin (:mod:`kew.held_out_report_plugin`) is the script:
    loaded from its own path under a name drawn at random, it registers
    before any ``-p`` or conftest can load anything, and writes the run's
    verdict to ``$held_out_report`` — refused unless all the run loaded is
    what the candidate's pins allow. The pinned plugins follow by name;
    ``PYTEST_PLUGINS`` and ``PYTHONPATH`` let an xdist worker load the report
    plugin too. *name_passes* has the report name the tests that passed.

    The configuration is the base's (``-c``, :func:`held_out_config`), and the
    rootdir the project directory, as on the base: nothing in the head's
    pytest configuration reaches the run. *collect* is the base's own
    ``--collect-only`` of the same run, in the base's own tree, which lists
    what it collected; the head's run is held to that list (:data:`_EXPECTED`).
    """
    tree = "$held_out_root/base" if collect else "$held_out_root/repo"
    report = _EXPECTED if collect else '"$held_out_report"'
    settings = [
        *(f"{name}={value}" for name, value in AUTOLOAD_OFF.items()),
        'PYTEST_PLUGINS="$held_out_plugin"',
        f"{REPORT_ENV}={report}",
        f"{SETTINGS_ENV}={shlex.quote(candidate.held_out_pins.settings())}",
        f'{CHECKOUT_ENV}="{tree}"',
        f'{TRUSTED_ENV}="{tree}/{HELD_OUT_TESTS}"',
        'PYTHONPATH="$held_out_root/plugin${PYTHONPATH:+:$PYTHONPATH}"',
        f"{LIST_COLLECTED_ENV}=1" if collect else f"{EXPECTED_ENV}={_EXPECTED}",
    ]
    if name_passes:
        settings.append(f"{NAME_PASSES_ENV}=1")
    script = '"$held_out_root/plugin/$held_out_plugin.py"'
    plugins = "".join(f" {arg}" for arg in candidate.held_out_pins.args())
    config = f" -c {shlex.quote(candidate.held_out_config.name)} --rootdir=."
    only = " --collect-only -q" if collect else ""
    return (
        f"{' '.join(settings)} {HELD_OUT_RUNNER} {script}{plugins}{config} {args}{only}"
    )


def _run_lines(
    candidate: VerifyCandidate, args: str, log: str, *, name_passes: bool = False
) -> list[str]:
    """The base's own collection of a run, then the run against the head.

    The collection runs first, in the base's own tree with the base's own
    ``src`` and environment, so what the head's run must collect is fixed
    before the head's code runs. Its output goes to a log of its own, shown
    only when it reported nothing.
    """
    base = '"$held_out_root/base.log"'
    collect = _held_out_pytest(candidate, args, collect=True)
    command = _held_out_pytest(candidate, args, name_passes=name_passes)
    return [
        "    held_out_status=0",
        f'    rm -f "$held_out_report" {_EXPECTED}',
        f'    (cd "$held_out_root/base/{HELD_OUT_PROJECT}" && {collect}) '
        f"> {base} 2>&1 || true",
        f"    [ -f {_EXPECTED} ] || tail -n {_COLLECT_TAIL} {base} | sed 's/^/| /'",
        f'    (cd "$held_out_root/repo/{HELD_OUT_PROJECT}" && {command}) '
        f"> {log} 2>&1 || held_out_status=$?",
    ]


def _report_plugin_lines() -> list[str]:
    """Write kew's report plugin, as this kew ships it, under a random name.

    Beside the held-out tree, never in it, and drawn on the host once the
    static gates have ended, so the candidate can neither shadow the module
    nor know its name in advance. An environment the head committed under
    ``kew/.venv`` is removed, so the one the run uses is the one uv builds.
    """
    source = resources.files("kew").joinpath("held_out_report_plugin.py")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return [
        f'    held_out_plugin="{PLUGIN_PREFIX}{_RANDOM_HEX.format(n=8)}"',
        '    mkdir "$held_out_root/plugin"',
        f"    printf '%s' {shlex.quote(encoded)} | base64 -d "
        '> "$held_out_root/plugin/$held_out_plugin.py"',
        '    held_out_report="$held_out_root/report.json"',
        f'    rm -rf "$held_out_root/repo/{HELD_OUT_PROJECT}/.venv"',
    ]


def _python_lines(candidate: VerifyCandidate, *, main_python: bool) -> list[str]:
    """Find the interpreter every held-out run is given, outside any project.

    From the held-out root, with no configuration and no project, so no
    ``.python-version`` or ``uv.toml`` in the tree steers the find itself.
    With *main_python* the request is ``==X.Y.Z``, the exact version of the
    interpreter the job's ``uv sync`` built the checkout environment on
    (:func:`main_python_lines`). That sync read the checkout's own
    ``.python-version`` and ``requires-python``, the head's among them, so
    the head can steer which X.Y.Z it is; what reaches this request is only
    that version number, and the base-collection run, in a clone of the
    base, is refused by uv on an interpreter outside the base's own
    ``requires-python``. Otherwise, or when the job synced none, the request
    is the base's own (:func:`held_out_python`), read by kew from the base.
    A request nothing on the host answers is the host's problem, not the
    candidate's: the job says so with :data:`ENVIRONMENT_BROKEN_MARK` and
    ends with :data:`ENVIRONMENT_BROKEN_EXIT`.
    """
    request = shlex.quote(candidate.held_out_python)
    lines = [f"    held_out_request={request}"]
    if main_python:
        lines.append(
            f'    [ -z "${MAIN_PYTHON_VAR}" ] || '
            f'held_out_request="==${MAIN_PYTHON_VAR}"'
        )
    return [
        *lines,
        '    if ! held_out_python="$(cd "$held_out_root" && '
        'uv python find --no-config --no-project "$held_out_request")"; then',
        f'        echo "{ENVIRONMENT_BROKEN_MARK} no Python on this host answers '
        'the held-out request $held_out_request"',
        f"        exit {ENVIRONMENT_BROKEN_EXIT}",
        "    fi",
    ]


def _report_line(label: str) -> list[str]:
    """Print the run's report as one *label* line, ``-`` when it wrote none."""
    return [
        '    if [ -f "$held_out_report" ]; then',
        f'        echo "$held_out_mark {label} '
        '$(base64 < "$held_out_report" | tr -d \'\\n\')"',
        "    else",
        f'        echo "$held_out_mark {label} -"',
        "    fi",
    ]


def _opening_lines() -> list[str]:
    """Draw the phase's nonce and open its section of the log with it."""
    return [
        f'held_out_nonce="{_RANDOM_HEX.format(n=16)}"',
        f'held_out_mark="{_HELD_OUT_MARK}$held_out_nonce"',
        f'echo "{_HELD_OUT_MARK} begin $held_out_nonce"',
    ]


#: The line that opens the held-out phase's section, and its nonce.
_HELD_OUT_BEGIN = re.compile(
    rf"^{_HELD_OUT_MARK} begin ([0-9a-f]{{32}})$", re.MULTILINE
)


def held_out_section(log: str) -> str:
    """The held-out phase's own lines in one job's *log*, untagged.

    Only the lines after the opening line that carry its nonce. The phase
    opens once per job; a log that opens it more than once carries a line
    something else printed — before it or, from a process left running, after
    it — and is read as the one line ``opened <n>``, which fails the phase
    (:func:`held_out_report`) rather than letting either opening win. ``""``
    when no phase opened.
    """
    opened = list(_HELD_OUT_BEGIN.finditer(log))
    if not opened:
        return ""
    if len(opened) > 1:
        return f"{_HELD_OUT_MARK} opened {len(opened)}\n"
    tag = f"{_HELD_OUT_MARK}{opened[-1].group(1)} "
    return "".join(
        f"{_HELD_OUT_MARK} {line[len(tag) :]}\n"
        for line in log[opened[-1].end() :].splitlines()
        if line.startswith(tag)
    )


#: pytest's statuses a run that reached a verdict exits with: all passed, some
#: failed, nothing selected. Anything else is a run that broke.
_HELD_OUT_VERDICTS = (0, 1, 5)

_HELD_OUT_RUN_REPORT = re.compile(rf"^{_HELD_OUT_MARK} report (\S+)$", re.MULTILINE)
_HELD_OUT_EXIT = re.compile(
    rf"^{_HELD_OUT_MARK} exit (\d+) seconds (\d+)$", re.MULTILINE
)
_HELD_OUT_SKIPPED = re.compile(rf"^{_HELD_OUT_MARK} skipped (.+)$", re.MULTILINE)
_HELD_OUT_OPENED = re.compile(rf"^{_HELD_OUT_MARK} opened (\d+)$", re.MULTILINE)
_CONTRACT_EXIT = re.compile(rf"^{_HELD_OUT_MARK} contract-exit (\d+)$", re.MULTILINE)
_HELD_OUT_STALE_LOCK = re.compile(rf"^{_HELD_OUT_MARK} stale-lock$", re.MULTILINE)

#: What the held-out phase's error says first when the head's ``uv.lock`` no
#: longer matches its ``pyproject.toml`` (#3234): ``--frozen`` installs the
#: stale lock, so a dependency the head added is missing and its import
#: fails every base test file that reaches it.
STALE_LOCK_ERROR = "the head's uv.lock is stale: run `uv lock` and commit it"
_CONTRACT_REPORT = re.compile(
    rf"^{_HELD_OUT_MARK} contract-report (\S+)$", re.MULTILINE
)

#: The heading that opens the PR body's **Behavior changes** section, as a
#: markdown heading or a bold line, and whatever opens the next section.
_BEHAVIOR_HEADING = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)behaviou?r changes\b", re.I)
_SECTION_START = re.compile(r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*:?\s*$)")

#: A pytest node id as a PR body writes one: a test file, and optionally the
#: test inside it.
_NODE_ID = re.compile(r"[\w./-]+\.py(?:::[^\s`'\"]+)?")


@dataclass(frozen=True)
class HeldOutReport:
    """What the held-out phase answered, judged against the PR body."""

    #: ``passed``, ``failed``, ``skipped`` (the base has no test tree, or the
    #: candidate changed no test file) or ``not_run`` (no job reported the
    #: phase at all).
    outcome: str

    #: Every base test the phase reported failing or erroring, by node id.
    failed: tuple[str, ...] = ()

    #: The failures the PR body's **Behavior changes** section declared.
    declared: tuple[str, ...] = ()

    #: The failures it did not — any one of these fails the verification.
    undeclared: tuple[str, ...] = ()

    #: The phase's wall time on the host, summed over its runs.
    seconds: int | None = None

    #: Why the phase failed or never reported, in words.
    error: str | None = None

    #: The node's acceptance contract file, by project-relative path, when the
    #: phase ran one; its failures are never declared.
    contract: str | None = None

    #: Every contract file the phase ran, ``contract`` first — more than one
    #: for a merge-queue train (#2814).
    contracts: tuple[str, ...] = ()

    #: Why a ``skipped`` phase ran nothing, as the job reported it.
    reason: str | None = None

    #: Why the runs reached no verdict at all — a run that broke, a report
    #: that does not count, a red run that named no test, a run missing — or
    #: ``None`` when every run reached one (:func:`_verdictless_error`).
    verdictless: str | None = None

    #: Each run's exit status, in the order the runs reported.
    exits: tuple[int, ...] = ()

    #: The failures that are base tests a run never collected — beside their
    #: file's collector failure, when that is why.
    uncollected: tuple[str, ...] = ()


def read_contract(repo_root: Path, row: AcceptanceContractRow) -> TrustedContract:
    """The contract file *row* records, read at its contract commit.

    Never from the head: the head's copy is the implementer's to edit, and the
    contract is the version the ledger recorded. The text must still hash to
    the recorded sha256, and the path must sit in the test tree the held-out
    phase runs, or the contract could be recorded and never run; either way
    the verification is refused rather than run without it.
    """
    path = row.path
    name = path.rsplit("/", 1)[-1]
    if not path.startswith(f"{HELD_OUT_TESTS}/") or not (
        name.startswith("test_") and name.endswith(".py")
    ):
        raise VerifyRefused(
            f"the acceptance contract {path} is not a test file under "
            f"{HELD_OUT_TESTS}, so the held-out phase would never run it"
        )
    text = _git_text(repo_root, "cat-file", "blob", f"{row.commit}:{path}")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != row.sha256:
        raise VerifyRefused(
            f"the acceptance contract {path} at {row.commit[:12]} does not "
            "match the sha256 the ledger recorded for it"
        )
    return TrustedContract(path=path, text=text)


def read_contracts(
    repo_root: Path, rows: Sequence[AcceptanceContractRow]
) -> tuple[TrustedContract, ...]:
    """Each of *rows*' contract files (:func:`read_contract`), once by path.

    Two different contracts at one path would overwrite each other in the
    held-out tree, so that is refused rather than one of them run.
    """
    held: dict[str, TrustedContract] = {}
    for row in rows:
        read = read_contract(repo_root, row)
        if held.setdefault(read.path, read) != read:
            raise VerifyRefused(
                f"two different acceptance contracts are recorded at {read.path}"
            )
    return tuple(held.values())


def _git_text(repo_root: Path, *args: str) -> str:
    """``git``'s stdout as it printed it — a file's bytes keep their last newline."""
    try:
        completed = subprocess_utils.run(
            ["git", *args], cwd=repo_root, check=True, capture=True
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise VerifyRefused(f"git {' '.join(args)} could not be read: {exc}") from exc
    return completed.stdout or ""


def _contract_nodes(candidate: VerifyCandidate) -> tuple[str, ...]:
    """The contract files as node ids relative to the project."""
    return tuple(held_out_node_id(c.path) for c in candidate.held_contracts)


def _write_lines(tree: str, path: str, text: str) -> list[str]:
    """Write *text* to *path* in *tree* (``repo`` or ``base``), and stage it."""
    where = shlex.quote(path)
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return [
        f'    mkdir -p "$(dirname "$held_out_root/{tree}/"{where})"',
        f"    printf '%s' {shlex.quote(encoded)} | base64 -d "
        f'> "$held_out_root/{tree}/"{where}',
        f'    git -C "$held_out_root/{tree}" add -f -- {where}',
    ]


def _contract_lines(contracts: Sequence[TrustedContract], tree: str) -> list[str]:
    """Write the contracts into *tree*, over whatever was there."""
    lines = []
    for contract in contracts:
        lines += _write_lines(tree, contract.path, contract.text)
        if tree == "repo":
            lines.append(
                f'    echo "$held_out_mark contract "{shlex.quote(contract.path)}'
            )
    return lines


def _config_lines(candidate: VerifyCandidate, tree: str) -> list[str]:
    """Write the base's pytest configuration into *tree*'s project directory."""
    config = candidate.held_out_config
    return _write_lines(tree, f"{HELD_OUT_PROJECT}/{config.name}", config.text)


def _held_out_tree_lines(candidate: VerifyCandidate, tree: str) -> list[str]:
    """Clone the candidate, swap its test tree for base's, add the contract.

    The base's pytest configuration is written beside the head's, under a
    name of kew's (:func:`held_out_config`). Then the base itself is cloned
    beside it, with the contracts and that configuration, for the base's own
    collection of each run (:func:`_run_lines`).
    """
    base = shlex.quote(candidate.base_sha)
    base_tests = f"git -C {tree} checkout {base} -- {HELD_OUT_TESTS}"
    if candidate.held_contracts:
        # A contract is run even over a base without a test tree of its own.
        base_tests = f"if {_has_base_tests(candidate)}; then {base_tests}; fi"
    own = '"$held_out_root/base"'
    return [
        f"    git clone --quiet --shared --no-checkout .. {tree}",
        f"    git -C {tree} checkout --quiet --detach {shlex.quote(candidate.ref_sha)}",
        f"    git -C {tree} rm -r -q --ignore-unmatch {HELD_OUT_TESTS}",
        f"    {base_tests}",
        *_contract_lines(candidate.held_contracts, "repo"),
        *_config_lines(candidate, "repo"),
        # Committed, so the tree is clean to a test that reads git as the main
        # phase's checkout is; empty when the candidate left the tests alone.
        f"    git -C {tree} -c user.name=kew -c user.email=kew@invalid.local "
        "commit --quiet --no-verify --allow-empty -m 'kew: held-out base tests'",
        f"    git clone --quiet --shared --no-checkout .. {own}",
        f"    git -C {own} checkout --quiet --detach {base}",
        *_contract_lines(candidate.held_contracts, "base"),
        *_config_lines(candidate, "base"),
    ]


def _has_base_tests(candidate: VerifyCandidate) -> str:
    """The shell test for a base with a test tree to hold out."""
    base = shlex.quote(candidate.base_sha)
    return f"git -C .. cat-file -e {base}:{HELD_OUT_TESTS} 2>/dev/null"


def _contract_run_lines(candidate: VerifyCandidate, log: str) -> list[str]:
    """Run the contracts alone, by path, in one run, and report what passed.

    The base-test runs above take the base's ``addopts``, which could narrow
    the contract away; this run clears them, and its report names every test
    it passes, so :func:`held_out_report` can tell a contract that passed from
    one that never ran. ``--confcutdir`` keeps conftests above the base's test
    tree — the head's own, whose hooks could rewrite a failing report as a
    pass — out of the run. The rest of its configuration is the base's,
    ``pythonpath`` included, so the head's own ``src`` is imported where the
    base's would be, and nothing of the head's configuration is read.
    """
    contracts = candidate.held_contracts
    if not contracts:
        return []
    node = " ".join(shlex.quote(held_out_node_id(c.path)) for c in contracts)
    args = f"-o addopts= --confcutdir=tests {_HELD_OUT_REPORTING} {node}"
    return [
        *_run_lines(candidate, args, log, name_passes=True),
        *_report_line("contract-report"),
        f"    {_tail_unless_verdict(log)}",
        '    echo "$held_out_mark contract-exit $held_out_status"',
    ]


def _tail_unless_verdict(log: str) -> str:
    """Show the end of a run that broke, each line fenced off with ``| ``.

    The fence keeps a line the run printed from reading as one of the phase's.
    """
    return (
        'case "$held_out_status" in 0|1|5) ;; '
        f"*) tail -n {_COLLECT_TAIL} {log} | sed 's/^/| /' ;; esac"
    )


def held_out_lines(
    candidate: VerifyCandidate,
    selections: Iterable[str],
    *,
    run_contract: bool = True,
    main_python: bool = False,
) -> list[str]:
    """Run the base branch's tests against the candidate's src, and report.

    Run from the job's project directory, so the checkout root is ``..``. A
    base without a test tree has nothing to hold out and says so, unless the
    node has an acceptance contract, which is run regardless. The phase never
    fails the job on a test failure — it opens its section of the log with a
    nonce (:func:`held_out_section`), then reports one ``report`` line and one
    ``exit`` line per run, each tagged with it, which :func:`held_out_report`
    reads. A setup step that fails (the clone, the checkout) does fail the
    job, under the script's own ``set -e``.

    *run_contract* adds the contract's own run (:func:`_contract_run_lines`);
    the gate job runs it once, sharded or not. *main_python* gives every run
    the job's own synced interpreter (:func:`_python_lines`); a job that
    synced no checkout environment leaves it off.

    Each run covers the candidate's held-out selection (#2816): the whole base
    tree, or only the base files the candidate changed, and nothing at all —
    reported as skipped, never as a pass — when it changed no test file.
    """
    paths = _held_out_paths(candidate)
    if paths == ():
        return [
            *_opening_lines(),
            f'echo "$held_out_mark skipped {NO_TEST_FILE_CHANGED}"',
        ]
    targets = "".join(f" {shlex.quote(path)}" for path in paths or ())
    tree = '"$held_out_root/repo"'
    log = '"$held_out_root/run.log"'
    condition = "true" if candidate.held_contracts else _has_base_tests(candidate)
    lines = [
        *_opening_lines(),
        f"if {condition}; then",
        '    held_out_root="$(mktemp -d "${TMPDIR:-/tmp}/kew-held-out-XXXXXX")"',
        "    trap 'rm -rf \"$held_out_root\"' EXIT",
        f"    {_worker_cap()}",
        *_held_out_tree_lines(candidate, tree),
        *_report_plugin_lines(),
        *_python_lines(candidate, main_python=main_python),
        *_lock_check_lines(candidate, tree),
    ]
    for selection in selections:
        lines += [
            "    held_out_started=$(date +%s)",
            *_run_lines(candidate, f"{selection} {_HELD_OUT_REPORTING}{targets}", log),
            *_report_line("report"),
            f"    {_tail_unless_verdict(log)}",
            '    echo "$held_out_mark exit $held_out_status seconds '
            '$(( $(date +%s) - held_out_started ))"',
        ]
    if run_contract:
        lines += _contract_run_lines(candidate, log)
    lines += [
        "else",
        f'    echo "$held_out_mark skipped the base has no {HELD_OUT_TESTS}"',
        "fi",
    ]
    return lines


def _lock_check_lines(candidate: VerifyCandidate, tree: str) -> list[str]:
    """Say so when the head's ``uv.lock`` is stale, before the frozen runs.

    Only for a head that changed its ``pyproject.toml`` or ``uv.lock``: a lock
    the head left as the base had it is the base's, not a cause the head can
    be told to fix. The check (:mod:`kew.held_out_lock_check`, as this kew
    ships it) reads the two files and nothing else — no resolver, no network,
    so a host that cannot reach an index still names a dependency the head
    added without locking it (#3234). It is written outside the head's tree
    and run by the interpreter kew found, isolated (``-I``), so nothing of the
    head's is imported; a check that cannot run prints nothing. It only ever
    adds a cause to the phase's error, never a failure of its own.
    """
    project = f'"$held_out_root/repo/{HELD_OUT_PROJECT}"'
    changed = (
        f"git -C {tree} diff --quiet {shlex.quote(candidate.base_sha)} "
        f"{shlex.quote(candidate.ref_sha)} -- "
        f"{HELD_OUT_PROJECT}/pyproject.toml {HELD_OUT_PROJECT}/uv.lock"
    )
    source = resources.files("kew").joinpath("held_out_lock_check.py")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    script = '"$held_out_root/lock_check.py"'
    check = (
        f'"$held_out_python" -I {script} '
        f"{project}/pyproject.toml {project}/uv.lock 2>/dev/null || true"
    )
    return [
        f"    if ! {changed}; then",
        f"        printf '%s' {shlex.quote(encoded)} | base64 -d > {script}",
        f'        case "$({check})" in stale) echo "$held_out_mark stale-lock" ;; esac',
        "    fi",
    ]


def _held_out_paths(candidate: VerifyCandidate) -> tuple[str, ...] | None:
    """The project-relative paths each held-out run names, or ``None`` for the
    whole tree. A contract is always among them: it is never selected away."""
    if candidate.held_out.mode == FULL:
        return None
    paths = [held_out_node_id(path) for path in candidate.held_out.files]
    for contract in _contract_nodes(candidate):
        if contract not in paths:
            paths.append(contract)
    return tuple(paths)


#: How many held-out runs a verification reports: the parallel run and the
#: serial run, both in the gate job (:func:`job_script`), sharded or not.
HELD_OUT_RUNS = 2


def held_out_node_id(text: str) -> str:
    """*text* as a node id relative to the project, whatever it was written as."""
    node = text.strip().strip("`").removeprefix("./")
    return node.removeprefix(f"{HELD_OUT_PROJECT}/")


def _declared_on(line: str) -> list[str]:
    """The node ids *line* declares — only when it also gives a reason."""
    ids = _NODE_ID.findall(line)
    reason = _NODE_ID.sub("", line)
    if not ids or not re.search(r"[A-Za-z]{2}", reason):
        return []
    return [held_out_node_id(node) for node in ids]


def declared_behavior_changes(body: str | None) -> frozenset[str]:
    """The node ids the PR body's **Behavior changes** section declares.

    A node id counts only on a line that also gives a reason: a bare id is a
    failure nobody explained. The section runs from its heading to the next
    heading or bold section title; nothing outside it declares anything.
    """
    declared: set[str] = set()
    for line in behavior_change_lines(body):
        declared.update(_declared_on(line))
    return frozenset(declared)


def is_behavior_heading(line: str) -> bool:
    """True when *line* opens a **Behavior changes** section."""
    return bool(_BEHAVIOR_HEADING.match(line))


def behavior_change_lines(body: str | None) -> list[str]:
    """The lines inside *body*'s **Behavior changes** section(s), as written.

    The section runs from its heading to the next heading or bold section
    title, the span :func:`declared_behavior_changes` reads.
    """
    lines: list[str] = []
    inside = False
    for line in (body or "").splitlines():
        if _BEHAVIOR_HEADING.match(line):
            inside = True
            continue
        if inside and _SECTION_START.match(line):
            inside = False
        if inside:
            lines.append(line)
    return lines


def declared_on_line(line: str) -> frozenset[str]:
    """The node ids one **Behavior changes** line declares (it needs a reason)."""
    return frozenset(_declared_on(line))


def named_node_ids(text: str) -> list[str]:
    """Every node id *text* names, as held-out node ids, reason or not."""
    return [held_out_node_id(node) for node in _NODE_ID.findall(text)]


def without_node_ids(line: str) -> str:
    """*line* with every node id it names taken out — what is left is its reason."""
    return _NODE_ID.sub("", line)


def _held_out_error(
    log: str,
    statuses: list[int],
    failed: tuple[str, ...],
    undeclared: tuple[str, ...],
    runs: int,
    contracts: tuple[str, ...] = (),
    uncollected: frozenset[str] = frozenset(),
) -> str | None:
    """Why the held-out phase fails, or ``None`` when it passes.

    A run that broke rather than reached a verdict, a run whose own report
    does not count, a red run that named no test, a run that never reported,
    then any acceptance contract test that failed, then any other failure
    nobody declared, then the contracts' own run (:func:`_contract_run_error`).
    """
    return (
        _verdictless_error(log, statuses, failed, runs)
        or _undeclared_error(undeclared, contracts, uncollected)
        or _contract_run_error(log, contracts)
    )


def _verdictless_error(
    log: str, statuses: list[int], failed: tuple[str, ...], runs: int
) -> str | None:
    """Why the runs reached no verdict to judge, or ``None`` when they did: a
    run that broke, a report that does not count, a red run that named no
    test, a run that never reported."""
    broken = _broken_error(statuses) or _report_error(_HELD_OUT_RUN_REPORT.findall(log))
    if broken:
        return broken
    if 1 in statuses and not failed:
        return "a held-out run failed without naming a failing test"
    if len(statuses) < runs:
        return f"the held-out phase reported {len(statuses)} of {runs} runs"
    return None


def _broken_error(statuses: list[int]) -> str | None:
    """Why a held-out run broke rather than reached a verdict, or ``None``."""
    broken = [status for status in statuses if status not in _HELD_OUT_VERDICTS]
    if broken:
        return f"a held-out run exited {broken[0]} without reaching a verdict"
    return None


def _report_error(texts: list[str]) -> str | None:
    """Why a held-out run's own report does not count, or ``None``."""
    for text in texts:
        error = _run_report(text).error
        if error:
            return f"a held-out run {error}"
    return None


def _undeclared_error(
    undeclared: tuple[str, ...],
    contracts: tuple[str, ...],
    uncollected: frozenset[str] = frozenset(),
) -> str | None:
    """Why the failures nobody may or did declare fail the held-out phase.

    A test the base collects for a run and the run never collected is named
    as such: a head that kept a base test from being collected did not pass it.
    """
    broken_contract = [node for node in undeclared if _in_contract(node, contracts)]
    if broken_contract:
        return (
            "acceptance contract tests failed against the candidate's src: "
            f"{', '.join(broken_contract)}"
            + _never_collected(broken_contract, uncollected)
        )
    if undeclared:
        return (
            f"base tests failed against the candidate's src and are not declared "
            f"under Behavior changes in the PR body: {', '.join(undeclared)}"
            + _never_collected(undeclared, uncollected)
        )
    return None


def _never_collected(nodes: Sequence[str], uncollected: frozenset[str]) -> str:
    """Which of *nodes* the run never collected, though the base collects them."""
    missing = [node for node in nodes if node in uncollected]
    if not missing:
        return ""
    return (
        f" ({len(missing)} of them the base collects for the run and the "
        f"candidate's run never collected: {', '.join(missing)})"
    )


def _contract_run_error(log: str, contracts: tuple[str, ...]) -> str | None:
    """Why the contracts' own run does not show every contract passing.

    Their failures are named with the base tests' and judged there; this is
    the run that never reported, broke, wrote no report of its own, exited
    red with no failure inside a contract to show for it, or passed none of a
    contract's tests — a contract the head's config dropped or skipped.
    """
    if not contracts:
        return None
    named = ", ".join(contracts)
    exits = [int(code) for code in _CONTRACT_EXIT.findall(log)]
    if not exits:
        return f"the acceptance contract {named} run never reported"
    if exits[0] not in _HELD_OUT_VERDICTS:
        return f"the acceptance contract {named} run exited {exits[0]}"
    reports = [_run_report(text) for text in _CONTRACT_REPORT.findall(log)]
    if not reports:
        return f"the acceptance contract {named} run wrote no report"
    if reports[0].error:
        return f"the acceptance contract {named} run {reports[0].error}"
    return _contract_verdict_error(exits[0], reports[0], contracts)


def _contract_verdict_error(
    status: int, report: _RunReport, contracts: tuple[str, ...]
) -> str | None:
    """Why the contracts' run's own report does not bear out its exit status."""
    if status == 1:
        if any(_in_contract(node, contracts) for node in report.failed):
            return None
        return (
            f"the acceptance contract {', '.join(contracts)} run exited 1 "
            "with no failing test inside the contract"
        )
    for contract in contracts:
        if not any(_in_contract(node, (contract,)) for node in report.passed):
            return f"the acceptance contract {contract} ran no test"
    return _unpassed_error(report, contracts)


def _unpassed_error(report: _RunReport, contracts: tuple[str, ...]) -> str | None:
    """Why a contract that passed some tests still did not pass: one it
    collected was skipped or xfailed, and a contract passes only whole."""
    unpassed = [
        node
        for node in report.collected
        if _in_contract(node, contracts) and node not in report.passed
    ]
    if not unpassed:
        return None
    return (
        "acceptance contract tests ran and did not pass (skipped or "
        f"xfailed): {', '.join(unpassed)}"
    )


@dataclass(frozen=True)
class _RunReport:
    """One held-out run's verdict, as kew's report plugin wrote it."""

    #: Every test or collector that failed — and every test the base's own
    #: collection of the run has that the run never collected.
    failed: tuple[str, ...] = ()
    passed: tuple[str, ...] = ()
    #: The tests among *failed* that the run never collected.
    uncollected: tuple[str, ...] = ()
    #: Every test the run collected — reported by the contracts' run, each of
    #: whose contract tests must pass, a skip included.
    collected: tuple[str, ...] = ()
    #: Why the report does not count — missing, unreadable or refused.
    error: str | None = None


def _run_report(text: str) -> _RunReport:
    """Read a report line's payload: the base64 of the plugin's JSON."""
    if text == "-":
        return _RunReport(error="wrote no report")
    try:
        data = _report_json(text)
    except (ValueError, UnicodeDecodeError):
        return _RunReport(error="wrote a report kew cannot read")
    lists = _report_lists(data)
    if lists is None:
        return _RunReport(error="wrote a report kew cannot read")
    if data.get("refused"):
        return _RunReport(error=f"was refused: {data['refused']}")
    failed, passed, missing, collected = lists
    return _RunReport(
        failed=failed + missing,
        passed=passed,
        uncollected=missing,
        collected=collected,
    )


def _report_lists(
    data: dict[str, object],
) -> tuple[tuple[str, ...], ...] | None:
    """A report's failed, passed, uncollected and collected node ids, or
    ``None`` when one is not a list of node ids. The last two are optional."""
    lists = [data.get("failed"), data.get("passed")]
    lists += [data.get("uncollected", []), data.get("collected", [])]
    checked = [nodes for nodes in lists if _node_list(nodes)]
    if len(checked) != len(lists):
        return None
    return tuple(tuple(held_out_node_id(node) for node in nodes) for nodes in checked)


def _report_json(text: str) -> dict[str, object]:
    """The JSON map a report line carries as base64; ``ValueError`` otherwise."""
    data = json.loads(base64.b64decode(text, validate=True))
    if not isinstance(data, dict):
        raise ValueError("a report is a JSON map")
    return data


def _node_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(node, str) for node in value)


def _unreported_held_out(log: str) -> HeldOutReport:
    """A log with no held-out run in it: skipped on purpose, or silent."""
    skipped = _HELD_OUT_SKIPPED.search(log)
    if skipped:
        return HeldOutReport(outcome="skipped", reason=skipped.group(1).strip())
    return HeldOutReport(outcome="not_run", error="the held-out phase reported nothing")


def _run_reports(log: str) -> list[_RunReport]:
    """Every report the log carries: each held-out run's, then the contracts'."""
    texts = _HELD_OUT_RUN_REPORT.findall(log) + _CONTRACT_REPORT.findall(log)
    return [_run_report(text) for text in texts]


def _uncollected_ids(reports: list[_RunReport]) -> frozenset[str]:
    """Each test a run never collected though the base collects it for the run."""
    return frozenset(node for report in reports for node in report.uncollected)


def _failed_ids(reports: list[_RunReport]) -> tuple[str, ...]:
    """Each failing node id the reports name, once."""
    return tuple(dict.fromkeys(node for report in reports for node in report.failed))


def _in_contract(node: str, contracts: tuple[str, ...]) -> bool:
    """Whether *node* is a contract file or a test inside one."""
    return any(
        node == contract or node.startswith(f"{contract}::") for contract in contracts
    )


def _test_function(node: str) -> str:
    """*node* with its parametrization cut: ``f.py::T::t[x]`` is ``f.py::T::t``."""
    test, bracket, _ = node.partition("[")
    return test if bracket and "::" in test and node.endswith("]") else node


def declaration_excuses(node: str, declared: frozenset[str]) -> bool:
    """Whether *declared* names *node* exactly, or the test it parametrizes.

    The one rule a held-out failure is excused by: a line for the function
    covers each of its cases, and a line for one case covers that case only.
    """
    return node in declared or _test_function(node) in declared


def _undeclared(
    failed: tuple[str, ...],
    declared: frozenset[str],
    contracts: tuple[str, ...],
    uncollected: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """The failures the PR body did not declare, and every contract failure.

    A line for a test file whose collection failed declares that collector and
    each test of the file the run never collected (:func:`_uncollected_by`):
    one cause, one line. A test of that file that was collected and failed
    stays undeclared until a line names it.
    """
    excused = declared | _uncollected_by(failed, declared, uncollected)
    return tuple(
        node
        for node in failed
        if not declaration_excuses(node, excused) or _in_contract(node, contracts)
    )


def _uncollected_by(
    failed: tuple[str, ...], declared: frozenset[str], uncollected: frozenset[str]
) -> frozenset[str]:
    """The uncollected tests whose file's collector failed and is declared."""
    broken = {node for node in failed if "::" not in node and node in declared}
    return frozenset(node for node in uncollected if node.split("::", 1)[0] in broken)


def _contract_set(contract: str | None, contracts: tuple[str, ...]) -> tuple[str, ...]:
    """*contract* then *contracts*, each once."""
    return tuple(dict.fromkeys(([] if contract is None else [contract]) + [*contracts]))


def held_out_report(
    log: str,
    *,
    runs: int,
    declared: frozenset[str],
    contract: str | None = None,
    contracts: tuple[str, ...] = (),
) -> HeldOutReport:
    """Read the held-out phase's lines and judge them against *declared*.

    *log* is the phase's own section, as :func:`held_out_section` takes it out
    of a job's log — never the whole log, which the candidate prints into.
    Silence is not a pass: a log reporting fewer runs than *runs* fails, as
    does a run that broke rather than reaching a verdict. Every reported
    failure is either declared or fails the verification by name — and a
    failure in the *contract* file fails it whatever the PR body declares,
    because the contract is what the change is judged against. *contracts*
    are more contract files, judged the same way.
    """
    nodes = _contract_set(contract, contracts)
    first = nodes[0] if nodes else None
    reopened = _HELD_OUT_OPENED.search(log)
    if reopened:
        return HeldOutReport(
            outcome="failed",
            error=(
                f"a job's log opens the held-out phase {reopened.group(1)} times; "
                "the phase opens once, so a line something else printed is there"
            ),
            contract=first,
            contracts=nodes,
        )
    exits = [(int(code), int(secs)) for code, secs in _HELD_OUT_EXIT.findall(log)]
    if not exits:
        return replace(_unreported_held_out(log), contract=first, contracts=nodes)
    reports = _run_reports(log)
    failed = _failed_ids(reports)
    uncollected = _uncollected_ids(reports)
    undeclared = _undeclared(failed, declared, nodes, uncollected)
    statuses = [code for code, _ in exits]
    error = _stale_lock_first(
        log,
        _held_out_error(log, statuses, failed, undeclared, runs, nodes, uncollected),
    )
    return HeldOutReport(
        outcome="failed" if error else "passed",
        failed=failed,
        declared=tuple(node for node in failed if node not in undeclared),
        undeclared=undeclared,
        seconds=sum(secs for _, secs in exits),
        error=error,
        contract=first,
        contracts=nodes,
        verdictless=_verdictless_error(log, statuses, failed, runs),
        exits=tuple(statuses),
        uncollected=tuple(filter(uncollected.__contains__, failed)),
    )


def _stale_lock_first(log: str, error: str | None) -> str | None:
    """*error*, led by :data:`STALE_LOCK_ERROR` when the phase found the
    head's lock stale: that is the likely cause of the failures it names. A
    phase that passed anyway is left passing."""
    if error is None or not _HELD_OUT_STALE_LOCK.search(log):
        return error
    return f"{STALE_LOCK_ERROR}; {error}"


def held_out_sections(result: VerifyResult) -> str:
    """Every job's held-out section (:func:`held_out_section`), untagged.

    Only the gate job's log when the verification ran in stages
    (:attr:`VerifyResult.gate_log`), so nothing the suite prints after it is
    read. Otherwise each job's own log, so no job's output can open a section
    another job's lines are read from.
    """
    if result.gate_log is not None:
        return held_out_section(result.gate_log)
    logs = [shard.result.log for shard in result.shards] or [result.log]
    return "".join(held_out_section(log) for log in logs)


def apply_held_out(
    result: VerifyResult,
    *,
    runs: int,
    declared: frozenset[str],
    contract: str | None = None,
    contracts: tuple[str, ...] = (),
) -> VerifyResult:
    """*result* with its held-out phase read, and failed by it when it fails.

    Only a verification the gate passed can be failed here: one that already
    failed keeps its own status, and one with no answer stays incomplete. A
    held-out phase that never reported, behind a gate that passed, fails it.

    Only each job's own held-out section is read (:func:`held_out_sections`).
    """
    report = held_out_report(
        held_out_sections(result),
        runs=runs,
        declared=declared,
        contract=contract,
        contracts=contracts,
    )
    if result.exit_code != 0 or report.outcome not in ("failed", "not_run"):
        return replace(result, held_out=report)
    return replace(
        result,
        exit_code=1,
        error=_recorded(result.error, f"held-out: {report.error}"),
        held_out=report,
    )


def _held_out_payload(report: HeldOutReport | None) -> dict[str, object] | None:
    """The held-out phase as the verify event records it.

    The contract is recorded only for a node that has one, so a node without
    one records exactly what the phase recorded before contracts existed.
    """
    if report is None:
        return None
    payload: dict[str, object] = {
        "outcome": report.outcome,
        "failed": list(report.failed),
        "declared": list(report.declared),
        "undeclared": list(report.undeclared),
        "seconds": report.seconds,
        "error": report.error,
    }
    if report.contract is not None:
        payload["contract"] = report.contract
    if len(report.contracts) > 1:
        payload["contracts"] = list(report.contracts)
    if report.reason is not None:
        payload["reason"] = report.reason
    return payload


def _held_out_summary(report: HeldOutReport) -> str:
    """The held-out phase's part of :func:`summary_line`."""
    seconds = "" if report.seconds is None else f" held_out_seconds={report.seconds}"
    return f" held_out={report.outcome}{seconds}"


# --- Diff-scoped mutation report (#2495, #2817) ------------------------------
# Mutation testing per change, on the changed code only: the weekly job mutates
# whole modules, so a PR's new tests were never asked whether they kill mutants
# in the code the PR changed. Report only, so it is off the gate: it runs after
# the merge as a job of its own, and is read back off that job's log.

#: The audit event a post-merge mutation report writes.
MUTATION_EVENT = "mutation_reported"

#: The report job's name, as it appears in a result and in lock diagnostics.
MUTATION_JOB_NAME = "mutation-report"

#: The wall-clock cap on the mutation phase. A run that reaches it is stopped
#: and reports what it finished, naming the cap.
MUTATION_CAP_SECONDS = 600

#: What every line the phase prints starts with (``scripts/mutmut-sampler.py``).
_MUTATION_MARK = "kew-mutation:"

_MUTATION_SURVIVED = re.compile(
    rf"^{_MUTATION_MARK} survived (\S+):(\d+) (\S+) (.*?)\s*$", re.MULTILINE
)
_MUTATION_DONE = re.compile(
    rf"^{_MUTATION_MARK} done exit=(\S+) capped=(yes|no) cap=(\d+(?:\.\d+)?)s "
    r"seconds=(\d+) selected=(\d+) killed=(\d+) survived=(\d+) "
    r"not_checked=(\d+)",
    re.MULTILINE,
)
_MUTATION_SKIPPED = re.compile(rf"^{_MUTATION_MARK} skipped (.*?)\s*$", re.MULTILINE)
_MUTATION_ERROR = re.compile(rf"^{_MUTATION_MARK} error (.*?)\s*$", re.MULTILINE)
_MUTATION_REASON = re.compile(rf"^{_MUTATION_MARK} reason (.*?)\s*$", re.MULTILINE)
_MUTATION_DESELECTED = re.compile(rf"^{_MUTATION_MARK} deselected (\S+) ", re.MULTILINE)


@dataclass(frozen=True)
class SurvivingMutant:
    """One mutant on a changed line that no test killed."""

    #: The source file, relative to the project (``src/kew/x.py``).
    path: str

    #: The line the mutation sits on.
    line: int

    #: mutmut's name for it — what ``mutmut show`` takes.
    mutant: str

    #: What it changed, as ``'old' -> 'new'``.
    mutation: str


@dataclass(frozen=True)
class MutationReport:
    """What the diff-scoped mutation phase answered."""

    #: ``completed``, ``capped`` (the cap stopped it; results are partial),
    #: ``skipped`` (nothing to mutate or no tests to run), ``error``,
    #: ``not_run`` (no job reported the phase at all), or ``deferred`` (a
    #: gating verification: the report runs after the merge).
    outcome: str

    #: Every survivor, located.
    survivors: tuple[SurvivingMutant, ...] = ()

    #: How many mutants sat on the changed lines.
    selected: int | None = None

    #: How many of those the tests killed.
    killed: int | None = None

    #: How many the run never reached — the cap stopped it first.
    not_checked: int | None = None

    #: The cap the phase ran under, in seconds.
    cap_seconds: float | None = None

    #: The phase's wall time on the host: what it adds to a verification.
    seconds: int | None = None

    #: Why it was skipped, or what went wrong, in words.
    reason: str | None = None

    #: The tests left out because they failed mutmut's baseline run under
    #: mutants/ (#2804): mutants they alone would kill read as survivors.
    deselected: tuple[str, ...] = ()


#: What a gating verification reports for the phase it no longer runs.
DEFERRED = MutationReport(
    outcome="deferred", reason="the mutation report runs after the merge"
)


def mutation_lines(candidate: VerifyCandidate) -> list[str]:
    """Run the mutation phase on the lines *candidate* changed since its base.

    Run from the job's project directory. The phase never fails the job: the
    sampler exits 0 whatever mutmut answered, and one that dies anyway is
    reported as an error line rather than stopping the script.
    """
    command = (
        "uv run python scripts/mutmut-sampler.py "
        f"--diff-scope {shlex.quote(candidate.base_sha)} "
        f"--cap {MUTATION_CAP_SECONDS}"
    )
    return [
        f'{command} || echo "{_MUTATION_MARK} error the mutation phase exited $?"',
    ]


def mutation_report(log: str) -> MutationReport:
    """Read the mutation phase off a job log."""
    done = _MUTATION_DONE.search(log)
    if done is None:
        skipped = _MUTATION_SKIPPED.search(log)
        if skipped:
            return MutationReport(outcome="skipped", reason=skipped.group(1))
        error = _MUTATION_ERROR.search(log)
        if error:
            return MutationReport(outcome="error", reason=error.group(1))
        return MutationReport(
            outcome="not_run", reason="the mutation phase reported nothing"
        )
    survivors = tuple(
        SurvivingMutant(path=path, line=int(line), mutant=mutant, mutation=change)
        for path, line, mutant, change in _MUTATION_SURVIVED.findall(log)
    )
    exit_code = done.group(1)
    capped = done.group(2) == "yes"
    cap = float(done.group(3))
    outcome, reason = "completed", None
    if capped:
        outcome, reason = "capped", f"the run hit the {cap:g}s cap; results are partial"
    elif exit_code != "0":
        # mutmut that dies (a scoped test failing under mutants/) leaves every
        # mutant unchecked: zero survivors there is no answer, not a clean run.
        # The sampler names mutmut's own reason when it has one (#2804).
        outcome, reason = "error", f"mutmut exited {exit_code} before finishing"
        why = _MUTATION_REASON.search(log)
        if why:
            reason = f"{reason}: {why.group(1)}"
    return MutationReport(
        outcome=outcome,
        survivors=survivors,
        selected=int(done.group(5)),
        killed=int(done.group(6)),
        not_checked=int(done.group(8)),
        cap_seconds=cap,
        seconds=int(done.group(4)),
        reason=reason,
        deselected=tuple(_MUTATION_DESELECTED.findall(log)),
    )


def _mutation_payload(report: MutationReport | None) -> dict[str, object] | None:
    """The mutation phase as the verify event records it."""
    if report is None:
        return None
    return {
        "outcome": report.outcome,
        "survivors": [
            {
                "path": survivor.path,
                "line": survivor.line,
                "mutant": survivor.mutant,
                "mutation": survivor.mutation,
            }
            for survivor in report.survivors
        ],
        "selected": report.selected,
        "killed": report.killed,
        "not_checked": report.not_checked,
        "cap_seconds": report.cap_seconds,
        "seconds": report.seconds,
        "reason": report.reason,
        "deselected": list(report.deselected),
    }


def _mutation_summary(report: MutationReport) -> str:
    """The mutation phase's part of :func:`summary_line`."""
    parts = [f" mutation={report.outcome}"]
    if report.selected is not None and report.outcome != "error":
        parts.append(f" survived={len(report.survivors)}/{report.selected}")
    if report.seconds is not None:
        parts.append(f" mutation_seconds={report.seconds}")
    if report.outcome == "capped" and report.cap_seconds is not None:
        parts.append(f" mutation_cap={report.cap_seconds:g}s")
    return "".join(parts)


def mutation_job_script(candidate: VerifyCandidate) -> str:
    """The post-merge report job: the mutation phase alone, from ``kew/``."""
    lines = ["set -eu", "umask 022", "cd kew", *mutation_lines(candidate)]
    return "\n".join(lines) + "\n"


def _report_of(result: VerifyResult) -> MutationReport:
    """What the report job answered, or why it answered nothing."""
    report = mutation_report(result.log)
    if report.outcome == "not_run" and result.error:
        return MutationReport(outcome="error", reason=result.error)
    return report


def run_mutation_report(
    config: DispatchConfig,
    *,
    store: RunStore | None,
    merged_sha: str,
    base_sha: str,
    pr_number: int,
    repo_root: Path,
    host: str | None = None,
) -> MutationReport | None:
    """Mutation-test the merged commit's diff against *base_sha* as its own job.

    Runs on *host* (``[verify] host`` by default) and records
    :data:`MUTATION_EVENT`. ``None`` — nothing run, nothing recorded — while
    another job holds the host: a report is never worth a gating
    verification's wait, so it yields and is asked again later.
    """
    candidate = VerifyCandidate(
        ref=merged_sha,
        ref_sha=merged_sha,
        base_ref=base_sha,
        base_sha=base_sha,
        frontend_changed=False,
    )
    try:
        (provider,) = provider_pool(
            config, provider=host or config.verify.host, count=1
        )
        record_lapsed_leases(store, [provider], config=config)
    except HostError as exc:
        raise VerifyRefused(str(exc)) from exc
    ceiling = MUTATION_CAP_SECONDS * 2
    job = Job(
        name=MUTATION_JOB_NAME,
        command=mutation_job_script(candidate),
        environment={},
        timeout_seconds=float(ceiling),
    )
    with work_dirs.work_tempdir("kew-mutation-") as workspace:
        bundle = write_bundle(repo_root, candidate, Path(workspace))
        try:
            # Claimed before the session, so a host another job holds is a
            # yield, not a report that it was busy (the claim is re-entrant).
            with provider.claim():
                result = _run_on_host(provider, bundle, job)
        except HostBusy as exc:
            logger.warning("mutation report for PR #%s yields: %s", pr_number, exc)
            return None
    report = _report_of(result)
    emit(
        store,
        MUTATION_EVENT,
        actor=resolve_actor(),
        repo=config.project.repo or None,
        payload={
            "pr": pr_number,
            "ref_sha": merged_sha,
            "base_sha": base_sha,
            "host": result.host,
            "mutation": _mutation_payload(report),
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )
    return report


#: How many survivors a PR comment lists before it says how many it left out.
_COMMENT_SURVIVORS = 50


def mutation_comment(merged_sha: str, report: MutationReport) -> str:
    """The PR thread's copy of a post-merge mutation report."""
    lines = [
        f"**Mutation report** for the merged commit `{merged_sha[:12]}`:"
        f"{_mutation_summary(report)}"
    ]
    if report.reason:
        lines.append(f"\n{report.reason}")
    if report.survivors:
        lines.append("\n| file | line | mutation |\n|---|---|---|")
        lines += [
            f"| `{survivor.path}` | {survivor.line} | "
            f"`{survivor.mutation.replace('|', '\\|')}` |"
            for survivor in report.survivors[:_COMMENT_SURVIVORS]
        ]
        left = len(report.survivors) - _COMMENT_SURVIVORS
        if left > 0:
            lines.append(f"\n…and {left} more, in the `{MUTATION_EVENT}` event.")
    return "\n".join(lines) + "\n"
