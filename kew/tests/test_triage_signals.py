"""Host-side triage signals: measured, not judged (#2293).

:mod:`kew.triage.signals` is the half of triage that needs no model, so it is
the half a reader has to be able to trust absolutely. Two properties matter:

* **The duplicate candidates are the host's.** They come from
  ``gh search issues`` over open AND closed issues, capped at
  ``[triage] dedupe_candidates``, carrying numbers and titles only — and they
  are the only numbers a classifier is allowed to name as a duplicate
  (``kew.triage.policy``).
* **A search that could not run is not "no duplicates".** Every failure path
  comes back as ``None`` from the reader and lands in ``unavailable``, because
  a silent empty list would read as a clean answer (CONTRIBUTING.md, "No
  silent clean defaults").
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from kew.config import DispatchConfig, ProjectConfig, TriageConfig
from kew.triage.signals import (
    DEDUPE_SEARCH_SOURCE,
    body_features,
    collect_signals,
    duplicate_candidates,
    error_strings,
    search_query,
    title_terms,
)

BUG_REPORT = """
## Steps to reproduce

1. run `kew dispatch 7`
2. watch the run

Expected: a run id. Actual: a KeyError.

kew 1.4.2

Traceback (most recent call last):
  File "dispatch.py", line 44, in run
    raise KeyError("run_id")
"""


def _config(**triage: Any) -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(name="kew", repo="owner/repo"),
        triage=TriageConfig(**triage),
    )


def _gh(
    stdout: str = "[]", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


class TestBodyFeatures:
    def test_a_complete_bug_report_measures_every_feature(self) -> None:
        features = body_features(BUG_REPORT)
        assert features.has_steps is True
        assert features.has_expected_actual is True
        assert features.has_version is True
        assert features.has_stack_trace is True
        assert features.length == len(BUG_REPORT)

    def test_a_one_liner_measures_none_of_them(self) -> None:
        features = body_features("it is broken")
        assert (
            features.has_steps,
            features.has_expected_actual,
            features.has_version,
            features.has_stack_trace,
        ) == (False, False, False, False)

    def test_an_absent_body_is_zero_length_not_a_crash(self) -> None:
        assert body_features(None).length == 0

    def test_a_numbered_list_alone_counts_as_steps(self) -> None:
        assert body_features("1. open it\n2. it breaks").has_steps is True

    def test_a_javascript_stack_trace_is_recognised_too(self) -> None:
        trace = "TypeError: x is not a function\n    at foo (bundle.js:1:2)"
        assert body_features(trace).has_stack_trace is True


class TestQueryTerms:
    def test_title_terms_drop_stopwords_and_short_words(self) -> None:
        assert title_terms("The dispatcher drops a run id when it is busy") == [
            "dispatcher",
            "drops",
            "busy",
        ]

    def test_title_terms_are_distinct_and_capped(self) -> None:
        terms = title_terms(" ".join(f"word{n}" for n in range(20)))
        assert len(terms) == 6
        assert len(set(terms)) == 6

    def test_error_strings_are_the_names_the_body_quotes(self) -> None:
        assert error_strings("ValueError: boom, then KeyError: nope") == [
            "ValueError",
            "KeyError",
        ]

    def test_error_strings_are_capped(self) -> None:
        body = "AError: a BError: b CError: c DError: d"
        assert len(error_strings(body)) == 3

    def test_the_query_joins_the_title_terms_and_the_error_strings(self) -> None:
        assert search_query("dispatcher drops run", "KeyError: run_id") == (
            "dispatcher drops KeyError"
        )

    def test_a_title_of_stopwords_with_no_error_gives_no_query(self) -> None:
        assert search_query("it is the one that was", None) == ""


class TestDuplicateCandidates:
    def test_open_and_closed_issues_are_both_searched(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *_a: Any, **_k: Any) -> Any:
            calls.append(cmd)
            state = cmd[cmd.index("--state") + 1]
            number = 1 if state == "open" else 2
            return _gh(json.dumps([{"number": number, "title": state}]))

        with patch("kew.triage.signals.run", fake_run):
            found = duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config()
            )
        assert [(c.number, c.title) for c in found] == [(1, "open"), (2, "closed")]
        assert [cmd[cmd.index("--state") + 1] for cmd in calls] == ["open", "closed"]

    def test_only_numbers_and_titles_are_requested(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], *_a: Any, **_k: Any) -> Any:
            seen.append(cmd)
            return _gh()

        with patch("kew.triage.signals.run", fake_run):
            duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config()
            )
        for cmd in seen:
            assert cmd[cmd.index("--json") + 1] == "number,title"
            assert cmd[:3] == ["gh", "search", "issues"]

    def test_the_candidate_list_is_capped(self) -> None:
        rows = json.dumps([{"number": n, "title": f"t{n}"} for n in range(1, 20)])
        with patch("kew.triage.signals.run", lambda *a, **k: _gh(rows)):
            found = duplicate_candidates(
                title="dispatcher drops run",
                body=None,
                config=_config(dedupe_candidates=3),
            )
        assert len(found) == 3

    def test_the_item_itself_is_never_its_own_candidate(self) -> None:
        rows = json.dumps([{"number": 7, "title": "me"}, {"number": 8, "title": "x"}])
        with patch("kew.triage.signals.run", lambda *a, **k: _gh(rows)):
            found = duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config(), exclude=7
            )
        assert [c.number for c in found] == [8]

    def test_a_duplicate_number_across_states_appears_once(self) -> None:
        rows = json.dumps([{"number": 8, "title": "x"}])
        with patch("kew.triage.signals.run", lambda *a, **k: _gh(rows)):
            found = duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config()
            )
        assert [c.number for c in found] == [8]

    def test_nothing_searchable_is_an_empty_answer_not_a_failure(self) -> None:
        with patch("kew.triage.signals.run") as runner:
            found = duplicate_candidates(title="it is the", body=None, config=_config())
        assert found == ()
        runner.assert_not_called()

    def test_no_repository_configured_is_an_empty_answer(self) -> None:
        config = DispatchConfig(triage=TriageConfig())
        config.project.repo = ""
        with patch("kew.triage.signals.run") as runner:
            assert (
                duplicate_candidates(
                    title="dispatcher drops run", body=None, config=config
                )
                == ()
            )
        runner.assert_not_called()


class TestAFailedSearchIsNeverACleanAnswer:
    @pytest.mark.parametrize(
        "result",
        [
            _gh(returncode=1, stderr="gh: not authenticated"),
            _gh(stdout="not json"),
            _gh(stdout='{"number": 1}'),
        ],
        ids=["nonzero-exit", "unreadable-json", "not-a-list"],
    )
    def test_every_failure_shape_answers_none(self, result: Any) -> None:
        with patch("kew.triage.signals.run", lambda *a, **k: result):
            assert (
                duplicate_candidates(
                    title="dispatcher drops run", body=None, config=_config()
                )
                is None
            )

    def test_a_raising_subprocess_answers_none(self) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("gh is gone")

        with patch("kew.triage.signals.run", boom):
            assert (
                duplicate_candidates(
                    title="dispatcher drops run", body=None, config=_config()
                )
                is None
            )

    def test_a_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level("WARNING"),
            patch(
                "kew.triage.signals.run",
                lambda *a, **k: _gh(returncode=1, stderr="rate limited"),
            ),
        ):
            duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config()
            )
        assert "rate limited" in caplog.text

    def test_one_state_failing_with_results_from_the_other_is_partial_not_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Half an answer is served, and the gap is named in the log."""

        def fake_run(cmd: list[str], *_a: Any, **_k: Any) -> Any:
            if cmd[cmd.index("--state") + 1] == "closed":
                return _gh(returncode=1, stderr="boom")
            return _gh(json.dumps([{"number": 1, "title": "open one"}]))

        with caplog.at_level("WARNING"), patch("kew.triage.signals.run", fake_run):
            found = duplicate_candidates(
                title="dispatcher drops run", body=None, config=_config()
            )
        assert [c.number for c in found] == [1]
        assert "candidate list is partial" in caplog.text


class TestCollectSignals:
    def test_the_signals_carry_the_digest_features_labels_and_candidates(self) -> None:
        rows = json.dumps([{"number": 812, "title": "an older report"}])
        with patch("kew.triage.signals.run", lambda *a, **k: _gh(rows)):
            signals = collect_signals(
                item_id="2293",
                title="dispatcher drops run id",
                body=BUG_REPORT,
                config=_config(),
                issue_num=2293,
                labels=("kew:triage", "bug"),
            )
        assert signals.item_id == "2293"
        assert len(signals.body_digest) == 64
        assert signals.features.has_stack_trace is True
        assert signals.labels == ("kew:triage", "bug")
        assert signals.candidate_numbers == {812}
        assert signals.unavailable == ()

    def test_a_failed_search_lands_in_unavailable(self) -> None:
        with patch("kew.triage.signals.run", lambda *a, **k: _gh(returncode=1)):
            signals = collect_signals(
                item_id="2293",
                title="dispatcher drops run id",
                body=BUG_REPORT,
                config=_config(),
            )
        assert signals.candidates == ()
        assert signals.unavailable == (DEDUPE_SEARCH_SOURCE,)

    def test_search_false_skips_the_search_without_claiming_a_failure(self) -> None:
        with patch("kew.triage.signals.run") as runner:
            signals = collect_signals(
                item_id="2293",
                title="dispatcher drops run id",
                body=BUG_REPORT,
                config=_config(),
                search=False,
            )
        runner.assert_not_called()
        assert signals.candidates == ()
        assert DEDUPE_SEARCH_SOURCE not in signals.unavailable

    def test_dedupe_candidates_zero_disables_the_search(self) -> None:
        with patch("kew.triage.signals.run") as runner:
            signals = collect_signals(
                item_id="2293",
                title="dispatcher drops run id",
                body=BUG_REPORT,
                config=_config(dedupe_candidates=0),
            )
        runner.assert_not_called()
        assert signals.candidates == ()

    def test_the_digest_matches_the_recorded_plans_digest(self) -> None:
        """One body identity across the plan and the decision."""
        from kew.triage.plan import body_digest

        with patch("kew.triage.signals.run", lambda *a, **k: _gh()):
            signals = collect_signals(
                item_id="2293",
                title="t",
                body=BUG_REPORT,
                config=_config(),
            )
        assert signals.body_digest == body_digest(BUG_REPORT)
