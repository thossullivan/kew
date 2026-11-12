"""Fleet-wide placement and slot accounting across the Mac and the VMs (#2398).

The acceptance these cover, in the owner's words:

* AC1 — with ``max_concurrent = 3`` and two runs on gcp, a third is admitted and
  a fourth waits, whatever host the fourth would go to.
* AC2 — ``prefer-gcp`` falls back to local when gcp's ``max_runs`` is reached.
* AC3 — a VM with an in-flight run is never stopped by the idle stop.

AC3's guard is only as good as the WRITE behind it, so the placement path's own
recording of host and instance is covered here and end to end (through a real
``place_dispatch_on_host``) in ``tests/test_hosts_remote_dispatch.py``: the
ledger's ``run_host`` defaults to ``local``, so a column nothing writes reports
every instance free rather than unknown.
"""

from datetime import UTC, datetime, timedelta

import pytest

from kew.config import DispatchConfig
from kew.fleet_placement import (
    LOCAL_SITE,
    FleetOccupancy,
    Placement,
    PlacementNotRecorded,
    RunSite,
    abandoned_after_seconds,
    free_gcp_site,
    gcp_sites,
    instance_is_occupied,
    is_remote,
    place_run,
    placement_lines,
    read_occupancy,
    record_placement,
    resolved_policy,
    run_site,
)
from kew.hosts.errors import HostBusy
from kew.hosts.gcp import GcpProvider
from kew.run_store import RunRecord, SqliteRunStore

STARTED = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _pending(run_id, *, issue_num=1, host="local", instance=None, run_kind="dispatch"):
    """One in-flight run row, placed where the caller says."""
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status="pending",
        exit_code=0,
        started_at=STARTED,
        item_id=str(issue_num),
        run_kind=run_kind,
        run_host=host,
        run_host_instance=instance,
    )


def _config(**overrides):
    """A config with a two-instance gcp pool, plus whatever the test overrides."""
    base = {
        "hosts": {
            "gcp": {
                "project": "proj",
                "zone": "us-central1-a",
                "instances": ["vm-one", "vm-two"],
            }
        }
    }
    base.update(overrides)
    return DispatchConfig(**base)


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


# --------------------------------------------------------------------------
# The ledger records WHERE a run ran
# --------------------------------------------------------------------------


def test_a_run_remembers_its_host_and_instance_across_a_reopen(tmp_path):
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as store:
        store.save(_pending("r1", host="gcp", instance="vm-one"))
    with SqliteRunStore(db) as store:
        record = store.get("r1")
    assert record is not None
    assert run_site(record) == RunSite("gcp", "vm-one")
    assert is_remote(record)


def test_a_pre_placement_row_reads_as_local_not_as_unknown(store):
    """A run written before there was a second host ran on the first one."""
    store.save(_pending("r1"))
    record = store.get("r1")
    assert record is not None
    assert run_site(record) == LOCAL_SITE
    assert not is_remote(record)


def test_a_later_save_cannot_clobber_a_recorded_placement(store):
    """The placement is settled by the launch; every later save rebuilds a record
    that knows nothing about it, so the upsert must preserve it."""
    store.save(_pending("r1", host="gcp", instance="vm-one"))
    finished = _pending("r1")
    finished.status = "success"
    store.save(finished)
    record = store.get("r1")
    assert record is not None
    assert run_site(record) == RunSite("gcp", "vm-one")


def test_set_run_site_records_a_placement_and_drops_an_empty_instance(store):
    store.save(_pending("r1"))
    store.set_run_site("r1", "gcp", "vm-two")
    assert run_site(store.get("r1")) == RunSite("gcp", "vm-two")
    store.set_run_site("r1", "local", "")
    record = store.get("r1")
    assert record.run_host == "local"
    assert record.run_host_instance is None


# --------------------------------------------------------------------------
# Occupancy: one read answers both "is the fleet full" and "is this VM full"
# --------------------------------------------------------------------------


def test_occupancy_counts_every_host_and_groups_the_pool_by_instance(store):
    store.save(_pending("r1", issue_num=1, host="gcp", instance="vm-one"))
    store.save(_pending("r2", issue_num=2, host="gcp", instance="vm-one"))
    store.save(_pending("r3", issue_num=3, host="gcp", instance="vm-two"))
    store.save(_pending("r4", issue_num=4))

    occupancy = read_occupancy(store)
    assert occupancy.total == 4
    assert occupancy.on(RunSite("gcp", "vm-one")) == 2
    assert occupancy.on(RunSite("gcp", "vm-two")) == 1
    assert occupancy.on(LOCAL_SITE) == 1
    # Grouped by INSTANCE, not merely by host: ``max_runs`` is per machine, so
    # "three on gcp" is not a fine enough answer to place a fourth run with.
    assert occupancy.on(RunSite("gcp", "vm-three")) == 0


def test_occupancy_drops_only_a_provably_dead_run(store):
    store.save(_pending("dead", issue_num=1, host="gcp", instance="vm-one"))
    store.save(_pending("unknown", issue_num=2, host="gcp", instance="vm-one"))
    store.save(_pending("alive", issue_num=3, host="gcp", instance="vm-two"))

    verdicts = {"dead": False, "unknown": None, "alive": True}
    occupancy = read_occupancy(store, liveness=lambda run: verdicts[run.run_id])
    # The unprobeable row still holds its slot: prove dead, never infer.
    assert occupancy.on(RunSite("gcp", "vm-one")) == 1
    assert occupancy.total == 2


def test_occupancy_can_exclude_the_run_doing_the_asking(store):
    store.save(_pending("mine", host="gcp", instance="vm-one"))
    occupancy = read_occupancy(store, exclude_run_id="mine")
    assert occupancy.total == 0


def test_a_finished_run_vacates_its_site(store):
    record = _pending("r1", host="gcp", instance="vm-one")
    store.save(record)
    record.status = "success"
    store.save(record)
    assert read_occupancy(store).total == 0


# --------------------------------------------------------------------------
# AC1 — the cap is fleet-wide, whatever host the next run would go to
# --------------------------------------------------------------------------


def test_fleet_cap_defaults_to_the_configured_worker_count():
    assert DispatchConfig(parallel={"max_workers": 2}).fleet_concurrency() == 2


def test_fleet_cap_overrides_the_worker_count_when_set():
    config = DispatchConfig(parallel={"max_workers": 1}, fleet={"max_concurrent": 3})
    assert config.fleet_concurrency() == 3


@pytest.mark.parametrize("fourth_host", ["local", "gcp"])
def test_two_gcp_runs_admit_a_third_and_make_a_fourth_wait(store, fourth_host):
    """AC1. The cap counts agents ANYWHERE, so the host of the next run is not
    part of the question — a count that only saw this machine would have read
    the two VM runs as zero and admitted five agents under a cap of three."""
    cap = _config(fleet={"max_concurrent": 3}).fleet_concurrency()
    assert cap == 3
    for num, instance in ((1, "vm-one"), (2, "vm-two")):
        assert store.try_admit_run(
            _pending(f"gcp-{num}", issue_num=num, host="gcp", instance=instance), cap
        )

    third = _pending("third", issue_num=3)
    assert store.try_admit_run(third, cap) is True

    fourth = _pending(
        "fourth",
        issue_num=4,
        host=fourth_host,
        instance="vm-one" if fourth_host == "gcp" else None,
    )
    assert store.try_admit_run(fourth, cap) is False
    assert store.get("fourth") is None
    assert read_occupancy(store).total == 3


def _owned(run_id, *, host, instance, container=None):
    """A run whose recorded owner is a COMPLETE identity this machine lacks.

    The identity has to be complete — pgid, matching ``owner_pid`` and a birth
    time — or ``_owner_alive`` answers ``None`` for want of evidence and the
    probe returns ``None`` whether the remote guard is there or not. With it
    complete, the local probe reaches the process table, finds no such group and
    answers ``False``: *provably dead*, which is what the guard has to stop.
    """
    run = _pending(run_id, host=host, instance=instance)
    run.pgid = 10**9  # a pid this machine certainly does not have
    run.owner_pid = run.pgid
    run.owner_start_time = "123456"
    run.container_name = container
    return run


def test_a_run_on_a_vm_is_unprobeable_from_here_never_provably_dead():
    """The local probes read this machine's process table and this machine's
    container daemon. A VM's agent is absent from both, and absent is how those
    probes spell *provably dead* — which would retire the run from the fleet
    count and admit a replacement beside it.

    The identical record placed locally pins what the guard is worth: the probe
    must answer ``False`` there, so deleting the guard makes this test fail
    rather than pass twice over an unresolved ``None``.
    """
    from kew.graph.driver.tick import default_run_liveness

    probe = default_run_liveness(DispatchConfig())

    assert probe(_owned("r1", host="gcp", instance="vm-one")) is None
    assert probe(_owned("r2", host="local", instance=None)) is False


def test_a_contained_run_on_a_vm_is_not_dead_because_no_container_is_here(monkeypatch):
    """The contained branch is a second way to reach the same wrong answer: this
    daemon has never heard of the VM's container, and ``absent`` is how the
    inspect probe spells dead. The guard runs ahead of BOTH branches."""
    from kew.graph.driver import tick

    monkeypatch.setattr(tick, "_container_alive", lambda *_args: False)
    probe = tick.default_run_liveness(DispatchConfig())

    remote = _owned("r1", host="gcp", instance="vm-one", container="kew-1")
    local = _owned("r2", host="local", instance=None, container="kew-2")
    assert probe(remote) is None
    assert probe(local) is False


# --------------------------------------------------------------------------
# AC2 — placement policy
# --------------------------------------------------------------------------


def test_local_is_the_default_policy_and_needs_no_cloud_account():
    assert DispatchConfig().sandbox.placement_policy() == "local"
    placed = place_run(DispatchConfig(), FleetOccupancy({}))
    assert placed.site == LOCAL_SITE


def test_placement_falls_back_to_the_sandbox_host_key_n5_shipped():
    config = DispatchConfig(sandbox={"host": "gcp"})
    assert config.sandbox.placement_policy() == "gcp"


def test_a_per_kind_host_pin_outranks_the_fleet_policy():
    config = _config(sandbox={"placement": "gcp", "hosts": {"review": "local"}})
    assert resolved_policy(config, "review") == "local"
    assert resolved_policy(config, "dispatch") == "gcp"
    assert place_run(config, FleetOccupancy({}), run_kind="review").site == LOCAL_SITE


def test_gcp_placement_fills_the_pool_in_order():
    config = _config(sandbox={"placement": "gcp"})
    assert [site.label() for site in gcp_sites(config)] == ["gcp/vm-one", "gcp/vm-two"]
    empty = place_run(config, FleetOccupancy({}))
    assert empty.site == RunSite("gcp", "vm-one")
    one_full = place_run(config, FleetOccupancy({RunSite("gcp", "vm-one"): 1}))
    assert one_full.site == RunSite("gcp", "vm-two")


def test_prefer_gcp_falls_back_to_local_when_max_runs_is_reached():
    """AC2."""
    config = _config(sandbox={"placement": "prefer-gcp"})
    assert config.hosts.gcp.max_runs == 1
    full = FleetOccupancy({RunSite("gcp", "vm-one"): 1, RunSite("gcp", "vm-two"): 1})
    assert free_gcp_site(config, full) is None
    placed = place_run(config, full)
    assert placed.site == LOCAL_SITE
    assert "max_runs = 1" in placed.reason


def test_placement_never_spends_capacity_the_host_transport_refuses(tmp_path):
    """The per-instance capacity placement spends is the capacity a host honours.

    PR #2466 review: ``max_runs = 2`` would have had ``prefer-gcp`` place a
    second run on an instance whose own lock refuses it — a ``HostBusy`` landing
    where the fallback to this machine belongs, because a job holds its instance
    for the whole session and every job there shares one checkout. So the raise
    is refused at load (never clamped at placement, which would spend a number
    the operator did not choose), and with the one legal value a busy instance
    reads full and ``prefer-gcp`` falls back.
    """
    with pytest.raises(ValueError, match="hosts.gcp.max_runs must be 1"):
        _config(
            hosts={
                "gcp": {
                    "project": "proj",
                    "zone": "us-central1-a",
                    "instances": ["vm-one"],
                    "max_runs": 2,
                }
            }
        )

    config = _config(sandbox={"placement": "prefer-gcp"})
    one_each = FleetOccupancy(
        {RunSite("gcp", "vm-one"): 1, RunSite("gcp", "vm-two"): 1}
    )
    assert free_gcp_site(config, one_each) is None
    assert place_run(config, one_each).site == LOCAL_SITE

    # The transport side of the same fact, through the claim a real placement
    # takes: one holder per instance, and the second asker is refused rather
    # than queued behind it.
    settings = config.hosts.gcp.model_copy(update={"instance": "vm-one"})
    holder = GcpProvider(settings, lock_dir=tmp_path)
    second = GcpProvider(settings, lock_dir=tmp_path)
    with holder.claim(), pytest.raises(HostBusy), second.claim():
        pass


def test_a_gcp_pin_waits_rather_than_quietly_landing_on_the_mac():
    """An explicit pin is not a preference: falling back here would undo the
    operator's decision without saying so."""
    config = _config(sandbox={"placement": "gcp"})
    full = FleetOccupancy({RunSite("gcp", "vm-one"): 1, RunSite("gcp", "vm-two"): 1})
    placed = place_run(config, full)
    assert placed.waits
    assert placed.site is None
    assert "every instance is at max_runs" in placed.reason


def test_a_gcp_policy_with_no_instance_configured_says_so():
    config = DispatchConfig(sandbox={"placement": "gcp"})
    placed = place_run(config, FleetOccupancy({}))
    assert placed.waits
    assert "no gcp instance is configured" in placed.reason


def test_prefer_gcp_with_no_cloud_account_runs_on_this_machine():
    """`local` keeps working with no cloud account — and so does prefer-gcp."""
    config = DispatchConfig(sandbox={"placement": "prefer-gcp"})
    assert place_run(config, FleetOccupancy({})).site == LOCAL_SITE


@pytest.mark.parametrize("bad", ["gpc", "prefer_gcp", "cloud"])
def test_an_unknown_placement_is_refused_at_load_not_at_launch(bad):
    with pytest.raises(ValueError, match="sandbox.placement"):
        DispatchConfig(sandbox={"placement": bad})


def test_max_runs_below_one_is_refused():
    with pytest.raises(ValueError, match="max_runs must be at least 1"):
        DispatchConfig(hosts={"gcp": {"max_runs": 0}})


# --------------------------------------------------------------------------
# AC3 — a VM with a run on it is never stopped by the idle stop
# --------------------------------------------------------------------------


def test_an_instance_with_another_in_flight_run_is_occupied(store):
    """Two rows on one instance is a LEDGER state, not a lock state.

    A placement is recorded before its claim is taken, so a second process can
    have this instance's name on a row of its own while the first run finishes —
    and a run queued for the instance holds it with no container to see. The
    question is asked of the ledger for exactly that reason: the lock the
    finishing run holds cannot see either of them.
    """
    store.save(_pending("mine", issue_num=1, host="gcp", instance="vm-one"))
    store.save(_pending("theirs", issue_num=2, host="gcp", instance="vm-one"))
    assert instance_is_occupied(store, instance="vm-one", exclude_run_id="mine")


def test_the_last_run_on_an_instance_leaves_it_free_to_stop(store):
    store.save(_pending("mine", issue_num=1, host="gcp", instance="vm-one"))
    store.save(_pending("elsewhere", issue_num=2, host="gcp", instance="vm-two"))
    assert not instance_is_occupied(store, instance="vm-one", exclude_run_id="mine")


def test_a_stranded_run_stops_pinning_the_vm_up_once_it_is_a_corpse(store):
    """Nothing here can prove a remote container dead, so without an age bound a
    killed run would mean no later release ever arms a stop and the instance
    bills forever."""
    config = _config()
    bound = abandoned_after_seconds(config)
    assert bound == 2 * 60 * config.agent.timeout_minutes

    corpse = _pending("corpse", issue_num=1, host="gcp", instance="vm-one")
    corpse.started_at = STARTED
    store.save(corpse)
    just_before = STARTED + timedelta(seconds=bound - 1)
    just_after = STARTED + timedelta(seconds=bound + 1)
    assert instance_is_occupied(
        store, instance="vm-one", abandoned_after=bound, now=just_before
    )
    assert not instance_is_occupied(
        store, instance="vm-one", abandoned_after=bound, now=just_after
    )


def test_the_fleet_cap_is_never_aged_out_only_the_idle_stop_is(store):
    """Age is inference. The cap's rule is prove dead or keep the slot — over-
    admitting agents is the failure it exists to prevent — so the default read
    counts every pending row however old it is."""
    old = _pending("ancient", issue_num=1, host="gcp", instance="vm-one")
    old.started_at = STARTED
    store.save(old)
    long_after = STARTED + timedelta(days=30)
    assert read_occupancy(store, now=long_after).total == 1


def test_a_naive_stamp_is_aged_as_utc_not_dropped():
    """The ledger refuses a naive stamp, so this is the defensive branch for any
    other row source: read it as UTC, the only timezone a kew writer stamps in.
    Tolerated, never repaired — and never a reason to drop the row."""
    naive = _pending("naive", issue_num=1, host="gcp", instance="vm-one")
    naive.started_at = STARTED.replace(tzinfo=None)

    class _Ledger:
        def list_pending(self):
            return [naive]

    young = instance_is_occupied(
        _Ledger(),
        instance="vm-one",
        abandoned_after=3600,
        now=STARTED + timedelta(minutes=5),
    )
    assert young
    assert not instance_is_occupied(
        _Ledger(),
        instance="vm-one",
        abandoned_after=3600,
        now=STARTED + timedelta(hours=5),
    )


def test_a_queued_run_keeps_its_instance_warm_before_its_container_exists(store):
    """ "Keep warm while runs are queued": an admitted run already has its row and
    its placement, so the instance it is queued for reads occupied."""
    store.save(_pending("queued", issue_num=1, host="gcp", instance="vm-one"))
    assert instance_is_occupied(store, instance="vm-one")


def test_an_unnameable_instance_is_reported_occupied(store):
    """The answer that cannot stop a running agent: a skipped stop costs money,
    a wrong stop costs the run."""
    assert instance_is_occupied(store, instance="   ")


def test_the_idle_stop_is_skipped_while_a_run_is_still_on_the_instance(store, caplog):
    """AC3. End to end through the release path remote dispatch actually uses."""
    from kew.hosts import remote_dispatch

    store.save(_pending("mine", issue_num=1, host="gcp", instance="vm-one"))
    store.save(_pending("theirs", issue_num=2, host="gcp", instance="vm-one"))

    released = []
    dropped = []

    class _Provider:
        address = "vm-one"
        name = "gcp"

        def release(self):
            released.append(True)

        def drop_hold(self):
            dropped.append(True)

    with caplog.at_level("INFO"):
        remote_dispatch._release(_Provider(), store=store, run_id="mine")
    assert released == []
    assert "not arming the idle stop" in caplog.text
    # The stop is the other run's to arm, but this run's lease is given up,
    # or the other run would be refused the instance (#2627).
    assert dropped == [True]


def test_the_last_run_off_an_instance_does_arm_the_idle_stop(store):
    """No VM is left billing: a lone run still arms its own stop."""
    from kew.hosts import remote_dispatch

    store.save(_pending("mine", issue_num=1, host="gcp", instance="vm-one"))

    released = []

    class _Provider:
        address = "vm-one"
        name = "gcp"

        def release(self):
            released.append(True)

    remote_dispatch._release(_Provider(), store=store, run_id="mine")
    assert released == [True]


def test_a_release_with_no_ledger_arms_the_stop_as_it_did_before(store):
    from kew.hosts import remote_dispatch

    released = []

    class _Provider:
        address = "vm-one"
        name = "gcp"

        def release(self):
            released.append(True)

    remote_dispatch._release(_Provider())
    assert released == [True]


# --------------------------------------------------------------------------
# Reporting: one renderer, so the CLI and the console cannot disagree
# --------------------------------------------------------------------------


def test_placement_lines_name_the_host_and_the_instance():
    lines = placement_lines(
        [
            _pending("r1", issue_num=12, host="gcp", instance="vm-one"),
            _pending("r2", issue_num=13, run_kind="review"),
        ]
    )
    assert "#12" in lines[0] and "gcp/vm-one" in lines[0]
    assert "#13" in lines[1] and lines[1].rstrip().endswith("local")


def test_a_placement_reports_the_policy_that_chose_it():
    config = _config(sandbox={"placement": "prefer-gcp"})
    placed = place_run(config, FleetOccupancy({}))
    assert isinstance(placed, Placement)
    assert placed.policy == "prefer-gcp"
    assert not placed.waits


def test_kew_status_names_the_host_and_instance_of_each_in_flight_run(store, capsys):
    """The CLI surface, off the ledger alone — no GitHub call, no cloud call."""
    from kew.cli import _status_in_flight

    store.save(_pending("r1", issue_num=12, host="gcp", instance="vm-one"))
    store.save(_pending("r2", issue_num=13))
    _status_in_flight(store)
    out = capsys.readouterr().out
    assert "In flight:" in out
    assert "gcp/vm-one" in out
    assert "#12" in out and "#13" in out


def test_kew_status_says_nothing_when_nothing_is_in_flight(store, capsys):
    """An empty heading reads as a broken query."""
    from kew.cli import _status_in_flight

    _status_in_flight(store)
    assert capsys.readouterr().out == ""


def test_the_console_route_dimension_carries_the_placement():
    """The console reads the same recorded fact the CLI does, in two flat fields
    so it can group by host without parsing a label."""
    from kew.service.work import _route_dto

    remote = _pending("r1", host="gcp", instance="vm-one")
    remote.runner_name = "claude-code"
    remote.container_name = "kew-1"
    dto = _route_dto(remote)
    assert (dto.host, dto.instance) == ("gcp", "vm-one")
    assert dto.containment == "contained"

    local = _route_dto(_pending("r2"))
    assert (local.host, local.instance) == ("local", "")


def test_every_host_a_run_kind_may_be_pinned_to_is_also_a_placement():
    """What makes :func:`resolved_policy` safe without a fallback: a per-kind pin
    is validated against the hosts list, and every name on it is a policy
    :func:`place_run` carries out. Grow one list without the other and a pinned
    kind would reach the "not a placement kew makes" branch."""
    from kew.config import KNOWN_PLACEMENTS
    from kew.run_store import KNOWN_RUN_HOSTS

    assert KNOWN_RUN_HOSTS <= KNOWN_PLACEMENTS


def test_an_unknown_policy_waits_rather_than_quietly_landing_on_the_mac():
    """Unreachable through config, which refuses one at load. Spelled as a wait
    anyway: a fallback here would be the silent clean default the load-time check
    exists to prevent, one layer further in."""
    config = _config()
    config.sandbox.placement = "somewhere-else"
    placed = place_run(config, FleetOccupancy({}))
    assert placed.waits
    assert "is not a placement kew makes" in placed.reason


def test_the_config_reader_and_the_placement_reader_cannot_disagree():
    """One answer to "what governs this run kind". Two readers — a config one that
    looked only at `host` and a placement one that read the policy — is how an
    operator who set `placement` and left `host` alone would find half of kew
    placing work one way and half the other."""
    config = _config(sandbox={"placement": "prefer-gcp"})
    for kind in ("dispatch", "adversarial_review", "ci_fix"):
        assert config.sandbox.host_for(kind) == resolved_policy(config, kind)
        assert resolved_policy(config, kind) == "prefer-gcp"
    assert resolved_policy(config) == "prefer-gcp"


def test_kew_status_json_carries_the_placement_as_two_flat_fields():
    """So a reader can group on the host without parsing a slash out of a label."""
    from kew.cli import _status_export_worker

    remote = _status_export_worker(
        _pending("r1", issue_num=12, host="gcp", instance="vm-one")
    )
    assert (remote["host"], remote["instance"]) == ("gcp", "vm-one")
    local = _status_export_worker(_pending("r2", issue_num=13))
    assert (local["host"], local["instance"]) == ("local", "")


# --------------------------------------------------------------------------
# The WRITE behind every read: a column nothing writes reads as "local"
# --------------------------------------------------------------------------


def test_record_placement_is_what_makes_the_idle_stop_guard_answer_at_all(store):
    """AC3's guard reads ``run_host``/``run_host_instance``, which default to
    ``local``/NULL — so without a writer it reports every instance FREE and
    arms the stop under a working agent. ``record_placement`` is that writer,
    and the placement path calls it (``place_dispatch_on_host``)."""
    store.save(_pending("r1"))
    assert not instance_is_occupied(store, instance="vm-one")

    record_placement(store, run_id="r1", host="gcp", instance="vm-one")

    assert instance_is_occupied(store, instance="vm-one")
    assert read_occupancy(store).on(RunSite("gcp", "vm-one")) == 1


def test_a_placement_with_no_row_to_write_is_refused_not_dropped(store):
    """The ``UPDATE`` matching nothing is not an error to SQLite, and the ledger
    afterwards is indistinguishable from a run that is still local — so the
    instance would read FREE to the idle-stop guard with a run going onto it.
    Refused on the way in, where it costs a queue wait rather than that run."""
    with pytest.raises(PlacementNotRecorded, match="not-admitted-yet"):
        record_placement(
            store, run_id="not-admitted-yet", host="gcp", instance="vm-one"
        )

    assert store.get("not-admitted-yet") is None
    assert not instance_is_occupied(store, instance="vm-one")


def test_the_refusal_names_the_site_the_placement_was_for(store):
    """What an operator needs off the line: which machine has no holder."""
    with pytest.raises(PlacementNotRecorded, match=r"gcp/vm-one"):
        record_placement(store, run_id="gone", host="gcp", instance="vm-one")


def test_set_run_site_reports_whether_it_wrote_a_row(store):
    """The rowcount record_placement refuses on. Reported rather than dropped:
    nothing downstream can tell an unwritten placement from a local run."""
    store.save(_pending("r1"))
    assert store.set_run_site("r1", "gcp", "vm-one") is True
    assert store.set_run_site("no-such-run", "gcp", "vm-one") is False


def test_the_guard_asks_about_the_host_the_writer_recorded(store):
    """One provider name, written and read. A reader pinned to ``gcp`` while
    the writer stored another host's instance of the same name would find the
    machine free with a run on it."""
    store.save(_pending("r1"))
    record_placement(store, run_id="r1", host="local", instance="vm-one")

    assert not instance_is_occupied(store, instance="vm-one")
    assert instance_is_occupied(store, instance="vm-one", host="local")


# --------------------------------------------------------------------------
# The cap is a number every concurrency reader can be sized from
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1])
def test_a_fleet_cap_below_one_is_refused_at_load_not_at_pool_construction(bad):
    """0 does not mean "unbounded" here. It is the number the scheduler sizes
    its ProcessPoolExecutor from (which refuses 0), the capacity the drain
    reports, and the epic driver's dispatch budget — so a 0 the admission gate
    alone read as unbounded would halt the fleet everywhere else."""
    with pytest.raises(ValueError, match="fleet.max_concurrent"):
        DispatchConfig(fleet={"max_concurrent": bad})


def test_the_scheduler_always_has_a_poolable_number_of_slots():
    from kew.scheduler import DispatchScheduler

    scheduler = DispatchScheduler(config=DispatchConfig(fleet={"max_concurrent": 1}))
    assert scheduler.max_workers == 1
    assert scheduler.has_capacity()
    assert scheduler.available_slots == 1


# --------------------------------------------------------------------------
# The attested ceiling is the ceiling in force
# --------------------------------------------------------------------------


def test_the_policy_snapshot_attests_the_cap_that_is_actually_enforced():
    """Raising the real cap must move ``policy_hash``: an audit event that
    attests ``parallel_max_workers`` while ``[fleet] max_concurrent`` is what
    the admission gate reads attests a ceiling that is not in force."""
    from kew.policy import HOOK_ABSENT, policy_hash, policy_snapshot

    base = DispatchConfig(parallel={"max_workers": 3})
    raised = DispatchConfig(parallel={"max_workers": 3}, fleet={"max_concurrent": 10})

    assert policy_snapshot(base, HOOK_ABSENT)["limits"]["fleet_max_concurrent"] == 3
    assert policy_snapshot(raised, HOOK_ABSENT)["limits"]["fleet_max_concurrent"] == 10
    assert policy_hash(policy_snapshot(base, HOOK_ABSENT)) != policy_hash(
        policy_snapshot(raised, HOOK_ABSENT)
    )


# --------------------------------------------------------------------------
# --workers overrides the cap in force, not a key nothing reads
# --------------------------------------------------------------------------


def test_the_workers_flag_overrides_a_configured_fleet_cap():
    """Documented as the override, so it must not be shadowed by ``[fleet]``."""
    from kew.cli import _apply_workers_override

    config = DispatchConfig(parallel={"max_workers": 1}, fleet={"max_concurrent": 10})
    _apply_workers_override(config, 2)

    assert config.fleet_concurrency() == 2
    assert config.parallel.max_workers == 2
    assert config.fleet.max_concurrent == 2


def test_no_workers_flag_leaves_both_keys_alone():
    from kew.cli import _apply_workers_override

    config = DispatchConfig(parallel={"max_workers": 1}, fleet={"max_concurrent": 10})
    _apply_workers_override(config, None)

    assert config.fleet_concurrency() == 10


# --------------------------------------------------------------------------
# The env layer is above the file for the cap in force, not just for a key
# --------------------------------------------------------------------------


def _toml(tmp_path, body):
    path = tmp_path / "kew.toml"
    path.write_text(body)
    return path


def test_env_workers_are_not_shadowed_by_a_file_fleet_cap(tmp_path):
    """``load_config`` documents env > DB > toml, and ``fleet_concurrency`` is
    the ONE cap enforced — so an operator throttling the fleet with
    ``KEW_MAX_WORKERS`` over a kew.toml that sets ``[fleet] max_concurrent``
    must not be silently ignored by exactly that file."""
    from kew.config import load_config

    path = _toml(
        tmp_path, "[parallel]\nmax_workers = 4\n\n[fleet]\nmax_concurrent = 10\n"
    )
    config = load_config(path, env={"KEW_MAX_WORKERS": "1"})

    assert config.fleet_concurrency() == 1
    assert config.parallel.max_workers == 1


def test_an_explicit_fleet_env_var_still_wins_over_the_worker_count(tmp_path):
    """Same layer, and it names the fleet cap: the specific key decides."""
    from kew.config import load_config

    path = _toml(tmp_path, "[parallel]\nmax_workers = 4\n")
    config = load_config(
        path, env={"KEW_MAX_WORKERS": "1", "KEW_FLEET_MAX_CONCURRENT": "6"}
    )

    assert config.fleet_concurrency() == 6
    assert config.parallel.max_workers == 1


def test_a_file_fleet_cap_still_overrides_the_files_own_worker_count(tmp_path):
    """With nothing exported, the file decides as it always did — the fix is
    about the layer above it, not about which key the file prefers."""
    from kew.config import load_config

    path = _toml(
        tmp_path, "[parallel]\nmax_workers = 1\n\n[fleet]\nmax_concurrent = 10\n"
    )

    assert load_config(path, env={}).fleet_concurrency() == 10


def _cap_provenance(path, env):
    """``kew config effective`` / ``GET /api/config/effective`` for the cap."""
    from kew.config import load_config
    from kew.service.config import config_snapshot

    config = load_config(path, env=env)
    snapshot = config_snapshot(config, config_path=path, overrides={}, env=env)
    return snapshot["fleet.max_concurrent"]


def test_the_carried_fleet_cap_is_reported_as_the_env_var_not_as_the_file(tmp_path):
    """The env layer WRITES ``[fleet] max_concurrent`` from ``KEW_MAX_WORKERS``,
    so every provenance reader has to know it did. Reporting ``kew.toml`` credits
    the file with a number it never stated — 1 against a line reading 10 — and
    sends the operator to edit the key that is no longer deciding (#2298's rule:
    an environment variable can never be reported as something the file said)."""
    path = _toml(
        tmp_path, "[parallel]\nmax_workers = 4\n\n[fleet]\nmax_concurrent = 10\n"
    )

    assert _cap_provenance(path, {"KEW_MAX_WORKERS": "1"}) == {
        "value": 1,
        "source": "env (KEW_MAX_WORKERS)",
    }


def test_an_explicit_fleet_env_var_is_reported_under_its_own_name(tmp_path):
    """It names the fleet cap and it won, so the carried name must not mask it."""
    path = _toml(
        tmp_path, "[parallel]\nmax_workers = 4\n\n[fleet]\nmax_concurrent = 10\n"
    )

    assert _cap_provenance(
        path, {"KEW_MAX_WORKERS": "1", "KEW_FLEET_MAX_CONCURRENT": "6"}
    ) == {"value": 6, "source": "env (KEW_FLEET_MAX_CONCURRENT)"}


def test_a_file_fleet_cap_with_nothing_exported_is_still_reported_as_the_file(tmp_path):
    """The carry is conditional, so the provenance must be too: without
    ``KEW_MAX_WORKERS`` the file really is the layer that decided the cap."""
    path = _toml(
        tmp_path, "[parallel]\nmax_workers = 1\n\n[fleet]\nmax_concurrent = 10\n"
    )

    assert _cap_provenance(path, {}) == {"value": 10, "source": "kew.toml"}


def test_kew_status_prints_one_number_for_one_cap(store, capsys, monkeypatch):
    """Two numbers for one ceiling leaves the reader to guess which binds."""
    import subprocess

    from kew import cli

    monkeypatch.setattr(
        cli,
        "run_with_backoff",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "0", ""),
    )
    # No repo, so the optional orphan-PR query is not attempted.
    config = DispatchConfig(parallel={"max_workers": 1}, fleet={"max_concurrent": 4})
    cli._status_queue_counts(store, config)
    output = capsys.readouterr().out

    assert "Workers configured: 4" in output
    assert "Workers configured: 1" not in output
