"""Every surface a run retains text on, for credential-redaction tests.

``retained(value, surface, ...)`` sends *value* through one retained surface —
the progress display, an exception's text, derived evidence, the derived
report, the published agent report, or a circuit-breaker audit event — and
returns what that surface kept, so a test can assert a credential never
survives it.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig
from kew.progress import display_event
from kew.runner_fields import exception_text, runner_text
from kew.transcript_metrics import StreamDerivation, write_derived_evidence
from support.agent_report import visible_text

SURFACES = ("display", "exception", "evidence", "report", "publication", "audit")


def _audit_text(text, tmp_path):
    from kew.dispatch_core import _maybe_emit_breaker
    from kew.run_store import SqliteRunStore
    from kew.tracking import DispatchResult

    with SqliteRunStore(str(tmp_path / "audit.db")) as store:
        run = SimpleNamespace(
            store=store,
            config=DispatchConfig(),
            issue_num=1933,
            item_id=None,
            run_id=None,
            effective_model="claude-sonnet-4-6",
            complexity="medium",
        )
        result = DispatchResult(exit_code=124, issue_num=1933, log_file=None)
        result.oom_probe_diagnostic = text
        _maybe_emit_breaker(run, result)
        [event] = store.list_audit_events(event="circuit_breaker")
        return json.dumps(event.payload)


def _report_text(text, surface, tmp_path):
    from kew.dispatch_core import _post_agent_report

    log = tmp_path / "dispatch.log"
    derivation = StreamDerivation()
    derivation.observe(json.dumps({"type": "result", "result": text}))
    path = write_derived_evidence(log, derivation.evidence())
    if surface == "report":
        return json.loads(path.read_text())["report"]
    with patch("kew.dispatch_core.run", return_value=MagicMock(returncode=0)) as post:
        _post_agent_report(
            "https://github.com/example/repo/pull/1941", log, DispatchConfig()
        )
    argv = post.call_args.args[0]
    # The posted report is quoted with zero-width breaks (#3349): read what
    # a reader sees.
    return visible_text(argv[argv.index("--body") + 1])


def retained(value, surface, tmp_path, capsys):
    if surface == "display":
        display_event({"type": "result", "result": runner_text(value)})
        return capsys.readouterr().err
    if surface == "exception":
        return exception_text(ValueError(value))
    if surface == "evidence":
        evidence = SimpleNamespace(as_json=lambda: {"payload": value})
        path = write_derived_evidence(tmp_path / "dispatch.log", evidence)
        return json.dumps(json.loads(path.read_text())["payload"])
    if surface == "audit":
        return _audit_text(value, tmp_path)
    return _report_text(value, surface, tmp_path)
