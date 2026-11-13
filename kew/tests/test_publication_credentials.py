"""Credential-exposed contained output never crosses publication boundaries."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest.mock import call as mock_call

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

import kew.readiness as readiness
from kew.audit import Actor, continuation_evidence, emit_continuation_evidence
from kew.audit_identity import continuation_evidence_identity
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    ensure_publication_credentials,
    publication_producer_for_head,
    require_normalized_job_credentials,
    require_publication_credentials,
    require_publication_launch_credentials,
)
from kew.publication_filter import FilterVerdict, Flag, ReviewRequired
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore


@pytest.mark.parametrize("rescue", [False, True], ids=["push", "rescue"])
def test_dispatch_policy_seam_intercepts_each_credential_recovery_attempt(
    store: SqliteRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rescue: bool
) -> None:
    from kew import dispatch_core, publication_credentials
    from kew.config import DispatchConfig

    policy = Mock(
        side_effect=[
            publication_credentials.IssuedKeyPublicationHeld(
                "retry required", run_id="producer", key_hash="a" * 64
            ),
            CredentialPublicationRefused("issued_key_evidence_missing"),
        ]
    )
    recover = Mock()
    publish, commit = Mock(), Mock()
    monkeypatch.setattr(dispatch_core, "require_publication_credentials", policy)
    monkeypatch.setattr(publication_credentials, "reconcile_pending_key", recover)
    monkeypatch.setattr(dispatch_core, "publish_checkout", publish)
    monkeypatch.setattr(dispatch_core, "_commit_all_if_dirty", commit)
    entry = (
        partial(
            dispatch_core._rescue_incomplete_run,
            tmp_path,
            "agent/7",
            7,
            "test",
            DispatchConfig(),
            item_id="7",
        )
        if rescue
        else partial(dispatch_core._git_push_branch, "agent/7", tmp_path)
    )
    with pytest.raises(CredentialPublicationRefused) as raised:
        entry(store=store, run_id="run")
    assert raised.value.reason == "issued_key_evidence_missing"
    assert (
        policy.call_args_list
        == [
            mock_call(
                store,
                run_id="run",
                producer_run_id=None,
                attempt_number=None,
                published_head=None,
            )
        ]
        * 2
    )
    recover.assert_called_once_with(store, "producer", "a" * 64)
    publish.assert_not_called()
    commit.assert_not_called()


#: A head no scan was recorded for: the gate must refuse to publish it.
_UNSCANNED_HEAD = "e" * 40


def _run(
    store: SqliteRunStore,
    run_id: str,
    *,
    attempts: int = 1,
    status: str = "awaiting_publication",
    container_name: str | None = None,
    branch: str = "agent/issue-7",
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="7",
            branch=branch,
            status=status,  # type: ignore[arg-type]
            exit_code=0,
            started_at=datetime(2026, 9, 5, tzinfo=UTC),
            attempt_number=attempts,
            container_name=container_name,
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )


def _profile(
    store: SqliteRunStore,
    run_id: str,
    attempt: int,
    *,
    containment: Containment,
    auth_path: str | None,
) -> None:
    boundary = RealizedLaunchBoundary(
        containment=containment,
        image_ref="kew-agent:latest" if containment is Containment.CONTAINED else None,
        image_id=(
            "sha256:" + "a" * 64 if containment is Containment.CONTAINED else None
        ),
        auth_path=auth_path,
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=attempt,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=boundary,
        )
    )


def test_containment_refusal_exclusion_requires_no_launch_evidence(store, monkeypatch):
    from kew import publication_credentials as credentials

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    now = datetime(2026, 9, 15, tzinfo=UTC)
    refused = RunRecord(
        run_id="refusal",
        issue_num=99,
        item_id="99",
        branch="agent/issue-7",
        status="refused",
        error_category="containment_refused",
        exit_code=2,
        started_at=now,
        finished_at=now,
    )
    store.save(refused, update_ownership=True)
    assert (
        publication_producer_for_head(store, branch=refused.branch, head_sha="f" * 40)
        == "producer"
    )

    # Conflicting launch or terminal evidence must preserve branch protection.
    for changes in (
        {"status": "failure"},
        {"error_category": "launch_failed"},
        {"attempt_number": 2},
        {"finished_at": None},
        {"pgid": 123},
        {"container_name": "kew-worker"},
        {"session_id": "session"},
        {"log_file": Path("run.log")},
        {"pr_url": "https://example.test/pull/7"},
        {"total_tokens": 1},
        {"duration_seconds": 1.0},
        {"estimated_cost_usd": 0.1},
    ):
        row = replace(refused, **changes)
        # Each case starts fresh; main preserves the highest attempt number.
        store.delete(refused.run_id)
        store.save(row, update_ownership=True)
        assert not credentials.prelaunch_containment_refusal(store, row), changes
        assert credentials._single_branch_item(store, branch=row.branch) is None

    store.delete(refused.run_id)
    store.save(refused, update_ownership=True)
    for method in ("list_route_profiles", "continuation_origins"):
        with monkeypatch.context() as patcher:
            original = getattr(store, method)

            def unreadable(run_id, *args, original=original, **kwargs):
                if run_id == refused.run_id:
                    raise OSError("injected unreadable launch evidence")
                return original(run_id, *args, **kwargs)

            patcher.setattr(store, method, unreadable)
            with pytest.raises(CredentialPublicationRefused) as error:
                publication_producer_for_head(
                    store, branch=refused.branch, head_sha="f" * 40
                )
            assert error.value.reason == "publication_evidence_unavailable"

    _profile(
        store,
        refused.run_id,
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    assert not credentials.prelaunch_containment_refusal(store, refused)
    with pytest.raises(CredentialPublicationRefused) as error:
        publication_producer_for_head(store, branch=refused.branch, head_sha="f" * 40)
    assert error.value.reason == "publication_lineage_ambiguous"


def _prelaunch_refusal(store, run_id):
    now = datetime(2026, 9, 5, tzinfo=UTC)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=99,
            item_id="",
            branch="agent/issue-7",
            status="refused",
            error_category="containment_refused",
            exit_code=2,
            started_at=now,
            finished_at=now,
        )
    )


@pytest.mark.parametrize("lookup", ["producer", "remediation"])
def test_branch_history_limit_counts_only_potential_producers(store, lookup):
    from kew.review import _remediation_row_branch

    def admitted(newest):
        if lookup == "producer":
            assert (
                publication_producer_for_head(
                    store, branch="agent/issue-7", head_sha="f" * 40
                )
                == newest
            )
        else:
            assert (
                _remediation_row_branch(store, "7", "agent/issue-7") == "agent/issue-7"
            )

    # Equal timestamps exercise the rowid tie-breaker across mixed pages.
    for index in range(31):
        _run(store, f"producer-{index}", status="success")
        _profile(
            store,
            f"producer-{index}",
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )
        for suffix in ("a", "b"):
            _prelaunch_refusal(store, f"refusal-{index}-{suffix}")
    admitted("producer-30")

    _run(store, "producer-31", status="success")
    if lookup == "producer":
        admitted("producer-31")  # A producer lookup accepts exactly 32 rows.
    else:
        with pytest.raises(CredentialPublicationRefused) as error:
            admitted("producer-31")  # Remediation must reserve one branch row.
        assert error.value.reason == "publication_lineage_too_deep"

    _run(store, "producer-32", status="success")
    with pytest.raises(CredentialPublicationRefused) as error:
        admitted("producer-32")
    assert error.value.reason == "publication_lineage_too_deep"

    # The same limit applies to 33 producers without any refusal rows.
    for index in range(31):
        for suffix in ("a", "b"):
            assert store.delete(f"refusal-{index}-{suffix}")
    with pytest.raises(CredentialPublicationRefused) as error:
        admitted("producer-32")
    assert error.value.reason == "publication_lineage_too_deep"


def test_refusal_pages_preserve_older_protected_producer(store):
    _run(store, "older-producer", status="success")
    _profile(
        store,
        "older-producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _run(store, "newer-producer", status="success")
    _profile(
        store,
        "newer-producer",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    for index in range(66):
        _prelaunch_refusal(store, f"refusal-{index}")

    with pytest.raises(CredentialPublicationRefused) as error:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
    assert error.value.reason == "publication_lineage_ambiguous"


@pytest.mark.parametrize("lookup", ["producer", "remediation"])
def test_unreadable_later_branch_page_refuses(store, monkeypatch, lookup):
    from kew.review import _remediation_row_branch

    _run(store, "producer", status="success")
    for index in range(33):
        _prelaunch_refusal(store, f"refusal-{index}")
    read_page = store.list_runs_by_branch_after

    def unreadable(branch, **kwargs):
        if kwargs.get("after") is not None:
            raise OSError("injected failure reading the second branch page")
        return read_page(branch, **kwargs)

    monkeypatch.setattr(store, "list_runs_by_branch_after", unreadable)
    with pytest.raises(CredentialPublicationRefused) as error:
        if lookup == "producer":
            publication_producer_for_head(
                store, branch="agent/issue-7", head_sha="f" * 40
            )
        else:
            _remediation_row_branch(store, "7", "agent/issue-7")
    assert error.value.reason == "publication_evidence_unavailable"


def _history_lookup(store, lookup):
    from kew.review import _remediation_row_branch

    if lookup == "producer":
        return publication_producer_for_head(
            store, branch="agent/issue-7", head_sha="f" * 40
        )
    return _remediation_row_branch(store, "7", "agent/issue-7")


def _history_producer(store, index):
    run_id = f"producer-{index}"
    _run(store, run_id, status="success")
    _profile(store, run_id, 1, containment=Containment.NATIVE, auth_path="cli-login")


@pytest.mark.parametrize("lookup", ["producer", "remediation"])
def test_branch_history_shared_connection_delete_preserves_oldest(
    store, monkeypatch, lookup
):
    _run(store, "producer-0", status="success")
    _profile(
        store,
        "producer-0",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    for index in range(1, 32):
        _history_producer(store, index)
    for index in range(2):
        _prelaunch_refusal(store, f"refusal-{index}")
    reason = (
        "publication_lineage_ambiguous"
        if lookup == "producer"
        else "publication_lineage_too_deep"
    )
    # The control proves that the oldest producer protects this branch.
    with pytest.raises(CredentialPublicationRefused) as error:
        _history_lookup(store, lookup)
    assert error.value.reason == reason
    read_page = store.list_runs_by_branch_after
    scans = []

    def concurrent_delete(branch, **kwargs):
        page, cursor = read_page(branch, **kwargs)
        if kwargs.get("after") is None:
            scans.append([])
        # Publication checks ownership before its legacy producer scan.
        if len(scans) == (2 if lookup == "producer" else 1) and not scans[-1]:
            assert store.delete("refusal-1")
        scans[-1].append(page)
        return page, cursor

    monkeypatch.setattr(store, "list_runs_by_branch_after", concurrent_delete)
    with pytest.raises(CredentialPublicationRefused) as error:
        _history_lookup(store, lookup)
    assert error.value.reason == reason
    _assert_history_pages(scans, 32)


def _history_page_producers(pages):
    return [row.run_id for page in pages for row in page if row.status == "success"]


def _assert_history_pages(scans, count, *, two_pages=False, unique_only=False):
    for pages in scans:
        ids = _history_page_producers(pages)
        if unique_only:
            assert len(ids) == len(set(ids)) == count
        else:
            assert ids == [f"producer-{index}" for index in reversed(range(count))]
        if two_pages:
            assert len(pages) == 2


@pytest.fixture
def shared_history_store(tmp_path):
    with SqliteRunStore(str(tmp_path / "shared.db"), check_same_thread=False) as store:
        yield store


@pytest.mark.parametrize(
    ("lookup", "producer_count"), [("producer", 32), ("remediation", 31)]
)
def test_branch_history_shared_connection_save_survives_read(
    shared_history_store, monkeypatch, lookup, producer_count
):
    store = shared_history_store
    for index in range(producer_count):
        _history_producer(store, index)
    for index in range(2):
        _prelaunch_refusal(store, f"refusal-{index}")
    first_page = threading.Event()
    inserted = threading.Event()
    read_finished = threading.Event()
    saved = threading.Event()
    errors = []
    connection = store._conn
    scans = []
    read_page = store.list_runs_by_branch_after

    class PausedCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            # save() has inserted its row, but cannot commit until reading ends.
            inserted.set()
            assert read_finished.wait(10), "reader did not finish"
            connection.commit()

    def paused_page(branch, **kwargs):
        page, cursor = read_page(branch, **kwargs)
        if kwargs.get("after") is None:
            scans.append([])
        if len(scans) == (2 if lookup == "producer" else 1) and not scans[-1]:
            first_page.set()
            assert inserted.wait(10), "writer did not insert its run"
        scans[-1].append(page)
        return page, cursor

    def writer():
        try:
            assert first_page.wait(10), "reader did not read its first page"
            _run(store, "concurrent-producer", status="success")
            saved.set()
        except BaseException as exc:
            errors.append(exc)
            inserted.set()

    monkeypatch.setattr(store, "_conn", PausedCommit())
    monkeypatch.setattr(store, "list_runs_by_branch_after", paused_page)
    thread = threading.Thread(target=writer, name="shared-connection-writer")
    thread.start()
    try:
        result = _history_lookup(store, lookup)
    finally:
        read_finished.set()
        thread.join(timeout=10)
    assert not thread.is_alive(), "writer did not finish"
    assert not errors, errors
    assert saved.is_set(), "save() did not return successfully"
    assert store.get("concurrent-producer") is not None
    expected = (
        f"producer-{producer_count - 1}" if lookup == "producer" else "agent/issue-7"
    )
    assert result == expected
    _assert_history_pages(scans, producer_count, two_pages=True)
    assert not connection.in_transaction


@pytest.mark.parametrize(
    ("lookup", "producer_count"),
    [("producer", 31), ("remediation", 31), ("producer", 32)],
)
def test_branch_history_concurrent_refusal_insert(
    store, tmp_path, monkeypatch, lookup, producer_count
):
    for index in range(producer_count):
        _history_producer(store, index)
    for index in range(2):
        _prelaunch_refusal(store, f"refusal-{index}")

    read_page = store.list_runs_by_branch_after
    scans = []
    # A separate WAL connection commits between page reads on the reader.
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as writer:

        def concurrent_insert(branch, **kwargs):
            page, cursor = read_page(branch, **kwargs)
            if kwargs.get("after") is None:
                scans.append([])
                _prelaunch_refusal(writer, f"concurrent-refusal-{len(scans)}")
            scans[-1].append(page)
            return page, cursor

        with monkeypatch.context() as patcher:
            patcher.setattr(store, "list_runs_by_branch_after", concurrent_insert)
            expected = (
                f"producer-{producer_count - 1}"
                if lookup == "producer"
                else "agent/issue-7"
            )
            assert _history_lookup(store, lookup) == expected

    assert scans
    _assert_history_pages(scans, producer_count, two_pages=True, unique_only=True)
    assert store.get("concurrent-refusal-1") is not None
    assert not store._conn.in_transaction

    # Keyset pagination and deduplication must retain the distinct-run limit.
    for index in range(producer_count, 33):
        _history_producer(store, index)
    with pytest.raises(CredentialPublicationRefused) as error:
        _history_lookup(store, lookup)
    assert error.value.reason == "publication_lineage_too_deep"


@pytest.mark.parametrize(
    ("lookup", "producer_count"), [("producer", 32), ("remediation", 31)]
)
def test_branch_history_duplicate_producer_counts_once(
    store, monkeypatch, lookup, producer_count
):
    for index in range(producer_count):
        _history_producer(store, index)
    for index in range(2):
        _prelaunch_refusal(store, f"refusal-{index}")
    read_page = store.list_runs_by_branch_after

    def duplicate_producer(branch, **kwargs):
        page, cursor = read_page(branch, **kwargs)
        if kwargs.get("after") is not None:
            # Model a store returning the same producer across two pages.
            duplicate = store.get("producer-1")
            assert duplicate is not None
            page.append(duplicate)
        return page, cursor

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "list_runs_by_branch_after", duplicate_producer)
        expected = (
            f"producer-{producer_count - 1}"
            if lookup == "producer"
            else "agent/issue-7"
        )
        assert _history_lookup(store, lookup) == expected

    for index in range(producer_count, 33):
        _history_producer(store, index)
    with pytest.raises(CredentialPublicationRefused) as error:
        _history_lookup(store, lookup)
    assert error.value.reason == "publication_lineage_too_deep"


@pytest.mark.parametrize("lookup", ["producer", "remediation"])
@pytest.mark.parametrize("failure", ["enter", "exit"])
def test_branch_history_does_not_use_snapshot(store, monkeypatch, lookup, failure):
    _history_producer(store, 0)

    @contextmanager
    def unavailable_snapshot():
        if failure == "enter":
            raise OSError("injected snapshot entry failure")
        yield
        raise OSError("injected snapshot exit failure")

    monkeypatch.setattr(store, "read_snapshot", unavailable_snapshot)
    expected = "producer-0" if lookup == "producer" else "agent/issue-7"
    assert _history_lookup(store, lookup) == expected


def _sidecar_packet():
    packet_type = getattr(readiness, "SidecarPacketEvidence", None)
    outcome_type = getattr(readiness, "TopologyProbeResult", None)
    packet_status = getattr(readiness, "PacketProbeStatus", None)
    assert packet_type is not None
    assert outcome_type is not None
    assert packet_status is not None
    return packet_type(
        proxy_health=packet_status.REACHABLE,
        proxy_route=packet_status.REACHABLE,
        denials=tuple(
            outcome_type(
                name=name,
                positive_control=packet_status.REACHABLE,
                worker=packet_status.DENIED,
                passed=True,
            )
            for name in readiness.SIDECAR_PACKET_DENIALS
        ),
    )


def _sidecar_profile(
    store: SqliteRunStore, run_id: str, *, image_id: str = "sha256:" + "a" * 64
) -> object:
    from kew.inference_transport import ResolvedInferenceTransport

    profile = compose_realized_profile(
        run_id=run_id,
        attempt_number=1,
        route=compose_route_profile(
            runner="claude-code",
            requested_model="claude-opus-5",
            transport=ResolvedInferenceTransport(
                mode="switchyard",
                requested_model="claude-opus-5",
                model="claude-opus-5",
                config_sha256="b" * 64,
                endpoint="http://192.0.2.10:4000",
                route="synthetic/route",
                sidecar=True,
            ),
        ),
        boundary=RealizedLaunchBoundary(
            containment=Containment.CONTAINED,
            image_ref="kew-agent:latest",
            image_id=image_id,
            endpoint="http://192.0.2.10:4000",
            auth_path="synthetic:per-attempt",
        ),
    )
    store.record_route_profile(profile)
    return profile


def _sidecar_evidence(
    run_id: str,
    profile: object,
    *,
    stage: str,
    worker_image_id: str = "sha256:" + "a" * 64,
):
    evidence_type = getattr(readiness, "SidecarTopologyEvidence", None)
    assert evidence_type is not None
    return evidence_type(
        schema_version=1,
        stage=stage,
        run_id=run_id,
        attempt_number=1,
        profile_fingerprint=profile.fingerprint,
        route="synthetic/route",
        config_sha256="b" * 64,
        owner=("c" if run_id == "producer" else "d") * 32,
        worker_image_id=worker_image_id,
        proxy_image_id="sha256:" + "e" * 64,
        network_id=("f" if run_id == "producer" else "1") * 64,
        egress_network_id="2" * 64,
        proxy_id="3" * 64,
        worker_id="4" * 64,
        config_helper_id="5" * 64,
        filter_helper_id="6" * 64,
        config_storage=f"kew-switchyard-{run_id}-config",
        endpoint="http://192.0.2.10:4000",
        observed_version="0.1.0",
        packet=_sidecar_packet(),
        filter_verdict="passed",
        cleanup_verdict="passed" if stage == "terminal" else "pending",
    )


def _append_complete_sidecar_evidence(
    store: SqliteRunStore, run_id: str, profile: object
) -> None:
    import kew.audit as audit

    append = getattr(audit, "append_sidecar_evidence", None)
    assert append is not None
    for stage in ("admission", "terminal"):
        append(
            store,
            _sidecar_evidence(run_id, profile, stage=stage),
            actor=Actor("policy", "sidecar-supervisor"),
            issue_num=7,
            item_id="7",
        )


def _stored_native_profile(run_id: str) -> SimpleNamespace:
    profile = compose_realized_profile(
        run_id=run_id,
        attempt_number=1,
        route=compose_route_profile(
            runner="claude-code", requested_model="claude-opus-5"
        ),
        boundary=RealizedLaunchBoundary(
            containment=Containment.NATIVE,
            auth_path="cli-login",
        ),
    )
    return SimpleNamespace(
        attempt_number=1,
        profile_json=profile.to_json(),
        fingerprint=profile.fingerprint,
        containment="native",
        auth_path="cli-login",
    )


def _attest_claim(
    store: SqliteRunStore,
    *,
    worktree: Path,
    continuation_run_id: str,
) -> None:
    record = store.get_continuation(
        "epic:7", "7", "producer", "agent/issue-7", worktree
    )
    assert record is not None
    emit_continuation_evidence(
        store,
        "continuation_claimed",
        actor=Actor("policy", "test"),
        phase="claim",
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=continuation_run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation,
        state=record.state,
        outcome="continuing",
        run_id=continuation_run_id,
        issue_num=7,
    )


def _claim_and_requeue_without_evidence(
    store: SqliteRunStore,
    *,
    worktree: Path,
    original_run_id: str,
    continuation_run_id: str,
    item_id: str = "7",
    branch: str = "agent/issue-7",
) -> None:
    store.record_continuation(
        "epic:7",
        item_id,
        original_run_id,
        branch,
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    # Preserve coverage of ledgers written before atomic claim evidence.
    with patch.object(store, "_attest_continuation_claim"):
        assert (
            store.claim_continuation_under_cap(
                "epic:7",
                item_id,
                original_run_id,
                branch,
                worktree,
                continuation_run_id,
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:7",
                    item_id,
                    original_run_id,
                    branch,
                    worktree,
                    continuation_run_id,
                ),
            )
            == "claimed"
        )
    record = store.get_continuation(
        "epic:7", item_id, original_run_id, branch, worktree
    )
    assert record is not None
    assert store.mark_continuation_awaiting(
        "epic:7",
        item_id,
        original_run_id,
        branch,
        worktree,
        record.generation,
        continuation_run_id,
    )


class _SafeProjectionStore:
    """Expose safe-looking columns beside retained canonical profile JSON."""

    def __init__(self, store: SqliteRunStore, unknown_run_ids: set[str]) -> None:
        self._store = store
        self._unknown_run_ids = unknown_run_ids
        self.jobs: dict[str, object] = {}

    def get(self, run_id: str):
        return self._store.get(run_id)

    def list_route_profiles(self, run_id: str):
        profiles = self._store.list_route_profiles(run_id)
        if run_id not in self._unknown_run_ids:
            return profiles
        return [
            replace(profile, containment="native", auth_path="cli-login")
            for profile in profiles
        ]

    def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
        return self._store.continuation_origins(run_id, limit=limit)

    def get_normalized_job(self, job_id: str) -> object | None:
        return self.jobs.get(job_id)


@pytest.fixture
def store(tmp_path: Path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def test_verified_sidecar_lineage_allows_publication(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    for run_id in ("producer", "continuation"):
        _run(store, run_id)
        profile = _sidecar_profile(store, run_id)
        _append_complete_sidecar_evidence(store, run_id, profile)

    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:7",
        "7",
        "producer",
        "agent/issue-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            "agent/issue-7",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:7",
                "7",
                "producer",
                "agent/issue-7",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    _attest_claim(store, worktree=worktree, continuation_run_id="continuation")

    require_publication_credentials(store, run_id="continuation")


def test_stale_sidecar_identity_blocks_all_publication_effects(
    store: SqliteRunStore,
) -> None:
    import kew.audit as audit

    _run(store, "stale-sidecar")
    profile = _sidecar_profile(store, "stale-sidecar")
    for stage in ("admission", "terminal"):
        audit.append_sidecar_evidence(
            store,
            _sidecar_evidence(
                "stale-sidecar",
                profile,
                stage=stage,
                worker_image_id="sha256:" + "9" * 64,
            ),
            actor=Actor("policy", "sidecar-supervisor"),
            issue_num=7,
            item_id="7",
        )

    job = SimpleNamespace(
        job_id="job-stale",
        run_id="stale-sidecar",
        attempt_number=1,
        predecessor_job_id=None,
    )
    effects: list[str] = []
    guarded_effects = (
        lambda: (
            require_publication_credentials(store, run_id="stale-sidecar"),
            effects.append("direct"),
        ),
        lambda: (
            require_normalized_job_credentials(store, job),
            effects.append("normalized"),
        ),
    )

    for guarded_effect in guarded_effects:
        with pytest.raises(CredentialPublicationRefused) as raised:
            guarded_effect()
        assert raised.value.reason == "sidecar_identity_stale"

    assert effects == []


def test_current_sidecar_admission_does_not_require_future_cleanup(
    store: SqliteRunStore,
) -> None:
    import kew.audit as audit

    _run(store, "current-sidecar", status="pending")
    profile = _sidecar_profile(store, "current-sidecar")
    audit.append_sidecar_evidence(
        store,
        _sidecar_evidence("current-sidecar", profile, stage="admission"),
        actor=Actor("policy", "sidecar-supervisor"),
        issue_num=7,
        item_id="7",
    )

    require_publication_launch_credentials(
        store,
        run_id="current-sidecar",
        run_kind="dispatch",
        attempt_number=1,
    )
    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="current-sidecar")

    assert raised.value.reason == "sidecar_terminal_evidence_missing"


def test_unavailable_sidecar_packet_matrix_refuses_agent_admission(
    store: SqliteRunStore,
) -> None:
    import kew.audit as audit

    _run(store, "unproved-sidecar", status="pending")
    profile = _sidecar_profile(store, "unproved-sidecar")
    packet = replace(
        _sidecar_packet(),
        proxy_health=readiness.PacketProbeStatus.UNAVAILABLE,
    )
    evidence = replace(
        _sidecar_evidence("unproved-sidecar", profile, stage="admission"),
        packet=packet,
    )
    audit.append_sidecar_evidence(
        store,
        evidence,
        actor=Actor("policy", "sidecar-supervisor"),
        issue_num=7,
        item_id="7",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_launch_credentials(
            store,
            run_id="unproved-sidecar",
            run_kind="dispatch",
            attempt_number=1,
        )

    assert raised.value.reason == "sidecar_packet_evidence_missing"


def test_conflicting_sidecar_events_refuse_publication(
    store: SqliteRunStore,
) -> None:
    import kew.audit as audit

    _run(store, "conflicting-sidecar")
    profile = _sidecar_profile(store, "conflicting-sidecar")
    admission = _sidecar_evidence("conflicting-sidecar", profile, stage="admission")
    for evidence in (admission, replace(admission, proxy_id="7" * 64)):
        audit.append_sidecar_evidence(
            store,
            evidence,
            actor=Actor("policy", "sidecar-supervisor"),
            issue_num=7,
            item_id="7",
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="conflicting-sidecar")

    assert raised.value.reason == "sidecar_identity_stale"


@pytest.mark.parametrize(
    "payload",
    [
        b"synthetic-credential-value",
        base64.b64encode(b"synthetic-credential-value"),
        b"synthetic-credential-" + b"value",
        bytes(value ^ 0x5A for value in b"synthetic-credential-value"),
        b"\x00synthetic-credential-value\xff",
    ],
    ids=["literal", "base64", "split", "transformed", "binary"],
)
def test_every_output_encoding_is_refused_by_exposure_lineage(
    store: SqliteRunStore, tmp_path: Path, payload: bytes
) -> None:
    output = tmp_path / "worker-output.bin"
    output.write_bytes(payload)
    _run(store, "exposed")
    _profile(
        store,
        "exposed",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="exposed", published_head=_UNSCANNED_HEAD
        )

    assert raised.value.category == CREDENTIAL_PUBLICATION_REFUSAL
    assert raised.value.reason == "credential_scan_missing"
    assert payload not in str(raised.value).encode()


def test_attempt_one_exposure_survives_a_later_safe_looking_attempt(
    store: SqliteRunStore,
) -> None:
    _run(store, "retried", attempts=2)
    _profile(
        store,
        "retried",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _profile(
        store,
        "retried",
        2,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )

    with pytest.raises(CredentialPublicationRefused, match="not releasable"):
        require_publication_credentials(
            store, run_id="retried", published_head=_UNSCANNED_HEAD
        )


def test_known_contained_legacy_run_without_profiles_fails_closed(
    store: SqliteRunStore,
) -> None:
    _run(store, "legacy", container_name="kew-7")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="legacy")

    assert raised.value.reason == "contained_profile_missing"


def test_known_contained_attempt_without_auth_evidence_fails_closed(
    store: SqliteRunStore,
) -> None:
    _run(store, "missing-auth")
    _profile(
        store,
        "missing-auth",
        1,
        containment=Containment.CONTAINED,
        auth_path=None,
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="missing-auth")

    assert raised.value.reason == "contained_auth_evidence_missing"


def test_a_missing_profile_for_a_protected_retry_fails_closed(
    store: SqliteRunStore,
) -> None:
    _run(store, "profile-gap", attempts=2)
    _profile(
        store,
        "profile-gap",
        1,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="profile-gap")

    assert raised.value.reason == "contained_profile_missing"


def test_requested_publication_attempt_requires_its_own_profile(
    store: SqliteRunStore,
) -> None:
    _run(store, "pending-profile")
    _profile(
        store,
        "pending-profile",
        1,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="pending-profile", attempt_number=2
        )

    assert raised.value.reason == "contained_profile_missing"


def test_continuation_cannot_clear_original_exposure(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "producer")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _run(store, "continuation")
    _profile(
        store,
        "continuation",
        1,
        containment=Containment.HOST,
        auth_path="cli-login",
    )
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:7",
        "7",
        "producer",
        "agent/issue-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            "agent/issue-7",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:7",
                "7",
                "producer",
                "agent/issue-7",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    _attest_claim(store, worktree=worktree, continuation_run_id="continuation")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="continuation", published_head=_UNSCANNED_HEAD
        )

    assert raised.value.reason == "credential_scan_missing"


def test_requeued_continuation_keeps_every_generation_in_ancestry(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "producer")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    _run(store, "exposed-continuation")
    _profile(
        store,
        "exposed-continuation",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _run(store, "native-continuation")
    _profile(
        store,
        "native-continuation",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:7",
        "7",
        "producer",
        "agent/issue-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            "agent/issue-7",
            worktree,
            "exposed-continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:7",
                "7",
                "producer",
                "agent/issue-7",
                worktree,
                "exposed-continuation",
            ),
        )
        == "claimed"
    )
    _attest_claim(store, worktree=worktree, continuation_run_id="exposed-continuation")
    assert store.mark_continuation_awaiting(
        "epic:7",
        "7",
        "producer",
        "agent/issue-7",
        worktree,
        1,
        "exposed-continuation",
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            "agent/issue-7",
            worktree,
            "native-continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:7",
                "7",
                "producer",
                "agent/issue-7",
                worktree,
                "native-continuation",
            ),
        )
        == "claimed"
    )
    _attest_claim(store, worktree=worktree, continuation_run_id="native-continuation")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="native-continuation", published_head=_UNSCANNED_HEAD
        )

    assert raised.value.reason == "credential_scan_missing"
    # Launch admission precedes any output; the push-time scan decides (#1969).
    require_publication_launch_credentials(
        store,
        run_id="native-continuation",
        run_kind="dispatch",
        attempt_number=1,
    )


def test_requeued_missing_claim_evidence_refuses_direct_and_explicit_publication(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "producer")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _run(store, "continuation")
    _profile(
        store,
        "continuation",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    _run(store, "remediation")
    _profile(
        store,
        "remediation",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    _claim_and_requeue_without_evidence(
        store,
        worktree=tmp_path / "preserved",
        original_run_id="producer",
        continuation_run_id="continuation",
    )

    calls = (
        lambda: require_publication_credentials(store, run_id="continuation"),
        lambda: require_publication_credentials(
            store,
            run_id="remediation",
            producer_run_id="continuation",
        ),
    )
    for call in calls:
        with pytest.raises(CredentialPublicationRefused) as raised:
            call()
        assert raised.value.reason == "publication_evidence_unavailable"


def test_live_missing_claim_evidence_still_refuses(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "original")
    _run(store, "continuation")
    _profile(
        store,
        "continuation",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:7",
        "7",
        "original",
        "agent/issue-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    # Preserve coverage of ledgers written before atomic claim evidence.
    with patch.object(store, "_attest_continuation_claim"):
        assert (
            store.claim_continuation_under_cap(
                "epic:7",
                "7",
                "original",
                "agent/issue-7",
                worktree,
                "continuation",
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:7",
                    "7",
                    "original",
                    "agent/issue-7",
                    worktree,
                    "continuation",
                ),
            )
            == "claimed"
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="continuation")

    assert raised.value.reason == "publication_evidence_unavailable"


def test_missing_claim_family_preserves_original_and_unrelated_native_runs(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    for run_id, item_id, branch in (
        ("original", "7", "agent/issue-7"),
        ("another-item", "8", "agent/issue-7"),
        ("another-branch", "7", "agent/issue-8"),
    ):
        _run(store, run_id, branch=branch)
        if item_id != "7":
            record = store.get(run_id)
            assert record is not None
            store.save(replace(record, item_id=item_id))
        _profile(
            store,
            run_id,
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )
    _claim_and_requeue_without_evidence(
        store,
        worktree=tmp_path / "preserved",
        original_run_id="original",
        continuation_run_id="lost-owner",
    )

    for run_id in ("original", "another-item", "another-branch"):
        require_publication_credentials(store, run_id=run_id)


def test_mixed_original_and_possible_continuation_families_refuse(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "run")
    _profile(
        store,
        "run",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    for index, original in enumerate(("run", "other-original"), start=1):
        _claim_and_requeue_without_evidence(
            store,
            worktree=tmp_path / f"preserved-{index}",
            original_run_id=original,
            continuation_run_id=f"lost-owner-{index}",
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="run")

    assert raised.value.reason == "publication_evidence_unavailable"


def test_missing_claim_recovery_family_query_is_bounded(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "possible-continuation")
    _profile(
        store,
        "possible-continuation",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    for index in range(33):
        _claim_and_requeue_without_evidence(
            store,
            worktree=tmp_path / f"preserved-{index}",
            original_run_id=f"original-{index}",
            continuation_run_id=f"lost-owner-{index}",
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="possible-continuation")

    assert raised.value.reason == "publication_lineage_too_deep"


def test_missing_continuation_generation_fails_closed(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    for run_id in ("producer", "generation-two"):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )
    emit_continuation_evidence(
        store,
        "continuation_claimed",
        actor=Actor("policy", "test"),
        phase="claim",
        epic_ref="epic:7",
        item_id="7",
        original_run_id="producer",
        continuation_run_id="generation-two",
        branch="agent/issue-7",
        worktree_identity=tmp_path / "preserved",
        generation=2,
        state="continuing",
        outcome="continuing",
        run_id="generation-two",
        issue_num=7,
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="generation-two")

    assert raised.value.reason == "publication_evidence_unavailable"


def test_conflicting_continuation_generation_fails_closed(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    for run_id in ("producer", "first-owner", "other-owner"):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )
    for continuation_run_id in ("first-owner", "other-owner"):
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref="epic:7",
            item_id="7",
            original_run_id="producer",
            continuation_run_id=continuation_run_id,
            branch="agent/issue-7",
            worktree_identity=tmp_path / "preserved",
            generation=1,
            state="continuing",
            outcome="continuing",
            run_id=continuation_run_id,
            issue_num=7,
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="first-owner")

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_continuation_generation_query_overflow_fails_closed(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "producer")
    for generation in range(1, 34):
        run_id = f"continuation-{generation}"
        _run(store, run_id)
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref="epic:7",
            item_id="7",
            original_run_id="producer",
            continuation_run_id=run_id,
            branch="agent/issue-7",
            worktree_identity=tmp_path / "preserved",
            generation=generation,
            state="continuing",
            outcome="continuing",
            run_id=run_id,
            issue_num=7,
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="continuation-33")

    assert raised.value.reason == "publication_lineage_too_deep"


def test_continuation_identity_ignores_the_unchained_audit_item_column(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _run(store, "producer")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _run(store, "continuation")
    payload = continuation_evidence(
        phase="claim",
        epic_ref="epic:7",
        item_id="7",
        original_run_id="producer",
        continuation_run_id="continuation",
        branch="agent/issue-7",
        worktree_identity=tmp_path / "preserved",
        generation=1,
        state="continuing",
        outcome="continuing",
    )
    identity = continuation_evidence_identity("continuation_claimed", payload)
    assert identity is not None
    payload["evidence_identity"] = identity
    store.append_audit_event(
        timestamp="2026-09-06T00:00:00+00:00",
        event="continuation_claimed",
        run_id="continuation",
        issue_num=7,
        item_id="attacker-selected-item",
        actor_type="policy",
        actor_id="test",
        payload=payload,
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="continuation", published_head=_UNSCANNED_HEAD
        )

    assert raised.value.reason == "credential_scan_missing"


def test_referenced_parent_requires_a_retained_run_row() -> None:
    class MissingParentStore:
        def get(self, run_id: str) -> object | None:
            return object() if run_id == "continuation" else None

        def list_route_profiles(self, run_id: str) -> list[object]:
            return []

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return ["missing-parent"] if run_id == "continuation" else []

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(  # type: ignore[arg-type]
            MissingParentStore(), run_id="continuation"
        )

    assert raised.value.reason == "publication_ancestor_evidence_missing"


@pytest.mark.parametrize(
    "relation", ["explicit-producer", "continuation-parent", "job-predecessor"]
)
def test_referenced_unknown_profiles_cannot_use_safe_projection_columns(
    store: SqliteRunStore, tmp_path: Path, relation: str
) -> None:
    for run_id in ("current", "ancestor"):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=(
                Containment.UNKNOWN if run_id == "ancestor" else Containment.NATIVE
            ),
            auth_path="cli-login",
        )
    wrapped = _SafeProjectionStore(store, {"ancestor"})

    if relation == "explicit-producer":
        call = lambda: require_publication_credentials(  # noqa: E731
            wrapped, run_id="current", producer_run_id="ancestor"
        )
    elif relation == "continuation-parent":
        worktree = tmp_path / "preserved"
        store.record_continuation(
            "epic:7",
            "7",
            "ancestor",
            "agent/issue-7",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )
        assert (
            store.claim_continuation_under_cap(
                "epic:7",
                "7",
                "ancestor",
                "agent/issue-7",
                worktree,
                "current",
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:7",
                    "7",
                    "ancestor",
                    "agent/issue-7",
                    worktree,
                    "current",
                ),
            )
            == "claimed"
        )
        record = store.get_continuation(
            "epic:7", "7", "ancestor", "agent/issue-7", worktree
        )
        assert record is not None
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            continuation_run_id="current",
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            generation=record.generation,
            state=record.state,
            outcome="continuing",
            run_id="current",
            issue_num=7,
        )
        call = lambda: require_publication_credentials(  # noqa: E731
            wrapped, run_id="current"
        )
    else:
        predecessor = SimpleNamespace(
            job_id="predecessor",
            run_id="ancestor",
            attempt_number=1,
            predecessor_job_id=None,
            superseded_by="current-job",
            lineage_key="github:acme/widgets:agent/issue-7",
            source_kind="github",
            source_id="acme/widgets",
            branch="agent/issue-7",
        )
        current = SimpleNamespace(
            job_id="current-job",
            run_id="current",
            attempt_number=1,
            predecessor_job_id="predecessor",
            superseded_by=None,
            lineage_key=predecessor.lineage_key,
            source_kind=predecessor.source_kind,
            source_id=predecessor.source_id,
            branch=predecessor.branch,
        )
        wrapped.jobs[predecessor.job_id] = predecessor
        call = lambda: require_normalized_job_credentials(  # noqa: E731
            wrapped, current
        )

    with pytest.raises(CredentialPublicationRefused):
        call()


def test_referenced_canonical_unknown_profile_requires_positive_evidence(
    store: SqliteRunStore,
) -> None:
    for run_id, containment in (
        ("current", Containment.NATIVE),
        ("ancestor", Containment.UNKNOWN),
    ):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=containment,
            auth_path="cli-login",
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store,
            run_id="current",
            producer_run_id="ancestor",
        )

    assert raised.value.reason == "publication_ancestor_evidence_missing"


@pytest.mark.parametrize(
    ("problem", "expected_reason"),
    [
        ("missing", "publication_ancestor_evidence_missing"),
        ("unreadable", "publication_ancestor_evidence_missing"),
        ("stale", "contained_profile_stale"),
    ],
)
def test_referenced_ancestor_requires_readable_consistent_profile_evidence(
    store: SqliteRunStore, problem: str, expected_reason: str
) -> None:
    for run_id in ("current", "ancestor"):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )
    ancestor_profile = store.list_route_profiles("ancestor")[0]

    class ProblemStore:
        def get(self, run_id: str):
            return store.get(run_id)

        def list_route_profiles(self, run_id: str):
            if run_id != "ancestor":
                return store.list_route_profiles(run_id)
            if problem == "missing":
                return []
            if problem == "unreadable":
                return [replace(ancestor_profile, profile_json="{")]
            return [replace(ancestor_profile, containment="host")]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return []

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            ProblemStore(),  # type: ignore[arg-type]
            run_id="current",
            producer_run_id="ancestor",
        )

    assert raised.value.reason == expected_reason


def test_referenced_native_ancestor_with_positive_profile_evidence_is_allowed(
    store: SqliteRunStore,
) -> None:
    for run_id in ("current", "ancestor"):
        _run(store, run_id)
        _profile(
            store,
            run_id,
            1,
            containment=Containment.NATIVE,
            auth_path="cli-login",
        )

    require_publication_credentials(
        store,
        run_id="current",
        producer_run_id="ancestor",
    )


def test_referenced_producer_requires_every_protected_attempt(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", attempts=2)
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )
    _run(store, "remediation")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store,
            run_id="remediation",
            producer_run_id="producer",
        )

    assert raised.value.reason == "contained_profile_missing"


def test_supervised_native_publication_remains_outside_this_split(
    store: SqliteRunStore,
) -> None:
    _run(store, "native")
    _profile(
        store,
        "native",
        1,
        containment=Containment.NATIVE,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )

    require_publication_credentials(store, run_id="native")


def test_unreferenced_legacy_native_entry_remains_allowed(
    store: SqliteRunStore,
) -> None:
    _run(store, "legacy-native")

    require_publication_credentials(store, run_id="legacy-native")


def test_safe_contained_branch_without_a_head_binding_fails_closed(
    store: SqliteRunStore,
) -> None:
    _run(store, "contained-without-binding")
    _profile(
        store,
        "contained-without-binding",
        1,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


_EMPTY_REVIEW_DIGEST = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


class _LegacyReviewEvidenceStore:
    """A failed empty reviewer follows a real native branch producer."""

    def __init__(self) -> None:
        self.reviewer = SimpleNamespace(
            run_id="reviewer",
            run_kind="adversarial_review",
            status="failure",
            exit_code=1,
            finished_at=datetime(2026, 9, 10, tzinfo=UTC),
            attempt_number=1,
            container_name="synthetic-review-container",
            item_id="7",
            pr_url=None,
        )
        self.producer = SimpleNamespace(
            run_id="producer", container_name=None, item_id="7", branch="agent/issue-7"
        )
        self.records = [self.reviewer, self.producer]
        self.events = [
            SimpleNamespace(
                event="adversarial_review_verdict",
                run_id="reviewer",
                actor_type="policy",
                actor_id="epic-driver",
                item_id="7",
                payload={
                    "item_id": "7",
                    "verdict": "DEGRADED",
                    "head_sha": "f" * 40,
                    "round": 1,
                    "sidecar_digest": _EMPTY_REVIEW_DIGEST,
                },
            )
        ]
        self.rounds = [
            SimpleNamespace(
                item_id="7",
                verdict="DEGRADED",
                head_sha="f" * 40,
                round=1,
                sidecar_digest=_EMPTY_REVIEW_DIGEST,
                sidecar_path=None,
            )
        ]
        self.audit_queries: list[dict[str, object]] = []

    def normalized_jobs_for_head(self, **kwargs):
        return []

    def list_runs_by_branch_after(self, branch, **kwargs):
        return self.records, ("2026-09-05T00:00:00+00:00", 1)

    def get(self, run_id):
        return next(record for record in self.records if record.run_id == run_id)

    def list_route_profiles(self, run_id):
        return [_stored_native_profile(run_id)] if run_id == "producer" else []

    def continuation_origins(self, run_id, **kwargs):
        return []

    def list_audit_events(self, **kwargs):
        self.audit_queries.append(kwargs)
        wanted = kwargs.get("event")
        # A real store filters by event name; a malformed row (no event
        # attribute) is still returned so the guard has to reject it itself.
        return [
            event
            for event in self.events
            if wanted is None or not hasattr(event, "event") or event.event == wanted
        ]

    def list_review_rounds(self, item_id):
        assert item_id == "7"
        return self.rounds


def test_empty_failed_review_selects_actual_native_producer() -> None:
    store = _LegacyReviewEvidenceStore()
    selected = None
    refusal = None
    try:
        selected = publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )
    except CredentialPublicationRefused as exc:
        refusal = exc.reason

    assert selected == "producer", refusal
    require_publication_credentials(  # type: ignore[arg-type]
        store, run_id=selected, producer_run_id=selected
    )


def _assert_empty_review_not_exempt(store: _LegacyReviewEvidenceStore) -> None:
    with pytest.raises(CredentialPublicationRefused):
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )


def _assert_unparseable_review_selects_producer(store) -> None:
    selected = None
    refusal = None
    try:
        selected = publication_producer_for_head(
            store, branch="agent/issue-7", head_sha="f" * 40
        )
    except CredentialPublicationRefused as exc:
        refusal = exc.reason

    assert selected == "producer", refusal
    require_publication_credentials(store, run_id=selected, producer_run_id=selected)


@pytest.mark.parametrize(
    "mismatch",
    ["event_digest", "route_profile", "issuer_record", "other_run", "second_event"],
)
def test_unparseable_review_exemption_preserves_protection(mismatch, monkeypatch):
    store = _LegacyReviewEvidenceStore()
    store.events[0].payload["verdict"] = "UNPARSEABLE"
    store.rounds = []
    # Establish acceptance before changing one part of the attested evidence.
    _assert_unparseable_review_selects_producer(store)

    if mismatch == "event_digest":
        store.events[0].payload["sidecar_digest"] = "a" * 64
    elif mismatch == "route_profile":
        monkeypatch.setattr(
            store,
            "list_route_profiles",
            lambda run_id: [_stored_native_profile(run_id)],
        )
    elif mismatch == "issuer_record":
        store.events.append(
            SimpleNamespace(
                event="sandbox_resolved",
                run_id="reviewer",
                actor_type="policy",
                actor_id="credential-issuer",
                item_id=None,
                payload={
                    "phase": "issuance",
                    "credential_issuer": {"issuer": "openrouter", "key_hash": "a" * 64},
                },
            )
        )
    elif mismatch == "other_run":
        store.events[0].run_id = "other-reviewer"
    else:
        store.events.append(
            SimpleNamespace(
                **{
                    **vars(store.events[0]),
                    "payload": {**store.events[0].payload, "round": 2},
                }
            )
        )

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, None),
        ("verdict", "READY"),
        ("verdict", "DEGRADED"),
        ("verdict", "UNPARSEABLE"),
        ("sidecar_digest", "a" * 64),
        ("sidecar_path", "synthetic-report.json"),
        ("sidecar_path", ""),
        ("round", True),
        ("round", 1.0),
    ],
)
def test_unparseable_review_exemption_ignores_rounds_at_its_head(field, value):
    """The driver records no round for UNPARSEABLE (#1937 review round 1).

    A row at the crashed review's head and round number belongs to a later
    review that reused the number; it neither grants nor revokes the
    exemption, which is keyed on the crashed run's own verdict event.
    """
    store = _LegacyReviewEvidenceStore()
    store.events[0].payload["verdict"] = "UNPARSEABLE"
    if field is None:
        store.rounds.append(SimpleNamespace(**vars(store.rounds[0])))
    else:
        setattr(store.rounds[0], field, value)

    _assert_unparseable_review_selects_producer(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(
            ("run_kind", kind)
            for kind in (
                "dispatch",
                "delegation",
                "review",
                "review_remediation",
                "planning",
                "readiness_probe",
            )
        ),
        ("status", "success"),
        ("status", "pending"),
        ("exit_code", 0),
        ("exit_code", None),
        ("exit_code", True),
        ("exit_code", "1"),
        ("finished_at", None),
        ("finished_at", "malformed"),
        ("pr_url", "synthetic-pr"),
        ("pr_url", ""),
        ("item_id", None),
        ("item_id", ""),
        ("run_id", ""),
    ],
)
def test_empty_review_exemption_requires_terminal_failed_review(field, value):
    store = _LegacyReviewEvidenceStore()
    setattr(store.reviewer, field, value)

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "unrelated"),
        ("run_id", "unrelated"),
        ("item_id", "unrelated"),
        ("actor_type", "agent"),
        ("actor_id", "unrelated"),
        ("payload", None),
        ("payload", []),
    ],
)
def test_empty_review_exemption_requires_exact_policy_event(field, value):
    store = _LegacyReviewEvidenceStore()
    setattr(store.events[0], field, value)

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_id", "unrelated"),
        ("verdict", "READY"),
        ("verdict", "NEEDS_CHANGES"),
        ("sidecar_digest", "a" * 64),
        ("sidecar_digest", None),
        ("round", None),
        ("round", 0),
        ("round", True),
        ("round", 1.0),
        ("head_sha", None),
        ("head_sha", ""),
        ("head_sha", "g" * 40),
    ],
)
def test_empty_review_exemption_requires_complete_empty_verdict(field, value):
    store = _LegacyReviewEvidenceStore()
    store.events[0].payload[field] = value

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_id", "unrelated"),
        ("head_sha", "a" * 40),
        ("round", 2),
        ("round", True),
        ("round", 1.0),
        ("verdict", "NEEDS_CHANGES"),
        ("sidecar_digest", "a" * 64),
        ("sidecar_path", "synthetic-report.json"),
        ("sidecar_path", ""),
    ],
)
def test_empty_review_exemption_requires_matching_output_free_round(field, value):
    store = _LegacyReviewEvidenceStore()
    setattr(store.rounds[0], field, value)

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize("surface", ["events", "rounds"])
@pytest.mark.parametrize("shape", ["missing", "duplicate", "conflicting", "malformed"])
def test_empty_review_exemption_refuses_uncertain_evidence(surface, shape):
    store = _LegacyReviewEvidenceStore()
    rows = getattr(store, surface)
    if shape == "missing":
        rows.clear()
    elif shape == "duplicate":
        rows.append(rows[0])
    elif shape == "conflicting":
        changed = dict(vars(rows[0]))
        if surface == "events":
            changed["payload"] = {**changed["payload"], "verdict": "NEEDS_CHANGES"}
        else:
            changed["sidecar_digest"] = "a" * 64
        rows.append(SimpleNamespace(**changed))
    else:
        rows.append(object())

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize("method", ["list_audit_events", "list_review_rounds"])
def test_empty_review_exemption_refuses_unavailable_evidence(method, monkeypatch):
    store = _LegacyReviewEvidenceStore()

    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable evidence")

    monkeypatch.setattr(store, method, unavailable)

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize("count", [32, 33])
@pytest.mark.parametrize("surface", ["events", "rounds"])
def test_empty_review_exemption_bounds_evidence(surface, count):
    store = _LegacyReviewEvidenceStore()
    rows = getattr(store, surface)
    for index in range(count - 1):
        other = dict(vars(rows[0]))
        other["run_id" if surface == "events" else "head_sha"] = (
            f"other-{index}" if surface == "events" else f"{index:040x}"
        )
        rows.append(SimpleNamespace(**other))

    if count > 32:
        _assert_empty_review_not_exempt(store)
    else:
        assert (
            publication_producer_for_head(  # type: ignore[arg-type]
                store, branch="agent/issue-7", head_sha="f" * 40
            )
            == "producer"
        )
    assert store.audit_queries == [
        {
            "limit": 33,
            "event": "publication_intent",
            "actor_type": "policy",
            "item_id": "7",
            "after_id": None,
        },
        # Both claim shapes are paged before the head falls back to the
        # legacy heuristic; neither one alone decides the head is unclaimed.
        {
            "limit": 33,
            "event": "publication_head_attested",
            "actor_type": "human",
            "item_id": "7",
            "after_id": None,
        },
        # The reviewer names no issued key before the exemption applies.
        {"limit": 257, "event": "sandbox_resolved", "run_id": "reviewer"},
        {"limit": 33, "event": "adversarial_review_verdict", "run_id": "reviewer"},
    ]


def test_only_empty_failed_review_does_not_become_a_producer() -> None:
    store = _LegacyReviewEvidenceStore()
    store.records = [store.reviewer]

    assert (
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )
        is None
    )


def test_empty_review_exemption_keeps_real_contained_producer_protected() -> None:
    store = _LegacyReviewEvidenceStore()
    store.producer.container_name = "synthetic-producing-container"

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize("lineage", ["explicit", "continuation", "normalized"])
def test_empty_review_remains_protected_when_explicitly_in_lineage(
    lineage, monkeypatch
):
    store = _LegacyReviewEvidenceStore()
    if lineage == "continuation":
        monkeypatch.setattr(
            store,
            "continuation_origins",
            lambda run_id, **kwargs: ["reviewer"] if run_id == "producer" else [],
        )
    elif lineage == "normalized":
        job = SimpleNamespace(
            run_id="reviewer",
            attempt_number=1,
            branch="agent/issue-7",
            head_sha="f" * 40,
        )
        monkeypatch.setattr(store, "normalized_jobs_for_head", lambda **kwargs: [job])

    with pytest.raises(CredentialPublicationRefused) as raised:
        if lineage == "normalized":
            publication_producer_for_head(  # type: ignore[arg-type]
                store, branch="agent/issue-7", head_sha="f" * 40
            )
        else:
            require_publication_credentials(  # type: ignore[arg-type]
                store,
                run_id="producer",
                producer_run_id="reviewer" if lineage == "explicit" else None,
            )

    assert raised.value.reason == "publication_ancestor_evidence_missing"
    assert store.audit_queries == []


def test_conflicting_publication_head_bindings_fail_closed() -> None:
    job = SimpleNamespace(branch="agent/issue-7", head_sha="f" * 40)

    class ConflictingHeadStore:
        def normalized_jobs_for_head(
            self, *, branch: str, head_sha: str, limit: int
        ) -> list[object]:
            return [job, job]

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(  # type: ignore[arg-type]
            ConflictingHeadStore(), branch="agent/issue-7", head_sha="f" * 40
        )

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_publication_predecessor_query_overflow_fails_closed() -> None:
    jobs = {}
    for index in range(34):
        jobs[str(index)] = SimpleNamespace(
            job_id=str(index),
            run_id=f"run-{index}",
            attempt_number=1,
            predecessor_job_id=str(index + 1) if index < 33 else None,
            superseded_by=str(index - 1) if index > 0 else None,
            lineage_key="github:acme/widgets:agent/issue-7",
            source_kind="github",
            source_id="acme/widgets",
            branch="agent/issue-7",
        )

    class DeepJobStore:
        def list_audit_events(
            self, *, event: str, run_id: str, limit: int
        ) -> list[object]:
            assert event == "sandbox_resolved"
            return []

        def get(self, run_id: str) -> object:
            return object()

        def list_route_profiles(self, run_id: str) -> list[object]:
            return [_stored_native_profile(run_id)]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return []

        def get_normalized_job(self, job_id: str) -> object | None:
            return jobs.get(job_id)

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_normalized_job_credentials(  # type: ignore[arg-type]
            DeepJobStore(), jobs["0"]
        )

    assert raised.value.reason == "publication_lineage_too_deep"


def test_generic_review_approval_cannot_release_credential_exposure(
    store: SqliteRunStore,
) -> None:
    _run(store, "reviewed-exposure")
    _profile(
        store,
        "reviewed-exposure",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    verdict = FilterVerdict(flags=(Flag("Makefile", "executes during a build"),))
    assert verdict.approved(reviewed=True) == ()

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="reviewed-exposure", published_head=_UNSCANNED_HEAD
        )

    assert not isinstance(raised.value, ReviewRequired)
    assert raised.value.reason == "credential_scan_missing"


def test_malformed_profile_evidence_fails_with_a_policy_refusal(
    store: SqliteRunStore,
) -> None:
    _run(store, "stale")
    _profile(
        store,
        "stale",
        1,
        containment=Containment.CONTAINED,
        auth_path="workload-identity",
    )
    stale = replace(store.list_route_profiles("stale")[0], profile_json="{")

    class StaleStore:
        def get(self, run_id: str):
            return store.get(run_id)

        def list_route_profiles(self, run_id: str):
            return [stale]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return []

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(StaleStore(), run_id="stale")  # type: ignore[arg-type]

    assert raised.value.reason == "contained_profile_stale"


def test_stale_projection_cannot_downgrade_canonical_containment(
    store: SqliteRunStore,
) -> None:
    _run(store, "downgraded")
    _profile(
        store,
        "downgraded",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    downgraded = replace(
        store.list_route_profiles("downgraded")[0], containment="native"
    )

    class DowngradedStore:
        def get(self, run_id: str):
            return store.get(run_id)

        def list_route_profiles(self, run_id: str):
            return [downgraded]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return []

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(DowngradedStore(), run_id="downgraded")  # type: ignore[arg-type]

    assert raised.value.reason == "contained_profile_stale"


def test_ambiguous_continuation_parents_fail_closed() -> None:
    class AmbiguousStore:
        def get(self, run_id: str) -> None:
            return None

        def list_route_profiles(self, run_id: str) -> list[object]:
            return []

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return ["producer-a", "producer-b"]

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(AmbiguousStore(), run_id="continuation")  # type: ignore[arg-type]

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_explicit_producer_may_also_be_the_durable_parent() -> None:
    class ConvergingStore:
        def list_audit_events(
            self, *, event: str, run_id: str, limit: int
        ) -> list[object]:
            assert event == "sandbox_resolved"
            return []

        def get(self, run_id: str) -> object:
            return object()

        def list_route_profiles(self, run_id: str) -> list[object]:
            return [_stored_native_profile(run_id)]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return ["producer"] if run_id == "continuation" else []

    require_publication_credentials(
        ConvergingStore(),  # type: ignore[arg-type]
        run_id="continuation",
        producer_run_id="producer",
    )


def test_bounded_lineage_refuses_a_cycle_without_recursing() -> None:
    class CyclicStore:
        def get(self, run_id: str) -> object:
            return object()

        def list_route_profiles(self, run_id: str) -> list[object]:
            return [_stored_native_profile(run_id)]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return ["b" if run_id == "a" else "a"]

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(CyclicStore(), run_id="a")  # type: ignore[arg-type]

    assert raised.value.reason == "publication_lineage_cycle"


def test_bounded_lineage_refuses_excessive_depth() -> None:
    class DeepStore:
        def get(self, run_id: str) -> object:
            return object()

        def list_route_profiles(self, run_id: str) -> list[object]:
            return [_stored_native_profile(run_id)]

        def continuation_origins(self, run_id: str, *, limit: int) -> list[str]:
            return [str(int(run_id) + 1)]

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(DeepStore(), run_id="0")  # type: ignore[arg-type]

    assert raised.value.reason == "publication_lineage_too_deep"


_GOVERNED_REVIEW_DIGEST = (
    "1047c348748cb1c454522ec87c1ff2f869c0f0fb866fa30822ad18f4a2fe90f1"
)


class _GovernedReviewEvidenceStore(_LegacyReviewEvidenceStore):
    """A completed contained governed review follows a real native producer.

    This is the shape every driver-reviewed branch has after round one: the
    reviewer ran in a container, recorded no route profile, and the driver
    attested a real verdict. Selecting that reviewer as the legacy producer
    refused every remediation with publication_ancestor_evidence_missing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reviewer.status = "success"
        self.reviewer.exit_code = 0
        self.events[0].payload["verdict"] = "NEEDS_CHANGES"
        self.events[0].payload["sidecar_digest"] = _GOVERNED_REVIEW_DIGEST
        self.rounds[0].verdict = "NEEDS_CHANGES"
        self.rounds[0].sidecar_digest = _GOVERNED_REVIEW_DIGEST
        self.rounds[0].sidecar_path = "/logs/review-sidecar-reviewer.json"


def _assert_governed_review_not_exempt(store: _GovernedReviewEvidenceStore) -> None:
    with pytest.raises(CredentialPublicationRefused):
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )


def test_completed_governed_review_selects_actual_native_producer() -> None:
    store = _GovernedReviewEvidenceStore()

    selected = publication_producer_for_head(  # type: ignore[arg-type]
        store, branch="agent/issue-7", head_sha="f" * 40
    )

    assert selected == "producer"
    require_publication_credentials(  # type: ignore[arg-type]
        store, run_id=selected, producer_run_id=selected
    )
    assert store.audit_queries == [
        {
            "limit": 33,
            "event": "publication_intent",
            "actor_type": "policy",
            "item_id": "7",
            "after_id": None,
        },
        # Both claim shapes are paged before the legacy heuristic runs.
        {
            "limit": 33,
            "event": "publication_head_attested",
            "actor_type": "human",
            "item_id": "7",
            "after_id": None,
        },
        # The reviewer names no issued key before the exemption applies.
        {"limit": 257, "event": "sandbox_resolved", "run_id": "reviewer"},
        {"limit": 33, "event": "adversarial_review_verdict", "run_id": "reviewer"},
        # Re-audit F8: issuance evidence gates every run, contained or not.
        {"limit": 257, "event": "sandbox_resolved", "run_id": "producer"},
    ]


# NEEDS_FULL (#2111) is as real as the other two — a delta READY recorded as
# still owing a full round. Dropping it from the exempt set makes a successful
# NEEDS_FULL reviewer the selected legacy branch producer, which refuses
# credentials for every later remediation on the branch.
@pytest.mark.parametrize("verdict", ["READY", "NEEDS_CHANGES", "NEEDS_FULL"])
def test_governed_review_exemption_accepts_each_real_verdict(verdict: str) -> None:
    store = _GovernedReviewEvidenceStore()
    store.events[0].payload["verdict"] = verdict
    store.rounds[0].verdict = verdict

    assert (
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )
        == "producer"
    )


def test_only_governed_review_does_not_become_a_producer() -> None:
    store = _GovernedReviewEvidenceStore()
    store.records = [store.reviewer]

    assert (
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_kind", "dispatch"),
        ("run_kind", "review_remediation"),
        ("status", "failure"),
        ("status", "pending"),
        ("exit_code", 1),
        ("exit_code", None),
        ("exit_code", True),
        ("finished_at", None),
        ("pr_url", "synthetic-pr"),
        ("item_id", ""),
        ("run_id", ""),
    ],
)
def test_governed_review_exemption_requires_completed_review(field, value) -> None:
    store = _GovernedReviewEvidenceStore()
    setattr(store.reviewer, field, value)

    _assert_governed_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verdict", "DEGRADED"),
        ("verdict", "ready"),
        ("verdict", None),
        ("sidecar_digest", _EMPTY_REVIEW_DIGEST),
        ("sidecar_digest", "a" * 63),
        ("sidecar_digest", None),
        ("round", 0),
        ("round", True),
        ("head_sha", "g" * 40),
        ("item_id", "unrelated"),
    ],
)
def test_governed_review_exemption_requires_complete_verdict(field, value) -> None:
    store = _GovernedReviewEvidenceStore()
    store.events[0].payload[field] = value
    # The stored round agrees with the mutated payload, so the refusal can
    # only come from the payload validation, not from round disagreement.
    if hasattr(store.rounds[0], field):
        setattr(store.rounds[0], field, value)

    _assert_governed_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_type", "agent"),
        ("actor_id", "unrelated"),
        ("run_id", "unrelated"),
    ],
)
def test_governed_review_exemption_requires_policy_attestation(field, value) -> None:
    store = _GovernedReviewEvidenceStore()
    setattr(store.events[0], field, value)

    _assert_governed_review_not_exempt(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verdict", "READY"),
        ("sidecar_digest", "b" * 64),
        ("sidecar_path", None),
        ("sidecar_path", ""),
        ("head_sha", "e" * 40),
        ("round", 2),
    ],
)
def test_governed_review_exemption_requires_agreeing_round(field, value) -> None:
    store = _GovernedReviewEvidenceStore()
    setattr(store.rounds[0], field, value)

    _assert_governed_review_not_exempt(store)


def test_governed_review_exemption_requires_exactly_one_round() -> None:
    store = _GovernedReviewEvidenceStore()
    store.rounds.append(SimpleNamespace(**dict(vars(store.rounds[0]))))

    _assert_governed_review_not_exempt(store)


def _governed_reviewer_on_real_store(
    store: SqliteRunStore, *, container_name: str = "kew-review-7"
) -> None:
    """A completed contained governed review, newest on the branch, no profile."""
    from kew.audit import emit

    digest = _GOVERNED_REVIEW_DIGEST
    store.save(
        RunRecord(
            run_id="reviewer",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, 1, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, 2, tzinfo=UTC),
            attempt_number=1,
            container_name=container_name,
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="adversarial_review",
        )
    )
    emit(
        store,
        "adversarial_review_verdict",
        actor=Actor("policy", "epic-driver"),
        run_id="reviewer",
        issue_num=7,
        item_id="7",
        payload={
            "item_id": "7",
            "head_sha": "f" * 40,
            "round": 1,
            "verdict": "NEEDS_CHANGES",
            "sidecar_digest": digest,
        },
    )
    store.record_review_round(
        "epic",
        "7",
        head_sha="f" * 40,
        verdict="NEEDS_CHANGES",
        sidecar_digest=digest,
        sidecar_path="/logs/review-sidecar-reviewer.json",
    )


def test_real_store_governed_review_selects_native_producer(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _governed_reviewer_on_real_store(store)
    assert [r.run_id for r in store.list_runs_by_branch("agent/issue-7")][
        0
    ] == "reviewer"

    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )


def test_governed_review_exemption_keeps_real_contained_producer_protected(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success", container_name="kew-7")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _governed_reviewer_on_real_store(store)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_relabeled_run_with_retained_contained_profile_is_not_exempt(
    store: SqliteRunStore,
) -> None:
    """A run relabeled adversarial_review still carries its exposure evidence."""
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _governed_reviewer_on_real_store(store)
    _profile(
        store,
        "reviewer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_reviewer_with_a_retained_native_profile_is_not_exempt(monkeypatch) -> None:
    store = _GovernedReviewEvidenceStore()
    monkeypatch.setattr(
        store, "list_route_profiles", lambda run_id: [_stored_native_profile(run_id)]
    )

    _assert_governed_review_not_exempt(store)


def test_reviewer_with_a_continuation_origin_is_not_exempt(monkeypatch) -> None:
    """An excluded reviewer must not hide retained continuation ancestry."""
    store = _GovernedReviewEvidenceStore()
    monkeypatch.setattr(
        store,
        "continuation_origins",
        lambda run_id, **kwargs: ["exposed-origin"] if run_id == "reviewer" else [],
    )

    _assert_governed_review_not_exempt(store)


@pytest.mark.parametrize("method", ["list_route_profiles", "continuation_origins"])
def test_governed_review_exemption_refuses_unavailable_reviewer_evidence(
    method, monkeypatch
) -> None:
    store = _GovernedReviewEvidenceStore()
    original = getattr(store, method)

    def unavailable(run_id, *args, **kwargs):
        # Only the reviewer's read fails; the producer's evidence stays
        # readable, so the refusal can only come from the exemption itself.
        if run_id == "reviewer":
            raise OSError("synthetic unavailable evidence")
        return original(run_id, *args, **kwargs)

    monkeypatch.setattr(store, method, unavailable)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )

    assert raised.value.reason == "publication_evidence_unavailable"


def _empty_failed_reviewer_on_real_store(
    store: SqliteRunStore,
    *,
    verdict: str = "DEGRADED",
    record_round: bool = True,
    verdict_run_id: str = "reviewer",
) -> None:
    """An attested empty failed review, newest on the branch, no profile."""
    from kew.audit import emit

    store.save(
        RunRecord(
            run_id="reviewer",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="failure",
            exit_code=1,
            started_at=datetime(2026, 9, 13, 1, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, 2, tzinfo=UTC),
            attempt_number=1,
            container_name="kew-review-7",
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="adversarial_review",
        )
    )
    emit(
        store,
        "adversarial_review_verdict",
        actor=Actor("policy", "epic-driver"),
        run_id=verdict_run_id,
        issue_num=7,
        item_id="7",
        payload={
            "item_id": "7",
            "head_sha": "f" * 40,
            "round": 1,
            "verdict": verdict,
            "sidecar_digest": _EMPTY_REVIEW_DIGEST,
        },
    )
    if record_round:
        store.record_review_round(
            "epic",
            "7",
            head_sha="f" * 40,
            verdict=verdict,
            sidecar_digest=_EMPTY_REVIEW_DIGEST,
            sidecar_path=None,
        )


@pytest.mark.parametrize("record_round", [False, True], ids=["no_round", "one_round"])
def test_real_store_unparseable_review_selects_newest_native_producer(
    store: SqliteRunStore, record_round: bool
) -> None:
    _run(store, "older-producer", status="success")
    older = store.get("older-producer")
    assert older is not None
    store.save(replace(older, started_at=datetime(2026, 9, 4, tzinfo=UTC)))
    _profile(
        store,
        "older-producer",
        1,
        containment=Containment.NATIVE,
        auth_path="cli-login",
    )
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(
        store, verdict="UNPARSEABLE", record_round=record_round
    )

    _assert_unparseable_review_selects_producer(store)


def _ready_review_after_crash_on_real_store(store: SqliteRunStore) -> None:
    """A later governed READY review on the crashed review's head.

    The crash recorded no round, so this review takes the same round number.
    """
    from kew.audit import emit

    store.save(
        RunRecord(
            run_id="ready-reviewer",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, 3, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, 4, tzinfo=UTC),
            attempt_number=1,
            container_name="kew-review-7",
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="adversarial_review",
        )
    )
    round_num = store.record_review_round(
        "epic",
        "7",
        head_sha="f" * 40,
        verdict="READY",
        sidecar_digest=_GOVERNED_REVIEW_DIGEST,
        sidecar_path="/logs/review-sidecar-ready-reviewer.json",
    )
    assert round_num == 1
    emit(
        store,
        "adversarial_review_verdict",
        actor=Actor("policy", "epic-driver"),
        run_id="ready-reviewer",
        issue_num=7,
        item_id="7",
        payload={
            "item_id": "7",
            "head_sha": "f" * 40,
            "round": round_num,
            "verdict": "READY",
            "sidecar_digest": _GOVERNED_REVIEW_DIGEST,
        },
    )


def test_real_store_ready_round_reusing_number_keeps_unparseable_exempt(
    store: SqliteRunStore,
) -> None:
    """#1937 review round 1: a later READY round on the same head reuses the
    crashed review's round number and must not revoke its exemption."""
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(
        store, verdict="UNPARSEABLE", record_round=False
    )
    _ready_review_after_crash_on_real_store(store)

    _assert_unparseable_review_selects_producer(store)


def test_real_store_unparseable_event_naming_another_run_is_not_exempt(
    store: SqliteRunStore,
) -> None:
    """The exemption is keyed on the failed run: an UNPARSEABLE verdict event
    for a different run does not exempt this one."""
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(
        store,
        verdict="UNPARSEABLE",
        record_round=False,
        verdict_run_id="other-reviewer",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


@pytest.mark.parametrize("phase", ["issuance", "revocation"])
def test_real_store_unparseable_review_holding_issuer_record_is_not_exempt(
    store: SqliteRunStore, phase: str
) -> None:
    """A run any issuer record names stays under the issuance-evidence gate."""
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(
        store, verdict="UNPARSEABLE", record_round=False
    )
    _round10_issuer_event(store, run_id="reviewer", phase=phase)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def test_real_store_empty_failed_review_selects_native_producer(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(store)

    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )


@pytest.mark.parametrize("verdict", ["UNPARSEABLE", "DEGRADED"])
def test_real_store_conflicting_verdict_under_another_item_keeps_protection(
    store: SqliteRunStore, verdict: str
) -> None:
    """A second verdict event for the same run under another item id is
    conflicting evidence: the exemption must see it, so the reviewer row
    stays a candidate producer and publication refuses."""
    from kew.audit import emit

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(
        store, verdict=verdict, record_round=verdict == "DEGRADED"
    )
    emit(
        store,
        "adversarial_review_verdict",
        actor=Actor("policy", "epic-driver"),
        run_id="reviewer",
        issue_num=8,
        item_id="8",
        payload={
            "item_id": "8",
            "head_sha": "f" * 40,
            "round": 1,
            "verdict": "READY",
            "sidecar_digest": "sha256:" + "a" * 64,
        },
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def test_real_store_governed_review_conflicting_verdict_under_another_item(
    store: SqliteRunStore,
) -> None:
    """The governed exemption sees a conflicting event filed under another
    item id too, and keeps the reviewer row protected."""
    from kew.audit import emit

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _governed_reviewer_on_real_store(store)
    emit(
        store,
        "adversarial_review_verdict",
        actor=Actor("policy", "epic-driver"),
        run_id="reviewer",
        issue_num=8,
        item_id="8",
        payload={
            "item_id": "8",
            "head_sha": "f" * 40,
            "round": 1,
            "verdict": "NEEDS_CHANGES",
            "sidecar_digest": "sha256:" + "b" * 64,
        },
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


@pytest.mark.parametrize("stored", [1.5, 9.223372036854776e18])
def test_real_store_malformed_round_value_keeps_protection(
    store: SqliteRunStore, stored: object
) -> None:
    """A REAL or text round in the ledger is malformed, not a match.

    ``int()`` on read would turn ``1.5`` into round ``1`` and a huge float
    into a plausible integer; the store now refuses to read such a row, and
    the exemption refuses on unavailable evidence instead of accepting it.
    """
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(store, verdict="DEGRADED", record_round=True)
    store._execute("UPDATE review_rounds SET round = ? WHERE item_id = '7'", (stored,))

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_evidence_unavailable"


def test_empty_failed_review_with_retained_contained_profile_is_not_exempt(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _empty_failed_reviewer_on_real_store(store)
    _profile(
        store,
        "reviewer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_empty_failed_review_with_continuation_origin_is_not_exempt(
    monkeypatch,
) -> None:
    store = _LegacyReviewEvidenceStore()
    monkeypatch.setattr(
        store,
        "continuation_origins",
        lambda run_id, **kwargs: ["exposed-origin"] if run_id == "reviewer" else [],
    )

    _assert_empty_review_not_exempt(store)


@pytest.mark.parametrize("lineage", ["explicit", "continuation"])
def test_governed_review_remains_protected_when_explicitly_in_lineage(
    lineage, monkeypatch
) -> None:
    store = _GovernedReviewEvidenceStore()
    if lineage == "continuation":
        monkeypatch.setattr(
            store,
            "continuation_origins",
            lambda run_id, **kwargs: ["reviewer"] if run_id == "producer" else [],
        )

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(  # type: ignore[arg-type]
            store,
            run_id="producer",
            producer_run_id="reviewer" if lineage == "explicit" else None,
        )

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def _unattested_reviewer_on_real_store(
    store: SqliteRunStore, run_id: str, hour: int
) -> None:
    """A contained review recorded on the branch with no attestation at all.

    Operator-launched reviews before the driver owned the lane look like this:
    a run row with the branch and a container name, no verdict event, no round.
    """
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-review-{run_id}",
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="adversarial_review",
        )
    )


def _intent_on_real_store(
    store: SqliteRunStore,
    run_id: str,
    *,
    head_sha: str,
    branch: str = "agent/issue-7",
    actor: Actor | None = None,
) -> None:
    from kew.audit import emit

    emit(
        store,
        "publication_intent",
        actor=actor or Actor("policy", "host-publication"),
        run_id=run_id,
        issue_num=7,
        item_id="7",
        payload={
            "branch": branch,
            "commit_sha": head_sha,
            "tree_sha": "d" * 40,
            "parent_sha": "c" * 40,
        },
    )


def test_intent_binds_the_head_to_its_producer_past_unattested_reviews(
    store: SqliteRunStore,
) -> None:
    _unattested_reviewer_on_real_store(store, "old-review", 1)
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40)
    _unattested_reviewer_on_real_store(store, "new-review", 3)
    assert [r.run_id for r in store.list_runs_by_branch("agent/issue-7")][
        0
    ] == "new-review"

    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )


def test_intent_for_another_head_falls_back_to_the_legacy_protection(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="e" * 40)
    _unattested_reviewer_on_real_store(store, "new-review", 3)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def test_intent_for_another_branch_does_not_bind(store: SqliteRunStore) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40, branch="agent/issue-8")
    _unattested_reviewer_on_real_store(store, "new-review", 3)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


@pytest.mark.parametrize(
    "actor",
    [Actor("agent", "codex-operator"), Actor("policy", "epic-driver")],
    ids=["agent-actor", "policy-actor-with-other-id"],
)
def test_intent_from_a_non_host_actor_does_not_bind(
    store: SqliteRunStore, actor: Actor
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40, actor=actor)
    _unattested_reviewer_on_real_store(store, "new-review", 3)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def test_two_intent_claims_for_one_head_are_ambiguous(store: SqliteRunStore) -> None:
    for run_id in ("producer", "other"):
        _run(store, run_id, status="success")
        _profile(
            store, run_id, 1, containment=Containment.NATIVE, auth_path="cli-login"
        )
        _intent_on_real_store(store, run_id, head_sha="f" * 40)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_intent_bound_contained_producer_binds_its_published_head(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success", container_name="kew-7")
    _profile(
        store,
        "producer",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40)

    # The head is already public; binding it needs no scan. The remediation's
    # own push is scanned and gated on its new head (#1969).
    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )


def test_intent_lookup_does_not_query_for_a_malformed_head() -> None:
    store = _LegacyReviewEvidenceStore()

    selected = publication_producer_for_head(  # type: ignore[arg-type]
        store, branch="agent/issue-7", head_sha="F" * 40
    )

    assert selected == "producer"
    assert all(q.get("event") != "publication_intent" for q in store.audit_queries)


def test_intent_lookup_refuses_unavailable_audit_reads(monkeypatch) -> None:
    store = _LegacyReviewEvidenceStore()

    def unavailable(**kwargs):
        if kwargs.get("event") == "publication_intent":
            raise OSError("synthetic unavailable evidence")
        return store.events

    monkeypatch.setattr(store, "list_audit_events", unavailable)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )

    assert raised.value.reason == "publication_evidence_unavailable"


def test_intent_window_overflow_refuses_instead_of_deciding(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    for index in range(33):
        _intent_on_real_store(store, "producer", head_sha=f"{index:040x}")
    _intent_on_real_store(store, "producer", head_sha="f" * 40)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_too_deep"


def test_only_the_branch_run_cap_reads_as_the_branch_run_cap(
    store: SqliteRunStore,
) -> None:
    """Every bounded walk refuses as too deep; only a branch past its run cap
    carries the cap's diagnosis, so a hold for it names the right cause."""
    from kew.publication_credentials import is_branch_run_cap

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    for index in range(33):
        _intent_on_real_store(store, "producer", head_sha=f"{index:040x}")
    with pytest.raises(CredentialPublicationRefused) as intents:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
    for index in range(33):
        _run(store, f"producer-{index}", status="success")
    with pytest.raises(CredentialPublicationRefused) as runs:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert intents.value.reason == runs.value.reason == "publication_lineage_too_deep"
    assert not is_branch_run_cap(str(intents.value))
    assert is_branch_run_cap(str(runs.value))


def test_intent_for_another_item_does_not_bind(store: SqliteRunStore) -> None:
    """An intent recorded under a different item never binds this branch."""
    from kew.audit import emit

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="producer",
        issue_num=8,
        item_id="8",
        payload={
            "branch": "agent/issue-7",
            "commit_sha": "f" * 40,
            "tree_sha": "d" * 40,
            "parent_sha": "c" * 40,
        },
    )
    _unattested_reviewer_on_real_store(store, "new-review", 3)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_ancestor_evidence_missing"


def test_bound_run_must_sit_on_the_requested_branch(store: SqliteRunStore) -> None:
    _run(store, "producer", status="success", branch="agent/issue-7-other")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _run(store, "sibling", status="success")
    _profile(store, "sibling", 1, containment=Containment.NATIVE, auth_path="cli-login")
    _intent_on_real_store(store, "producer", head_sha="f" * 40)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_bound_run_without_a_row_refuses(store: SqliteRunStore) -> None:
    _run(store, "sibling", status="success")
    _profile(store, "sibling", 1, containment=Containment.NATIVE, auth_path="cli-login")
    _intent_on_real_store(store, "ghost", head_sha="f" * 40)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_evidence_unavailable"


def _reviewer_with_item(
    store: SqliteRunStore, run_id: str, item_id: str, hour: int
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id=item_id,
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-review-{run_id}",
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="adversarial_review",
        )
    )


def test_a_standalone_review_row_abstains_from_its_branchs_ownership(
    store: SqliteRunStore,
) -> None:
    """A ``review`` row's item is a fleet key, so it names no owner (#2446).

    Such a row must not read as a run asserting its branch names no tracker
    work: that turned an owned branch into an ambiguously owned one, and the
    head a hand-started review pushed could then never be bound to it. It
    abstains instead — the branch's tracker-work runs answer for it.
    """
    from kew import publication_credentials as credentials

    branch = "agent/issue-7"
    store.save(
        RunRecord(
            run_id="review-run",
            issue_num=43,
            item_id=None,  # the store backfills the PR number it counts this by
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, tzinfo=UTC),
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="review",
        )
    )
    assert store.get("review-run").item_id == "43"
    assert credentials.abstains_from_branch_ownership(store.get("review-run"))
    # Nothing but abstaining rows: the branch names no tracker work (branch scope).
    assert credentials._single_branch_item(store, branch=branch) == ""
    # A branch with no retained run at all stays ambiguous, as it always was.
    assert credentials._single_branch_item(store, branch="agent/issue-9") is None
    # The branch's implementation run answers for it, abstention notwithstanding.
    _run(store, "producer", status="success", branch=branch)
    assert credentials._single_branch_item(store, branch=branch) == "7"
    # A second tracker item on the branch is still ambiguous ownership.
    store.save(replace(store.get("producer"), run_id="other-item", item_id="8"))
    assert credentials._single_branch_item(store, branch=branch) is None


def _contained_standalone_review(
    store: SqliteRunStore, run_id: str, hour: int, *, branch: str = "agent/issue-7"
) -> None:
    """A hand-started ``kew review`` row that names the branch it reviews.

    Contained, so its output is protected, and carrying no tracker item of its
    own — the store backfills the PR number it counts the run by.
    """
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=43,
            item_id=None,
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-review-{run_id}",
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="review",
        )
    )


def test_a_contained_review_on_its_branch_protects_an_unrecognised_head(
    store: SqliteRunStore,
) -> None:
    """A hand-started review's row makes the branch's protected output visible.

    A standalone ``kew review`` row names the branch it reviews so its own push
    records a bindable intent (#2446). Its contained output is then visible on
    the branch, so an unrecognised head there — one no claim and no operator
    attestation names — stops being bound by the newest-run heuristic.

    That is this rule working, not a regression of it. The rule is branch
    level, not head level: ``_require_head_binding_for_protected`` is not even
    passed a head, because an unrecognised head may carry the contained run's
    output in its ancestry. Before the row named the branch the same output was
    published anyway and the heuristic bound such a head to the implementation
    run, silently — the row only stops the rule being bypassed.

    A hand-started review gets no exemption because, unlike a governed
    reviewer, it publishes: ``_attested_governed_adversarial_review`` is
    justified by a reviewer never pushing a branch head, which this run does.
    """
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _contained_standalone_review(store, "hand-review", 14)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
    assert raised.value.reason == "publication_ancestor_evidence_missing"
    # Identical to what this rule already answers for any other contained run
    # on a branch, which is the point: the review row is not a special case.
    _unattested_reviewer_on_real_store(store, "governed-review", 15)
    with pytest.raises(CredentialPublicationRefused) as same:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
    assert same.value.reason == raised.value.reason


def test_a_contained_reviews_own_head_needs_no_attestation(
    store: SqliteRunStore,
) -> None:
    """The refusal #2446 removes is the one on the head the review pushed.

    Its row names the branch, so the push records a claim and the next round
    binds that head to the run that produced it. Only a head nobody recorded
    still needs ``kew publication attest``, which is where that verb belongs.
    """
    from kew.publication_credentials import recorded_head_claims

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _contained_standalone_review(store, "hand-review", 14)
    _intent_on_real_store(store, "hand-review", head_sha="e" * 40)

    assert recorded_head_claims(store, branch="agent/issue-7", head_sha="e" * 40) == {
        "hand-review"
    }
    assert not recorded_head_claims(store, branch="agent/issue-7", head_sha="f" * 40)


@pytest.mark.parametrize("other_item", ["8", ""], ids=["mixed-items", "empty-item"])
def test_branch_without_one_item_is_still_bound_by_its_exact_head_intent(
    store: SqliteRunStore, other_item: str
) -> None:
    """A later run naming another item no longer hides the head's own claim.

    This branch used to skip the claim and name the newest run, a reviewer
    with no route profile, which refused the head as missing ancestor
    evidence for good (#3067).
    """
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40)
    _reviewer_with_item(store, "new-review", other_item, 3)

    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )


def test_branch_run_overflow_refuses_before_intent_binding(
    store: SqliteRunStore,
) -> None:
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    _intent_on_real_store(store, "producer", head_sha="f" * 40)
    for index in range(32):
        _unattested_reviewer_on_real_store(store, f"review-{index}", 3)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == "publication_lineage_too_deep"


def test_intent_row_without_a_payload_refuses() -> None:
    store = _LegacyReviewEvidenceStore()
    store.events.append(
        SimpleNamespace(
            event="publication_intent",
            actor_type="policy",
            actor_id="host-publication",
            run_id="producer",
            item_id="7",
        )
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(  # type: ignore[arg-type]
            store, branch="agent/issue-7", head_sha="f" * 40
        )

    assert raised.value.reason == "publication_evidence_unavailable"


def test_bounded_issuer_identity_never_clears_exposure_lineage(store):
    from kew.credential_issuer import BoundedRunKey
    from kew.readiness import RealizedRouteProfile

    key = BoundedRunKey("run-secret-1944", "a" * 64, 1.0, "2099-01-01T00:00:00Z")
    _run(store, "bounded-parent")
    profile = compose_realized_profile(
        run_id="bounded-parent",
        attempt_number=1,
        route=compose_route_profile(
            runner="opencode", requested_model="openrouter/test"
        ),
        boundary=RealizedLaunchBoundary(
            containment=Containment.CONTAINED,
            image_id="sha256:" + "a" * 64,
            auth_path="env:OPENROUTER_API_KEY",
        ),
        credential_issuer=key.identity,
    )
    store.record_route_profile(profile)
    restored = RealizedRouteProfile.from_json(profile.to_json())
    assert restored.credential_issuer.value == key.identity
    assert restored.fingerprint == profile.fingerprint
    with pytest.raises(CredentialPublicationRefused) as caught:
        require_publication_credentials(
            store,
            run_id="new-run",
            producer_run_id="bounded-parent",
            published_head=_UNSCANNED_HEAD,
        )
    assert caught.value.reason == "credential_scan_missing"


@pytest.mark.parametrize("run_kind", ["ci_fix", "review"])
def test_branch_scope_requires_explicit_matching_evidence(store, monkeypatch, run_kind):
    from dataclasses import replace
    from unittest.mock import Mock

    from kew import publication_credentials
    from kew.audit import emit

    branch = "agent/issue-7"
    _run(store, "producer", status="success")
    store.save(
        replace(
            store.get("producer"),
            item_id="" if run_kind == "ci_fix" else "99",
            run_kind=run_kind,
        )
    )
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    fallback = Mock(return_value="legacy")
    monkeypatch.setattr(publication_credentials, "_legacy_branch_producer", fallback)

    def intent(head, scope, key=branch, actor="host-publication", run_id="producer"):
        emit(
            store,
            "publication_intent",
            actor=Actor("policy", actor),
            run_id=run_id,
            item_id=key,
            payload={"scope": scope, "branch": branch, "commit_sha": head},
        )

    intent("a" * 40, "branch")
    assert (
        publication_producer_for_head(store, branch=branch, head_sha="a" * 40)
        == "producer"
    )
    fallback.assert_not_called()
    for index, kwargs in enumerate(
        [
            {"scope": "item"},
            {"scope": None},
            {"scope": "branch", "key": "99"},
            {"scope": "branch", "actor": "other"},
        ],
        start=1,
    ):
        head = f"{index:040x}"
        intent(head, **kwargs)
        assert (
            publication_producer_for_head(store, branch=branch, head_sha=head)
            == "legacy"
        )
    assert fallback.call_count == 4
    # Valid branch claims must still identify an existing row on this branch.
    intent("b" * 40, "branch", run_id="missing")
    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch=branch, head_sha="b" * 40)
    assert raised.value.reason == "publication_evidence_unavailable"


def test_item_branch_never_accepts_a_branch_scoped_intent(store, monkeypatch):
    from unittest.mock import Mock

    from kew import publication_credentials
    from kew.audit import emit

    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    # Deliberately put branch scope under the item key to check both dimensions.
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="producer",
        item_id="7",
        payload={
            "scope": "branch",
            "branch": "agent/issue-7",
            "commit_sha": "a" * 40,
        },
    )
    fallback = Mock(return_value="legacy")
    monkeypatch.setattr(publication_credentials, "_legacy_branch_producer", fallback)
    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="a" * 40)
        == "legacy"
    )
    fallback.assert_called_once()


@pytest.mark.parametrize("legacy_payload", [False, True], ids=["scoped", "legacy"])
@pytest.mark.parametrize(
    ("other_scope", "other_branch"),
    [("branch", "7"), ("item", "7"), ("branch", "agent/issue-7")],
)
def test_intent_window_counts_only_matching_scope_and_branch(
    store, legacy_payload, other_scope, other_branch
):
    from kew.audit import emit

    branch = "agent/issue-7"
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="producer",
        item_id="7",
        payload={
            "branch": branch,
            "commit_sha": "f" * 40,
            **({} if legacy_payload else {"scope": "item"}),
        },
    )
    # Trackerless branch "7" shares the item key. Its newer rows must not
    # exhaust the item branch's evidence window or hide its older claim.
    for index in range(32):
        emit(
            store,
            "publication_intent",
            actor=Actor("policy", "host-publication"),
            item_id="7",
            payload={
                "branch": other_branch,
                "scope": other_scope,
                "commit_sha": f"{index:040x}",
            },
        )
    assert len(store.list_audit_events(event="publication_intent", item_id="7")) == 33
    assert (
        publication_producer_for_head(store, branch=branch, head_sha="f" * 40)
        == "producer"
    )


_ROUND10_HASH = "a" * 64


def _round10_issuer_event(
    store,
    *,
    phase="issuance",
    run_id="issued",
    attempt=1,
    key_hash=_ROUND10_HASH,
    issuer="openrouter",
    revoked=None,
    reason=None,
    actor_id="credential-issuer",
):
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="sandbox_resolved",
        actor_type="policy",
        actor_id=actor_id,
        run_id=run_id,
        payload={
            "phase": phase,
            "attempt_number": attempt,
            "credential_issuer": {
                "issuer": issuer,
                "key_hash": key_hash,
                "revoked": revoked,
                "reason": reason,
            },
        },
    )


def _round10_issued_profile(store, *, attempt=1, auth_path=None):
    _run(store, "issued", attempts=attempt)
    _profile(
        store,
        "issued",
        attempt,
        containment=Containment.CONTAINED,
        auth_path=auth_path or "issued:openrouter:" + _ROUND10_HASH,
    )


@pytest.mark.parametrize("admission", [True, False])
@pytest.mark.parametrize(
    "mismatch", ["missing", "hash", "run", "attempt", "issuer", "actor", "phase"]
)
def test_round10_issued_evidence_matches_every_identity_field(
    store, admission, mismatch
):
    _round10_issued_profile(store)
    if mismatch != "missing":
        overrides = {
            "hash": {"key_hash": "b" * 64},
            "run": {"run_id": "another-run"},
            "attempt": {"attempt": 2},
            "issuer": {"issuer": "another-issuer"},
            "actor": {"actor_id": "worker"},
            "phase": {"phase": "inherited"},
        }[mismatch]
        _round10_issuer_event(store, **overrides)
    with pytest.raises(CredentialPublicationRefused) as raised:
        if admission:
            require_publication_launch_credentials(
                store, run_id="issued", run_kind="dispatch", attempt_number=1
            )
        else:
            require_publication_credentials(store, run_id="issued")
    assert raised.value.reason == "issued_key_evidence_missing"


@pytest.mark.parametrize("mode", ["canonical", "checkout"])
@pytest.mark.parametrize(
    "revocation",
    [None, False, "wrong_hash", "wrong_run", "wrong_issuer", "wrong_actor"],
)
def test_round10_publication_holds_until_matching_revocation_then_releases(
    store, tmp_path, monkeypatch, mode, revocation
):
    from unittest.mock import Mock

    from kew import dispatch_core
    from kew.publication_canonical import PublicationHeld

    _round10_issued_profile(store)
    _round10_issuer_event(store)
    if revocation is not None:
        overrides = {
            False: {"revoked": False, "reason": "issuer unavailable"},
            "wrong_hash": {"key_hash": "b" * 64},
            "wrong_run": {"run_id": "another-run"},
            "wrong_issuer": {"issuer": "another-issuer"},
            "wrong_actor": {"actor_id": "worker"},
        }[revocation]
        _round10_issuer_event(
            store, **{"phase": "revocation", "revoked": True, **overrides}
        )
    publish = Mock()
    monkeypatch.setattr(dispatch_core, "_canonical_push_branch", publish)
    monkeypatch.setattr(dispatch_core, "_checkout_push_branch", publish)
    # The real shared gate runs before mode selection and either host publisher.
    kwargs = {
        "store": store,
        "run_id": "issued",
        "publish": mode,
        "dangerously_skip_containment": mode == "checkout",
    }
    require_publication_launch_credentials(
        store, run_id="issued", run_kind="dispatch", attempt_number=1
    )
    with pytest.raises(PublicationHeld) as raised:
        dispatch_core._git_push_branch("agent/issue-7", tmp_path, **kwargs)
    assert raised.value.reason == "issued_key_unrevoked"
    expected = (
        "issuer unavailable" if revocation is False else "revocation evidence missing"
    )
    assert raised.value.issuer_reason == expected
    assert raised.value.flags == ("issued_key_unrevoked", expected)
    publish.assert_not_called()
    _round10_issuer_event(store, phase="revocation", revoked=True)
    dispatch_core._git_push_branch("agent/issue-7", tmp_path, **kwargs)
    publish.assert_called_once()


def test_round10_revoked_issued_family_releases_and_ambient_family_still_refuses(store):
    from kew.credential_issuer import BoundedRunKey

    key = BoundedRunKey("secret", _ROUND10_HASH, 1.0, "2030-01-01T00:00:00Z")
    _round10_issued_profile(store, auth_path=key.auth_path)
    _round10_issuer_event(store)
    _round10_issuer_event(store, phase="revocation", revoked=True)
    require_publication_credentials(store, run_id="issued")
    assert key.auth_path == "issued:openrouter:" + _ROUND10_HASH
    _run(store, "ambient")
    _profile(
        store,
        "ambient",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:OPENROUTER_API_KEY",
    )
    require_publication_launch_credentials(
        store, run_id="ambient", run_kind="dispatch", attempt_number=1
    )
    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(
            store, run_id="ambient", published_head=_UNSCANNED_HEAD
        )
    assert raised.value.reason == "credential_scan_missing"


def test_round10_retry_admission_keeps_shared_key_live_until_run_publication(
    store, monkeypatch
):
    from unittest.mock import Mock

    from kew import dispatch_core
    from kew.credential_issuer import BoundedRunKey
    from kew.publication_canonical import PublicationHeld

    _round10_issued_profile(store)
    _round10_issuer_event(store)
    _round10_issued_profile(store, attempt=2)
    key = BoundedRunKey("secret", _ROUND10_HASH, 1.0, "2030-01-01T00:00:00Z")
    run = SimpleNamespace(
        store=store,
        credential=key,
        run_id="issued",
        run_kind="dispatch",
        transport=SimpleNamespace(
            resolved=SimpleNamespace(sidecar=False, mode="direct")
        ),
    )
    monkeypatch.setattr(
        dispatch_core, "_attempt_route_profile", Mock(return_value=None)
    )
    monkeypatch.setattr(
        dispatch_core, "_tracked_runner_name", Mock(return_value="opencode")
    )
    monkeypatch.setattr(dispatch_core, "_record_sidecar_admission_evidence", Mock())
    dispatch_core._admit_attempt_route_profile(run, 2)
    events = store.list_audit_events(event="sandbox_resolved", run_id="issued")
    assert any(event.payload.get("attempt_number") == 2 for event in events)
    with pytest.raises(PublicationHeld):
        require_publication_credentials(store, run_id="issued")
    _round10_issuer_event(store, phase="revocation", revoked=True)
    require_publication_credentials(store, run_id="issued")


def test_round11_publication_retry_reconciles_every_ancestor_key(
    store, tmp_path, monkeypatch
):
    from functools import partial
    from unittest.mock import Mock

    from kew import credential_issuer, dispatch_core, run_credentials

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    http = Mock(return_value=(503, b"unavailable"))
    monkeypatch.setattr(
        credential_issuer,
        "revoke_run_key",
        partial(credential_issuer.revoke_run_key, http=http),
    )
    for run_id, key_hash in (("producer", "a" * 64), ("continuation", "b" * 64)):
        _run(store, run_id)
        key = credential_issuer.BoundedRunKey(
            "secret", key_hash, 1, "2099-01-01T00:00:00Z"
        )
        _profile(
            store, run_id, 1, containment=Containment.CONTAINED, auth_path=key.auth_path
        )
        run_credentials._record(store, run_id, key, phase="issuance")
        run_credentials._revoke(store, run_id, key)
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:7",
        "7",
        "producer",
        "agent/issue-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            "agent/issue-7",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:7",
                "7",
                "producer",
                "agent/issue-7",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    _attest_claim(store, worktree=worktree, continuation_run_id="continuation")
    # Both retry projections become due; use an old durable deadline without sleeping.
    store._conn.execute(
        "UPDATE meta SET value=json_set(value, '$.next_attempt_at', "
        "'2000-01-01T00:00:00+00:00')"
        " WHERE key LIKE 'run_key_revocation:%'"
    )
    store._conn.commit()
    http.return_value = (200, b'{"deleted":true}')
    publish = Mock()
    monkeypatch.setattr(dispatch_core, "_canonical_push_branch", publish)
    dispatch_core._git_push_branch(
        "agent/issue-7", tmp_path, store=store, run_id="continuation"
    )
    assert http.call_count == 4
    publish.assert_called_once()
    require_publication_credentials(store, run_id="continuation")


@pytest.mark.parametrize("count", [32, 33])
@pytest.mark.parametrize("lookup", ["producer", "remediation"])
def test_round15_replacement_refusals_do_not_exhaust_history(store, count, lookup):
    from kew.publication_credentials import publication_branch_runs
    from kew.review import _remediation_row_branch

    _run(store, "producer", status="success")
    for index in range(count):
        _prelaunch_refusal(store, f"replacement-{index}")
        row = store.get(f"replacement-{index}")
        store.save(replace(row, error_category="reservation_replaced", exit_code=1))
    assert [
        row.run_id for row in publication_branch_runs(store, branch="agent/issue-7")
    ] == ["producer"]
    if lookup == "producer":
        assert (
            publication_producer_for_head(
                store, branch="agent/issue-7", head_sha="f" * 40
            )
            == "producer"
        )
    else:
        assert _remediation_row_branch(store, "7", "agent/issue-7") == "agent/issue-7"


@pytest.mark.parametrize(
    "evidence",
    [
        "pgid",
        "container_name",
        "session_id",
        "log_file",
        "pr_url",
        "duration_seconds",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "estimated_cost_usd",
        "classifier_overhead_input_tokens",
        "classifier_overhead_output_tokens",
        "classifier_overhead_cost_usd",
        "steps_to_first_edit",
        "tokens_to_first_edit",
        "profile",
        "binding",
        "continuation",
        "finished_later",
        "unfinished",
        "unknown_category",
    ],
)
def test_round15_replacement_refusal_with_launch_evidence_counts(store, evidence):
    from kew import publication_credentials as credentials
    from kew.audit import Actor, emit

    _prelaunch_refusal(store, "replacement")
    row = replace(
        store.get("replacement"), error_category="reservation_replaced", exit_code=1
    )
    store.save(row, update_ownership=True)
    assert credentials.publication_branch_runs(store, branch=row.branch) == []
    if evidence == "profile":
        _profile(
            store, row.run_id, 1, containment=Containment.NATIVE, auth_path="cli-login"
        )
    elif evidence == "binding":
        emit(
            store,
            "dispatch_reservation_bound",
            actor=Actor("policy", "dispatch"),
            run_id=row.run_id,
        )
    elif evidence == "continuation":
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref="alpha",
            item_id="7",
            original_run_id="producer",
            continuation_run_id=row.run_id,
            branch=row.branch,
            worktree_identity="/test/checkout",
            generation=1,
            run_id=row.run_id,
        )
    else:
        values = {
            "pgid": 7,
            "container_name": "worker",
            "session_id": "session",
            "log_file": Path("launch.log"),
            "pr_url": "https://example.test/pull/7",
            "finished_later": row.started_at.replace(year=2027),
            "unfinished": None,
            "unknown_category": "other_refusal",
        }
        field = {
            "finished_later": "finished_at",
            "unfinished": "finished_at",
            "unknown_category": "error_category",
        }.get(evidence, evidence)
        row = replace(row, **{field: values.get(evidence, 1)})
        store.save(row, update_ownership=True)
    assert [
        r.run_id for r in credentials.publication_branch_runs(store, branch=row.branch)
    ] == [row.run_id]


@pytest.mark.parametrize("timing", ["equal", "before", "no_start"])
def test_round15_prelaunch_timing_accepts_only_proven_no_elapsed_work(store, timing):
    from datetime import timedelta

    from kew import publication_credentials as credentials

    _prelaunch_refusal(store, "replacement")
    row = replace(
        store.get("replacement"), error_category="reservation_replaced", exit_code=2
    )
    if timing == "before":
        row.finished_at -= timedelta(seconds=1)
    elif timing == "no_start":
        row.started_at = None
    predicate = getattr(
        credentials,
        "prelaunch_refusal_without_launch",
        credentials.prelaunch_containment_refusal,
    )
    assert predicate(store, row)


def _round17_real_gate_refusal(store, monkeypatch, config, index):
    from unittest.mock import Mock

    from kew import dispatch_core as core
    from kew.runners import get_runner
    from kew.runners.registry import ResolvedRoute

    categories = (
        "requeued_over_cap",
        "github_unreachable",
        "fleet_stopped",
        "transport_admission_refused",
    )
    category = categories[index % len(categories)]
    run_id = f"refusal-{index}"
    assert store.reserve_epic_node_item("alpha", "7", run_id)
    with monkeypatch.context() as patcher:
        patcher.setattr(core, "_capture_root_checkout_state", lambda *_a, **_k: None)
        patcher.setattr(
            core, "_warn_if_scheduler_does_not_own_cap_warning", lambda _: None
        )
        patcher.setattr(core, "_check_existing_pr", lambda *_: (False, None))
        patcher.setattr(core, "reset_issue_to_ready", Mock())
        patcher.setattr(
            core,
            "_worker_cap_reached",
            lambda *_: (category == "requeued_over_cap", 1, 1),
        )
        patcher.setattr(
            core,
            "_check_github_connectivity",
            lambda _: category != "github_unreachable",
        )
        patcher.setattr(
            core,
            "resolve_runner",
            lambda *_a, **_k: ResolvedRoute(
                get_runner("claude-code"), "claude-sonnet-4-6"
            ),
        )
        patcher.setattr(core, "_preflight_pricing", lambda *_: None)

        def transport(*_):
            raise core.TransportAdmissionError("refused", reason="unavailable")

        patcher.setattr(core, "_require_dispatch_transport_admission", transport)
        if category == "fleet_stopped":
            store.set_fleet_stopped("test")
        outcome = core.execute_dispatch(
            issue={"number": 7, "title": "t"},
            config=config,
            store=store,
            epic_ref="alpha",
            item_id="7",
            approved=True,
            expected_reservation_run_id=run_id,
            expected_reservation_generation=run_id,
        )
        store.clear_fleet_stopped()
    assert outcome.category == category
    store.release_epic_node_item("7", run_id=run_id)
    row = store.get(run_id)
    assert row.error_category == category
    return row


def test_round17_mixed_real_gate_refusals_do_not_exhaust_producer_history(
    store, monkeypatch, tmp_path
):
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _run(store, "producer", status="success")
    _profile(
        store, "producer", 1, containment=Containment.NATIVE, auth_path="cli-login"
    )
    for index in range(32):
        _round17_real_gate_refusal(store, monkeypatch, config, index)
    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "producer"
    )
    assert all(
        row.pre_admission_refusal
        for row in store.list_by_item("7")
        if row.run_id != "producer"
    )


@pytest.mark.parametrize("evidence", ["usage", "route", "binding"])
def test_round17_marked_refusal_with_launch_evidence_counts(
    store, monkeypatch, tmp_path, evidence
):
    import kew.publication_credentials as credentials
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    for index in range(32):
        _run(store, f"producer-{index}", status="success")
    row = _round17_real_gate_refusal(store, monkeypatch, config, 0)
    assert getattr(row, "pre_admission_refusal", False) is True
    if evidence == "usage":
        row = replace(row, total_tokens=1)
        store.save(row)
    elif evidence == "route":
        _profile(
            store, row.run_id, 1, containment=Containment.NATIVE, auth_path="cli-login"
        )
    else:
        store.append_audit_event(
            timestamp=datetime.now(UTC).isoformat(),
            event="dispatch_reservation_bound",
            run_id=row.run_id,
            actor_type="policy",
            actor_id="dispatch-admission",
        )
    assert not credentials.prelaunch_refusal_without_launch(store, row)
    with pytest.raises(CredentialPublicationRefused) as refused:
        publication_producer_for_head(store, branch=row.branch, head_sha="f" * 40)
    assert refused.value.reason == "publication_lineage_too_deep"


@pytest.mark.parametrize("launch", ["host", "native", "uncontained", "row_missing"])
def test_issued_key_holds_publication_regardless_of_containment(store, launch):
    """Re-audit F8: a host run whose key revocation failed must not publish."""
    from kew.publication_credentials import IssuedKeyPublicationHeld

    if launch != "row_missing":
        _run(store, "issued")
        _profile(
            store,
            "issued",
            1,
            containment=Containment(launch),
            auth_path="issued:openrouter:" + _ROUND10_HASH,
        )
    _round10_issuer_event(store)
    _round10_issuer_event(
        store, phase="revocation", revoked=False, reason="issuer unavailable"
    )
    with pytest.raises(IssuedKeyPublicationHeld) as raised:
        require_publication_credentials(store, run_id="issued")
    assert raised.value.issuer_reason == "issuer unavailable"
    assert raised.value.key_hash == _ROUND10_HASH
    # Launch admission precedes revocation of the run's own shared key.
    require_publication_launch_credentials(
        store, run_id="issued", run_kind="dispatch", attempt_number=1
    )
    _round10_issuer_event(store, phase="revocation", revoked=True)
    require_publication_credentials(store, run_id="issued")


@pytest.mark.parametrize("launch", ["host", "native", "uncontained"])
def test_driver_dispatched_child_publishes_under_its_live_driver_key(
    tmp_path, monkeypatch, launch
):
    """PR #1984 review round 1: the driver revokes its child's key only after
    the child exits, so the child's own publication keeps the treatment it had
    before the issuance gate. The same key named by a later producer is gated.
    """
    from kew import credential_issuer
    from kew.config import DispatchConfig
    from kew.graph.driver.credentials import issue_driver_key
    from kew.publication_credentials import IssuedKeyPublicationHeld

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1984")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        Mock(
            return_value=credential_issuer.BoundedRunKey(
                "run-secret-1984", _ROUND10_HASH, 25, "2099-01-01T00:00:00Z"
            )
        ),
    )
    revoke = Mock()
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    # The driver's store carries a ledger identity, as issuance requires.
    store = SqliteRunStore(str(tmp_path / "driver.db"), project_root=tmp_path)
    key = issue_driver_key(store, DispatchConfig(), "child", "7")
    _run(store, "child", status="pending")
    _profile(
        store, "child", 1, containment=Containment(launch), auth_path=key.auth_path
    )
    require_publication_credentials(store, run_id="child")
    with pytest.raises(IssuedKeyPublicationHeld) as raised:
        require_publication_credentials(store, run_id="child", producer_run_id="child")
    assert raised.value.key_hash == _ROUND10_HASH
    revoke.assert_not_called()


@pytest.mark.parametrize("outcome", ["refused", "unavailable", "pending", "expired"])
def test_driver_key_without_a_live_owner_holds_the_child(
    tmp_path, monkeypatch, outcome
):
    """PR #1984 review round 2: the driver-key exemption ends at the first
    revocation record for the key, or at its expiry, never lasting forever.
    """
    from kew import credential_issuer, publication_credentials, run_credentials
    from kew.config import DispatchConfig
    from kew.graph.driver.credentials import issue_driver_key
    from kew.publication_credentials import IssuedKeyPublicationHeld

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1984")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        Mock(
            return_value=credential_issuer.BoundedRunKey(
                "run-secret-1984", _ROUND10_HASH, 25, "2099-01-01T00:00:00Z"
            )
        ),
    )
    failure = {
        "refused": credential_issuer.IssuerRefused(),
        "unavailable": OSError("synthetic issuer outage"),
    }.get(outcome)
    revoke = Mock(side_effect=failure)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    store = SqliteRunStore(str(tmp_path / "driver.db"), project_root=tmp_path)
    key = issue_driver_key(store, DispatchConfig(), "child", "7")
    _run(store, "child", status="pending")
    _profile(store, "child", 1, containment=Containment.HOST, auth_path=key.auth_path)
    require_publication_credentials(store, run_id="child")  # live driver key
    if outcome in {"refused", "unavailable"}:
        run_credentials._revoke(store, "child", key)
        revoke.assert_called_once_with(_ROUND10_HASH, issued_key="run-secret-1984")
    elif outcome == "pending":
        _round10_issuer_event(store, run_id="child", phase="revocation_pending")
    else:

        class _AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2099, 1, 1, 0, 0, 1, tzinfo=tz)

        monkeypatch.setattr(publication_credentials, "datetime", _AfterExpiry)
    with pytest.raises(IssuedKeyPublicationHeld) as raised:
        require_publication_credentials(store, run_id="child")
    assert raised.value.key_hash == _ROUND10_HASH


def _merge_main_marker(store: SqliteRunStore, run_id: str, hour: int) -> None:
    """The driver's merge-main bookkeeping row: no agent, no route profile."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            error_category="base_integration_step",
            run_kind="resolve",
            title="merge-main step at cccccccccccc",
        )
    )


def _branch_run_profile(store: SqliteRunStore, run_id: str, *, contained: bool) -> None:
    """One attempt profile: contained under a provider key, or native."""
    if contained:
        _profile(
            store,
            run_id,
            1,
            containment=Containment.CONTAINED,
            auth_path="env:SYNTHETIC_PROVIDER_KEY",
        )
    else:
        _profile(
            store, run_id, 1, containment=Containment.NATIVE, auth_path="cli-login"
        )


def _merge_main_resolve(
    store: SqliteRunStore,
    run_id: str,
    hour: int,
    *,
    contained: bool = False,
    branch: str = "agent/issue-7",
) -> None:
    """A drive resolve run that recorded no tracker item, and pushed the head."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="",
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-resolve-{run_id}" if contained else None,
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="resolve",
        )
    )
    _branch_run_profile(store, run_id, contained=contained)


def _pr_number_review(
    store: SqliteRunStore, run_id: str, hour: int, *, contained: bool = False
) -> None:
    """A hand-started ``kew review 40``: its ``item_id`` is the PR number."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=40,
            item_id="40",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 13, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-review-{run_id}" if contained else None,
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind="review",
        )
    )
    _branch_run_profile(store, run_id, contained=contained)


def _head_claim(
    store: SqliteRunStore,
    run_id: str,
    *,
    head_sha: str = "f" * 40,
    scope_key: str = "agent/issue-7",
    actor: Actor | None = None,
    event: str = "publication_intent",
) -> None:
    """A recorded claim on a head of ``agent/issue-7``.

    The scope follows the key the way the host publisher writes it: the branch
    itself is branch scope, anything else is a tracker item.
    """
    from kew.audit import emit

    emit(
        store,
        event,
        actor=actor or Actor("policy", "host-publication"),
        run_id=run_id,
        issue_num=7,
        item_id=scope_key,
        payload={
            "branch": "agent/issue-7",
            "scope": "branch" if scope_key == "agent/issue-7" else "item",
            "commit_sha": head_sha,
            "tree_sha": "d" * 40,
            "parent_sha": "c" * 40,
        },
    )


def test_merge_main_marker_never_stands_in_for_the_head_producer(
    store: SqliteRunStore,
) -> None:
    """A head with no claim is not refused over the newer marker row.

    The branch carries the tracker item's dispatch, a standalone review keyed
    by its PR number, and a resolve run that names no item at all, all native,
    and no claim names the head, so the newest-run fallback decides. The
    driver's merge-main marker is newer than the resolve run, but it is
    bookkeeping with no route profile: naming it refused every later launch as
    missing ancestor evidence, forever.
    """
    _run(store, "dispatch", status="success")
    _branch_run_profile(store, "dispatch", contained=False)
    _pr_number_review(store, "pr-review", 2)
    _merge_main_resolve(store, "resolve", 4)
    _merge_main_marker(store, "marker", 5)

    producer = publication_producer_for_head(
        store, branch="agent/issue-7", head_sha="f" * 40
    )

    assert producer == "resolve"
    # The remediation's admission checks the bound producer's credentials next.
    ensure_publication_credentials(store, run_id=producer, producer_run_id=producer)


def _contained_mixed_branch(store: SqliteRunStore) -> None:
    """``agent/issue-7`` as the drive left #3040's branch (#3067), unclaimed.

    Runs name three items: the tracker item ``7`` (the drive's dispatch), the
    PR number ``40`` (a hand-started ``kew review``), and ``''`` (the drive's
    merge-main resolve run, which pushed the head). Every agent run is
    contained, so the newest-run fallback refuses the branch outright, and the
    newest row is the profile-less merge-main marker.
    """
    _run(store, "dispatch", status="success", container_name="kew-7")
    _branch_run_profile(store, "dispatch", contained=True)
    _pr_number_review(store, "pr-review", 2, contained=True)
    _merge_main_resolve(store, "resolve", 4, contained=True)
    _merge_main_marker(store, "marker", 5)


def test_a_claim_binds_its_head_on_a_branch_whose_runs_name_mixed_items(
    store: SqliteRunStore,
) -> None:
    """The host's claim on the exact head names its producer, whatever else ran.

    The acceptance case of #3067: the resolve run's branch-scope
    ``publication_intent`` names the head, so the lookup returns that run, and
    the remediation admission (the bound producer's credential check) admits
    the launch. Before, the mixed items skipped the claim and the fallback
    refused the head forever.
    """
    _contained_mixed_branch(store)
    _head_claim(store, "resolve")

    producer = publication_producer_for_head(
        store, branch="agent/issue-7", head_sha="f" * 40
    )

    assert producer == "resolve"
    ensure_publication_credentials(store, run_id=producer, producer_run_id=producer)


def test_an_operator_attestation_binds_its_head_on_a_mixed_branch(
    store: SqliteRunStore,
) -> None:
    """A named human's ``publication_head_attested`` rescues the node too."""
    _contained_mixed_branch(store)
    _head_claim(
        store,
        "resolve",
        scope_key="7",
        actor=Actor("human", "operator"),
        event="publication_head_attested",
    )

    assert (
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)
        == "resolve"
    )


def _second_claim_on_the_head(store: SqliteRunStore) -> None:
    _head_claim(store, "resolve")
    _head_claim(store, "dispatch", scope_key="7")


def _claim_on_another_head(store: SqliteRunStore) -> None:
    _head_claim(store, "resolve", head_sha="e" * 40)


def _claim_from_a_run_on_another_branch(store: SqliteRunStore) -> None:
    _merge_main_resolve(store, "elsewhere", 4, contained=True, branch="agent/issue-8")
    _head_claim(store, "elsewhere")


def _claim_by_a_disallowed_actor(store: SqliteRunStore) -> None:
    _head_claim(store, "resolve", actor=Actor("agent", "codex-operator"))


def _claim_under_an_item_no_branch_run_names(store: SqliteRunStore) -> None:
    _head_claim(store, "resolve", scope_key="8")


def _claim_by_a_run_without_route_profiles(store: SqliteRunStore) -> None:
    _head_claim(store, "marker")


def _no_claim(store: SqliteRunStore) -> None:
    del store


@pytest.mark.parametrize(
    ("record_claims", "reason"),
    [
        (_second_claim_on_the_head, "publication_lineage_ambiguous"),
        (_claim_on_another_head, "publication_lineage_ambiguous"),
        (_claim_from_a_run_on_another_branch, "publication_lineage_ambiguous"),
        (_claim_by_a_disallowed_actor, "publication_lineage_ambiguous"),
        (_claim_under_an_item_no_branch_run_names, "publication_lineage_ambiguous"),
        (
            _claim_by_a_run_without_route_profiles,
            "publication_ancestor_evidence_missing",
        ),
        (_no_claim, "publication_lineage_ambiguous"),
    ],
    ids=[
        "two-claims",
        "other-head",
        "run-off-branch",
        "disallowed-actor",
        "item-not-on-branch",
        "claimer-without-profile",
        "no-claim",
    ],
)
def test_a_mixed_branch_refuses_every_claim_but_one_exact_valid_one(
    store: SqliteRunStore,
    record_claims: Callable[[SqliteRunStore], None],
    reason: str,
) -> None:
    """Mixed ownership reads claims, never a weaker claim than one scope would.

    Two claims, a claim on another head, a claimer that is not a run on this
    branch, a writer that may not claim, a scope no branch run names, and a
    claimer with no route profile are each refused, as is no claim at all:
    the fallback still refuses a branch with contained output.
    """
    _contained_mixed_branch(store)
    record_claims(store)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch="agent/issue-7", head_sha="f" * 40)

    assert raised.value.reason == reason
