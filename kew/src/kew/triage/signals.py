"""Host-side, deterministic signals about one work item (#2293).

Everything a classifier is told about an item that is NOT the item's own prose
is computed here, by the host, from sources the host reads itself:

* **duplicate candidates** — ``gh search issues`` over open and closed issues,
  for the item's title terms plus the error strings its body quotes. Numbers
  and titles only, capped at ``[triage] dedupe_candidates``: a candidate list
  is a menu the model picks from, so it stays small and it stays data.
* **body features** — whether the body states steps, an expected/actual pair, a
  version string or a stack trace, and how long it is. Regex over the body, so
  "this bug report is complete" is a fact about the text rather than a model's
  impression of it.
* **existing labels** — what people already said about the item.
* **the body digest** — the identity every triage row is keyed on, the same
  ``sha256`` the recorded plan uses, so a decision and a plan read from one
  body are joinable.

Nothing here calls a model, and nothing here WRITES. The only subprocess is a
``gh search`` read; a search that cannot run is named in ``unavailable`` and
contributes no candidates, because "no duplicates" and "we could not look" are
different answers (CONTRIBUTING.md, "No silent clean defaults").
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kew.subprocess_utils import run
from kew.triage.plan import body_digest

if TYPE_CHECKING:
    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

DEDUPE_SEARCH_SOURCE = "dedupe_search"
"""The name a failed duplicate search is reported under in ``unavailable``."""

# --- body features ---------------------------------------------------------
#
# Each pattern answers one question a triager asks of a bug report. They are
# deliberately loose: the feature says "the body has something that looks like
# a stack trace", never "the stack trace is valid".
_STEPS_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:steps\s+to\s+reproduce|reproduction|repro(?:\s+steps)?)\b"
    r"|^\s*\d+[.)]\s+\S"
)
_EXPECTED_ACTUAL_RE = re.compile(r"(?is)\bexpected\b.{0,400}?\bactual\b")
_VERSION_RE = re.compile(
    r"(?i)\b(?:v|version[\s:=]*v?)\d+\.\d+(?:\.\d+)?\b|\b\d+\.\d+\.\d+\b"
)
_STACK_TRACE_RE = re.compile(
    r'(?m)^\s*(?:Traceback \(most recent call last\)|File "[^"]+", line \d+'
    r"|at [\w$.]+\([^)]*\)|\s+at .+:\d+:\d+)"
)

# An error string is the most searchable thing a body contains, so it is
# extracted for the duplicate query rather than left inside the prose.
_ERROR_STRING_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning))\b")
_MAX_ERROR_STRINGS = 3
_MAX_TITLE_TERMS = 6
_MIN_TERM_CHARS = 4
_SEARCH_TIMEOUT_SECONDS = 30
# Words that make a GitHub search broader rather than narrower.
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "allow",
        "also",
        "because",
        "been",
        "before",
        "being",
        "could",
        "does",
        "doesn",
        "from",
        "have",
        "into",
        "just",
        "kew",
        "like",
        "make",
        "more",
        "must",
        "never",
        "only",
        "other",
        "over",
        "same",
        "should",
        "some",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
    }
)


@dataclass(frozen=True)
class DuplicateCandidate:
    """One issue the host found by search. A candidate, not a duplicate."""

    number: int
    title: str


@dataclass(frozen=True)
class BodyFeatures:
    """What the body's TEXT contains, measured rather than judged."""

    length: int
    has_steps: bool = False
    has_expected_actual: bool = False
    has_version: bool = False
    has_stack_trace: bool = False


@dataclass(frozen=True)
class IssueSignals:
    """Everything the host knows about an item without asking a model.

    ``unavailable`` names the readers that could not run. It reaches the policy
    and the ledger row, so a decision taken over a failed duplicate search is
    legible as such instead of reading like a clean "no duplicates".
    """

    item_id: str
    body_digest: str
    features: BodyFeatures
    issue_num: int = 0
    labels: tuple[str, ...] = ()
    candidates: tuple[DuplicateCandidate, ...] = ()
    unavailable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidate_numbers(self) -> frozenset[int]:
        """The numbers a model is allowed to name as a duplicate.

        A ``duplicate_of`` outside this set was never offered to the model, so
        it is an invention rather than a choice (see
        :func:`kew.triage.policy.decide`).
        """
        return frozenset(candidate.number for candidate in self.candidates)


def body_features(body: str | None) -> BodyFeatures:
    """Measure *body*'s shape: the features a triager reads before the prose."""
    text = body or ""
    return BodyFeatures(
        length=len(text),
        has_steps=bool(_STEPS_RE.search(text)),
        has_expected_actual=bool(_EXPECTED_ACTUAL_RE.search(text)),
        has_version=bool(_VERSION_RE.search(text)),
        has_stack_trace=bool(_STACK_TRACE_RE.search(text)),
    )


def error_strings(body: str | None) -> list[str]:
    """The distinct error/exception names the body quotes, in order of first use.

    Capped at :data:`_MAX_ERROR_STRINGS`: these are query terms, and a query
    naming ten exceptions matches nothing.
    """
    found: list[str] = []
    for match in _ERROR_STRING_RE.finditer(body or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
        if len(found) >= _MAX_ERROR_STRINGS:
            break
    return found


def title_terms(title: str) -> list[str]:
    """The searchable words of *title*: long enough, not a stopword, distinct."""
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_]+", title or ""):
        lowered = word.lower()
        if len(lowered) < _MIN_TERM_CHARS or lowered in _STOPWORDS:
            continue
        if lowered not in terms:
            terms.append(lowered)
        if len(terms) >= _MAX_TITLE_TERMS:
            break
    return terms


def search_query(title: str, body: str | None) -> str:
    """The query the duplicate search runs. Empty when nothing is searchable."""
    return " ".join([*title_terms(title), *error_strings(body)])


def collect_signals(
    *,
    item_id: str,
    title: str,
    body: str | None,
    config: DispatchConfig,
    issue_num: int = 0,
    labels: tuple[str, ...] = (),
    search: bool = True,
) -> IssueSignals:
    """Collect every host-side signal about one item.

    *search* exists so a caller that must not touch the network (a dry run, a
    test) still gets the deterministic half; the duplicate search is then
    simply not performed, and its absence is visible because ``candidates`` is
    empty while :data:`DEDUPE_SEARCH_SOURCE` is NOT in ``unavailable``.
    """
    candidates: tuple[DuplicateCandidate, ...] = ()
    unavailable: tuple[str, ...] = ()
    if search and config.triage.dedupe_candidates > 0:
        found = duplicate_candidates(
            title=title, body=body, config=config, exclude=issue_num
        )
        if found is None:
            unavailable = (DEDUPE_SEARCH_SOURCE,)
        else:
            candidates = found
    return IssueSignals(
        item_id=item_id,
        issue_num=issue_num,
        body_digest=body_digest(body),
        features=body_features(body),
        labels=tuple(labels),
        candidates=candidates,
        unavailable=unavailable,
    )


def duplicate_candidates(
    *,
    title: str,
    body: str | None,
    config: DispatchConfig,
    exclude: int = 0,
) -> tuple[DuplicateCandidate, ...] | None:
    """Search open and closed issues for *title*/*body*'s terms.

    Returns the candidates, or **None** when the search could not be run — the
    caller reports that as unavailable rather than as "no duplicates found". An
    empty tuple is the real "nothing matched".
    """
    query = search_query(title, body)
    repo = config.project.repo
    if not query or not repo:
        # Nothing searchable is a finding, not a failure: a title of stopwords
        # and a body with no error string give a query that would match the
        # whole repository.
        return ()
    found, failures = _candidates_over_states(
        query, repo=repo, limit=config.triage.dedupe_candidates, exclude=exclude
    )
    if failures and not found:
        return None
    if failures:
        _logger.warning(
            "Triage duplicate search could not read %s issues for %s;"
            " the candidate list is partial",
            " and ".join(failures),
            repo,
        )
    return tuple(found)


def _candidates_over_states(
    query: str, *, repo: str, limit: int, exclude: int
) -> tuple[list[DuplicateCandidate], list[str]]:
    """Merge the open and closed searches, de-duplicated and capped.

    Returns the candidates found and the states whose search FAILED, so the
    caller can tell a partial list from an empty one.
    """
    found: list[DuplicateCandidate] = []
    seen: set[int] = {exclude} if exclude else set()
    failures: list[str] = []
    for state in ("open", "closed"):
        rows = _search_issues(query, repo=repo, state=state, limit=limit)
        if rows is None:
            failures.append(state)
            continue
        if _take_candidates(rows, found, seen, limit):
            break
    return found, failures


def _take_candidates(
    rows: list[DuplicateCandidate],
    found: list[DuplicateCandidate],
    seen: set[int],
    limit: int,
) -> bool:
    """Append the unseen *rows* to *found*; True once the cap is reached."""
    for row in rows:
        if row.number in seen:
            continue
        seen.add(row.number)
        found.append(row)
        if len(found) >= limit:
            return True
    return False


def _search_issues(
    query: str, *, repo: str, state: str, limit: int
) -> list[DuplicateCandidate] | None:
    """One ``gh search issues`` read. None on any failure (never a silent []).

    Read-only by construction: ``gh search`` cannot label, comment or close.
    """
    cmd = [
        "gh",
        "search",
        "issues",
        query,
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(limit),
        "--json",
        "number,title",
    ]
    try:
        result = run(cmd, check=False, timeout=_SEARCH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a failed search is reported, not raised
        _logger.warning(
            "Triage duplicate search (%s) could not run for %s: %s", state, repo, exc
        )
        return None
    if result.returncode != 0:
        _logger.warning(
            "Triage duplicate search (%s) failed for %s: %s",
            state,
            repo,
            (result.stderr or "").strip()[:200],
        )
        return None
    return _parse_candidates(result.stdout, state=state, repo=repo)


def _parse_candidates(
    payload: str, *, state: str, repo: str
) -> list[DuplicateCandidate] | None:
    """Parse ``gh search issues --json number,title``. None when unreadable."""
    try:
        data = json.loads(payload or "[]")
    except (TypeError, ValueError) as exc:
        _logger.warning(
            "Triage duplicate search (%s) returned unreadable JSON for %s: %s",
            state,
            repo,
            exc,
        )
        return None
    if not isinstance(data, list):
        _logger.warning(
            "Triage duplicate search (%s) returned %s, not a list, for %s",
            state,
            type(data).__name__,
            repo,
        )
        return None
    rows: list[DuplicateCandidate] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int):
            continue
        rows.append(
            DuplicateCandidate(number=number, title=str(entry.get("title", "")))
        )
    return rows
