"""A scripted LLMClient for the extractor tests. CI never reaches Lexora."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from verimend.extractor import Completion, ExtractionSession, LLMHTTPError, LLMTransportError

Reply = str | Exception | Completion


def claims(*items: dict[str, Any]) -> str:
    return json.dumps(list(items))


def port_claim(line: int, port: int, polarity: str = "current", **extra: Any) -> dict[str, Any]:
    item = {
        "anchor_start": line,
        "anchor_end": line,
        "text": f"listens on {port}",
        "claim_type": "port",
        "subject": {"polarity": polarity, "port": port, "service": None},
    }
    item.update(extra)
    return item


def http(status: int, retry_after: str | None = None) -> LLMHTTPError:
    return LLMHTTPError(status, retry_after)


def transport() -> LLMTransportError:
    return LLMTransportError("ConnectTimeout")


class FakeClient:
    """Answers from ``script`` in order, or from ``by_chunk(first_line) -> reply`` when given."""

    def __init__(self, *script: Reply, by_chunk: Callable[[int], Reply] | None = None) -> None:
        self.script = list(script)
        self.by_chunk = by_chunk
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], tier: str) -> Completion:
        self.calls.append(messages)
        if self.by_chunk is not None:
            reply = self.by_chunk(chunk_start(messages))
        else:
            assert self.script, "the fake LLM was called more often than scripted"
            reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Completion):
            return reply
        return Completion(text=reply, model="fake-model")

    def chunk_starts(self) -> list[int]:
        return [chunk_start(m) for m in self.calls]


def chunk_start(messages: list[dict[str, str]]) -> int:
    return int(re.search(r"lines (\d+)-\d+:", messages[1]["content"]).group(1))


def session(client: Any, **kwargs: Any) -> tuple[ExtractionSession, list[float]]:
    """A session whose sleep records instead of sleeping."""
    sleeps: list[float] = []
    kwargs.setdefault("prompt", "test prompt")
    return ExtractionSession(client, sleep=sleeps.append, **kwargs), sleeps
