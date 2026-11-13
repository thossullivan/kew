#!/usr/bin/env bash
# The opted-in run must really select the Switchyard image tests.
#
# KEW_SWITCHYARD_IMAGE=1 declares that this machine holds the pinned private
# Switchyard source input, so the tests marked requires_switchyard_image
# (kew/tests/conftest.py) run here — they are the sidecar packet
# certification. The image tooling is opt-in, so a drifting marker, a broken
# deselection or a mistyped switch would quietly empty that set in a job that
# believes it runs it, and nothing would go red. Count the selection before
# the suite starts and refuse an empty one.
#
# Without the switch there is nothing to assert: the marked tests are
# deselected by design (docs/operations.md, "Public repository runner rule").
set -euo pipefail

cd "$(dirname "$0")/../../kew"

marker="requires_switchyard_image"

if [ "${KEW_SWITCHYARD_IMAGE:-}" != "1" ]; then
    echo "KEW_SWITCHYARD_IMAGE is not 1: the $marker tests are deselected here, so there is nothing to assert."
    exit 0
fi

# `grep -c` exits 1 on no match and pytest exits 5 on an empty selection, so
# both report through the count, not through a failed pipeline.
selected="$(uv run pytest -m "$marker" --collect-only -q tests | grep -c '::' || true)"

if [ "$selected" -lt 1 ]; then
    echo "::error::KEW_SWITCHYARD_IMAGE=1 but no @pytest.mark.$marker test was selected, so this job would not run the sidecar packet certification." >&2
    exit 1
fi

echo "KEW_SWITCHYARD_IMAGE=1: $selected @pytest.mark.$marker tests selected."
