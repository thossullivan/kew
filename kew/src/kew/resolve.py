"""Merge conflict resolution dispatch for agent PRs.

Detects merge conflicts on PR branches and dispatches an agent
to resolve them, using the configured strategy (merge or rebase).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Literal

from jinja2 import Environment, FileSystemLoader

from kew import _TEMPLATES_DIR
from kew.audit import Actor, emit
from kew.command_cleanup import finish_command_cleanup
from kew.command_outcome import PR_HEAD_MOVED, CommandOutcome
from kew.config import DispatchConfig
from kew.dispatch_core import (
    WORKER_CAP_REACHED_MESSAGE,
    GitHubAuthError,
    WorkerCapReached,
    _check_github_connectivity,
    budget_watchdog,
    revoke_ci_approval,
    run_tracked_agent,
)
from kew.dispatch_core import (
    resolve_issue_number as _resolve_issue_number,
)
from kew.inference_transport import TransportAdmissionError, resolve_inference_transport
from kew.merge_claim import wait_out_merge_claim
from kew.progress import _error, _log, _success, _warn
from kew.prompt import fence_untrusted, fence_vocabulary_filter, make_sentinel
from kew.prompt_addendum import (
    PROMPT_CONFIGURATION_REFUSAL,
    ProfileAddendum,
    PromptConfigurationError,
    profile_version_id,
)
from kew.prompt_refusals import record_prompt_refusal
from kew.publication import PublicationError, select_publication_mode
from kew.publication_canonical import (
    AcceptanceContract,
    CanonicalNoChange,
    PublicationHeld,
    acceptance_contracts,
    host_commit_identity,
    publication_remote,
    publish_checkout,
    remote_branch_sha,
    remote_commit_is_ancestor,
)
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    ensure_publication_credentials,
    publication_output_guard,
    publication_producer_for_head,
)
from kew.remediation_containment import (
    RemediationContainment,
    record_remediation_refusal,
    resolve_remediation_containment,
)
from kew.resolve_checks import (
    ResolutionChecksFailed,
    record_resolution_evidence,
    run_resolution_checks,
)
from kew.run_store import RESOLVE_RUN_KIND
from kew.runners import resolve_runner
from kew.sandbox import (
    POSTURE_SANDBOXED,
    SandboxRuntimeUnavailable,
    attest_flow_posture,
)
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import run as subprocess_run
from kew.tracking import LaunchState, command_outcome, startup_refused
from kew.worktree import (
    ConflictInfo,
    _checkout_read_command,
    _commit_publication_worktree,
    _fetched_base_sha,
    _intent_item_id,
    _intent_recorder,
    checkout_existing_worktree,
    checkout_no_fetch_env,
    cleanup_worktree,
    has_uncommitted_changes,
    publication_remote_sha,
    publish_existing_pr_branch,
    release_checkout_claims,
    require_pinned_pr_head,
)

if TYPE_CHECKING:
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.run_store import RunStore
    from kew.runners import ResolvedRoute
    from kew.tracking import DispatchResult


def _get_pr_metadata(pr_number: int, repo: str) -> dict[str, Any] | None:
    """Fetch PR metadata including mergeable state via gh pr view."""
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
                "number,headRefName,headRefOid,baseRefName,body,title,state,mergeable",
            ],
            check=True,
        )
        data: dict[str, Any] = json.loads(result.stdout)
        return data
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def _build_resolve_prompt(
    *,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    base_branch: str,
    conflict_info: ConflictInfo,
    config: DispatchConfig,
    host_publication: bool = False,
    profile_addendum: ProfileAddendum | None = None,
) -> str:
    """Build the resolve prompt from Jinja2 template.

    *profile_addendum* renders once, under its own heading, before the safety
    and publication sections; without one the bytes are the base render's.
    """
    # Fence the attacker-influenced conflicted paths with a per-render sentinel
    # (H7); the unfenceable branch names lose the fence vocabulary at the render
    # boundary instead (#2454 round 4).
    sentinel = make_sentinel()
    files_text = "\n".join(f"- `{f}`" for f in conflict_info.conflicted_files)
    conflicted_files_fenced = fence_untrusted(files_text, sentinel)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        finalize=fence_vocabulary_filter(sentinel),
    )
    template = env.get_template("resolve.md.j2")
    return template.render(
        pr_number=pr_number,
        issue_number=issue_number,
        branch_name=branch_name,
        base_branch=base_branch,
        strategy=conflict_info.strategy_used,
        conflicted_files=conflict_info.conflicted_files,
        conflicted_files_fenced=conflicted_files_fenced,
        repo=config.project.repo,
        commands_ci=config.commands.ci,
        commands_format=config.commands.format,
        host_publication=host_publication,
        profile_addendum=profile_addendum,
    )


def _host_publication_required(publication_style: str, *, contained: bool) -> bool:
    """Return whether the host must publish the resolved branch."""
    return publication_style == "host" or contained


def _canonical_or_host_publication(
    mode: Literal["canonical", "checkout"],
    publication_style: str,
    *,
    contained: bool,
) -> bool:
    """Require the host whenever checkout verification is forbidden."""
    return mode == "canonical" or _host_publication_required(
        publication_style, contained=contained
    )


def _publication_extraction_boundary(
    config: DispatchConfig, containment: Any
) -> tuple[str | None, str | None]:
    """Use container extraction only when this flow launched an image."""
    if containment.image is None:
        return None, None
    return config.sandbox.runtime, containment.image


def _worktree_head_sha(worktree_dir: Path) -> str:
    """Return the local commit, or an empty string when Git cannot resolve it."""
    result = _checkout_git(worktree_dir, "rev-parse", "--verify", "HEAD^{commit}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _checkout_git(worktree_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Read the agent's checkout with hardened git.

    The agent can write the checkout's config, and plain git runs programs it
    names (``core.fsmonitor`` on an index read, hooks): these reads run on the
    host after the agent, so each one switches them off (#3499). None of them
    hashes a working-tree file, so no filter driver runs. The environment is
    the operator's own, as before: the marker-size check must resolve the
    attributes git used when it wrote the conflict, a global
    ``core.attributesFile`` included, but lazy fetch is off: a partial-clone
    config would otherwise run its transport on a missing object.
    """
    return subprocess_run(
        _checkout_read_command(worktree_dir, *args),
        check=False,
        env=checkout_no_fetch_env(os.environ),
    )


def _is_ancestor(worktree_dir: Path, ancestor: str, descendant: str) -> bool:
    """Whether *ancestor* is an ancestor of *descendant* in the checkout."""
    ancestry = _checkout_git(
        worktree_dir, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return ancestry.returncode == 0


_CHECK_ATTR_CHUNK = 400


def _assert_standard_conflict_marker_size(worktree_dir: Path) -> None:
    """Refuse a checkout whose EFFECTIVE conflict-marker-size is below seven.

    The marker scan looks for runs of seven or more, git's default. An
    effective ``conflict-marker-size`` of one to six on any tracked path —
    from a tracked ``.gitattributes``, ``info/attributes``, or a global
    ``core.attributesFile`` — makes git emit markers the scan cannot see,
    and a three-character scan would flag every doctest prompt in the tree.
    ``git check-attr`` answers with the value git itself resolves, so every
    spelling (``03``, ``+3``) and every source is covered at once.
    """
    listed = _checkout_git(worktree_dir, "ls-files", "-z")
    if listed.returncode != 0:
        raise RuntimeError("cannot list the checkout's tracked paths")
    paths = [entry for entry in listed.stdout.split("\0") if entry]
    for start in range(0, len(paths), _CHECK_ATTR_CHUNK):
        chunk = paths[start : start + _CHECK_ATTR_CHUNK]
        attrs = _checkout_git(
            worktree_dir, "check-attr", "-z", "conflict-marker-size", "--", *chunk
        )
        if attrs.returncode != 0:
            raise RuntimeError("cannot resolve the checkout's conflict-marker-size")
        fields = attrs.stdout.split("\0")
        for value in fields[2::3]:
            if _marker_size_below_seven(value):
                raise RuntimeError(
                    "the checkout sets conflict-marker-size below 7 "
                    "(or to a value git may not read as a number)"
                )


def _marker_size_below_seven(value: str) -> bool:
    """Whether a ``check-attr`` value resolves to a size git would treat as 1..6.

    A value that is not a number cannot be proven to leave the default in
    place, so it counts as short: the scan must not trust a marker width it
    cannot compute.
    """
    if value in ("unspecified", "unset", "set", ""):
        return False
    try:
        size = int(value.strip(), 10)
    except ValueError:
        _warn(f"conflict-marker-size {value!r} is not a number; refusing the checkout")
        return True
    return 1 <= size <= 6


def _assert_resolved_worktree(worktree_dir: Path) -> None:
    """Refuse host publication when Git still reports conflict state."""
    unmerged = _checkout_git(worktree_dir, "ls-files", "-u")
    if unmerged.returncode != 0:
        raise RuntimeError("cannot inspect the worktree for unmerged entries")
    if unmerged.stdout.strip():
        raise RuntimeError("the worktree still has unmerged entries")

    _assert_standard_conflict_marker_size(worktree_dir)
    # Only the opening, base and closing markers identify a conflict: git
    # writes all of them together, so a lone `=======` line is never one on
    # its own — it is a reStructuredText table rule, a Markdown setext
    # underline, or a banner comment, and this repository carries several.
    markers = _checkout_git(
        worktree_dir, "grep", "-I", "-n", "-E", r"^(<{7,}|\|{7,}|>{7,})( |$)", "--"
    )
    if markers.returncode == 0:
        raise RuntimeError("a tracked file still contains conflict markers")
    if markers.returncode != 1:
        raise RuntimeError("cannot inspect tracked files for conflict markers")
    _assert_no_short_marker_blocks(worktree_dir)


# Widths are matched independently and the divider may carry trailing
# whitespace: a runner that touches one marker of a block has still left
# the conflict in place, and the block shape — an opening, then a divider,
# then a closing — is what identifies it.
_SHORT_OPENING = re.compile(r"^<{1,6}(?!<)( |$)")
_ANY_DIVIDER = re.compile(r"^=+[ \t]*$")
_SHORT_CLOSING = re.compile(r"^>{1,6}(?!>)( |$)")


def _file_has_short_marker_block(text: str) -> bool:
    """Whether *text* holds a complete conflict block below the default width.

    A block is a short opening line, then a divider, then a short closing
    line, in that order. Single lines are not enough: ``>>> x`` is a doctest
    prompt and ``===`` a heading rule, and neither ever appears as a block.
    """
    stage = 0
    for line in text.splitlines():
        if stage == 0 and _SHORT_OPENING.match(line):
            stage = 1
        elif stage == 1 and _ANY_DIVIDER.match(line):
            stage = 2
        elif stage == 2 and _SHORT_CLOSING.match(line):
            return True
    return False


def _assert_no_short_marker_blocks(worktree_dir: Path) -> None:
    """Refuse residual conflict blocks written at a marker width below seven.

    The attribute check proves the width git would use NOW; git may already
    have written narrower markers under a rule the runner then removed. A
    complete block at any width 1..6 in any tracked text file is that
    residue, whatever the current attributes say.
    """
    listed = _checkout_git(worktree_dir, "ls-files", "-z")
    if listed.returncode != 0:
        raise RuntimeError("cannot list the checkout's tracked paths")
    for entry in listed.stdout.split("\0"):
        if not entry:
            continue
        path = worktree_dir / entry
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue  # binary by git's own heuristic; its -I skips these too
        # A byte-order mark would sit in front of a first-line marker and
        # defeat the anchored pattern; invalid UTF-8 is still text to git,
        # so bad bytes are escaped rather than making the file unscannable.
        text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "surrogateescape")
        if _file_has_short_marker_block(text):
            raise RuntimeError(
                f"a tracked file still contains conflict markers ({entry})"
            )


def _publish_resolved_branch(
    *,
    worktree_dir: Path,
    branch_name: str,
    expected_head_sha: str,
    base_sha: str,
    base_branch: str,
    repo: str,
    commit_message: str,
    force_with_lease: bool,
    publish: Literal["canonical", "checkout"] = "canonical",
    dangerously_skip_containment: bool = False,
    runtime: str | None = None,
    image: str | None = None,
    proxy_environment: str = "none",
    store: RunStore | None = None,
    run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> None:
    """Publish a resolution as a merge commit from a host-owned repository.

    Canonical publication always creates a merge commit. A rebase checkout's
    rewritten history cannot be reproduced from file data alone.
    """
    if not expected_head_sha:
        raise RuntimeError("the PR head SHA is missing")
    if not base_sha:
        raise RuntimeError("the fetched base SHA is missing")
    mode = select_publication_mode(
        publish,
        dangerously_skip_containment=dangerously_skip_containment,
        branch=branch_name,
        repo=repo,
    )
    if mode == "canonical":
        _publish_resolved_branch_canonical(
            worktree_dir=worktree_dir,
            branch_name=branch_name,
            expected_head_sha=expected_head_sha,
            base_sha=base_sha,
            base_branch=base_branch,
            repo=repo,
            commit_message=commit_message,
            runtime=runtime,
            image=image,
            proxy_environment=proxy_environment,
            store=store,
            run_id=run_id,
            before_push=before_push,
        )
        return

    _assert_resolved_worktree(worktree_dir)
    # Commit first, prove second, push last. When the runner leaves edits
    # uncommitted, the host commit is the resolution commit, and it must pass
    # the same ancestry proof as a runner-made commit BEFORE anything reaches
    # the remote. Proving only the pre-commit HEAD let an aborted merge plus a
    # stray file publish a non-resolution commit that the later verification
    # then rejected — after the remote had already moved.
    _commit_publication_worktree(worktree_dir, commit_message)
    # The pre-commit check only sees tracked files; the host commit has just
    # tracked whatever the runner left untracked, so prove the tree again.
    _assert_resolved_worktree(worktree_dir)
    local_head = _worktree_head_sha(worktree_dir)
    if not local_head or local_head == expected_head_sha:
        raise RuntimeError("the resolution did not create a new local commit")
    _assert_resolution_ancestry(
        worktree_dir=worktree_dir,
        local_head=local_head,
        expected_head_sha=expected_head_sha,
        base_sha=base_sha,
    )
    published = publish_existing_pr_branch(
        worktree_dir,
        branch_name,
        commit_message=commit_message,
        force_with_lease=force_with_lease,
        expected_remote_sha=expected_head_sha,
        repo=repo,
        publish="checkout",
        dangerously_skip_containment=dangerously_skip_containment,
        store=store,
        run_id=run_id,
        before_push=before_push,
    )
    _verify_resolved_branch(
        worktree_dir=worktree_dir,
        branch_name=branch_name,
        expected_head_sha=expected_head_sha,
        base_sha=base_sha,
        repo=repo,
        published=bool(published),
    )


def _resolve_acceptance(
    store: RunStore | None, run_id: str | None, branch_name: str
) -> tuple[AcceptanceContract, ...]:
    """The PR's acceptance contracts, read for the item the resolve run names.

    A resolution is agent-written like any remediation, so a merge that edits
    the node's acceptance test is held, never pushed (#2729).
    """
    item = _intent_item_id(store, run_id=run_id, item_id=None, branch=branch_name)
    return acceptance_contracts(store, item)


def _publish_resolved_branch_canonical(
    *,
    worktree_dir: Path,
    branch_name: str,
    expected_head_sha: str,
    base_sha: str,
    base_branch: str,
    repo: str,
    commit_message: str,
    runtime: str | None,
    image: str | None,
    proxy_environment: str,
    store: RunStore | None = None,
    run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> None:
    remote = publication_remote(repo)
    with TemporaryDirectory(prefix="kew-resolve-publication-") as temp:
        scratch = Path(temp)
        branch_head = remote_branch_sha(
            remote, branch_name, scratch=scratch / "branch-query"
        )
        observed_base = remote_branch_sha(
            remote, base_branch, scratch=scratch / "base-query"
        )
        if branch_head != expected_head_sha:
            raise PublicationError(
                "push", "the PR head moved before resolved publication"
            )
        _require_base_fast_forward(
            remote,
            base_sha=base_sha,
            observed_base=observed_base,
            scratch=scratch / "base-ancestry",
        )
        base_already_integrated = remote_commit_is_ancestor(
            remote,
            base_sha,
            branch_head,
            scratch=scratch / "ancestry",
        )
        try:
            publication = publish_checkout(
                worktree_dir,
                remote=remote,
                branch=branch_name,
                parents=(branch_head, base_sha),
                base_sha=branch_head,
                message=commit_message,
                identity=host_commit_identity(),
                force_with_lease=f"{branch_name}:{expected_head_sha}",
                runtime=runtime,
                image=image,
                proxy_environment=proxy_environment,
                require_resolved=True,
                publish_empty_tree=not base_already_integrated,
                before_push=before_push,
                acceptance=_resolve_acceptance(store, run_id, branch_name),
                on_intent=_intent_recorder(
                    store,
                    run_id=run_id,
                    item_id=None,
                    branch=branch_name,
                    parent_sha=branch_head,
                ),
                output_guard=publication_output_guard(store, run_id=run_id),
                scratch=scratch / "publish",
            )
        except PublicationHeld as exc:
            raise PublicationError("push", exc) from exc
    if isinstance(publication, CanonicalNoChange):
        raise PublicationError("commit", "the resolution produced no changed tree")
    if publication.parent_shas[0] != expected_head_sha:
        raise PublicationError("commit", "the resolution has the wrong first parent")


def _require_base_fast_forward(
    remote: str, *, base_sha: str, observed_base: str, scratch: Path
) -> None:
    """Refuse publication unless the base still holds the resolved-against commit.

    A base that only moved forward keeps the resolution correct: the merge
    with parents ``(branch_head, base_sha)`` integrates part of the base, and
    the next merge-main integrates the commits after ``base_sha`` (#2654).
    A rewritten base drops ``base_sha`` from its history, and publishing a
    merge of it would bring abandoned commits back.
    """
    if not observed_base:
        raise PublicationError("push", "the base branch head is unavailable")
    if observed_base == base_sha:
        return
    if not remote_commit_is_ancestor(remote, base_sha, observed_base, scratch=scratch):
        raise PublicationError(
            "push",
            f"the base branch was rewritten from {base_sha[:12]} to "
            f"{observed_base[:12]} before resolved publication",
        )
    _warn(
        f"The base branch advanced from {base_sha[:12]} to {observed_base[:12]} "
        f"during the resolution; publishing the merge of {base_sha[:12]} and "
        "leaving the newer commits to the next base integration."
    )


def _assert_resolution_ancestry(
    *,
    worktree_dir: Path,
    local_head: str,
    expected_head_sha: str,
    base_sha: str,
) -> None:
    """Require the resolution commit to integrate both recorded inputs."""
    for ancestor, label in (
        (expected_head_sha, "recorded PR head"),
        (base_sha, "fetched base"),
    ):
        if not ancestor:
            raise RuntimeError(f"the {label} SHA is missing")
        if not _is_ancestor(worktree_dir, ancestor, local_head):
            raise RuntimeError(f"the published resolution omits the {label}")


def _verify_resolved_branch(
    *,
    worktree_dir: Path,
    branch_name: str,
    expected_head_sha: str,
    base_sha: str,
    repo: str,
    published: bool | None = None,
) -> None:
    """Prove that a completed resolution changed and reached the PR branch."""
    if not expected_head_sha:
        raise RuntimeError("the PR head SHA is missing")
    _assert_resolved_worktree(worktree_dir)
    if has_uncommitted_changes(worktree_dir):
        raise RuntimeError("the worktree has unpublished changes")
    local_head = _worktree_head_sha(worktree_dir)
    if not local_head or local_head == expected_head_sha:
        raise RuntimeError("the resolution did not create a new local commit")
    _assert_resolution_ancestry(
        worktree_dir=worktree_dir,
        local_head=local_head,
        expected_head_sha=expected_head_sha,
        base_sha=base_sha,
    )
    remote_head = publication_remote_sha(worktree_dir, branch_name, repo=repo)
    if remote_head != local_head:
        action = "runner publication" if published is None else "host publication"
        if published is False:
            action = "prior publication"
        raise RuntimeError(f"cannot prove {action} reached the PR branch")


def _preflight_resolve(
    pr_number: int,
    config: DispatchConfig,
    *,
    store: RunStore | None,
    force_budget: bool,
    issue_number: int,
) -> int | None:
    """Run pre-flight checks. Returns exit code on failure, None on success."""
    try:
        reachable = _check_github_connectivity(config)
    except GitHubAuthError as exc:
        _error(f"Resolve skipped: {exc}")
        return 1
    if not reachable:
        _error("Resolve skipped: GitHub is unreachable")
        return 1

    if store is not None and not force_budget:
        from kew.budget import BudgetExceeded, check_budget

        try:
            check_budget(store, config.budget, issue_number)
        except BudgetExceeded as exc:
            _error(f"Resolve skipped: {exc}")
            return 1

    return None


def _result_run_id(result: DispatchResult) -> str | None:
    """Return the durable run ID when the result contains one."""
    return result.run_id if isinstance(result.run_id, str) else None


def _require_remediation_publication(
    store: RunStore | None,
    result: DispatchResult,
    producer_run_id: str | None,
) -> None:
    if store is None:
        return
    current_run_id = _result_run_id(result)
    target_run_id = current_run_id or producer_run_id
    if not target_run_id:
        return
    ensure_publication_credentials(
        store,
        run_id=target_run_id,
        producer_run_id=producer_run_id,
    )


def _record_credential_publication_refusal(
    store: RunStore | None,
    *,
    run_id: str | None,
    issue_number: int,
    config: DispatchConfig,
    refusal: CredentialPublicationRefused,
) -> None:
    """Retain a sanitized refusal when no remediation run can own it."""
    if store is None:
        return
    emit(
        store,
        CREDENTIAL_PUBLICATION_REFUSAL,
        actor=Actor("policy", "resolve"),
        run_id=run_id,
        issue_num=issue_number,
        item_id=str(issue_number),
        repo=config.project.repo or None,
        payload={"reason": refusal.reason, "publication_allowed": False},
    )


def _resolution_execution_failed(result: DispatchResult) -> bool:
    """A wrapper refusal takes precedence over its raw zero exit."""
    return result.exit_code != 0 or startup_refused(result)


def _check_conflict_resolution(
    log_file: Path | None, worktree_dir: Path, config: DispatchConfig, *, since: str
) -> None:
    """Run the resolved tree's checks; a refusal keeps the check's whole
    output beside the run's log (#3495)."""
    try:
        run_resolution_checks(worktree_dir, config, since=since)
    except ResolutionChecksFailed as exc:
        if log_file is not None:
            record_resolution_evidence(log_file, since=since, failure=exc)
        raise


def _finish_conflict_resolution(
    *,
    result: DispatchResult,
    containment: Any,
    host_publication: bool,
    publication_mode: Literal["canonical", "checkout"],
    worktree_dir: Path,
    conflict_info: ConflictInfo,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    base_branch: str,
    expected_head_sha: str,
    config: DispatchConfig,
    store: RunStore | None,
    producer_run_id: str | None = None,
) -> CommandOutcome:
    """Verify or publish one completed conflict-resolution agent run."""
    if _resolution_execution_failed(result):
        _error(
            f"Conflict resolution for PR #{pr_number} failed "
            f"(exit {result.exit_code}): "
            f"{result.stop_reason}"
        )
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return command_outcome(result)

    try:
        _require_remediation_publication(store, result, producer_run_id)
        if host_publication:
            # Not onto a head a merge verify claimed meanwhile (#3198): the
            # resolution waits, intact, and publishes once the claim ends.
            wait_out_merge_claim(
                store, pr_number, expected_head_sha, flow="conflict resolution"
            )
            # A resolution that does not build is never pushed (#3200).
            # The log file only places the run's evidence; a result without
            # one never decides the push (#3495).
            _check_conflict_resolution(
                getattr(result, "log_file", None),
                worktree_dir,
                config,
                since=expected_head_sha,
            )
            publication_runtime, publication_image = _publication_extraction_boundary(
                config, containment
            )
            _publish_resolved_branch(
                worktree_dir=worktree_dir,
                # A new resolution head needs fresh human CI approval (#2033).
                before_push=partial(revoke_ci_approval, branch_name, config),
                branch_name=branch_name,
                expected_head_sha=expected_head_sha,
                base_sha=conflict_info.base_sha,
                base_branch=base_branch,
                repo=config.project.repo,
                commit_message=(
                    f"fix: resolve merge conflicts with {base_branch} "
                    f"on PR #{pr_number}"
                ),
                force_with_lease=conflict_info.strategy_used == "rebase",
                publish=publication_mode,
                dangerously_skip_containment=(
                    config.sandbox.dangerously_skip_containment
                ),
                runtime=publication_runtime,
                image=publication_image,
                proxy_environment=config.sandbox.proxy_environment,
                store=store,
                run_id=_result_run_id(result),
            )
        else:
            _verify_resolved_branch(
                worktree_dir=worktree_dir,
                branch_name=branch_name,
                expected_head_sha=expected_head_sha,
                base_sha=conflict_info.base_sha,
                repo=config.project.repo,
            )
    except CredentialPublicationRefused as exc:
        current_run_id = _result_run_id(result)
        if store is not None and current_run_id:
            store.transition_run_status(
                current_run_id,
                expected_status="success",
                new_status="refused",
                error_category=CREDENTIAL_PUBLICATION_REFUSAL,
            )
        _record_credential_publication_refusal(
            store,
            run_id=current_run_id or producer_run_id,
            issue_number=issue_number,
            config=config,
            refusal=exc,
        )
        _error(f"Conflict resolution for PR #{pr_number} was not published: {exc}")
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return CommandOutcome("executed", 1)
    except ResolutionChecksFailed as exc:
        return _checks_failed(pr_number, worktree_dir, exc)
    except Exception as exc:
        _error(f"Completion verification for PR #{pr_number} failed: {exc}")
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return CommandOutcome("executed", 1)
    _success(f"Conflict resolution for PR #{pr_number} completed successfully.")
    cleanup_worktree(worktree_dir, branch_name)
    return CommandOutcome("executed", 0)


def _execute_resolve(**kwargs: Any) -> CommandOutcome:
    """Run the resolution; the checkout lease ends with it on every exit
    (review round 12 of #1440)."""
    try:
        return _run_resolve(**kwargs)
    finally:
        finish_command_cleanup(release_checkout_claims, command="resolve")


def _clean_integration_run_id(
    store: RunStore | None, producer_run_id: str | None
) -> str | None:
    """Reuse only producers whose intent can pass the next referenced lookup."""
    if store is None or not producer_run_id:
        return None
    ensure_publication_credentials(store, run_id=producer_run_id)
    if not store.list_route_profiles(producer_run_id):
        # Legacy native runs have no immutable profile. Keep their legacy
        # lookup instead of creating an intent that requires missing evidence.
        return None
    ensure_publication_credentials(
        store, run_id=producer_run_id, producer_run_id=producer_run_id
    )
    return producer_run_id


def _finish_clean_resolution(
    *,
    worktree_dir: Path,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    base_branch: str,
    expected_head_sha: str,
    integrated_base_sha: str,
    config: DispatchConfig,
    store: RunStore | None,
    producer_run_id: str | None,
) -> CommandOutcome:
    """Guard and publish an integration that produced no conflicts."""
    try:
        integration_run_id = _clean_integration_run_id(store, producer_run_id)
    except CredentialPublicationRefused as exc:
        _record_credential_publication_refusal(
            store,
            run_id=producer_run_id,
            issue_number=issue_number,
            config=config,
            refusal=exc,
        )
        _error(f"PR #{pr_number} integration was not published: {exc}")
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return CommandOutcome("executed", 1)
    select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        store=store,
        issue_num=issue_number,
        item_id=str(issue_number),
        branch=branch_name,
        repo=config.project.repo or None,
    )
    # Not onto a head a merge verify claimed meanwhile (#3198).
    wait_out_merge_claim(store, pr_number, expected_head_sha, flow="base integration")
    # A clean merge can still break the build: check before the push (#3200).
    run_resolution_checks(worktree_dir, config, since=expected_head_sha)
    push_status = _push_clean_integration(
        worktree_dir=worktree_dir,
        # A new integration head needs fresh human CI approval (#2033).
        before_push=partial(revoke_ci_approval, branch_name, config),
        branch_name=branch_name,
        base_branch=base_branch,
        strategy=config.resolve.strategy,
        expected_head_sha=expected_head_sha,
        base_sha=integrated_base_sha,
        repo=config.project.repo,
        publish=config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        store=store,
        run_id=integration_run_id,
    )
    if push_status == "push_failed":
        _error(
            f"PR #{pr_number}: base '{base_branch}' integrated cleanly but "
            f"the push to '{branch_name}' was rejected."
        )
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return CommandOutcome("executed", 1)
    if push_status == "pushed":
        _success(
            f"PR #{pr_number}: base '{base_branch}' integrated cleanly and "
            f"pushed to '{branch_name}'."
        )
    else:
        _log(
            f"PR #{pr_number}: already up to date with base "
            f"'{base_branch}', nothing to integrate."
        )
    cleanup_worktree(worktree_dir, branch_name)
    return CommandOutcome("executed", 0)


def _checks_failed(
    pr_number: int, worktree_dir: Path, exc: ResolutionChecksFailed
) -> CommandOutcome:
    """A resolution its checks refused: nothing pushed, and the reason says
    which check and its first failing line (#3200), so the hold that follows
    names them instead of a bare exit code."""
    _error(f"PR #{pr_number}: the resolution was not pushed: {exc}")
    _warn(f"Worktree preserved for debugging: {worktree_dir}")
    return CommandOutcome("executed", 1, reason=exc.summary)


def _complete_clean_resolution(**kwargs: Any) -> CommandOutcome:
    """A clean integration already performed work before publication."""
    try:
        return _finish_clean_resolution(**kwargs)
    except ResolutionChecksFailed as exc:
        return _checks_failed(kwargs["pr_number"], kwargs["worktree_dir"], exc)
    except Exception as exc:
        _error(f"Resolve publication for PR #{kwargs['pr_number']} failed: {exc}")
        return CommandOutcome("executed", 1)


def _resolve_containment_for_run(
    config: DispatchConfig,
    *,
    store: RunStore | None,
    runner: str,
    run_id: str,
    issue_number: int,
    branch_name: str,
    inference_transport: ResolvedInferenceTransport,
) -> RemediationContainment:
    """Record containment refusals before the command handles their exit."""
    try:
        return resolve_remediation_containment(
            config,
            runner=runner,
            # Threaded, not omitted: the OpenShell egress gate reads its
            # certification off this store (#2389 review).
            store=store,
            inference_transport=inference_transport,
        )
    except SandboxRuntimeUnavailable as exc:
        try:
            record_remediation_refusal(
                store,
                config,
                exc,
                runner=runner,
                run_id=run_id,
                run_kind="resolve",
                issue_num=issue_number,
                branch=branch_name,
            )
        except Exception as audit_exc:
            _error(f"Failed to record containment refusal {run_id}: {audit_exc}")
        raise


def _resolve_host_preflight(
    config: DispatchConfig, runner_name: str, posture: str
) -> str | None:
    from kew.runner_preflight import runner_preflight_failure

    if posture == POSTURE_SANDBOXED:
        return None
    return runner_preflight_failure(config, runner_name=runner_name)


def _run_resolve(
    *,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    base_branch: str,
    expected_head_sha: str,
    pr_title: str,
    config: DispatchConfig,
    store: RunStore | None,
    producer_run_id: str | None = None,
    item_id: str | None = None,
    on_conflict: Callable[[ConflictInfo], None] | None = None,
    force_budget: bool = False,
) -> CommandOutcome:
    """Checkout worktree, build prompt, and dispatch agent.

    *on_conflict* observes the conflicts the base integration left, before any
    attempt to resolve them. The epic driver's merge-main step (#2149) needs the
    conflicting paths to name them in the hold a resolution failure earns; the
    exit code alone cannot say which files an operator must look at.
    """
    # Refuse before fetching refs or changing a preserved checkout.
    resolved = resolve_runner(config)
    inference_transport = resolve_inference_transport(
        config, resolved.runner, resolved.model
    )
    flow_run_id = str(uuid.uuid4())
    containment = _resolve_containment_for_run(
        config,
        store=store,
        runner=resolved.runner.name,
        run_id=flow_run_id,
        issue_number=issue_number,
        branch_name=branch_name,
        inference_transport=inference_transport,
    )
    _log(f"Checking out worktree for PR #{pr_number} (branch: {branch_name})...")
    worktree_dir, conflict_info = checkout_existing_worktree(
        issue_num=issue_number,
        branch_name=branch_name,
        config=config,
        base_branch=base_branch,
        expected_head_sha=expected_head_sha,
        pinned_head_sha=expected_head_sha,
    )
    integrated_base_sha = (
        conflict_info.base_sha
        if conflict_info is not None
        else _fetched_base_sha(worktree_dir, base_branch)
    )

    if conflict_info is not None and on_conflict is not None:
        on_conflict(conflict_info)

    if conflict_info is None:
        # Base integrated with zero conflicts. Distinguish a real integration
        # commit (which the PR needs pushed) from a branch already up to date
        # with base — the pre-#790 code conflated both as "merged cleanly".
        return _complete_clean_resolution(
            worktree_dir=worktree_dir,
            pr_number=pr_number,
            issue_number=issue_number,
            branch_name=branch_name,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
            integrated_base_sha=integrated_base_sha,
            config=config,
            store=store,
            producer_run_id=producer_run_id,
        )

    return _run_conflicted_resolve(
        worktree_dir=worktree_dir,
        conflict_info=conflict_info,
        pr_number=pr_number,
        issue_number=issue_number,
        branch_name=branch_name,
        base_branch=base_branch,
        expected_head_sha=expected_head_sha,
        pr_title=pr_title,
        config=config,
        store=store,
        producer_run_id=producer_run_id,
        item_id=item_id,
        resolved=resolved,
        inference_transport=inference_transport,
        containment=containment,
        flow_run_id=flow_run_id,
        force_budget=force_budget,
    )


def _run_conflicted_resolve(
    *,
    worktree_dir: Path,
    conflict_info: ConflictInfo,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    base_branch: str,
    expected_head_sha: str,
    pr_title: str,
    config: DispatchConfig,
    store: RunStore | None,
    producer_run_id: str | None,
    item_id: str | None = None,
    resolved: ResolvedRoute,
    inference_transport: ResolvedInferenceTransport,
    containment: RemediationContainment,
    flow_run_id: str,
    force_budget: bool = False,
) -> CommandOutcome:
    """Check runner prerequisites, then execute and verify conflict resolution.

    The runner, transport, containment and run id were admitted before the
    checkout was touched (a containment refusal never fetches refs)."""
    _log(
        f"Dispatching conflict resolution for PR #{pr_number} "
        f"({len(conflict_info.conflicted_files)} file(s))..."
    )
    failure = _resolve_host_preflight(config, resolved.runner.name, containment.posture)
    if failure is not None:
        _error(failure)
        cleanup_worktree(worktree_dir, branch_name)
        return CommandOutcome("not_run", 1)
    attest_flow_posture(
        store,
        flow="conflict resolution",
        runner=resolved.runner.name,
        config=config.sandbox,
        posture=containment.posture,
        image=containment.image,
        reason=containment.reason,
        run_id=flow_run_id,
        checkout_dir=worktree_dir,
        repo=config.project.repo or None,
        issue_num=issue_number,
        inference_transport=inference_transport,
    )
    publication_mode = select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        store=store,
        issue_num=issue_number,
        item_id=str(issue_number),
        branch=branch_name,
        repo=config.project.repo or None,
    )
    # Canonical mode never verifies through checkout-rooted Git.
    # A contained runner also needs host publication under the escape hatch.
    host_publication = _canonical_or_host_publication(
        publication_mode,
        resolved.runner.publication_style,
        contained=containment.contained,
    )
    try:

        def _require_producer_credentials() -> None:
            if store is not None and producer_run_id:
                ensure_publication_credentials(store, run_id=producer_run_id)

        _require_producer_credentials()

    except CredentialPublicationRefused as exc:
        _record_credential_publication_refusal(
            store,
            run_id=producer_run_id,
            issue_number=issue_number,
            config=config,
            refusal=exc,
        )
        _error(f"Conflict resolution for PR #{pr_number} was refused: {exc}")
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
        return CommandOutcome("not_run", 1)
    launch_state = LaunchState()
    try:
        # Off the host store, never the checkout being resolved (#3023).
        addendum = launch_addendum(store, "resolve")
        prompt = _build_resolve_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            branch_name=branch_name,
            base_branch=base_branch,
            conflict_info=conflict_info,
            config=config,
            host_publication=host_publication,
            profile_addendum=addendum,
        )
        require_pinned_pr_head(worktree_dir, branch_name, expected_head_sha)
        result = run_tracked_agent(
            launch_state=launch_state,
            prompt=prompt,
            prompt_profile_version_id=profile_version_id(addendum),
            worktree_dir=worktree_dir,
            config=config,
            issue_num=issue_number,
            store=store,
            title=pr_title,
            branch=branch_name,
            item_id=item_id,
            source_tracker=config.tracker.type,
            worker_cap=config.fleet_concurrency(),
            cap_exclude_issue=issue_number,
            run_kind=RESOLVE_RUN_KIND,
            model=resolved.model,
            runner=resolved.runner,
            loop_id=None,
            run_id=flow_run_id,
            budget_override_usd=None,
            contained_image=containment.image,
            inference_transport=inference_transport,
            sandbox_posture=containment.posture,
            validate_continuation=False,
            force_budget=force_budget,
            # No override here, so the ceiling is agent.max_budget_usd. It reaches
            # claude-code as an argv flag on the host and NOT inside a container,
            # so a contained run needs kew to watch instead (#1462).
            budget_check=budget_watchdog(
                config.agent.max_budget_usd,
                runner_name=resolved.runner.name,
                contained=containment.contained,
            ),
        )

        launch_state.observe(result)
        return _finish_conflict_resolution(
            result=result,
            containment=containment,
            host_publication=host_publication,
            publication_mode=publication_mode,
            worktree_dir=worktree_dir,
            conflict_info=conflict_info,
            pr_number=pr_number,
            issue_number=issue_number,
            branch_name=branch_name,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
            config=config,
            store=store,
            producer_run_id=producer_run_id,
        )
    except WorkerCapReached:
        _error(WORKER_CAP_REACHED_MESSAGE)
        return CommandOutcome("not_run", 1)
    except TransportAdmissionError as exc:
        _error(f"Resolve dispatch for PR #{pr_number} could not run: {exc}")
        return CommandOutcome(launch_state.kind, 1)
    except PromptConfigurationError as exc:
        # The operator's resolve profile refused before any runner (#3023).
        _error(f"Resolve dispatch for PR #{pr_number} refused: {exc}")
        record_prompt_refusal(
            store,
            config,
            run_id=flow_run_id,
            run_kind=RESOLVE_RUN_KIND,
            issue_num=issue_number,
            branch=branch_name,
            item_id=item_id if item_id is not None else str(issue_number),
            runner=resolved.runner.name,
        )
        cleanup_worktree(worktree_dir, branch_name)
        # Raw exit 2, the code the refused run row carries.
        return CommandOutcome(
            "not_run", 2, str(exc), category=PROMPT_CONFIGURATION_REFUSAL
        )
    except Exception as exc:
        _error(f"Resolve dispatch for PR #{pr_number} failed: {exc}")
        return CommandOutcome(launch_state.kind, 1)


def _push_clean_integration(
    *,
    worktree_dir: Path,
    branch_name: str,
    base_branch: str,
    strategy: str,
    expected_head_sha: str,
    base_sha: str,
    repo: str,
    publish: Literal["canonical", "checkout"] = "canonical",
    dangerously_skip_containment: bool = False,
    store: RunStore | None = None,
    run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> Literal["pushed", "up_to_date", "push_failed"]:
    """Publish a clean integration as a merge commit in canonical mode.

    A clean merge/rebase against a moved base is still a branch update the PR
    needs — discarding it silently is what let #790's "merged cleanly" lie.

    Returns ``"pushed"`` when the worktree advanced beyond ``origin/<branch_name>``
    and the update was published; ``"up_to_date"`` when the branch was already up
    to date with base (nothing to push); ``"push_failed"`` when there was a real
    integration commit but the push was rejected (e.g. a stale
    ``--force-with-lease`` or a non-fast-forward) — reporting that as success is
    the exact class of lie #790 exists to kill. A ``rebase`` strategy rewrites
    history, so its push uses ``--force-with-lease`` (safe: base was just fetched).
    """
    mode = select_publication_mode(
        publish,
        dangerously_skip_containment=dangerously_skip_containment,
        branch=branch_name,
        repo=repo,
    )
    if mode == "canonical":
        return _push_clean_integration_canonical(
            worktree_dir=worktree_dir,
            branch_name=branch_name,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
            base_sha=base_sha,
            repo=repo,
            store=store,
            run_id=run_id,
            before_push=before_push,
        )
    return _push_clean_integration_checkout(
        worktree_dir=worktree_dir,
        branch_name=branch_name,
        base_branch=base_branch,
        strategy=strategy,
        expected_head_sha=expected_head_sha,
        repo=repo,
        dangerously_skip_containment=dangerously_skip_containment,
        store=store,
        run_id=run_id,
        before_push=before_push,
    )


def _push_clean_integration_canonical(
    *,
    worktree_dir: Path,
    branch_name: str,
    base_branch: str,
    expected_head_sha: str,
    base_sha: str,
    repo: str,
    store: RunStore | None = None,
    run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> Literal["pushed", "up_to_date", "push_failed"]:
    if not expected_head_sha:
        return "push_failed"
    remote = publication_remote(repo)
    with TemporaryDirectory(prefix="kew-clean-integration-") as temp:
        scratch = Path(temp)
        branch_head = remote_branch_sha(
            remote, branch_name, scratch=scratch / "branch-query"
        )
        observed_base = remote_branch_sha(
            remote, base_branch, scratch=scratch / "base-query"
        )
        if branch_head != expected_head_sha or not observed_base:
            return "push_failed"
        if observed_base != base_sha:
            _warn(
                f"Canonical integration refused: base branch {base_branch!r} "
                f"moved from {base_sha[:12]} to {observed_base[:12]}."
            )
            return "push_failed"
        try:
            base_already_integrated = remote_commit_is_ancestor(
                remote,
                base_sha,
                branch_head,
                scratch=scratch / "ancestry",
            )
        except Exception as exc:
            _warn(f"Canonical integration ancestry verification failed: {exc}")
            return "push_failed"
        try:
            result = publish_checkout(
                worktree_dir,
                remote=remote,
                branch=branch_name,
                parents=(branch_head, base_sha),
                base_sha=branch_head,
                message=(f"Merge {base_branch} into {branch_name} (kew resolve)"),
                identity=host_commit_identity(),
                force_with_lease=f"{branch_name}:{expected_head_sha}",
                require_resolved=True,
                publish_empty_tree=not base_already_integrated,
                before_push=before_push,
                acceptance=_resolve_acceptance(store, run_id, branch_name),
                on_intent=_intent_recorder(
                    store,
                    run_id=run_id,
                    item_id=None,
                    branch=branch_name,
                    parent_sha=branch_head,
                ),
                output_guard=publication_output_guard(store, run_id=run_id),
                scratch=scratch / "publish",
            )
        except PublicationHeld as exc:
            raise PublicationError("push", exc) from exc
        except PublicationError as exc:
            _warn(f"Canonical integration publication failed: {exc}")
            return "push_failed"
        except Exception as exc:
            _warn(f"Canonical integration verification failed: {exc}")
            return "push_failed"
    return "up_to_date" if isinstance(result, CanonicalNoChange) else "pushed"


def _push_clean_integration_checkout(
    *,
    worktree_dir: Path,
    branch_name: str,
    base_branch: str,
    strategy: str,
    expected_head_sha: str,
    repo: str,
    dangerously_skip_containment: bool = False,
    store: RunStore | None = None,
    run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> Literal["pushed", "up_to_date", "push_failed"]:

    if not expected_head_sha or has_uncommitted_changes(worktree_dir):
        return "push_failed"
    remote_head = publication_remote_sha(worktree_dir, branch_name, repo=repo)
    if remote_head != expected_head_sha:
        return "push_failed"
    local_head = _worktree_head_sha(worktree_dir)
    if not local_head:
        return "push_failed"
    if local_head == expected_head_sha:
        return "up_to_date"
    if strategy != "rebase" and not _is_ancestor(
        worktree_dir, expected_head_sha, local_head
    ):
        return "push_failed"

    try:
        publish_existing_pr_branch(
            worktree_dir,
            branch_name,
            commit_message=f"fix: integrate {base_branch} into {branch_name}",
            force_with_lease=strategy == "rebase",
            before_push=before_push,
            expected_remote_sha=expected_head_sha,
            repo=repo,
            publish="checkout",
            dangerously_skip_containment=dangerously_skip_containment,
            store=store,
            run_id=run_id,
        )
    except Exception as exc:
        _warn(f"Checkout integration publication failed: {exc}")
        return "push_failed"
    remote_head = publication_remote_sha(worktree_dir, branch_name, repo=repo)
    return "pushed" if remote_head == local_head else "push_failed"


@dataclass
class _ResolveContext:
    """Parsed PR metadata for conflict resolution."""

    branch_name: str
    head_sha: str
    base_branch: str
    pr_title: str
    pr_body: str
    pr_state: str
    mergeable: str


def _parse_pr_context(pr_number: int, meta: dict[str, Any]) -> _ResolveContext | None:
    """Extract and validate fields from PR metadata.

    Returns None (and logs an error) when required fields are missing.
    """
    branch_name: str = meta.get("headRefName", "")
    if not branch_name:
        _error(f"PR #{pr_number} has no head branch")
        return None

    return _ResolveContext(
        branch_name=branch_name,
        head_sha=meta.get("headRefOid", "") or "",
        base_branch=meta.get("baseRefName", "") or "main",
        pr_title=meta.get("title", "") or f"PR #{pr_number}",
        pr_body=meta.get("body", "") or "",
        pr_state=meta.get("state", ""),
        mergeable=meta.get("mergeable", "UNKNOWN"),
    )


def _resolve_skip_result(
    pr_number: int,
    ctx: _ResolveContext,
    *,
    dry_run: bool,
) -> int | None:
    """Return a successful exit when this PR needs no resolve dispatch."""
    if ctx.pr_state in ("MERGED", "CLOSED"):
        _log(f"PR #{pr_number} is {ctx.pr_state.lower()} — nothing to resolve.")
        return 0
    if ctx.mergeable == "MERGEABLE":
        _log(f"PR #{pr_number} has no merge conflicts.")
        return 0
    if dry_run:
        _log(f"PR #{pr_number} has mergeable state: {ctx.mergeable}")
        _log(f"  Branch: {ctx.branch_name} → {ctx.base_branch}")
        _log("  Would dispatch agent to resolve conflicts.")
        return 0
    return None


def _resolve_source_identity(
    store: RunStore | None,
    ctx: _ResolveContext,
    pr_number: int,
) -> tuple[int, str | None]:
    """Resolve the tracker issue and immutable producer for this PR."""
    producer_run_id = (
        publication_producer_for_head(
            store, branch=ctx.branch_name, head_sha=ctx.head_sha
        )
        if store is not None
        else None
    )
    producer = (
        store.get(producer_run_id) if store is not None and producer_run_id else None
    )
    stored_issue = None if producer is None else producer.issue_num
    issue_number = (
        stored_issue
        if isinstance(stored_issue, int) and stored_issue > 0
        else _resolve_issue_number(ctx.pr_body, ctx.branch_name)
    )
    return issue_number or pr_number, producer_run_id


def _admitted_source_identity(
    store: RunStore | None, ctx: _ResolveContext, pr_number: int, config: DispatchConfig
) -> tuple[int, str | None] | None:
    """Store-first resolution (canonical PR<->item join, A6), gh parse fallback.

    A credential-lineage refusal is recorded before launch and yields ``None``.
    """
    try:
        return _resolve_source_identity(store, ctx, pr_number)
    except CredentialPublicationRefused as exc:
        issue_number = _resolve_issue_number(ctx.pr_body, ctx.branch_name) or pr_number
        _record_credential_publication_refusal(
            store,
            run_id=None,
            issue_number=issue_number,
            config=config,
            refusal=exc,
        )
        _error(f"Resolve of PR #{pr_number} was refused before launch: {exc}")
        return None


def _resolve_item_id(
    store: RunStore | None, ctx: _ResolveContext, producer_run_id: str | None
) -> str:
    """The tracker item the resolve run row may name (empty for trackerless PRs).

    A producer that abstains from branch ownership (a ``kew review <PR>`` row,
    keyed by the PR number) resolves to the branch's owning item, so the resolve
    row does not leave the branch naming two items (#3067).
    """
    parsed_item = _resolve_issue_number(ctx.pr_body, ctx.branch_name)
    return (
        _intent_item_id(
            store,
            run_id=producer_run_id,
            item_id=str(parsed_item) if parsed_item is not None else None,
            branch=ctx.branch_name,
        )
        or ""
    )


def _moved_head(
    pr_number: int, ctx: _ResolveContext, expected_head_sha: str | None
) -> CommandOutcome | None:
    """Refuse a resolve whose PR head moved since the caller checked it.

    The merge-main step decides from the head it read whether an operator owns
    the branch. An operator who pushed on top in between would otherwise get
    the base merged into their commit and pushed (the 3148 incident), so the
    resolve checks nothing out, pushes nothing, and the caller re-reads.
    """
    if not expected_head_sha or ctx.head_sha == expected_head_sha:
        return None
    reason = (
        f"the PR head moved since kew checked it ({expected_head_sha[:12]} -> "
        f"{ctx.head_sha[:12]}); nothing was resolved or pushed"
    )
    _warn(f"PR #{pr_number}: {reason}")
    return CommandOutcome("not_run", 1, reason=reason, category=PR_HEAD_MOVED)


def dispatch_resolve(
    *,
    pr_number: int,
    config: DispatchConfig,
    dry_run: bool = False,
    store: RunStore | None = None,
    force_budget: bool = False,
    on_conflict: Callable[[ConflictInfo], None] | None = None,
    expected_head_sha: str | None = None,
) -> CommandOutcome:
    """Orchestrate merge conflict resolution for a PR.

    *expected_head_sha* pins the resolve to the head its caller checked (the
    merge-main step's ownership check, #3200): a PR whose head has moved since
    is not resolved, and the outcome says so under :data:`PR_HEAD_MOVED`.

    Returns:
        Execution state and raw exit code; the CLI maps these to public codes.
    """
    meta = _get_pr_metadata(pr_number, config.project.repo)
    if meta is None:
        _error(f"Could not fetch metadata for PR #{pr_number}")
        return CommandOutcome("not_run", 1)

    ctx = _parse_pr_context(pr_number, meta)
    if ctx is None:
        return CommandOutcome("not_run", 1)

    moved = _moved_head(pr_number, ctx, expected_head_sha)
    if moved is not None:
        return moved

    skip_result = _resolve_skip_result(pr_number, ctx, dry_run=dry_run)
    if skip_result is not None:
        return CommandOutcome("executed", skip_result)

    identity = _admitted_source_identity(store, ctx, pr_number, config)
    if identity is None:
        return CommandOutcome("not_run", 1)
    issue_number, producer_run_id = identity

    preflight_rc = _preflight_resolve(
        pr_number,
        config,
        store=store,
        force_budget=force_budget,
        issue_number=issue_number,
    )
    if preflight_rc is not None:
        return CommandOutcome("not_run", preflight_rc)

    try:
        return _execute_resolve(
            pr_number=pr_number,
            issue_number=issue_number,
            branch_name=ctx.branch_name,
            base_branch=ctx.base_branch,
            expected_head_sha=ctx.head_sha,
            pr_title=ctx.pr_title,
            config=config,
            store=store,
            producer_run_id=producer_run_id,
            item_id=_resolve_item_id(store, ctx, producer_run_id),
            force_budget=force_budget,
            on_conflict=on_conflict,
        )
    except SandboxRuntimeUnavailable as exc:
        _error(f"Conflict resolution for PR #{pr_number} was refused: {exc}")
        return CommandOutcome("not_run", 2)
    except Exception as exc:
        _error(f"Resolve dispatch for PR #{pr_number} failed: {exc}")
        return CommandOutcome("not_run", 1)
