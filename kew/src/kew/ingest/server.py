"""Loopback-only OTLP/HTTP receiver for Claude Code metrics (#256).

Single-threaded stdlib http.server. Accepts POST /v1/metrics with an
OTLP/JSON body (<= 1 MiB), feeds ingest_document, and never crashes on bad
input. Bind-host loopback enforcement lives in IngestConfig; this module
trusts the host it is given but reads nothing remote by design.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BufferedIOBase, BytesIO

from kew.ingest.core import ingest_document, ingest_tracker_event
from kew.run_store import RunStore

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 << 20  # 1 MiB
_METRICS_PATH = "/v1/metrics"
_LINEAR_WEBHOOK_PATH = "/webhooks/linear"
# Linear signs with the workspace webhook secret (env only, never config/logs).
_LINEAR_SECRET_ENV = "LINEAR_WEBHOOK_SECRET"  # nosec B105 — env var name, not a secret  # noqa: S105
# Reject deliveries whose payload timestamp is outside this window (replay
# discipline). Linear stamps webhookTimestamp (ms epoch) and recommends ~1 min.
LINEAR_REPLAY_WINDOW_SECONDS = 60


class _ChunkedBodyTooLarge(Exception):
    pass


class _InvalidChunkedBody(Exception):
    pass


def _read_chunked(rfile: BufferedIOBase, limit: int) -> bytes:
    """Read a chunked transfer-encoded body, enforcing *limit*."""
    buf = bytearray()
    while True:
        line = rfile.readline(64)
        if not line:
            raise _InvalidChunkedBody
        try:
            chunk_size = int(line.strip(), 16)
        except ValueError:
            raise _InvalidChunkedBody from None
        if chunk_size == 0:
            rfile.readline(64)
            break
        if len(buf) + chunk_size > limit:
            raise _ChunkedBodyTooLarge
        data = rfile.read(chunk_size)
        if len(data) != chunk_size:
            raise _InvalidChunkedBody
        buf.extend(data)
        if rfile.readline(64) != b"\r\n":
            raise _InvalidChunkedBody
    return bytes(buf)


def _decompress_gzip_bounded(raw: bytes, limit: int) -> bytes | None:
    """Decompress gzip, returning None if the result exceeds *limit*."""
    with gzip.GzipFile(fileobj=BytesIO(raw)) as gz:
        data = gz.read(limit + 1)
    if len(data) > limit:
        return None
    return data


def _linear_timestamp_fresh(doc: object) -> bool:
    """True iff the payload's webhookTimestamp is within the replay window.

    A missing or non-numeric timestamp fails closed: without it we cannot
    bound replay, so the delivery is rejected.
    """
    if not isinstance(doc, dict):
        return False
    ts = doc.get("webhookTimestamp")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    now_ms = datetime.now(UTC).timestamp() * 1000
    return abs(now_ms - ts) <= LINEAR_REPLAY_WINDOW_SECONDS * 1000


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: str = "") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_raw_body(self) -> bytes | None:
        """Read the raw request body, replying on failure."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            try:
                return _read_chunked(self.rfile, MAX_BODY_BYTES)
            except _ChunkedBodyTooLarge:
                self._reply(413, '{"error": "payload too large"}')
                return None
            except _InvalidChunkedBody:
                self._reply(400, '{"error": "invalid chunked body"}')
                return None
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._reply(400, '{"error": "invalid content-length"}')
            return None
        if length < 0:
            self._reply(400, '{"error": "invalid content-length"}')
            return None
        if length > MAX_BODY_BYTES:
            self._reply(413, '{"error": "payload too large"}')
            return None
        return self.rfile.read(length)

    def _maybe_decompress(self, raw: bytes) -> bytes | None:
        """Decompress gzip body if Content-Encoding requires it, replying on failure."""
        ce = (self.headers.get("Content-Encoding") or "").lower()
        if "gzip" not in ce:
            return raw
        try:
            decompressed = _decompress_gzip_bounded(raw, MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001
            self._reply(400, '{"error": "invalid gzip body"}')
            return None
        if decompressed is None:
            self._reply(413, '{"error": "payload too large"}')
            return None
        return decompressed

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler contract)
        if self.path == _METRICS_PATH:
            self._handle_metrics()
            return
        if self.path == _LINEAR_WEBHOOK_PATH:
            self._handle_linear_webhook()
            return
        self._reply(404, '{"error": "not found"}')

    def _handle_metrics(self) -> None:
        raw = self._read_raw_body()
        if raw is None:
            return
        raw = self._maybe_decompress(raw)
        if raw is None:
            return

        try:
            doc = json.loads(raw)
        except ValueError:
            self._reply(400, '{"error": "invalid json"}')
            return
        store: RunStore = self.server.store  # type: ignore[attr-defined]
        try:
            summary = ingest_document(doc, ingest_source="serve", store=store)
        except Exception as exc:  # noqa: BLE001 (defense-in-depth: malformed OTLP body)
            logger.warning("ingest_document failed: %s", exc)
            self._reply(400, '{"error": "malformed otlp body"}')
            return
        self._reply(200, json.dumps(summary.__dict__))

    def _handle_linear_webhook(self) -> None:
        raw = self._read_raw_body()
        if raw is None:
            return
        raw = self._maybe_decompress(raw)
        if raw is None:
            return

        # HMAC verification precedes any use of the body. Reject unsigned,
        # unverifiable, or replayed deliveries with a bare 401 (no body echo).
        if not self._linear_signature_ok(raw):
            self._reply(401)
            return
        try:
            doc = json.loads(raw)
        except ValueError:
            self._reply(400, '{"error": "invalid json"}')
            return
        if not _linear_timestamp_fresh(doc):
            self._reply(401)
            return

        delivery_id = self.headers.get("Linear-Delivery")
        if not delivery_id:
            # Fall back to the signature: it is a stable per-body fingerprint,
            # so redelivery of the same event still dedups.
            delivery_id = self.headers.get("Linear-Signature", "")
        store: RunStore = self.server.store  # type: ignore[attr-defined]
        try:
            result = ingest_tracker_event(
                doc, delivery_id=delivery_id, source="linear", store=store
            )
        except Exception as exc:  # noqa: BLE001 (defense-in-depth: malformed body)
            logger.warning("ingest_tracker_event failed: %s", exc)
            self._reply(400, '{"error": "malformed webhook body"}')
            return
        self._reply(200, json.dumps({"status": result.status}))

    def _linear_signature_ok(self, raw: bytes) -> bool:
        """Verify the Linear-Signature HMAC-SHA256 over the raw body.

        Returns False on a missing secret (unverifiable) or a missing/invalid
        signature. Never logs the secret or the signature.
        """
        secret = os.environ.get(_LINEAR_SECRET_ENV)
        if not secret:
            return False
        signature = self.headers.get("Linear-Signature", "")
        if not signature:
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


class _IngestServer(HTTPServer):
    def __init__(self, address: tuple[str, int], store: RunStore) -> None:
        super().__init__(address, _Handler)
        self.store = store


def make_server(host: str, port: int, store: RunStore) -> HTTPServer:
    """Build (do not start) the loopback OTLP/HTTP ingest server."""
    return _IngestServer((host, port), store)
