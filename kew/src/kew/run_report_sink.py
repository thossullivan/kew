"""Run-report sink: per-run decision-intelligence exports (#1578).

Every kew-orchestrated run, in every repo, can export one small markdown
**run report** to a machine-level sink so cross-project questions ("which
model handles docs PRs well", "what did runs shaped like this cost") have a
corpus to retrieve from. Design: docs/design/2026-08-21-run-report-sink-design.md.

The report contract:

- YAML frontmatter of kew-authored structured fields ONLY — no transcript
  text, no agent prose (the lessons D8/D9 discipline). Missing RunRecord
  fields are omitted, never fabricated.
- Body is a mechanical rendering of the same fields plus the run's
  chain-attested audit event ids.
- Export is best-effort: any failure logs and never affects the run.
- Runs that write code (dispatch, ci_fix, resolve, review_remediation) carry
  test-integrity counts measured from the run's ``base...head`` diff (#2494):
  integers and kew's own flag names, never a line of the diff.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.run_store import (
    CI_FIX_RUN_KIND,
    RESOLVE_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
)
from kew.runner_fields import exception_text
from kew.test_integrity import (
    FLAG_ASSERTS_REMOVED,
    FLAG_CHANGED_EXPECTED,
    FLAG_MOCK_ANY_ADDED,
    FLAG_RAISES_LOOSENED,
    FLAG_SKIP_ADDED,
    FLAG_TEST_ONLY_FIX_ROUND,
    FLAG_TESTS_REMOVED,
    TestIntegrityReport,
)

if TYPE_CHECKING:  # pragma: no cover
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord, RunStore

_logger = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION = 1
_EXPORTED_AT_KEY = "exported_at"
_ALLOWED_INDEXER_KEYS = ("permalink",)
_MAX_REPORT_BYTES = 64 * 1024

# Bounded fetch when collecting the run's audit events; a run emits a handful
# of events, so a generous page filtered by run_id is enough.
_EVIDENCE_EVENT_LIMIT = 500

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)", re.MULTILINE)


#: Run kinds whose branch is the run's own code change, so its test diff is
#: the run's doing. A reviewer, planner or loop run writes no branch.
INTEGRITY_RUN_KINDS = frozenset(
    {"dispatch", CI_FIX_RUN_KIND, RESOLVE_RUN_KIND, REVIEW_REMEDIATION_RUN_KIND}
)
#: The integer test-integrity fields, in frontmatter order.
_INTEGRITY_COUNT_KEYS = (
    "tests_touched",
    "asserts_removed",
    "skips_added",
    "changed_expected",
    "test_only_fix_round",
)
_INTEGRITY_FLAGS_KEY = "integrity_flags"
_INTEGRITY_KEYS = (*_INTEGRITY_COUNT_KEYS, _INTEGRITY_FLAGS_KEY)
_KNOWN_FLAGS = frozenset(
    {
        FLAG_ASSERTS_REMOVED,
        FLAG_TESTS_REMOVED,
        FLAG_SKIP_ADDED,
        FLAG_CHANGED_EXPECTED,
        FLAG_TEST_ONLY_FIX_ROUND,
        FLAG_RAISES_LOOSENED,
        FLAG_MOCK_ANY_ADDED,
    }
)
_COUNT_VALUE = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")


@dataclass(frozen=True)
class IntegrityDiff:
    """Where the run's ``base...head`` diff can be read: a kew-trusted
    checkout and the two refs, resolved at report time."""

    checkout: Path
    base_ref: str
    head_ref: str


def _slug(value: str) -> str:
    """Filesystem-safe path component ("owner/repo" -> "owner--repo").

    A component made only of dots/dashes (e.g. a repo of ``..``) maps to
    ``unknown`` — dot-only names would otherwise resolve outside the sink.
    """
    cleaned = _SLUG_UNSAFE.sub("--", value.strip())
    if not cleaned or not cleaned.strip(".-"):
        return "unknown"
    return cleaned


def _outcome(record: RunRecord) -> str:
    """Terminal outcome for retrieval: merged beats the raw run status."""
    if record.pr_merged_at:
        return "merged"
    return str(record.status)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rounded(value: float, digits: int) -> float | None:
    """Non-finite floats (NaN/inf) are dropped — they are corruption, not
    measurements. A genuine ``0.0`` is kept: RunRecord's cost and duration are
    non-optional, so zero is a recorded value (a free local run), and erasing
    it would recreate exactly the free-vs-unpriced ambiguity of #1565."""
    return round(value, digits) if math.isfinite(value) else None


def _frontmatter_fields(record: RunRecord, repo: str | None) -> dict[str, Any]:
    """The allowlisted decision fields. Unrecorded values are dropped so a
    sparse RunRecord (a known reality, 2026-07-01 dogfood finding) produces a
    sparse-but-honest report rather than fabricated values. Non-optional
    numerics (costs, token counts, attempts) are always emitted — for them
    zero is a recorded fact, not an absence. The falsy-dropped group is the
    string fields plus ``issue`` (``0`` is the documented non-numeric-item
    sentinel), where empty means "not recorded" by RunRecord convention, and
    ``self_check``, where false means "this prompt carried no self-check" —
    true on the dispatch runs that rendered it is the whole signal (#2454)."""
    from kew.runner_fields import reported_model_identity, service_tier_token

    fields: dict[str, Any] = {
        "run_id": record.run_id,
        "repo": repo or record.repo,
        "item_id": record.item_id,
        "reported_model": reported_model_identity(record.reported_model) or None,
        "reported_service_tier": service_tier_token(record.reported_service_tier)
        or None,
        "review_cycles": record.review_cycles,
        "status": str(record.status),
        "outcome": _outcome(record),
        "pr_url": record.pr_url,
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
        "cost_usd": _rounded(record.estimated_cost_usd, 6),
        "duration_seconds": _rounded(record.duration_seconds, 2),
        "total_tokens": record.total_tokens,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "attempts": record.attempt_number,
    }
    dropped_when_falsy: dict[str, Any] = {
        "issue": record.issue_num,
        "title": record.title,
        "branch": record.branch,
        "complexity": record.complexity,
        "run_kind": record.run_kind,
        "runner": record.runner_name,
        "model": record.model,
        "error_category": record.error_category,
        "self_check": record.self_check,
    }
    fields.update({k: v for k, v in dropped_when_falsy.items() if v})
    return {k: v for k, v in fields.items() if v is not None}


#: The keys a command's ``--json`` result summary carries (#2505): a subset of
#: the report frontmatter under the SAME names, so an agent reads one vocabulary
#: whether it parses a command's stdout or the exported report.
RESULT_SUMMARY_KEYS = (
    "run_id",
    "item_id",
    "outcome",
    "error_category",
    "pr_url",
    "cost_usd",
    "duration_seconds",
)


def result_summary_fields(record: RunRecord) -> dict[str, Any]:
    """The summary keys of *record*'s frontmatter; an unrecorded one is None
    (an empty string included: a run with no PR has ``pr_url: null``)."""
    fields = _frontmatter_fields(record, None)
    return {
        key: fields.get(key) if fields.get(key) != "" else None
        for key in RESULT_SUMMARY_KEYS
    }


def _evidence_ids(store: RunStore | None, record: RunRecord) -> list[str]:
    """Chain-attested audit event ids for this run, ``audit:<id>:<event>``.

    The fetch is scoped to the run's work item (``issue_num``, falling back to
    ``item_id`` for the ``0``-sentinel runs) — a run with neither gets no
    evidence rather than a misleading slice of a store-wide window. Only
    events that carry this ``run_id`` qualify. Best-effort: an unreadable
    store yields an empty list, never an error.
    """
    if store is None:
        return []
    try:
        if record.issue_num:
            events = store.list_audit_events(
                limit=_EVIDENCE_EVENT_LIMIT, issue_num=record.issue_num
            )
        elif record.item_id:
            events = store.list_audit_events(
                limit=_EVIDENCE_EVENT_LIMIT, item_id=record.item_id
            )
        else:
            return []
        return [f"audit:{e.id}:{e.event}" for e in events if e.run_id == record.run_id]
    except Exception as exc:
        _logger.warning("run-report: audit events unavailable: %s", exc)
        return []


# The code points YAML forbids as non-printable that json.dumps(ensure_ascii=
# False) passes through literally: DEL, the C1 block (which contains NEL
# U+0085), and the Unicode line/paragraph separators.
_YAML_FORBIDDEN = re.compile("[\x7f-\x9f\u2028\u2029]")


def _escape_yaml_forbidden(text: str) -> str:
    return _YAML_FORBIDDEN.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _render_float(value: float) -> str:
    # Decimal form always: repr's exponent shapes ("2e-05") have no dot in the
    # mantissa and parse back as *strings* under YAML 1.1 — reachable for any
    # sub-cent cost, i.e. exactly the cheap local runs this corpus compares.
    # The dot is kept ("1800.0", never "1800") so a field's YAML type is
    # invariant across values, not float-or-int depending on the number.
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _render_yaml_value(value: Any) -> str:
    """A YAML-safe scalar or list.

    Strings and lists render via ``json.dumps(ensure_ascii=False)`` — NOT
    ``ensure_ascii=True``, whose surrogate-pair ``\\uXXXX`` escapes for
    non-BMP characters (emoji in issue titles) decode as two unpaired
    surrogates under YAML, producing strings that cannot be UTF-8 encoded.
    The few code points YAML forbids (DEL/C1/NEL, U+2028/29) are escaped
    afterwards; the forbidden set contains no quote or backslash, so the
    substitution is safe on already-quoted output. Numbers render decimally.
    """
    if isinstance(value, bool):  # ``self_check``, and defensively any later one
        return "true" if value else "false"
    if isinstance(value, float):
        return _render_float(value)
    if isinstance(value, int):
        return repr(value)
    return _escape_yaml_forbidden(json.dumps(value, ensure_ascii=False))


def integrity_fields(report: TestIntegrityReport) -> dict[str, Any]:
    """The report's test-integrity frontmatter: counts and flag names only.

    Every evidence line in *report* is diff text, adversary-authored, so none
    of it is copied — only how many there were, and kew's own flag names.
    ``test_only_fix_round`` counts the fix-round commits, not their files.
    """
    fix_rounds = {e.commit for e in report.test_only_fix_rounds}
    return {
        "tests_touched": len(report.test_files),
        "asserts_removed": len(report.asserts_removed),
        "skips_added": len(report.skips_added),
        "changed_expected": len(report.changed_expected),
        "test_only_fix_round": len(fix_rounds),
        _INTEGRITY_FLAGS_KEY: [f for f in report.flags if f in _KNOWN_FLAGS],
    }


def _integrity_values(existing: str) -> dict[str, str]:
    """The raw integrity values in *existing*'s frontmatter, by key."""
    try:
        frontmatter, _ = _split_frontmatter(existing)
    except ValueError as exc:
        _logger.warning("run-report: integrity fields not carried: %s", exc)
        return {}
    raw: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in _INTEGRITY_KEYS:
            raw[key] = value.strip()
    return raw


def _carried_flags(text: str) -> list[str] | None:
    """*text* as a flag list, when every entry is one of kew's flag names."""
    try:
        flags = json.loads(text)
    except ValueError as exc:
        _logger.warning("run-report: integrity flags not carried: %s", exc)
        return None
    if not isinstance(flags, list) or not all(f in _KNOWN_FLAGS for f in flags):
        return None
    return flags


def _carried_integrity(existing: str | None) -> dict[str, Any] | None:
    """The integrity fields an earlier export of this report recorded.

    A re-export with no diff to read (``sync_outcomes`` settling the outcome
    long after the checkout is gone) keeps what the first export measured
    rather than erasing it. Only values this module could have written are
    carried — whole-set or nothing — so a hand-edited report cannot smuggle
    free text through.
    """
    raw = _integrity_values(existing) if existing is not None else {}
    if set(raw) != set(_INTEGRITY_KEYS):
        return None
    if not all(_COUNT_VALUE.match(raw[k]) for k in _INTEGRITY_COUNT_KEYS):
        return None
    flags = _carried_flags(raw[_INTEGRITY_FLAGS_KEY])
    if flags is None:
        return None
    carried: dict[str, Any] = {k: int(raw[k]) for k in _INTEGRITY_COUNT_KEYS}
    carried[_INTEGRITY_FLAGS_KEY] = flags
    return carried


def _measured_integrity(
    record: RunRecord, diff: IntegrityDiff | None, path: Path
) -> dict[str, Any] | None:
    """Integrity fields for *record*: measured from *diff* now, else carried
    from the report already at *path*, else none. Non-code runs get none."""
    if record.run_kind not in INTEGRITY_RUN_KINDS:
        return None
    if diff is not None:
        from kew.review_integrity import read_integrity_report

        report = read_integrity_report(diff.checkout, diff.base_ref, diff.head_ref)
        if report is not None:
            return integrity_fields(report)
    try:
        return _carried_integrity(_read_existing_report(path))
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning(
            "run-report: earlier integrity fields unreadable: %s", exception_text(exc)
        )
        return None


def render_report(
    record: RunRecord,
    repo: str | None,
    evidence: list[str],
    integrity: dict[str, Any] | None = None,
) -> str:
    """The full markdown report: frontmatter contract + mechanical body.

    *integrity* is the :func:`integrity_fields` mapping; it is rendered only
    for a code-writing run kind, whatever the caller passes.
    """
    fields = _frontmatter_fields(record, repo)
    if integrity is not None and record.run_kind in INTEGRITY_RUN_KINDS:
        fields.update({k: integrity[k] for k in _INTEGRITY_KEYS})
    if evidence:
        fields["evidence"] = evidence
    fields["schema"] = REPORT_SCHEMA_VERSION
    fields[_EXPORTED_AT_KEY] = datetime.now(UTC).isoformat()

    lines = ["---"]
    lines += [f"{key}: {_render_yaml_value(value)}" for key, value in fields.items()]
    lines.append("---")
    lines.append("")
    lines.append(f"# Run {record.run_id}")
    lines.append("")
    # Body values go through the same escaping as the frontmatter: an issue
    # title is externally supplied text, and rendered raw it could forge
    # report structure (a newline plus "---" opens a second fence).
    lines += [
        f"- **{key}**: {_render_yaml_value(value)}"
        for key, value in fields.items()
        if key not in ("evidence", "schema")
    ]
    if evidence:
        lines.append("")
        lines.append("## Attested events")
        lines.append("")
        lines += [f"- {eid}" for eid in evidence]
    lines.append("")
    return "\n".join(lines)


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Return frontmatter and body from a fenced markdown report."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("missing opening frontmatter fence")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise ValueError("missing closing frontmatter fence")


def _stable_body(body: str) -> str:
    """The body minus its exported_at line and outer whitespace. Indexers that
    parse with python-frontmatter (basic-memory) strip the body on read and
    write it back without the trailing newline, so outer whitespace is not
    report content."""
    prefix = f"- **{_EXPORTED_AT_KEY}**: "
    return "".join(
        line for line in body.splitlines(keepends=True) if not line.startswith(prefix)
    ).strip()


def _is_safe_report_inode(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_size <= _MAX_REPORT_BYTES
    )


def _read_existing_report(path: Path) -> str | None:
    # A first export has no report yet; that is the normal case, not an error.
    if not os.path.lexists(path):
        return None
    entry_info = os.lstat(path)
    if not _is_safe_report_inode(entry_info):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        opened_info = os.fstat(fd)
        if not _is_safe_report_inode(opened_info):
            return None
        if (entry_info.st_dev, entry_info.st_ino) != (
            opened_info.st_dev,
            opened_info.st_ino,
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_REPORT_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_REPORT_BYTES:
            return None
        return data.decode("utf-8", errors="strict")
    finally:
        os.close(fd)


def _frontmatter_keys_match(existing: str, new: str) -> bool:
    existing_keys = _FRONTMATTER_KEY.findall(existing)
    existing_key_set = set(existing_keys)
    if len(existing_keys) != len(existing_key_set):
        return False
    new_key_set = set(_FRONTMATTER_KEY.findall(new))
    allowed_keys = new_key_set | set(_ALLOWED_INDEXER_KEYS)
    return new_key_set <= existing_key_set <= allowed_keys


def _report_is_current(path: Path, content: str) -> bool:
    """Compare stable report content, failing toward an atomic rewrite."""
    try:
        existing = _read_existing_report(path)
        if existing is None:
            return False
        existing_frontmatter, existing_body = _split_frontmatter(existing)
        new_frontmatter, new_body = _split_frontmatter(content)
        schema_line = f"schema: {REPORT_SCHEMA_VERSION}"
        return (
            _frontmatter_keys_match(existing_frontmatter, new_frontmatter)
            and schema_line in existing_frontmatter.splitlines()
            and _stable_body(existing_body) == _stable_body(new_body)
        )
    except Exception as exc:
        _logger.warning(
            "run-report: current-report check failed, rewriting: %s",
            exception_text(exc),
        )
        return False


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export_run_report(
    config: DispatchConfig,
    store: RunStore | None,
    record: RunRecord,
    *,
    repo: str | None = None,
    integrity_diff: IntegrityDiff | None = None,
) -> Path | None:
    """Write the run's report into the sink. Best-effort by contract: returns
    the written path, or ``None`` when disabled or on any failure (logged) —
    a report that cannot be written must never fail a run that already ran.
    The settings read sits inside the ``try`` so even a config object with no
    ``run_reports`` attribute keeps the promise.

    *integrity_diff* names the run's ``base...head`` diff; the caller vouches
    for the checkout, since git runs in it. Without one, a code-writing run's
    report keeps the integrity fields an earlier export measured."""
    try:
        settings = config.run_reports
        if not settings.enabled:
            return None
        effective_repo = repo or record.repo or config.project.repo or ""
        sink = Path(settings.sink_path).expanduser()
        path = sink / _slug(effective_repo) / f"{_slug(record.run_id)}.md"
        content = render_report(
            record,
            effective_repo or None,
            _evidence_ids(store, record),
            _measured_integrity(record, integrity_diff, path),
        )
        if _report_is_current(path, content):
            return path
        _atomic_write(path, content)
        return path
    except Exception as exc:
        _logger.warning("run-report export failed (non-fatal): %s", exception_text(exc))
        return None


def repo_report_dir(config: DispatchConfig, repo: str) -> Path:
    """The sink directory *repo*'s run reports are written to."""
    return Path(config.run_reports.sink_path).expanduser() / _slug(repo)


def repo_reports(config: DispatchConfig, repo: str) -> list[Path]:
    """Every run report the sink holds for *repo*, sorted."""
    source = repo_report_dir(config, repo)
    if not source.is_dir():
        return []
    return sorted(source.glob("*.md"))


#: The audit event a host publication binds its pushed commit to its run with,
#: and the envelope only the host publisher writes it under.
_PUBLICATION_INTENT_EVENT = "publication_intent"
_HOST_PUBLISHER = ("policy", "host-publication")


def _publishing_run_ids(store: RunStore, item_id: str, head_sha: str) -> list[str]:
    """The runs whose host publication of *item_id* pushed *head_sha*."""
    events = store.list_audit_events(
        limit=None, event=_PUBLICATION_INTENT_EVENT, item_id=item_id
    )
    run_ids: list[str] = []
    for event in events:
        payload = event.payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (
            (event.actor_type, event.actor_id) == _HOST_PUBLISHER
            and isinstance(payload, dict)
            and payload.get("commit_sha") == head_sha
            and event.run_id
            and event.run_id not in run_ids
        ):
            run_ids.append(event.run_id)
    return run_ids


def export_published_integrity(
    config: DispatchConfig,
    store: RunStore,
    *,
    item_id: str,
    head_sha: str,
    diff: IntegrityDiff,
) -> list[Path]:
    """Re-export the report of the run that published *head_sha*, measured.

    A code-writing run's own checkout is agent-writable and its work is
    committed after it finishes, so its completion snapshot has no diff kew
    can read. The reviewer's checkout is the first kew-materialized one that
    holds the published head: *diff* names its pinned refs. Best-effort like
    :func:`export_run_report` — returns the reports written, never raises."""
    try:
        if not config.run_reports.enabled:
            return []
        written: list[Path] = []
        for run_id in _publishing_run_ids(store, item_id, head_sha):
            record = store.get(run_id)
            if record is None or record.run_kind not in INTEGRITY_RUN_KINDS:
                continue
            path = export_run_report(config, store, record, integrity_diff=diff)
            if path is not None:
                written.append(path)
        return written
    except Exception as exc:
        _logger.warning(
            "run-report integrity export failed (non-fatal): %s", exception_text(exc)
        )
        return []
