"""Run the deterministic conformance pack and record what it proves — no more.

Three modes, and the boundary between them is the whole point:

``candidate``
    Run the deterministic PCJ / C-gate pack for an explicit subject SHA and
    emit a **sanitized candidate**: what was proven, over what window, with
    which exclusions. It is labelled ``contract`` evidence, and its claim is
    ``certification_ready`` — deterministic build acceptance. It is not, and
    cannot become, a journey certificate.

``append-approved``
    Record an approved candidate on the append-only certificate carrier
    (:mod:`kew.certificate_ref`), compare-and-swap and fast-forward-only, bound
    into the audit chain. It requires a resolved operator and a reviewed
    reason, refuses a pack that did not pass green over the whole pack, and
    only ever writes a ``certification_ready`` envelope. Appending a
    ``journey_certificate`` is deliberately not implementable from here: that
    claim needs the live reference suite this tool cannot run.

``verify``
    Read back what was recorded, with no writes. It re-reads the manifest,
    proves the exact envelope is present at the digest the candidate names,
    and prints the resolver's reading — which for a ready envelope must be
    ``uncertified``. A ``certified`` reading out of this tool is a failure,
    not a success.

Sanitization is enforced, not documented: the emitted document is scanned and
the tool refuses to write anything carrying an absolute path, a credential
shape, or raw prompt/provider content. The pack's own output is never
embedded — only its exit status, counts and scope — so there is nothing for a
transcript to leak through.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kew.audit_chain import verify_chain
from kew.certificate_ref import (
    CERTIFICATE_REF,
    KIND_READY,
    AppendResult,
    CertificateEnvelope,
    CertificateRefError,
    ManifestEntry,
    append_certificate,
    read_manifest,
    resolve_certificate,
)
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import preflight_tools

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The pack this tool is the runner for. A candidate that ran anything else is
#: not this pack and may not be appended.
PACK_SCOPE = "tests/conformance"

DOCUMENT_VERSION = 1
DOCUMENT_KIND = "certification_candidate"

#: Deterministic contract evidence. The word matters: a live label may only be
#: written by the post-merge reference suite, which this tool cannot run.
EVIDENCE_LABEL = "contract"

#: Every gate the pack claims. Kept here rather than imported from the tests so
#: the script does not depend on the test tree; parity with the pack's own
#: catalogue is asserted by ``tests/test_certification_conformance.py``.
GATES: tuple[str, ...] = (
    "PCJ-0",
    "PCJ-1",
    "PCJ-3",
    "PCJ-4",
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
    "C8",
)

#: What a deterministic pre-merge run cannot prove, stated up front so a reader
#: never has to infer the boundary from what is missing.
EXCLUSIONS: tuple[str, ...] = (
    "PCJ-2 live supervised epic: needs a real forge, reviewer and dependency"
    " edge; only the in-release A-to-B fixture is covered here",
    "live route readiness: no paid provider call, no source or forge"
    " reachability check, and no spend is performed by this pack",
    "external anchor: no audit anchor is claimed, so nothing here can resolve"
    " as certified",
)

_ABSOLUTE_PATH = re.compile(r"(^|\s)(/|~/|[A-Za-z]:\\)[^\s]")
_CREDENTIAL = re.compile(
    r"(api[_-]?key|secret|bearer\s|authorization:|sk-[A-Za-z0-9]{8})",
    re.IGNORECASE,
)
_RAW_CONTENT = re.compile(r"(prompt_text|system_prompt|provider_response|stdout)")


class Refused(RuntimeError):
    """The tool declined to produce or record evidence. Nothing was written."""


# --------------------------------------------------------------------------- #
# Sanitization
# --------------------------------------------------------------------------- #


def _offending(value: Any, path: str) -> list[str]:
    if isinstance(value, dict):
        return [
            problem
            for key, item in value.items()
            for problem in _offending(key, path) + _offending(item, f"{path}.{key}")
        ]
    if isinstance(value, list | tuple):
        return [
            problem
            for index, item in enumerate(value)
            for problem in _offending(item, f"{path}[{index}]")
        ]
    if not isinstance(value, str):
        return []
    for pattern, why in (
        (_ABSOLUTE_PATH, "an absolute filesystem path"),
        (_CREDENTIAL, "a credential shape"),
        (_RAW_CONTENT, "raw prompt or provider content"),
    ):
        if pattern.search(value):
            return [f"{path} carries {why}"]
    return []


def sanitized(document: dict[str, Any]) -> dict[str, Any]:
    """*document* if it is safe to publish; otherwise refuse to emit anything."""
    problems = _offending(document, "candidate")
    if problems:
        raise Refused("the candidate is not sanitized: " + "; ".join(sorted(problems)))
    return document


# --------------------------------------------------------------------------- #
# candidate
# --------------------------------------------------------------------------- #


def _run_pack(scope: str) -> dict[str, Any]:
    """Run the pack as a subprocess. Only its verdict crosses back."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-m", "pytest", "-q", "--no-header", scope],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = completed.stdout.strip().splitlines()[-1:] or [""]
    counts = re.findall(r"(\d+) (passed|failed|error|errors)", tail[0])
    return {
        "scope": scope,
        "result": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "counts": {label: int(number) for number, label in counts},
        "command": "python -m pytest -q --no-header " + scope,
    }


def _fingerprint(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "cc1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _audit_state(store_path: str | None) -> dict[str, Any]:
    if store_path is None:
        return {"head": None, "verified": False, "detail": "no ledger was read"}
    store = SqliteRunStore(store_path, read_only=True)
    try:
        result = verify_chain(store.read_chain_rows())
    finally:
        store.close()
    return {
        "head": result.head_hash,
        "verified": result.ok,
        "count": result.count,
        "detail": "the ledger's own tamper-evident chain verification",
    }


def build_candidate(
    *,
    subject: str,
    lane: str,
    started_at: datetime,
    finished_at: datetime,
    pack: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the sanitized candidate. Pure: no clock, no filesystem."""
    body: dict[str, Any] = {
        "version": DOCUMENT_VERSION,
        "kind": DOCUMENT_KIND,
        "evidence_label": EVIDENCE_LABEL,
        "claim": KIND_READY,
        "subject": subject,
        "lane": lane,
        "gates": list(GATES),
        "pack": pack,
        "window": {
            "started_at": started_at.astimezone(UTC).isoformat(),
            "finished_at": finished_at.astimezone(UTC).isoformat(),
        },
        # A deterministic pack calls no provider, so the honest accounting is
        # "no spend", stated as a source and a certainty rather than as $0.
        "cost": {
            "source": "not_applicable",
            "certainty": "exact",
            "usd": 0.0,
            "detail": "the pack performs no provider call and no spend",
        },
        "audit": audit,
        "exclusions": list(EXCLUSIONS),
        "findings": [] if pack["result"] == "passed" else ["the pack did not pass"],
    }
    # The fingerprint covers WHAT was proven, never WHEN: a rerun of the same
    # pack over the same subject and lane is the same evidence.
    body["fingerprint"] = _fingerprint(
        {k: body[k] for k in ("subject", "lane", "gates", "claim", "evidence_label")}
        | {"scope": pack["scope"], "result": pack["result"]}
    )
    return sanitized(body)


def _candidate(args: argparse.Namespace) -> int:
    started = _moment(args.now)
    pack = (
        {
            "scope": args.scope,
            "result": "not_run",
            "exit_code": None,
            "counts": {},
            "command": "not run",
        }
        if args.skip_pack
        else _run_pack(args.scope)
    )
    document = build_candidate(
        subject=args.subject,
        lane=args.lane,
        started_at=started,
        finished_at=_moment(args.now),
        pack=pack,
        audit=_audit_state(args.store),
    )
    _emit(document, args.out)
    return 0 if pack["result"] == "passed" else 1


# --------------------------------------------------------------------------- #
# append-approved
# --------------------------------------------------------------------------- #


def _operator(explicit: str | None) -> str:
    """The authorized operator recording this append, or a refusal."""
    actor = explicit or os.environ.get("KEW_CERTIFICATION_ACTOR") or ""
    actor = actor.strip()
    if not actor:
        raise Refused(
            "no authorized operator resolved: pass --actor or set"
            " KEW_CERTIFICATION_ACTOR; an unattributed append is not evidence"
        )
    return actor


def _approved_candidate(path: Path) -> dict[str, Any]:
    return _approve(json.loads(path.read_text(encoding="utf-8")))


def _approve(document: dict[str, Any]) -> dict[str, Any]:
    """*document* if it may be recorded, or a refusal. Nothing is written."""
    if document.get("version") != DOCUMENT_VERSION:
        raise Refused(f"unsupported candidate version {document.get('version')!r}")
    if document.get("kind") != DOCUMENT_KIND:
        raise Refused("this document is not a certification candidate")
    if document.get("claim") != KIND_READY:
        raise Refused(
            f"this tool only records {KIND_READY!r}; a journey certificate needs"
            " the live reference suite"
        )
    pack = document.get("pack") or {}
    if pack.get("result") != "passed":
        raise Refused("the pack did not pass; there is nothing to approve")
    if pack.get("scope") != PACK_SCOPE:
        raise Refused(
            f"the candidate ran {pack.get('scope')!r}, not the whole pack"
            f" ({PACK_SCOPE!r})"
        )
    sanitized(document)
    return document


def envelope_for(
    document: dict[str, Any], *, actor: str, reason: str, issued_at: datetime
) -> CertificateEnvelope:
    """The one envelope kind this tool may write."""
    return CertificateEnvelope(
        kind=KIND_READY,
        subject=str(document["subject"]),
        status="build_accepted",
        lane=str(document["lane"]),
        fingerprint=str(document["fingerprint"]),
        issued_at=issued_at.astimezone(UTC).isoformat(),
        actor=actor,
        body={
            "evidence_label": document["evidence_label"],
            "gates": document["gates"],
            "pack": document["pack"],
            "window": document["window"],
            "cost": document["cost"],
            "audit": document["audit"],
            "exclusions": document["exclusions"],
            "reason": reason,
        },
    )


def _reviewed_reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if len(reason) < 12:
        raise Refused("a reviewed reason is required and must say something")
    return reason


def _record(
    document: dict[str, Any],
    *,
    args: argparse.Namespace,
    actor: str,
    reason: str,
) -> AppendResult:
    """Write the one envelope this tool may write, bound into the ledger."""
    if args.store is None:
        raise Refused("an append with no audit binding is not evidence: pass --store")
    envelope = envelope_for(
        document, actor=actor, reason=reason, issued_at=_moment(args.now)
    )
    store = SqliteRunStore(args.store)
    try:
        return append_certificate(
            Path(args.repo),
            envelope,
            ledger=store,
            now=_moment(args.now),
            ref=args.ref,
        )
    finally:
        store.close()


def _append_approved(args: argparse.Namespace) -> int:
    document = _approved_candidate(Path(args.candidate))
    actor = _operator(args.actor)
    result = _record(
        document, args=args, actor=actor, reason=_reviewed_reason(args.reason)
    )
    _emit(
        {
            "recorded": "certification_ready",
            "subject": result.subject,
            "digest": result.digest,
            "ref": result.ref,
            "ref_before": result.ref_before,
            "ref_after": result.ref_after,
            "predecessor": result.predecessor,
            "actor": actor,
        },
        args.out,
    )
    return 0


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _verify(args: argparse.Namespace) -> int:
    _emit(_readback(args, subject=args.subject, digest=args.digest), args.out)
    return 0


def _readback(
    args: argparse.Namespace, *, subject: str, digest: str | None
) -> dict[str, Any]:
    """Re-read what the carrier actually holds. No writes, no assumptions."""
    repo = Path(args.repo)
    manifest = read_manifest(repo, subject, ref=args.ref)
    if manifest is None or not manifest.entries:
        raise Refused(f"no certificate evidence names {subject}")
    digests = [entry.digest for entry in manifest.entries]
    if digest and digest not in digests:
        raise Refused(f"{digest} is not recorded for {subject}")
    now = _moment(args.now)
    store = SqliteRunStore(args.store, read_only=True) if args.store else None
    try:
        rows = store.list_audit_events(limit=None) if store is not None else []
        reading = resolve_certificate(
            repo,
            subject,
            ref=args.ref,
            audit_rows=rows,
            fetched_at=now,
            now=now,
        )
    finally:
        if store is not None:
            store.close()
    tail = manifest.entries[-1]
    if reading.state == "certified":
        raise Refused(
            "this tool records build acceptance only; a certified reading here"
            " means the carrier holds a journey certificate it did not write"
        )
    return {
        "subject": subject,
        "ref": args.ref,
        "entries": len(manifest.entries),
        "tail": _summary(tail),
        "reading": {
            "state": reading.state,
            "availability": reading.availability,
            "detail": reading.detail,
        },
    }


# --------------------------------------------------------------------------- #
# gate (E15) and status
# --------------------------------------------------------------------------- #

#: The release gate this wiring is. E15 runs the pack against an exact merged
#: SHA and records what it proved; it is not an epic node and dispatches
#: nothing.
GATE = "E15"

#: The status a recorded envelope reaches. It is a RECORDED status, not a
#: passing test run: the pack passing is the evidence, the envelope attached to
#: the carrier is the claim.
READY_STATUS = "certification-ready"

#: What this gate may never reach, stated on every report rather than left to
#: be inferred from the absence of a stronger word.
GATED_ON = (
    "production-certified stays behind Phase 2: the promoted-broker GO and the"
    " live reference journey suite, neither of which this gate runs"
)


def _summary(entry: ManifestEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "status": entry.status,
        "digest": entry.digest,
        "lane": entry.envelope.lane,
        "fingerprint": entry.envelope.fingerprint,
        "actor": entry.envelope.actor,
        "predecessor": entry.predecessor,
    }


def recorded_ready(
    repo: Path, subject: str, *, ref: str, fingerprint: str | None = None
) -> ManifestEntry | None:
    """The ready envelope already attached for *subject*, if the carrier holds one.

    Convergence is keyed on the candidate's FINGERPRINT — what was proven —
    and never on the envelope digest, which also covers the instant, the
    operator and the reviewed reason. Keying on the digest would let an honest
    second run of the same pack over the same subject append a duplicate.

    A carrier that cannot be read raises rather than answering "nothing is
    recorded": an unreadable carrier is not an empty one.
    """
    manifest = read_manifest(repo, subject, ref=ref)
    matches = [
        entry
        for entry in (manifest.entries if manifest else ())
        if entry.kind == KIND_READY
        and (fingerprint is None or entry.envelope.fingerprint == fingerprint)
    ]
    return matches[-1] if matches else None


def gate_report(
    document: dict[str, Any],
    *,
    entry: ManifestEntry | None,
    recorded: bool,
    verification: dict[str, Any] | None,
    detail: str,
) -> dict[str, Any]:
    """What the gate decided, and the boundary it stopped at. Pure."""
    return {
        "gate": GATE,
        "subject": document["subject"],
        "lane": document["lane"],
        "pack": document["pack"],
        "fingerprint": document["fingerprint"],
        "recorded": recorded,
        "converged": entry is not None and not recorded,
        "envelope": None if entry is None else _summary(entry),
        # Reached only because the envelope was read back OFF the carrier —
        # never because the pack passed in this process.
        "status": READY_STATUS if entry is not None else None,
        "production_certified": False,
        "gated_on": GATED_ON,
        "verification": verification,
        "detail": detail,
    }


def _record_once(
    document: dict[str, Any], *, args: argparse.Namespace, actor: str, reason: str
) -> tuple[ManifestEntry, bool]:
    """Attach this evidence unless the carrier already holds it."""
    repo = Path(args.repo)
    subject, fingerprint = str(document["subject"]), str(document["fingerprint"])
    already = recorded_ready(repo, subject, ref=args.ref, fingerprint=fingerprint)
    if already is not None:
        return already, False
    _record(_approve(document), args=args, actor=actor, reason=reason)
    entry = recorded_ready(repo, subject, ref=args.ref, fingerprint=fingerprint)
    if entry is None:
        raise Refused("the append reported success but the carrier does not hold it")
    return entry, True


def _gate(args: argparse.Namespace) -> int:
    # Everything that can refuse for free refuses BEFORE the pack is spent.
    reason = _reviewed_reason(args.reason)
    actor = _operator(args.actor)
    if args.store is None:
        raise Refused("a gate with no audit binding is not evidence: pass --store")
    started = _moment(args.now)
    document = build_candidate(
        subject=args.subject,
        lane=args.lane,
        started_at=started,
        finished_at=_moment(args.now),
        pack=_run_pack(args.scope),
        audit=_audit_state(args.store),
    )
    if args.candidate_out:
        Path(args.candidate_out).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if document["pack"]["result"] != "passed":
        _emit(
            gate_report(
                document,
                entry=None,
                recorded=False,
                verification=None,
                detail="the pack did not pass, so nothing was recorded",
            ),
            args.out,
        )
        return 1
    entry, recorded = _record_once(document, args=args, actor=actor, reason=reason)
    verification = _readback(
        args, subject=str(document["subject"]), digest=entry.digest
    )
    _emit(
        gate_report(
            document,
            entry=entry,
            recorded=recorded,
            verification=verification,
            detail=(
                "the pack passed and its approved envelope was attached"
                if recorded
                else "this evidence was already attached; nothing was recorded"
            ),
        ),
        args.out,
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    """Is the release certification-ready? Answered off the carrier alone."""
    entry = recorded_ready(
        Path(args.repo), args.subject, ref=args.ref, fingerprint=args.fingerprint
    )
    _emit(
        {
            "gate": GATE,
            "subject": args.subject,
            "ref": args.ref,
            "status": READY_STATUS if entry is not None else None,
            "envelope": None if entry is None else _summary(entry),
            "production_certified": False,
            "gated_on": GATED_ON,
            "detail": (
                "an approved certification_ready envelope is attached to the"
                " evidence ref"
                if entry is not None
                else "no approved certification_ready envelope is attached to"
                " the evidence ref"
            ),
        },
        args.out,
    )
    return 0 if entry is not None else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _moment(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _emit(document: dict[str, Any], out: str | None) -> None:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="certification_conformance.py",
        description=(
            "Run the deterministic PCJ/C-gate conformance pack and record what"
            " it proves. It records build acceptance; it never certifies."
        ),
    )
    modes = parser.add_subparsers(dest="mode", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--repo", default=str(REPO_ROOT), help="checkout to write to")
        sub.add_argument("--ref", default=CERTIFICATE_REF, help="evidence ref")
        sub.add_argument("--store", default=None, help="run-store db for the ledger")
        sub.add_argument("--now", default=None, help="ISO-8601 instant (tests)")
        sub.add_argument("--out", default=None, help="also write the JSON here")

    make = modes.add_parser("candidate", help="produce a sanitized candidate")
    make.add_argument("--subject", required=True, help="the exact kew SHA")
    make.add_argument("--lane", default="claude-code/github")
    make.add_argument("--scope", default=PACK_SCOPE, help="what to run")
    make.add_argument(
        "--skip-pack",
        action="store_true",
        help="do not run the pack (the candidate records that it did not)",
    )
    common(make)
    make.set_defaults(handler=_candidate)

    record = modes.add_parser("append-approved", help="record an approved candidate")
    record.add_argument("--candidate", required=True)
    record.add_argument("--reason", required=True, help="the reviewed reason")
    record.add_argument("--actor", default=None, help="the authorized operator")
    common(record)
    record.set_defaults(handler=_append_approved)

    run = modes.add_parser("gate", help="run the pack and record what it proved")
    run.add_argument("--subject", required=True, help="the exact merged kew SHA")
    run.add_argument("--reason", required=True, help="the reviewed reason")
    run.add_argument("--actor", default=None, help="the authorized operator")
    run.add_argument("--lane", default="claude-code/github")
    run.add_argument("--scope", default=PACK_SCOPE, help="what to run")
    run.add_argument(
        "--candidate-out", default=None, help="preserve the sanitized candidate here"
    )
    common(run)
    run.set_defaults(handler=_gate)

    state = modes.add_parser("status", help="is the subject certification-ready?")
    state.add_argument("--subject", required=True)
    state.add_argument("--fingerprint", default=None, help="pin the exact evidence")
    common(state)
    state.set_defaults(handler=_status)

    check = modes.add_parser("verify", help="read back a recorded envelope")
    check.add_argument("--subject", required=True)
    check.add_argument("--digest", default=None, help="the exact envelope digest")
    common(check)
    check.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Seed the subprocess allowlist the carrier's git reads require. Without
    # this a clean standalone invocation (the way the program doc runs the gate)
    # raises an uncaught "not in allowlist" error before any work; the CLI
    # commands do the same preflight for the same reason.
    preflight_tools()
    try:
        return int(args.handler(args))
    except (Refused, CertificateRefError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
