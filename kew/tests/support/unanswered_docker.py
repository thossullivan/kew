"""A ``docker`` whose ``create`` never answers (#3103, #3186).

kew's own process call times the create out and kills it, exactly as it does a
slow ``docker`` on a loaded host. Pair it with a small
``RUNTIME_CONTROL_TIMEOUT_SECONDS`` so the ceiling, not a sleep, decides the
test's length. Every create is counted in a state file the test reads back.

:meth:`UnansweredCreateDocker.install` puts it first on ``PATH`` as well as on
``TOOL_PATHS``: a drive tick re-registers its tools from ``PATH`` (the tick's
``preflight_tools``), and a fake only on ``TOOL_PATHS`` would be replaced by the
host's real ``docker`` mid-test.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kew import sandbox, subprocess_utils

_SCRIPT = """#!{python}
import json, sys, threading
from pathlib import Path

STATE = Path({state!r})
args = sys.argv[1:]
verb = args[0] if args else ""
if verb == "create":
    state = json.loads(STATE.read_text())
    state["creates"] += 1
    STATE.write_text(json.dumps(state))
    threading.Event().wait()
elif verb == "info":
    fields = len(args[args.index("--format") + 1].split())
    print(" ".join(["engine-1"] * fields))
elif verb == "image":
    print(json.dumps([{{"Id": "sha256:" + "f" * 64}}]))
else:
    # The name the create never made is free: kew may clear it and try again.
    sys.stderr.write(f"Error: No such container: {{args[-1]}}\\n")
    sys.exit(1)
"""


class UnansweredCreateDocker:
    """The executable and the count of creates it was asked for."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.state = root / "docker-state.json"
        self.state.write_text(json.dumps({"creates": 0}))
        self.path = root / "docker"
        self.path.write_text(
            _SCRIPT.format(python=sys.executable, state=str(self.state))
        )
        self.path.chmod(0o755)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make this the only ``docker`` the process can find, for the whole
        test: first on ``PATH`` (what every re-registration resolves),
        on the subprocess allowlist, and in the runtime-detection cache."""
        path = os.environ.get("PATH", "")
        monkeypatch.setenv("PATH", f"{self.root}{os.pathsep}{path}")
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(self.path))
        monkeypatch.setitem(sandbox._RUNTIME_CACHE, "docker", str(self.path))

    def creates(self) -> int:
        return int(json.loads(self.state.read_text())["creates"])
