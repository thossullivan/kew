"""One non-agent JSON call to an OpenAI-compatible ``/chat/completions`` (#2293).

The triage station needs a classifier, not an agent: one request, one JSON
object back, validated against a pydantic model. No tools, no loop, no
filesystem — so this module is deliberately NOT
:mod:`kew.runners.lemonade_agent`, which is the agentic loop over the same
endpoint. It reuses that module's transport shape (stdlib ``urllib``, no new
dependency) and the endpoint an operator already configured in
``[agent.lemonade]``.

Two things make the answer trustworthy enough to record:

* **Schema enforcement where the server offers it.** The first attempt sends
  ``response_format: {"type": "json_schema", ...}``. A server that rejects the
  field (an older llama-server, Ollama's ``/v1``) answers 4xx, and the call
  falls back to asking for JSON in the message content — validated by the SAME
  pydantic model, so the contract does not weaken, only the enforcement point
  moves. ``CompletionResult.schema_enforced`` records which happened.
* **Exactly one repair retry, then fail closed.** Unparseable or invalid JSON
  earns one more turn carrying the bad answer and the validation error. If that
  turn is also invalid, :class:`StructuredCompletionError` is raised. The caller
  turns that into "unavailable" — never into a half-read decision
  (``kew.triage.policy``).

Nothing here retries a TRANSPORT failure: an unreachable endpoint is reported
immediately, because the station's fallback for that is another backend, not
another request to the same dead socket.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from kew.config import LemonadeConfig

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_ERROR_DETAIL = 500
_MAX_CONTENT_ECHO = 4000

_JSON_INSTRUCTION = (
    "Reply with a single JSON object and nothing else: no prose, no markdown"
    " fence, no explanation. It must validate against this JSON Schema:\n"
)
_REPAIR_INSTRUCTION = (
    "That reply was not a valid instance of the schema. The error was:\n"
    "{error}\n\n"
    "Reply again with ONLY the corrected JSON object."
)


class StructuredCompletionError(RuntimeError):
    """The endpoint gave no valid instance of the requested schema.

    Raised for a transport failure, an HTTP error (including a 4xx on any
    request that already dropped ``response_format``), an unreadable response
    envelope, and an answer that still failed validation after the one repair
    retry. The caller treats every one of them the same way: fail closed.

    A failure can still have cost money: when the first turn came back and the
    repair turn then failed, ``model`` names the model that answered and the
    token fields total every turn that came back. ``model`` is None when no
    turn returned a readable envelope — nothing reached the model's ledger.
    """

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class CompletionResult[M: BaseModel]:
    """One validated structured answer, and how it was obtained.

    ``schema_enforced`` is False when the server refused
    ``response_format: json_schema`` and the JSON came back in the message
    content instead; ``repaired`` is True when the first answer failed
    validation and the single retry fixed it. Both reach the ledger row: a
    decision the model had to be corrected into is not the same evidence as one
    it got right. The token counts total every turn that came back, so a
    repaired answer reports what both calls cost, not only the second.
    """

    value: M
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    repaired: bool = False
    schema_enforced: bool = True


def complete_json[M: BaseModel](
    schema: type[M],
    *,
    system: str,
    user: str,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    schema_name: str = "decision",
) -> CompletionResult[M]:
    """Ask *base_url* for one instance of *schema*, or raise.

    *user* is the prompt, which carries attacker-reachable content fenced by
    its caller (``kew.triage.classify``). This function never interprets it: it
    is a string in a request body.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"{user}\n\n{_JSON_INSTRUCTION}{json.dumps(schema.model_json_schema())}"
            ),
        },
    ]
    enforced, body = _first_attempt(
        schema,
        messages,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        schema_name=schema_name,
    )
    content, reported_model, usage = _read_envelope(body, model)
    try:
        value = _validate(schema, content)
    except ValidationError as first_error:
        return _repair(
            schema,
            messages,
            bad_content=content,
            error=first_error,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            enforced=enforced,
            spent=(reported_model, usage),
        )
    return CompletionResult(
        value=value,
        model=reported_model,
        input_tokens=usage[0],
        output_tokens=usage[1],
        schema_enforced=enforced,
    )


def _first_attempt[M: BaseModel](
    schema: type[M],
    messages: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    schema_name: str,
) -> tuple[bool, dict[str, Any]]:
    """POST with ``response_format``, falling back once if it is refused.

    The fallback is narrow on purpose: only a 4xx — the shape a server uses to
    say "I do not know this field" — drops schema enforcement. A 5xx or a
    transport error is a failure and propagates, because retrying it without
    the schema would turn an outage into a weaker answer.
    """
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema.model_json_schema()},
    }
    try:
        return True, _chat(
            messages,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            response_format=response_format,
        )
    except _HttpRefusal as refusal:
        _logger.info(
            "Structured completion endpoint refused response_format"
            " (HTTP %s); asking for JSON in the content instead",
            refusal.status,
        )
    return False, _chat(
        messages,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        response_format=None,
    )


def _repair[M: BaseModel](
    schema: type[M],
    messages: list[dict[str, Any]],
    *,
    bad_content: str,
    error: ValidationError,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    enforced: bool,
    spent: tuple[str, tuple[int, int]],
) -> CompletionResult[M]:
    """The one repair turn. A second invalid answer fails closed.

    *spent* is the first turn's ``(model, (input, output))``: that call was
    already paid for, so it rides on the result's token totals, and on the
    error when this turn fails, rather than being dropped.
    """
    retry_messages = [
        *messages,
        {"role": "assistant", "content": bad_content[:_MAX_CONTENT_ECHO]},
        {"role": "user", "content": _REPAIR_INSTRUCTION.format(error=error)},
    ]
    first_model, (first_in, first_out) = spent
    try:
        body = _chat(
            retry_messages,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            response_format=None,
        )
        content, reported_model, usage = _read_envelope(body, model)
    except StructuredCompletionError as exc:
        _attach_spend(exc, first_model, first_in, first_out)
        raise
    input_tokens, output_tokens = first_in + usage[0], first_out + usage[1]
    try:
        value = _validate(schema, content)
    except ValidationError as second_error:
        failure = StructuredCompletionError(
            f"invalid {schema.__name__} after one repair retry: {second_error}"
        )
        _attach_spend(failure, reported_model, input_tokens, output_tokens)
        raise failure from second_error
    return CompletionResult(
        value=value,
        model=reported_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        repaired=True,
        schema_enforced=enforced,
    )


def _attach_spend(
    exc: StructuredCompletionError, model: str, input_tokens: int, output_tokens: int
) -> None:
    """Record on *exc* the turns that came back before it was raised."""
    exc.model = model
    exc.input_tokens = input_tokens
    exc.output_tokens = output_tokens


def _validate[M: BaseModel](schema: type[M], content: str) -> M:
    """Validate *content* against *schema*, tolerating a markdown fence.

    A fenced object is still an answer; anything else is not, and a non-JSON
    reply is raised as a ``ValidationError`` so the caller's single repair path
    handles "not JSON" and "wrong JSON" identically.
    """
    text = _unfence(content)
    try:
        return schema.model_validate_json(text)
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError.from_exception_data(
            schema.__name__,
            [
                {
                    "type": "json_invalid",
                    "loc": (),
                    "input": text[:_MAX_CONTENT_ECHO],
                    "ctx": {"error": str(exc)},
                }
            ],
        ) from exc


def _unfence(content: str) -> str:
    """Strip a ```` ```json ```` fence, if the model wrapped its object in one."""
    text = (content or "").strip()
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else ""
    return body.rsplit("```", 1)[0].strip()


def _read_envelope(
    body: dict[str, Any], requested_model: str
) -> tuple[str, str, tuple[int, int]]:
    """The message content, the reported model, and (input, output) tokens.

    An envelope with no choices is a protocol failure rather than an empty
    answer: raising here keeps "the server said nothing" from reaching the
    repair path as "the model replied badly".
    """
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise StructuredCompletionError("response carried no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise StructuredCompletionError("response choice carried no message")
    raw_usage = body.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    return (
        str(message.get("content") or ""),
        str(body.get("model") or requested_model),
        (
            _as_int(usage.get("prompt_tokens")),
            _as_int(usage.get("completion_tokens")),
        ),
    )


def _as_int(value: Any) -> int:
    """A reported token count, or 0 when the server reported none.

    Deliberately not an ``except`` handler: a local endpoint commonly omits
    ``usage`` altogether, and that absence is a KNOWN state read as one rather
    than a failure defaulted over (CONTRIBUTING.md, "No silent clean
    defaults"). The count is evidence about a free local call, never a gate.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


class _HttpRefusal(StructuredCompletionError):
    """A 4xx from the endpoint: the request shape was refused, not the service.

    A subclass of :class:`StructuredCompletionError` on purpose. Only
    :func:`_first_attempt` has any use for the distinction — it drops
    ``response_format`` and asks once more. Every OTHER request in this module
    (that fallback itself, the repair turn) already carries no
    ``response_format``, so a 4xx there has no narrower reading than "no valid
    answer"; escaping as a bare exception would crash the caller instead of
    becoming the documented ``unavailable`` row
    (``kew.triage.classify._classify_local``).
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def _chat(
    messages: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
    """POST one non-streaming completion. ``temperature = 0`` — this is a classifier."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    return _post_json(
        base_url.rstrip("/") + "/chat/completions", payload, api_key, timeout
    )


def _post_json(
    url: str, payload: dict[str, Any], api_key: str, timeout: float
) -> dict[str, Any]:
    """POST JSON to an OpenAI-compatible endpoint and return the parsed body.

    The same stdlib shape as ``kew.runners.lemonade_agent._post_json``: no new
    dependency, and one place per transport.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'lemonade'}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: E501  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — base_url is operator-configured (kew.toml), not attacker-controlled
            body: Any = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:_MAX_ERROR_DETAIL]
        if 400 <= exc.code < 500:
            raise _HttpRefusal(exc.code, detail) from exc
        raise StructuredCompletionError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise StructuredCompletionError(f"connection: {exc}") from exc
    except ValueError as exc:
        raise StructuredCompletionError(f"unreadable response body: {exc}") from exc
    if not isinstance(body, dict):
        raise StructuredCompletionError(
            f"response body is {type(body).__name__}, not an object"
        )
    return body


def endpoint_for(lemonade: LemonadeConfig) -> tuple[str, str]:
    """The ``(base_url, api_key)`` pair an operator already configured.

    One reader for the endpoint, so the classifier cannot drift from the
    runner that shares it.
    """
    return lemonade.base_url, lemonade.api_key
