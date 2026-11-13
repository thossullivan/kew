"""Tests for the DetailPane widget and format_detail helper."""

from __future__ import annotations


class TestFormatDetail:
    def _make_issue(self, **kwargs):
        base = {
            "number": 122,
            "title": "docs: update all documentation to reflect current feature set",
            "body": "## Summary\n\nThe documentation needs updating.",
            "labels": [{"name": "ready"}, {"name": "p0-critical"}],
            "assignees": [{"login": "localuser"}],
            "createdAt": "2026-03-22T10:00:00Z",
        }
        base.update(kwargs)
        return base

    def test_with_p0_priority(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "ready"}, {"name": "p0-critical"}])
        output = format_detail(issue)
        assert "#122" in output
        assert "P0" in output
        assert "Priority:" in output

    def test_with_p1_priority(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "p1-high"}])
        output = format_detail(issue)
        assert "P1" in output

    def test_without_priority(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "ready"}])
        output = format_detail(issue)
        assert "--" in output
        assert "Priority:" in output

    def test_complexity_simple(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "simple"}, {"name": "ready"}])
        output = format_detail(issue)
        assert "simple" in output
        assert "Complexity:" in output

    def test_complexity_complex(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "complex"}])
        output = format_detail(issue)
        assert "complex" in output

    def test_complexity_defaults_to_medium(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(labels=[{"name": "ready"}])
        output = format_detail(issue)
        assert "medium" in output

    def test_with_body(self):
        from kew.tui.widgets.detail_pane import format_detail

        body = "## Summary\n\nThis is a test issue body with details."
        issue = self._make_issue(body=body)
        output = format_detail(issue)
        assert "This is a test issue body with details." in output

    def test_with_empty_body(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(body="")
        output = format_detail(issue)
        assert "#122" in output

    def test_with_none_body(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(body=None)
        output = format_detail(issue)
        assert "#122" in output

    def test_with_assignees(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(assignees=[{"login": "alice"}, {"login": "bob"}])
        output = format_detail(issue)
        assert "alice" in output
        assert "bob" in output

    def test_without_assignees(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(assignees=[])
        output = format_detail(issue)
        assert "none" in output
        assert "Assignees:" in output

    def test_created_date_formatted(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(createdAt="2026-03-22T10:00:00Z")
        output = format_detail(issue)
        assert "2026-03-22" in output
        assert "Created:" in output

    def test_all_labels_listed(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(
            labels=[{"name": "ready"}, {"name": "p0-critical"}, {"name": "complex"}]
        )
        output = format_detail(issue)
        assert "ready" in output
        assert "p0-critical" in output
        assert "complex" in output
        assert "Labels:" in output

    def test_title_in_header(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue(title="My important issue title")
        output = format_detail(issue)
        assert "My important issue title" in output

    def test_header_has_priority(self):
        from kew.tui.widgets.detail_pane import format_detail

        issue = self._make_issue()
        output = format_detail(issue)
        assert "Priority:" in output


class TestDetailPaneWidget:
    def test_is_vertical_scroll(self):
        from textual.containers import VerticalScroll

        from kew.tui.widgets.detail_pane import DetailPane

        assert issubclass(DetailPane, VerticalScroll)

    def test_compose_yields_static(self):
        from kew.tui.widgets.detail_pane import DetailPane

        pane = DetailPane()
        widgets = list(pane.compose())
        type_names = [type(w).__name__ for w in widgets]
        assert "Static" in type_names

    def test_has_update_issue(self):
        from kew.tui.widgets.detail_pane import DetailPane

        pane = DetailPane()
        assert hasattr(pane, "update_issue")


class TestDetailPaneUpdatesContent:
    def test_format_header_has_metadata(self):
        from kew.tui.widgets.detail_pane import format_header

        issue = {
            "number": 42,
            "title": "Test issue",
            "labels": [{"name": "ready"}],
            "assignees": [],
            "createdAt": "2026-03-22T10:00:00Z",
        }
        header = format_header(issue)
        assert "#42" in header
        assert "Test issue" in header
        assert "2026-03-22" in header
