"""The structured JSON call against a fake OpenAI-compatible server (#2293, AC3).

Every test here talks to a real ``http.server`` on loopback — not a mock — so
the transport, the ``response_format`` negotiation, the repair retry and the
fail-closed boundary are exercised end to end. The server is scripted: each
test hands it the replies to give, in order, and reads back the requests it
received.

The contract under test, in three parts:

* **Schema-validity.** A valid instance of the pydantic model comes back as a
  validated object, and the first request carries
  ``response_format: json_schema``.
* **Fallback, not weakening.** A server that answers 4xx to ``response_format``
  gets asked again without it, and the SAME model still validates the content.
* **Fail closed.** One repair retry, and then
  :class:`~kew.structured_completion.StructuredCompletionError` — never a
  partially-read object.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel
from support.fake_openai import FakeOpenAI, unreachable_base_url
from support.fake_openai import completion as _completion

from kew.structured_completion import (
    CompletionResult,
    StructuredCompletionError,
    complete_json,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class Answer(BaseModel):
    """A two-field schema, with ``extra = "forbid"`` like ``TriageDecision``."""

    model_config = {"extra": "forbid"}

    verdict: str
    score: float


@pytest.fixture
def server() -> Iterator[Any]:
    """A loopback OpenAI-compatible server whose replies each test scripts."""
    fake = FakeOpenAI()
    try:
        yield fake.start
    finally:
        fake.close()


def _call(base_url: str) -> CompletionResult[Answer]:
    return complete_json(
        Answer,
        system="classify",
        user="the item",
        base_url=base_url,
        model="fake-local-7b",
        timeout=10.0,
    )


class TestSchemaValidity:
    def test_a_valid_object_comes_back_validated(self, server: Any) -> None:
        base_url, script = server(
            [(200, _completion(json.dumps({"verdict": "bug", "score": 0.9})))]
        )
        result = _call(base_url)
        assert result.value == Answer(verdict="bug", score=0.9)
        assert result.model == "fake-local-7b"
        assert (result.input_tokens, result.output_tokens) == (11, 7)
        assert result.repaired is False
        assert result.schema_enforced is True

    def test_the_first_request_asks_for_the_schema(self, server: Any) -> None:
        base_url, script = server(
            [(200, _completion(json.dumps({"verdict": "bug", "score": 0.9})))]
        )
        _call(base_url)
        sent = script.requests[0]["body"]
        assert sent["response_format"]["type"] == "json_schema"
        assert (
            sent["response_format"]["json_schema"]["schema"]
            == Answer.model_json_schema()
        )
        assert sent["stream"] is False
        assert sent["temperature"] == 0.0
        assert script.requests[0]["path"].endswith("/chat/completions")

    def test_the_schema_reaches_the_prompt_too(self, server: Any) -> None:
        """A server that ignores ``response_format`` still sees the schema."""
        base_url, script = server(
            [(200, _completion(json.dumps({"verdict": "bug", "score": 0.9})))]
        )
        _call(base_url)
        user = script.requests[0]["body"]["messages"][-1]["content"]
        assert "verdict" in user and "score" in user

    def test_a_fenced_object_is_still_an_answer(self, server: Any) -> None:
        fenced = '```json\n{"verdict": "chore", "score": 0.5}\n```'
        base_url, _ = server([(200, _completion(fenced))])
        assert _call(base_url).value.verdict == "chore"


class TestResponseFormatFallback:
    def test_a_refused_response_format_is_retried_without_it(self, server: Any) -> None:
        base_url, script = server(
            [
                (400, {"error": {"message": "unknown field response_format"}}),
                (200, _completion(json.dumps({"verdict": "feature", "score": 0.8}))),
            ]
        )
        result = _call(base_url)
        assert result.value.verdict == "feature"
        assert result.schema_enforced is False
        assert "response_format" in script.requests[0]["body"]
        assert "response_format" not in script.requests[1]["body"]

    def test_the_fallback_still_validates_against_the_model(self, server: Any) -> None:
        """Dropping enforcement moves the check; it does not remove it."""
        base_url, _ = server(
            [
                (400, {"error": "no response_format here"}),
                (200, _completion(json.dumps({"verdict": "bug"}))),
                (200, _completion(json.dumps({"verdict": "bug"}))),
            ]
        )
        with pytest.raises(StructuredCompletionError):
            _call(base_url)

    def test_a_refusal_of_the_fallback_request_too_fails_closed(
        self, server: Any
    ) -> None:
        """There is nothing left to drop: a 4xx here is just "no answer".

        The fallback request carries no ``response_format``, so its refusal
        cannot mean "I do not know that field". It must reach the caller as
        :class:`StructuredCompletionError` — the error every caller already
        turns into ``unavailable`` — and not as some other exception that
        escapes the handler.
        """
        base_url, script = server(
            [
                (400, {"error": {"message": "unknown field response_format"}}),
                (400, {"error": {"message": "context length exceeded"}}),
            ]
        )
        with pytest.raises(StructuredCompletionError, match="context length"):
            _call(base_url)
        assert len(script.requests) == 2, "one fallback, then closed"

    def test_a_server_error_is_not_a_reason_to_drop_the_schema(
        self, server: Any
    ) -> None:
        base_url, script = server([(503, {"error": "overloaded"})])
        with pytest.raises(StructuredCompletionError, match="HTTP 503"):
            _call(base_url)
        assert len(script.requests) == 1


class TestOneRepairRetryThenFailClosed:
    def test_invalid_json_earns_exactly_one_repair_turn(self, server: Any) -> None:
        base_url, script = server(
            [
                (200, _completion("I think it is a bug, honestly")),
                (200, _completion(json.dumps({"verdict": "bug", "score": 0.7}))),
            ]
        )
        result = _call(base_url)
        assert result.value == Answer(verdict="bug", score=0.7)
        assert result.repaired is True
        assert len(script.requests) == 2

    def test_the_repair_turn_carries_the_bad_answer_and_the_error(
        self, server: Any
    ) -> None:
        base_url, script = server(
            [
                (200, _completion('{"verdict": "bug"}')),
                (200, _completion(json.dumps({"verdict": "bug", "score": 0.1}))),
            ]
        )
        _call(base_url)
        messages = script.requests[1]["body"]["messages"]
        assert messages[-2]["role"] == "assistant"
        assert messages[-2]["content"] == '{"verdict": "bug"}'
        assert "score" in messages[-1]["content"]

    def test_a_second_invalid_answer_fails_closed(self, server: Any) -> None:
        base_url, script = server(
            [
                (200, _completion("not json")),
                (200, _completion("still not json")),
                (200, _completion(json.dumps({"verdict": "bug", "score": 0.1}))),
            ]
        )
        with pytest.raises(StructuredCompletionError, match="after one repair retry"):
            _call(base_url)
        assert len(script.requests) == 2, "exactly one repair retry, no more"

    def test_a_repaired_answer_reports_both_turns_tokens(self, server: Any) -> None:
        """Both calls were billed, so the result totals both (#2518 review)."""
        base_url, _ = server(
            [
                (200, _completion("not json")),
                (200, _completion(json.dumps({"verdict": "bug", "score": 0.7}))),
            ]
        )
        result = _call(base_url)
        assert (result.input_tokens, result.output_tokens) == (22, 14)

    def test_a_failed_repair_carries_the_spend_of_both_turns(self, server: Any) -> None:
        base_url, _ = server(
            [(200, _completion("not json")), (200, _completion("still not json"))]
        )
        with pytest.raises(StructuredCompletionError) as caught:
            _call(base_url)
        assert caught.value.model == "fake-local-7b"
        assert (caught.value.input_tokens, caught.value.output_tokens) == (22, 14)

    def test_a_refused_repair_turn_still_carries_the_first_turns_spend(
        self, server: Any
    ) -> None:
        base_url, _ = server(
            [
                (200, _completion("not json")),
                (400, {"error": {"message": "too many messages"}}),
            ]
        )
        with pytest.raises(StructuredCompletionError) as caught:
            _call(base_url)
        assert caught.value.model == "fake-local-7b"
        assert (caught.value.input_tokens, caught.value.output_tokens) == (11, 7)

    def test_a_failure_before_any_answer_carries_no_spend(self, server: Any) -> None:
        base_url, _ = server([(503, {"error": "overloaded"})])
        with pytest.raises(StructuredCompletionError) as caught:
            _call(base_url)
        assert caught.value.model is None

    def test_a_4xx_on_the_repair_turn_fails_closed(self, server: Any) -> None:
        """The repair turn's own refusal is a StructuredCompletionError."""
        base_url, script = server(
            [
                (200, _completion("not json")),
                (400, {"error": {"message": "too many messages"}}),
            ]
        )
        with pytest.raises(StructuredCompletionError, match="too many messages"):
            _call(base_url)
        assert len(script.requests) == 2, "the refused repair turn is not retried"

    def test_an_extra_field_is_a_validation_failure(self, server: Any) -> None:
        """``extra = "forbid"``: a model that invents fields has not answered."""
        invented = json.dumps({"verdict": "bug", "score": 0.9, "invented": True})
        base_url, _ = server(
            [(200, _completion(invented)), (200, _completion(invented))]
        )
        with pytest.raises(StructuredCompletionError):
            _call(base_url)

    def test_an_envelope_with_no_choices_fails_closed(self, server: Any) -> None:
        base_url, script = server([(200, {"model": "fake", "choices": []})])
        with pytest.raises(StructuredCompletionError, match="no choices"):
            _call(base_url)
        assert len(script.requests) == 1, "a broken envelope is not a repairable answer"

    def test_an_unreachable_endpoint_fails_closed_without_retrying(self) -> None:
        with pytest.raises(StructuredCompletionError, match="connection"):
            _call(unreachable_base_url())
