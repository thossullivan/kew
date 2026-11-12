"""The pure scorecard module is store-free and matches the report re-export."""

from __future__ import annotations

import pytest

import kew.report as report
import kew.scorecard as scorecard


def test_report_reexports_the_pure_symbols() -> None:
    # The CLI + existing report tests import these from kew.report; keep them.
    assert report.compute_model_scorecard is scorecard.compute_model_scorecard
    assert report.render_model_scorecard is scorecard.render_model_scorecard
    assert report.ModelScorecard is scorecard.ModelScorecard


def test_scorecard_module_does_not_import_run_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    # Importing kew.scorecard alone must not pull kew.run_store into sys.modules
    # via a top-level import. (Import it fresh to be sure.) The modules come
    # back at teardown: a later `import kew.run_store` on this worker would
    # otherwise load a second copy whose SqliteRunStore fails every isinstance
    # check against stores built from the first (13 prompt-preview tests read
    # "profile none" on one xdist worker).
    for mod in ("kew.scorecard", "kew.run_store"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    import importlib

    importlib.import_module("kew.scorecard")
    assert "kew.run_store" not in sys.modules


def _rec(**overrides: object) -> object:
    """A duck-typed run record with scorecard-relevant defaults."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    base = dict(
        model="claude-opus-4-8",
        complexity="medium",
        estimated_cost_usd=1.0,
        status="success",
        run_kind="dispatch",
        pr_url="https://github.com/o/r/pull/1",
        pr_state="MERGED",
        review_cycles=1,
        started_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _row(card: scorecard.ModelScorecard, model: str) -> scorecard.ModelRow:
    return next(r for r in card.rows if r.model == model)


def test_pr_is_credited_to_the_authoring_dispatch_not_the_reviewer() -> None:
    """A reviewed PR's opened/merged land on the model that wrote it (#1131).

    The reviewer's run starts after the dispatch and carries the same pr_url,
    so latest-started_at attribution used to hand it the merge.
    """
    from datetime import UTC, datetime

    author = _rec(
        model="claude-sonnet-4-6",
        run_kind="dispatch",
        started_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
    )
    reviewer = _rec(
        model="claude-opus-5",
        run_kind="adversarial_review",
        started_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    card = scorecard.compute_model_scorecard([author, reviewer])

    authored = _row(card, "claude-sonnet-4-6")
    assert (authored.opened, authored.merged) == (1, 1)
    reviewed = _row(card, "claude-opus-5")
    assert (reviewed.opened, reviewed.merged) == (0, 0)
    # runs + spend stay per-run: the reviewer still owns what it spent.
    assert reviewed.runs == 1
    assert reviewed.spend == 1.0
    # Complexity sub-rows follow the same attribution.
    assert authored.by_complexity["medium"].merged == 1
    assert reviewed.by_complexity["medium"].merged == 0


def test_pr_is_not_credited_to_a_clock_skewed_ci_fix() -> None:
    """A CI fix never becomes the author, even with an earlier timestamp."""
    from datetime import UTC, datetime

    author = _rec(
        model="author",
        started_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
    )
    ci_fix = _rec(
        model="fixer",
        run_kind="ci_fix",
        started_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
    )

    card = scorecard.compute_model_scorecard([author, ci_fix])

    assert _row(card, "author").merged == 1
    assert _row(card, "fixer").merged == 0


def test_review_cycles_land_on_the_author_row_not_the_reviewer() -> None:
    """``review_cycles`` is a per-PR fact (GitHub review submissions), so it
    belongs to the PR's author row alongside opened/merged (#1131)."""
    from datetime import UTC, datetime

    card = scorecard.compute_model_scorecard(
        [
            _rec(
                model="claude-sonnet-4-6",
                review_cycles=3,
                started_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
            ),
            _rec(
                model="claude-opus-5",
                run_kind="review_remediation",
                review_cycles=3,
                started_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
            ),
        ]
    )
    authored = _row(card, "claude-sonnet-4-6")
    assert (authored.review_total, authored.review_n) == (3, 1)
    reviewed = _row(card, "claude-opus-5")
    assert (reviewed.review_total, reviewed.review_n) == (0, 0)


def test_earliest_dispatch_authors_the_pr_when_redispatched() -> None:
    """Two dispatch runs on one PR: the first one wrote it."""
    from datetime import UTC, datetime

    card = scorecard.compute_model_scorecard(
        [
            _rec(model="A", started_at=datetime(2026, 7, 20, 10, tzinfo=UTC)),
            _rec(model="B", started_at=datetime(2026, 7, 21, 10, tzinfo=UTC)),
        ]
    )
    assert _row(card, "A").merged == 1
    assert _row(card, "B").merged == 0


def test_review_only_pr_still_attributed_to_latest_run() -> None:
    """No dispatch run for the pr_url (out-of-band PR, #611): fall back to the
    latest-started run so the PR is not dropped from the scorecard."""
    from datetime import UTC, datetime

    card = scorecard.compute_model_scorecard(
        [
            _rec(
                model="A",
                run_kind="review",
                started_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
            ),
            _rec(
                model="B",
                run_kind="adversarial_review",
                started_at=datetime(2026, 7, 21, 10, tzinfo=UTC),
            ),
        ]
    )
    assert _row(card, "A").merged == 0
    assert _row(card, "B").merged == 1


def test_records_without_run_kind_are_treated_as_authoring() -> None:
    """Records are duck-typed; a record lacking ``run_kind`` must not crash and
    defaults to 'dispatch' (the run_store column default)."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    rec = SimpleNamespace(
        model="A",
        complexity="medium",
        estimated_cost_usd=0.0,
        pr_url="https://github.com/o/r/pull/9",
        pr_state="MERGED",
        review_cycles=None,
        started_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert _row(scorecard.compute_model_scorecard([rec]), "A").merged == 1


def test_non_authoring_kinds_cover_the_store_pr_subject_kinds() -> None:
    """Every PR-subject run must be excluded from scorecard authorship."""
    from kew.run_store import PR_SUBJECT_RUN_KINDS

    assert PR_SUBJECT_RUN_KINDS <= scorecard._NON_AUTHORING_RUN_KINDS


def test_scorecard_counts_non_success_representative_as_merged() -> None:
    """A run whose status is not 'success' but whose PR merged out-of-band
    (backfilled by the widened sync, #611) still counts as a merged PR for its
    model. The representative run need not be successful."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    rec = SimpleNamespace(
        model="lemonade/Qwen3-Coder-30B",
        complexity="medium",
        estimated_cost_usd=0.0,
        status="failure",
        pr_url="https://github.com/o/r/pull/604",
        pr_state="MERGED",
        pr_merged_at="2026-07-15T16:31:36Z",
        review_cycles=1,
        started_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    card = scorecard.compute_model_scorecard([rec])
    (row,) = card.rows
    assert row.model == "lemonade/Qwen3-Coder-30B"
    assert row.opened == 1
    assert row.merged == 1


def test_readiness_probe_runs_never_enter_the_scorecard() -> None:
    """I05A: a probe is operational spend, not a model's authored work."""
    probe = _rec(
        model="claude-opus-5",
        run_kind="readiness_probe",
        estimated_cost_usd=0.02,
        pr_url=None,
        complexity="",
    )
    work = _rec(model="claude-opus-5", estimated_cost_usd=3.0)

    card = scorecard.compute_model_scorecard([probe, work])

    (row,) = card.rows
    assert row.model == "claude-opus-5"
    assert row.runs == 1
    assert row.spend == 3.0
    assert list(row.by_complexity) == ["medium"]


def test_excluded_kinds_cover_the_store_readiness_probe_kind() -> None:
    """Drift guard: the store's probe run_kind must be excluded from the
    scorecard (scorecard.py cannot import run_store)."""
    from kew.run_store import READINESS_PROBE_RUN_KIND

    assert READINESS_PROBE_RUN_KIND in scorecard._EXCLUDED_RUN_KINDS


def test_probe_presence_leaves_the_scorecard_identical() -> None:
    """Task 18: a probe can neither improve nor degrade a model's numbers."""
    work = [
        _rec(model="claude-opus-5", estimated_cost_usd=3.0, pr_state="MERGED"),
        _rec(
            model="claude-opus-5",
            estimated_cost_usd=2.0,
            pr_url="https://github.com/o/r/pull/2",
            pr_state="CLOSED",
        ),
    ]
    probe = _rec(
        model="claude-opus-5",
        run_kind="readiness_probe",
        estimated_cost_usd=0.02,
        pr_url=None,
        complexity="",
    )

    assert scorecard.compute_model_scorecard([*work, probe]) == (
        scorecard.compute_model_scorecard(work)
    )


def test_acceptance_author_runs_do_not_count_as_authoring() -> None:
    """The acceptance author is not the PR's author (#2726).

    It writes the node's acceptance tests BEFORE the implementer starts, so it
    is the earliest run on the PR. Earliest-non-review attribution would hand
    it the merge, and with it the implementing model's $/merged-PR.
    """
    from datetime import UTC, datetime

    acceptance = _rec(
        model="test-writer",
        run_kind="acceptance_author",
        started_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
    )
    implementer = _rec(
        model="implementer",
        run_kind="dispatch",
        started_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
    )

    card = scorecard.compute_model_scorecard([acceptance, implementer])

    assert (_row(card, "implementer").opened, _row(card, "implementer").merged) == (
        1,
        1,
    )
    written = _row(card, "test-writer")
    assert (written.opened, written.merged) == (0, 0)
    # It still ran and still spent: the run count and the money stay its own.
    assert (written.runs, written.spend) == (1, 1.0)


def test_non_authoring_kinds_cover_the_store_acceptance_author_kind() -> None:
    """The scorecard's literal tracks the store's constant (#2726)."""
    from kew.run_store import ACCEPTANCE_AUTHOR_RUN_KIND, KNOWN_RUN_KINDS
    from kew.run_store_outcomes import NON_AUTHORING_RUN_KINDS

    assert ACCEPTANCE_AUTHOR_RUN_KIND in KNOWN_RUN_KINDS
    assert ACCEPTANCE_AUTHOR_RUN_KIND in scorecard._NON_AUTHORING_RUN_KINDS
    assert ACCEPTANCE_AUTHOR_RUN_KIND in NON_AUTHORING_RUN_KINDS
