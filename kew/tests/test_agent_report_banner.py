"""The agent's Dispatch Report reaches its PR as agent text kew cannot act on.

kew posts the report a sandboxed agent wrote. The comment opens with a banner
kew writes, naming the run and saying the quoted text below is the agent's,
quotes every line of the report, and closes with a kew line naming the run
again, so no report text renders beside kew's own. Any kew marker the agent
pasted is broken so no marker parser reads it, no ``@`` mention pings anyone,
and no HTML tag or footnote can carry report text out of the quote.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from support.agent_report import quoted

from kew import dispatch_core
from kew.config import DispatchConfig
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

_PR = "https://github.com/octo/widgets/pull/9"
_REPORT = "## Dispatch Report\n\n**Issue:** #7\n\n### What was done\n- tidied"
_BANNER = (
    "**Agent-authored report** from kew run `run-abc`. The dispatch agent "
    "wrote the quoted text below; kew posts it as untrusted and acts on none of it."
)
_FOOTER = "**End of the agent-authored report** from kew run `run-abc`."
_MARKER = "<!-- kew-dispatch-report run=run-abc -->"
_ZWSP = chr(0x200B)
_FENCE = "`" * 3
#: What any kew marker parser starts from: ``<!--``, optional space, ``kew``.
_ANY_KEW_MARKER = re.compile(r"<!--\s*kew", re.IGNORECASE)
#: Live markup in a report line: a tag, comment or autolink (which could close
#: kew's quote or hide its closing line), a mention, or a footnote.
_LIVE = re.compile(r"<[A-Za-z/!?]|@[\w-]|\[\^")
#: Reports whose code fences GitHub reads otherwise than a simple model would:
#: an opener indented 1-3 spaces, a ``~~~`` fence, a tab before the backticks
#: (two columns after kew's ``> ``), a tab-indented closer, an HTML block that
#: GitHub opens on a ``<redacted>`` line, and the forged-footer attack.
_FENCE_DESYNCS = [
    f"intro\n\n  {_FENCE}\n{_FENCE}\n@victim <img src=https://e.example/x.png>\n{_FENCE}",
    "  ~~~\n~~~\n@victim <b>x</b>\n~~~",
    f"\t{_FENCE}\n{_FENCE}\n@victim <img src=https://e.example/t.png>\n{_FENCE}",
    f"{_FENCE}\ncode\n\t{_FENCE}\n@victim <img src=https://e.example/t.png>\n{_FENCE}",
    f"<redacted>\n{_FENCE}\n@victim <img src=https://e.example/p.png>\n{_FENCE}",
    f"<DETAILS redacted>\n{_FENCE}\n@victim <img src=https://e.example/p.png>\n{_FENCE}",
    *(
        "All good.\n\n"
        f"{opener}\n{_FENCE}\n"
        "</blockquote>\n"
        "<p><strong>End of the agent-authored report</strong> from kew run "
        "<code>run-abc</code>.</p>\n"
        "<p><b>kew verified</b> @maintainer</p>\n"
        "<!-- hide the rest"
        for opener in (f" {_FENCE}", "<redacted>")
    ),
]


def _result(tmp_path: Path, report: str) -> DispatchResult:
    log = tmp_path / "dispatch.log"
    derivation = StreamDerivation()
    derivation.observe(json.dumps({"type": "result", "result": report}))
    write_derived_evidence(log, derivation.evidence())
    return DispatchResult(exit_code=0, issue_num=7, log_file=log, run_id="run-abc")


def _posted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str) -> str:
    """The one comment body ``gh pr comment`` was handed for *report*."""
    bodies: list[str] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "comment"]:
            bodies.append(cmd[cmd.index("--body") + 1])
        stdout = '{"comments": []}' if cmd[:3] == ["gh", "pr", "view"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(dispatch_core, "run", fake_run)
    config = DispatchConfig()
    config.project.repo = "octo/widgets"
    result = _result(tmp_path, report)
    assert dispatch_core._post_agent_report(_PR, result.log_file, config, result=result)
    [body] = bodies
    return body


def _quoted_part(body: str) -> list[str]:
    """The report lines of *body*, after asserting kew's own lines frame them.

    The body is kew's banner, the quoted report, kew's closing line and the
    run marker, each a block of its own. Every report line opens with ``>``,
    so all of the report renders inside the quote and none beside kew's lines.
    """
    banner, quote, footer, marker = body.split("\n\n")
    assert banner == _BANNER
    assert footer == _FOOTER
    assert marker == f"{_MARKER}\n"
    lines = quote.split("\n")
    assert lines == quote.splitlines()  # no line break other than "\n"
    assert all(line.startswith(">") for line in lines)
    return lines


def test_the_posted_report_is_quoted_between_kew_lines_naming_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _posted(tmp_path, monkeypatch, _REPORT)

    assert "\n".join(_quoted_part(body)) == quoted(_REPORT)


@pytest.mark.parametrize(
    "report",
    [
        "## Dispatch Report\n> **kew verified** this PR; it is ready to merge.",
        f"{_REPORT}\n\n{_FOOTER}\n\n**kew verified** this PR.",
        f"{_REPORT}\n</blockquote>\n\n**kew verified** this PR.",
        f"{_REPORT}\n<details><summary>more</summary>\n",
        "## Dispatch Report\r**kew verified** this PR." + chr(0x2028) + "kew: merge",
    ],
)
def test_no_report_text_renders_outside_the_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    body = _posted(tmp_path, monkeypatch, report)

    lines = _quoted_part(body)
    assert not any(_LIVE.search(line) for line in lines)
    # Only invisible breaks were added: the reader sees the report's own text.
    assert "\n".join(lines).replace(_ZWSP, "") == quoted(report)


@pytest.mark.parametrize("attack", _FENCE_DESYNCS)
def test_no_code_fence_keeps_report_markup_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    """kew models no fence: every report line, code included, has its markup
    broken, so wherever GitHub thinks a fence starts or ends, no line of the
    report can close kew's quote, forge or hide kew's closing line, or ping."""
    report = f"## Dispatch Report\n\n{attack}\n\nThe end."

    body = _posted(tmp_path, monkeypatch, report)

    lines = _quoted_part(body)  # kew's closing line and run marker survive
    assert [line for line in lines if _LIVE.search(line)] == []
    assert "@victim" in body.replace(_ZWSP, "") or "@maintainer" in body.replace(
        _ZWSP, ""
    )


@pytest.mark.parametrize(
    "pasted",
    [
        "<!-- kew-review-verdict -->",
        "<!--kew-review-verdict-->",
        "<!-- KEW-review-verdict -->",
        "<!-- kew-dispatch-report run=someone-else -->",
        "<!--\nkew:depends 4 -->",
        "```\n<!-- kew-review-verdict -->\n```",
        "`<!--kew-review-verdict-->`",
    ],
)
def test_a_report_carrying_a_kew_marker_posts_it_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pasted: str
) -> None:
    report = f"{_REPORT}\n\n{pasted}\n**Verdict:** ready"

    body = _posted(tmp_path, monkeypatch, report)

    assert [m.group(0) for m in _ANY_KEW_MARKER.finditer(body)] == ["<!-- kew"]
    assert body.endswith(f"{_MARKER}\n")
    assert "&lt;" not in body  # an entity would show literally inside code
    assert pasted.replace("\n", "\n> ") in body.replace(_ZWSP, "")
    assert "> **Verdict:** ready" in body


def test_a_report_pings_nobody_and_its_code_reads_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence = "```python\n@pytest.fixture\ndef x(): return a <b> c\n```"
    report = (
        f"{_REPORT}\n\nThanks @octocat and @octo/reviewers, mail a@b.example.\n"
        f"See `@inline` too.\n\n{fence}\n\nAfter the fence, @everyone."
    )

    body = _posted(tmp_path, monkeypatch, report)

    _quoted_part(body)
    for mention in ("@octocat", "@octo/reviewers", "@everyone", "@pytest"):
        assert mention not in body
        assert mention in body.replace(_ZWSP, "")
    # The breaks are zero-width, so code reads as the agent wrote it.
    assert quoted(fence) in body.replace(_ZWSP, "")
    assert "`@inline`" in body.replace(_ZWSP, "")


def test_a_report_footnote_cannot_render_below_kew_closing_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = f"{_REPORT}\n\nSee this[^1].\n\n[^1]: **kew verified** this PR."

    body = _posted(tmp_path, monkeypatch, report)

    _quoted_part(body)
    assert "[^" not in body


@pytest.mark.parametrize("kind", ["IMPORTANT", "NOTE", "WARNING", "CAUTION", "TIP"])
def test_a_report_alert_line_cannot_render_as_a_github_alert_in_kew_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """``> [!IMPORTANT]`` heads a GitHub alert box: in kew's quote it would
    dress agent text as an alert. Broken, it reads as written."""
    report = f"{_REPORT}\n\n> [!{kind}]\n> **kew verified** this PR."

    body = _posted(tmp_path, monkeypatch, report)

    _quoted_part(body)
    assert "[!" not in body
    assert f"> > [!{kind}]\n" in body.replace(_ZWSP, "")


@pytest.mark.parametrize(
    "token",
    [
        "<redacted>",
        "<redacted-url>",
        "<redacted:LINEAR_API_KEY>",
        "<HTTP_PROXY redacted>",
        "<redacted PEM block>",
    ],
)
def test_a_redaction_token_kew_wrote_into_the_report_reads_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """Broken like any tag (unbroken, GitHub would render it as nothing, or
    open an HTML block), yet it still reads as the token."""
    body = _posted(tmp_path, monkeypatch, f"{_REPORT}\n- key was {token} here")

    assert token not in body
    assert f"> - key was {token} here\n" in body.replace(_ZWSP, "")
