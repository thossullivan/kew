"""A scripted, loopback OpenAI-compatible ``/chat/completions`` server (#2293).

Shared by the structured-completion tests and the triage station's tests, so
"what the model said" is a fixture rather than a mock of kew's own transport:
the request shape, the ``response_format`` negotiation and the repair retry all
cross a real socket.

Each test hands :class:`FakeOpenAI` the replies to give, in order, and reads
back the requests it received.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


def completion(content: str, *, model: str = "fake-local-7b") -> dict[str, Any]:
    """An OpenAI-shaped envelope carrying *content* as the assistant message."""
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def json_completion(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """An envelope whose content is *payload* serialized."""
    return completion(json.dumps(payload), **kwargs)


class _Script:
    """The replies a fake server gives, and the requests it got."""

    def __init__(self, replies: list[tuple[int, dict[str, Any]]]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def next_reply(self) -> tuple[int, dict[str, Any]]:
        if not self.replies:
            return 500, {"error": "the script ran out of replies"}
        return self.replies.pop(0)


class _Handler(BaseHTTPRequestHandler):
    script: _Script

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.script.requests.append({"path": self.path, "body": body})
        status, payload = self.script.next_reply()
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: Any) -> None:
        """Silence the handler's stderr logging under pytest."""


class FakeOpenAI:
    """Starts scripted servers and shuts every one of them down on exit."""

    def __init__(self) -> None:
        self._started: list[HTTPServer] = []

    def start(self, replies: list[tuple[int, dict[str, Any]]]) -> tuple[str, _Script]:
        """Serve *replies* in order; return ``(base_url, script)``."""
        script = _Script(replies)
        handler = type("_Scripted", (_Handler,), {"script": script})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        self._started.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        host, port = httpd.server_address[0], httpd.server_address[1]
        return f"http://{host}:{port}/v1", script

    def close(self) -> None:
        for httpd in self._started:
            httpd.shutdown()
            httpd.server_close()
        self._started.clear()


def unreachable_base_url() -> str:
    """A loopback URL with nothing listening — for the fail-closed path."""
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    httpd.server_close()
    return f"http://127.0.0.1:{port}/v1"
