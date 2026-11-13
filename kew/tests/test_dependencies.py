"""Tests for dependency parsing and auto-unblock logic."""

import json
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig
from kew.dependencies import (
    check_dependencies_resolved,
    depends_marker_count,
    find_blocked_dependents,
    parse_depends,
    parse_depends_ids,
)


class TestHashTokens:
    """A bare ``#N`` token in a kew:depends marker means issue ``N``."""

    def test_hash_and_bare_markers_give_the_same_ids(self):
        hashed = "<!-- kew:depends #12 #13 -->"
        bare = "<!-- kew:depends 12 13 -->"
        assert parse_depends(hashed) == parse_depends(bare) == [12, 13]
        assert parse_depends_ids(hashed) == parse_depends_ids(bare) == ["12", "13"]

    def test_mixed_forms_deduplicate_to_one_id(self):
        assert parse_depends_ids("<!-- kew:depends #12 12 -->") == ["12"]

    def test_qualified_and_native_ids_parse_as_before(self):
        body = "<!-- kew:depends owner/repo#12 ENG-123 # #abc -->"
        assert parse_depends_ids(body) == ["owner/repo#12", "ENG-123", "#", "#abc"]
        assert parse_depends(body) == []

    def test_zero_padded_tokens_read_as_the_member_number(self):
        for token in ("#02", "02", "2", "#002"):
            assert parse_depends_ids(f"<!-- kew:depends {token} -->") == ["2"]
        assert parse_depends_ids("<!-- kew:depends #02 02 2 -->") == ["2"]
        assert parse_depends("<!-- kew:depends #02 -->") == [2]
        assert parse_depends_ids("<!-- kew:depends 0 #00 -->") == ["0"]


class TestParseDepends:
    """parse_depends extracts issue numbers from kew:depends HTML comments."""

    def test_single_dependency(self):
        body = "Some text\n<!-- kew:depends 70 -->\nMore text"
        assert parse_depends(body) == [70]

    def test_multiple_dependencies(self):
        body = "<!-- kew:depends 70 71 -->"
        assert parse_depends(body) == [70, 71]

    def test_many_dependencies(self):
        body = "<!-- kew:depends 1 2 3 4 5 -->"
        assert parse_depends(body) == [1, 2, 3, 4, 5]

    def test_no_comment_returns_empty(self):
        body = "Just a normal issue body with no dependencies."
        assert parse_depends(body) == []

    def test_empty_body(self):
        assert parse_depends("") == []

    def test_none_body(self):
        assert parse_depends(None) == []

    def test_empty_depends_comment(self):
        body = "<!-- kew:depends  -->"
        assert parse_depends(body) == []

    def test_whitespace_variations(self):
        body = "<!--  kew:depends  70   71  -->"
        assert parse_depends(body) == [70, 71]

    def test_malformed_non_numeric_ignored(self):
        body = "<!-- kew:depends abc 70 def 71 -->"
        assert parse_depends(body) == [70, 71]

    def test_multiline_body(self):
        body = """## Summary
Fix the widget

## Dependencies

<!-- kew:depends 42 43 -->
<!-- Add space-separated issue numbers above -->

## Acceptance Criteria
- [ ] Widget works
"""
        assert parse_depends(body) == [42, 43]

    def test_every_comment_is_unioned(self):
        # #2652 carried two comments; the second was silently dropped.
        body = "Text\n<!-- kew:depends 2502 -->\n<!-- kew:depends 2500 -->"
        assert parse_depends(body) == [2502, 2500]
        assert parse_depends_ids(body) == ["2502", "2500"]
        assert depends_marker_count(body) == 2

    def test_one_comment_parses_as_before(self):
        body = "Text\n<!-- kew:depends 70 abc 71 70 -->\nMore"
        assert parse_depends(body) == [70, 71, 70]
        assert parse_depends_ids(body) == ["70", "abc", "71"]
        assert depends_marker_count(body) == 1

    def test_comment_in_inline_code_is_ignored(self):
        body = "Write `<!-- kew:depends 99 -->` to declare one.\n"
        assert parse_depends(body) == []
        assert parse_depends_ids(body) == []
        assert depends_marker_count(body) == 0

    def test_comment_in_double_backtick_span_is_ignored(self):
        body = "Like ``<!-- kew:depends 99 -->`` here"
        assert parse_depends(body) == []

    def test_comment_in_fenced_block_is_ignored(self):
        body = "Syntax:\n```html\n<!-- kew:depends 99 -->\n```\n<!-- kew:depends 5 -->"
        assert parse_depends(body) == [5]
        assert parse_depends_ids(body) == ["5"]
        assert depends_marker_count(body) == 1

    def test_comment_in_tilde_fence_is_ignored(self):
        body = "~~~\n<!-- kew:depends 99 -->\n~~~\n"
        assert parse_depends(body) == []

    def test_comment_after_an_unclosed_fence_still_counts(self):
        # A stray fence must not swallow the marker `kew epic plan` appends.
        body = (
            "Notes\n```python\nx = 1\n\n## Dependencies\n\n<!-- kew:depends 2500 -->\n"
        )
        assert parse_depends(body) == [2500]
        assert parse_depends_ids(body) == ["2500"]
        assert depends_marker_count(body) == 1

    def test_closed_fence_after_an_unclosed_one_is_still_skipped(self):
        body = "```\n~~~\n<!-- kew:depends 99 -->\n~~~\n<!-- kew:depends 5 -->"
        assert parse_depends(body) == [5]

    def test_many_unclosed_fences_do_not_exhaust_the_stack(self):
        # PR #2670 review: the rescan recursed once per unclosed fence, so a
        # ~6KB body of info-string fences raised RecursionError for the epic.
        body = "```x\n" * 5000 + "<!-- kew:depends 2500 -->\n"
        assert parse_depends(body) == [2500]
        assert parse_depends_ids(body) == ["2500"]
        assert depends_marker_count(body) == 1

    def test_longer_unclosed_fence_does_not_unhide_a_closed_one(self):
        # The four-backtick opener has no closer long enough; the three-backtick
        # fence below it still closes and still hides its marker.
        body = "````\n```\n<!-- kew:depends 99 -->\n```\n<!-- kew:depends 5 -->"
        assert parse_depends(body) == [5]

    def test_comment_beside_a_closed_code_span_still_counts(self):
        body = "see `x` <!-- kew:depends 7 -->"
        assert parse_depends(body) == [7]

    def test_indented_comment_still_counts(self):
        body = "- item\n    <!-- kew:depends 8 -->"
        assert parse_depends(body) == [8]

    def test_comment_with_no_numbers(self):
        body = "<!-- kew:depends -->"
        assert parse_depends(body) == []


class TestCheckDependenciesResolved:
    """check_dependencies_resolved checks if all dependency issues are closed."""

    @patch("kew.dependencies.run")
    def test_all_closed_returns_true(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"state": "CLOSED"}),
        )
        assert check_dependencies_resolved([70, 71], "owner/repo") is True
        assert mock_run.call_count == 2

    @patch("kew.dependencies.run")
    def test_one_open_returns_false(self, mock_run: MagicMock):
        def side_effect(cmd, **kwargs):
            issue_num = cmd[3]  # "gh issue view <num> ..."
            if issue_num == "70":
                return MagicMock(returncode=0, stdout=json.dumps({"state": "CLOSED"}))
            return MagicMock(returncode=0, stdout=json.dumps({"state": "OPEN"}))

        mock_run.side_effect = side_effect
        assert check_dependencies_resolved([70, 71], "owner/repo") is False

    @patch("kew.dependencies.run")
    def test_empty_list_returns_true(self, mock_run: MagicMock):
        assert check_dependencies_resolved([], "owner/repo") is True
        mock_run.assert_not_called()

    @patch("kew.dependencies.run")
    def test_gh_failure_treated_as_open(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert check_dependencies_resolved([70], "owner/repo") is False

    @patch("kew.dependencies.run")
    def test_malformed_json_treated_as_open(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        assert check_dependencies_resolved([70], "owner/repo") is False


class TestFindBlockedDependents:
    """find_blocked_dependents finds issues to unblock after a completion."""

    @patch("kew.dependencies.check_dependencies_resolved")
    @patch("kew.dependencies.run")
    def test_unblocks_dependent_with_all_deps_closed(
        self, mock_run: MagicMock, mock_check: MagicMock
    ):
        config = DispatchConfig()
        blocked_issues = [
            {
                "number": 72,
                "body": "<!-- kew:depends 70 -->",
                "labels": [{"name": "blocked"}],
            }
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(blocked_issues)
        )
        mock_check.return_value = True

        result = find_blocked_dependents(70, config)
        assert result == [72]

    @patch("kew.dependencies.check_dependencies_resolved")
    @patch("kew.dependencies.run")
    def test_skips_issue_not_depending_on_completed(
        self, mock_run: MagicMock, mock_check: MagicMock
    ):
        config = DispatchConfig()
        blocked_issues = [
            {
                "number": 72,
                "body": "<!-- kew:depends 99 -->",
                "labels": [{"name": "blocked"}],
            }
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(blocked_issues)
        )

        result = find_blocked_dependents(70, config)
        assert result == []
        mock_check.assert_not_called()

    @patch("kew.dependencies.check_dependencies_resolved")
    @patch("kew.dependencies.run")
    def test_multi_dep_not_unblocked_if_other_still_open(
        self, mock_run: MagicMock, mock_check: MagicMock
    ):
        config = DispatchConfig()
        blocked_issues = [
            {
                "number": 73,
                "body": "<!-- kew:depends 70 71 -->",
                "labels": [{"name": "blocked"}],
            }
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(blocked_issues)
        )
        mock_check.return_value = False

        result = find_blocked_dependents(70, config)
        assert result == []

    @patch("kew.dependencies.check_dependencies_resolved")
    @patch("kew.dependencies.run")
    def test_multiple_dependents_some_ready(
        self, mock_run: MagicMock, mock_check: MagicMock
    ):
        config = DispatchConfig()
        blocked_issues = [
            {
                "number": 72,
                "body": "<!-- kew:depends 70 -->",
                "labels": [{"name": "blocked"}],
            },
            {
                "number": 73,
                "body": "<!-- kew:depends 70 71 -->",
                "labels": [{"name": "blocked"}],
            },
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(blocked_issues)
        )
        # #72 deps all resolved, #73 still has open dep
        mock_check.side_effect = [True, False]

        result = find_blocked_dependents(70, config)
        assert result == [72]

    @patch("kew.dependencies.check_dependencies_resolved")
    @patch("kew.dependencies.run")
    def test_completed_issue_excluded_from_dep_check(
        self, mock_run: MagicMock, mock_check: MagicMock
    ):
        """The completed issue should not be re-checked as a dependency."""
        config = DispatchConfig()
        blocked_issues = [
            {
                "number": 73,
                "body": "<!-- kew:depends 70 71 -->",
                "labels": [{"name": "blocked"}],
            }
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(blocked_issues)
        )
        mock_check.return_value = True

        find_blocked_dependents(70, config)
        # completed_issue=70 should be excluded; only [71] passed to check
        mock_check.assert_called_once_with([71], config.project.repo)

    @patch("kew.dependencies.run")
    def test_gh_list_failure_returns_empty(self, mock_run: MagicMock):
        config = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = find_blocked_dependents(70, config)
        assert result == []

    @patch("kew.dependencies.run")
    def test_idempotent_no_error_on_double_call(self, mock_run: MagicMock):
        config = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        first = find_blocked_dependents(70, config)
        second = find_blocked_dependents(70, config)
        assert first == []
        assert second == []
        assert mock_run.call_count == 2


class TestParseDependsIds:
    def test_numeric_tokens_kept_as_strings(self):
        assert parse_depends_ids("<!-- kew:depends 70 71 -->") == ["70", "71"]

    def test_native_ids_survive(self):
        result = parse_depends_ids("<!-- kew:depends ENG-123 144 -->")
        assert result == ["ENG-123", "144"]

    def test_dedup_preserves_first_seen_order(self):
        assert parse_depends_ids("<!-- kew:depends 5 5 3 -->") == ["5", "3"]

    def test_empty_and_none(self):
        assert parse_depends_ids("") == []
        assert parse_depends_ids(None) == []
        assert parse_depends_ids("no marker here") == []

    def test_legacy_parse_depends_unchanged(self):
        # Guard: the int-returning legacy path must not change behavior.
        assert parse_depends("<!-- kew:depends 70 71 -->") == [70, 71]
        assert parse_depends("<!-- kew:depends ENG-123 144 -->") == [144]
