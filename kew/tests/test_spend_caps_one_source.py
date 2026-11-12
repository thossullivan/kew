"""The console's spend caps come from one config source (#2847 Q2).

The Monitor read spend twice every beat: its own ``/api/report/spend`` poll and
the pulse's ``/api/dashboard`` ``glance.caps``. The report re-read
``load_config(store=...)``, so a Budget-panel override was in its cap; the
dashboard took the config ``kew serve`` started with, which never sees an
override set since. The Monitor now draws its gauges off the dashboard alone,
so the dashboard has to print the cap the governor enforces — the same one the
report prints.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kew.config import load_config
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app


def test_an_override_set_after_serve_started_is_the_dashboard_cap(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bound config discovery to this directory and keep the developer's env out
    # of the layer under test.
    (tmp_path / ".git").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = ""\n\n[budget]\ndaily_usd = 50.0\nweekly_usd = 200.0\n'
    )
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith(("KEW_", "DISPATCH_"))]:
        monkeypatch.delenv(name)

    served = load_config()  # what `kew serve` read at startup: no override yet
    db = str(tmp_path / "runs.db")
    with SqliteRunStore(db) as store:
        store.set_config_override(
            field="budget.daily_usd",
            value_json="75.0",
            file_value_json="50.0",
            set_at="2026-10-13T09:00:00+00:00",
            set_by="operator",
            reason="raised for the launch",
        )

    app = create_app(
        store_factory=lambda: SqliteRunStore(db), repo_dir=tmp_path, config=served
    )
    client = inbox_client(app, loopback_peer=True)
    dashboard = client.get("/api/dashboard")
    report = client.get("/api/report/spend")

    assert dashboard.status_code == 200
    assert report.status_code == 200
    caps = dashboard.json()["glance"]["caps"]
    assert "caps" not in dashboard.json()["unavailable"]
    assert caps["daily"]["cap"] == 75.0
    assert caps["weekly"]["cap"] == 200.0
    assert {w: caps[w]["cap"] for w in ("daily", "weekly")} == {
        w: report.json()[w]["cap"] for w in ("daily", "weekly")
    }


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kew.toml at 50/200 the server discovers, with no developer env leaking in."""
    (tmp_path / ".git").mkdir()
    toml = tmp_path / "kew.toml"
    _write_caps(toml, daily=50.0)
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith(("KEW_", "DISPATCH_"))]:
        monkeypatch.delenv(name)
    return toml


def _write_caps(toml: Path, *, daily: float) -> None:
    toml.write_text(
        f'[project]\nrepo = ""\n\n[budget]\ndaily_usd = {daily}\nweekly_usd = 200.0\n'
    )
    # A same-size rewrite inside one mtime tick must still read as a new file.
    stamp = toml.stat().st_mtime_ns + 1_000_000_000
    os.utime(toml, ns=(stamp, stamp))


def _served_daily_caps(client) -> tuple[float, float]:
    dashboard = client.get("/api/dashboard").json()
    report = client.get("/api/report/spend").json()
    assert "caps" not in dashboard["unavailable"]
    return dashboard["glance"]["caps"]["daily"]["cap"], report["daily"]["cap"]


def test_a_kew_toml_edit_after_serve_started_is_the_cap(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The governor reads kew.toml on every check, so the gauges do too.

    Holding the config ``kew serve`` started with froze the caps at startup:
    the governor enforced the edited 25 while Monitor and the report said 50.
    """
    toml = _project(tmp_path, monkeypatch)
    db = str(tmp_path / "runs.db")
    app = create_app(
        store_factory=lambda: SqliteRunStore(db),
        repo_dir=tmp_path,
        config=load_config(),
    )
    client = inbox_client(app, loopback_peer=True)
    assert _served_daily_caps(client) == (50.0, 50.0)

    _write_caps(toml, daily=25.0)

    assert _served_daily_caps(client) == (25.0, 25.0)


def test_reverting_an_override_brings_back_the_file_cap(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the override gone, the cap is the file's current value, not startup's."""
    toml = _project(tmp_path, monkeypatch)
    db = str(tmp_path / "runs.db")
    app = create_app(
        store_factory=lambda: SqliteRunStore(db),
        repo_dir=tmp_path,
        config=load_config(),
    )
    client = inbox_client(app, loopback_peer=True)

    with SqliteRunStore(db) as store:
        store.set_config_override(
            field="budget.daily_usd",
            value_json="75.0",
            file_value_json="50.0",
            set_at="2026-10-13T09:00:00+00:00",
            set_by="operator",
            reason="raised for the launch",
        )
    _write_caps(toml, daily=25.0)
    assert _served_daily_caps(client) == (75.0, 75.0)

    with SqliteRunStore(db) as store:
        assert store.revert_config_override("budget.daily_usd")

    assert _served_daily_caps(client) == (25.0, 25.0)
