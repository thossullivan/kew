"""One rule for a pull request's state, read from the runs that name it.

Several run rows can carry one ``pr_url``: the dispatch that opened it, review
and remediation runs, a failed or refused attempt on the same branch, a row
held for publication. The outcome sync refreshes only some of them, so older
rows go stale (a failed dispatch still says ``OPEN`` after the PR merged; a PR
closed, reopened and merged keeps ``CLOSED`` on its older rows). Every report
figure that counts merged pull requests reads the state through
:func:`pr_states_by_url`, so the headline, the per-model scorecard and the
per-epic ledger cannot disagree about the same PR.

The rule matches the ledger queries that already decide "merged" in SQL
(``run_store_outcomes``, ``run_store_prompt_profiles``): **a pull request is
MERGED if any of its records says MERGED or carries a merge time; otherwise its
state is the newest non-empty state among its records.** A merge is final on
GitHub, so one record that saw it outweighs any number of stale ones.

Pure and store-free: records are duck-typed (``pr_url``, ``pr_state``,
``pr_merged_at``, ``started_at``), so ``kew.scorecard`` and ``kew.service`` can
import this without pulling ``kew.run_store`` into their import graph.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MERGED = "MERGED"


def _says_merged(record: Any) -> bool:
    state = str(getattr(record, "pr_state", None) or "").upper()
    return state == MERGED or bool(getattr(record, "pr_merged_at", None))


def pr_states_by_url(records: Iterable[Any]) -> dict[str, str]:
    """Map each ``pr_url`` among *records* to the pull request's state.

    ``MERGED`` when any record for the PR says ``MERGED`` (in any case) or
    carries a ``pr_merged_at``; otherwise the newest (by ``started_at``)
    non-empty ``pr_state``, as stored. A PR none of whose records carries a
    state or a merge time is left out, as the readers have always treated a PR
    with no stored state.
    """
    by_url: dict[str, list[Any]] = {}
    for record in records:
        url = getattr(record, "pr_url", None)
        if url:
            by_url.setdefault(url, []).append(record)
    states: dict[str, str] = {}
    for url, group in by_url.items():
        if any(_says_merged(r) for r in group):
            states[url] = MERGED
            continue
        stated = [r for r in group if getattr(r, "pr_state", None)]
        if stated:
            states[url] = str(_newest(stated).pr_state)
    return states


def _newest(records: list[Any]) -> Any:
    """The latest-started record; a record with no start time loses to any
    record that has one. A stable sort keeps the caller's order among equal
    start times, so the record listed later wins a tie."""
    dated = [r for r in records if getattr(r, "started_at", None) is not None]
    if not dated:
        return records[-1]
    return sorted(dated, key=lambda r: r.started_at)[-1]
