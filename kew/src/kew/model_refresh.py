"""``kew models refresh``: merge fetched prices and lifecycle into the catalog.

Two sources feed the packaged model catalog (``models.json``):

- prices from the OpenRouter models API (``$/token`` strings, stored as
  ``$/MTok``);
- lifecycle from model-eol feeds (spec ``model-eol/0.1``: a JSON object
  ``{"spec": "model-eol/0.1", "models": [...]}`` whose rows have the curated
  registry row shape — ``id``, ``provider``, ``status``, ``shutdown``,
  ``replacement``, ``evidence``).

Fetched data is untrusted: it sets cost figures and routing facts. A source sets
only a row's price and lifecycle. It never sets an id, display name, runner,
routable flag, lane default or alias, and it never adds a row; an id a source
names that the catalog lacks is listed in the report for a person to add. Every
fetched value a refresh would use is checked before anything is written, and
any failure refuses the whole refresh with the row named.

A row whose ``price_source`` is ``first-party`` keeps its price; an OpenRouter
value that disagrees goes into the report only.

Nothing here runs at dispatch time. Governed cost reads the committed catalog,
so a dispatch is priced the same way on every machine.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kew.model_catalog import CatalogError, parse_catalog
from kew.model_eol_snapshot import RegistryError, RegistryRow, _parse_row

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_PREFIX = "openrouter/"
EOL_FEED_SPEC = "model-eol/0.1"
# No list price kew routes to comes near this; a fetched value at or above it
# is a unit slip or a hostile response, never a price.
PRICE_CEILING_PER_MTOK = 1000.0
# A fetched price more than double, or less than half, the committed one
# refuses unless the operator passes --accept-large-change.
LARGE_CHANGE_RATIO = 2.0
_FETCH_TIMEOUT_SECONDS = 30
# The OpenRouter models list is a few MB; anything far past that is not it.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_PRICE_FIELDS = (("input_per_mtok", "prompt"), ("output_per_mtok", "completion"))
_VERSION_DASH = re.compile(r"(?<=\d)-(?=\d)")

Fetch = Callable[[str], Any]


class RefreshError(ValueError):
    """Fetched data failed a check; nothing was written."""


def fetch_json(url: str) -> Any:
    """GET *url* over HTTPS and decode its JSON body."""
    if not url.startswith("https://"):
        raise RefreshError(f"refusing non-https source URL: {url!r}")
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:  # nosec B310
        if not str(resp.geturl()).startswith("https://"):
            raise RefreshError(f"{url!r} redirected off https to {resp.geturl()!r}")
        body = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise RefreshError(f"{url!r} answered more than {_MAX_RESPONSE_BYTES} bytes")
    return json.loads(body)


@dataclass(frozen=True)
class Change:
    """One field a refresh changes (or would change) on one row."""

    model: str
    field: str
    old: str
    new: str
    source: str


@dataclass
class RefreshResult:
    """The merged catalog payload and everything the report lists."""

    payload: dict[str, Any]
    sources: list[str]
    price_changes: list[Change] = field(default_factory=list)
    lifecycle_changes: list[Change] = field(default_factory=list)
    disagreements: list[Change] = field(default_factory=list)
    new_ids: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.price_changes or self.lifecycle_changes)


# ---------------------------------------------------------------------------
# OpenRouter prices
# ---------------------------------------------------------------------------


def _openrouter_slugs(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The OpenRouter model ids that may price one catalog row."""
    model_id = str(row["id"])
    if model_id.startswith(OPENROUTER_PREFIX):
        return (model_id.removeprefix(OPENROUTER_PREFIX),)
    base = f"{row['provider']}/{model_id}"
    dotted = f"{row['provider']}/{_VERSION_DASH.sub('.', model_id)}"
    return (base,) if dotted == base else (base, dotted)


def _openrouter_index(payload: Any) -> dict[str, Mapping[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise RefreshError(f"{OPENROUTER_MODELS_URL}: response has no 'data' array")
    return {
        item["id"]: item
        for item in data
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _per_mtok(value: Any, model: str, name: str) -> float:
    """A fetched ``$/token`` price as ``$/MTok``, inside the bounds."""
    try:
        per_token = float(value) if isinstance(value, str | int | float) else math.nan
    except ValueError:
        per_token = math.nan
    if isinstance(value, bool) or not math.isfinite(per_token):
        raise RefreshError(f"row {model!r} field {name!r}: not a price: {value!r}")
    price = round(per_token * 1_000_000, 6)
    if not 0 < price < PRICE_CEILING_PER_MTOK:
        raise RefreshError(
            f"row {model!r} field {name!r}: ${price}/MTok is outside "
            f"(0, {PRICE_CEILING_PER_MTOK})"
        )
    return price


def _quote(item: Mapping[str, Any], model: str) -> dict[str, float]:
    pricing = item.get("pricing")
    if not isinstance(pricing, Mapping):
        raise RefreshError(f"row {model!r} field 'price': OpenRouter has no pricing")
    return {
        name: _per_mtok(pricing.get(key), model, f"price.{name}")
        for name, key in _PRICE_FIELDS
    }


def _check_ratio(model: str, name: str, old: float, new: float) -> str | None:
    if old <= 0:
        return None
    ratio = new / old
    if 1 / LARGE_CHANGE_RATIO <= ratio <= LARGE_CHANGE_RATIO:
        return None
    return (
        f"row {model!r} field 'price.{name}': ${old} -> ${new}/MTok is a "
        f"{ratio:.2f}x change; rerun with --accept-large-change after checking it"
    )


def _price_changes(
    row: Mapping[str, Any], quote: dict[str, float]
) -> list[tuple[str, float | None, float]]:
    """(field, old, new) for each price field the quote moves."""
    old_price = row["price"]
    moved = []
    for name, new in quote.items():
        old = None if old_price is None else float(old_price[name])
        if old != new:
            moved.append((name, old, new))
    return moved


def _apply_quote(
    row: dict[str, Any],
    quote: dict[str, float],
    result: RefreshResult,
    accept_large_change: bool,
) -> list[str]:
    model = str(row["id"])
    first_party = row["price_source"] == "first-party"
    changes: list[Change] = []
    errors: list[str] = []
    for name, old, new in _price_changes(row, quote):
        changes.append(
            Change(model, f"price.{name}", _num(old), _num(new), OPENROUTER_MODELS_URL)
        )
        problem = None if old is None else _check_ratio(model, name, old, new)
        if problem and not first_party and not accept_large_change:
            errors.append(problem)
    if first_party:
        result.disagreements += changes
    elif not errors:
        result.price_changes += changes
        old_price = row["price"]
        row["price"] = (
            {**quote, "tiers": []} if old_price is None else {**old_price, **quote}
        )
    return errors


def _openrouter_new_ids(
    rows: list[dict[str, Any]], unclaimed: set[str]
) -> list[tuple[str, str]]:
    """Unclaimed OpenRouter ids under a vendor the catalog already routes to."""
    vendors = {
        str(row["id"]).removeprefix(OPENROUTER_PREFIX).split("/")[0]
        for row in rows
        if str(row["id"]).startswith(OPENROUTER_PREFIX)
    }
    return [
        (f"{OPENROUTER_PREFIX}{slug}", OPENROUTER_MODELS_URL)
        for slug in sorted(unclaimed)
        if slug.split("/")[0] in vendors
    ]


def _merge_prices(
    rows: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
    result: RefreshResult,
    accept_large_change: bool,
) -> list[str]:
    errors: list[str] = []
    unclaimed = set(index)
    for row in rows:
        slug = next((s for s in _openrouter_slugs(row) if s in index), None)
        if slug is None:
            continue
        unclaimed.discard(slug)
        try:
            quote = _quote(index[slug], str(row["id"]))
        except RefreshError as exc:
            errors.append(str(exc))
            continue
        errors += _apply_quote(row, quote, result, accept_large_change)
    result.new_ids += _openrouter_new_ids(rows, unclaimed)
    return errors


# ---------------------------------------------------------------------------
# model-eol lifecycle
# ---------------------------------------------------------------------------


def _feed_rows(payload: Any, url: str) -> tuple[list[RegistryRow], list[str]]:
    if not isinstance(payload, Mapping) or payload.get("spec") != EOL_FEED_SPEC:
        return [], [f"{url}: not a {EOL_FEED_SPEC} feed"]
    models = payload.get("models")
    if not isinstance(models, list):
        return [], [f"{url}: feed 'models' must be a JSON array"]
    rows: list[RegistryRow] = []
    errors: list[str] = []
    for index, item in enumerate(models):
        try:
            rows.append(_parse_row(item))
        except RegistryError as exc:
            label = item.get("id") if isinstance(item, Mapping) else None
            errors.append(f"{url}: row {label or f'#{index}'!r}: {exc}")
    return rows, errors


def _lifecycle_of(feed_row: RegistryRow) -> dict[str, Any]:
    return {
        "status": feed_row.status,
        "shutdown": feed_row.shutdown,
        "replacement": feed_row.replacement,
        "evidence": [
            {"kind": e.kind, "url": e.url, "checkedAt": e.checked_at}
            for e in feed_row.evidence
        ],
    }


def _lifecycle_text(key: str, value: Any) -> str:
    if key == "evidence":
        return ", ".join(str(item["url"]) for item in value) or "none"
    return "null" if value is None else str(value)


def _merge_lifecycle(
    rows: list[dict[str, Any]],
    feed_rows: list[RegistryRow],
    url: str,
    result: RefreshResult,
) -> None:
    by_name = {name: row for row in rows for name in (row["id"], *row["aliases"])}
    for feed_row in feed_rows:
        row = by_name.get(feed_row.id)
        if row is None:
            result.new_ids.append((feed_row.id, url))
            continue
        new = _lifecycle_of(feed_row)
        for key, value in new.items():
            if row["lifecycle"][key] != value:
                result.lifecycle_changes.append(
                    Change(
                        str(row["id"]),
                        f"lifecycle.{key}",
                        _lifecycle_text(key, row["lifecycle"][key]),
                        _lifecycle_text(key, value),
                        url,
                    )
                )
        row["lifecycle"] = new


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

# What a source may never touch: everything but price and lifecycle.
_PROTECTED_KEYS = (
    "id",
    "display_name",
    "provider",
    "runner",
    "price_source",
    "routable",
    "default_lanes",
    "aliases",
)


def _require_identity_kept(before: list[Any], after: list[Any]) -> None:
    """Belt and braces: the merge changed nothing but price and lifecycle."""
    protected = [{k: row[k] for k in _PROTECTED_KEYS} for row in before]
    if protected != [{k: row[k] for k in _PROTECTED_KEYS} for row in after]:
        raise RefreshError("refresh changed a field a source may not set")


def refresh_catalog(
    catalog: Mapping[str, Any],
    *,
    eol_feeds: Sequence[str] = (),
    accept_large_change: bool = False,
    fetch: Fetch | None = None,
) -> RefreshResult:
    """Fetch every source and merge it into a copy of *catalog*.

    Raises :class:`RefreshError` naming each failing row when any fetched value
    fails a check; the caller writes nothing in that case.
    """
    get = fetch_json if fetch is None else fetch
    try:
        parse_catalog(catalog)
    except CatalogError as exc:
        raise RefreshError(f"the committed catalog does not validate: {exc}") from exc
    payload: dict[str, Any] = json.loads(json.dumps(catalog))
    rows: list[dict[str, Any]] = payload["models"]
    result = RefreshResult(payload=payload, sources=[OPENROUTER_MODELS_URL, *eol_feeds])
    errors = _merge_prices(
        rows, _openrouter_index(get(OPENROUTER_MODELS_URL)), result, accept_large_change
    )
    for url in eol_feeds:
        feed_rows, feed_errors = _feed_rows(get(url), url)
        errors += feed_errors
        _merge_lifecycle(rows, feed_rows, url, result)
    if errors:
        raise RefreshError("refresh refused; nothing written:\n" + "\n".join(errors))
    _require_identity_kept(list(catalog["models"]), rows)
    try:
        parse_catalog(payload)
    except CatalogError as exc:
        raise RefreshError(f"refresh refused; nothing written: {exc}") from exc
    return result


def render_catalog(payload: Mapping[str, Any]) -> str:
    """The committed ``models.json`` text for *payload*."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_catalog(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace *path* atomically, so a failed write leaves the old bytes."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(render_catalog(payload))
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _num(value: float | None) -> str:
    return "none" if value is None else f"{value:g}"


def _cell(value: str) -> str:
    """Fetched text as an inert table cell: one line, no pipes, no markup."""
    text = " ".join(value.split()).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


def _change_table(title: str, changes: list[Change], old: str, new: str) -> list[str]:
    lines = [f"### {title}", ""]
    if not changes:
        return [*lines, "None.", ""]
    lines += [f"| Model | Field | {old} | {new} | Source |", "|---|---|---|---|---|"]
    lines += [
        "| "
        + " | ".join(_cell(v) for v in (c.model, c.field, c.old, c.new, c.source))
        + " |"
        for c in changes
    ]
    return [*lines, ""]


def render_report(result: RefreshResult) -> str:
    """The Markdown report; it becomes the refresh PR body."""
    lines = ["## Model catalog refresh", "", "Sources:", ""]
    lines += [f"- {_cell(url)}" for url in result.sources]
    lines.append("")
    lines += _change_table("Price changes", result.price_changes, "Old", "New")
    lines += _change_table("Lifecycle changes", result.lifecycle_changes, "Old", "New")
    lines += _change_table(
        "First-party prices OpenRouter disagrees with (not applied)",
        result.disagreements,
        "Catalog",
        "OpenRouter",
    )
    lines += ["### New ids (not added; a person adds the row)", ""]
    if result.new_ids:
        lines += ["| Id | Source |", "|---|---|"]
        lines += [f"| {_cell(i)} | {_cell(src)} |" for i, src in result.new_ids]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"
