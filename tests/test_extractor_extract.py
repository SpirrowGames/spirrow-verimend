"""ExtractionSession: output validation and the failure classes of design 5.2."""

from __future__ import annotations

import json

import httpx
import pytest
from extractor_fakes import FakeClient, claims, http, port_claim, session, transport

from verimend.extractor import Completion, Document, ExtractorAborted, ExtractorConfigError, LexoraClient
from verimend.extractor.chunking import MAX_CHUNK_TOKENS
from verimend.extractor.extract import parse_retry_after

ONE = Document("o/r", "README.md", "# Ports\nThe API listens on 8113.\n")
# three single-section chunks starting at lines 1, 3, 5
THREE = Document("o/r", "README.md", "# A\na\n# B\nb\n# C\nc\n")
FOUR = Document("o/r", "README.md", "# A\na\n# B\nb\n# C\nc\n# D\nd\n")


# ----------------------------------------------------------- happy path


def test_valid_output_becomes_claims_with_the_canonical_subject() -> None:
    s, sleeps = session(FakeClient(claims(port_claim(2, 8113))))
    result = s.extract(ONE)
    assert result.failures == []
    (c,) = result.claims
    assert (c.anchor_start, c.anchor_end, c.claim_type, c.model) == (2, 2, "port", "fake-model")
    assert json.loads(c.subject_json) == {"polarity": "current", "port": 8113, "service": None}
    assert sleeps == []


def test_the_llm_sees_absolute_line_numbers_and_the_tier() -> None:
    seen = []

    class Client(FakeClient):
        def complete(self, messages, tier):
            seen.append(tier)
            return super().complete(messages, tier)

    s, _ = session(Client("[]", "[]"), tier="heavy")
    s.extract(Document("o/r", "d.md", "# A\nx\n# B\ny\n"))
    assert seen == ["heavy", "heavy"]
    assert "4: y" in s.client.calls[1][1]["content"]


def test_a_code_fence_around_the_array_is_tolerated() -> None:
    s, _ = session(FakeClient("```json\n" + claims(port_claim(2, 8113)) + "\n```"))
    assert len(s.extract(ONE).claims) == 1


# -------------------------------------------------- frame and subject


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        json.dumps({"claims": []}),
        claims({k: v for k, v in port_claim(2, 8113).items() if k != "text"}),
        claims(port_claim(2, 8113, extra_key=1)),
        claims(port_claim(2, 8113, claim_type="opinion")),
        claims(port_claim(2, 8113, anchor_start=9, anchor_end=9)),  # outside the chunk (1-2)
        claims(port_claim(2, 8113, anchor_start=2, anchor_end=1)),  # start > end
        claims(port_claim(2, 8113, anchor_start="2")),
    ],
    ids=["not-json", "not-array", "missing-key", "extra-key", "unknown-type", "anchor-out-of-range",
         "start-after-end", "anchor-string"],
)
def test_frame_violation_is_retried_once_with_the_violations(bad: str) -> None:
    client = FakeClient(bad, claims(port_claim(2, 8113)))
    s, _ = session(client)
    result = s.extract(ONE)
    assert len(result.claims) == 1 and result.failures == []
    assert len(client.calls) == 2
    correction = client.calls[1][-1]["content"]
    assert "rejected" in correction
    assert client.calls[1][-2] == {"role": "assistant", "content": bad}


def test_frame_violation_twice_fails_the_chunk_as_schema_with_no_claims() -> None:
    client = FakeClient(claims(port_claim(2, 8113), port_claim(9, 1)), "nope")
    s, _ = session(client)
    result = s.extract(ONE)
    assert result.claims == []
    assert [(f.lines, f.error) for f in result.failures] == [((1, 2), "schema")]
    assert len(client.calls) == 2


def test_a_reply_without_message_content_is_a_frame_violation() -> None:
    client = FakeClient(Completion(text=None), "[]")
    s, _ = session(client)
    assert s.extract(ONE).failures == []
    assert len(client.calls) == 2


def test_subject_violation_is_retried_then_kept_raw_for_the_reconciler() -> None:
    bad_subject = port_claim(2, 8113)
    bad_subject["subject"] = {"polarity": "current", "port": "8113"}  # strict: a string is not a port
    client = FakeClient(claims(bad_subject), claims(bad_subject))
    s, _ = session(client)
    result = s.extract(ONE)
    assert len(client.calls) == 2
    assert result.failures == []
    (c,) = result.claims
    assert json.loads(c.subject_json) == {"polarity": "current", "port": "8113"}


def test_a_type_without_a_subject_model_requires_null() -> None:
    behaviour = {"anchor_start": 2, "anchor_end": 2, "text": "t", "claim_type": "behavior", "subject": {"x": 1}}
    fixed = dict(behaviour, subject=None)
    client = FakeClient(claims(behaviour), claims(fixed))
    s, _ = session(client)
    (c,) = s.extract(ONE).claims
    assert c.subject_json is None
    assert "must be null" in client.calls[1][-1]["content"]


# ------------------------------------------------------ oversized lines


def test_oversized_line_fails_without_calling_the_llm() -> None:
    doc = Document("o/r", "d.md", "# T\n" + "z" * (MAX_CHUNK_TOKENS * 4 + 8) + "\n")
    client = FakeClient(by_chunk=lambda start: "[]")
    s, _ = session(client)
    result = s.extract(doc)
    assert ("oversized_line" in [f.error for f in result.failures])
    assert 2 not in client.chunk_starts()


# ------------------------------------------------ permanent HTTP errors


@pytest.mark.parametrize("status", [400, 413, 422])
def test_permanent_4xx_is_not_retried_and_not_counted_as_transient(status: int) -> None:
    client = FakeClient(http(status), http(status), "[]", http(status))
    s, sleeps = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == [f"http_{status}"] * 3
    assert len(client.calls) == 4  # one call per chunk, no retry
    assert s.aborted is None and sleeps == [] and s.consecutive_transient == 0


@pytest.mark.parametrize("status", [400, 413, 422])
def test_three_permanent_4xx_in_a_row_abort_the_stage(status: int) -> None:
    client = FakeClient(http(status), http(status), http(status))
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == [f"http_{status}"] * 3 + ["extractor_aborted"]
    assert len(client.calls) == 3  # chunk 4 is never sent
    assert s.aborted == {
        "reason": "permanent_http_streak",
        "at_chunk": {"doc_repo": "o/r", "doc_path": "README.md", "lines": [5, 6]},
        "status": status,
    }


def test_a_successful_call_resets_the_permanent_http_streak() -> None:
    client = FakeClient(http(400), "[]", http(400), http(400))
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["http_400"] * 3
    assert s.aborted is None and len(client.calls) == 4


def test_a_transient_failure_does_not_reset_the_permanent_http_streak() -> None:
    client = FakeClient(http(400), http(400), http(503), http(503), http(400))
    s, _ = session(client)
    s.extract(FOUR)
    assert s.aborted is not None and s.aborted["reason"] == "permanent_http_streak"
    assert len(client.calls) == 5


def test_schema_failures_are_not_counted_in_the_permanent_http_streak() -> None:
    # chunk 1: 400; chunks 2-3: malformed twice (schema); chunk 4: 400 -> streak never reaches 3
    client = FakeClient(http(400), "x", "y", "x", "y", http(400))
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["http_400", "schema", "schema", "http_400"]
    assert s.aborted is None


def test_strict_raises_on_a_permanent_http_streak() -> None:
    s, _ = session(FakeClient(http(400), http(400), http(400)), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(FOUR)
    assert info.value.reason == "permanent_http_streak"


# ------------------------------------------------ permanent schema streak (msg-203)

BAD = ("x", "y")  # one chunk whose output stays malformed after the correction request


def test_three_schema_failures_in_a_row_abort_with_their_own_reason() -> None:
    client = FakeClient(*BAD, *BAD, *BAD, "[]")
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["schema"] * 3 + ["extractor_aborted"]
    assert len(client.calls) == 6  # chunk 4 is never sent
    assert s.aborted == {
        "reason": "permanent_schema_streak",
        "at_chunk": {"doc_repo": "o/r", "doc_path": "README.md", "lines": [5, 6]},
    }  # no status key: nothing answered with a 4xx


def test_strict_schema_streak_chains_the_third_failure() -> None:
    from verimend.llm_policy import Permanent

    s, _ = session(FakeClient(*BAD, *BAD, *BAD), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(FOUR)
    assert info.value.reason == "permanent_schema_streak"
    assert "status" not in info.value.detail
    assert isinstance(info.value.__cause__, Permanent) and info.value.__cause__.error == "schema"
    assert len(s.client.calls) == 6


def test_a_well_formed_response_resets_the_schema_streak() -> None:
    client = FakeClient(*BAD, "[]", *BAD, *BAD)
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["schema"] * 3
    assert s.aborted is None


def test_an_http_4xx_does_not_reset_the_schema_streak() -> None:
    client = FakeClient(*BAD, http(400), *BAD, *BAD)
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["schema", "http_400", "schema", "schema"]
    assert s.aborted is not None and s.aborted["reason"] == "permanent_schema_streak"
    assert "status" not in s.aborted and s.aborted["at_chunk"]["lines"] == [7, 8]


def test_a_transient_failure_does_not_reset_the_schema_streak() -> None:
    client = FakeClient(*BAD, *BAD, http(503), http(503), *BAD)
    s, _ = session(client)
    s.extract(FOUR)
    assert s.aborted is not None and s.aborted["reason"] == "permanent_schema_streak"


def test_a_schema_failure_resets_the_http_streak_and_is_counted_apart() -> None:
    client = FakeClient(http(400), *BAD, http(400), http(400))
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["http_400", "schema", "http_400", "http_400"]
    assert s.aborted is None
    assert (s.streaks.permanent_http, s.streaks.permanent_schema) == (2, 1)


# ------------------------------------------------ transient HTTP errors


@pytest.mark.parametrize("error", [http(500), http(502), http(503), http(429), http(408), transport()],
                         ids=["500", "502", "503", "429", "408", "transport"])
def test_transient_failure_is_retried_once_after_two_seconds(error: Exception) -> None:
    client = FakeClient(error, claims(port_claim(2, 8113)))
    s, sleeps = session(client)
    result = s.extract(ONE)
    assert len(result.claims) == 1 and result.failures == []
    assert sleeps == [2.0]


def test_transient_failure_after_its_retry_fails_the_chunk() -> None:
    s, _ = session(FakeClient(http(429), http(429), "[]", "[]"))
    result = s.extract(THREE)
    assert [(f.lines, f.error) for f in result.failures] == [((1, 2), "http_429")]
    assert s.consecutive_transient == 0  # reset by the next success
    assert s.aborted is None


def test_three_transient_chunk_failures_in_a_row_abort_the_stage() -> None:
    client = FakeClient(transport(), transport(), http(503), http(503), http(429), http(429))
    s, _ = session(client)
    result = s.extract(FOUR)
    assert [f.error for f in result.failures] == ["transport", "http_503", "http_429", "extractor_aborted"]
    assert len(client.calls) == 6  # chunk 4 is never sent
    assert s.aborted == {
        "reason": "consecutive_transient_failures",
        "at_chunk": {"doc_repo": "o/r", "doc_path": "README.md", "lines": [5, 6]},
    }


def test_after_an_abort_later_documents_are_not_sent_either() -> None:
    client = FakeClient(*[transport()] * 6)
    s, _ = session(client)
    s.extract(THREE)
    later = s.extract(ONE)
    assert [f.error for f in later.failures] == ["extractor_aborted"]
    assert len(client.calls) == 6


# ----------------------------------------------------------- Retry-After


def test_retry_after_within_the_cap_is_waited_in_full_even_beyond_the_socket_timeout() -> None:
    client = FakeClient(http(429, "90"), "[]")
    s, sleeps = session(client, retry_after_max_s=120)
    assert s.extract(ONE).failures == []
    assert sleeps == [90.0]


def test_retry_after_applies_to_503_too() -> None:
    s, sleeps = session(FakeClient(http(503, "7"), "[]"))
    s.extract(ONE)
    assert sleeps == [7.0]


def test_retry_after_is_ignored_on_other_statuses() -> None:
    s, sleeps = session(FakeClient(http(502, "60"), "[]"))
    s.extract(ONE)
    assert sleeps == [2.0]


def test_retry_after_over_the_cap_aborts_at_once_without_calling_the_next_chunk() -> None:
    client = FakeClient(http(429, "600"), by_chunk=None)
    s, sleeps = session(client, retry_after_max_s=120)
    result = s.extract(THREE)
    assert len(client.calls) == 1
    assert sleeps == []
    assert [f.error for f in result.failures] == ["http_429", "extractor_aborted", "extractor_aborted"]
    assert s.aborted == {
        "reason": "retry_after_exceeds_max",
        "retry_after_s": 600,
        "at_chunk": {"doc_repo": "o/r", "doc_path": "README.md", "lines": [1, 2]},
    }


def test_retry_after_over_the_cap_on_503_is_http_503() -> None:
    s, _ = session(FakeClient(http(503, "121")), retry_after_max_s=120)
    assert [f.error for f in s.extract(ONE).failures] == ["http_503"]
    assert s.aborted["reason"] == "retry_after_exceeds_max"


@pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5", "1.5", ""])
def test_http_date_and_unreadable_retry_after_wait_two_seconds(value: str) -> None:
    s, sleeps = session(FakeClient(http(429, value), "[]"))
    assert s.extract(ONE).failures == []
    assert sleeps == [2.0]


def test_parse_retry_after_accepts_non_negative_integer_seconds_only() -> None:
    assert parse_retry_after("0") == 0
    assert parse_retry_after(" 30 ") == 30
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("٣") is None  # a non-ASCII digit is not a number of seconds here


# ---------------------------------------------------- configuration errors


@pytest.mark.parametrize("status", [401, 403, 404])
def test_401_403_404_raise_a_configuration_error_without_retry(status: int) -> None:
    client = FakeClient(http(status))
    s, sleeps = session(client)
    with pytest.raises(ExtractorConfigError) as info:
        s.extract(ONE)
    assert info.value.reason == f"http_{status}"
    assert len(client.calls) == 1 and sleeps == []


# --------------------------------------------------------- strict (eval)


def test_strict_raises_on_a_single_transient_chunk_failure() -> None:
    client = FakeClient(http(429), http(429), "[]")
    s, _ = session(client, strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(THREE)
    assert info.value.reason == "http_429"
    assert len(client.calls) == 2


def test_strict_raises_on_an_unwaitable_retry_after() -> None:
    s, _ = session(FakeClient(http(429, "999")), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(ONE)
    assert info.value.reason == "retry_after_exceeds_max"


def test_strict_still_records_permanent_failures() -> None:
    s, _ = session(FakeClient(http(422), "x", "y", "[]"), strict=True)
    result = s.extract(THREE)
    assert [f.error for f in result.failures] == ["http_422", "schema"]


# ------------------------------------------------------- the HTTP client


def _lexora(handler) -> LexoraClient:
    return LexoraClient("http://lexora.test/", 5.0, transport=httpx.MockTransport(handler))


def test_lexora_client_posts_the_tier_as_model_to_chat_completions() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "Qwen", "choices": [{"message": {"content": "[]"}}]})

    completion = _lexora(handler).complete([{"role": "user", "content": "x"}], "light")
    assert seen["url"] == "http://lexora.test/v1/chat/completions"
    assert seen["body"]["model"] == "light"
    assert completion == Completion(text="[]", model="Qwen")


def test_lexora_client_reports_status_and_retry_after() -> None:
    client = _lexora(lambda r: httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(Exception) as info:
        client.complete([], "light")
    assert (info.value.status, info.value.retry_after) == (429, "30")


def test_lexora_client_reports_transport_errors() -> None:
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    from verimend.extractor import LLMTransportError

    with pytest.raises(LLMTransportError):
        _lexora(handler).complete([], "light")


def test_lexora_client_returns_no_text_for_a_non_completion_body() -> None:
    assert _lexora(lambda r: httpx.Response(200, json={"oops": 1})).complete([], "light").text is None


# ----------------------------------- abort detail shape and cause (msg-176 / msg-178)


def test_strict_transient_abort_detail_has_the_abort_shape_and_chains_the_cause() -> None:
    second = http(503)
    s, _ = session(FakeClient(http(503), second), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(ONE)
    detail = info.value.detail
    assert detail == {"reason": "http_503", "at_chunk": {"doc_repo": "o/r", "doc_path": "README.md", "lines": [1, 2]}}
    # explicit chaining: the session's Transient, itself raised from the client's error
    assert info.value.__cause__ is not None and info.value.__cause__.__cause__ is second
    assert "cause" not in detail
    json.dumps(detail)


def test_strict_permanent_streak_abort_has_the_same_keys_and_chains_the_third_failure() -> None:
    third = http(400)
    s, _ = session(FakeClient(http(400), http(400), third), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        s.extract(FOUR)
    detail = info.value.detail
    assert set(detail) == {"reason", "at_chunk", "status"}
    assert set(detail["at_chunk"]) == {"doc_repo", "doc_path", "lines"} and detail["at_chunk"]["lines"] == [5, 6]
    assert info.value.__cause__.__cause__ is third
    assert "cause" not in detail
    json.dumps(detail)


def test_an_abort_without_a_cause_keeps_the_implicit_context() -> None:
    s, _ = session(FakeClient(), strict=True)
    marker = RuntimeError("outer")
    with pytest.raises(ExtractorAborted) as info:
        try:
            raise marker
        except RuntimeError:
            s._abort("consecutive_transient", {"doc_repo": "o/r", "doc_path": "d.md", "lines": [1, 2]})
    assert info.value.__cause__ is None and info.value.__context__ is marker
    assert info.value.__suppress_context__ is False


def test_a_non_strict_abort_logs_the_cause_traceback(caplog) -> None:
    third = http(400)
    s, _ = session(FakeClient(http(400), http(400), third))
    with caplog.at_level("ERROR"):
        s.extract(FOUR)
    (record,) = [r for r in caplog.records if r.getMessage().startswith("extraction aborted")]
    assert record.exc_info is not None and record.exc_info[1].__cause__ is third
    assert "cause" not in s.aborted
