"""`kew publication` command group: operator evidence about published heads."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from kew import cli as _c
from kew.cli._apps import publication_app
from kew.progress import _error, _success
from kew.publication import PublicationError
from kew.publication_attest import HeadAttestationRefused, attest_published_head
from kew.publication_canonical import PublicationHeld
from kew.runner_fields import exception_text
from kew.worktree import (
    _operator_remote_branch_head,
    _repo_root,
    operator_base_branch,
    operator_commit_on_base,
    operator_commit_parents,
)


def _attestation_base_branch(config_base: str) -> str:
    """The base branch as the operator repository names it, or a refusal.

    With ``branches.base`` unset the name comes from a git read of the operator
    repository, and that read can fail — a repository root git cannot answer
    for raises. Every other read this command makes is injected and turned into
    a refusal by the policy layer; this one is eager, so it is turned into one
    here, which keeps an unreadable repository inside this family's documented
    exit codes instead of an exit-1 traceback (#2177).
    """
    try:
        return operator_base_branch(config_base)
    except RuntimeError as exc:
        raise HeadAttestationRefused(
            "the base branch of the operator repository could not be read, so "
            f"no head can be attested: {exc}"
        ) from None


@publication_app.command("attest")
def publication_attest(
    branch: Annotated[str, typer.Argument(help="Branch whose head was published.")],
    sha: Annotated[
        str, typer.Argument(help="Full 40-character sha of that branch's remote head.")
    ],
    item: Annotated[
        str,
        typer.Option("--item", help="Tracker item the branch's runs belong to."),
    ],
) -> None:
    """Name an operator-pushed head as the published output of its producer.

    For commits an operator pushed by hand onto a branch kew already owns: the
    owner approved protected output, or a NEEDS_CHANGES finding was fixed
    locally. kew did not push them, so it recorded no intent and the governed
    remediation leg refuses the branch as unbound.

    The head must be the branch's current remote head, and its first-parent
    path must reach a commit that already binds to a producer — this extends a
    bound history, it never invents a producer for one. A hand-resolved merge
    of the base branch into that bound head binds too, when the base is its
    second parent, and so does a bounded chain of such commits, which
    is what an operator who cannot force-push leaves behind.
    """
    # The attestation shells out to git for the remote head, the parents and
    # the base branch; without this the allowlist is empty and resolve_cmd
    # raises.
    _c.preflight_tools()
    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        config = _c.load_config(store=store)
        _c._detect_repo(config)
        try:
            producer = attest_published_head(
                store,
                branch=branch,
                head_sha=sha,
                item_id=item,
                base_branch=_attestation_base_branch(config.branches.base),
                remote_head=lambda name: _operator_remote_branch_head(
                    _repo_root(), name
                ),
                commit_parents=lambda commit: operator_commit_parents(
                    _repo_root(), commit
                ),
                commit_on_base=lambda commit, base: operator_commit_on_base(
                    _repo_root(), commit, base
                ),
            )
        except HeadAttestationRefused as exc:
            _error(f"kew publication attest: {exception_text(exc)}")
            raise typer.Exit(2) from None
        except (PublicationError, PublicationHeld) as exc:
            # The verb converts the evidence layer's refusals itself; this is
            # the backstop that keeps any it has not seen inside this family's
            # documented exit codes, which have no code 1 (#2124).
            _error(f"kew publication attest: {exception_text(exc)}")
            raise typer.Exit(2) from None
    _success(f"Attested {sha[:12]} on {branch} as the output of run {producer}.")
