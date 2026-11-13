import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import kew.cli.serve as cli_serve
import kew.serve
from kew.cli import app

runner = CliRunner()

# CliRunner swaps sys.stdout for this wrapper while a command runs, so the
# wrapper's isatty is the terminal check `kew serve` sees.
_RUNNER_STDOUT_ISATTY = "click.testing._NamedTextIOWrapper.isatty"


def test_serve_static_dir_resolves_to_the_built_bundle_location():
    """The serve command must look for the SPA bundle where ``build_web.sh``
    writes it — ``kew/serve/web`` — NOT ``kew/cli/serve/web``.

    Regression guard: the resolution was ``Path(__file__).parent / "serve" /
    "web"``, i.e. ``kew/cli/serve/web`` (off by one directory level). That path
    never exists, so ``kew serve`` silently fell back to the hint page no matter
    how many times the bundle was built. The fix is ``.parent.parent``. Anchored
    on ``kew.cli.serve`` (the serve command's own module after the #1001
    extraction — where the resolution now lives), so a ``.parent`` regression
    THERE fails this. Pure path-equality, no build required.
    """
    cli_resolved = Path(cli_serve.__file__).parent.parent / "serve" / "web"
    pkg_serve_web = Path(kew.serve.__file__).parent / "web"
    assert cli_resolved == pkg_serve_web
    # The immediate parent must be the `serve` package, never `cli`.
    assert cli_resolved.parent.name == "serve"


def test_serve_nonloopback_without_expose_aborts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # minimal config; default loopback. Force a non-loopback bind via --host.
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "kew.cli.preflight_tools"
    ):
        result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "expose" in result.output.lower()


def test_serve_wildcard_with_expose_aborts(tmp_path, monkeypatch):
    # Exposing a wildcard bind can't populate the Host/Origin allowlist -> abort.
    monkeypatch.chdir(tmp_path)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "kew.cli.preflight_tools"
    ):
        result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--expose"])
    assert result.exit_code == 2
    assert "wildcard" in result.output.lower()


def test_serve_banner_signs_in_at_root(tmp_path, monkeypatch):
    # The sign-in link kew prints must sign in at the root (token pickup now
    # runs in the root layout), not at /inbox — the redesign relocates the
    # judgment surface off /inbox.
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.serve.app.create_app", return_value=MagicMock()),
        patch("uvicorn.run"),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
        patch(_RUNNER_STDOUT_ISATTY, return_value=True),
    ):
        result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    assert "/#token=" in result.output
    assert "/inbox#token=" not in result.output


def test_serve_startup_writes_state_file(tmp_path, monkeypatch):
    """serve startup writes serve_state.json with the bind host and port."""
    from kew.config import DispatchConfig, LoggingConfig

    monkeypatch.chdir(tmp_path)
    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.serve.app.create_app", return_value=MagicMock()),
        patch("uvicorn.run"),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    state_file = tmp_path / "serve_state.json"
    assert state_file.exists(), "serve_state.json not written"
    state = json.loads(state_file.read_text())
    assert state["host"] == "127.0.0.1"
    assert state["port"] == 8765


def test_serve_startup_copy_friendly_url_line(tmp_path, monkeypatch):
    """serve startup prints a bare http:// line for easy copy-paste."""
    from kew.config import DispatchConfig, LoggingConfig

    monkeypatch.chdir(tmp_path)
    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.serve.app.create_app", return_value=MagicMock()),
        patch("uvicorn.run"),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
        patch(_RUNNER_STDOUT_ISATTY, return_value=True),
    ):
        result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    bare_url_lines = [line for line in lines if line.strip().startswith("http://")]
    assert any("/#token=" in line for line in bare_url_lines), (
        f"No bare URL line found in output: {result.output!r}"
    )


def test_serve_url_no_state_aborts(tmp_path, monkeypatch):
    """serve url exits 2 with a clear message when no serve state file exists."""
    from kew.config import DispatchConfig, LoggingConfig

    monkeypatch.chdir(tmp_path)
    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
    with patch("kew.cli.load_config", return_value=config):
        result = runner.invoke(app, ["serve", "url"])
    assert result.exit_code == 2
    assert "not running" in result.output.lower()


def test_serve_url_prints_access_url(tmp_path, monkeypatch):
    """serve url prints the full access URL with token fragment."""
    from kew.config import DispatchConfig, LoggingConfig

    monkeypatch.chdir(tmp_path)
    state_file = tmp_path / "serve_state.json"
    state_file.write_text(json.dumps({"host": "127.0.0.1", "port": 8765}))
    token_file = tmp_path / "serve_token"
    token_file.write_text("mytesttoken")
    token_file.chmod(0o600)

    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
    with patch("kew.cli.load_config", return_value=config):
        result = runner.invoke(app, ["serve", "url"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8765/#token=mytesttoken" in result.output


def test_serve_prints_the_tokened_url_only_to_a_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_SERVE_TOKEN", "tty-token")
    with patch(_RUNNER_STDOUT_ISATTY, return_value=True):
        result, _ = _run_serve(_serve_config(tmp_path), ["serve"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8765/#token=tty-token" in result.output
    assert "kew serve url" not in result.output


def test_serve_without_a_terminal_prints_the_plain_url_and_points_to_serve_url(
    tmp_path, monkeypatch
):
    """A service manager or CI job captures stdout into a log; the admin token
    must never land there. `kew serve url` reads it back from the token file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KEW_SERVE_TOKEN", raising=False)
    with patch(_RUNNER_STDOUT_ISATTY, return_value=False):
        result, uvicorn_run = _run_serve(_serve_config(tmp_path), ["serve"])
    assert result.exit_code == 0, result.output
    uvicorn_run.assert_called_once()
    file_token = (tmp_path / "serve_token").read_text().strip()
    assert "http://127.0.0.1:8765/" in result.output
    assert "kew serve url" in result.output
    assert "#token=" not in result.output
    assert file_token not in result.output


def test_serve_with_an_env_token_and_no_terminal_points_to_the_env_token(
    tmp_path, monkeypatch
):
    """With KEW_SERVE_TOKEN set kew writes no token file, so `kew serve url`
    from another shell has nothing to read: the pointer names the variable
    the link is built from, never its value."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_SERVE_TOKEN", "env-held-token")
    with patch(_RUNNER_STDOUT_ISATTY, return_value=False):
        result, uvicorn_run = _run_serve(_serve_config(tmp_path), ["serve"])
    assert result.exit_code == 0, result.output
    uvicorn_run.assert_called_once()
    assert not (tmp_path / "serve_token").exists()
    assert "http://127.0.0.1:8765/" in result.output
    assert "KEW_SERVE_TOKEN" in result.output
    assert "kew serve url" not in result.output
    assert "env-held-token" not in result.output


def _serve_config(tmp_path, **flight_recorder):
    from kew.config import DispatchConfig, FlightRecorderConfig, LoggingConfig

    return DispatchConfig(
        logging=LoggingConfig(dir=str(tmp_path)),
        flight_recorder=FlightRecorderConfig(**flight_recorder),
    )


def _run_serve(config, argv):
    """Invoke `kew serve` with uvicorn stubbed; returns (result, uvicorn mock)."""
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.serve.app.create_app", return_value=MagicMock()),
        patch("uvicorn.run") as uvicorn_run,
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        return runner.invoke(app, argv), uvicorn_run


def test_serve_expose_without_a_trusted_proxy_is_refused(tmp_path, monkeypatch):
    """--expose over plaintext HTTP puts the admin token on the wire; with no
    TLS terminator declared, kew refuses instead of degrading (W1.6)."""
    monkeypatch.chdir(tmp_path)
    result, uvicorn_run = _run_serve(
        _serve_config(tmp_path), ["serve", "--host", "192.168.1.10", "--expose"]
    )
    assert result.exit_code == 2
    assert "trusted_proxies" in result.output
    assert "TLS" in result.output
    uvicorn_run.assert_not_called()


def test_serve_expose_from_config_alone_is_refused_the_same_way(tmp_path, monkeypatch):
    # The flag is not the trigger — the exposed mode is. `expose = true` in
    # kew.toml on a loopback bind reaches the same refusal.
    monkeypatch.chdir(tmp_path)
    result, uvicorn_run = _run_serve(_serve_config(tmp_path, expose=True), ["serve"])
    assert result.exit_code == 2
    assert "trusted_proxies" in result.output
    uvicorn_run.assert_not_called()


def test_serve_expose_with_a_declared_proxy_runs_and_trusts_only_that_proxy(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result, uvicorn_run = _run_serve(
        _serve_config(tmp_path, expose=True, trusted_proxies=["10.0.0.5"]),
        ["serve"],
    )
    assert result.exit_code == 0, result.output
    kwargs = uvicorn_run.call_args.kwargs
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == ["10.0.0.5"]


def test_serve_loopback_default_trusts_no_forwarded_headers(tmp_path, monkeypatch):
    """uvicorn trusts X-Forwarded-* from 127.0.0.1 by default; kew does not,
    so a local process can never rewrite the peer the read guard sees."""
    monkeypatch.chdir(tmp_path)
    result, uvicorn_run = _run_serve(_serve_config(tmp_path), ["serve"])
    assert result.exit_code == 0, result.output
    kwargs = uvicorn_run.call_args.kwargs
    assert kwargs["proxy_headers"] is False
    assert kwargs["forwarded_allow_ips"] == []


def test_serve_expose_forwarded_kwargs_are_real_uvicorn_parameters():
    """kew hands its proxy allowlist to uvicorn by keyword. Pin the names: a
    rename would either explode at startup or, worse, land in a **kwargs sink
    where the allowlist is accepted and never applied."""
    import inspect

    import uvicorn

    params = inspect.signature(uvicorn.Config.__init__).parameters
    assert "proxy_headers" in params
    assert "forwarded_allow_ips" in params
