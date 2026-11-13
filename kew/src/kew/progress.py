"""Stream-json event parsing and terminal display.

Parses Claude Code's stream-json output format for real-time
operator visibility during dispatch.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import typer

from kew.credential_surface import redact_credential_values, runner_text

# Set while a finished transcript is being replayed for telemetry only — see
# ``display_suppressed``.
_display_muted: ContextVar[bool] = ContextVar("kew_display_muted", default=False)


# Set while a driver worker thread runs one node's review or remediation, so
# its lines stay attributable when several run at once (#2110).
_line_scope: ContextVar[str | None] = ContextVar("kew_line_scope", default=None)
# One console line at a time: concurrent workers never interleave mid-line.
# Reentrant: a signal handler (SIGINT) runs on the main thread and prints
# through _emit; if it fires while that thread is inside _emit, a plain Lock
# would deadlock the process.
_echo_lock = threading.RLock()
# Set by a ``--json`` command (#2505): every warning and error line is also
# kept here, so the result summary can name why a command refused or failed.
_problem_lines: ContextVar[list[str] | None] = ContextVar(
    "kew_problem_lines", default=None
)


@contextmanager
def capture_problems() -> Iterator[list[str]]:
    """Collect the warning and error lines printed in this context."""
    lines: list[str] = []
    token = _problem_lines.set(lines)
    try:
        yield lines
    finally:
        _problem_lines.reset(token)


def _keep_problem(text: str) -> str:
    lines = _problem_lines.get()
    if lines is not None:
        lines.append(text)
    return text


@contextmanager
def line_scope(label: str) -> Iterator[None]:
    """Prefix every progress line printed in this context with ``[label]``.

    A ``ContextVar``, so it covers only the thread (or context) that set it; a
    concurrent review worker's lines name its node while the tick thread's
    lines stay unprefixed.
    """
    token = _line_scope.set(label)
    try:
        yield
    finally:
        _line_scope.reset(token)


def _scoped(msg: str) -> str:
    scope = _line_scope.get()
    return redact_credential_values(msg if scope is None else f"[{scope}] {msg}")


def _emit(text: str) -> None:
    with _echo_lock:
        typer.echo(text, err=True)


def _log(msg: str) -> None:
    _emit(typer.style("[dispatch]", fg=typer.colors.BLUE) + f" {_scoped(msg)}")


def _warn(msg: str) -> None:
    _emit(
        typer.style("[dispatch]", fg=typer.colors.YELLOW)
        + f" {_keep_problem(_scoped(msg))}"
    )


def _error(msg: str) -> None:
    _emit(
        typer.style("[dispatch]", fg=typer.colors.RED)
        + f" {_keep_problem(_scoped(msg))}"
    )


def _success(msg: str) -> None:
    _emit(typer.style("[dispatch]", fg=typer.colors.GREEN) + f" {_scoped(msg)}")


def _dim(msg: str) -> None:
    _emit(typer.style(f"[dispatch] {_scoped(msg)}", dim=True))


def _line_text(value: object) -> str:
    """Redact complete headers, then check credentials after line normalization."""
    text = redact_credential_values(runner_text(value).strip())
    return redact_credential_values(text.replace("\n", " "))


def summarize_tool_input(tool_input: Any) -> str:
    """Extract a short summary from a tool_use input field."""
    if isinstance(tool_input, str):
        return _line_text(tool_input)[:80]
    if isinstance(tool_input, dict):
        return runner_text(
            tool_input.get("command", "")
            or tool_input.get("pattern", "")
            or tool_input.get("file_path", "")
            or tool_input.get("description", "")
            or runner_text(tool_input)[:80]
        )
    return runner_text(tool_input)[:80]


def parse_stream_event(line: str) -> dict[str, Any] | None:
    """Parse a stream-json line. Returns event dict or None."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return None


@contextmanager
def display_suppressed() -> Iterator[None]:
    """Mute :func:`display_event` for the duration of the block.

    A runner's ``parse_line`` narrates to the operator console as a side effect,
    which is right when the line is being read live and wrong when a finished
    transcript is REPLAYED for its telemetry — the sandbox finalizer does
    exactly that, and without this the whole run would print a second time at
    completion (#1147). A ``ContextVar`` rather than a module global so a replay
    in one thread never mutes another thread's live stream.
    """
    token = _display_muted.set(True)
    try:
        yield
    finally:
        _display_muted.reset(token)


def display_event(event: dict[str, Any]) -> None:
    """Format and print a parsed stream-json event."""
    if _display_muted.get():
        return
    event_type = event.get("type", "")

    if event_type == "assistant":
        msg = event.get("message", "")
        if isinstance(msg, str) and msg.strip():
            _dim(f"[assistant] {_line_text(msg)[:120]}")
        elif isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")
                    if block_type == "tool_use":
                        name = runner_text(block.get("name", ""))
                        _log(
                            f"  [{name}] {summarize_tool_input(block.get('input', ''))}"
                        )
                    elif block_type == "text":
                        raw = block.get("text", "")
                        text = _line_text(raw)
                        if text:
                            _dim(f"  {text[:120]}")
                    # thinking blocks are intentionally suppressed
    elif event_type == "tool_use":
        tool = runner_text(event.get("tool", ""))
        _log(f"[{tool}] {summarize_tool_input(event.get('input', ''))}")
    elif event_type == "result":
        result = event.get("result", "")
        if isinstance(result, str):
            _success(f"[result] {_line_text(result)[:120]}")
    elif event_type == "error":
        _error(f"[error] {runner_text(event.get('error', ''))}")
    elif event_type == "system":
        _display_system_event(event)


def _format_usage(usage: Any) -> str:
    """Format a usage dict into a compact summary string."""
    if not isinstance(usage, dict) or not usage.get("total_tokens"):
        return ""
    parts = [f"{usage['total_tokens']:,} tokens"]
    if usage.get("tool_uses"):
        parts.append(f"{runner_text(usage['tool_uses'])} tools")
    if usage.get("duration_ms"):
        parts.append(f"{usage['duration_ms'] / 1000:.0f}s")
    return f" ({', '.join(parts)})"


def _display_system_event(event: dict[str, Any]) -> None:
    """Format system sub-events."""
    subtype = event.get("subtype", "")

    if subtype == "task_started":
        desc = runner_text(event.get("description", ""))
        task_type = runner_text(event.get("task_type", ""))
        suffix = f" ({task_type})" if task_type else ""
        _log(f"[started] {desc}{suffix}")
    elif subtype == "task_progress":
        desc = runner_text(event.get("description", ""))
        tool = runner_text(event.get("last_tool_name", ""))
        if desc:
            prefix = f"[{tool}] " if tool else ""
            _dim(f"  {prefix}{desc[:120]}")
    elif subtype == "task_notification":
        status = event.get("status", "")
        summary = runner_text(event.get("summary", ""))
        tokens = _format_usage(event.get("usage", {}))
        log_fn = _success if status == "completed" else _warn
        log_fn(f"[{runner_text(status)}] {summary}{tokens}")
    elif subtype == "init":
        model = runner_text(event.get("model", ""))
        version = runner_text(event.get("claude_code_version", ""))
        if model or version:
            _dim(f"[init] model={model} version={version}")
    elif subtype in ("hook_started", "hook_response"):
        pass  # Suppress noisy hook events
    else:
        msg = runner_text(event.get("message", "") or subtype)
        if msg:
            _dim(f"[system] {msg}")


def format_worker_status(workers: dict[int, dict[str, object]]) -> list[str]:
    """Format a compact status line for each active worker.

    Args:
        workers: Dict mapping issue_num to status dict with keys:
            - status: "initializing" | "running" | "completed" | "failed"
            - elapsed_min: int
            - description: str (current task description)

    Returns:
        List of formatted status lines, one per worker.
    """
    lines: list[str] = []
    for issue_num in sorted(workers):
        info = workers[issue_num]
        status = str(info.get("status", "unknown"))
        elapsed_raw = info.get("elapsed_min", 0)
        try:
            elapsed = (
                int(elapsed_raw)
                if isinstance(elapsed_raw, (int, float))
                else int(float(str(elapsed_raw)))
            )
        except (ValueError, TypeError):
            elapsed = 0
        desc = str(info.get("description", ""))

        if status == "initializing":
            bar = "\u2591\u2591\u2591\u2591\u2591\u2591"
            label = "initializing..."
        elif status == "completed":
            bar = "\u2588\u2588\u2588\u2588\u2588\u2588"
            label = f"done \u2014 {desc}" if desc else "done"
        elif status == "failed":
            bar = "\u2588\u2588\u2591\u2591\u2591\u2591"
            label = f"failed \u2014 {desc}" if desc else "failed"
        else:
            # Running — fill proportional to time (cap at 20min = full)
            filled = min(6, int(elapsed / 20 * 6))
            bar = "\u2588" * filled + "\u2591" * (6 - filled)
            label = desc if desc else "running..."

        lines.append(f"[#{issue_num}] {bar} {elapsed}m elapsed \u2014 {label}")
    return lines
