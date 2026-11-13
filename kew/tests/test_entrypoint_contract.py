"""Reference entrypoints acknowledge the current attempt before agent execution."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("packaged", [False, True])
# No codex: docker/agent-codex.Dockerfile is OpenShell-only and ships no
# entrypoint — `openshell sandbox exec` replaces one, and the docker lane for
# that runner does not exist (native containment, no in-container credential).
# Its pre-dispatch work lives in docker/kew-openshell-launch and is covered by
# tests/test_openshell_launcher.py. The reference script that used to be here
# was deleted with the image's arrival (#2317): it ran `codex exec "$@"` while
# CodexRunner.container_args() already sends `exec`.
@pytest.mark.parametrize("agent", ["claude", "opencode"])
def test_round_ten_entrypoint_acknowledges_before_exec(tmp_path, packaged, agent):
    root = Path(__file__).resolve().parents[2]
    base = root / ("kew/src/kew/_docker/docker" if packaged else "docker")
    name = (
        "agent-entrypoint.sh" if agent == "claude" else f"agent-{agent}-entrypoint.sh"
    )
    script = base / name
    assert script.exists(), f"Missing reference entrypoint: {name}"
    bins = tmp_path / "bin"
    bins.mkdir()
    for binary, body in {
        "git": "exit 0",
        "cp": "exit 0",
        agent: "printf 'agent-exec\\n'; cat",
    }.items():
        path = bins / binary
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    prompt = tmp_path / "prompt"
    prompt.write_text("workload-prompt\n")
    nonce = "a" * 32
    env = {
        **os.environ,
        "PATH": f"{bins}:/usr/bin:/bin",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "KEW_PROMPT_FILE": str(prompt),
        "KEW_AGENT_START_NONCE": nonce,
    }
    reply = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, check=False
    )
    assert reply.stdout.splitlines() == [
        f"KEW_AGENT_START {nonce}",
        "agent-exec",
        "workload-prompt",
    ]
    assert reply.returncode == 0, reply.stderr
    prompt.unlink()
    refused = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, check=False
    )
    assert refused.returncode == 64
    assert "KEW_AGENT_START" not in refused.stdout


def test_round_ten_host_binds_each_attempt_to_a_fresh_nonce(tmp_path):
    from unittest.mock import patch

    from kew.sandbox import build_run_argv, build_worker_agent_exec_argv

    nonces = []
    with patch("kew.sandbox.resource_limit_args", return_value=[]):
        for _attempt in range(2):
            run = build_run_argv(
                runtime="docker",
                image="image",
                checkout_dir=tmp_path,
                slug="42",
                network="open",
                runner="claude-code",
            )
            execute = build_worker_agent_exec_argv(
                runtime="docker", worker_id="a" * 64, command=["--print"]
            )
            for argv in (run, execute):
                values = [
                    arg.split("=", 1)[1]
                    for arg in argv
                    if arg.startswith("KEW_AGENT_START_NONCE=")
                ]
                assert len(values) == 1
                nonce = values[0]
                assert len(nonce) == 32 and all(
                    char in "0123456789abcdef" for char in nonce
                )
                nonces.append(nonce)
    assert len(set(nonces)) == 4
