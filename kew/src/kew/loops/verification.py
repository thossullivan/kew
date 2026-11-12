"""Verification gate for first-class loops (Plan 3).

A loop closes as ``achieved`` only when a real check passes.
The agent's word never closes a loop.
``GateEvaluator`` supports command and CI conditions with a diff guard.

SECURITY: the command run by ``CommandCondition`` is OPERATOR-CONFIG-ONLY
(``config.commands.verify_loop``). It is executed as ``sh -c "<command>"`` —
arbitrary code — so it must NEVER be issue/agent-sourced. The diff-guard only
stops an agent from cheating a *trusted* command; it cannot defend against an
attacker-chosen command. See the Global Constraints in the plan.

Canonical command verification materializes the published commit in host-owned
scratch space. The project files remain agent-authored and need admission.
Checkout publication retains the admitted legacy behavior.
Checklist, human, and judge conditions remain deferred.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kew import sandbox, subprocess_utils, work_dirs
from kew.config import SandboxConfig
from kew.loops.ci import CiResult, wait_for_ci
from kew.publication import sanitize_publication_evidence
from kew.publication_repository import HostGitPreparation, fetch_commit, provision
from kew.sandbox import (
    ContainedImageUnresolvedError,
    SandboxImageUnmapped,
    build_checkout_program_argv,
    proxy_redaction_scope,
    redact_credentials,
    require_image_id,
    resolve_runner_image,
    run_container_with_cpu_cap_retry,
)

# Cap captured output so a noisy command cannot bloat status_reason.
_MAX_EVIDENCE_CHARS = 500
HOST_OWNED_VERIFICATION_POSTURE = "host_owned_verification"
UNCONTAINED_VERIFICATION_POSTURE = "uncontained_host_verification"
CONTAINED_VERIFICATION_POSTURE = "contained_verification"
HOST_VERIFICATION_POSTURE = "host_verification"
VERIFICATION_REFUSAL_PREFIX = "#1946 refusal:"
VERIFICATION_TIMEOUT_SECONDS = 600.0
VERIFICATION_OUTPUT_BYTES = 256 * 1024


@dataclass(frozen=True)
class HostVerificationAdmission:
    """Resolved authority and posture for one host verification operation."""

    posture: str | None
    refusal: str | None
    runtime: str | None = None
    image: str | None = None
    image_ref: str | None = None
    proxy_environment: str = "none"
    #: In-image paths the contained run mounts writable over its read-only
    #: root: only a ``[sandbox] check_image`` admission names any (#3495).
    scratch_paths: tuple[str, ...] = ()

    def record_fields(self) -> dict[str, str | None]:
        """Fields retained with admission and verification evidence."""
        return {
            "verification_posture": self.posture or "refused_verification",
            "verification_image": self.image,
            "verification_image_ref": self.image_ref,
            "verification_refusal": self.refusal,
            "verification_proxy_environment": (
                _verification_proxy_environment(self) if self.posture else None
            ),
        }

    def __post_init__(self) -> None:
        if (self.posture is None) == (self.refusal is None):
            raise ValueError("host verification admission needs one resolved outcome")


def resolve_host_verification_admission(
    *, agent_written_checkout: bool, dangerously_skip_containment: bool
) -> HostVerificationAdmission:
    """Resolve host verification authority from checkout ownership and config."""
    if not agent_written_checkout:
        return HostVerificationAdmission(
            posture=HOST_OWNED_VERIFICATION_POSTURE,
            refusal=None,
            proxy_environment="inherit",
        )
    if dangerously_skip_containment:
        return HostVerificationAdmission(
            posture=UNCONTAINED_VERIFICATION_POSTURE,
            refusal=None,
            proxy_environment="inherit",
        )
    return HostVerificationAdmission(
        posture=None,
        refusal=(
            "#1444 refusal: host verification cannot execute in an agent-written "
            "checkout without [sandbox] dangerously_skip_containment = true"
        ),
    )


def resolve_command_verification_admission(
    config: SandboxConfig, *, runner: str
) -> HostVerificationAdmission:
    """Prefer a local approved image, regardless of the agent's sandbox setting.

    Verification needs no runner credentials or runner-native containment.
    Resolve the configured image to immutable bytes before executing any work.
    """
    runtime = sandbox.detect_runtime_cached(config)
    image_ref = None
    try:
        image_ref = resolve_runner_image(config, runner)
        image = require_image_id(
            runtime, image_ref, runner=runner, refusal="command verification refused"
        )
    except (SandboxImageUnmapped, ContainedImageUnresolvedError) as exc:
        return _missing_verification_containment(config, str(exc), image_ref)
    return HostVerificationAdmission(
        posture=CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime=config.runtime,
        image=image,
        image_ref=image_ref,
        proxy_environment=config.proxy_environment,
    )


def resolve_check_image_admission(
    config: SandboxConfig, *, runner: str
) -> HostVerificationAdmission:
    """The container a repository's own checks run in (#3281).

    ``[sandbox] check_image`` names an image with the repository's
    dependencies built in, for the pre-review gate and the resolver's checks.
    Unset, this is :func:`resolve_command_verification_admission` exactly. Set,
    the image is resolved to its immutable ID the same way, and one the runtime
    cannot resolve is a refusal: never the runner image, never the host, since
    the operator asked for checks that only that image can run.
    """
    check_image = config.check_image.strip()
    if not check_image:
        return resolve_command_verification_admission(config, runner=runner)
    try:
        image = _check_image_id(
            sandbox.detect_runtime_cached(config), check_image, runner=runner
        )
    except ContainedImageUnresolvedError as exc:
        return HostVerificationAdmission(
            posture=None,
            refusal=redact_credentials(f"{VERIFICATION_REFUSAL_PREFIX} {exc}"),
            image_ref=check_image,
        )
    return HostVerificationAdmission(
        posture=CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime=config.runtime,
        image=image,
        image_ref=check_image,
        proxy_environment=config.proxy_environment,
        scratch_paths=sandbox.CHECK_IMAGE_SCRATCH_PATHS,
    )


_CHECK_IMAGE_REFUSAL = (
    "check verification refused until [sandbox] check_image is built "
    "(kew's own: bash scripts/build-check-image.sh)"
)


def _check_image_id(runtime: str | None, image: str, *, runner: str) -> str:
    """Resolve the check image to its immutable ID, or raise with the remedy.

    :func:`kew.sandbox.require_image_id` tells an operator to build an absent
    image with ``kew sandbox build``, which builds runner images: wrong for a
    check image, which the repository's own script builds. The image is
    inspected once.
    """
    if runtime is None:
        # No runtime at all: the shared seam words that, inspecting nothing.
        return require_image_id(
            runtime, image, runner=runner, refusal=_CHECK_IMAGE_REFUSAL
        )
    outcome = sandbox.inspect_image_id(runtime, image)
    if outcome.status is sandbox.ImageIdStatus.RESOLVED and outcome.image_id:
        return outcome.image_id
    if outcome.status is sandbox.ImageIdStatus.ABSENT:
        raise ContainedImageUnresolvedError(
            f"check image {image!r} is not present in the local container "
            f"runtime ({runtime}), and kew never pulls it: build it with "
            "scripts/build-check-image.sh (kew's own; another repository "
            "builds its own) and point [sandbox] check_image at the tag it "
            f"prints. Until then, {_CHECK_IMAGE_REFUSAL}."
        )
    detail = f" ({outcome.detail})" if outcome.detail else ""
    raise ContainedImageUnresolvedError(
        f"cannot resolve check image {image!r} to an immutable image ID: the "
        f"container runtime {runtime!r} did not answer{detail}; "
        f"{_CHECK_IMAGE_REFUSAL}. This is a runtime problem, not a missing "
        "image: the image may well be there."
    )


def _missing_verification_containment(
    config: SandboxConfig, reason: str, image_ref: str | None
) -> HostVerificationAdmission:
    fallback = resolve_host_verification_admission(
        agent_written_checkout=True,
        dangerously_skip_containment=(
            config.dangerously_skip_containment and config.enabled is not True
        ),
    )
    if fallback.refusal is None:
        return HostVerificationAdmission(
            posture=HOST_VERIFICATION_POSTURE,
            refusal=None,
            image_ref=image_ref,
            proxy_environment="inherit",
        )
    return HostVerificationAdmission(
        posture=None,
        refusal=redact_credentials(
            f"{VERIFICATION_REFUSAL_PREFIX} {reason}; {fallback.refusal}"
        ),
        image_ref=image_ref,
    )


def _verification_proxy_environment(admission: HostVerificationAdmission | None) -> str:
    """Match contained launch policy; host children inherit their environment."""
    if admission is not None and admission.posture == CONTAINED_VERIFICATION_POSTURE:
        return admission.proxy_environment
    return "inherit"


def _host_verification_refusal(
    *, agent_written_checkout: bool, dangerously_skip_containment: bool
) -> str | None:
    """Explain why host verification cannot enter an agent-written checkout."""
    return resolve_host_verification_admission(
        agent_written_checkout=agent_written_checkout,
        dangerously_skip_containment=dangerously_skip_containment,
    ).refusal


@dataclass(frozen=True)
class ConditionResult:
    """Outcome of one stopping condition."""

    met: bool
    evidence: str
    # Unmet because a human has not acted yet (CI awaiting ``ci-approved``,
    # #2033), not because the work failed. The gate reports it as waiting.
    waiting: bool = False
    # Unmet because the command never finished, or could not be executed at all
    # — a gate that could not run, not a proven failure. The merge queue (#2148)
    # withholds a merge on either rather than reporting back to the PR a defect
    # that has not been shown to exist.
    timed_out: bool = False
    unrunnable: bool = False
    # The command's own exit status, when it ran to an exit at all. The
    # pre-review gate (#3201) reads 126/127 as a tool the image does not carry,
    # not a defect in the code it was pointed at.
    exit_code: int | None = None
    # Unrunnable because the command printed more than the output budget, not
    # because it could not start: the pre-review gate (#3201) reads that as the
    # command's own failure, with the kept tail as its evidence.
    output_overflowed: bool = False
    # The command's whole output, credentials redacted and bounded by the
    # output budget, its lines intact. The evidence keeps only a sanitized
    # excerpt (the head of a host run, the tail of each contained stream),
    # which can miss every error line; the resolution checks (#3495) quote
    # those wherever they fall, and sanitize what they keep.
    output: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", redact_credentials(self.evidence))
        object.__setattr__(self, "output", redact_credentials(self.output))


class StoppingCondition(Protocol):
    """Evaluates loop + worktree state; returns met | unmet (+ evidence).

    Plain ``Protocol`` (no ``@runtime_checkable``): used only for the static
    ``list[StoppingCondition]`` typing on ``GateEvaluator``.
    """

    def check(
        self,
        *,
        worktree: Path,
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
    ) -> ConditionResult: ...


def _truncate(text: str) -> str:
    text = _sanitize_evidence(text).strip()
    if len(text) <= _MAX_EVIDENCE_CHARS:
        return text
    return text[:_MAX_EVIDENCE_CHARS] + "…"


def _whole_output(stdout: str | None, stderr: str | None) -> str:
    """Both streams, line by line, credentials redacted before the budget
    bounds them. Path ancestry is kept, so a consumer can name a file relative
    to the tree it checked; one that retains the output sanitizes it."""
    stdout = stdout or ""
    if stdout and stderr and not stdout.endswith("\n"):
        stdout += "\n"
    text = redact_credentials(stdout + (stderr or ""))
    if len(text) > VERIFICATION_OUTPUT_BYTES:
        text = text[:VERIFICATION_OUTPUT_BYTES].rpartition("\n")[0]
    return text


def _sanitize_evidence(text: str) -> str:
    """Remove credentials and host ancestry before selecting evidence excerpts."""
    if not text:
        return ""
    return sanitize_publication_evidence(redact_credentials(text), limit=None)


def published_changed_paths(
    *, remote: str, base_sha: str, published_sha: str
) -> list[str]:
    """Read the complete path delta between two exact published commits."""
    if published_sha == base_sha:
        return []
    with work_dirs.work_tempdir("kew-loop-verification-diff-") as temp:
        git = HostGitPreparation(Path(temp))
        repo = provision(Path(temp) / "repo", source=remote, base_sha=base_sha, git=git)
        fetch_commit(repo, source=remote, sha=published_sha, git=git)
        result = git.run(
            [
                "git",
                "-C",
                str(repo.path),
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                base_sha,
                published_sha,
                "--",
            ],
            check=False,
        )
        if result.returncode != 0:
            detail = _truncate(result.stderr or result.stdout or "")
            raise RuntimeError(f"published diff failed: {detail}")
        return sorted(path for path in (result.stdout or "").split("\0") if path)


def _materialize_verification_checkout(
    repository: Path, checkout: Path, published_sha: str, git: HostGitPreparation
) -> subprocess.CompletedProcess[str]:
    """Keep trusted Git metadata inside the mount, without links to host objects."""
    # Fetched commits have no branch. Give clone an exact, host-owned ref.
    bound = git.run(
        [
            "git",
            "-C",
            str(repository),
            "update-ref",
            "refs/heads/verification",
            published_sha,
        ],
        check=False,
    )
    if bound.returncode != 0:
        return bound
    cloned = git.run(
        [
            "git",
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            "--branch",
            "verification",
            "--",
            str(repository),
            str(checkout),
        ],
        check=False,
    )
    if cloned.returncode != 0:
        return cloned
    return git.run(
        ["git", "-C", str(checkout), "checkout", "--detach", "--force", published_sha],
        check=False,
    )


class CommandCondition:
    """Run a configured command against the admitted verification tree.

    Canonical mode contains execution over a host-owned copy of the published commit.
    Checkout mode uses the agent tree only after explicit admission.
    The operator command runs through allowlisted ``sh -c``.

    Timeout cleanup: ``subprocess_utils.run`` does not start a new session, so a
    bare ``sh -c`` that times out would orphan grandchildren. When a timeout is
    requested AND the allowlisted ``timeout``/``gtimeout`` tool is available, we
    wrap the command as ``timeout -k 5 <secs> sh -c "<command>"`` so the whole
    process group is signalled. Without the tool we fall back to
    ``subprocess_utils.run(timeout=...)`` and accept the orphan risk.
    """

    requires_uncontained_host_execution = True

    def __init__(
        self,
        command: str,
        *,
        timeout: float | None = None,
        needs_writes: bool = False,
    ) -> None:
        self._command = command
        self._timeout = timeout
        self._needs_writes = needs_writes
        self.admission: HostVerificationAdmission | None = None
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("verification timeout must be finite and positive")

    def _build_cmd(self) -> tuple[list[str], float | None]:
        """Return (argv, subprocess_timeout). When the timeout tool is used it
        owns the deadline, so the subprocess-level timeout is left as a wider
        safety net (timeout + grace + slack)."""
        base = ["sh", "-c", self._command]
        if self._timeout is None:
            return base, None
        timeout_tool = subprocess_utils.find_timeout_cmd()
        if timeout_tool is not None:
            # `timeout -k <grace> <secs> sh -c "<cmd>"`: -k sends SIGKILL to the
            # process group <grace>s after the initial SIGTERM. Keep a wider
            # subprocess-level deadline as a backstop in case the tool hangs.
            wrapped = [timeout_tool, "-k", "5", str(self._timeout), *base]
            return wrapped, self._timeout + 10
        return base, self._timeout

    def check(
        self,
        *,
        worktree: Path,
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
    ) -> ConditionResult:
        admission = self.admission if published_sha is not None else None
        with proxy_redaction_scope(_verification_proxy_environment(admission)):
            refusal = (
                admission.refusal
                if admission is not None
                else _host_verification_refusal(
                    agent_written_checkout=agent_written_checkout
                    or published_sha is not None,
                    dangerously_skip_containment=dangerously_skip_containment,
                )
            )
            if refusal is not None:
                return ConditionResult(met=False, evidence=refusal)
            if published_sha is not None:
                return self._check_published(
                    published_sha=published_sha,
                    publication_base_sha=publication_base_sha,
                    publication_remote=publication_remote,
                )
            return self.check_directory(worktree)

    def _check_published(
        self,
        *,
        published_sha: str,
        publication_base_sha: str | None,
        publication_remote: str | None,
    ) -> ConditionResult:
        """Run the command in a host-owned tree for the published commit."""
        if not publication_base_sha or not publication_remote:
            return ConditionResult(
                met=False,
                evidence="canonical verify checkout lacks a base or remote",
            )
        try:
            with work_dirs.work_tempdir("kew-loop-verification-") as temp:
                root = Path(temp)
                git = HostGitPreparation(root)
                repo = provision(
                    root / "repo",
                    source=publication_remote,
                    base_sha=publication_base_sha,
                    git=git,
                )
                if published_sha != publication_base_sha:
                    fetch_commit(
                        repo, source=publication_remote, sha=published_sha, git=git
                    )
                checkout = root / "checkout"
                materialized = _materialize_verification_checkout(
                    repo.path, checkout, published_sha, git
                )
                if materialized.returncode != 0:
                    detail = _truncate(materialized.stderr or materialized.stdout or "")
                    return ConditionResult(
                        met=False,
                        evidence=f"canonical verify checkout failed: {detail}",
                    )
                if (
                    self.admission is not None
                    and self.admission.posture == CONTAINED_VERIFICATION_POSTURE
                ):
                    return self._check_contained(
                        checkout,
                        self.admission,
                        timeout=min(
                            self._requested_timeout(), VERIFICATION_TIMEOUT_SECONDS
                        ),
                    )
                return self.check_directory(checkout)
        except (OSError, RuntimeError, ValueError) as exc:
            return ConditionResult(
                met=False,
                evidence=f"canonical verify checkout failed: {_truncate(str(exc))}",
            )

    def _requested_timeout(self) -> float:
        """The caller's budget, defaulted — never None, so a cap can apply."""
        return self._timeout or VERIFICATION_TIMEOUT_SECONDS

    def _check_contained(
        self,
        checkout: Path,
        admission: HostVerificationAdmission,
        *,
        timeout: float,
    ) -> ConditionResult:
        """Execute only inside the admitted image, including on launch failure.

        *timeout* is the caller's budget, already capped by whatever policy owns
        it: the loop gate caps an operator's ``verify_loop`` timeout at
        ``VERIFICATION_TIMEOUT_SECONDS``, while the merge queue's budget
        (``MERGE_VERIFY_TIMEOUT_SECONDS``) is its own constant, not operator
        input, and a full combined-tree gate legitimately outlives ten minutes.

        Every non-exit-0 outcome distinguishes "the command failed" from "the
        command could not be run": a launch the runtime refused, a quota, a
        client failure and a timeout all set ``unrunnable``/``timed_out``, which
        is what keeps the merge queue from recording a container problem as a
        defect in someone's PR.
        """
        assert admission.runtime is not None and admission.image is not None
        argv = build_checkout_program_argv(
            runtime=admission.runtime,
            image=admission.image,
            checkout_dir=checkout,
            slug=f"verify-{uuid.uuid4().hex}",
            program=["/bin/sh", "-c", self._command],
            uid=os.getuid(),
            gid=os.getgid(),
            checkout_writable=self._needs_writes,
            scratch_paths=admission.scratch_paths,
        )
        try:
            result = run_container_with_cpu_cap_retry(
                argv, timeout=timeout, output_limit=VERIFICATION_OUTPUT_BYTES
            )
        except subprocess_utils.OutputBudgetExceeded as exc:
            output = _contained_output_evidence(exc.stdout, exc.stderr, truncated=True)
            return ConditionResult(
                False,
                f"{exc}: {output}",
                unrunnable=True,
                output_overflowed=True,
                output=_whole_output(
                    _complete_output_tail(exc.stdout), _complete_output_tail(exc.stderr)
                ),
            )
        except sandbox.CheckoutQuotaExceeded as exc:
            return ConditionResult(False, _truncate(str(exc)), unrunnable=True)
        except subprocess.TimeoutExpired:
            return ConditionResult(
                False, f"command timeout after {timeout}s", timed_out=True
            )
        except (OSError, ValueError) as exc:
            return ConditionResult(
                False,
                f"command not runnable: {_truncate(redact_credentials(str(exc)))}",
                unrunnable=True,
            )
        failure = sandbox.container_client_failure(result)
        evidence = (
            f"command exited {result.returncode}: "
            f"{failure + ': ' if failure else ''}"
            f"{_contained_output_evidence(result.stdout, result.stderr)}"
        )
        if failure is not None:
            return ConditionResult(False, evidence, unrunnable=True)
        return ConditionResult(
            met=result.returncode == 0,
            evidence=evidence,
            exit_code=result.returncode,
            output=_whole_output(result.stdout, result.stderr),
        )

    def check_admitted_directory(
        self, worktree: Path, admission: HostVerificationAdmission
    ) -> ConditionResult:
        """Run the command over *worktree* under an already-resolved admission.

        The seam the serial merge queue (#2148) uses for its combined tree. The
        queue resolves the SAME admission the loop gate resolves
        (:func:`resolve_command_verification_admission`), so the default posture
        is containment: the command executes inside the approved image, over a
        checkout mounted into it, with no network and none of the host's
        environment — never as a host child of the driver that holds the merge
        credentials. A refusal is returned as an unrunnable result rather than
        executed anyway, and a host posture is only ever reached when the
        operator's own ``dangerously_skip_containment`` produced one.
        """
        with proxy_redaction_scope(_verification_proxy_environment(admission)):
            if admission.refusal is not None:
                return ConditionResult(
                    met=False, evidence=admission.refusal, unrunnable=True
                )
            if admission.posture == CONTAINED_VERIFICATION_POSTURE:
                return self._check_contained(
                    worktree, admission, timeout=self._requested_timeout()
                )
            return self._run_in_directory(worktree)

    def check_directory(self, worktree: Path) -> ConditionResult:
        """Run the configured command in one admitted directory.

        Public because the serial merge queue (#2148) runs the same
        operator-configured command over the combined tree it materializes, and
        one definition of "run the verify command with a timeout that does not
        orphan its process group" is the point.

        The redaction scope is entered HERE, not only in ``check``: a proxy URL's
        path, query and fragment segments are credential forms only inside it
        (:func:`kew.sandbox.proxy_redaction_scope`), and this evidence is
        *retained* — the merge queue writes it to a review sidecar under
        ``logging.dir`` and feeds it to a remediation agent. Scoping the
        primitive rather than each call site is what keeps a direct caller from
        leaving it out.
        """
        with proxy_redaction_scope(_verification_proxy_environment(None)):
            return self._run_in_directory(worktree)

    def _run_in_directory(self, worktree: Path) -> ConditionResult:
        """Execute the command and read its outcome as evidence."""
        argv, sp_timeout = self._build_cmd()
        try:
            result = subprocess_utils.run(
                argv,
                capture=True,
                check=False,
                cwd=worktree,
                timeout=sp_timeout,
            )
        except subprocess.TimeoutExpired:
            return ConditionResult(
                met=False,
                evidence=f"command timeout after {self._timeout}s: {self._command}",
                timed_out=True,
            )
        except ValueError as exc:
            # sh/timeout not on the allowlist (preflight_tools not run / no shell).
            return ConditionResult(
                met=False,
                evidence=f"command not runnable: {exc}",
                unrunnable=True,
            )

        output = _truncate((result.stdout or "") + (result.stderr or ""))
        # `timeout` exits 124 when it kills the command for overrunning.
        if result.returncode == 124 and self._timeout is not None:
            return ConditionResult(
                met=False,
                evidence=f"command timeout after {self._timeout}s: {self._command}",
                timed_out=True,
            )
        if result.returncode == 0:
            return ConditionResult(met=True, evidence="command exited 0")
        return ConditionResult(
            met=False,
            evidence=f"command exited {result.returncode}: {output}",
            exit_code=result.returncode,
            output=_whole_output(result.stdout, result.stderr),
        )


def _complete_output_tail(text: str) -> str:
    marker = subprocess_utils.BoundedOutput.marker.decode()
    if marker in text:
        # The tail starts at an arbitrary byte. Drop its first partial line.
        text = text.rsplit(marker, 1)[1].partition("\n")[2]
    return text.rpartition("\n")[0]


def _contained_output_evidence(
    stdout: str, stderr: str, *, truncated: bool = False
) -> str:
    if truncated:
        # Overflow can split a credential at the last byte. Omit unfinished
        # lines before redaction so their credential fragments cannot escape.
        stdout = _complete_output_tail(stdout)
        stderr = _complete_output_tail(stderr)
    # Redact before slicing, so truncation cannot retain fragments of a secret.
    stdout = _sanitize_evidence(stdout or "")[-_MAX_EVIDENCE_CHARS // 2 :]
    stderr = _sanitize_evidence(stderr or "")[-_MAX_EVIDENCE_CHARS // 2 :]
    # Keep stream fields separate when ConditionResult sanitizes the final text.
    return f"stdout: {stdout}\nstderr: {stderr}"


class CiGreenCondition:
    """Gate a loop on CI for its exact canonical publication.

    Canonical mode receives the recorded published SHA from the driver.
    Checkout mode retains its admitted ``git rev-parse HEAD`` fallback.
    Every non-success conclusion fails closed.
    """

    requires_uncontained_host_execution = False

    def __init__(
        self,
        *,
        repo: str,
        timeout_secs: int,
        gated_checks: Sequence[str],
        _wait: Callable[..., CiResult] = wait_for_ci,
    ) -> None:
        self._repo = repo
        self._timeout_secs = timeout_secs
        self._gated_checks = tuple(gated_checks)
        self._wait = _wait

    def check(
        self,
        *,
        worktree: Path,
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
    ) -> ConditionResult:
        sha = published_sha
        if sha is None:
            refusal = _host_verification_refusal(
                agent_written_checkout=agent_written_checkout,
                dangerously_skip_containment=dangerously_skip_containment,
            )
            if refusal is not None:
                return ConditionResult(met=False, evidence=refusal)
            try:
                res = subprocess_utils.run(
                    ["git", "rev-parse", "HEAD"],
                    capture=True,
                    check=False,
                    cwd=worktree,
                )
            except ValueError as exc:
                return ConditionResult(met=False, evidence=f"git not runnable: {exc}")
            sha = (res.stdout or "").strip()
            if res.returncode != 0 or not sha:
                return ConditionResult(met=False, evidence="cannot resolve HEAD sha")
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            return ConditionResult(met=False, evidence="published sha is invalid")
        result = self._wait(
            self._repo,
            sha,
            timeout_secs=self._timeout_secs,
            gated_checks=self._gated_checks,
        )
        return ConditionResult(
            met=result.conclusion == "success",
            evidence=result.evidence,
            waiting=result.conclusion == "awaiting_approval",
        )


@dataclass(frozen=True)
class GateVerdict:
    """The gate's terminal judgment for a loop.

    ``passed`` is the ONLY authority to stamp ``achieved`` — Plan 2's driver
    *returns from* (maps) this verdict onto the loop's terminal status and never
    closes on the agent's self-report (the anti-proxy invariant). The gate only
    RETURNS the verdict; the driver does the ``achieved`` / ``needs_human``
    routing. ``evidence`` flows to ``loop.status_reason``; ``sabotage`` marks a
    Safety-C2 diff-guard trip.
    """

    passed: bool
    evidence: str
    sabotage: bool


class Gate(Protocol):
    """Injectable gate interface — Plan 2's driver types its param as ``Gate``.

    ``GateEvaluator`` below satisfies this structurally; tests inject fakes.
    """

    def evaluate(
        self,
        *,
        worktree: Path,
        base_ref: str,
        changed_paths: list[str],
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
        extra_guarded_paths: Sequence[str] = (),
    ) -> GateVerdict: ...


# ALWAYS-guarded paths (Safety-C2). evaluate() unions these with the caller's
# guarded_paths, so a caller passing [] cannot disable sabotage protection. The
# verify-script path itself is loop-specific and appended by the driver (Plan 2).
# These globs use path-aware semantics (`*` does NOT cross `/`; `**` does) via
# the glob->regex translation in _matches_guarded (Task 3). Test and
# agent-instruction globs match at any depth: a monorepo keeps tests under
# `kew/tests/`, and colocated tests sit beside the code they cover.
DEFAULT_GUARDED_PATHS: list[str] = [
    "**/tests/**",  # any tests/ tree (incl. a brand-new always-passing file)
    "**/test_*.py",  # pytest-named test modules outside a tests/ tree
    "**/*_test.py",  # colocated Python tests (src/foo_test.py)
    "**/*.test.*",  # colocated JS/TS tests (x.test.ts)
    "**/*.spec.*",  # colocated spec files (x.spec.ts)
    "**/AGENTS.md",  # agent instructions: an agent must not rewrite its rules
    "**/CLAUDE.md",
    "**/conftest.py",  # pytest fixtures/collection hooks at any depth
    "**/ratchets/**",  # ratchet ledgers: an agent must not widen its own exceptions
    ".github/workflows/**",  # CI config at any depth
]


def _glob_token(pattern: str, i: int) -> tuple[str, int]:
    """Translate the glob token at ``pattern[i]`` to a regex fragment.

    Returns ``(regex_fragment, next_index)``. Path-aware semantics:
    ``**/`` → optional leading segments, ``**`` → any chars (crosses ``/``),
    ``*`` → any chars except ``/``, ``?`` → one char except ``/``.
    """
    c = pattern[i]
    if c == "*" and pattern[i + 1 : i + 2] == "*":
        if pattern[i + 2 : i + 3] == "/":
            return "(?:.*/)?", i + 3
        return ".*", i + 2
    if c == "*":
        return "[^/]*", i + 1
    if c == "?":
        return "[^/]", i + 1
    return re.escape(c), i + 1


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX-path glob to an anchored regex (see ``_glob_token``)."""
    parts: list[str] = ["(?s:"]
    i = 0
    while i < len(pattern):
        fragment, i = _glob_token(pattern, i)
        parts.append(fragment)
    parts.append(r")\Z")
    return re.compile("".join(parts))


def _matches_guarded(changed_paths: list[str], guarded_paths: list[str]) -> list[str]:
    """Return the changed paths that match ANY guarded glob.

    Glob matching is path-aware (``*`` does not cross ``/``; ``**`` does) — see
    ``_glob_to_regex``. The caller's ``guarded_paths`` are unioned with
    ``DEFAULT_GUARDED_PATHS`` by ``evaluate`` before this is called, so an empty
    caller list still protects test/conftest/CI paths.
    """
    compiled = [_glob_to_regex(p) for p in guarded_paths]
    hits: list[str] = []
    for path in changed_paths:
        if any(rx.match(path) for rx in compiled):
            hits.append(path)
    return hits


class GateEvaluator:
    """AND-composes stopping conditions and applies the Safety-C2 diff-guard.

    ``passed`` is True only when EVERY condition is met AND the loop's diff did
    not touch any guarded path. An empty condition list never passes (no
    vacuous truth — that would defeat the anti-proxy invariant). Satisfies the
    ``Gate`` Protocol structurally.
    """

    def __init__(
        self, conditions: list[StoppingCondition], *, guarded_paths: list[str]
    ) -> None:
        self._conditions = conditions
        # Defensive copy: a caller mutating their list later must not be able to
        # shrink our guarded set and silently disable sabotage protection.
        self._guarded_paths = list(guarded_paths)
        self.verification_admission: HostVerificationAdmission | None = None

    def prepare_verification(self, config: SandboxConfig, *, runner: str) -> None:
        """Bind command conditions to the loop runner's admitted image."""
        commands = [
            condition
            for condition in self._conditions
            if isinstance(condition, CommandCondition)
        ]
        if not commands:
            return
        self.verification_admission = resolve_command_verification_admission(
            config, runner=runner
        )
        for condition in commands:
            condition.admission = self.verification_admission

    @property
    def requires_uncontained_host_execution(self) -> bool:
        """Return whether any condition executes the candidate project locally."""
        return any(
            bool(getattr(condition, "requires_uncontained_host_execution", True))
            for condition in self._conditions
        )

    def evaluate(
        self,
        *,
        worktree: Path,
        base_ref: str,
        changed_paths: list[str],
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
        extra_guarded_paths: Sequence[str] = (),
    ) -> GateVerdict:
        # Safety-C2 diff-guard (the v1 defense). base_ref is reserved for the
        # deferred trusted-ref execution overlay (spec R12); v1 trusts the
        # diff-guard over changed_paths. ALWAYS union the caller's guard set with
        # DEFAULT_GUARDED_PATHS so a caller passing [] cannot disable protection.
        # Run the guard FIRST so a sabotaging diff is labelled sabotage, not
        # masked by the command's exit code.
        # ``extra_guarded_paths`` are this run's own: the node's acceptance
        # test file (#2729), which the implementer may read but never edit.
        guards = [*self._guarded_paths, *DEFAULT_GUARDED_PATHS, *extra_guarded_paths]
        tampered = _matches_guarded(changed_paths, guards)
        if tampered:
            return GateVerdict(
                passed=False,
                evidence=f"sabotage: diff edits {', '.join(sorted(tampered))}",
                sabotage=True,
            )
        if not self._conditions:
            return GateVerdict(
                passed=False,
                evidence="gate_failed: no conditions configured",
                sabotage=False,
            )
        evidence: list[str] = []
        for condition in self._conditions:
            result = condition.check(
                worktree=worktree,
                agent_written_checkout=agent_written_checkout,
                dangerously_skip_containment=dangerously_skip_containment,
                published_sha=published_sha,
                publication_base_sha=publication_base_sha,
                publication_remote=publication_remote,
            )
            evidence.append(result.evidence)
            if not result.met:
                outcome = "gate_waiting" if result.waiting else "gate_failed"
                return GateVerdict(
                    passed=False,
                    evidence=f"{outcome}: {result.evidence}",
                    sabotage=False,
                )
        return GateVerdict(
            passed=True, evidence="gate passed: " + "; ".join(evidence), sabotage=False
        )
