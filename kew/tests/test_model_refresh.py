"""``kew models refresh``: fetched prices and lifecycle are untrusted (#2711).

Every test here runs over stored responses in ``fixtures/model_catalog/``. The
autouse guard refuses any socket connection and fails the test if the real
fetch is ever reached, so nothing in this module touches the network.
"""

from __future__ import annotations

import copy
import io
import json
import socket
import urllib.error
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew import model_refresh
from kew.cli import app
from kew.model_refresh import (
    OPENROUTER_MODELS_URL,
    RefreshError,
    refresh_catalog,
    render_catalog,
    render_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "model_catalog"
FEED_URL = "https://feeds.example.test/model-eol.json"
SONNET = "claude-sonnet-5"
QWEN = "openrouter/qwen/qwen3-coder-next"
FLASH = "openrouter/deepseek/deepseek-v4-flash-0731"

runner = CliRunner()


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _responses() -> dict[str, Any]:
    return {
        OPENROUTER_MODELS_URL: _load("openrouter_models.json"),
        FEED_URL: _load("model_eol_feed.json"),
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Refuse every socket, and record any call that reaches the real fetch."""
    reached: list[str] = []

    def refuse_connect(*args: Any, **kwargs: Any) -> None:
        reached.append(f"socket {args[1:]!r}")
        raise AssertionError("a model refresh test opened a network socket")

    def refuse_urlopen(url: Any, *args: Any, **kwargs: Any) -> None:
        reached.append(str(url))
        raise AssertionError("a model refresh test reached the real fetch")

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket, "create_connection", refuse_connect)
    monkeypatch.setattr(model_refresh.urllib.request, "urlopen", refuse_urlopen)
    yield reached
    assert reached == [], f"the real fetch was reached: {reached}"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``fetch_json`` to answer from the stored responses."""
    responses = _responses()

    def fake_fetch(url: str) -> Any:
        return copy.deepcopy(responses[url])

    monkeypatch.setattr(model_refresh, "fetch_json", fake_fetch)
    return responses


@pytest.fixture
def catalog_file(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(render_catalog(_load("catalog.json")), encoding="utf-8")
    return path


def _refresh(catalog_file: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        ["models", "refresh", "--catalog", str(catalog_file), "--eol-feed", FEED_URL]
        + list(extra),
    )


def _row(payload: dict[str, Any], model_id: str) -> dict[str, Any]:
    row: dict[str, Any] = next(r for r in payload["models"] if r["id"] == model_id)
    return row


# --- AC2: the guard itself --------------------------------------------------


def test_the_real_fetch_is_refused_by_the_guard(no_network: list[str]) -> None:
    with pytest.raises(AssertionError, match="reached the real fetch"):
        model_refresh.fetch_json(OPENROUTER_MODELS_URL)
    with pytest.raises(AssertionError, match="opened a network socket"):
        socket.create_connection(("openrouter.ai", 443))
    assert len(no_network) == 2
    no_network.clear()


def test_fetch_json_refuses_a_non_https_url() -> None:
    with pytest.raises(RefreshError, match="non-https"):
        model_refresh.fetch_json("http://openrouter.ai/api/v1/models")


class _Response(io.BytesIO):
    def __init__(self, body: bytes, final_url: str) -> None:
        super().__init__(body)
        self.final_url = final_url

    def geturl(self) -> str:
        return self.final_url


@pytest.mark.parametrize(
    ("body", "final_url", "problem"),
    [
        (b'{"data": []}', "http://openrouter.ai/api/v1/models", "redirected off"),
        (b" " * 40, OPENROUTER_MODELS_URL, "more than 32 bytes"),
        (b'{"data": []}', OPENROUTER_MODELS_URL, None),
    ],
    ids=["redirect-off-https", "oversized-body", "plain-https"],
)
def test_fetch_json_holds_the_response_to_https_and_a_size_cap(
    monkeypatch: pytest.MonkeyPatch, body: bytes, final_url: str, problem: str | None
) -> None:
    monkeypatch.setattr(model_refresh, "_MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(
        model_refresh.urllib.request,
        "urlopen",
        lambda url, timeout: _Response(body, final_url),
    )

    if problem is None:
        assert model_refresh.fetch_json(OPENROUTER_MODELS_URL) == {"data": []}
        return
    with pytest.raises(RefreshError, match=problem):
        model_refresh.fetch_json(OPENROUTER_MODELS_URL)


def test_a_failed_fetch_exits_two_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, catalog_file: Path
) -> None:
    def unreachable(url: str) -> Any:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(model_refresh, "fetch_json", unreachable)
    before = catalog_file.read_bytes()

    result = _refresh(catalog_file, "--write")

    assert result.exit_code == 2, result.output
    assert "no route to host" in result.output
    assert catalog_file.read_bytes() == before


# --- AC1: fixture responses produce the expected catalog and report ---------


def test_refresh_writes_the_expected_catalog_and_report(
    served: dict[str, Any], catalog_file: Path, tmp_path: Path
) -> None:
    report = tmp_path / "report.md"

    result = _refresh(catalog_file, "--write", "--report", str(report))

    assert result.exit_code == 0, result.output
    expected = (FIXTURES / "expected_catalog.json").read_text(encoding="utf-8")
    assert catalog_file.read_text(encoding="utf-8") == expected
    expected_report = (FIXTURES / "expected_report.md").read_text(encoding="utf-8")
    assert report.read_text(encoding="utf-8") == expected_report


def test_without_write_the_catalog_is_untouched(
    served: dict[str, Any], catalog_file: Path
) -> None:
    before = catalog_file.read_bytes()

    result = _refresh(catalog_file)

    assert result.exit_code == 0, result.output
    assert "| `openrouter/qwen/qwen3-coder-next` | `price.input_per_mtok`" in (
        result.output
    )
    assert catalog_file.read_bytes() == before


def test_a_second_refresh_over_the_same_responses_changes_nothing(
    served: dict[str, Any], catalog_file: Path
) -> None:
    assert _refresh(catalog_file, "--write").exit_code == 0
    after_first = catalog_file.read_bytes()

    result = _refresh(catalog_file, "--write")

    assert result.exit_code == 0, result.output
    assert "already matches" in result.output
    assert catalog_file.read_bytes() == after_first


# --- AC3: untrusted values refuse, name the row, leave the file unchanged ---


def _negative_price(responses: dict[str, Any]) -> str:
    item = _item(responses, "qwen/qwen3-coder-next")
    item["pricing"]["prompt"] = "-0.0000003"
    return QWEN


def _triple_price(responses: dict[str, Any]) -> str:
    item = _item(responses, "qwen/qwen3-coder-next")
    item["pricing"]["completion"] = "0.0000045"  # 1.5 -> 4.5 $/MTok
    return QWEN


def _malformed_date(responses: dict[str, Any]) -> str:
    responses[FEED_URL]["models"][0]["shutdown"] = "2027-13-45"
    return SONNET


def _http_evidence(responses: dict[str, Any]) -> str:
    evidence = responses[FEED_URL]["models"][0]["evidence"][0]
    evidence["url"] = "http://platform.claude.com/docs/en/about-claude/models"
    return SONNET


def _item(responses: dict[str, Any], slug: str) -> dict[str, Any]:
    data = responses[OPENROUTER_MODELS_URL]["data"]
    item: dict[str, Any] = next(i for i in data if i["id"] == slug)
    return item


@pytest.mark.parametrize(
    ("corrupt", "problem"),
    [
        (_negative_price, "outside"),
        (_triple_price, "3.00x"),
        (_malformed_date, "ISO YYYY-MM-DD"),
        (_http_evidence, "must be https"),
    ],
    ids=["negative-price", "3x-jump", "malformed-date", "http-evidence-url"],
)
def test_an_untrusted_value_refuses_names_the_row_and_writes_nothing(
    served: dict[str, Any],
    catalog_file: Path,
    tmp_path: Path,
    corrupt: Callable[[dict[str, Any]], str],
    problem: str,
) -> None:
    row = corrupt(served)
    before = catalog_file.read_bytes()
    report = tmp_path / "report.md"

    result = _refresh(catalog_file, "--write", "--report", str(report))

    assert result.exit_code == 2, result.output
    assert row in result.output
    assert problem in result.output
    assert catalog_file.read_bytes() == before
    assert not report.exists()


def test_a_large_change_applies_with_accept_large_change(
    served: dict[str, Any], catalog_file: Path
) -> None:
    _triple_price(served)

    result = _refresh(catalog_file, "--write", "--accept-large-change")

    assert result.exit_code == 0, result.output
    written = json.loads(catalog_file.read_text(encoding="utf-8"))
    assert _row(written, QWEN)["price"]["output_per_mtok"] == 4.5


@pytest.mark.parametrize("price", ["0", "1000", "NaN", "inf", "", None, True])
def test_a_price_outside_the_bounds_is_never_a_price(
    served: dict[str, Any], price: Any
) -> None:
    _item(served, "qwen/qwen3-coder-next")["pricing"]["prompt"] = price

    with pytest.raises(RefreshError, match=QWEN):
        refresh_catalog(_load("catalog.json"), accept_large_change=True)


def test_an_unknown_feed_spec_refuses(served: dict[str, Any]) -> None:
    served[FEED_URL]["spec"] = "model-eol/9"

    with pytest.raises(RefreshError, match="not a model-eol/0.1 feed"):
        refresh_catalog(_load("catalog.json"), eol_feeds=[FEED_URL])


# --- AC4: a source sets only price and lifecycle ----------------------------


def test_a_source_cannot_change_identity_fields_or_add_a_row(
    served: dict[str, Any],
) -> None:
    catalog = _load("catalog.json")
    # Hostile responses carry identity fields for known and unknown ids.
    for item in served[OPENROUTER_MODELS_URL]["data"]:
        item.update(
            display_name="Hijacked",
            runner="codex",
            routable=True,
            default_lanes=["default"],
            aliases=["claude-opus-5"],
        )
    feed_rows = served[FEED_URL]["models"]
    feed_rows[0]["provider"] = "evil"

    result = refresh_catalog(catalog, eol_feeds=[FEED_URL])

    merged = result.payload["models"]
    assert [row["id"] for row in merged] == [row["id"] for row in catalog["models"]]
    for before, after in zip(catalog["models"], merged, strict=True):
        for key in (
            "display_name",
            "provider",
            "runner",
            "price_source",
            "routable",
            "default_lanes",
            "aliases",
        ):
            assert after[key] == before[key], (before["id"], key)
    assert ("claude-sonnet-6", FEED_URL) in result.new_ids
    assert ("openrouter/qwen/qwen3.5-max", OPENROUTER_MODELS_URL) in result.new_ids
    assert "claude-sonnet-6" in render_report(result)


def test_new_ids_from_vendors_the_catalog_does_not_route_are_not_listed(
    served: dict[str, Any],
) -> None:
    result = refresh_catalog(_load("catalog.json"))

    listed = {model for model, _ in result.new_ids}
    assert "openrouter/openai/gpt-9" not in listed
    assert "openrouter/openrouter/auto" not in listed


# --- AC5: first-party prices are kept; the disagreement is reported ---------


def test_a_first_party_row_keeps_its_price_and_reports_the_disagreement(
    served: dict[str, Any],
) -> None:
    catalog = _load("catalog.json")

    result = refresh_catalog(catalog)

    assert _row(result.payload, SONNET)["price"] == _row(catalog, SONNET)["price"]
    assert {(c.model, c.field, c.old, c.new) for c in result.disagreements} == {
        (SONNET, "price.input_per_mtok", "2", "3"),
        (SONNET, "price.output_per_mtok", "10", "15"),
    }
    assert all(c.model != SONNET for c in result.price_changes)
    report = render_report(result)
    disagreements = report.split("First-party prices")[1].split("###")[0]
    assert "`claude-sonnet-5` | `price.input_per_mtok` | `2` | `3`" in disagreements


def test_a_first_party_price_jump_is_reported_not_refused(
    served: dict[str, Any],
) -> None:
    _item(served, "anthropic/claude-sonnet-5")["pricing"]["prompt"] = "0.00002"

    result = refresh_catalog(_load("catalog.json"))

    assert (SONNET, "price.input_per_mtok", "2", "20") in {
        (c.model, c.field, c.old, c.new) for c in result.disagreements
    }


# --- Report hygiene ---------------------------------------------------------


def test_fetched_text_cannot_break_out_of_its_report_cell(
    served: dict[str, Any],
) -> None:
    served[FEED_URL]["models"][1]["id"] = "evil | <b>x</b>`\n## Approved"

    report = render_report(refresh_catalog(_load("catalog.json"), eol_feeds=[FEED_URL]))

    line = next(row for row in report.splitlines() if "evil" in row)
    assert line == f"| `evil \\| <b>x</b>' ## Approved` | `{FEED_URL}` |"
    assert "\n## Approved" not in report


def test_the_packaged_catalog_renders_byte_for_byte() -> None:
    from kew.model_catalog import PACKAGED_CATALOG_PATH

    text = PACKAGED_CATALOG_PATH.read_text(encoding="utf-8")
    assert render_catalog(json.loads(text)) == text
