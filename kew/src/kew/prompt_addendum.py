"""Prompt-profile addendum types and prompt configuration refusals (#3022).

A leaf module: it imports only the standard library (``kew.config`` for
typing only), so :mod:`kew.service.prompt_profiles` and
:mod:`kew.service.prompts` can use these types without importing
:mod:`kew.prompt`, which reaches ``kew.graph`` through ``kew.worktree`` and
the dispatch stack (the import-linter contract that keeps ``kew.service`` off
``kew.graph``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kew.config import DispatchConfig

#: The outcome category of a launch refused by :class:`PromptConfigurationError`.
PROMPT_CONFIGURATION_REFUSAL = "prompt_configuration_refused"


class PromptConfigurationError(ValueError):
    """The operator's ``[prompt]`` configuration refuses this render.

    Raised before any runner starts, and fixed only in configuration, so a
    launch reports it as a configuration refusal, never as an agent failure.
    """


class HostPublicationPromptError(PromptConfigurationError):
    """Host publication requires the reviewed built-in prompt without append.

    An approved :class:`ProfileAddendum` is not refused: the approver gate and
    its audit event are the review a ``[prompt]`` append never had.
    """


class ProfileAddendumNotRenderedError(PromptConfigurationError):
    """A custom ``[prompt] template`` is configured while a profile is active.

    Only the built-in template renders the canonical addendum block (heading,
    body, then the precedence line before the safety section). A custom one
    cannot be shown to do the same — the body's bytes may as well come from the
    untrusted issue fence — so the launch would record a
    ``prompt_profile_version_id`` for a profile the agent never received.
    Refusing before the render keeps that column truthful.
    """


class ProfileAddendumRefusedError(PromptConfigurationError):
    """The active profile's body may not render.

    Either it no longer matches the digest it was approved under, or it could
    open a Markdown block over the safety rules.
    :func:`kew.service.prompt_profiles.propose` refuses the latter; an active
    row that fails either check (approved before that rule, or changed outside
    the service) is refused at launch, so it is never rendered.
    """


@dataclass(frozen=True)
class ProfileAddendum:
    """An approved operator addendum to a role's base prompt (#3022).

    Read only from the host store
    (:func:`kew.service.prompt_profiles.launch_addendum`), never from a
    checkout: the approver gate and the audit trail are the review host
    publication asks of anything added to the built-in prompt.
    """

    version_id: int
    role: str
    version: int
    body: str


def profile_version_id(addendum: ProfileAddendum | None) -> int | None:
    """The ``runs.prompt_profile_version_id`` a launch under *addendum* records."""
    return addendum.version_id if addendum is not None else None


def refuse_custom_template_with_addendum(
    config: DispatchConfig, addendum: ProfileAddendum | None
) -> None:
    """Fail closed on a custom ``[prompt] template`` while a profile is active.

    The rendered text is never inspected: a substring check passes when the
    issue body carries the addendum's bytes inside the untrusted fence. A
    dispatch also runs this before any paid launch that precedes the render
    (the acceptance author), so the refusal costs nothing, and the prompt
    preview runs it against the served configuration so it shows the refusal
    the launch would make (#3023).
    """
    if addendum is not None and config.prompt.template:
        raise ProfileAddendumNotRenderedError(
            f"the {addendum.role} prompt profile v{addendum.version} is active "
            "and only the built-in prompt renders it; retire the profile or "
            "remove [prompt] template from kew.toml"
        )
