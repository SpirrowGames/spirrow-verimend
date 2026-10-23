"""The LLM client the extractor talks to: Lexora's OpenAI-compatible API (docs/design.md 6).

``LLMClient`` is the port. ``LexoraClient`` is the one adapter, posting to
``<lexora_url>/v1/chat/completions`` with the *tier* name as the model (Lexora
maps tiers to models; Verimend never names a model). Tests and the evaluation
replay use fakes, so CI never reaches Lexora.

The client does one request and reports what happened; it does not retry.
Retrying is a policy decision (which failures, how long to wait) and lives in
``verimend.extractor.extract``. So every failure is raised as one of two
exceptions carrying just enough to classify it:

``LLMHTTPError``       the server answered with a non-2xx status. Carries the
                       status and the raw ``Retry-After`` header, if any.
``LLMTransportError``  no answer: timeout, refused or reset connection.

A 2xx whose body is not a chat completion is not an exception here: ``text``
comes back None and the extractor treats it as an output it cannot read, the
same as malformed JSON from the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class Completion:
    text: str | None
    model: str | None = None


class LLMHTTPError(Exception):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class LLMTransportError(Exception):
    pass


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], tier: str) -> Completion: ...


class LexoraClient:
    """``LLMClient`` over httpx. ``timeout_s`` is the httpx timeout and nothing else."""

    def __init__(self, url: str, timeout_s: float, transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = url.rstrip("/") + "/v1/chat/completions"
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def complete(self, messages: list[dict[str, str]], tier: str) -> Completion:
        try:
            response = self._http.post(self.endpoint, json={"model": tier, "messages": messages, "temperature": 0})
        except httpx.TransportError as exc:  # timeouts, connect / read errors
            raise LLMTransportError(f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise LLMHTTPError(response.status_code, response.headers.get("retry-after"))
        return _completion(response)

    def close(self) -> None:
        self._http.close()


def _completion(response: httpx.Response) -> Completion:
    try:
        body: Any = response.json()
        text = body["choices"][0]["message"]["content"]
        model = body.get("model")
    except (ValueError, KeyError, IndexError, TypeError):
        return Completion(text=None)
    return Completion(
        text=text if isinstance(text, str) else None,
        model=model if isinstance(model, str) else None,
    )
