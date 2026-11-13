"""Child credential filtering usable below the dispatch and runner layers."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from kew.credential_issuer import INFERENCE_ENV, MANAGEMENT_CREDENTIAL_ENVS


def child_environment(
    env: Mapping[str, str] | None = None, *, retain: Iterable[str] = ()
) -> dict[str, str]:
    """Remove OpenRouter management, inference, and handoff authority.

    ``retain`` names the route credentials a boundary declares explicitly
    (the managed Switchyard server and the sidecar proxy worker declare their
    upstream credential); those names pass through untouched. Management and
    handoff authority never pass.
    """
    source = os.environ if env is None else env
    excluded = {*MANAGEMENT_CREDENTIAL_ENVS, INFERENCE_ENV, "KEW_ISSUED_RUN_KEY_HASH"}
    excluded -= set(retain) - {*MANAGEMENT_CREDENTIAL_ENVS, "KEW_ISSUED_RUN_KEY_HASH"}
    return {name: value for name, value in source.items() if name not in excluded}
