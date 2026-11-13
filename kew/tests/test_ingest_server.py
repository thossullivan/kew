import hashlib
import hmac
import http.client
import json
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from kew.ingest.server import MAX_BODY_BYTES, make_server
from kew.run_store import SqliteRunStore


def _fixture_bytes():
    return (Path(__file__).parent / "fixtures" / "claude_code_otel.json").read_bytes()


def _serve(store):
    server = make_server("127.0.0.1", 0, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post(port, path, body, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers=headers or {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


_SECRET = "linear-test-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _linear_body(ts_ms=None, event_type="Issue", action="create"):
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    doc = {
        "action": action,
        "type": event_type,
        "data": {"id": "issue-abc", "title": "hi"},
        "webhookTimestamp": ts_ms,
    }
    return json.dumps(doc).encode()


def _post_full(port, path, body, headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_webhook_accepts_signed_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body()
            status, resp = _post_full(
                port,
                "/webhooks/linear",
                body,
                {
                    "Content-Type": "application/json",
                    "Linear-Signature": _sign(body),
                    "Linear-Delivery": "deliv-1",
                },
            )
            assert status == 200
            assert json.loads(resp)["status"] == "recorded"
            rows = store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC))
            assert len(rows) == 1
            assert rows[0].event_type == "Issue"
            assert rows[0].delivery_id == "deliv-1"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_idempotent_on_delivery_id(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body()
            headers = {
                "Content-Type": "application/json",
                "Linear-Signature": _sign(body),
                "Linear-Delivery": "deliv-1",
            }
            assert _post_full(port, "/webhooks/linear", body, headers)[0] == 200
            status, resp = _post_full(port, "/webhooks/linear", body, headers)
            assert status == 200
            assert json.loads(resp)["status"] == "deduped"
            assert (
                len(store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC)))
                == 1
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_rejects_bad_signature_no_body_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body()
            status, resp = _post_full(
                port,
                "/webhooks/linear",
                body,
                {
                    "Content-Type": "application/json",
                    "Linear-Signature": "deadbeef",
                    "Linear-Delivery": "deliv-1",
                },
            )
            assert status == 401
            assert resp == b""  # no body echo
            assert (
                store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC)) == []
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_rejects_unsigned(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body()
            status, _ = _post_full(
                port, "/webhooks/linear", body, {"Content-Type": "application/json"}
            )
            assert status == 401
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_rejects_when_secret_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body()
            status, _ = _post_full(
                port,
                "/webhooks/linear",
                body,
                {
                    "Content-Type": "application/json",
                    "Linear-Signature": _sign(body),
                    "Linear-Delivery": "deliv-1",
                },
            )
            assert status == 401
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_rejects_stale_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            # 10 minutes in the past — outside the replay window.
            stale = int(time.time() * 1000) - 600_000
            body = _linear_body(ts_ms=stale)
            status, _ = _post_full(
                port,
                "/webhooks/linear",
                body,
                {
                    "Content-Type": "application/json",
                    "Linear-Signature": _sign(body),
                    "Linear-Delivery": "deliv-1",
                },
            )
            assert status == 401
            assert (
                store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC)) == []
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_webhook_ignores_unsupported_event_type(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _linear_body(event_type="Cycle")
            status, resp = _post_full(
                port,
                "/webhooks/linear",
                body,
                {
                    "Content-Type": "application/json",
                    "Linear-Signature": _sign(body),
                    "Linear-Delivery": "deliv-1",
                },
            )
            assert status == 200
            assert json.loads(resp)["status"] == "ignored"
            assert (
                store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC)) == []
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_ingests_posted_metrics(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            status = _post(port, "/v1/metrics", _fixture_bytes())
            assert status == 200
            rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
            assert len(rows) == 3
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_wrong_path_and_bad_json(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            assert _post(port, "/wrong", _fixture_bytes()) == 404
            assert _post(port, "/v1/metrics", b"not json") == 400
            # server still serving after a bad request
            assert _post(port, "/v1/metrics", _fixture_bytes()) == 200
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_non_numeric_content_length(tmp_path):
    # urllib overrides Content-Length, so use raw http.client to send "abc".
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.putrequest("POST", "/v1/metrics")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "abc")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            conn.close()
            # server survived — subsequent valid request succeeds
            assert _post(port, "/v1/metrics", _fixture_bytes()) == 200
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_negative_content_length(tmp_path):
    # int("-1") succeeds; read(-1) would read to EOF and bypass the cap.
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.putrequest("POST", "/v1/metrics")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "-1")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            conn.close()
            # nothing ingested, and the server survived
            assert (
                store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
                == []
            )
            assert _post(port, "/v1/metrics", _fixture_bytes()) == 200
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_ingests_chunked_transfer_encoding(tmp_path):
    """Real Claude Code sends OTLP/HTTP with Transfer-Encoding: chunked (#383)."""
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _fixture_bytes()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.putrequest("POST", "/v1/metrics")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            # Send body as a single chunk followed by the terminator.
            chunk_header = f"{len(body):x}\r\n".encode()
            conn.send(chunk_header + body + b"\r\n0\r\n\r\n")
            resp = conn.getresponse()
            assert resp.status == 200
            rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
            assert len(rows) == 3
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_oversized_chunked_body(tmp_path):
    """Chunked bodies exceeding 1 MiB must be rejected (#383)."""
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            big = b"x" * (MAX_BODY_BYTES + 1)
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.putrequest("POST", "/v1/metrics")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            chunk_header = f"{len(big):x}\r\n".encode()
            try:
                conn.send(chunk_header + big + b"\r\n0\r\n\r\n")
                resp = conn.getresponse()
                assert resp.status == 413
            except (ConnectionError, OSError):
                pass  # connection reset is an acceptable rejection
            assert (
                store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
                == []
            )
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_ingests_gzip_encoded_body(tmp_path):
    """OTLP exporters may gzip the body; the server should decompress (#383)."""
    import gzip as _gzip

    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            body = _gzip.compress(_fixture_bytes())
            status = _post(
                port,
                "/v1/metrics",
                body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
            assert status == 200
            rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
            assert len(rows) == 3
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_malformed_chunked_body(tmp_path):
    """Malformed chunk framing returns 400, not 413 (#385)."""
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.putrequest("POST", "/v1/metrics")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"NOT_HEX\r\n")
            resp = conn.getresponse()
            assert resp.status == 400
            assert (
                store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
                == []
            )
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_oversized_gzip_body(tmp_path):
    """A gzip bomb that expands past 1 MiB returns 413, not OOM (#385)."""
    import gzip as _gzip

    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            big = b"x" * (MAX_BODY_BYTES + 1024)
            body = _gzip.compress(big)
            status = _post(
                port,
                "/v1/metrics",
                body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
            assert status == 413
            assert (
                store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
                == []
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_server_rejects_oversized_body(tmp_path):
    # The handler rejects an oversized body WITHOUT draining it (security:
    # never read >1 MiB). Depending on socket timing the client sees either a
    # clean 413 or a connection reset — both are valid "rejected". Assert the
    # real property: nothing was ingested and the server still serves.
    with SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False) as store:
        server, thread = _serve(store)
        try:
            port = server.server_address[1]
            big = b"x" * (MAX_BODY_BYTES + 1)
            try:
                status = _post(port, "/v1/metrics", big)
                assert status in (413, 400)
            except (urllib.error.URLError, ConnectionError):
                pass  # connection reset is an acceptable rejection
            assert (
                store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
                == []
            )
            # server survived the oversized request
            assert _post(port, "/v1/metrics", _fixture_bytes()) == 200
        finally:
            server.shutdown()
            thread.join(timeout=5)
