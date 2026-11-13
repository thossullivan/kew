"""Export the FastAPI OpenAPI schema; --check fails if openapi.json is stale.

The schema is committed and `--check` runs as a gate on both execution paths —
ci.yml's `lint` job and `scripts/verify.sh` (parity: tests/test_verify_parity.py)
— so the frontend's contract never silently drifts from the backend. Until #1438
that was a claim this docstring made and nothing enforced.

What the committed file must then account for is
tests/test_openapi_route_coverage.py: every write route documented in
docs/security.md, every read route explicitly allowlisted.
"""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path

from fastapi.testclient import TestClient

from kew.serve.app import create_app

OUT = Path(__file__).resolve().parent.parent / "openapi.json"


class _EmptyStore:
    def list_all(self) -> list[object]:
        return []


def schema() -> dict[str, object]:
    app = create_app(lambda: nullcontext(_EmptyStore()), repo_dir=Path("."))
    with TestClient(app):
        return app.openapi()


def main() -> int:
    current = json.dumps(schema(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        existing = OUT.read_text() if OUT.exists() else ""
        if existing != current:
            print(
                "openapi.json is stale — run: uv run python scripts/export_openapi.py"
            )
            return 1
        return 0
    OUT.write_text(current)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
