"""``kew report provenance``: the build history behind each console feature (#3143).

The operator lists the console's features in a TOML file — per feature an id,
a title, its routes, and the epics and issue numbers that built it. This
module joins that list to the ledger and writes two files:

* ``provenance.json`` — per feature: its epics, issues, pull requests (with the
  merge queue's ``landed_sha``, ``null`` for a hand-merged PR), run counts by
  run kind, total list cost, review rounds by verdict, the held-out failures of
  each item's latest merge verify, and the test-integrity flags the run-report
  corpus recorded for the feature's runs.
* ``runs.json`` — one row per run, built from the run report's own frontmatter
  (:func:`kew.run_report_sink._frontmatter_fields`) and cut down to
  :data:`RUN_EXPORT_KEYS`. A model id the catalog does not name becomes
  ``"other"``: the corpus holds private provider ids. So does an error
  category outside :data:`PUBLISHED_ERROR_CATEGORIES`, and an empty item id
  is ``null``.

Only the runs recorded against the project repo are read (:func:`in_repo`),
and of those only the ones whose PR link, if any, is on that repo
(:func:`pr_in_repo`) and whose run id is a uuid; only that repo's review
verdicts and merge verifies count
(:func:`_in_repo_verdicts`; ``repo=`` of :func:`item_merge_verify`), and
those recorded against a slug ``kew rename-repo`` renamed to it before the
rename (:func:`kew.repo_rename.repo_match`). Both
files carry ``as_of``, the newest exported ``finished_at``, never the clock:
the same ledger always writes the same bytes.

Only counts, ids, URLs, shas and kew's own vocabulary cross into either file:
no log, prompt, title, branch, error text or held-out node id ever does.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import shutil
import tempfile
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kew.ids import item_id_sort_key
from kew.model_catalog import load_catalog
from kew.repo_rename import RepoMatch, repo_match
from kew.report import _record_item_key
from kew.report_export import _epic_membership, epic_snapshot_nodes
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_report_sink import (
    _INTEGRITY_FLAGS_KEY,
    _carried_flags,
    _frontmatter_fields,
    _read_existing_report,
    _split_frontmatter,
)
from kew.run_store import RunRecord
from kew.service.merge_verify_reading import (
    VerifyUnreadable,
    item_head_events,
    item_merge_verify,
    read_verify_rows,
)

_logger = logging.getLogger(__name__)

PROVENANCE_SCHEMA = "kew/provenance@1"
RUNS_SCHEMA = "kew/provenance-runs@1"
PROVENANCE_FILENAME = "provenance.json"
RUNS_FILENAME = "runs.json"

#: The ONLY keys a ``runs.json`` row may carry. Everything else the run report
#: holds — title, branch, self-check, repo, token splits — stays behind.
RUN_EXPORT_KEYS: tuple[str, ...] = (
    "run_id",
    "item_id",
    "epic",
    "pr_url",
    "run_kind",
    "model",
    "runner",
    "status",
    "outcome",
    "cost_usd",
    "duration_seconds",
    "total_tokens",
    "review_cycles",
    "attempts",
    "started_at",
    "finished_at",
    "error_category",
)

#: What a model id outside the catalog is published as.
OTHER_MODEL = "other"

#: What an error category outside :data:`PUBLISHED_ERROR_CATEGORIES` is published as.
OTHER_ERROR_CATEGORY = "other"

#: The ONLY error categories ``runs.json`` names; any other code is published as
#: :data:`OTHER_ERROR_CATEGORY`, as a model id off the catalog is (#3144): an
#: error category is a code, but nothing stops a writer recording one kew never
#: reviewed. These are the codes kew itself writes where it records a run,
#: seeded from the real ledger's in-repo runs. The public demo's schema reads
#: the same list from ``frontend/demo/error-categories.json``, held equal to
#: this one by ``tests/test_error_category_vocabulary.py``.
PUBLISHED_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "admission_refused",
        "base_integration_exhausted",
        "base_integration_step",
        "budget_exceeded",
        "budget_refused",
        "budget_stop",
        "budget_stopped",
        "circuit_breaker",
        "container_start_failed",
        "containment_refused",
        "cost_anomaly_flagged",
        "credential_issuer_refused",
        "credential_missing",
        "credential_publication_refused",
        "delegation_item_gone",
        "dispatch_child_died",
        "dispatch_child_refused",
        "exception",
        "github_auth",
        "github_unreachable",
        "host_connection_lost",
        "host_interrupted",
        "host_job_deadline_exceeded",
        "host_restart_repeated",
        "killed",
        "launch_failed",
        "needs_human",
        "orphaned_pending_preserved",
        "orphaned_pending_reaped",
        "permanent",
        "prompt_configuration_refused",
        "publication_configuration_refused",
        "publication_failed",
        "publication_held",
        "publication_rate_limited",
        "publication_review_required",
        "publication_unavailable",
        "quota_exhausted",
        "rate_limited",
        "readiness_probe_abandoned",
        "readiness_probe_failed",
        "remote_host_artifact_refused",
        "remote_host_failed",
        "remote_host_interrupted",
        "remote_host_run_kind_refused",
        "requeued_over_cap",
        "reservation_replaced",
        "resource",
        "review_budget_refused",
        "review_out_of_band",
        "review_stage_finished",
        "review_stage_never_started",
        "sandbox_launch_error",
        "sandbox_no_change_needed",
        "sandbox_push_failed",
        "sandbox_realized_checkout_refused",
        "sandbox_refused",
        "sandbox_runtime_missing",
        "sandbox_startup_refused",
        "sandbox_startup_unknown",
        "sandbox_stream_error",
        "sidecar_lifecycle_error",
        "stale_pending",
        "transient",
        "transport_unavailable",
        "watchdog_kill",
    }
)

#: A run with no ``run_kind`` is a dispatch (the column predates the others).
_DEFAULT_RUN_KIND = "dispatch"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PR_URL = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/([0-9]+)/?$")
#: A run id as kew mints it (``str(uuid.uuid4())``): the only shape exported.
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FEATURE_KEYS = frozenset({"id", "title", "routes", "epics", "issues"})


class FeaturesError(ValueError):
    """The features file is missing, not TOML, or not the documented shape."""


@dataclass(frozen=True)
class Feature:
    """One console feature and the work the operator says built it."""

    id: str
    title: str
    routes: tuple[str, ...]
    epics: tuple[str, ...] = ()
    issues: tuple[int, ...] = ()


@dataclass
class ProvenanceExport:
    """The two documents, plus the notes the command prints on stderr."""

    provenance: dict[str, Any]
    runs: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    #: Whether any run in the ledger is recorded against the export's repo.
    repo_matched: bool = True


# --- the features file -------------------------------------------------------


def _strings(value: object, where: str, *, required: bool) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise FeaturesError(f"{where} must be a list of non-empty strings")
    if required and not value:
        raise FeaturesError(f"{where} must name at least one entry")
    return tuple(v.strip() for v in value)


def _issue_numbers(value: object, where: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value
    ):
        raise FeaturesError(f"{where} must be a list of positive issue numbers")
    return tuple(value)


def _feature(entry: object, index: int) -> Feature:
    where = f"features[{index}]"
    if not isinstance(entry, dict):
        raise FeaturesError(f"{where} must be a table")
    unknown = sorted(set(entry) - _FEATURE_KEYS)
    if unknown:
        raise FeaturesError(f"{where} has unknown keys: {', '.join(unknown)}")
    ident, title = entry.get("id"), entry.get("title")
    if not isinstance(ident, str) or not ident.strip():
        raise FeaturesError(f"{where}.id must be a non-empty string")
    if not isinstance(title, str) or not title.strip():
        raise FeaturesError(f"{where}.title must be a non-empty string")
    return Feature(
        id=ident.strip(),
        title=title.strip(),
        routes=_strings(entry.get("routes"), f"{where}.routes", required=True),
        epics=_strings(entry.get("epics"), f"{where}.epics", required=False),
        issues=_issue_numbers(entry.get("issues"), f"{where}.issues"),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise FeaturesError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise FeaturesError(f"{path} is not UTF-8 text: {exc.reason}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise FeaturesError(f"{path} is not valid TOML: {exc}") from exc


def load_features(path: Path) -> tuple[Feature, ...]:
    """The ``[[features]]`` tables of *path*, validated, in file order."""
    entries = _read_toml(path).get("features")
    if not isinstance(entries, list) or not entries:
        raise FeaturesError(f"{path} lists no [[features]]")
    features = tuple(_feature(entry, i) for i, entry in enumerate(entries))
    seen = Counter(f.id for f in features)
    duplicated = sorted(ident for ident, n in seen.items() if n > 1)
    if duplicated:
        raise FeaturesError(f"duplicate feature ids: {', '.join(duplicated)}")
    return features


# --- the run-report corpus ---------------------------------------------------


def _report_flags(text: str) -> tuple[str, list[str]] | None:
    """The run id and integrity flags one run report records, else ``None``.

    Raises ``ValueError`` when the frontmatter or its ``run_id`` is malformed.
    The ``run_id`` is a JSON string, as the sink writes it, or a bare uuid, as
    a YAML emitter writes one; any other bare value is malformed.
    """
    frontmatter, _ = _split_frontmatter(text)
    raw: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in ("run_id", _INTEGRITY_FLAGS_KEY):
            raw[key] = value.strip()
    run_id = _run_id_scalar(raw.get("run_id", "null"))
    if not isinstance(run_id, str) or _INTEGRITY_FLAGS_KEY not in raw:
        return None
    flags = _carried_flags(raw[_INTEGRITY_FLAGS_KEY])
    return None if flags is None else (run_id, flags)


def _run_id_scalar(value: str) -> object:
    """A frontmatter ``run_id`` value: a bare uuid as itself, else JSON."""
    return value if _RUN_ID.fullmatch(value) else json.loads(value)


def read_corpus_flags(corpus: Path) -> dict[str, list[str]]:
    """Each run's integrity flags, by run id, from a run-report sink directory.

    The sink keeps one ``<repo>/<run_id>.md`` per run. A report that cannot be
    read safely, or carries no integrity fields, contributes nothing.
    """
    flags: dict[str, list[str]] = {}
    for path in sorted(corpus.glob("*/*.md")):
        try:
            text = _read_existing_report(path)
        except (OSError, UnicodeDecodeError) as exc:
            _logger.warning("provenance: skipped run report %s: %s", path.name, exc)
            continue
        try:
            parsed = _report_flags(text) if text is not None else None
        except ValueError as exc:
            _logger.warning("provenance: skipped run report %s: %s", path.name, exc)
            continue
        if parsed is not None:
            flags[parsed[0]] = parsed[1]
    return flags


# --- the ledger join ---------------------------------------------------------


def _finite(value: float) -> float:
    return value if math.isfinite(value) else 0.0


def _published_model(model: object, catalog_ids: frozenset[str]) -> object:
    if not isinstance(model, str) or not model:
        return None
    return model if model in catalog_ids else OTHER_MODEL


def _published_error_category(code: object) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    return code if code in PUBLISHED_ERROR_CATEGORIES else OTHER_ERROR_CATEGORY


def _catalog_ids() -> frozenset[str]:
    return frozenset(
        name for row in load_catalog().rows for name in (row.id, *row.aliases)
    )


def run_row(
    record: RunRecord, epic: str | None, catalog_ids: frozenset[str]
) -> dict[str, Any]:
    """One ``runs.json`` row: the run report's fields, allowlist only.

    An empty item id is written as ``null``, never ``""``.
    """
    fields = _frontmatter_fields(record, None)
    fields["epic"] = epic
    fields["item_id"] = fields.get("item_id") or None
    fields["model"] = _published_model(fields.get("model"), catalog_ids)
    fields["error_category"] = _published_error_category(fields.get("error_category"))
    return {key: fields.get(key) for key in RUN_EXPORT_KEYS}


def _landed_shas(store: Any, pr_urls: Iterable[str]) -> dict[str, str | None]:
    """Each PR URL's merge-queue ``landed_sha``; ``None`` when it has none."""
    landed: dict[str, str | None] = {}
    queues: dict[str, dict[int, str]] = {}
    for url in pr_urls:
        match = _PR_URL.match(url)
        if match is None:
            landed[url] = None
            continue
        repo, number = match.group(1), int(match.group(2))
        if repo not in queues:
            entries = store.list_merge_queue(repo, include_settled=True)
            queues[repo] = {e.pr: e.landed_sha for e in entries if e.landed_sha}
        landed[url] = queues[repo].get(number)
    return landed


@dataclass
class _Ledger:
    """What one export reads from the store, read once."""

    #: The project repo every count is scoped to.
    repo: str
    #: Which audit rows are *repo*'s, renames included (read once).
    repo_match: RepoMatch
    records: list[RunRecord]
    #: Item to its first epic: the ``epic`` column of ``runs.json``.
    membership: dict[str, str]
    known_epics: frozenset[str]
    #: Each epic a feature names, to the nodes its own latest snapshot holds.
    nodes: dict[str, frozenset[str]]
    #: Every ``verify_completed`` row, or why the store would not give them.
    verify_history: Sequence[Any] | VerifyUnreadable = ()

    def verify_rows(self) -> Sequence[Any]:
        """The export's one read of the verify history; raises when it failed."""
        if isinstance(self.verify_history, VerifyUnreadable):
            raise VerifyUnreadable(str(self.verify_history))
        return self.verify_history

    def items_of(self, feature: Feature) -> list[str]:
        items = {item for ref in feature.epics for item in self.nodes.get(ref, ())}
        items.update(str(n) for n in feature.issues)
        return sorted(items, key=item_id_sort_key)

    def epic_of(self, feature: Feature, item: str) -> str | None:
        """The epic *feature* names that holds *item*, else the item's first."""
        for ref in feature.epics:
            if item in self.nodes.get(ref, ()):
                return ref
        return self.membership.get(item)

    def runs_of(self, items: Sequence[str]) -> list[RunRecord]:
        wanted = set(items)
        return [r for r in self.records if _record_item_key(r) in wanted]


#: One attested review round: ``(item_id, round, head_sha, verdict)``.
RoundKey = tuple[str, int, str, str]


def _in_repo_verdicts(
    store: Any, items: Sequence[str], repo: RepoMatch
) -> Counter[RoundKey]:
    """How many review verdicts recorded against *repo* name each round key.

    ``review_rounds`` records no repo, and a ledger shared by several repos
    holds same-numbered items of each. A governed review or an operator
    send-back attests its round as an ``adversarial_review_verdict`` audit
    row, which does record the repo, and only such a row recorded against
    *repo* vouches for a round. Not every round has one: the merge queue
    attests the round a failed merge verify records as ``merge_verify``, and a
    send-back on a project with no ``project.repo`` records no repo. Those
    rounds are not counted, and the export says how many (:func:`_vouched`).
    A row recorded against a slug ``kew rename-repo`` renamed to *repo*
    vouches too, when it came before the rename (:class:`RepoMatch`).
    Fails closed: a row with no repo, or another repo's, vouches for nothing.
    """
    verdicts: Counter[RoundKey] = Counter()
    if not repo.repo or not items:
        return verdicts
    rows = store.list_audit_events(
        limit=None, event=REVIEW_VERDICT_EVENT, item_ids=list(items)
    )
    verdicts.update(
        key
        for row in rows
        if repo.holds(row) and (key := _round_key(row.payload)) is not None
    )
    return verdicts


def _round_key(payload: object) -> RoundKey | None:
    """The round a verdict row's payload names, ``None`` when malformed."""
    if not isinstance(payload, Mapping):
        return None
    item, review_round = payload.get("item_id"), payload.get("round")
    head, verdict = payload.get("head_sha"), payload.get("verdict")
    if not (isinstance(item, str) and isinstance(verdict, str)):
        return None
    if not (isinstance(review_round, int) and isinstance(head, str) and head):
        return None
    return item, review_round, head.lower(), verdict


def _vouched(
    item: str, rounds: Iterable[Any], verdicts: Counter[RoundKey]
) -> tuple[list[Any], int]:
    """The *rounds* an in-repo verdict vouches for, and how many it does not.

    Each verdict row vouches for one round of its key: two rounds on one key
    (a fork reviewing the same commit after a retry) need two rows.
    """
    left = Counter(verdicts)
    kept: list[Any] = []
    dropped = 0
    for r in rounds:
        key = (item, r.round, str(r.head_sha).lower(), str(r.verdict))
        if left[key] > 0:
            left[key] -= 1
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


def _note_once(notes: list[str], note: str) -> None:
    if note not in notes:
        notes.append(note)


def _review_rounds(
    store: Any, items: Sequence[str], verdicts: Counter[RoundKey], notes: list[str]
) -> dict[str, int]:
    """Review rounds by verdict, only those an in-repo verdict vouches for."""
    counts: Counter[str] = Counter()
    for item in items:
        kept, dropped = _vouched(item, store.list_review_rounds(item), verdicts)
        counts.update(str(r.verdict) for r in kept)
        if dropped:
            rounds = "round has" if dropped == 1 else "rounds have"
            verb = "is" if dropped == 1 else "are"
            _note_once(
                notes,
                f"item {item}: {dropped} review {rounds} no in-repo verdict "
                f"and {verb} not counted",
            )
    return dict(sorted(counts.items()))


def _in_repo_heads(
    store: Any, item: str, repo: RepoMatch, verdicts: Counter[RoundKey]
) -> tuple[list[Any], tuple[Any, ...]]:
    """The cycle's in-repo rounds and the item's newest in-repo head events.

    The join's head set is built from these alone, so another repo's newer
    round or event can never push this repo's newest head out of it.
    """
    try:
        cycle = store.review_rounds_in_cycle(item)
    except Exception as exc:
        raise VerifyUnreadable(f"the review rounds did not answer: {exc}") from exc
    rounds, _dropped = _vouched(item, cycle, verdicts)
    return rounds, item_head_events(store, item, repo=repo)


def _held_out(
    store: Any,
    items: Mapping[str, str | None],
    ledger: _Ledger,
    verdicts: Counter[RoundKey],
    notes: list[str],
) -> dict[str, int]:
    """Held-out failures of each item's latest project-repo merge verify, counted.

    *items* maps each item to the epic whose snapshot names its PR head. The
    heads come from in-repo rounds and events only (:func:`_in_repo_heads`),
    and only a verify recorded against the ledger's repo joins: one with no
    repo never does. Every item joins against the ledger's one read of the
    verify history.
    """
    totals = {"items_failed": 0, "tests_failed": 0, "tests_undeclared": 0}
    for item, epic_ref in items.items():
        try:
            rounds, events = _in_repo_heads(store, item, ledger.repo_match, verdicts)
            verify = item_merge_verify(
                store,
                item,
                epic_ref=epic_ref,
                rounds=rounds,
                events=events,
                repo=ledger.repo_match,
                rows=ledger.verify_rows(),
            )
        except VerifyUnreadable as exc:
            notes.append(f"item {item}: merge verify unreadable ({exc})")
            continue
        held = verify.held_out if verify is not None else None
        if held is None or held.outcome != "failed":
            continue
        totals["items_failed"] += 1
        totals["tests_failed"] += len(held.failed) + held.failed_more
        totals["tests_undeclared"] += len(held.undeclared) + held.undeclared_more
    return totals


def _integrity(
    runs: Sequence[RunRecord], corpus: Mapping[str, list[str]] | None
) -> dict[str, Any]:
    flags: Counter[str] = Counter()
    flagged = 0
    for record in runs:
        found = (corpus or {}).get(record.run_id) or []
        flagged += bool(found)
        flags.update(found)
    return {"runs_flagged": flagged, "flags": dict(sorted(flags.items()))}


def _unknown_issue(store: Any, ledger: _Ledger, issue: int) -> bool:
    """No in-repo run, in-repo review verdict or epic node names *issue*."""
    item = str(issue)
    return (
        item not in ledger.membership
        and not ledger.runs_of([item])
        and not _in_repo_verdicts(store, [item], ledger.repo_match)
    )


def _feature_notes(store: Any, feature: Feature, ledger: _Ledger) -> list[str]:
    notes = [
        f"feature {feature.id}: epic {ref!r} is not in the ledger"
        for ref in feature.epics
        if ref not in ledger.known_epics
    ]
    notes.extend(
        f"feature {feature.id}: issue {issue} has no run, review round "
        "or epic node in the ledger"
        for issue in feature.issues
        if _unknown_issue(store, ledger, issue)
    )
    if not feature.epics and not feature.issues:
        notes.append(f"feature {feature.id}: names no epic or issue")
    return notes


def _feature_entry(
    store: Any,
    feature: Feature,
    ledger: _Ledger,
    corpus: Mapping[str, list[str]] | None,
    notes: list[str],
) -> dict[str, Any]:
    items = ledger.items_of(feature)
    runs = ledger.runs_of(items)
    notes.extend(_feature_notes(store, feature, ledger))
    pr_urls = sorted({r.pr_url for r in runs if r.pr_url})
    landed = _landed_shas(store, pr_urls)
    kinds = Counter(r.run_kind or _DEFAULT_RUN_KIND for r in runs)
    verdicts = _in_repo_verdicts(store, items, ledger.repo_match)
    return {
        "id": feature.id,
        "title": feature.title,
        "routes": list(feature.routes),
        "epics": list(feature.epics),
        "issues": items,
        "prs": [{"url": url, "landed_sha": landed[url]} for url in pr_urls],
        "runs": dict(sorted(kinds.items())),
        "list_cost_usd": round(sum(_finite(r.estimated_cost_usd) for r in runs), 6),
        "review_rounds": _review_rounds(store, items, verdicts, notes),
        "held_out": _held_out(
            store,
            {item: ledger.epic_of(feature, item) for item in items},
            ledger,
            verdicts,
            notes,
        ),
        "integrity": _integrity(runs, corpus),
    }


def in_repo(record: RunRecord, repo: str) -> bool:
    """Whether *record* was dispatched against *repo*. Fails closed.

    The ledger is shared by every repo the machine dispatches into, and a
    private repo's runs must never reach a public export. A run with no
    recorded repo is dropped whatever its ``pr_url`` says: only the recorded
    repo vouches for where a run belongs.
    """
    return bool(repo) and record.repo == repo


def pr_in_repo(record: RunRecord, repo: str) -> bool:
    """Whether *record*'s pull request, when it names one, is on *repo*. Fails closed.

    The recorded repo vouches for the run, and the PR link must agree with it:
    a run of the project repo whose ``pr_url`` points at any other repository,
    or is not exactly a GitHub pull request URL of *repo* (``/<repo>/pull/<n>``
    on the GitHub host, as :data:`_PR_URL` reads one), contributes nothing to
    either file. A run with no PR at all is kept.
    """
    if record.pr_url is None:
        return True
    pattern = rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*"
    return bool(repo) and re.fullmatch(pattern, record.pr_url) is not None


def _dropped_note(dropped: int, why: str) -> list[str]:
    if not dropped:
        return []
    runs = "1 run" if dropped == 1 else f"{dropped} runs"
    verb = "was" if dropped == 1 else "were"
    return [f"{runs} with {why} {verb} dropped"]


def _project_runs(store: Any, repo: str) -> tuple[list[RunRecord], list[str]]:
    """The runs of *repo* the export may name, oldest first; and the notes.

    A run of *repo* is dropped whole when its PR, if any, is elsewhere
    (:func:`pr_in_repo`), or when its run id is not the uuid kew mints: a
    legacy id is not a shape the public demo's schema admits. The notes count
    each kind of dropped run and never name one: the dropped value is what
    must not get out.
    """
    scoped = [r for r in store.list_since(_EPOCH) if in_repo(r, repo)]
    on_repo = [r for r in scoped if pr_in_repo(r, repo)]
    records = sorted(
        (r for r in on_repo if _RUN_ID.fullmatch(r.run_id or "")), key=_run_sort_key
    )
    return records, [
        *_dropped_note(
            len(scoped) - len(on_repo), "a pull request off the project repo"
        ),
        *_dropped_note(len(on_repo) - len(records), "a run id that is not a uuid"),
    ]


def _run_sort_key(record: RunRecord) -> tuple[str, str]:
    started = record.started_at.isoformat() if record.started_at else ""
    return started, record.run_id


def _verify_history(store: Any) -> Sequence[Any] | VerifyUnreadable:
    """Every ``verify_completed`` row, read once per export, or why not.

    Every row, not the work page's newest window: the export covers the whole
    history.
    """
    try:
        return read_verify_rows(store, window=None)
    except VerifyUnreadable as exc:
        return exc


def build_provenance(
    store: Any,
    features: Sequence[Feature],
    corpus: Mapping[str, list[str]] | None,
    *,
    repo: str,
) -> ProvenanceExport:
    """Join *features* to the ledger in *store* and the *corpus* flags.

    Only the runs recorded against *repo* are read (:func:`in_repo`), and of
    those only the ones whose PR, if any, is on *repo* (:func:`pr_in_repo`).
    *corpus* is ``None`` when no run-report corpus was found: every integrity
    count is then 0 and the export says so in ``integrity_source``.
    """
    records, notes = _project_runs(store, repo)
    # A run of *repo* is either exported or counted in a drop note.
    repo_matched = bool(records or notes)
    known_epics = frozenset(store.list_epic_refs())
    named = {ref for f in features for ref in f.epics if ref in known_epics}
    ledger = _Ledger(
        repo=repo,
        repo_match=repo_match(store, repo),
        records=records,
        membership=_epic_membership(store),
        known_epics=known_epics,
        nodes={
            ref: frozenset(epic_snapshot_nodes(store, ref)) for ref in sorted(named)
        },
        verify_history=_verify_history(store),
    )
    entries = [_feature_entry(store, f, ledger, corpus, notes) for f in features]
    as_of = _as_of(records)
    catalog_ids = _catalog_ids()
    rows = [
        run_row(r, ledger.membership.get(_record_item_key(r)), catalog_ids)
        for r in records
    ]
    return ProvenanceExport(
        provenance={
            "schema": PROVENANCE_SCHEMA,
            "as_of": as_of,
            "integrity_source": "none" if corpus is None else "corpus",
            "features": entries,
        },
        runs={"schema": RUNS_SCHEMA, "as_of": as_of, "runs": rows},
        notes=notes,
        repo_matched=repo_matched,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_of(records: Sequence[RunRecord]) -> str | None:
    """The newest ``finished_at`` of the exported runs, as its row states it.

    Read from the ledger, never the clock, so the same ledger always exports
    the same bytes. ``None`` when no exported run has finished.
    """
    finished = [r.finished_at for r in records if r.finished_at is not None]
    return max(finished, key=_aware).isoformat() if finished else None


def _render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def _write_temp(out_dir: Path, name: str, text: str) -> Path:
    """*text* in a new temp file in *out_dir*, beside the file it replaces."""
    fd, raw = tempfile.mkstemp(dir=out_dir, prefix=f".{name}.", suffix=".tmp")
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        temp.chmod(0o644)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def write_provenance(export: ProvenanceExport, out_dir: Path) -> tuple[Path, Path]:
    """Write ``provenance.json`` and ``runs.json`` into *out_dir*: both or neither.

    Both documents are rendered first, each is written to a temp file in
    *out_dir*, and only then are both moved into place. A target that exists
    but is not a regular file is refused before either move. When the second
    move fails, the first target is put back as it was (its old bytes, or no
    file). Raises :class:`OSError` when *out_dir* cannot be written; no temp
    file is left.
    """
    targets = (
        (out_dir / PROVENANCE_FILENAME, _render(export.provenance)),
        (out_dir / RUNS_FILENAME, _render(export.runs)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, _ in targets:
        if path.exists() and not path.is_file():
            raise IsADirectoryError(errno.EISDIR, "not a regular file", str(path))
    temps: list[Path] = []
    try:
        for path, text in targets:
            temps.append(_write_temp(out_dir, path.name, text))
        first = targets[0][0]
        backup = _backup(first)
        temps.extend([backup] if backup is not None else [])
        os.replace(temps[0], first)
        try:
            os.replace(temps[1], targets[1][0])
        except BaseException:
            _restore(first, backup)
            raise
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
    return targets[0][0], targets[1][0]


def _backup(path: Path) -> Path | None:
    """A copy of *path* beside it, to put back if the second move fails.

    ``None`` when there is no *path*: restoring then removes the new one.
    """
    if not path.exists():
        return None
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".bak")
    os.close(fd)
    backup = Path(raw)
    try:
        shutil.copy2(path, backup)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def _restore(path: Path, backup: Path | None) -> None:
    """Put *path* back as :func:`_backup` found it."""
    if backup is None:
        path.unlink(missing_ok=True)
    else:
        os.replace(backup, path)
