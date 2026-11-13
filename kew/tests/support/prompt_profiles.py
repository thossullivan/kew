"""Prompt-profile test helpers: rows the service would refuse, and run rows (#3022)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from kew.audit import resolve_actor
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore
from kew.run_store_prompt_profiles import ProfileAudit
from kew.service import prompt_profiles as profiles


def _audit(role: str, version: int) -> ProfileAudit:
    return ProfileAudit(
        by="alice",
        at="2026-10-01T00:00:00+00:00",
        actor_type="human",
        repo=None,
        policy_hash=None,
        payload={"role": role, "version": version},
    )


def propose_unchecked(
    store: SqliteRunStore,
    body: str,
    role: str = "dispatch",
    *,
    version: int = 1,
    body_sha256: str | None = None,
) -> None:
    """Record *body* as *role*'s proposed *version* through the store API,
    around the service's validation.

    *body_sha256* overrides the recorded digest, for a row whose body does not
    match what was proposed."""
    store.insert_prompt_profile_proposal(
        role=role,
        version=version,
        body=body,
        body_sha256=body_sha256 or hashlib.sha256(body.encode()).hexdigest(),
        event=profiles.PROFILE_PROPOSED_EVENT,
        audit=_audit(role, version),
    )


def activate_unchecked(
    store: SqliteRunStore,
    body: str,
    role: str = "dispatch",
    *,
    body_sha256: str | None = None,
) -> None:
    """Make *body* *role*'s active v1 through the store API, around the
    service's validation: what a row approved before a newer rule looks like.

    *body_sha256* overrides the recorded digest, for a row whose body no longer
    matches what was approved."""
    propose_unchecked(store, body, role, body_sha256=body_sha256)
    store.activate_prompt_profile(
        role=role,
        version=1,
        expected_generation=profiles.current_generation(store, role),
        event=profiles.PROFILE_ACTIVATED_EVENT,
        audit=_audit(role, 1),
    )


def save_launch_row(store: SqliteRunStore, launch: dict[str, Any]) -> str:
    """Write the pending ``runs`` row the run writer saves for *launch*.

    *launch* is what a flow handed ``run_tracked_agent``; the row is written by
    ``dispatch_core._save_pending_run``, the writer that call uses, so the
    stored version is the one the flow chose, not one this helper picked.
    """
    from kew.dispatch_core import _save_pending_run, _TrackedRun
    from kew.inference_transport import (
        AdmittedInferenceTransport,
        ResolvedInferenceTransport,
    )
    from kew.runners import get_runner

    run_id = str(launch.get("run_id") or "run-fixup")
    _save_pending_run(
        _TrackedRun(
            config=DispatchConfig(),
            store=store,
            run_id=run_id,
            issue_num=int(launch["issue_num"]),
            item_id=launch.get("item_id"),
            branch=str(launch.get("branch") or "agent/issue-1"),
            title=str(launch.get("title") or "fix-up"),
            complexity="medium",
            run_kind=str(launch["run_kind"]),
            loop_id=None,
            prefix_key=None,
            provider="anthropic",
            runner=get_runner("claude-code"),
            effective_model="claude-opus-5",
            actor=resolve_actor(),
            endpoint=None,
            repo="o/r",
            log_file=Path("run.log"),
            dispatch_start=0.0,
            transport=AdmittedInferenceTransport(
                resolved=ResolvedInferenceTransport(
                    mode="direct",
                    requested_model="claude-opus-5",
                    model="claude-opus-5",
                    config_sha256="cfg",
                    endpoint="https://api.anthropic.com",
                ),
                observed_version=None,
                preflight_verdict="ready",
            ),
            prompt_profile_version_id=launch.get("prompt_profile_version_id"),
        ),
        worker_cap=0,
        cap_exclude_issue=None,
        admit_budget_check=None,
    )
    return run_id
