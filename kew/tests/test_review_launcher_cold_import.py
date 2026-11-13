"""Cold-process integration guard for the standalone reviewer launcher."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reviewer_image_resolves(monkeypatch):
    """The reviewer now launches by the resolved immutable image ID (#1552);
    without a runtime these tests would refuse at resolution, so resolve to a
    stable fake ID."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


def test_standalone_reviewer_launcher_cold_imports_and_launches(
    tmp_path: Path,
) -> None:
    """The public launcher must work without priming ``dispatch_core``."""
    script = textwrap.dedent(
        """
        import dataclasses
        import json
        import sys
        from pathlib import Path
        from unittest.mock import patch

        assert "kew.dispatch_core" not in sys.modules
        from kew.graph.driver.review_launcher import sandbox_reviewer_launcher

        assert "kew.dispatch_core" not in sys.modules
        import kew.graph.driver.review_launcher as review_launcher
        import kew.sandbox as sandbox
        from kew.config import DispatchConfig, SandboxConfig
        from kew.review_dispatch import ReviewerLaunchContext
        from kew.run_store import SqliteRunStore
        from kew.review_stream import VERDICT_BEGIN, VERDICT_END
        from kew.sandbox import PROMPT_FILENAME

        root = Path(sys.argv[1])
        worktree = root / "worktree"
        worktree.mkdir()
        log_dir = root / "logs"
        config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
        config.logging.dir = str(log_dir)
        config.sandbox.runtime = "docker"
        store = SqliteRunStore(":memory:")
        ctx = ReviewerLaunchContext(
            item_id="1",
            run_id="cold-review",
            prompt="review the change",
            prompt_artifact_sha="sha256:prompt",
            head_sha="a" * 40,
            worktree=worktree,
            log_dir=log_dir,
        )
        launched = []

        verdict = {
            "verdict": "READY",
            "head_sha": "a" * 40,
            "findings": [],
            "acceptance_evidence": [
                {"command": "pytest -q", "output_digest": "sha256:x"}
            ],
        }
        final = f"{VERDICT_BEGIN}\\n{json.dumps(verdict)}\\n{VERDICT_END}"
        result_event = {
            "type": "result", "subtype": "success", "is_error": False,
            "result": final,
        }

        class Handle:
            def __iter__(self):
                return iter([json.dumps(result_event) + "\\n"])

            def wait(self):
                return 0

        # Match sandbox.launch_container, including cancellation admission.
        def fake_provider(
            argv, *, container_name=None, container_id=None, checkout_dir=None,
            admission=None, on_spawn=None, prepare=None, env=None, credential=None,
            separate_stderr=False,
        ):
            assert admission is not None
            # A review reads its verdict from the stream, so the container's
            # stderr must ride a second pipe (#2053).
            assert separate_stderr is True
            assert admission.store is store
            assert admission.run_id == ctx.run_id
            # The reviewer route here mints no bounded key, and no launch
            # environment ever carries the provisioning credential.
            assert credential is None
            assert "OPENROUTER_PROVISIONING_KEY" not in env
            launched.append(argv)
            return Handle()

        resolution_fields = {
            field.name for field in dataclasses.fields(sandbox.ImageResolution)
        }

        def built_label(image):
            # The inputs digest a kew build of a docker-<12 hex> pin carries,
            # where the resolution has a field for it.
            if "inputs_digest" not in resolution_fields:
                return {}
            return {"inputs_digest": image.rpartition("docker-")[2] + "0" * 52}

        with (
            patch.object(
                review_launcher,
                "detect_runtime_cached",
                return_value="/usr/bin/docker",
            ),
            # Image resolution also discovers the runtime in this child, both
            # through the cache and directly. The parent fixture cannot patch
            # this separate process.
            patch.object(
                sandbox, "detect_runtime_cached", return_value="/usr/bin/docker"
            ),
            patch.object(sandbox, "detect_runtime", return_value="/usr/bin/docker"),
            # The reviewer launches by the resolved immutable ID (#1552); no
            # runtime exists in this cold process, so resolve to a fake sha.
            # A kew-built image carries the inputs label its pin tag names
            # (as support.image_resolution.built_by_kew answers).
            patch.object(
                sandbox,
                "inspect_image_id",
                side_effect=lambda runtime, image: sandbox.ImageResolution(
                    sandbox.ImageIdStatus.RESOLVED,
                    image_id="sha256:" + "f" * 64,
                    **built_label(image),
                ),
            ),
            patch.object(sandbox, "launch_container", side_effect=fake_provider),
        ):
            collected = sandbox_reviewer_launcher(config, store)(ctx)

        assert launched
        assert launched[0][0] == "docker"
        assert collected.parsed.verdict == "READY"
        assert not (worktree / PROMPT_FILENAME).exists()
        store.close()
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, (
        "cold reviewer launcher process failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
