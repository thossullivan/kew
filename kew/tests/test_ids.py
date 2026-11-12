"""Tests for kew.ids — opaque work-item id validation and formatting."""

import pytest

from kew.ids import (
    InvalidItemIdError,
    branch_slug,
    closes_marker,
    ensure_closes_marker,
    item_id_sort_key,
    item_ref,
    item_ref_or_num,
    provider_from_config,
    validate_item_id,
)


class TestValidateItemId:
    def test_github_numeric_ok(self):
        assert validate_item_id("123", provider="github") == "123"

    def test_linear_identifier_ok(self):
        assert validate_item_id("ENG-123", provider="linear") == "ENG-123"

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "../../etc",  # traversal
            "123\nrogue",  # newline (log/audit injection)
            "ENG 123",  # whitespace
            "eng-123",  # Linear ids are canonical-uppercase
            "issue-123/../..",  # path metachars
            "123; rm -rf",  # shell metachars
            "‮ENG-1",  # unicode direction override
        ],
    )
    def test_rejects_never_sanitizes(self, bad):
        with pytest.raises(InvalidItemIdError):
            validate_item_id(bad, provider="github")
        with pytest.raises(InvalidItemIdError):
            validate_item_id(bad, provider="linear")

    def test_github_rejects_linear_shape(self):
        with pytest.raises(InvalidItemIdError):
            validate_item_id("ENG-123", provider="github")

    def test_unknown_provider_raises(self):
        with pytest.raises(InvalidItemIdError):
            validate_item_id("123", provider="jira")


class TestBranchSlug:
    def test_github_passthrough(self):
        assert branch_slug("123") == "123"

    def test_linear_lowercased(self):
        assert branch_slug("ENG-123") == "eng-123"


class TestSortKey:
    def test_numeric_ids_sort_numerically(self):
        ids = ["10", "9", "1"]
        assert sorted(ids, key=item_id_sort_key) == ["1", "9", "10"]

    def test_mixed_ids_numeric_first_then_lexical(self):
        ids = ["ENG-2", "10", "ENG-10", "9"]
        assert sorted(ids, key=item_id_sort_key) == ["9", "10", "ENG-10", "ENG-2"]


class TestItemRef:
    def test_github_numeric_gets_hash(self):
        assert item_ref("42") == "#42"

    def test_linear_id_stays_bare(self):
        assert item_ref("SFD-12") == "SFD-12"


class TestItemRefOrNum:
    def test_prefers_item_id_when_present(self):
        assert item_ref_or_num("SFD-12", 0) == "SFD-12"

    def test_github_item_id_gets_hash(self):
        assert item_ref_or_num("42", 42) == "#42"

    def test_falls_back_to_issue_num_when_item_id_none(self):
        # Legacy rows written before the item_id column carry only the int.
        assert item_ref_or_num(None, 42) == "#42"


class TestClosesMarker:
    def test_github_numeric_gets_closes(self):
        assert closes_marker("42") == "Closes #42"

    def test_linear_id_bare_no_closes(self):
        # Linear PRs carry the item id but never a `Closes` marker GitHub can't
        # resolve — resolution comes from the branch lookup (A6).
        assert closes_marker("SFD-12") == "SFD-12"


class TestEnsureClosesMarker:
    @pytest.mark.parametrize(
        "keyword",
        [
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ],
    )
    def test_accepts_every_github_keyword_in_any_case(self, keyword: str):
        body = f"Details\n\n{keyword.upper()} #42"

        assert ensure_closes_marker(body, "42") == body

    def test_appends_the_marker_once_and_is_idempotent(self):
        body = "Details"

        repaired = ensure_closes_marker(body, "42")

        assert repaired == "Details\n\nCloses #42"
        assert ensure_closes_marker(repaired, "42") == repaired

    def test_other_issue_marker_does_not_satisfy_item(self):
        assert ensure_closes_marker("Closes #41", "42") == ("Closes #41\n\nCloses #42")


class TestProviderFromConfig:
    def test_github_default(self):
        from kew.config import TrackerConfig

        class _FakeConfig:
            tracker = TrackerConfig(type="github")

        assert provider_from_config(_FakeConfig()) == "github"

    def test_linear(self):
        from kew.config import TrackerConfig

        class _FakeConfig:
            tracker = TrackerConfig(type="linear")

        assert provider_from_config(_FakeConfig()) == "linear"
