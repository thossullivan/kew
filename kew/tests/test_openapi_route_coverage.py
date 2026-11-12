"""Every route in the committed OpenAPI schema is accounted for.

``scripts/export_openapi.py --check`` pins that ``openapi.json`` matches what
the app actually serves (the gate now runs in ci.yml's ``lint`` job and in
``scripts/verify.sh``; parity pinned by ``tests/test_verify_parity.py``). This
file pins the other half: that the schema matches what the *documentation*
claims the surface is.

Two arms, one rule — no route may exist unaccounted for:

* **Writes are documented.** Every POST/PUT/PATCH/DELETE path must appear in
  the write-surface table in ``docs/security.md``, and that table may not name
  a route the app no longer serves. The prose count ("these twelve routes",
  "the thirteenth write-shaped route") is parsed and checked against the table,
  so the "exactly N writes" claim class — a trust-critical understatement when
  it drifted, per the 2026-08-06 feature census — is now mechanically
  falsifiable rather than honour-system.
* **Reads are explicitly allowlisted.** Published docs do not enumerate the
  read endpoints, so each one is listed below with the surface it serves.
  Adding a read route fails this test until someone writes that line.

Path parameter names are normalized away (``{run_id}`` and ``{id}`` compare
equal), because the docs name parameters for the reader and FastAPI names them
for the handler; a rename is not a surface change. Route converters remain in
the key, so ``{id:int}`` and ``{name:str}`` stay distinct.

The committed schema's freshness is checked here too, not only by the CLI gate
in CI and ``verify.sh`` — a change that runs just the tests covering its own
files would otherwise never see the drift it caused.

All of that rests on the drift gate actually failing on drift, so the last test
here proves that too rather than assuming it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route, Router

from kew.serve.app import create_app

KEW_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = KEW_DIR.parent
OPENAPI_JSON = KEW_DIR / "openapi.json"
SECURITY_DOC = REPO_ROOT / "docs" / "security.md"
EXPORT_SCRIPT = KEW_DIR / "scripts" / "export_openapi.py"

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The one write-shaped route the security doc deliberately excludes from its
# table: it is the token->session exchange itself, so it cannot depend on the
# session every other write requires. Named in the prose immediately after the
# table, and pinned by the ordinal check below.
LOGIN_ROUTE = ("POST", "/api/auth/login")

# Read routes, each with the product surface it serves. This is the
# "explicitly allowlisted" arm: a new read route must be added here
# consciously. Stale entries fail too (see the no-phantom test), so the list
# cannot rot into a rubber stamp.
ALLOWLISTED_READS = {
    ("GET", "/api/approvals"): "inbox: the held-dispatch queue",
    ("GET", "/api/audit"): "audit view: the event feed",
    ("GET", "/api/audit/status"): "audit view: hash-chain verification status",
    ("GET", "/api/audit/histogram"): "audit view: event counts over a window",
    ("GET", "/api/auth/session"): "unauthenticated probe — is a login required?",
    ("GET", "/api/bundle"): "evidence bundle for one run",
    ("GET", "/api/config/budget"): "budget view: current spend caps",
    ("GET", "/api/config/effective"): "health view: 4-layer config provenance",
    ("GET", "/api/config/human_review"): "budget view: human-review settings",
    ("GET", "/api/config/overridable"): "budget view: which fields accept an override",
    ("GET", "/api/dashboard"): "cockpit shell: fleet summary",
    ("GET", "/api/dashboard/work"): "cockpit shell: work projections",
    (
        "GET",
        "/api/dispatch/resolve",
    ): "dispatch launchpad: the resolved route, with provenance",
    ("GET", "/api/epics"): "epics view: the epic list",
    ("GET", "/api/models"): "config page: the model catalog the model picker lists",
    ("GET", "/api/prompts"): "prompt catalog: every agent role and its prompt source",
    ("GET", "/api/prompts/{}"): "prompt catalog: one role's base prompt preview",
    ("GET", "/api/prompts/{}/versions"): (
        "prompt catalog: one role's operator profile history and generation"
    ),
    ("GET", "/api/prompts/{}/outcomes"): (
        "prompt catalog: one role's review outcomes per profile version"
    ),
    ("GET", "/api/run-requests"): "Run button: the newest run requests and the drive",
    ("GET", "/api/run-requests/{}"): "Run button: what the drive did with one request",
    ("GET", "/api/epics/{}"): "epics view: one epic's DAG",
    ("GET", "/api/epics/{}/nodes/{}"): "epics view: one node's detail",
    ("GET", "/api/epics/{}/stream"): "epics view: live DAG updates (SSE)",
    ("GET", "/api/epics/{}/ticks"): "epics view: driver tick history",
    ("GET", "/api/epics/{}/tick-strip"): "epics view: driver tick-attempt strip",
    ("GET", "/api/epics/{}/timeline"): "epics view: per-node run timeline",
    ("GET", "/api/fleet/status"): "inbox: fleet status strip",
    ("GET", "/api/health"): "health view: runtimes, images, posture, daemons",
    ("GET", "/api/health/env"): "health view: env var names and whether each is set",
    ("GET", "/api/readiness"): "readiness: the kew doctor checks, cached 60s",
    ("GET", "/api/loops"): "loops view: the loop list",
    ("GET", "/api/loops/{}"): "loops view: one loop's status",
    ("GET", "/api/loops/{}/audit"): "loops view: one loop's audit trail",
    ("GET", "/api/loops/{}/iterations"): "loops view: one loop's iteration strip",
    ("GET", "/api/meta"): "client bootstrap: server identity and capabilities",
    ("GET", "/api/prs/freshness"): "epic/work views: artifact staleness badges",
    ("GET", "/api/report/models"): "scorecard: cost per model",
    ("GET", "/api/report/spend"): "scorecard: spend report",
    ("GET", "/api/report/outcomes"): "scorecard: weekly outcome scorecard",
    ("GET", "/api/runs"): "runs view: fleet history",
    ("GET", "/api/runs/{}"): "runs view: one run's detail",
    ("GET", "/api/runs/{}/audit"): "run page: this run's ledger events, paged forward",
    ("GET", "/api/runs/{}/events"): "runs view: live run events (SSE)",
    ("GET", "/api/runs/{}/flight"): "runs view: flight-recorder frames",
    ("GET", "/api/runs/{}/journal"): "runs view: the run journal",
    ("GET", "/api/runs/{}/nodes/{}/diff"): "runs view: one node's diff",
    ("GET", "/api/runs/{}/prompt"): "runs view: the dispatched prompt (sensitive read)",
    ("GET", "/api/runs/{}/timeline"): "runs view: the run timeline",
    ("GET", "/api/verbs/work/{}"): "work projection for one tracker item",
    ("GET", "/api/work/{}/pipeline"): "work view: one item's pipeline stages",
    ("GET", "/api/work/{}/evidence"): "work view: one item's evidence chain",
    ("GET", "/api/work/{}/description"): "work view: one item's tracker description",
}

# Written-out counts the security doc uses in prose. Loud on an unmapped word
# rather than skipping the check: an unparsed count is an unchecked claim.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}

# `| `POST /api/verbs/resume` | Lift the fleet latch |` — a table row naming a
# method and a path.
DOC_TABLE_ROW = re.compile(r"^\|\s*`([A-Z]+)\s+(/api/\S*)`\s*\|")
WRITE_SURFACE_SECTION_HEADING = "## Approval inbox"
WRITE_SURFACE_TABLE_HEADER = "| Route | What it writes |"
MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# One group per quote level, repeated: `> > ` nests two blockquotes, and a
# fence inside the inner one is still a fence (round-4 review).
MARKDOWN_BLOCKQUOTE = re.compile(r"^(?: {0,3}> ?)+")
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
DOC_WRITE_COUNT = re.compile(r"write surface, at HEAD, is these (\w+) routes")
DOC_LOGIN_ORDINAL = re.compile(
    r"`POST /api/auth/login` is the (\w+) write-shaped route"
)


def _normalized_parameter(match: re.Match[str]) -> str:
    """Drop one parameter name while retaining its route converter."""
    _, separator, converter = match.group(1).partition(":")
    return f"{{:{converter}}}" if separator else "{}"


def _normalize(path: str) -> str:
    """Drop parameter names while retaining route converters."""
    return re.sub(r"\{([^}]*)\}", _normalized_parameter, path)


def _schema_routes() -> list[tuple[str, str]]:
    """Every ``(METHOD, normalized path)`` in the committed schema."""
    paths = json.loads(OPENAPI_JSON.read_text())["paths"]
    return sorted(
        (method.upper(), _normalize(path))
        for path, operations in paths.items()
        for method in operations
    )


def _unfenced_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield Markdown lines outside backtick and tilde code fences."""
    fence: tuple[str, int] | None = None
    for line in lines:
        fence_line = MARKDOWN_BLOCKQUOTE.sub("", line, count=1)
        marker = MARKDOWN_FENCE.match(fence_line)
        if fence is None:
            if marker is None:
                yield line
                continue
            marker_text = marker.group(1)
            if marker_text[0] == "`" and "`" in marker.group(2):
                yield line
                continue
            fence = (marker_text[0], len(marker_text))
            continue
        if marker is None:
            continue
        marker_text = marker.group(1)
        if (
            marker_text[0] == fence[0]
            and len(marker_text) >= fence[1]
            and not marker.group(2).strip()
        ):
            fence = None


def _documented_writes() -> set[tuple[str, str]]:
    """Every write route in the security doc's write-surface section."""
    lines = list(_unfenced_lines(SECURITY_DOC.read_text().splitlines()))
    sections = [
        index
        for index, line in enumerate(lines)
        if line.strip() == WRITE_SURFACE_SECTION_HEADING
    ]
    assert len(sections) == 1, (
        "docs/security.md must contain exactly one write-surface section headed "
        f"{WRITE_SURFACE_SECTION_HEADING!r}"
    )
    section_end = next(
        (
            index
            for index in range(sections[0] + 1, len(lines))
            if MARKDOWN_HEADING.match(lines[index])
        ),
        len(lines),
    )
    section_lines = lines[sections[0] + 1 : section_end]
    headers = [
        index
        for index, line in enumerate(section_lines)
        if line.strip() == WRITE_SURFACE_TABLE_HEADER
    ]
    assert len(headers) == 1, (
        "docs/security.md's write-surface section must contain exactly one table "
        f"headed {WRITE_SURFACE_TABLE_HEADER!r}"
    )
    table_lines = []
    for line in section_lines[headers[0] + 2 :]:
        if not line.startswith("|"):
            break
        table_lines.append(line)
    return {
        (match.group(1), _normalize(match.group(2)))
        for line in table_lines
        if (match := DOC_TABLE_ROW.match(line)) and match.group(1) in WRITE_METHODS
    }


def _route_path(prefix: str, path: str) -> str:
    """Join one route path to its accumulated mount prefix."""
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _walk_routes(
    routes: Iterable[Any], prefix: str = "", schema_prefix: str = ""
) -> Iterator[tuple[Any, str, str]]:
    """Yield every served leaf route with its full mounted path."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _walk_routes(candidates(), prefix, schema_prefix)
            continue
        concrete_route = getattr(route, "starlette_route", None) or route
        if isinstance(concrete_route, Mount):
            path = _route_path(prefix, concrete_route.path)
            path_format = concrete_route.path_format.removesuffix("/{path}")
            schema_path = _route_path(schema_prefix, path_format)
            subroutes = getattr(concrete_route.app, "routes", None)
            assert subroutes is not None, f"mount {path!r} has no walkable routes"
            yield from _walk_routes(subroutes, path, schema_path)
            continue
        assert hasattr(concrete_route, "path"), (
            f"unwalkable route object: {concrete_route!r}"
        )
        yield (
            concrete_route,
            _route_path(prefix, concrete_route.path),
            _route_path(schema_prefix, concrete_route.path_format),
        )


def _hidden_api_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Return API method/path pairs omitted from the generated schema."""
    schema_routes = {
        (method.upper(), _normalize(path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    served_routes = []
    for _route, path, path_format in _walk_routes(app.routes):
        if not (path == "/api" or path.startswith("/api/")):
            continue
        methods = getattr(_route, "methods", None) or {"WEBSOCKET"}
        served_routes.extend(
            (method.upper(), path, _normalize(path_format)) for method in methods
        )
    route_counts = Counter(
        (method, path_format) for method, _, path_format in served_routes
    )
    hidden = set()
    for method, path, path_format in served_routes:
        key = (method, path_format)
        if key not in schema_routes or route_counts[key] > 1:
            hidden.add((method, path))
    return hidden


def _is_allowlisted_read(
    route: tuple[str, str], allowlist: Mapping[tuple[str, str], str]
) -> bool:
    """Return whether an exact method and normalized path are allowlisted."""
    return route in allowlist


def test_documented_writes_only_reads_the_write_surface_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the unfenced table in the write-surface section is authoritative."""
    security_doc = tmp_path / "security.md"
    security_doc.write_text(
        """\
## Archived routes

| Route | What it writes |
|---|---|
| `POST /api/evil` | Unrelated inventory |

## Approval inbox

```markdown
| Route | What it writes |
|---|---|
| `POST /api/fenced` | Example only |
```

| Route | What it writes |
|---|---|
| `POST /api/approvals/{issue}` | Record a decision |

## Historical appendix

| Route | What it writes |
|---|---|
| `POST /api/archived` | Old route |
"""
    )
    monkeypatch.setattr(sys.modules[__name__], "SECURITY_DOC", security_doc)

    assert _documented_writes() == {("POST", "/api/approvals/{}")}


def test_unfenced_lines_respects_fence_character_and_length() -> None:
    """Only a CommonMark-compatible marker closes a fenced block."""
    lines = [
        "````markdown",
        "hidden before short marker",
        "```",
        "hidden after short marker",
        "````",
        "visible after backticks",
        "~~~markdown",
        "hidden by tildes",
        "~~~",
        "visible after tildes",
    ]

    assert list(_unfenced_lines(lines)) == [
        "visible after backticks",
        "visible after tildes",
    ]


def test_unfenced_lines_accepts_a_longer_matching_closer() -> None:
    """A longer marker of the opening character closes the fence."""
    lines = ["~~~markdown", "hidden", "~~~~", "visible"]

    assert list(_unfenced_lines(lines)) == ["visible"]


def test_unfenced_lines_rejects_a_different_closing_character() -> None:
    """A marker using the other fence character cannot close the fence."""
    lines = ["````markdown", "hidden", "~~~~", "still hidden", "````", "visible"]

    assert list(_unfenced_lines(lines)) == ["visible"]


def test_written_out_counts_ignore_fenced_decoys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fenced prose cannot satisfy the write-count assertions."""
    security_doc = tmp_path / "security.md"
    security_doc.write_text(
        """\
```markdown
The write surface, at HEAD, is these twenty routes.
`POST /api/auth/login` is the twentieth write-shaped route.
```

## Approval inbox

The write surface, at HEAD, is these one routes.

| Route | What it writes |
|---|---|
| `POST /api/approvals/{issue}` | Record a decision |

`POST /api/auth/login` is the second write-shaped route.
"""
    )
    monkeypatch.setattr(sys.modules[__name__], "SECURITY_DOC", security_doc)

    test_the_written_out_write_count_matches_the_surface()


@pytest.mark.parametrize("quote_depth", [1, 2])
def test_written_out_counts_ignore_blockquoted_fenced_decoys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quote_depth: int
) -> None:
    """A fenced example inside a blockquote cannot satisfy prose checks.

    Depth 2 nests the quote (`> > `); a parser that strips one marker sees
    the inner fence line as prose and counts the decoy (round-4 review).
    """
    quote = "> " * quote_depth
    security_doc = tmp_path / "security.md"
    security_doc.write_text(
        f"""\
{quote}````markdown
{quote}The write surface, at HEAD, is these twenty routes.
{quote}`POST /api/auth/login` is the twentieth write-shaped route.
{quote}````

## Approval inbox

The write surface, at HEAD, is these one routes.

| Route | What it writes |
|---|---|
| `POST /api/approvals/{{issue}}` | Record a decision |

`POST /api/auth/login` is the second write-shaped route.
"""
    )
    monkeypatch.setattr(sys.modules[__name__], "SECURITY_DOC", security_doc)

    test_the_written_out_write_count_matches_the_surface()


def test_read_allowlist_is_consumed_with_the_method() -> None:
    """Allowlisting GET cannot grant cover to POST on the same path."""
    allowlist = {("GET", "/api/x"): "test read"}

    assert _is_allowlisted_read(("GET", "/api/x"), allowlist)
    assert not _is_allowlisted_read(("POST", "/api/x"), allowlist)


def test_normalize_preserves_path_converters() -> None:
    """Converters remain distinct when parameter names differ."""
    assert _normalize("/api/x/{id:int}") == "/api/x/{:int}"
    assert _normalize("/api/x/{name:str}") == "/api/x/{:str}"


def test_read_allowlist_keys_have_the_expected_shape() -> None:
    """Every read allowlist key names one method and one API path."""
    assert all(
        isinstance(route, tuple)
        and len(route) == 2
        and route[0] not in WRITE_METHODS
        and route[1].startswith("/api/")
        for route in ALLOWLISTED_READS
    )


def test_hidden_api_route_detection_walks_mounted_routers() -> None:
    """A mounted hidden route remains visible to the coverage gate."""
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.post("/evil", include_in_schema=False)
    async def _evil() -> None:
        return None

    app = FastAPI()
    app.mount("/api", api)

    assert _hidden_api_routes(app) == {("POST", "/api/evil")}


def test_hidden_api_route_detection_walks_included_mounts() -> None:
    """An included raw mount remains visible through effective candidates."""

    async def _leak(_request: object) -> None:
        return None

    nested = Router(
        routes=[Route("/leak", _leak, methods=["POST"], include_in_schema=False)]
    )
    router = APIRouter()
    router.mount("/nested", nested)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    assert _hidden_api_routes(app) == {("POST", "/api/nested/leak")}


def test_hidden_api_route_detection_combines_all_nested_prefixes() -> None:
    """Included mounts retain prefixes through nested included raw routes."""

    async def _leak(_request: object) -> None:
        return None

    leaf = APIRouter()
    leaf.add_route("/leak", _leak, methods=["PATCH"], include_in_schema=False)
    child = APIRouter()
    child.include_router(leaf, prefix="/child")
    outer = APIRouter()
    outer.mount("/mounted", child)
    app = FastAPI()
    app.include_router(outer, prefix="/api")

    assert _hidden_api_routes(app) == {("PATCH", "/api/mounted/child/leak")}


def test_hidden_api_route_detection_walks_included_websockets() -> None:
    """An included WebSocket remains visible through effective candidates."""

    async def _socket(_websocket: object) -> None:
        return None

    router = APIRouter()
    router.add_websocket_route("/socket", _socket)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    assert _hidden_api_routes(app) == {("WEBSOCKET", "/api/socket")}


def test_mounted_sub_app_routes_are_missing_from_the_parent_schema() -> None:
    """A mounted sub-app route is hidden even when its own flag is true."""
    sub_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    child = APIRouter()

    @child.post("/write")
    async def _write() -> None:
        return None

    sub_app.include_router(child)
    app = FastAPI()
    app.mount("/api", sub_app)

    assert _hidden_api_routes(app) == {("POST", "/api/write")}


def test_raw_app_route_is_missing_from_the_parent_schema() -> None:
    """A raw Starlette route is hidden even when its flag is true."""

    async def _write(_request: object) -> None:
        return None

    app = FastAPI()
    app.add_route(
        "/api/raw", _write, methods=["POST", "DELETE"], include_in_schema=True
    )

    assert _hidden_api_routes(app) == {
        ("DELETE", "/api/raw"),
        ("POST", "/api/raw"),
    }


def test_shadowing_mount_cannot_borrow_parent_schema_membership() -> None:
    """A mounted handler cannot hide behind a later documented handler."""
    sub_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @sub_app.post("/write")
    async def _hidden_write() -> None:
        return None

    app = FastAPI()
    app.mount("/api", sub_app)

    @app.post("/api/write")
    async def _documented_write() -> None:
        return None

    assert _hidden_api_routes(app) == {("POST", "/api/write")}


def test_duplicate_routes_fail_even_when_identity_fields_match() -> None:
    """Duplicate runtime routes fail independently of handler identity."""

    async def _write() -> None:
        return None

    sub_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sub_app.add_api_route(
        "/write", _write, methods=["POST"], operation_id="shared_write"
    )
    app = FastAPI()
    app.mount("/api", sub_app)
    app.add_api_route(
        "/api/write", _write, methods=["POST"], operation_id="shared_write"
    )

    assert _hidden_api_routes(app) == {("POST", "/api/write")}


def test_openapi_extra_operation_id_does_not_hide_a_documented_route() -> None:
    """An explicit schema operationId does not change route membership."""
    app = FastAPI()

    app.add_api_route(
        "/api/write",
        lambda: None,
        methods=["POST"],
        openapi_extra={"operationId": "public_write"},
    )

    assert not _hidden_api_routes(app)


def test_converter_routes_fail_as_duplicate_schema_claims() -> None:
    """Routes differing only by converters cannot share one schema key."""
    app = FastAPI()

    @app.get("/api/items/{item_id:int}")
    async def _integer_item(item_id: int) -> None:
        return None

    @app.get("/api/items/{item_name:str}")
    async def _string_item(item_name: str) -> None:
        return None

    assert _hidden_api_routes(app) == {
        ("GET", "/api/items/{item_id:int}"),
        ("GET", "/api/items/{item_name:str}"),
    }


def test_route_converter_matches_its_openapi_path_format() -> None:
    """A converter-bearing documented route matches its exported path."""
    app = FastAPI()

    @app.post("/api/items/{item_id:int}")
    async def _item(item_id: int) -> None:
        return None

    assert not _hidden_api_routes(app)


def test_lifespan_added_hidden_route_is_detected() -> None:
    """The runtime walk includes routes registered during startup."""

    async def _late() -> None:
        return None

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.add_api_route("/api/late", _late, methods=["POST"], include_in_schema=False)
        yield

    app = FastAPI(lifespan=_lifespan)

    with TestClient(app):
        assert _hidden_api_routes(app) == {("POST", "/api/late")}


def test_every_served_api_route_is_included_in_schema() -> None:
    """Every live API route must remain visible to the OpenAPI drift gate."""
    app = create_app(lambda: nullcontext(object()), repo_dir=KEW_DIR)

    with TestClient(app):
        hidden = _hidden_api_routes(app)

    assert not hidden, (
        "served API routes are hidden from OpenAPI (set include_in_schema=True; "
        "a hidden route bypasses both the drift and documentation gates):\n"
        + "\n".join(f"  {method} {path}" for method, path in sorted(hidden))
    )


def test_every_write_route_is_documented() -> None:
    """No write route may exist without a row in docs/security.md."""
    schema_writes = {r for r in _schema_routes() if r[0] in WRITE_METHODS}
    undocumented = schema_writes - _documented_writes() - {LOGIN_ROUTE}
    assert not undocumented, (
        "write routes missing from the write-surface table in docs/security.md "
        "(add a row naming the route and what it writes — an undocumented "
        "write surface is exactly the claim drift this test exists to catch):\n"
        + "\n".join(f"  {method} {path}" for method, path in sorted(undocumented))
    )


def test_the_documented_write_surface_names_no_phantom_route() -> None:
    """The doc may not claim a write route the app no longer serves."""
    schema_writes = {r for r in _schema_routes() if r[0] in WRITE_METHODS}
    phantom = _documented_writes() - schema_writes
    assert not phantom, (
        "docs/security.md's write-surface table names routes absent from "
        "openapi.json (delete the row, or regenerate the schema with "
        "`uv run python scripts/export_openapi.py` if the route is new):\n"
        + "\n".join(f"  {method} {path}" for method, path in sorted(phantom))
    )


def test_the_written_out_write_count_matches_the_surface() -> None:
    """ "...is these twelve routes" is checked, not trusted.

    The census found this sentence claiming two writes while eight existed.
    The count is now a fact about the schema, so understating it fails here.
    """
    text = "\n".join(_unfenced_lines(SECURITY_DOC.read_text().splitlines()))
    match = DOC_WRITE_COUNT.search(text)
    assert match is not None, (
        "docs/security.md no longer states its write-surface count in the form "
        f"matched by {DOC_WRITE_COUNT.pattern!r}. The sentence is a trust claim; "
        "keep it in a checkable form rather than dropping the check with it."
    )
    word = match.group(1).lower()
    assert word in NUMBER_WORDS, (
        f"unmapped count word {word!r} in docs/security.md — add it to "
        "NUMBER_WORDS rather than leaving the claim unchecked."
    )
    documented = _documented_writes()
    assert NUMBER_WORDS[word] == len(documented), (
        f"docs/security.md says the write surface is {word} ({NUMBER_WORDS[word]}) "
        f"routes, but its own table lists {len(documented)}."
    )

    ordinal = DOC_LOGIN_ORDINAL.search(text)
    assert ordinal is not None, (
        "docs/security.md no longer names POST /api/auth/login as the "
        "write-shaped exception to that table. It is the one write route the "
        "table omits; unnamed, the count above silently undercounts it."
    )
    ordinal_word = ordinal.group(1).lower()
    assert ordinal_word in ORDINAL_WORDS, (
        f"unmapped ordinal {ordinal_word!r} in docs/security.md — add it to "
        "ORDINAL_WORDS rather than leaving the claim unchecked."
    )
    assert ORDINAL_WORDS[ordinal_word] == len(documented) + 1, (
        f"docs/security.md calls login the {ordinal_word} write-shaped route, "
        f"but the table lists {len(documented)} others."
    )


def test_every_read_route_is_documented_or_allowlisted() -> None:
    """No read route may exist without a line saying what it serves."""
    unaccounted = [
        (method, path)
        for method, path in _schema_routes()
        if method not in WRITE_METHODS
        and not _is_allowlisted_read((method, path), ALLOWLISTED_READS)
    ]
    assert not unaccounted, (
        "read routes with no entry in ALLOWLISTED_READS (add one naming the "
        "surface it serves — the API is a published contract, so a route "
        "nobody can describe should not ship):\n"
        + "\n".join(f"  {method} {path}" for method, path in unaccounted)
    )


def test_the_read_allowlist_names_no_phantom_route() -> None:
    """A removed read route must lose its allowlist entry with it."""
    served = {route for route in _schema_routes() if route[0] not in WRITE_METHODS}
    phantom = sorted(set(ALLOWLISTED_READS) - served)
    assert not phantom, (
        "ALLOWLISTED_READS names read routes absent from openapi.json (drop "
        "the entry; a stale allowlist grants cover to a future route that "
        "reuses the method and path):\n"
        + "\n".join(f"  {method} {path}" for method, path in phantom)
    )


def _load_export_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_export_schema_enters_app_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exported schema includes routes registered during startup."""

    async def _late() -> None:
        return None

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.add_api_route("/api/late", _late, methods=["POST"])
        yield

    app = FastAPI(lifespan=_lifespan)
    export = _load_export_script()

    def _create_app(*_args: object, **_kwargs: object) -> FastAPI:
        return app

    monkeypatch.setattr(export, "create_app", _create_app)

    assert "/api/late" in export.schema()["paths"]


@pytest.mark.policy
def test_the_committed_schema_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """``openapi.json`` matches what the app serves, checked from pytest.

    The gate itself lives in ci.yml's ``lint`` job and ``scripts/verify.sh``, so
    a change that only ever ran the tests covering the files it touched could
    leave the schema stale and see nothing red — which is how a route's docstring
    drifted from the committed schema on #2085. Same check, reachable from the
    place a change is most likely to look.

    Reachable is not the same as selected: a targeted round picks test modules
    named after the source modules it changed, and no edit to
    ``src/kew/serve/`` is ever named ``test_openapi_route_coverage``. #2300
    drifted the same docstring-to-schema way as #2085 for exactly that reason,
    so this one carries ``policy`` — the mark for a gate no targeted selection
    would reach, which every governed remediation round runs (#2174).
    """
    export = _load_export_script()
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    assert export.main() == 0, (
        "openapi.json is stale — regenerate it with "
        "`uv run python scripts/export_openapi.py`. Every route docstring, "
        "response model and parameter is part of the committed contract, so "
        "editing one is a schema change."
    )


def test_drift_fails_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale — or missing — openapi.json exits non-zero.

    The gate is only worth wiring into CI if it fails on the thing it exists to
    catch, and a check that cannot fail is the silent-clean-default archetype
    wearing a gate's clothes. Redirect the script's output path rather than
    touching the committed file, so the proof leaves no drift behind.
    """
    export = _load_export_script()
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])

    absent = tmp_path / "openapi.json"
    monkeypatch.setattr(export, "OUT", absent)
    assert export.main() == 1, "a missing schema passed --check"

    absent.write_text('{"openapi": "3.1.0", "paths": {}}\n')
    assert export.main() == 1, "a schema serving no routes at all passed --check"

    monkeypatch.setattr(sys, "argv", ["export_openapi.py"])
    assert export.main() == 0
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    assert export.main() == 0, "a freshly exported schema failed --check"
