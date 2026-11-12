"""W1.1 node 5d: shadow mode wired into a real dispatch.

Two things are under test, and the second matters more.

1. Off by default, and the config line is a real kill switch.
2. **A dispatch is unaffected by anything the shadow does.** Not "usually" —
   the comparison is given every way to fail and the push still happens. The
   whole feature is only safe to leave on if that is unconditionally true, and
   only useful if it is left on.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from kew.config import DispatchConfig
from kew.publication_shadow import (
    STATUS_DIVERGENT,
    STATUS_FAILED,
    STATUS_MATCH,
    STATUS_REFUSED,
    STATUS_REVIEW_REQUIRED,
    ShadowOutcome,
)
from kew.publication_shadow_hook import (
    EVIDENCE_DIR_NAME,
    _audit_payload,
    prune_evidence,
    resolve_shadow_inputs,
    run_shadow_comparison,
)

HOOK = "kew.publication_shadow_hook"

#: Comfortably past IN_FLIGHT_GRACE_SECONDS, so an aged directory is prunable.
DAY = 86_400

AUDIT_PAYLOAD_KEYS = {
    "schema_version",
    "item_id",
    "branch",
    "base_branch",
    "runtime",
    "image",
    "runner",
    "orchestrator_version",
    "orchestrator_commit",
    "orchestrator_dirty",
    "status",
    "base_sha",
    "legacy_tree",
    "broker_tree",
    "entries",
    "deletions",
    "rejection_count",
    "flag_count",
    "detail",
    "rejection_samples",
    "flag_samples",
    "evidence_basename",
}


def _config(tmp_path: Path, **publication: object) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "owner/repo"
    config.logging.dir = str(tmp_path / "logs")
    # Legacy shadow comparison exists only for the checkout escape hatch.
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    for key, value in publication.items():
        setattr(config.publication, key, value)
    return config


def _audit_context(tmp_path: Path) -> dict[str, object]:
    from kew.run_store import SqliteRunStore

    return {
        "store": SqliteRunStore(tmp_path / "dispatch.db"),
        "run_id": "run-1494",
        "issue_num": 1494,
        "item_id": "SFD-1494",
        "branch": "agent/SFD-1494",
        "base_branch": "trunk",
        "runner": "opencode",
        "provenance": {
            "orchestrator_version": "1.2.3",
            "orchestrator_commit": "abc123",
            "orchestrator_dirty": True,
        },
    }


@pytest.mark.parametrize(
    "outcome",
    [
        ShadowOutcome(status=STATUS_MATCH),
        ShadowOutcome(status=STATUS_DIVERGENT),
        ShadowOutcome(status=STATUS_REFUSED),
        ShadowOutcome(status=STATUS_REVIEW_REQUIRED),
        ShadowOutcome(status=STATUS_FAILED),
    ],
)
def test_terminal_outcomes_emit_uniform_bounded_audit_payload(
    tmp_path: Path, outcome: ShadowOutcome
):
    evidence = tmp_path / "private" / "retained-evidence"
    noisy = "secret\n\t" + ("x" * 600) + "\x00"
    samples = tuple(f"{i}\n{noisy}" for i in range(8))
    outcome = ShadowOutcome(
        **{
            **outcome.__dict__,
            "detail": noisy,
            "rejections": samples,
            "flags": samples,
            "evidence": evidence,
        }
    )
    context = _audit_context(tmp_path)
    store = context["store"]
    with (
        mock.patch(f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)),
        mock.patch(f"{HOOK}.compare_publication", return_value=outcome),
    ):
        run_shadow_comparison(
            _config(tmp_path, shadow=True),
            checkout_dir=tmp_path,
            slug="SFD-1494",
            image="registry/image:held",
            **context,
        )
        run_shadow_comparison(
            _config(tmp_path, shadow=True),
            checkout_dir=tmp_path,
            slug="SFD-1494",
            image="registry/image:held",
            **context,
        )
        # The exact append-once identity survives a fresh store opener.
        store.close()
        from kew.run_store import SqliteRunStore

        reopened = SqliteRunStore(tmp_path / "dispatch.db")
        rows = reopened.list_audit_events(event="publication_shadow_outcome")

    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == "run-1494"
    assert row.issue_num == 1494
    assert row.item_id == "SFD-1494"
    assert row.repo == "owner/repo"
    assert row.actor_type == "policy"
    assert row.actor_id == "publication-shadow"
    assert row.dedup_key == "publication-shadow:v1:run-1494"
    assert set(row.payload or {}) == AUDIT_PAYLOAD_KEYS
    payload = row.payload or {}
    assert payload["schema_version"] == 1
    assert payload["item_id"] == "SFD-1494"
    assert payload["branch"] == "agent/SFD-1494"
    assert payload["base_branch"] == "trunk"
    assert payload["runtime"] == "docker"
    assert payload["image"] == "registry/image:held"
    assert payload["runner"] == "opencode"
    assert payload["orchestrator_version"] == "1.2.3"
    assert payload["orchestrator_commit"] == "abc123"
    assert payload["orchestrator_dirty"] is True
    assert payload["status"] == outcome.status
    assert payload["base_sha"] == "a" * 40
    assert payload["legacy_tree"] == "b" * 40
    assert payload["rejection_count"] == 8
    assert payload["flag_count"] == 8
    assert len(payload["detail"]) <= 512
    assert "\n" not in payload["detail"] and "\x00" not in payload["detail"]
    assert len(payload["rejection_samples"]) == 5
    assert len(payload["flag_samples"]) == 5
    assert all(len(value) <= 256 for value in payload["rejection_samples"])
    assert payload["evidence_basename"] == "retained-evidence"
    assert str(tmp_path) not in str(payload)


def test_audit_payload_redacts_absolute_paths_from_failure_evidence() -> None:
    outcome = ShadowOutcome(
        status=STATUS_FAILED,
        detail=("missing /Users/someone/Projects/x/logs/secret-layout/manifest.json"),
        rejections=("refused /var/tmp/private-layout/rejection.json",),
    )

    payload = _audit_payload(
        outcome,
        item_id="SFD-1494",
        branch="agent/SFD-1494",
        base_branch="main",
        runtime="docker",
        image="image",
        runner="codex",
        provenance=None,
        base_sha=None,
        legacy_tree=None,
    )

    assert "/Users/" not in payload["detail"]
    assert "secret-layout" not in payload["detail"]
    assert "<path>/manifest.json" in payload["detail"]
    rejection = payload["rejection_samples"][0]
    assert "/var/" not in rejection
    assert "private-layout" not in rejection
    assert "<path>/rejection.json" in rejection


def test_shadow_event_precedes_report_and_prune(tmp_path: Path):
    order: list[str] = []
    context = _audit_context(tmp_path)
    store = context["store"]
    original_append = store.append_audit_event_once

    def append(**kwargs: object):
        order.append("audit")
        return original_append(**kwargs)

    with (
        mock.patch.object(store, "append_audit_event_once", side_effect=append),
        mock.patch(f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)),
        mock.patch(
            f"{HOOK}.compare_publication",
            return_value=ShadowOutcome(status=STATUS_MATCH),
        ),
        mock.patch(f"{HOOK}._report", side_effect=lambda *_: order.append("report")),
        mock.patch(
            f"{HOOK}.prune_evidence",
            side_effect=lambda *_a, **_k: order.append("prune"),
        ),
    ):
        run_shadow_comparison(
            _config(tmp_path, shadow=True),
            checkout_dir=tmp_path,
            slug="SFD-1494",
            image="image",
            **context,
        )

    assert order == ["audit", "report", "prune"]


def test_later_failure_retains_locally_resolved_legacy_tree(tmp_path: Path):
    context = _audit_context(tmp_path)
    store = context["store"]
    with (
        mock.patch(f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)),
        mock.patch(f"{HOOK}._publication_slug", side_effect=RuntimeError("later")),
    ):
        run_shadow_comparison(
            _config(tmp_path, shadow=True),
            checkout_dir=tmp_path,
            slug="SFD-1494",
            image="image",
            **context,
        )

    row = store.list_audit_events(event="publication_shadow_outcome")[0]
    assert row.payload["status"] == STATUS_FAILED
    assert row.payload["legacy_tree"] == "b" * 40


class TestOffUnlessAskedFor:
    def test_the_default_is_off(self):
        assert DispatchConfig().publication.shadow is False

    def test_off_does_not_even_look_at_the_checkout(self, tmp_path: Path):
        # Not merely "returns None": an operator who has not opted in should not
        # pay a git process, let alone a container.
        config = _config(tmp_path)
        with (
            mock.patch(f"{HOOK}.compare_publication") as compare,
            mock.patch(f"{HOOK}.resolve_shadow_inputs") as resolve,
        ):
            outcome = run_shadow_comparison(
                config, checkout_dir=tmp_path, slug="1", image="img"
            )

        assert outcome is None
        compare.assert_not_called()
        resolve.assert_not_called()

    def test_the_config_line_is_the_kill_switch(self, tmp_path: Path):
        config = _config(tmp_path, shadow=True)
        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                f"{HOOK}.compare_publication",
                return_value=ShadowOutcome(status=STATUS_MATCH),
            ) as compare,
        ):
            run_shadow_comparison(config, checkout_dir=tmp_path, slug="1", image="img")
        assert compare.call_count == 1

        config.publication.shadow = False
        with mock.patch(f"{HOOK}.compare_publication") as compare_off:
            run_shadow_comparison(config, checkout_dir=tmp_path, slug="1", image="img")
        compare_off.assert_not_called()

    def test_canonical_mode_emits_explicit_skip_evidence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(tmp_path, shadow=True, publish="canonical")
        context = _audit_context(tmp_path)
        store = context["store"]
        with (
            mock.patch(f"{HOOK}.resolve_shadow_inputs") as resolve,
            mock.patch(f"{HOOK}.compare_publication") as compare,
        ):
            outcome = run_shadow_comparison(
                config,
                checkout_dir=tmp_path,
                slug="SFD-1494",
                image="image",
                **context,
            )

        assert outcome is None
        resolve.assert_not_called()
        compare.assert_not_called()
        rows = store.list_audit_events(event="shadow.skipped")
        assert len(rows) == 1
        assert rows[0].payload == {
            "schema_version": 1,
            "reason": "canonical_publication",
            "branch": "agent/SFD-1494",
        }
        assert "shadow[SFD-1494]: skipped for canonical publication" in (
            capsys.readouterr().err
        )


class TestNothingItDoesCanBreakTheDispatch:
    @pytest.mark.parametrize(
        "boom",
        [
            RuntimeError("no merge base"),
            OSError("disk gone"),
            MemoryError("absurd"),
            RecursionError("deep"),
            ValueError("bad base"),
        ],
    )
    def test_a_failure_resolving_the_inputs_is_swallowed(
        self, tmp_path: Path, boom: BaseException
    ):
        config = _config(tmp_path, shadow=True)
        with mock.patch(f"{HOOK}.resolve_shadow_inputs", side_effect=boom):
            outcome = run_shadow_comparison(
                config, checkout_dir=tmp_path, slug="1", image="img"
            )

        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert type(boom).__name__ in outcome.detail

    def test_an_unresolvable_repo_is_a_finding_not_a_crash(self, tmp_path: Path):
        # The resolver is patched rather than left to the environment: whether
        # this passes must not depend on the machine's checkout having an
        # `origin`, which is what decided it before.
        config = _config(tmp_path, shadow=True)
        config.project.repo = ""
        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch("kew.dispatch_core._repo_slug_from_origin", return_value=None),
            mock.patch(f"{HOOK}.compare_publication") as compare,
        ):
            outcome = run_shadow_comparison(
                config, checkout_dir=tmp_path, slug="1", image="img"
            )

        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert "project.repo" in outcome.detail
        compare.assert_not_called()


class TestTheRemoteIsTheOperatorS:
    def test_an_unset_repo_falls_back_to_the_operators_origin(self, tmp_path: Path):
        """kew's own config leaves project.repo empty.

        Found by running shadow mode in situ against it: the feature refused to
        start, exactly as a real dispatch would have. The rest of kew already
        falls back to the operator checkout's `origin`, so this does too.
        """
        config = _config(tmp_path, shadow=True)
        config.project.repo = ""
        seen: dict[str, object] = {}

        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                "kew.dispatch_core._repo_slug_from_origin", return_value="octo/widgets"
            ),
            mock.patch(
                f"{HOOK}.compare_publication",
                side_effect=lambda **kw: (
                    seen.update(kw) or ShadowOutcome(status=STATUS_MATCH)
                ),
            ),
        ):
            run_shadow_comparison(config, checkout_dir=tmp_path, slug="1", image="img")

        assert seen["remote"] == "https://github.com/octo/widgets.git"

    def test_explicit_config_wins_over_origin(self, tmp_path: Path):
        # An operator who names the repo must not be silently overridden by
        # whatever `origin` happens to be.
        config = _config(tmp_path, shadow=True)
        config.project.repo = "explicit/repo"
        seen: dict[str, object] = {}

        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                "kew.dispatch_core._repo_slug_from_origin", return_value="other/repo"
            ) as origin,
            mock.patch(
                f"{HOOK}.compare_publication",
                side_effect=lambda **kw: (
                    seen.update(kw) or ShadowOutcome(status=STATUS_MATCH)
                ),
            ),
        ):
            run_shadow_comparison(config, checkout_dir=tmp_path, slug="1", image="img")

        assert seen["remote"] == "https://github.com/explicit/repo.git"
        origin.assert_not_called()

    def test_a_failure_pruning_evidence_is_swallowed(self, tmp_path: Path):
        # Housekeeping runs after a comparison has already produced a finding.
        # Losing the finding to a failed rmtree would be the worst possible
        # trade.
        config = _config(tmp_path, shadow=True)
        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                f"{HOOK}.compare_publication",
                return_value=ShadowOutcome(status=STATUS_MATCH, entries=3),
            ),
            mock.patch(f"{HOOK}.prune_evidence", side_effect=OSError("read-only")),
        ):
            outcome = run_shadow_comparison(
                config, checkout_dir=tmp_path, slug="1", image="img"
            )

        assert outcome is not None
        assert outcome.status == STATUS_MATCH

    def test_an_operator_interrupt_still_gets_through(self, tmp_path: Path):
        # The one exception, matching compare_publication's own contract: Ctrl-C
        # is the operator, not a shadow failure.
        config = _config(tmp_path, shadow=True)
        for boom in (KeyboardInterrupt(), SystemExit(1)):
            with (
                mock.patch(f"{HOOK}.resolve_shadow_inputs", side_effect=boom),
                pytest.raises(type(boom)),
            ):
                run_shadow_comparison(
                    config, checkout_dir=tmp_path, slug="1", image="img"
                )


class TestItReportsWhereTheEvidenceIs:
    def test_a_finding_prints_its_workspace(self, tmp_path: Path, capsys):
        evidence = tmp_path / "logs" / EVIDENCE_DIR_NAME / "kept"
        config = _config(tmp_path, shadow=True)
        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                f"{HOOK}.compare_publication",
                return_value=ShadowOutcome(
                    status=STATUS_DIVERGENT,
                    detail="trees differ",
                    evidence=evidence,
                    rejections=("x.txt: nope",),
                ),
            ),
        ):
            run_shadow_comparison(config, checkout_dir=tmp_path, slug="42", image="img")

        printed = capsys.readouterr()
        combined = printed.out + printed.err
        # Retained evidence nobody can find is the same as no evidence.
        assert str(evidence) in combined
        assert STATUS_DIVERGENT in combined
        assert "x.txt: nope" in combined


class TestEvidenceIsBounded:
    """Bounded, but never at the cost of a comparison that is still running.

    kew dispatches in parallel and every worker prunes this one directory, so
    "delete everything past the tenth newest" is not safe on its own.
    """

    @staticmethod
    def _aged(root: Path, name: str, age_seconds: float) -> Path:
        made = root / name
        made.mkdir()
        stamp = time.time() - age_seconds
        os.utime(made, (stamp, stamp))
        return made

    def test_stale_directories_past_the_limit_are_removed(self, tmp_path: Path):
        root = tmp_path / EVIDENCE_DIR_NAME
        root.mkdir()
        for stamp in ("20260101T000000", "20260102T000000", "20260103T000000"):
            self._aged(root, f"{stamp}-x", DAY)

        prune_evidence(root, keep=2)

        assert sorted(p.name for p in root.iterdir()) == [
            "20260102T000000-x",
            "20260103T000000-x",
        ]

    def test_a_comparison_still_running_is_never_pruned(self, tmp_path: Path):
        """The hazard, verified before it was fixed.

        A dispatch that started EARLIER sorts earlier, so it falls outside the
        keep window as soon as enough later comparisons finish — and it was
        deleted while still writing its clone. `protect` cannot cover it (it is
        older, not newer); recency can.
        """
        root = tmp_path / EVIDENCE_DIR_NAME
        root.mkdir()
        in_flight = self._aged(root, "20260810T170000000000-B", age_seconds=5)
        for n in range(10):
            self._aged(root, f"20260810T1701{n:02d}000000-x", DAY)
        current = self._aged(root, "20260810T170200000000-A", age_seconds=0)

        prune_evidence(root, keep=10, protect=current.name)

        assert in_flight.exists()
        assert current.exists()

    def test_the_current_workspace_survives_even_at_zero(self, tmp_path: Path):
        # keep=0 used to delete the very workspace whose path had just been
        # printed to the operator, making the report a lie.
        root = tmp_path / EVIDENCE_DIR_NAME
        root.mkdir()
        stale = self._aged(root, "20260101T000000-old", DAY)
        current = self._aged(root, "20260810T170300000000-C", age_seconds=0)

        prune_evidence(root, keep=0, protect=current.name)

        assert current.exists()
        assert not stale.exists()

    def test_protect_covers_a_comparison_slower_than_the_grace_period(
        self, tmp_path: Path
    ):
        """`protect` is deterministic where the grace period is a heuristic.

        A comparison that took longer than IN_FLIGHT_GRACE_SECONDS — a large
        fetch, a slow container — finishes with a workspace whose mtime is
        already past the cutoff. Recency would no longer defend it, so the run
        naming its own workspace is what keeps the reported evidence path true.
        """
        root = tmp_path / EVIDENCE_DIR_NAME
        root.mkdir()
        for n in range(3):
            self._aged(root, f"20260810T1701{n:02d}000000-x", DAY)
        slow = self._aged(root, "20260810T170000000000-SLOW", DAY)

        prune_evidence(root, keep=1, protect=slow.name)

        assert slow.exists(), "the current run's own workspace was pruned"

    def test_zero_removes_everything_stale(self, tmp_path: Path):
        root = tmp_path / EVIDENCE_DIR_NAME
        root.mkdir()
        self._aged(root, "20260101T000000-x", DAY)

        prune_evidence(root, keep=0)

        assert list(root.iterdir()) == []

    def test_a_missing_root_is_not_an_error(self, tmp_path: Path):
        prune_evidence(tmp_path / "never-created", keep=5)

    def test_a_negative_keep_is_refused_by_config(self):
        config = DispatchConfig()
        # Assigned OUTSIDE the block: pydantic does not validate on assignment
        # here, so leaving it inside would let the test pass on the assignment
        # raising and never exercise reconstruction at all.
        config.publication.shadow_evidence = -1

        with pytest.raises(ValueError, match="shadow_evidence"):
            DispatchConfig.model_validate(config.model_dump())

    def test_the_workspace_name_sorts_by_time(self, tmp_path: Path):
        # Pruning is newest-first BY NAME, so the timestamp has to lead and be
        # fixed-width or the ordering is decorative.
        config = _config(tmp_path, shadow=True)
        captured: dict[str, Path] = {}

        def capture(**kwargs: object) -> ShadowOutcome:
            captured["workspace"] = kwargs["workspace"]  # type: ignore[assignment]
            return ShadowOutcome(status=STATUS_MATCH)

        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(f"{HOOK}.compare_publication", side_effect=capture),
        ):
            run_shadow_comparison(
                config,
                checkout_dir=tmp_path,
                slug="42",
                image="img",
                now=datetime(2026, 8, 10, 17, 5, 3, tzinfo=UTC),
            )

        name = captured["workspace"].name
        assert name.startswith("20260810T170503")
        assert name.endswith("-42")

    def test_the_run_protects_its_own_workspace(self, tmp_path: Path):
        # The call site must PASS protect; a prune that does not know which
        # workspace is current cannot avoid deleting it.
        config = _config(tmp_path, shadow=True)
        seen: dict[str, object] = {}

        with (
            mock.patch(
                f"{HOOK}.resolve_shadow_inputs", return_value=("a" * 40, "b" * 40)
            ),
            mock.patch(
                f"{HOOK}.compare_publication",
                return_value=ShadowOutcome(status=STATUS_MATCH),
            ),
            mock.patch(
                f"{HOOK}.prune_evidence", side_effect=lambda *a, **k: seen.update(k)
            ),
        ):
            run_shadow_comparison(
                config,
                checkout_dir=tmp_path,
                slug="42",
                image="img",
                now=datetime(2026, 8, 10, 17, 5, 3, tzinfo=UTC),
            )

        assert seen.get("protect") == "20260810T170503000000-42"


class TestTheInputsComeFromTheRightPlace:
    def test_base_is_the_merge_base_with_the_remote_branch(self, tmp_path: Path):
        """A local base could name a commit the remote has never seen, and the
        canonical repo could not then fetch it."""
        repo = tmp_path / "checkout"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "-q", ".")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (repo / "a.txt").write_text("a\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        # A remote-tracking ref is what resolve_shadow_inputs names.
        git("update-ref", "refs/remotes/origin/main", base)
        (repo / "b.txt").write_text("b\n")
        git("add", "-A")
        git("commit", "-qm", "work")

        import shutil

        from kew import subprocess_utils

        resolved = shutil.which("git")
        assert resolved
        with mock.patch.dict(subprocess_utils.TOOL_PATHS, {"git": resolved}):
            base_sha, legacy_tree = resolve_shadow_inputs(repo, "main")

        assert base_sha == base
        assert legacy_tree == git("rev-parse", "HEAD^{tree}")
        # Not HEAD's own tree at base: the point is the pair describes the SAME
        # diff the producer will extract.
        assert legacy_tree != git("rev-parse", f"{base}^{{tree}}")
