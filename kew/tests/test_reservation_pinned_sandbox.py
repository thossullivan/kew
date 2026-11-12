"""Reservation identity must survive the sandbox launch wrapper."""

import inspect
from unittest.mock import patch

from kew import dispatch_core, dispatch_sandbox
from kew.config import DispatchConfig


def test_sandbox_wrapper_forwards_pinned_reservation(tmp_path):
    identity = {
        "expected_reservation_run_id": "reservation-A",
        "expected_reservation_generation": "reservation-A",
    }
    parameters = inspect.signature(dispatch_sandbox._run_sandboxed).parameters
    supported = {key: value for key, value in identity.items() if key in parameters}
    with (
        patch.object(
            dispatch_core, "_detect_sandbox_runtime", return_value="/mock/docker"
        ),
        patch.object(dispatch_sandbox, "_run_sandboxed_container") as launch,
    ):
        dispatch_sandbox._run_sandboxed(
            prompt="prompt",
            checkout_dir=tmp_path,
            config=DispatchConfig(),
            image="test-image",
            runner="claude-code",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            **supported,
        )
    assert launch.call_args.kwargs.get("expected_reservation_run_id") == "reservation-A"
    assert (
        launch.call_args.kwargs.get("expected_reservation_generation")
        == "reservation-A"
    )
