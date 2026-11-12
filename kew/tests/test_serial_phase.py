"""The serial phase: timing-sensitive tests run alone, after the xdist phase,
and nothing lands in the parallel phase by omission.

Pinned here:

* tests/conftest.py marks every test in a module that calls the Textual
  pilot entry point as ``serial`` (from the module source, not a hand-kept
  list), unless that module waits through tests/support/tui_wait.py, whose
  waits all carry a deadline (#2342). The rule is read off the syntax tree
  (tests/support/serial_phase.py), so the escape cannot be claimed by a
  docstring line or an unused import, and a module that claims it keeps no
  deadline-free pilot pump under any binding name. No helper or fixture
  outside a test module calls the pilot, and no test module imports another
  test module, because the probe cannot see through either indirection.
* The wall-clock deadline tests carry the marker by hand.
* ci.yml's ``test`` job, scripts/verify.sh, CONTRIBUTING and README run the
  suite in two phases: ``-n auto -m "not serial"`` then ``-m serial``.
* release.yml runs the same complete phases in separate jobs. The trusted
  serial job must succeed before the hosted release job starts.
* No phase has a worker flag, shell control operator, glued comment, heredoc,
  ``continue-on-error`` or a weakening ``if``. The CI coverage floor runs once.
  The one job-level ``if`` ci.yml's ``test`` job may carry is the agent PR gate
  (#2033), evaluated by tests/support/agent_pr_gate.py: it skips only an
  unapproved agent PR, and every human, approved and main run keeps both
  phases.

Commands are compared as shell tokens. The parity test sorts arguments, so
the ordered command lives here.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from support import repo_inventory, serial_phase
from support.agent_pr_gate import assert_job_gate_stays_live, assert_job_gated

KEW_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = KEW_DIR.parent
TESTS_DIR = KEW_DIR / "tests"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
VERIFY_SH = KEW_DIR / "scripts" / "verify.sh"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
README = REPO_ROOT / "README.md"

MARKER = "serial"
# Built by concatenation so this module's own source never matches the
# conftest probe and marks these tests serial.
PILOT = re.compile(r"\." + "run_" + "test" + r"\b")
# A test module that imports another test module could reach a pilot the
# probe cannot see in its own source.
# Any import statement; the rule below refuses one that names a pilot module
# (absolute, package-qualified or relative), so a wrapper in test_tui_app.py
# cannot reach a caller the probe does not mark. Test modules may import other
# test modules that have no pilot (the conformance pack does today).
# A statement may continue inside parentheses across lines.
IMPORT_LINE = re.compile(r"^\s*(from|import)\s+[^\n(]*(\([^)]*\))?", re.MULTILINE)

# Node-id suffixes of the tests that hold a real wall-clock deadline.
DEADLINE_TESTS = (
    "tests/test_proxy_round22.py::test_round22_five_hundred_matches_finish_within_bound",
    "tests/test_w42_round23.py::test_round23_pending_host_does_not_exhaust_concurrent_repair",
    "tests/test_w42_round23.py::test_round23_signal_only_receipt_is_unknown",
    *(
        "tests/test_loops_delegate.py::"
        f"test_writable_verification_enforces_checkout_quotas[{quota}-{pipes}]"
        for quota in ("bytes", "entries", "floor", "normal")
        for pipes in ("quiet", "closed")
    ),
    "tests/test_subprocess_utils.py::"
    "test_bounded_process_preserves_both_streams_and_shared_deadline",
    "tests/test_loops_verification.py::test_contained_verification_stops_output_flood[stdout]",
    "tests/test_loops_verification.py::test_contained_verification_stops_output_flood[stderr]",
    "tests/test_sandbox.py::TestContainerHandle::"
    "test_a_breach_before_the_container_exists_is_reaped_later",
    "tests/test_sandbox.py::TestContainerHandle::"
    "test_thread_watchdog_re_breaches_until_killed",
    "tests/test_sandbox.py::TestHostSideCaps::"
    "test_silent_stdout_past_deadline_is_killed_within_one_interval",
    "tests/test_sandbox.py::TestHostSideCaps::"
    "test_thread_watchdog_re_polls_on_its_cadence",
    "tests/test_sandbox.py::TestHostSideCaps::"
    "test_silent_stdout_over_checkout_quota_is_killed",
    "tests/test_serve_watch.py::test_new_frame_fans_out_within_poll_s",
    "tests/test_serve_watch.py::test_stream_terminates_on_deauth_within_poll_s",
    "tests/test_serve_api_authz.py::test_run_events_stream_ends_on_session_revocation",
    "tests/test_serve_api_authz.py::test_journal_stream_ends_on_session_revocation",
    "tests/test_serve_api_authz.py::"
    "test_a_proxied_run_stream_closes_when_its_session_expires",
    "tests/test_serve_api_epics.py::"
    "test_a_proxied_epic_stream_closes_when_its_session_expires",
)

# Node-id suffixes of tests that spawn real child processes and read their
# process group (under churn a reused pgid belongs to a foreign process and
# reads as alive), or drive the shared Docker daemon (a parallel worker's
# traffic dropped a buildkit session mid-build). They run when nothing else
# is spawning.
REAL_PROCESS_TESTS = (
    *(
        f"tests/test_round26_binding_evidence.py::{name}[{mode}]"
        for name in (
            "test_round26_forged_resume_refuses_before_git",
            "test_round26_forged_loop_resume_refuses_before_git",
            "test_round26_forged_paid_launch_refuses",
            "test_round26_forged_publication_refuses_before_git",
        )
        for mode in ("checkout", "canonical")
    ),
    "tests/test_round25_checkout_mode_generation.py::test_round25_publication_registration_keeps_bound_generation",
    "tests/test_round25_checkout_mode_generation.py::test_round25_checkout_resume_refuses_replacement_before_git",
    "tests/test_round25_checkout_mode_generation.py::test_round25_checkout_paid_launch_checks_and_refuses_replacement",
    "tests/test_round25_checkout_mode_generation.py::test_round25_checkout_producer_returns_recorded_generation",
    "tests/test_round25_checkout_mode_generation.py::test_round25_unbound_continuation_refuses_with_abandon",
    "tests/test_round24_lease_and_claim_events.py::test_round24_checkout_mode_claim_uses_process_flock",
    "tests/test_round24_lease_and_claim_events.py::test_round24_driver_releases_before_detached_child_claim",
    *(
        "tests/test_round19_loop_abandonment.py::"
        f"test_round22_loop_root_edits_launch_and_publish[{boundary}]"
        for boundary in ("first", "retry", "publication")
    ),
    "tests/test_continuation_recovery.py::test_round21_origin_main_abandon_then_tick[canonical]",
    "tests/test_continuation_recovery.py::test_round21_origin_main_abandon_then_tick[checkout]",
    "tests/test_continuation_recovery.py::test_round21_epic_resume_refuses_reused_identity[awaiting]",
    "tests/test_round19_loop_abandonment.py::test_round21_loop_retires_legacy_sidecar",
    *(
        "tests/test_round19_loop_abandonment.py::"
        f"test_round20_tracked_loop_refusal_records_exit_two[{attempt}]"
        for attempt in (1, 2)
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round20_missing_candidate_defers_bound_token[{occupant}]"
        for occupant in (False, True)
    ),
    *(
        "tests/test_round19_loop_abandonment.py::"
        f"test_round20_loop_identity_refusal_exits_two[{entrypoint}]"
        for entrypoint in ("dispatch", "resume")
    ),
    *(
        "tests/test_round19_loop_abandonment.py::"
        f"test_round20_loop_paid_boundary_refuses_changed_generation[{attempt}-{change}]"
        for attempt in ("setup", "first", "retry")
        for change in ("token", "directory")
    ),
    *(
        "tests/test_round19_loop_abandonment.py::"
        f"test_round20_loop_publication_refuses_changed_generation[{change}]"
        for change in ("token", "directory")
    ),
    "tests/test_round19_loop_abandonment.py::test_round20_unchanged_loop_launches_and_publishes",
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round20_recreated_checkout_refuses_consumers[{boundary}]"
        for boundary in ("admission", "quarantine", "base", "host")
    ),
    "tests/test_continuation_recovery.py::test_round20_epic_resume_refuses_recreated_checkout[awaiting]",
    *(
        "tests/test_round19_loop_abandonment.py::" + name
        for name in (
            "test_round19_same_path_consumes_before_first_step[canonical]",
            "test_round19_same_path_consumes_before_first_step[checkout]",
            "test_round19_moved_base_records_rebound_before_first_step",
            "test_round19_existing_recorded_path_refuses_without_writes[False]",
            "test_round19_existing_recorded_path_refuses_without_writes[True]",
            "test_round19_consumed_once_refuses_host_and_loop_after_retention",
            "test_round19_prelaunch_withdrawal_allows_fresh_retry[publication]",
            "test_round19_prelaunch_withdrawal_allows_fresh_retry[prompt]",
            "test_round19_prelaunch_withdrawal_allows_fresh_retry[runner]",
            "test_round19_no_store_refuses_before_setup",
            "test_round19_failed_expiry_retains_authority_for_retry",
            "test_round19_failed_cleanup_retains_replacement[failed]",
            "test_round19_failed_cleanup_retains_replacement[preserved]",
            "test_round19_worker_failure_preserves_successor_checkout",
        )
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round14_case_alias_refuses_active_dispatch[{epic}-{state}]"
        for epic in ("None", "epic:other")
        for state in ("awaiting", "continuing")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round14_missing_recorded_path_refuses_dispatch[{state}-awaiting]"
        for state in ("moved", "deleted")
    ),
    "tests/test_continuation_recovery.py::"
    "test_round14_alias_abandonment_authorizes_bound_replacement",
    *(
        f"tests/test_continuation_recovery.py::{test}[{epic}-{state}]"
        for test in (
            "test_active_continuation_refuses_setup_across_epics",
            "test_active_continuation_cli_refusal_releases_reservation",
        )
        for epic in ("None", "epic:other")
        for state in ("awaiting", "continuing")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_active_continuation_loop_cli_refuses_before_setup[{state}]"
        for state in ("awaiting", "continuing")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        "test_retained_refusal_requires_withdrawal_before_and_after_retention"
        f"[{epic}-{refusal}-{cleanup}]"
        for epic in ("None", "epic:other")
        for refusal in ("budget", "containment")
        for cleanup in ("failed", "preserved")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_abandoned_checkout_refuses_owning_epic_hold[{epic}-{hold}]"
        for epic in ("None", "epic:other")
        for hold in ("budget", "sandbox_containment", "operator_review")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_abandonment_capture_refuses_invalid_sidecar[{state}]"
        for state in ("empty", "malformed", "unreadable")
    ),
    "tests/test_continuation_recovery.py::"
    "test_legacy_abandonment_mints_claimed_token_and_refuses_replacement",
    "tests/test_continuation_recovery.py::"
    "test_replacement_outside_abandoning_epic_registers_new_work[None]",
    "tests/test_continuation_recovery.py::"
    "test_replacement_outside_abandoning_epic_registers_new_work[epic:other]",
    "tests/test_continuation_recovery.py::"
    "test_abandonment_refuses_changed_checkout_generation",
    *(
        "tests/test_continuation_recovery.py::"
        f"test_prelaunch_replacement_withdrawal_requires_removed_checkout[{cleanup}-{refusal}]"
        for cleanup in ("removed", "failed", "preserved")
        for refusal in ("cap", "budget", "containment", "transport", "checkout")
    ),
    # Round 18 (#1952): abandonment rebinds on a missing recorded checkout. These
    # drive a native checkout claim and real worktree bases under tmp_path.
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_missing_checkout_rebind_creates_and_expires[{base}]"
        for base in ("deleted-base", "moved-base")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_existing_recorded_checkout_refuses_move[{moved}]"
        for moved in ("False", "True")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_existing_candidate_generation_governs[{token}]"
        for token in ("valid-token", "invalid-token")
    ),
    "tests/test_continuation_recovery.py::"
    "test_round18_rebound_authority_is_consumed_once",
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_failed_rebind_keeps_authority_for_next_attempt[{stage}]"
        for stage in ("setup", "expiry")
    ),
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_missing_checkout_rebind_requires_item_and_branch[{owner}]"
        for owner in ("43-agent/issue-42", "42-agent/other")
    ),
    "tests/test_continuation_recovery.py::"
    "test_round18_rebound_withdrawal_keeps_original_evidence_family",
    *(
        "tests/test_continuation_recovery.py::"
        f"test_round18_rebind_expiry_requires_original_path_and_reason[{case}]"
        for case in (
            "rebound_from-another-checkout",
            "rebound_from-None",
            "rebound_reason-another-reason",
            "rebound_reason-None",
        )
    ),
    *(
        "tests/test_driver_dispatch.py::"
        f"test_round18_tick_launches_child_after_abandon_and_base_deletion[{base}]"
        for base in ("deleted-base", "moved-base")
    ),
    "tests/test_epic_driver_cli.py::"
    "test_abandon_refuses_worker_spawned_before_pid_recording",
    "tests/test_continuation_recovery.py::"
    "test_canonical_replacement_failure_preserves_its_own_continuation[False]",
    "tests/test_continuation_recovery.py::"
    "test_canonical_replacement_failure_preserves_its_own_continuation[True]",
    "tests/test_w42_round24.py::test_round24_double_failure_preserves_interruption_and_reaps[False]",
    "tests/test_w42_round24.py::test_round24_double_failure_preserves_interruption_and_reaps[True]",
    "tests/test_w42_round22.py::test_round22_missing_inner_executable_refuses_without_attempt",
    "tests/test_w42_round22.py::test_round22_nonexecutable_inner_file_refuses_without_attempt",
    "tests/test_w42_round22.py::test_round22_wrapper_failure_125_refuses_without_attempt",
    "tests/test_w42_round22.py::test_round22_agent_event_then_exit_one_starts",
    "tests/test_w42_round22.py::test_round22_silent_timeout_124_starts",
    "tests/test_w42_round22.py::test_round22_output_then_exit_127_starts[event]",
    "tests/test_w42_round22.py::test_round22_output_then_exit_127_starts[log]",
    "tests/test_w42_round22.py::test_round22_direct_missing_raises_before_ownership_without_receipt",
    "tests/test_w42_round22.py::test_round22_signal_before_output_keeps_startup_unknown",
    "tests/test_w42_round21.py::test_round21_created_container_refuses_without_consuming_retry[False]",
    "tests/test_w42_round21.py::test_round21_created_container_refuses_without_consuming_retry[True]",
    "tests/test_w42_round21.py::test_round21_host_receipt_retries_after_popen",
    "tests/test_w42_round21.py::test_round21_finalization_retries_receipt",
    *(
        "tests/test_w42_round20.py::"
        "test_round20_container_start_owns_cancellation_after_runtime_identity"
        f"[{site}]"
        for site in ("direct", "sidecar", "unidentified")
    ),
    "tests/test_w42_round20.py::test_round20_started_process_uses_execution_cleanup[False]",
    "tests/test_w42_round20.py::test_round20_started_process_uses_execution_cleanup[True]",
    "tests/test_w42_round20.py::test_round20_applied_label_interruption_does_not_consume_retry",
    "tests/test_w42_round20.py::test_round20_two_real_launches_exhaust_durable_attempts",
    "tests/test_bounded_run_keys.py::test_round18_crash_before_prelaunch_recovers_mismatched_hash",
    "tests/test_bounded_run_keys.py::test_round16_real_child_cli_preflight",
    "tests/test_bounded_run_keys.py::test_round16_real_child_detached_capture",
    "tests/test_bounded_run_keys.py::test_round16_uncontained_boundary_filters_ambient",
    "tests/test_bounded_run_keys.py::test_round15_blocked_post_survives_negative_lookup",
    *(
        "tests/test_bounded_run_keys.py::"
        f"test_round14_admission_child_environment_and_diagnostic[{owner}]"
        for owner in ("foreground", "driver")
    ),
    *(
        "tests/test_bounded_run_keys.py::"
        f"test_round14_scope_filters_generic_children_and_clears[{api}]"
        for api in ("run", "popen")
    ),
    *(
        "tests/test_bounded_run_keys.py::"
        f"test_round14_killed_response_before_marker_recovers_intent[{mode}]"
        for mode in ("present", "absent", "unsupported")
    ),
    "tests/test_bounded_run_keys.py::"
    "test_round14_killed_before_post_settles_after_one_lookup",
    *(
        "tests/test_subprocess_utils.py::"
        f"test_round6_real_grandchild_drops_inherited_key[{explicit_env}-{api}]"
        for explicit_env in (False, True)
        for api in ("run", "popen")
    ),
    "tests/test_driver_dispatch.py::test_driver_launcher_real_process_filters_authority",
    *(
        "tests/test_subprocess_utils.py::"
        f"test_discovery_children_filter_management_credentials[{probe}-{inference}]"
        for probe in (
            "version",
            "claude-auth",
            "codex-inspect",
            "readiness-boundary",
            "tracked-boundary",
        )
        for inference in ("absent", "inherited", "issued")
    ),
    *(
        "tests/test_subprocess_utils.py::"
        f"test_subprocess_boundary_filters_discovery_environments[{explicit_env}-{api}]"
        for explicit_env in (False, True)
        for api in (
            "run",
            "run_bytes",
            "run_pinned",
            "popen",
            "popen_bytes",
            "popen_pinned",
        )
    ),
    "tests/test_readiness_probe.py::"
    "test_bounded_probe_child_has_no_management_credentials[host-key-host]",
    "tests/test_readiness_probe.py::"
    "test_bounded_probe_child_has_no_management_credentials[host-key-contained]",
    "tests/test_readiness_probe.py::"
    "test_bounded_probe_child_has_no_management_credentials[issued-key-host]",
    "tests/test_readiness_probe.py::"
    "test_bounded_probe_child_has_no_management_credentials[issued-key-contained]",
    *(
        "tests/test_round26_claim_windows.py::"
        f"test_round26_real_process_window[{window}]"
        for window in ("W1", "W2", "W3", "W4", "W5")
    ),
    "tests/test_round26_claim_windows.py::"
    "test_round26_lifecycle_table_has_no_stranded_item",
    "tests/test_round25_claim_states.py::"
    "test_round25_dead_dispatcher_recovers_intended_state[Doing-st-doing]",
    "tests/test_round25_claim_states.py::"
    "test_round25_dead_dispatcher_recovers_intended_state[In Progress-st-progress]",
    "tests/test_round24_claim_recovery.py::"
    "test_round24_sigkill_after_remote_commit_redispatches_once",
    "tests/test_round22_dispatcher_liveness.py::"
    "test_round22_real_live_pid_start_mismatch_is_dead",
    "tests/test_round22_dispatcher_liveness.py::"
    "test_round22_zombie_reconciles_and_one_worker_watcher_redispatches",
    "tests/test_round22_dispatcher_liveness.py::"
    "test_round22_tick_reaps_all_detached_handles[driver]",
    "tests/test_round22_dispatcher_liveness.py::"
    "test_round22_tick_reaps_all_detached_handles[watcher]",
    "tests/test_round21_admission_owner.py::"
    "test_round21_reap_preserves_replacement_generation",
    "tests/test_round21_admission_owner.py::"
    "test_round21_sigkill_mid_hook_reaps_and_watcher_redispatches",
    "tests/test_round21_admission_owner.py::"
    "test_round21_two_day_dead_owner_reaped_live_hook_retained",
    "tests/test_round18_liveness.py::test_round18_stale_snapshot_pid_never_signalled",
    "tests/test_round18_liveness.py::test_round18_authenticated_group_stop_prevents_retry",
    "tests/test_round18_liveness.py::"
    "test_round18_attempt_death_witness_needs_no_owner_bookkeeping",
    "tests/test_round16_liveness.py::test_round16_real_exited_owner_with_same_group_child",
    *(
        "tests/test_round16_liveness.py::"
        f"test_round16_real_killed_owner_releases_capacity[{surface}]"
        for surface in ("plain", "identity", "reservation", "continuation")
    ),
    "tests/test_round15_liveness.py::test_round15_missing_attempt_never_executes[False]",
    "tests/test_round15_liveness.py::test_round15_missing_attempt_never_executes[True]",
    "tests/test_round15_liveness.py::test_round15_tokens_execute_and_stop_then_refuse_loss",
    "tests/test_round14_liveness.py::test_round14_real_detached_parent_exit[True]",
    "tests/test_round14_liveness.py::test_round14_real_detached_parent_exit[False]",
    "tests/test_round14_liveness.py::test_round14_real_same_second_tokens_differ",
    "tests/test_round13_liveness.py::test_round13_real_foreground_pipeline[True]",
    "tests/test_round13_liveness.py::test_round13_real_foreground_pipeline[False]",
    "tests/test_round13_liveness.py::test_round13b_real_live_descendant_admits",
    "tests/test_liveness_owner.py::test_round8_real_spawn_worker_stop",
    "tests/test_liveness_owner.py::test_round7_real_late_descendant[False]",
    "tests/test_liveness_owner.py::test_round7_real_late_descendant[True]",
    "tests/test_liveness_owner.py::test_round7_real_reparented_child_is_retained",
    "tests/test_liveness_owner.py::test_round7_stale_live_attempt_keeps_intent",
    "tests/test_liveness_owner.py::test_round6_owner_admission_gate[1]",
    "tests/test_liveness_owner.py::test_round6_owner_admission_gate[3]",
    "tests/test_liveness_owner.py::test_round6_real_leader_exit_escalation[False]",
    "tests/test_liveness_owner.py::test_round6_real_leader_exit_escalation[True]",
    "tests/test_fleet.py::TestEndToEndStopResume::test_full_cycle",
    "tests/test_liveness_owner.py::test_round5_stop_between_registration_and_permit",
    "tests/test_liveness_owner.py::test_round5_legacy_stop_resume_preserves_continuation",
    "tests/test_temp_root_isolation.py::"
    "test_success_cleanup_and_concurrent_pytest_roots_are_isolated",
    "tests/test_verify_parity.py::"
    "test_private_cache_recipe_preserves_status_and_another_allocation",
    "tests/test_verify_parity.py::"
    "test_private_cache_cleanup_preserves_an_active_uvx_environment",
    "tests/test_fleet.py::TestStopFleet::test_kills_live_run_and_latches",
    "tests/test_fleet.py::TestStopFleet::"
    "test_sigkill_after_grace_for_term_ignoring_process",
    "tests/test_fleet.py::TestStopFleet::test_dead_pgid_marked_killed_without_signal",
    "tests/test_fleet.py::TestStopFleet::test_missing_pgid_refuses_unowned_events_file",
    "tests/test_driver_tick.py::"
    "test_orphaned_pending_run_reaped_release_and_starved_node_dispatches",
    "tests/test_driver_tick.py::"
    "test_dead_pending_dirty_worktree_becomes_awaiting_continuation",
    "tests/test_driver_tick.py::"
    "test_continuation_crash_with_dirty_work_reopens_same_record",
    "tests/test_agent_image.py::TestAgentDockerfileLiveBuild::"
    "test_the_image_the_class_uses_is_present_locally",
    "tests/test_agent_image.py::TestAgentDockerfileLiveBuild::"
    "test_claude_starts_under_hardened_runtime_controls",
    "tests/test_agent_image_scan_gate.py::"
    "test_proxy_image_builds_from_exact_pinned_revision",
    "tests/test_switchyard_sidecar.py::"
    "test_internal_worker_reaches_only_proxy_listener",
    "tests/test_switchyard_sidecar.py::"
    "test_packet_matrix_enters_the_durable_production_contract",
    "tests/test_switchyard_sidecar.py::"
    "test_worker_netns_filter_blocks_host_gateway_and_peers",
    "tests/test_switchyard_sidecar.py::test_worker_netns_filter_allows_only_proxy",
    "tests/test_switchyard_sidecar.py::"
    "test_filter_readback_failure_prevents_agent_start",
    "tests/test_switchyard_sidecar.py::"
    "test_netns_filter_preserves_sc0_topology_baseline",
    "tests/test_switchyard_sidecar.py::"
    "test_proxy_image_starts_nonroot_with_final_hardening",
    "tests/test_switchyard_sidecar.py::"
    "test_egress_peer_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_host_gateway_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_macos_host_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_external_address_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_dns_udp_ipv6_and_extra_proxy_port_are_denied",
    "tests/test_switchyard_sidecar.py::"
    "test_worker_and_proxy_share_no_pid_mount_or_socket",
    "tests/test_switchyard_sidecar.py::test_failed_topology_start_removes_all_objects",
    "tests/test_sandbox.py::TestIsolatedSidecarNetworkArgv::"
    "test_sidecar_filter_never_mutates_global_firewall",
    "tests/test_agent_image_scan_gate.py::"
    "test_chunk_shipped_wrapper_real_cancellation_leaves_no_fake_docker_group[pid-term]",
    "tests/test_agent_image_scan_gate.py::"
    "test_chunk_shipped_wrapper_real_cancellation_leaves_no_fake_docker_group[pid-int]",
    "tests/test_agent_image_scan_gate.py::"
    "test_chunk_shipped_wrapper_real_cancellation_leaves_no_fake_docker_group[group-term]",
    "tests/test_agent_image_scan_gate.py::"
    "test_chunk_shipped_wrapper_real_cancellation_leaves_no_fake_docker_group[alarm]",
    # Spawns a real `openshell sandbox exec` child against an operator gateway
    # and waits on its exit. Skipped unless KEW_LIVE_OPENSHELL=1, so in CI it
    # is pinned here rather than running — the pin is what keeps the marker
    # from being an unexplained parking space (epic openshell-runtime, N6).
    "tests/test_openshell_launch.py::test_a_live_gateway_runs_claude_and_answers_ok",
    # The codex sibling of the line above, added with epic openshell-runtime
    # N10. Same shape and the same opt-in: a real `openshell sandbox exec`
    # child, skipped unless KEW_LIVE_OPENSHELL=1, pinned here so the serial
    # marker it carries is accounted for rather than unexplained.
    "tests/test_openshell_launch.py::"
    "test_a_live_gateway_runs_codex_exec_with_an_explicit_model",
)

# A token carrying one of these would let a failing phase exit zero. A glued
# '#' is a bash word character, not a comment: `85# || true` runs the tail.
CONTROL_OPERATORS = ("||", "&&", ";", "|", "&", "#")
# Every way xdist 3.8 starts workers: -n/--numprocesses, -d with --tx, --dist,
# --maxprocesses. `-n` and `-d` may carry a glued value (`-n2`).
# `-n` and `-d` take a glued value too (-nauto, -n2), so any token starting
# with either is a worker flag; no other single-dash pytest option does.
WORKER_FLAG = re.compile(r"^(-n|-d)|^--(numprocesses|dist|tx|maxprocesses)\b")
# The only tokens a phase command may carry, after `uv run pytest`. Every
# other option is refused by construction: selectors, early-exit options
# such as --version or --markers, plugin switches, config overrides.
ALLOWED_TOKENS = (
    re.compile(r"^-n$"),
    re.compile(r"^-m$"),
    re.compile(r"^--cov=src/kew$"),
    re.compile(r"^--cov-append$"),
    re.compile(r"^--cov-fail-under=[0-9]+(\.[0-9]+)?$"),
)
# Flags whose next token is a value, not a positional test path.
VALUE_FLAGS = {"-m", "-n"}
HEREDOC = "<<"


def _pilot_modules() -> list[Path]:
    return sorted(
        p
        for p in TESTS_DIR.rglob("test_*.py")
        if PILOT.search(p.read_text(encoding="utf-8"))
    )


def _on_bounded_waits(path: Path) -> bool:
    return serial_phase.waits_with_deadlines(path.read_text(encoding="utf-8"))


def _serial_pilot_modules() -> list[Path]:
    """The pilot modules conftest keeps in the serial phase."""
    return [p for p in _pilot_modules() if not _on_bounded_waits(p)]


def _parallel_pilot_modules() -> list[Path]:
    """The pilot modules that earned the parallel phase with bounded waits."""
    return [p for p in _pilot_modules() if _on_bounded_waits(p)]


def _marked(modules: list[str]) -> set[str]:
    """The collected tests in *modules* that carry the serial marker.

    Read off the collection the session already made
    (``support.repo_inventory``), as is every id these probes compare against:
    each one used to drive a nested ``pytest --collect-only`` of its own, and
    the whole-tree one was the slowest test in the suite (#2340). The
    inventory is recorded with the marker conftest applies and before any
    deselection, so it is still exactly what an opted-in ``-m serial`` run
    selects — the Switchyard image tests included, which a default collection
    drops.
    """
    return repo_inventory.ids_in(modules) & repo_inventory.ids_with_mark(MARKER)


# --- the marker --------------------------------------------------------------


@pytest.mark.policy
def test_there_are_pilot_modules_to_protect() -> None:
    assert _pilot_modules(), "no test module drives a pilot; update this guard"


# The probe's inputs, built by concatenation so this module's own source
# matches neither the pilot probe nor the bounded-wait probe. They are real
# modules, because the probe reads a syntax tree rather than the text.
_ENTRY = "run_" + "test"
_PUMP = "pause"
_HELPER = "support.tui_wait"
_HELPER_IMPORT = "from " + _HELPER + " import quiesce\n"
_FIXTURE_IMPORT = "from " + _HELPER + " import no_ambient_polls\n"
_ALIASED_IMPORT = "from " + _HELPER + " import quiesce as settled\n"
_IMPORT_IN_A_DOCSTRING = '"""Waits through:\n\n' + _HELPER_IMPORT + '"""\n'


def _pilot_module(header: str = "", binding: str = "pilot", body: str = "") -> str:
    """A module that opens a pilot, with *header* above it and *body* inside."""
    return (
        header
        + "async def test_subject(app):\n"
        + f"    async with app.{_ENTRY}() as {binding}:\n"
        + (body or f"        assert {binding}.app.screen_stack\n")
    )


def _fixture_pilot_module(
    body: str = "",
    *,
    decorator: str = "@pytest.fixture",
    defined: str = "pilot",
    requested: str = "",
) -> str:
    """A module whose pilot arrives from an in-module fixture.

    No ``with`` statement in the test binds the pilot: the fixture holds the
    entry point and the test holds the pilot under the name it asked the
    fixture for (#2342 review). The module claims the parallel phase, so
    *body* is what the bare-pump gate has to find.
    """
    requested = requested or defined
    return (
        "import pytest\n"
        + _HELPER_IMPORT
        + "\n\n"
        + decorator
        + "\n"
        + f"async def {defined}(app):\n"
        + f"    async with app.{_ENTRY}() as p:\n"
        + "        yield p\n\n\n"
        + f"async def test_subject({requested}):\n"
        + f"    await quiesce({requested})\n"
        + body
    )


def _helper_pilot_module(body: str = "") -> str:
    """A module whose pilot arrives from an in-module context-manager helper.

    The test's ``with`` statement opens the helper, not the entry point, so
    the target is a pilot only because the helper yields one.
    """
    return (
        "from contextlib import asynccontextmanager\n"
        + _HELPER_IMPORT
        + "\n\n@asynccontextmanager\n"
        + "async def _running(app):\n"
        + f"    async with app.{_ENTRY}() as p:\n"
        + "        yield p\n\n\n"
        + "async def test_subject(app):\n"
        + "    async with _running(app) as pilot:\n"
        + "        await quiesce(pilot)\n"
        + body
    )


@pytest.mark.parametrize(
    ("source", "serial"),
    [
        pytest.param(_pilot_module(), True, id="pilot-alone"),
        pytest.param(
            _pilot_module(header=_HELPER_IMPORT, body="        await quiesce(pilot)\n"),
            False,
            id="pilot-waits-bounded",
        ),
        pytest.param("assert True\n", False, id="no-pilot"),
        pytest.param(
            _HELPER_IMPORT + "async def test_x(pilot):\n    await quiesce(pilot)\n",
            False,
            id="helper-without-pilot",
        ),
        # The three ways the text-level rule could be claimed without being
        # earned (#2342 review). Each has to read as "no bounded waits".
        pytest.param(
            _pilot_module(header=_HELPER_IMPORT), True, id="import-never-called"
        ),
        pytest.param(
            _pilot_module(header=_IMPORT_IN_A_DOCSTRING),
            True,
            id="import-in-a-docstring",
        ),
        pytest.param(
            _pilot_module(
                header=_FIXTURE_IMPORT, body="        await no_ambient_polls(pilot)\n"
            ),
            True,
            id="the-fixture-is-not-a-wait",
        ),
        pytest.param(
            _pilot_module(
                header=_ALIASED_IMPORT, body="        await settled(pilot)\n"
            ),
            False,
            id="aliased-wait",
        ),
    ],
)
def test_the_probe_reads_both_halves_from_the_module_source(
    tmp_path: Path, source: str, serial: bool
) -> None:
    """The conftest rule itself, on sources the suite may not contain today.

    A blind pilot module is serial even when no such module exists in the tree
    right now, so the default cannot quietly invert.
    """
    module = tmp_path / "test_probe_subject.py"
    module.write_text(source, encoding="utf-8")
    # The probe memoizes by path for the session (a module's source cannot
    # change under a running collection), so read the rule itself: the cases
    # here share one tmp_path file name.
    probe = serial_phase.needs_the_serial_phase.__wrapped__
    assert probe(str(module)) is serial


def test_the_marker_and_these_gates_read_one_rule() -> None:
    """conftest applies the marker through the same probe the gates assert on."""
    import conftest

    assert conftest.serial_phase is serial_phase


@pytest.mark.parametrize(
    ("source", "pumped"),
    [
        pytest.param(
            _pilot_module(body=f"        await pilot.{_PUMP}()\n"), True, id="pilot"
        ),
        # The reviewer's case: the pump is deadline-free whatever name the
        # module bound the pilot to, so the gate cannot key on ``pilot``.
        pytest.param(
            _pilot_module(binding="p", body=f"        await p.{_PUMP}()\n"),
            True,
            id="bound-as-p",
        ),
        pytest.param(
            _pilot_module(binding="_pilot", body=f"        await _pilot.{_PUMP}()\n"),
            True,
            id="bound-as-underscore-pilot",
        ),
        pytest.param(
            _pilot_module(header=_HELPER_IMPORT, body="        await quiesce(pilot)\n"),
            False,
            id="waits-through-the-helper",
        ),
        pytest.param(
            _pilot_module(body=f"        pilot.app._detail_timer.{_PUMP}()\n"),
            False,
            id="a-timer-is-not-a-pilot",
        ),
        pytest.param(
            _pilot_module(body='        """await pilot.' + _PUMP + '()"""\n'),
            False,
            id="named-in-a-docstring",
        ),
        # The second reviewer's case: the pilot never passes through a ``with``
        # statement of the test's own, so a probe that reads only ``with``
        # targets sees no pump at all.
        pytest.param(
            _fixture_pilot_module(f"    await pilot.{_PUMP}()\n"),
            True,
            id="from-an-in-module-fixture",
        ),
        pytest.param(
            _fixture_pilot_module(
                f"    await driver.{_PUMP}()\n",
                decorator='@pytest.fixture(name="driver")',
                defined="_driver",
                requested="driver",
            ),
            True,
            id="from-a-renamed-in-module-fixture",
        ),
        pytest.param(
            _helper_pilot_module(f"        await pilot.{_PUMP}()\n"),
            True,
            id="from-an-in-module-context-manager",
        ),
        pytest.param(
            _fixture_pilot_module(), False, id="an-in-module-fixture-waits-bounded"
        ),
        pytest.param(
            _helper_pilot_module(), False, id="an-in-module-helper-waits-bounded"
        ),
    ],
)
def test_the_bare_pump_probe_reads_the_pilot_binding_name(
    source: str, pumped: bool
) -> None:
    """A deadline-free pump is found under the name the module actually used."""
    assert bool(serial_phase.bare_pumps(source)) is pumped


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_fixture_pilot_module(), id="from-a-fixture"),
        pytest.param(_helper_pilot_module(), id="from-a-context-manager"),
    ],
)
def test_a_pilot_from_an_in_module_factory_is_still_a_pilot_module(
    source: str,
) -> None:
    """The bare-pump gate only reads modules the pilot probe claims.

    A pilot opened in a fixture or a helper has to read as a pilot module on
    bounded waits, or the gate above is never pointed at it and the binding
    names it learned buy nothing.
    """
    assert serial_phase.drives_a_pilot(source)
    assert PILOT.search(source)
    assert serial_phase.waits_with_deadlines(source)


@pytest.mark.policy
def test_every_test_in_a_blind_pilot_module_is_marked_serial() -> None:
    """A pilot module that pumps the app without a deadline stays serial.

    Empty today — every pilot module in the tree waits through the helper — so
    the rule itself is pinned above, on sources the tree does not contain.
    """
    modules = [str(p.relative_to(KEW_DIR)) for p in _serial_pilot_modules()]
    if not modules:
        pytest.skip(
            "every pilot module waits through support/tui_wait.py; the rule is "
            "pinned by test_the_probe_reads_both_halves_from_the_module_source"
        )
    everything = repo_inventory.ids_in(modules)
    marked = _marked(modules)
    assert everything, "collection produced no items"
    assert marked == everything, (
        "tests in a pilot module escaped the serial marker (they would run in "
        "the parallel phase): " + ", ".join(sorted(everything - marked))
    )


@pytest.mark.policy
def test_a_pilot_module_on_bounded_waits_runs_in_the_parallel_phase() -> None:
    """The escape has to reach the parallel phase, or it bought nothing.

    Every wait in such a module carries a deadline and names what it waited
    for (tests/support/tui_wait.py), so contention lengthens a wait instead of
    defeating an assertion.
    """
    modules = [str(p.relative_to(KEW_DIR)) for p in _parallel_pilot_modules()]
    assert modules, (
        "no pilot module waits through support/tui_wait.py; the serial phase "
        "carries every TUI test again"
    )
    assert repo_inventory.ids_in(modules), "collection produced no items"
    marked = _marked(modules)
    assert marked == set(), (
        "a pilot module on bounded waits is still serial: " + ", ".join(sorted(marked))
    )


@pytest.mark.policy
def test_no_bare_pilot_pump_survives_in_a_parallel_pilot_module() -> None:
    """Importing the helper is not enough: the module must pump through it.

    The pilot's own pump asserts "the app got there within N idle points",
    which is exactly what stops holding under load. Found under whatever name
    the module bound the pilot to, so renaming it is not a way out.
    """
    offenders = sorted(
        f"{path.relative_to(KEW_DIR)}:{line}"
        for path in _parallel_pilot_modules()
        for line in serial_phase.bare_pumps(path.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        "a pilot module claims support/tui_wait.py and still pumps the app "
        "with a bare, deadline-free pause; wait on the state the assertion is "
        "about instead: " + ", ".join(offenders)
    )


def test_a_module_without_a_pilot_is_not_marked_serial() -> None:
    this = str(Path(__file__).relative_to(KEW_DIR))
    assert repo_inventory.ids_in([this])
    assert _marked([this]) == set()


@pytest.mark.policy
def test_no_pilot_call_hides_outside_a_test_module() -> None:
    """The probe reads the test module. A fixture in conftest or a helper
    module that drives the pilot would leave its callers unmarked."""
    offenders = sorted(
        str(p.relative_to(KEW_DIR))
        for p in TESTS_DIR.rglob("*.py")
        if not p.name.startswith("test_")
        and PILOT.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        "the Textual pilot is driven outside a test module, where the serial "
        "probe cannot see it; call it from the test module or mark the callers "
        "@pytest.mark.serial by hand: " + ", ".join(offenders)
    )


@pytest.mark.policy
def test_no_test_module_imports_a_pilot_module() -> None:
    """A wrapper in a pilot module used from another test module would leave
    the caller's tests unmarked. Refuse any import line naming a pilot module
    stem, in any form (absolute, package-qualified, relative)."""
    stems = [p.stem for p in _pilot_modules()]
    offenders = []
    for path in TESTS_DIR.rglob("test_*.py"):
        if path.stem in stems:
            continue
        for match in IMPORT_LINE.finditer(path.read_text(encoding="utf-8")):
            if any(re.search(rf"\b{stem}\b", match.group(0)) for stem in stems):
                offenders.append(
                    f"{path.relative_to(KEW_DIR)}: {match.group(0).strip()}"
                )
    assert offenders == [], (
        "a test module imports a pilot module, which can hide the pilot from "
        "the serial probe; move shared code to a helper without the pilot: "
        + ", ".join(sorted(offenders))
    )


@pytest.mark.policy
def test_nothing_under_tests_declares_pytest_plugins() -> None:
    """`pytest_plugins = (...)` loads another module as a plugin with no import
    statement, so a pilot fixture reached that way escapes both probes. That
    includes conftest.py naming a pilot module, and the annotated form
    `pytest_plugins: tuple[str, ...] = (...)`. The suite declares none."""
    pattern = re.compile(r"^\s*pytest_plugins\s*(:[^=\n]*)?=", re.M)
    offenders = sorted(
        str(p.relative_to(KEW_DIR))
        for p in TESTS_DIR.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        "pytest_plugins can load a pilot fixture the serial probe cannot see; "
        "share fixtures through conftest.py without a pilot: " + ", ".join(offenders)
    )


def test_hand_marked_tests_carry_the_serial_marker() -> None:
    expected = DEADLINE_TESTS + REAL_PROCESS_TESTS
    files = sorted({suffix.split("::")[0] for suffix in expected})
    marked = _marked(files)
    for suffix in expected:
        matches = [node for node in marked if node.endswith(suffix)]
        assert len(matches) == 1, (
            f"{suffix} holds a real wall-clock deadline or a real child "
            f"process and must match exactly one test carrying "
            f"@pytest.mark.{MARKER}; matched {matches}"
        )


@pytest.mark.policy
def test_the_serial_set_is_exactly_the_pilot_modules_plus_the_pinned_tests() -> None:
    """One-way pins let anyone park an unrelated test in the serial phase. The
    serial set must equal the blind pilot modules' tests plus the hand-marked
    ids — a pilot module on bounded waits contributes nothing to it."""
    serial = repo_inventory.ids_with_mark(MARKER)
    pilot_modules = [str(p.relative_to(KEW_DIR)) for p in _serial_pilot_modules()]
    expected = repo_inventory.ids_in(pilot_modules) if pilot_modules else set()
    hand_marked = DEADLINE_TESTS + REAL_PROCESS_TESTS
    everything = repo_inventory.ids_in(
        str(Path(suffix.split("::")[0])) for suffix in hand_marked
    )
    for suffix in hand_marked:
        expected.update(node for node in everything if node.endswith(suffix))
    unexpected = sorted(serial - expected)
    assert unexpected == [], (
        "tests carry @pytest.mark.serial without a pin in DEADLINE_TESTS or "
        "REAL_PROCESS_TESTS: " + ", ".join(unexpected)
    )
    missing = sorted(expected - serial)
    assert missing == [], "pinned or pilot tests not in the serial set: " + ", ".join(
        missing
    )


def test_the_marker_is_registered() -> None:
    pyproject = tomllib.loads((KEW_DIR / "pyproject.toml").read_text())
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith(f"{MARKER}:") for m in markers)


def test_no_addopts_can_narrow_the_phases() -> None:
    """`addopts` applies to every pytest run, the phases and this file's
    collection probes alike, so an `--ignore` there hides a red test from all
    of them at once."""
    pyproject = tomllib.loads((KEW_DIR / "pyproject.toml").read_text())
    assert "addopts" not in pyproject["tool"]["pytest"]["ini_options"], (
        "pytest addopts configured; put per-run options on the phase commands"
    )
    # pytest 9 reads these before pyproject.toml.
    for name in (
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "tox.ini",
        "setup.cfg",
    ):
        assert not (KEW_DIR / name).exists(), f"{name} could carry addopts"


# --- the commands ------------------------------------------------------------


def _pytest_commands(text: str) -> list[list[str]]:
    """Every ``uv run pytest`` line in ``text`` as shell tokens.

    A trailing ``# note`` after whitespace is a comment (doc lines carry
    them). A ``#`` glued to a token is not: bash keeps it in the word, so it
    stays in the token and the control-operator check refuses it.
    """
    commands = []
    for raw in text.splitlines():
        line = re.sub(r"\s#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=False)
        except ValueError:
            continue  # prose with an unbalanced quote, not a command
        if tokens[:3] == ["uv", "run", "pytest"]:
            commands.append(tokens)
    return commands


def _phase_of(tokens: list[str]) -> str | None:
    if tokens.count("-m") != 1:
        return None  # pytest keeps the last -m; two of them is a decoy
    expression = tokens[tokens.index("-m") + 1]
    if expression == f"not {MARKER}":
        return "parallel"
    if expression == MARKER:
        return "serial"
    return None


def _two_phases(text: str, where: str) -> tuple[list[str], list[str]]:
    commands = _pytest_commands(text)
    parallel = [c for c in commands if _phase_of(c) == "parallel"]
    serial = [c for c in commands if _phase_of(c) == "serial"]
    assert len(parallel) == 1, (
        f'{where}: expected one `-m "not {MARKER}"` phase, got {parallel}'
    )
    assert len(serial) == 1, f"{where}: expected one `-m {MARKER}` phase, got {serial}"
    p, s = parallel[0], serial[0]
    assert commands.index(p) < commands.index(s), (
        f"{where}: the serial phase must follow the parallel phase"
    )
    worker_flags = [t for t in p if WORKER_FLAG.match(t)]
    assert worker_flags == ["-n"] and p[p.index("-n") + 1] == "auto", (
        f"{where}: the parallel phase must carry exactly one worker flag, "
        f"`-n auto` (a later --numprocesses=0 would win): {p}"
    )
    assert not any(WORKER_FLAG.match(t) for t in s), (
        f"{where}: the serial phase runs under xdist again (#1772): {s}"
    )
    for tokens in (p, s):
        assert not any(op in t for t in tokens for op in CONTROL_OPERATORS), (
            f"{where}: a shell control operator or glued comment can hide a "
            f"failing phase: {tokens}"
        )
        _runs_the_whole_suite(tokens, where)
    return p, s


def _runs_the_whole_suite(tokens: list[str], where: str) -> None:
    """A phase selects by marker only. Every token after `uv run pytest` is
    one of the allowed flags or the value of -m / -n; a path, a selector, an
    early-exit option (--version, --markers) or a plugin switch is refused."""
    rest = tokens[3:]
    skip_next = False
    for token in rest:
        if skip_next:
            skip_next = False
            continue
        assert any(rule.match(token) for rule in ALLOWED_TOKENS), (
            f"{where}: {token!r} is not one of the tokens a phase may carry"
        )
        if token in VALUE_FLAGS:
            skip_next = True


def _no_heredoc(text: str, where: str) -> None:
    """Executed text only (docs may show a heredoc in an unrelated example)."""
    assert HEREDOC not in text, (
        f"{where}: a heredoc can turn a phase command into data; none allowed here"
    )


# The release job legitimately runs on main pushes and manual dispatch only.
# Pinned verbatim: any other job-level `if` on a phase-running job is a way to
# skip the gate wholesale.
# The release job runs only in the canonical repository (the `canonical` job
# reads REPO_SLUG from the site), on a manual run or a push to main.
RELEASE_JOB_IF = (
    "needs.canonical.outputs.canonical == 'true' && "
    "(github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/main')"
)


def _cannot_be_softened(
    workflow: dict,
    step: dict,
    job: dict,
    where: str,
    *,
    job_if: str | None = None,
    agent_pr_gate: bool = False,
) -> None:
    """Nothing in the workflow may skip the step or a job it needs, hide its
    exit code, narrow it, or swap the fail-fast shell. GitHub's default `run`
    shell on a non-Windows runner is bash with -e; `shell: bash` adds
    pipefail; a custom template such as `bash {0}` drops -e, and then a red
    first phase is hidden by a green second one."""
    for key in ("continue-on-error", "if"):
        assert key not in step, f"{where}: `{key}` on the step lets a red phase pass"
    assert step.get("shell", "bash") == "bash", (
        f"{where}: `shell: {step.get('shell')}` replaces the fail-fast default"
    )
    assert "continue-on-error" not in job, (
        f"{where}: `continue-on-error` on the job lets a red phase pass"
    )
    if agent_pr_gate:
        # The only permitted `if` skips an unapproved agent PR and nothing a
        # human, an approved agent head or a push to main needs (#2033).
        assert "if" in job, f"{where}: the agent PR gate is missing"
        assert_job_gated(job, where)
        assert_job_gate_stays_live(job, where)
    else:
        assert job.get("if") == job_if, (
            f"{where}: job-level `if` is {job.get('if')!r}; expected {job_if!r}"
        )
    for scope, name in ((job, "job"), (workflow, "workflow")):
        assert "defaults" not in scope, (
            f"{where}: `defaults` on the {name} can replace the fail-fast shell"
        )
    for scope, name in ((step, "step"), (job, "job"), (workflow, "workflow")):
        env = scope.get("env") or {}
        assert "PYTEST_ADDOPTS" not in env, (
            f"{where}: PYTEST_ADDOPTS on the {name} can narrow both phases"
        )
    for name in _prerequisites(workflow, job):
        assert "if" not in workflow["jobs"][name], (
            f"{where}: prerequisite job `{name}` carries an `if`; a skipped "
            "prerequisite skips every job that needs it, this gate included"
        )


def _prerequisites(workflow: dict, job: dict) -> list[str]:
    """Every job reachable through `needs`, transitively."""
    seen: list[str] = []
    frontier = job.get("needs") or []
    frontier = [frontier] if isinstance(frontier, str) else list(frontier)
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.append(name)
        more = workflow["jobs"][name].get("needs") or []
        frontier.extend([more] if isinstance(more, str) else list(more))
    return seen


def test_ci_test_job_runs_both_phases_and_the_floor_on_the_joined_coverage() -> None:
    data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    job = data["jobs"]["test"]
    step = next(s for s in job["steps"] if s.get("name") == "Test (pytest)")
    run = step["run"]
    lines = [line for line in run.splitlines() if line.strip()]
    assert len(lines) == 2, f"the test step runs exactly the two phases, got {lines}"
    _no_heredoc(run, "ci.yml test job")
    p, s = _two_phases(run, "ci.yml test job")
    _cannot_be_softened(data, step, job, "ci.yml test job", agent_pr_gate=True)
    assert "--cov=src/kew" in p and "--cov=src/kew" in s
    assert "--cov-append" in s and "--cov-append" not in p, (
        "the serial phase must append to the parallel phase's coverage, and the "
        "parallel phase must start fresh, or the floor reads a stale file"
    )
    floors = [t for t in s if t.startswith("--cov-fail-under")]
    assert len(floors) == 1 and float(floors[0].split("=", 1)[1]) > 0, (
        f"the serial phase must apply one positive coverage floor: {s}"
    )
    assert not any(t.startswith("--cov-fail-under") for t in p), (
        "the floor applies once, to the joined coverage"
    )


def test_verify_sh_mirrors_both_phases() -> None:
    text = VERIFY_SH.read_text()
    _no_heredoc(text, "scripts/verify.sh")
    _two_phases(text, "scripts/verify.sh")


def test_verify_sh_fails_fast_before_the_phases() -> None:
    """Two commands instead of one: without `set -e` a red parallel phase is
    hidden by a green serial phase. The last `set` before the first phase
    must enable errexit."""
    lines = VERIFY_SH.read_text().splitlines()
    first_phase = next(
        i
        for i, line in enumerate(lines)
        if _phase_of(_pytest_commands(line)[0] if _pytest_commands(line) else [])
        == "parallel"
    )
    sets = [
        line.strip() for line in lines[:first_phase] if line.strip().startswith("set ")
    ]
    assert sets, "scripts/verify.sh has no `set` line before the phases"
    last = sets[-1]
    assert re.match(r"^set -[a-z]*e", last) and "+e" not in last, (
        f"scripts/verify.sh: errexit is off at the phases (last set line: {last!r})"
    )


def test_release_runs_each_complete_phase_once() -> None:
    data = yaml.safe_load((WORKFLOWS_DIR / "release.yml").read_text())
    hits = [
        (job_name, step, job)
        for job_name, job in data["jobs"].items()
        for step in job.get("steps", [])
        if any(_phase_of(command) for command in _pytest_commands(step.get("run", "")))
    ]
    assert len(hits) == 2, f"expected two release phase steps, got {hits}"

    phases: dict[str, tuple[str, dict, dict, list[str]]] = {}
    for job_name, step, job in hits:
        commands = [
            command for command in _pytest_commands(step["run"]) if _phase_of(command)
        ]
        assert len(commands) == 1, f"{job_name}: expected one phase, got {commands}"
        command = commands[0]
        phase = _phase_of(command)
        assert phase is not None
        assert phase not in phases, f"release.yml runs {phase} more than once"
        _no_heredoc(step["run"], f"release.yml {job_name}")
        _runs_the_whole_suite(command, f"release.yml {job_name}")
        has_worker_flag = any(WORKER_FLAG.match(token) for token in command)
        assert has_worker_flag == (phase == "parallel"), (
            f"release.yml {job_name}: wrong worker posture for {phase}"
        )
        phases[phase] = (job_name, step, job, command)

    assert set(phases) == {"parallel", "serial"}
    parallel_name, parallel_step, parallel_job, _ = phases["parallel"]
    serial_name, serial_step, serial_job, _ = phases["serial"]
    assert parallel_name == "release"
    assert serial_name == "serial-tests"
    needs = parallel_job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    assert serial_name in needs
    _cannot_be_softened(
        data,
        parallel_step,
        parallel_job,
        "release.yml release",
        job_if=RELEASE_JOB_IF,
    )
    _cannot_be_softened(
        data,
        serial_step,
        serial_job,
        "release.yml serial-tests",
    )


def test_contributor_docs_show_both_phases() -> None:
    _two_phases(CONTRIBUTING.read_text(), "CONTRIBUTING.md")
    _two_phases(README.read_text(), "README.md")


def test_contributor_docs_show_the_worker_cap_before_the_phases() -> None:
    """A copied `-n auto` with no cap starts one worker per logical CPU; the
    documented commands carry the same cap CI and verify.sh apply."""
    ci = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    cap = str(ci["jobs"]["test"]["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"])
    for path in (CONTRIBUTING, README):
        assert _docs_show_an_active_cap(path.read_text(), cap), (
            f"{path.name} shows -n auto without an active `export "
            f"PYTEST_XDIST_AUTO_NUM_WORKERS={cap}` line before it"
        )


def _docs_show_an_active_cap(text: str, cap: str) -> bool:
    """True when an ACTIVE export line (not commented, not echoed, not part of
    prose) precedes every documented parallel phase."""
    active = re.compile(
        rf"^export PYTEST_XDIST_AUTO_NUM_WORKERS={re.escape(cap)}(?:\s+#.*)?$",
        re.MULTILINE,
    )
    phases = [m.start() for m in re.finditer(re.escape(XDIST_PHASE), text)]
    exports = [m.start() for m in active.finditer(text)]
    return bool(phases) and all(any(e < p for e in exports) for p in phases)


_CAP_LINE = "export PYTEST_XDIST_AUTO_NUM_WORKERS="
_PHASE_LINE = 'uv run pytest -n auto -m "not serial"'  # == XDIST_PHASE, defined below


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        (f"{_CAP_LINE}6  # cap\n{_PHASE_LINE}", True),
        (f"{_CAP_LINE}6\n{_PHASE_LINE}", True),
        (f"# {_CAP_LINE}6\n{_PHASE_LINE}", False),
        (f"echo {_CAP_LINE}6\n{_PHASE_LINE}", False),
        (f"{_PHASE_LINE}\n{_CAP_LINE}6", False),
        (f"{_CAP_LINE}12\n{_PHASE_LINE}", False),
        (f"{_CAP_LINE}6\n{_PHASE_LINE}\n{_PHASE_LINE}", True),
        ("no phases at all", False),
    ],
)
def test_docs_cap_pin_table(text: str, ok: bool) -> None:
    assert _docs_show_an_active_cap(text, "6") is ok


# --- the host ---------------------------------------------------------------


def test_ci_caps_xdist_workers_per_matrix_leg() -> None:
    """The test leg shares one host with every other job and with local runs.

    The cap is sized for ONE expanded leg (#1816). A second dimension or a
    ``matrix.include`` entry would double the workers with this test green,
    so the expansion is pinned here too.
    """
    data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    job = data["jobs"]["test"]
    matrix = job["strategy"]["matrix"]
    assert list(matrix) == ["python-version"] and len(matrix["python-version"]) == 1, (
        f"the worker cap assumes one expanded test leg, matrix is {matrix!r}"
    )
    env = job.get("env", {})
    cap = env.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    assert cap is not None, "ci.yml test job no longer caps xdist workers per leg"
    assert 1 <= int(cap) <= 6, (
        f"per-leg worker cap {cap!r} exceeds half of the 12-core host"
    )
    step = next(s for s in job["steps"] if s.get("name") == "Test (pytest)")
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in (step.get("env") or {}), (
        "the step env overrides the job's worker cap"
    )


CAP_VAR = "PYTEST_XDIST_AUTO_NUM_WORKERS"
# The override only lowers the cap: two CI legs plus a local run at the
# host's 12 logical CPUs would take all 24. Raise it in verify.sh and here.
CAP_MAX = 6
_CAP_EXPORT = re.compile(
    rf'^export {CAP_VAR}="\$\{{{CAP_VAR}:-(\d+)\}}"$',
    re.MULTILINE,
)
_CAP_ASSIGNMENT = re.compile(rf"(^|[\s;&|(])(export\s+)?{CAP_VAR}=", re.MULTILINE)
_CAP_UNSET = re.compile(rf"\bunset\b[^\n]*\b{CAP_VAR}\b")
_SOURCED_FILE = re.compile(r"^\s*(source|\.)\s", re.MULTILINE)
# Anywhere on a code line, not only at its start: `if true; then uv() {...}; fi`.
_SHELL_FUNCTION = re.compile(r"(?:^|[\s;{(])(?:function\s+\w+|\w+\s*\(\s*\))")
XDIST_PHASE = 'uv run pytest -n auto -m "not serial"'
PROBE_EXIT = 86  # the stubbed xdist phase stops verify.sh here, under set -e


def _code_lines(text: str) -> str:
    """Drop full-line comments only.

    A trailing ``#`` is NOT treated as a comment: ``: "#"; X=24`` hides an
    assignment behind a quoted hash, so anything after code on a line stays
    visible to the checks below (a false positive names the line).
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _cap_default() -> str:
    text = VERIFY_SH.read_text()
    match = _CAP_EXPORT.search(text)
    assert match, "scripts/verify.sh no longer defaults the xdist worker cap"
    assert match.start() < text.index(XDIST_PHASE), (
        "the cap is exported after the xdist phase"
    )
    return match.group(1)


def test_verify_sh_defaults_the_same_worker_cap_before_the_phases() -> None:
    """Local runs share the host with both self-hosted runners.

    An uncapped `-n auto` on the dev box starts 24 workers next to a CI leg
    (the 2026-09-02 "murdered locally" incident). verify.sh must export the
    same default as ci.yml, before the xdist phase, from ONE authoritative
    assignment: no other assignment, unset, sourced file, or shell function
    (a `uv()` wrapper can strip the variable) may exist in the script.
    """
    default = _cap_default()
    ci = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    ci_cap = ci["jobs"]["test"]["env"][CAP_VAR]
    assert default == str(ci_cap), f"verify.sh default {default} != ci.yml cap {ci_cap}"
    assert int(default) <= CAP_MAX
    code = _code_lines(VERIFY_SH.read_text())
    assert len(_CAP_ASSIGNMENT.findall(code)) == 1, (
        f"scripts/verify.sh assigns {CAP_VAR} more than once"
    )
    assert not _CAP_UNSET.search(code), f"scripts/verify.sh unsets {CAP_VAR}"
    assert not _SOURCED_FILE.search(code), (
        "scripts/verify.sh sources a file; the cap could be changed out of sight"
    )
    assert not _SHELL_FUNCTION.search(code), (
        "scripts/verify.sh defines a shell function; a wrapper could shadow uv"
    )


_UV_STUB = f"""#!/bin/bash
# Stub `uv`: every gate before the xdist phase succeeds silently. The xdist
# phase itself records what pytest-xdist would see and stops the script.
printf '%s\n' "${{UV_CACHE_DIR-UNSET}}" >> "$KEW_UV_CACHE_PROBE"
case " $* " in
  *" pytest -n auto "*)
    printf '%s|%s' "${{{CAP_VAR}-UNSET}}" "${{BASH_ENV-UNSET}}" >> "$KEW_CAP_PROBE"
    exit {PROBE_EXIT}
    ;;
esac
exit 0
"""
_UVX_STUB = """#!/bin/bash
printf '%s\n' "${UV_CACHE_DIR-UNSET}" >> "$KEW_UV_CACHE_PROBE"
exit 0
"""
_GITLEAKS_STUB = """#!/bin/bash
printf '%s\n' "${UV_CACHE_DIR-UNSET}" >> "$KEW_UV_CACHE_PROBE"
case "$1" in
  version) echo 8.30.0 ;;
esac
exit 0
"""


def _run_verify_with_stubs(
    tmp_path: Path, caller: str | None, extra_env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run the REAL scripts/verify.sh, top to the xdist phase, in a cleared env.

    Every external tool before the phase is a stub on PATH, so the whole
    prefix executes: any earlier assignment, wrapper function, or sourced
    state that changes the cap shows up in what the stubbed xdist phase sees.
    The environment is built from scratch (no BASH_ENV, no exported
    functions, no inherited cap) unless ``extra_env`` adds some on purpose.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stubs = (("uv", _UV_STUB), ("uvx", _UVX_STUB), ("gitleaks", _GITLEAKS_STUB))
    for name, body in stubs:
        stub = stub_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    probe = tmp_path / "probe"
    env = {
        "PATH": f"{stub_dir}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "KEW_CAP_PROBE": str(probe),
        "KEW_UV_CACHE_PROBE": str(tmp_path / "uv-cache-probe"),
        "LC_ALL": "C",
    }
    if caller is not None:
        env[CAP_VAR] = caller
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(VERIFY_SH)],
        cwd=KEW_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if not probe.exists():
        return proc.returncode, "", proc.stderr
    seen, bash_env = probe.read_text().split("|", 1)
    assert bash_env == "UNSET", (
        f"BASH_ENV={bash_env!r} reached the xdist phase; every `bash scripts/*.sh` "
        "gate verify.sh starts would source it"
    )
    return proc.returncode, seen, proc.stderr


@pytest.mark.parametrize(
    ("caller", "expected"),
    [(None, "6"), ("", "6"), ("1", "1"), ("3", "3"), ("6", "6")],
)
def test_the_cap_reaches_the_xdist_command(
    tmp_path: Path, caller: str | None, expected: str
) -> None:
    code, seen, stderr = _run_verify_with_stubs(tmp_path, caller)
    assert code == PROBE_EXIT, (
        f"verify.sh did not stop at the stubbed xdist phase:\n{stderr}"
    )
    assert seen == expected, f"xdist saw {seen!r} for caller {caller!r}"


def test_verify_preserves_uv_cache_across_every_tool(tmp_path: Path) -> None:
    cache = tmp_path / "caller-cache"
    cache.mkdir()
    marker = cache / "caller-owned"
    marker.write_bytes(b"preserved")

    code, _seen, stderr = _run_verify_with_stubs(
        tmp_path, None, {"UV_CACHE_DIR": str(cache)}
    )

    assert code == PROBE_EXIT, stderr
    observed = (tmp_path / "uv-cache-probe").read_text().splitlines()
    assert len(observed) >= 3
    assert set(observed) == {str(cache)}
    assert marker.read_bytes() == b"preserved"


@pytest.mark.parametrize(
    "caller", ["abc", "0", "-1", "6 ", "1.5", "auto", "7", "12", "600", "1" * 20]
)
def test_a_malformed_cap_fails_instead_of_reverting_to_cpu_count(
    tmp_path: Path, caller: str
) -> None:
    """pytest-xdist int()s the value, warns on failure, and falls back to
    one worker per logical CPU; and it has no upper bound. verify.sh stops."""
    code, seen, stderr = _run_verify_with_stubs(tmp_path, caller)
    assert code == 1, f"verify.sh accepted {caller!r} (exit {code})"
    assert seen == "", "pytest ran despite the malformed cap"
    assert CAP_VAR in stderr


_STRIPPING_WRAPPER = f'() {{ env -u {CAP_VAR} command uv "$@"; }}'


@pytest.mark.parametrize(
    "inherited",
    [
        # Bash imports exported functions (BASH_FUNC_name%%) before running
        # a script; a caller's `uv` wrapper would shadow the real tool.
        {"BASH_FUNC_uv%%": _STRIPPING_WRAPPER},
        # BASH_ENV names a file bash sources before a non-interactive script.
        {"BASH_ENV": "WRAPPER_FUNCTION"},
        # ...or that file points the command hash at a wrapper executable.
        {"BASH_ENV": "WRAPPER_HASH"},
    ],
    ids=["exported-function", "BASH_ENV-function", "BASH_ENV-hash"],
)
def test_an_inherited_uv_wrapper_cannot_strip_the_cap(
    tmp_path: Path, inherited: dict[str, str]
) -> None:
    """verify.sh drops inherited shell state for its tools up front (functions,
    the command hash, BASH_ENV itself), so the value the script validated is
    the value the real xdist phase sees."""
    if inherited.get("BASH_ENV") == "WRAPPER_FUNCTION":
        wrapper = tmp_path / "bash_env.sh"
        wrapper.write_text(f"uv{_STRIPPING_WRAPPER}\n")
        inherited = {"BASH_ENV": str(wrapper)}
    elif inherited.get("BASH_ENV") == "WRAPPER_HASH":
        exe = tmp_path / "uv-wrapper"
        exe.write_text(f'#!/bin/bash\nenv -u {CAP_VAR} uv "$@"\n')
        exe.chmod(0o755)
        wrapper = tmp_path / "bash_env.sh"
        wrapper.write_text(f'hash -p "{exe}" uv\n')
        inherited = {"BASH_ENV": str(wrapper)}
    code, seen, stderr = _run_verify_with_stubs(tmp_path, "3", inherited)
    assert code == PROBE_EXIT, stderr
    assert seen == "3", f"an inherited uv wrapper changed the cap to {seen!r}"


def test_pytest_xdist_is_a_declared_dev_dependency() -> None:
    pyproject = tomllib.loads((KEW_DIR / "pyproject.toml").read_text())
    dev = pyproject["dependency-groups"]["dev"]
    names = [d.split(">")[0].split("=")[0].strip() for d in dev if isinstance(d, str)]
    assert "pytest-xdist" in names, "-n auto needs pytest-xdist in the dev group"
