"""Generate a demo `dispatch.db` for the epic-DAG live-view demo (#454).

Drives the same scripted 5-node drain used by
`tests/test_epic_journal_e2e.py` (via `tests/support/epic_fixture.py`), but
standalone -- for Phase C's live-view demo/replay-verification capture.

Usage:
    uv run python scripts/make_demo_epic.py --out /tmp/kew-demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from support.epic_fixture import run_scripted_drain  # noqa: E402

EPIC_REF = "demo-epic"


class _MonkeyPatchShim:
    """Minimal `.setattr`/`.setitem` stand-in for pytest's `MonkeyPatch`.

    `run_scripted_drain` only ever calls the 3-arg `setattr` form and
    `setitem`, so recording/reverting originals isn't needed here: this
    process exits right after the drain finishes.
    """

    def setattr(self, target: Any, name: str, value: Any) -> None:
        setattr(target, name, value)

    def setitem(self, mapping: Any, key: Any, value: Any) -> None:
        mapping[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="directory to write dispatch.db into"
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "dispatch.db"
    if db_path.exists():
        db_path.unlink()
    Path(f"{db_path}-wal").unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)

    ticks = run_scripted_drain(
        db_path, epic_ref=EPIC_REF, monkeypatch=_MonkeyPatchShim()
    )

    print(f"wrote {db_path} ({len(ticks)} journaled ticks for epic '{EPIC_REF}')")
    print()
    print("View it with:")
    print(f"  cd {out_dir} && uv run kew serve")
    print(f"  then open http://127.0.0.1:8000/epics/{EPIC_REF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
