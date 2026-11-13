"""Native agentic loop for the Lemonade runner (``kew-lemonade`` CLI).

This is kew's *own* coding-agent loop — it does not shell out to Claude Code,
OpenCode, or pi. It drives a local, OpenAI-compatible model server (AMD
Lemonade at ``:13305/v1``, or any OpenAI-compatible endpoint: llama-server,
LM Studio, vLLM, Ollama's ``/v1``) through a tool-use loop and edits files in
the current working directory (the worktree kew hands it).

Design contract with :class:`kew.runners.lemonade.LemonadeRunner`
----------------------------------------------------------------
The runner shells out to ``kew-lemonade`` and parses this program's **stdout**,
one JSON object per line. The event schema deliberately mirrors pi's so the
runner's ``parse_line`` stays small and familiar:

* first line ``{"type": "session", "id": "<uuid>"}``
* per tool call ``{"type": "tool_execution_start", "toolName": ..., "args": {...}}``
* per assistant turn ``{"type": "message_end", "message": {...}}`` where the
  message carries ``content`` (text blocks), ``usage`` (input/output), a
  ``stopReason`` (``toolUse`` | ``stop`` | ``error`` | ``aborted``), the
  resolved ``model``, and — on failure — an ``errorMessage``.

Only the Python standard library is used (``urllib``) so the CLI adds no new
runtime dependency and stays trivially installable as a console script.

Budget note: this loop reports token *usage* but no dollar cost — a local model
has no per-token API price. kew's ``BillingConfig`` already frames local
inference as ≈free; the outer dispatch budget gate still applies as a wall.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 — runs the model's bash tool inside the worktree
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kew.credential_environment import child_environment

_SYSTEM_PROMPT = (
    "You are a precise software engineer working inside a git repository. "
    "The current working directory is the repository root. Use the provided "
    "tools to read and modify files and run commands. Make the smallest change "
    "that fully resolves the task, keep edits surgical, and run the project's "
    "tests or checks when relevant. When the task is complete, call the "
    "`finish` tool with a short summary. Do not ask the user questions; act."
)

# ---------------------------------------------------------------------------
# Event emission (stdout JSONL — the runner's parse_line consumes these)
# ---------------------------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    """Write one JSON event to stdout and flush (streaming to the runner)."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _emit_session(session_id: str) -> None:
    _emit({"type": "session", "id": session_id})


def _emit_tool_start(tool_name: str, args: dict[str, Any]) -> None:
    _emit({"type": "tool_execution_start", "toolName": tool_name, "args": args})


def _emit_message_end(
    *,
    text: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str,
    model: str,
    error_message: str = "",
) -> None:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": {"input": input_tokens, "output": output_tokens},
        "stopReason": stop_reason,
        "model": model,
    }
    if error_message:
        message["errorMessage"] = error_message
    _emit({"type": "message_end", "message": message})


# ---------------------------------------------------------------------------
# Tools — the agent's hands inside the worktree
# ---------------------------------------------------------------------------

_MAX_TOOL_OUTPUT = 20_000  # chars; keep tool results from blowing the context


def _clip(text: str, limit: int = _MAX_TOOL_OUTPUT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


def _resolve_in_worktree(root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``root``; return None if it escapes the worktree."""
    path = (root / rel).resolve()
    if path == root or root in path.parents:
        return path
    return None


def _tool_read_file(root: Path, args: dict[str, Any]) -> str:
    path = _resolve_in_worktree(root, str(args["path"]))
    if path is None:
        return f"error: path {args['path']!r} escapes the worktree"
    try:
        return _clip(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return f"error: {exc}"


def _tool_list_dir(root: Path, args: dict[str, Any]) -> str:
    target = _resolve_in_worktree(root, str(args.get("path", ".")))
    if target is None:
        return f"error: path {args.get('path')!r} escapes the worktree"
    try:
        entries = sorted(
            f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir()
        )
        return "\n".join(entries) or "(empty)"
    except OSError as exc:
        return f"error: {exc}"


def _tool_write_file(root: Path, args: dict[str, Any]) -> str:
    path = _resolve_in_worktree(root, str(args["path"]))
    if path is None:
        return f"error: path {args['path']!r} escapes the worktree"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return f"wrote {len(str(args['content']))} chars to {args['path']}"
    except OSError as exc:
        return f"error: {exc}"


def _tool_edit_file(root: Path, args: dict[str, Any]) -> str:
    """Exact-string replace: fails loudly if old_str is absent or ambiguous."""
    path = _resolve_in_worktree(root, str(args["path"]))
    if path is None:
        return f"error: path {args['path']!r} escapes the worktree"
    old = str(args["old_str"])
    new = str(args["new_str"])
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    count = content.count(old)
    if count == 0:
        return "error: old_str not found (no change made)"
    if count > 1:
        return f"error: old_str matches {count} times; make it unique"
    try:
        path.write_text(content.replace(old, new), encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    return f"edited {args['path']}"


def _tool_run_bash(root: Path, args: dict[str, Any]) -> str:
    """Run a model-supplied shell command in the worktree.

    This intentionally executes arbitrary shell — that is the whole point of a
    bash tool — so it is UNSANDBOXED at the process level. Routing it through the
    subprocess_utils allowlist (which permits only known executables like git/gh)
    would defeat the tool, not secure it. Blast-radius containment is the
    sandboxed-dispatch epic's job (#520): every runner, this loop included, is
    meant to run inside the hardened ephemeral container. cwd is scoped to the
    worktree.
    """
    cmd = str(args["command"])
    try:
        proc = subprocess.run(  # nosec B602 - model cmd, isolated worktree
            cmd,
            shell=True,  # noqa: E501  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true — bash tool runs model shell by design; contained by the sandbox epic (#520)
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=float(args.get("timeout", 300)),
            env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        return "error: command timed out"
    except OSError as exc:
        return f"error: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return _clip(f"[exit {proc.returncode}]\n{out}")


_TOOL_IMPLS = {
    "read_file": _tool_read_file,
    "list_dir": _tool_list_dir,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "run_bash": _tool_run_bash,
}

_TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact unique substring in a file. Fails if old_str "
                "is missing or matches more than once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the repo root and return output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Signal the task is complete. Provide a short summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Chat completion transport (OpenAI-compatible, stdlib only)
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    """Parsed assistant turn from a chat-completions response."""

    text: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    input_tokens: int
    output_tokens: int
    model: str


class _LemonadeError(RuntimeError):
    """Transport/protocol failure talking to the Lemonade endpoint."""


def _post_json(
    url: str, payload: dict[str, Any], api_key: str, timeout: float
) -> dict[str, Any]:
    """POST JSON to an OpenAI-compatible endpoint and return the parsed body."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'lemonade'}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: E501  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — base_url is operator-configured (kew.toml), not attacker-controlled
            body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise _LemonadeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise _LemonadeError(f"connection: {exc}") from exc
    except (ValueError, KeyError) as exc:
        raise _LemonadeError(f"bad response: {exc}") from exc


def _chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: float,
) -> _Turn:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": _TOOL_SCHEMA,
        "tool_choice": "auto",
        "temperature": 0.0,
        "stream": False,
    }
    body = _post_json(url, payload, api_key, timeout)
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    usage = body.get("usage", {}) or {}
    return _Turn(
        text=str(msg.get("content") or ""),
        tool_calls=list(msg.get("tool_calls") or []),
        finish_reason=str(choice.get("finish_reason") or ""),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        model=str(body.get("model") or model),
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class _Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    last_text: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)


def _run_tool(root: Path, name: str, args: dict[str, Any]) -> str:
    impl = _TOOL_IMPLS.get(name)
    if impl is None:
        return f"error: unknown tool {name!r}"
    try:
        return impl(root, args)
    except KeyError as exc:
        return f"error: missing required arg {exc}"
    except Exception as exc:  # tool errors must not crash the loop
        return f"error: {exc}"


def _handle_tool_calls(
    root: Path, tool_calls: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> bool:
    """Execute tool calls, append tool results to messages.

    Returns True if the model called ``finish`` (loop should stop).
    """
    finished = False
    for call in tool_calls:
        fn = call.get("function", {}) or {}
        name = str(fn.get("name", ""))
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        _emit_tool_start(name, args)
        if name == "finish":
            result = "ok"
            finished = True
        else:
            result = _run_tool(root, name, args)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            }
        )
    return finished


def _assistant_message(turn: _Turn) -> dict[str, Any]:
    """Reconstruct the assistant message to append to history."""
    msg: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
    if turn.tool_calls:
        msg["tool_calls"] = turn.tool_calls
    return msg


def run_loop(
    *,
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    session_id: str,
    max_steps: int,
    timeout: float,
    root: Path,
) -> int:
    """Drive the agent loop. Returns a process exit code (0 = success)."""
    _emit_session(session_id)
    totals = _Totals(model=model)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for _step in range(max_steps):
        try:
            turn = _chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                timeout=timeout,
            )
        except _LemonadeError as exc:
            _emit_message_end(
                text="",
                input_tokens=0,
                output_tokens=0,
                stop_reason="error",
                model=totals.model,
                error_message=str(exc),
            )
            return 1

        totals.input_tokens += turn.input_tokens
        totals.output_tokens += turn.output_tokens
        totals.model = turn.model or totals.model
        if turn.text:
            totals.last_text = turn.text

        messages.append(_assistant_message(turn))

        if turn.tool_calls:
            finished = _handle_tool_calls(root, turn.tool_calls, messages)
            _emit_message_end(
                text=turn.text,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                stop_reason="stop" if finished else "toolUse",
                model=turn.model,
            )
            if finished:
                return 0
            continue

        # No tool calls: the model produced a terminal answer. Surface the real
        # finish reason ("stop" normally, "length" when the response was
        # truncated) instead of always reporting "stop".
        _emit_message_end(
            text=turn.text,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            stop_reason=turn.finish_reason or "stop",
            model=turn.model,
        )
        return 0

    # Exhausted the step budget without an explicit finish.
    _emit_message_end(
        text=totals.last_text,
        input_tokens=0,
        output_tokens=0,
        stop_reason="aborted",
        model=totals.model,
        error_message=f"reached max_steps={max_steps} without finishing",
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kew-lemonade")
    parser.add_argument("-p", "--prompt", required=True)
    parser.add_argument("--base-url", default="http://localhost:13305/v1")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args(argv)

    session_id = args.session_id or str(uuid.uuid4())
    root = Path(args.cwd).resolve()
    # Prefer an explicit --api-key; otherwise take LEMONADE_API_KEY from the
    # environment (how the runner passes it, keeping it out of the process list).
    api_key = args.api_key or os.environ.get("LEMONADE_API_KEY", "")
    return run_loop(
        prompt=args.prompt,
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        session_id=session_id,
        max_steps=args.max_steps,
        timeout=args.timeout,
        root=root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
