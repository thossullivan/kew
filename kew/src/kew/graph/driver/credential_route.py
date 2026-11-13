"""Resolve detached credential eligibility using the child's route resolvers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from kew import selector
from kew.credential_handoff import DriverCredentialRoute, credential_route
from kew.inference_transport import resolve_inference_transport
from kew.runners import resolve_runner

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.models import GhIssue


def _item_complexity(config: DispatchConfig, item_id: str) -> str | None:
    if config.routing is None:
        return None
    issue: GhIssue | None
    if config.tracker.type == "linear":
        from kew.linear.client import LinearClient
        from kew.linear.tracker import LinearTracker

        issue = LinearTracker(
            config, LinearClient(os.environ.get("LINEAR_API_KEY", ""))
        ).fetch_issue(item_id)
    else:
        issue = selector.select_next_issue(config=config, force_issue=int(item_id))
    if issue is None:
        raise ValueError("dispatch item unavailable for route resolution")
    return selector.get_complexity(issue.get("labels", []))


def resolve_driver_credential_route(
    config: DispatchConfig, item_id: str
) -> DriverCredentialRoute:
    """Resolve item complexity, runner, model, and transport before issuance."""
    route = resolve_runner(config, complexity=_item_complexity(config, item_id))
    transport = resolve_inference_transport(config, route.runner, route.model)
    return credential_route(route.runner, transport)
