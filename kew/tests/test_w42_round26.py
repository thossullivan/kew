"""Round 26 contract regression, PR #1943.

``finalize_interruption`` retried the durable finalization path forever when
that path raised a BaseException that is not an Exception. The arm that caught
it recorded the deferred cancellation but neither counted the attempt nor left
the loop, so cleanup spun without sleeping, the run row stayed pending, and no
terminal record was ever written.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from test_w42_round21 import _seed

from kew.dispatch_admission import PendingAdmission
from kew.run_store import _BUSY_MAX_RETRIES, SqliteRunStore


def _reserve(store):
    _seed(store, "cancelled", started=False)
    assert store.reserve_epic_node_item("round26", "42", "cancelled")


def test_round26_cancellation_cleanup_stops_retrying_a_base_exception():
    """The loop leaves after a bounded number of attempts instead of spinning."""
    calls = 0

    def always_cancels(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt("second operator interrupt")

    with SqliteRunStore(":memory:") as store:
        _reserve(store)
        with (
            patch(
                "kew.dispatch_admission._finalize_interrupted_admission",
                side_effect=always_cancels,
            ),
            patch("time.sleep") as sleep,
            pytest.raises(KeyboardInterrupt) as caught,
            PendingAdmission(store, "cancelled"),
        ):
            raise KeyboardInterrupt("operator")

    assert calls == _BUSY_MAX_RETRIES, "the retry loop is bounded, not endless"
    sleep.assert_not_called(), "a bounded retry does not need to back off"
    assert str(caught.value) == "operator", "the original cancellation stays primary"
    assert isinstance(caught.value.__cause__, KeyboardInterrupt), (
        "the repeated interruption is recorded as the cause"
    )
