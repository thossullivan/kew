"""The model-eol docs list and the www lifecycle view are generated (#2710).

Both files copy ``kew/src/kew/models.json``; ``scripts/model_catalog_docs.py``
renders them, and these tests hold the committed files to its output.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from kew.model_catalog import load_catalog

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_catalog_docs.py"
_spec = importlib.util.spec_from_file_location("model_catalog_docs", SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
sys.modules["model_catalog_docs"] = gen
_spec.loader.exec_module(gen)

_INPUTS = (gen.CATALOG, gen.DOCS_PAGE, gen.WWW_VIEW, gen.ROUTING)


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A temporary copy of every file the generator reads or writes."""
    for path in _INPUTS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(gen.ROOT / path, target)
    return tmp_path


def _add_catalog_row(root: Path, model_id: str, **fields: object) -> None:
    path = root / gen.CATALOG
    doc = json.loads(path.read_text(encoding="utf-8"))
    row = json.loads(json.dumps(doc["models"][0]))
    row.update(id=model_id, display_name=model_id, aliases=[], default_lanes=[])
    row.update(fields)
    doc["models"] = sorted([*doc["models"], row], key=lambda r: r["id"])
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


@pytest.mark.policy
def test_committed_docs_list_and_www_view_equal_the_generated_output() -> None:
    for path, text in gen.render().items():
        committed = (gen.ROOT / path).read_text(encoding="utf-8")
        assert committed == text, (
            f"{path} differs from the model catalog; run `{gen.REGENERATE_COMMAND}`"
        )
    gen.check()


def test_a_new_catalog_row_fails_the_check_with_the_regenerate_command(
    repo_copy: Path,
) -> None:
    gen.check(repo_copy)  # the unmodified copy is clean

    _add_catalog_row(repo_copy, "vendor/brand-new-model")

    with pytest.raises(gen.DriftError) as excinfo:
        gen.check(repo_copy)
    message = str(excinfo.value)
    assert gen.REGENERATE_COMMAND in message
    assert str(gen.DOCS_PAGE) in message
    assert gen.stale_files(repo_copy) == [gen.DOCS_PAGE]

    assert gen.write(repo_copy) == [gen.DOCS_PAGE]
    page = (repo_copy / gen.DOCS_PAGE).read_text(encoding="utf-8")
    assert "| `vendor/brand-new-model` |" in page
    gen.check(repo_copy)


def test_a_newly_priced_catalog_row_also_drifts_the_www_view(repo_copy: Path) -> None:
    model_id = "openrouter/vendor/brand-new-model"
    _add_catalog_row(repo_copy, model_id, price_source="openrouter")

    assert gen.stale_files(repo_copy) == [gen.DOCS_PAGE, gen.WWW_VIEW]
    with pytest.raises(gen.DriftError, match="model_catalog_docs.py"):
        gen.check(repo_copy)

    gen.write(repo_copy)
    view = json.loads((repo_copy / gen.WWW_VIEW).read_text(encoding="utf-8"))
    row = next(item for item in view["models"] if item["id"] == model_id)
    assert (row["priced"], row["routed"]) == (True, False)


def test_text_outside_the_markers_is_left_alone() -> None:
    catalog = load_catalog()
    before = "# Title\n\nHand-written intro.\n\n"
    after = "\n\nHand-written outro.\n"
    page = f"{before}{gen.BEGIN_MARKER}\nstale list\n{gen.END_MARKER}{after}"

    rendered = gen.render_docs_page(page, catalog)

    assert rendered.startswith(before + gen.BEGIN_MARKER)
    assert rendered.endswith(gen.END_MARKER + after)
    assert "stale list" not in rendered


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("no markers at all\n", id="missing"),
        pytest.param(
            f"{gen.BEGIN_MARKER}\n{gen.END_MARKER}\n{gen.BEGIN_MARKER}\n{gen.END_MARKER}",
            id="duplicated",
        ),
        pytest.param(f"{gen.END_MARKER}\n{gen.BEGIN_MARKER}\n", id="reversed"),
    ],
)
def test_a_page_without_one_ordered_marker_pair_is_refused(page: str) -> None:
    with pytest.raises(gen.DriftError):
        gen.render_docs_page(page, load_catalog())


def test_the_generated_list_carries_its_own_count() -> None:
    catalog = load_catalog()
    block = gen.render_model_list(catalog)

    assert f"The catalog lists {len(catalog.rows)} models," in block
    table_rows = [line for line in block.splitlines() if line.startswith("| `")]
    assert len(table_rows) == len(catalog.rows)

    single = type(catalog)(catalog.schema, catalog.reviewed_at, catalog.rows[:1])
    assert "The catalog lists 1 model," in gen.render_model_list(single)
