"""Command results retain execution state and raw internal exit codes."""

from dataclasses import dataclass
from typing import Literal

SUCCESS_EXIT_CODE = 0
FINDINGS_EXIT_CODE = 1
COULD_NOT_RUN_EXIT_CODE = 2

#: The ``category`` of a ``kew resolve`` that ran nothing because the PR's head
#: is no longer the one its caller checked (#3200).
PR_HEAD_MOVED = "pr_head_moved"


@dataclass(frozen=True)
class CommandOutcome:
    """The CLI translates this result; runner records retain the raw exit code."""

    kind: Literal["executed", "not_run"]
    exit_code: int
    reason: str = ""
    #: The run this command recorded, when it got as far as minting one.
    run_id: str | None = None
    #: The refusal's ``error_category`` when a named one stopped the command
    #: before any runner (``prompt_configuration_refused``, #3023), else empty.
    category: str = ""
