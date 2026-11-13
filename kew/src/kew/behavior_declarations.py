"""Rounds a missing **Behavior changes** declaration rejects (#2872).

A reviewer that REJECTS an ``undeclared_test_edits`` or ``tests_removed``
answer (#2830) is asking for lines in the PR body, not for a code change. The
remediation that round earned fixed nothing — nothing was wrong — and wrote no
declaration either, because nothing told it a report section was the fix; the
drive re-ran it at the same head until the inbox flagged the loop.

This module is the host's side of that answer:

- the round's flagged node ids are recorded beside its stored sidecar when the
  reviewer runs (:func:`record_flagged_tests`), because the remediation leg
  runs on a later tick from persisted state;
- :func:`declaration_request` decides, from the round's structured
  ``test_changes`` answers and ``kind: "declaration"`` findings alone, whether
  those answers are all it rejects, and names the tests those findings ask to
  declare — a finding's location or prose never classifies it;
- :func:`requested_lines` rebuilds the lines a remediation's report declares
  — one requested id and the agent's escaped reason each, never its ids —
  and :func:`with_declared_lines` carries them into the PR body the merge's
  held-out gate reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kew.merge_verify import (
    behavior_change_lines,
    declaration_excuses,
    declared_on_line,
    is_behavior_heading,
    named_node_ids,
    without_node_ids,
)
from kew.pr_text import (
    escape_text,
    proposed_block_closed,
    proposed_section_lines,
    with_proposed_lines,
    without_proposed_tests,
)
from kew.review_contract import (
    DECLARATION_FINDING_KIND,
    TEST_CHANGE_REJECTED,
    DeclarationRuling,
    Finding,
    parse_verdict_sidecar,
    stored_declared_tests,
)
from kew.review_integrity import (
    declaration_parts,
    declared_test_paths,
    declared_tests,
    read_pr_body,
    without_test_paths,
)
from kew.subprocess_utils import run
from kew.test_integrity import (
    FLAG_TESTS_REMOVED,
    FLAG_UNDECLARED_TEST_EDITS,
    TestIntegrityReport,
)

_logger = logging.getLogger(__name__)

#: The test-change answers a **Behavior changes** line settles.
DECLARATION_FLAGS: tuple[str, ...] = (FLAG_UNDECLARED_TEST_EDITS, FLAG_TESTS_REMOVED)
_RECORD_SUFFIX = ".declarations"
_BEHAVIOR_HEADING = "**Behavior changes**"
#: A node id the brief may print outside the untrusted fence: it is read off
#: diff paths, so anything else is not asked for by name. ``(``, ``)`` and
#: ``+`` are SvelteKit route segments (``routes/(factory)/+page.test.ts``);
#: none of them opens a code span, a mention, an HTML comment or a shell
#: expansion, and a markdown link (``](``) is still refused.
_SAFE_NODE_ID = re.compile(r"^(?!.*\]\()[\w./\[\]:()+-]+$")
_MAX_REASON_CHARS = 300
#: What is left before the reason of a line with no separator once its node id
#: is taken out: the list marker, an emptied code span and a stray dash (#3187).
#: The reason itself is carried whole, escaped (:func:`kew.pr_text.escape_text`).
_LEADING_SEPARATOR = re.compile(r"^\s*(?:[-*+]\s+)?(?:``\s*)?(?:[—–:]|-{1,2})?\s*")


@dataclass(frozen=True)
class DeclarationRequest:
    """What a declaration-only round asks the remediation to write."""

    node_ids: tuple[str, ...]
    """The flagged tests to declare, by held-out node id, in report order."""
    reasons: tuple[str, ...]
    """The reviewer's own words: its REJECTED answers' reasons, then its
    findings' summaries. Reviewer output over an adversary-authored diff, so
    the brief renders them fenced, as data."""
    held_out: bool = False
    """Asked for by the pre-review held-out check (#3212), not by a reviewer:
    the base tests fail against the head, and the remediation either fixes the
    source or declares them. Never a "no code change" round."""
    section: tuple[str, ...] = ()
    """The PR body's **Behavior changes** lines when the check ran, for the
    brief to show what is declared already. PR text, so rendered fenced."""


#: Beside a pre-review round's sidecar: the held-out ids it asks about (#3212).
_HELD_OUT_SUFFIX = ".held-out"


def record_held_out_request(
    sidecar_path: str | Path,
    node_ids: Sequence[str],
    body: str | None,
) -> None:
    """Remember the held-out failures a pre-review round sends back, beside
    its sidecar, for the remediation a later tick may run."""
    record = {
        "node_ids": list(node_ids),
        "section": behavior_change_lines(body),
    }
    Path(f"{sidecar_path}{_HELD_OUT_SUFFIX}").write_text(
        json.dumps(record), encoding="utf-8"
    )


def load_held_out_request(sidecar_path: str | None) -> DeclarationRequest | None:
    """The held-out request a pre-review round recorded, or None for a round
    that recorded none or a record that does not read.

    No record is the ordinary case: a reviewer's round records none, so that
    says nothing. A record that is there but does not read is logged with its
    path and the error, and read as no request, as the writer's own failure is
    (the pre-review step in :mod:`kew.graph.driver.review_stage`): the round's
    findings still name every failing test, so the remediation is an ordinary
    round, and the merge verify still requires the declaration.
    """
    if not sidecar_path:
        return None
    record = Path(f"{sidecar_path}{_HELD_OUT_SUFFIX}")
    if not record.exists():
        return None
    try:
        data = json.loads(record.read_bytes())
        ids = tuple(str(node) for node in data["node_ids"])
        section = tuple(str(line) for line in data["section"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _logger.warning("review: held-out record %s unreadable: %r", record, exc)
        return None
    # An id the brief cannot print safely is left to the round's findings,
    # which carry it as data; the rest are asked for by name.
    safe = tuple(node for node in ids if _SAFE_NODE_ID.match(node))
    if not safe:
        return None
    return DeclarationRequest(node_ids=safe, reasons=(), held_out=True, section=section)


def _record_path(sidecar_path: str | Path) -> Path:
    return Path(f"{sidecar_path}{_RECORD_SUFFIX}")


def flagged_tests(report: TestIntegrityReport) -> dict[str, list[str]]:
    """The node ids *report* flags under each declaration flag."""
    undeclared = [e.text.split(" ", 1)[0] for e in report.undeclared_test_edits]
    return {
        FLAG_UNDECLARED_TEST_EDITS: list(dict.fromkeys(undeclared)),
        # Read off the base AST with class ancestry, so a removed test inside a
        # class is named the way the held-out gate reports it.
        FLAG_TESTS_REMOVED: list(report.removed_test_ids),
    }


def record_flagged_tests(sidecar_path: Path, report: TestIntegrityReport) -> str | None:
    """Store the round's flagged node ids beside its stored sidecar.

    Written whole or not at all (a temporary file renamed into place), and
    only when the report flags a test: a round with none is never read as
    declaration-only, record or not. Returns None once recorded, else the
    failure — the caller must then not record the round, because a round
    standing without its record is remediated at the same head with the
    ordinary brief, which is the loop #2872 exists to end.
    """
    flagged = flagged_tests(report)
    if not any(flagged.values()):
        return None
    path = _record_path(sidecar_path)
    staged = path.with_name(f"{path.name}.tmp")
    try:
        staged.write_text(json.dumps(flagged))
        os.replace(staged, path)
    except OSError as exc:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        _logger.error("review: flagged tests not recorded for %s: %s", path, exc)
        return f"{type(exc).__name__}: {exc.strerror or exc}"
    return None


def _read_record(sidecar_path: str) -> dict[str, list[str]] | None:
    path = _record_path(sidecar_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _logger.warning("review: flagged-test record %s unreadable: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return {
        flag: [i for i in ids if isinstance(i, str)]
        for flag, ids in data.items()
        if isinstance(flag, str) and isinstance(ids, list)
    }


def _rejected_answers(data: Mapping[str, Any]) -> dict[str, str]:
    """Each REJECTED ``test_changes`` flag with the reviewer's reason."""
    raw = data.get("test_changes")
    rejected: dict[str, str] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or entry.get("answer") != TEST_CHANGE_REJECTED:
            continue
        flag, reason = entry.get("flag"), entry.get("reason")
        if isinstance(flag, str):
            rejected.setdefault(flag.strip(), reason if isinstance(reason, str) else "")
    return rejected


def _rejected_node_ids(
    rejected: Mapping[str, str], flagged: Mapping[str, Sequence[str]]
) -> list[list[str]] | None:
    """The recorded node ids under each flag the reviewer rejected, or None
    when the host recorded none for one of them — that flag's tests cannot be
    named, so the round is not one a declaration settles."""
    per_flag = [
        list(flagged.get(flag, ())) for flag in DECLARATION_FLAGS if flag in rejected
    ]
    return per_flag if all(per_flag) else None


def _declared_by(finding: Finding, candidates: Sequence[str]) -> set[str] | None:
    """The node ids *finding* asks to declare, or None when it is not a
    declaration finding the host can honour.

    Only the reviewer's structured answer counts: ``kind: "declaration"`` with
    its ``node_ids`` (#2872). A finding without that kind, one that also fails
    an acceptance criterion, or one naming a test the host never flagged is a
    defect to remediate — its location and prose never classify it. The ids
    are kept exactly as named: a finding about one parametrized case asks for
    that case, never for the whole function.
    """
    if finding.kind != DECLARATION_FINDING_KIND or finding.criterion:
        return None
    asked = set(finding.node_ids)
    if not asked or not all(_flagged(n, candidates) for n in asked):
        return None
    return asked


def _flagged(node_id: str, candidates: Sequence[str]) -> bool:
    """*node_id* is a flagged test, or one parametrized case of one."""
    return node_id in candidates or node_id.split("[", 1)[0] in candidates


def _evidenced_tests(
    findings: Sequence[Finding], per_flag: Sequence[Sequence[str]]
) -> tuple[str, ...] | None:
    """The node ids the findings ask to declare, in record order — None unless
    every finding is a declaration finding and every rejected flag keeps one
    of its tests."""
    candidates = list(dict.fromkeys(i for ids in per_flag for i in ids))
    asked: list[str] = []
    for finding in findings:
        hits = _declared_by(finding, candidates)
        if hits is None:
            return None
        asked += sorted(hits - set(asked))
    rank = {test: n for n, test in enumerate(candidates)}
    node_ids = tuple(
        sorted(asked, key=lambda i: rank.get(i, rank.get(i.split("[", 1)[0], 0)))
    )
    return node_ids if _covers(node_ids, per_flag) else None


def _covers(node_ids: tuple[str, ...], per_flag: Sequence[Sequence[str]]) -> bool:
    """*node_ids* are printable and keep at least one test of every flag."""
    every_flag = all(any(_flagged(i, ids) for i in node_ids) for ids in per_flag)
    return bool(node_ids) and every_flag and all(map(_SAFE_NODE_ID.match, node_ids))


def _reasons(
    rejected: Mapping[str, str], findings: Sequence[Finding]
) -> tuple[str, ...]:
    reasons = [rejected[flag] for flag in DECLARATION_FLAGS if rejected.get(flag)]
    reasons += [f.summary for f in findings]
    return tuple(r.strip()[:_MAX_REASON_CHARS] for r in reasons if r.strip())


def declaration_request(
    sidecar: Mapping[str, Any],
    findings: Sequence[Finding],
    flagged: Mapping[str, Sequence[str]],
) -> DeclarationRequest | None:
    """The declaration a round asks for, when that is ALL it asks for.

    The round qualifies only when every ``test_changes`` answer it REJECTED is
    a declaration flag, at least one is, the host recorded node ids under each
    rejected flag, no acceptance criterion failed, and every finding is a
    ``kind: "declaration"`` finding whose ``node_ids`` the host flagged
    (:func:`_declared_by`). Only those ids are requested: a flag rejected over
    several edits does not waive the ones no finding asked for. Nothing is
    inferred from where a finding sits or what it says, so a finding without
    the kind — however it reads — or any doubt means the round is remediated
    as before.
    """
    rejected = _rejected_answers(sidecar)
    if not rejected or not set(rejected) <= set(DECLARATION_FLAGS):
        return None
    per_flag = _rejected_node_ids(rejected, flagged)
    node_ids = None if per_flag is None else _evidenced_tests(findings, per_flag)
    if node_ids is None:
        return None
    return DeclarationRequest(node_ids=node_ids, reasons=_reasons(rejected, findings))


def load_declaration_request(
    sidecar_path: str | None, criterion_ids: tuple[str, ...] = ()
) -> DeclarationRequest | None:
    """:func:`declaration_request` for a round's stored sidecar, or None when
    the sidecar or its flagged-test record is missing or unreadable.

    Parsed the way the round itself was: against *criterion_ids*, the ids its
    plan recorded, and the declared set recorded beside the sidecar (#3516),
    so the request names only the tests the round still asked for."""
    loaded = _load_request(sidecar_path, criterion_ids)
    return None if loaded is None else loaded[0]


def requested_by_round(
    sidecar_path: str | None, criterion_ids: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Every test a declaration-only round asked to declare: those of its
    request, then those kew excused off its findings as already declared
    (#3516). Empty when the round is not declaration-only.

    The request alone names only what the body left out at the time, so a
    later round asking again for a test the body declared then would not read
    as asked before — and would earn the same lines a second re-review."""
    loaded = _load_request(sidecar_path, criterion_ids)
    if loaded is None:
        return ()
    request, excused = loaded
    return tuple(dict.fromkeys((*request.node_ids, *excused)))


def _load_request(
    sidecar_path: str | None, criterion_ids: tuple[str, ...]
) -> tuple[DeclarationRequest, tuple[str, ...]] | None:
    """A round's declaration request and the tests excused off its findings."""
    if not sidecar_path:
        return None
    flagged = _read_record(sidecar_path)
    if flagged is None:
        return None
    try:
        raw = Path(sidecar_path).read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        _logger.warning(
            "review: sidecar %s unreadable; round not read as declaration-only: %s",
            sidecar_path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    parsed = parse_verdict_sidecar(
        raw,
        plan_criterion_ids=criterion_ids,
        declared_tests=stored_declared_tests(sidecar_path),
    )
    if parsed.verdict != "NEEDS_CHANGES" or parsed.failing_criteria:
        return None
    request = declaration_request(data, parsed.findings, flagged)
    return None if request is None else (request, parsed.excused_tests)


def undeclared(request: DeclarationRequest, text: str | None) -> tuple[str, ...]:
    """The requested node ids *text*'s **Behavior changes** section leaves out.

    By the merge's own rule (:func:`declaration_excuses`): a line for one
    parametrized case does not declare a request for the whole function.
    """
    declared = declared_tests(text)
    return tuple(i for i in request.node_ids if not declaration_excuses(i, declared))


def proposal_request(
    findings: Sequence[Finding], body: str | None
) -> DeclarationRequest | None:
    """The lines a round asks its remediation to propose, when that is ALL it
    asks for (#3493).

    Every finding must be a ``kind: "declaration"`` finding naming printable
    node ids and failing no acceptance criterion, and *body* must be readable.
    Unlike :func:`declaration_request`, the ids need no host-flagged record
    (a frontend test file has none): the lines the remediation writes land
    only under **Proposed behavior changes**, where the reviewer rules on each,
    so naming a test here waives nothing. The ids *body* already declares are
    dropped, and a round they all are is None: a re-review rejected declared
    tests, a disagreement no line settles, so it gets the ordinary brief.

    The ids *body* already proposes are dropped too, so the brief never sends
    the remediation after a test someone has a line for, and its own line
    never replaces theirs. Each such line came after the round: the round
    ruled on none of them, because a line it rejects is an ordinary finding
    and a line it accepts is declared. A round whose every undeclared id is
    proposed is None; :func:`proposals_answer` says a re-review is its due.
    """
    gap = _proposal_gap(findings, body)
    if gap is None or not gap[1]:
        return None
    return DeclarationRequest(gap[1], gap[0].reasons)


def proposals_answer(findings: Sequence[Finding], body: str | None) -> bool:
    """Whether *body*'s **Proposed behavior changes** block already holds a
    line for every test a proposal-only round asks about and *body* does not
    declare (#3493), whoever wrote them and whenever: only a reviewer can rule
    on them, so the round is owed a re-review, not a remediation."""
    gap = _proposal_gap(findings, body)
    return gap is not None and not gap[1]


def asks_only_lines(findings: Sequence[Finding]) -> bool:
    """Whether *findings* are a proposal-only round's (#3493): each asks only
    for Behavior changes lines, so a PR body's proposed block can answer it."""
    return bool(findings) and all(map(_asks_a_line, findings))


def _proposal_gap(
    findings: Sequence[Finding], body: str | None
) -> tuple[DeclarationRequest, tuple[str, ...]] | None:
    """A proposal-only round's request for the ids *body* does not declare,
    and those of them no proposed line answers; None when the round is not
    proposal-only, *body* is unread, or *body* declares every id."""
    if body is None or not asks_only_lines(findings):
        return None
    asked = tuple(dict.fromkeys(i for f in findings for i in f.node_ids))
    reasons = tuple(f.summary.strip()[:_MAX_REASON_CHARS] for f in findings)
    missing = undeclared(DeclarationRequest(asked, ()), body)
    if not missing:
        return None
    request = DeclarationRequest(missing, tuple(r for r in reasons if r))
    return request, _unproposed(missing, body)


def _unproposed(ids: Sequence[str], body: str) -> tuple[str, ...]:
    """The *ids* no line in *body*'s proposed block answers."""
    proposals = "\n".join([_BEHAVIOR_HEADING, *proposed_section_lines(body)])
    return tuple(
        i for i in ids if not requested_lines(DeclarationRequest((i,), ()), proposals)
    )


def _asks_a_line(finding: Finding) -> bool:
    """*finding* asks only for lines, for printable node ids (#3493)."""
    if finding.kind != DECLARATION_FINDING_KIND or finding.criterion:
        return False
    return bool(finding.node_ids) and all(map(_SAFE_NODE_ID.match, finding.node_ids))


def requested_lines(request: DeclarationRequest, report: str | None) -> list[str]:
    """The PR-body lines *report*'s **Behavior changes** section earns.

    Agent text is never copied through. A report line counts only when it
    declares exactly one node id, and that id declares a requested one by the
    merge's own rule (:func:`declaration_excuses`) — the requested id itself,
    or the function a requested parametrized case belongs to; a line for one
    case never answers a request for the whole function. A line naming any
    other id is refused whole, so it cannot waive a test nobody asked about.
    Each line is rebuilt as ``- <requested id> — <reason>`` from the canonical
    requested id, with the agent's reason carried whole and escaped.
    """
    out: list[str] = []
    for line in behavior_change_lines(report):
        for rebuilt in _rebuilt_lines(request, line):
            if rebuilt not in out:
                out.append(rebuilt)
    return out


def accepted_lines(rulings: Sequence[DeclarationRuling], body: str | None) -> list[str]:
    """The **Behavior changes** lines the reviewer's accepted rulings earn (#3475).

    Each is rebuilt from *body*'s own **Proposed behavior changes** line for
    that test — the line the reviewer read and ruled on — by
    :func:`requested_lines`' rules: one test per line, the canonical id, the
    proposal's reason escaped. A rejected ruling earns nothing, and neither
    does an accepted one with no proposed line behind it: the reviewer accepts
    the implementer's line, it never writes one.
    """
    ids = tuple(dict.fromkeys(r.node_id for r in rulings if r.accepted))
    if not ids:
        return []
    proposals = "\n".join([_BEHAVIOR_HEADING, *proposed_section_lines(body)])
    return requested_lines(DeclarationRequest(node_ids=ids, reasons=()), proposals)


def _declared_on(line: str) -> frozenset[str]:
    """What one line declares: node ids, and frontend test paths (#3187)."""
    return declared_on_line(line) | declared_test_paths(line)


def _declared_and_reason(line: str) -> tuple[frozenset[str], str] | None:
    """What a report line declares, and its reason; None when it is refused.

    On a line with a separator (``- <id> — <reason>``) only the part before it
    declares, so a path the reason mentions is prose (#3187). A reason naming
    another node id is refused whole: the merge reads every node id on a line,
    so the carried line would waive that test too. A line with no separator is
    read whole, the way the merge reads it.
    """
    parts = declaration_parts(line)
    if parts is None:
        rest = without_test_paths(without_node_ids(line))
        return _declared_on(line), _LEADING_SEPARATOR.sub("", rest)
    head, reason = parts
    if named_node_ids(reason):
        return None
    return frozenset(named_node_ids(head)) | declared_test_paths(line), reason


def _rebuilt_lines(request: DeclarationRequest, line: str) -> list[str]:
    read = _declared_and_reason(line)
    if read is None or len(read[0]) != 1:
        return []
    ids, text = read
    answered = [i for i in request.node_ids if declaration_excuses(i, ids)]
    reason = escape_text(text.strip())
    if not re.search(r"[A-Za-z]{2}", reason):
        return []
    return [f"- {node_id} — {reason}" for node_id in answered]


def with_declared_lines(body: str, lines: Sequence[str]) -> str:
    """*body* with each of *lines* added to its **Behavior changes** section.

    A line whose node ids the body already declares — by the merge's own rule —
    is skipped. With no such section, one is appended at the end of the body.
    """
    have = declared_tests(body)
    new = [
        ln
        for ln in lines
        if not all(declaration_excuses(i, have) for i in _declared_on(ln))
    ]
    if not new:
        return body
    out = body.splitlines()
    heading = next((n for n, ln in enumerate(out) if is_behavior_heading(ln)), None)
    if heading is None:
        section = "\n".join([_BEHAVIOR_HEADING, "", *new])
        return f"{body.rstrip()}\n\n{section}\n" if body.strip() else f"{section}\n"
    _insert_under(out, heading, new)
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


def _insert_under(out: list[str], heading: int, new: Sequence[str]) -> None:
    at = heading + 1
    if at < len(out) and not out[at].strip():
        at += 1  # keep the blank line under the heading
    out[at:at] = new


def carry_declared_lines(repo: str, number: int, lines: Sequence[str]) -> bool:
    """Add *lines* to PR *number*'s **Behavior changes** section on GitHub.

    GitHub's pull-request PATCH has no compare-and-swap, so the window for
    overwriting a concurrent edit is narrowed as far as REST allows: the body
    is re-read immediately before the write, only the missing lines go into
    that latest body, and a re-read after the write must show both the lines
    and the rest of the body. A mismatch is retried once. True when the body
    is confirmed to carry them; False otherwise, logged and never raised — the
    driver then reads the lines as missing and holds the node for a person.
    """
    for _ in range(2):
        confirmed = _carry_once(repo, number, lines)
        if confirmed is not None:
            return confirmed
    _logger.warning(
        "review: PR #%s body changed under the Behavior changes write twice; "
        "the %d lines are not confirmed there",
        number,
        len(lines),
    )
    return False


def _carry_once(repo: str, number: int, lines: Sequence[str]) -> bool | None:
    """One read-insert-write-confirm pass: None when the re-read after the
    write does not confirm it, which is worth one more pass."""
    body = read_pr_body(repo, number)
    if body is None:
        return False
    updated = with_declared_lines(body, lines)
    if updated == body:
        return True
    if not _patch_body(repo, number, updated):
        _logger.warning(
            "review: could not carry %d Behavior changes lines to PR #%s",
            len(lines),
            number,
        )
        return False
    after = read_pr_body(repo, number)
    if after is None or not _carries(after, body, lines):
        return None
    _logger.info(
        "review: carried %d Behavior changes lines to PR #%s", len(lines), number
    )
    return True


def propose_lines(repo: str, number: int, lines: Sequence[str]) -> bool:
    """Put a run's own proposed lines into PR *number*'s **Proposed behavior
    changes** block (#3475), where the next reviewer rules on them.

    They declare nothing there. The body is re-read before the write and
    after it, as :func:`carry_declared_lines` does; True once the block is
    confirmed to hold every line, False otherwise — logged, never raised.
    """
    for _ in range(2):
        body = read_pr_body(repo, number)
        if body is None:
            return False
        updated = with_proposed_lines(body, list(lines))
        if updated == body:
            return True
        if not _patch_body(repo, number, updated):
            break
        after = read_pr_body(repo, number)
        if after is not None and set(lines) <= set(proposed_section_lines(after)):
            return True
    _logger.warning(
        "review: %d proposed Behavior changes lines are not confirmed in PR #%s",
        len(lines),
        number,
    )
    return False


def unneeded_proposals(rulings: Sequence[DeclarationRuling]) -> tuple[str, ...]:
    """The tests whose proposals the reviewer rejected as needing no
    declaration (#3500): the lines kew drops from the proposed block."""
    return tuple(dict.fromkeys(r.node_id for r in rulings if r.not_needed))


def drop_proposed_lines(repo: str, number: int, node_ids: Sequence[str]) -> bool:
    """Remove the proposed lines naming *node_ids* from PR *number*'s
    **Proposed behavior changes** block (#3500).

    They declared nothing, so leaving one costs only the next reviewer's
    ruling on it again. The body is re-read before the write and after it, as
    :func:`propose_lines` does; True once no proposed line names one of them,
    False otherwise — logged, never raised. A body whose proposed fence never
    closes, or whose rewrite would change what it declares, is left as it is:
    the drop removes proposals, never a declaration nor a section's end.
    """
    for _ in range(2):
        body = read_pr_body(repo, number)
        if body is None:
            return False
        updated = without_proposed_tests(body, node_ids)
        if updated == body:
            return True
        if not _drop_is_safe(body, updated):
            _logger.warning(
                "review: PR #%s proposed block is unclosed or the drop would "
                "change its Behavior changes; left as it is",
                number,
            )
            return False
        if not _patch_body(repo, number, updated):
            break
        after = read_pr_body(repo, number)
        if after is not None and without_proposed_tests(after, node_ids) == after:
            return True
    _logger.warning(
        "review: %d unneeded proposed Behavior changes lines are not confirmed "
        "dropped from PR #%s",
        len(node_ids),
        number,
    )
    return False


def _drop_is_safe(body: str, updated: str) -> bool:
    """Whether *updated*, *body* less some proposed lines, declares exactly
    what *body* does, from a proposed block that ends at its closing fence."""
    if not proposed_block_closed(body):
        return False
    return declared_tests(updated) == declared_tests(body)


def _patch_body(repo: str, number: int, body: str) -> bool:
    result = run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/pulls/{number}",
            "-f",
            f"body={body}",
        ],
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def _carries(after: str, before: str, lines: Sequence[str]) -> bool:
    """Whether *after* declares every id *lines* name and keeps every line of
    *before*, the body the write was built on."""
    wanted: frozenset[str] = frozenset().union(*(_declared_on(ln) for ln in lines))
    kept = set(after.splitlines())
    return wanted <= declared_tests(after) and all(
        ln in kept for ln in before.splitlines()
    )
