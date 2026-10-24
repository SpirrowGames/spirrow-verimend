"""How a Lexora failure is classified and retried, for every LLM stage (docs/design.md 5.2).

The extractor (per chunk) and the reconciler's LLM judge (per claim) share
this one policy, so the table in design 5.2 cannot drift between them:

==================  ===============================  ==================  ==========================
class               which failures                   retry               raised as
==================  ===============================  ==================  ==========================
configuration       HTTP 401, 403, 404               none                ``ConfigStatus``
transient           timeout, connection error, 5xx,  once, after         ``Transient``
                    429, 408                         ``Retry-After``
                                                     (integer seconds,
                                                     429/503 only) or
                                                     2 s
transient, too      429 / 503 with ``Retry-After``   none                ``Unwaitable``
long to wait        over ``retry_after_max_s``
permanent           any other 4xx                    none                ``Permanent``
==================  ===============================  ==================  ==========================

404 is configuration, not permanent: an OpenAI-compatible
``/v1/chat/completions`` answers 404 when the endpoint or the model (the
tier) does not exist, which no request body can fix.

What a stage does with each class -- fail the unit, count it, abort -- is the
stage's own business; ``Streaks`` holds the two counters both stages abort on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported lazily below: verimend.extractor imports this module
    from verimend.extractor.client import Completion, LLMClient

CONSECUTIVE_TRANSIENT_LIMIT = 3
PERMANENT_HTTP_STREAK_LIMIT = 3
DEFAULT_WAIT_S = 2.0

ERROR_TRANSPORT = "transport"

ABORT_CONSECUTIVE = "consecutive_transient_failures"
ABORT_RETRY_AFTER = "retry_after_exceeds_max"
ABORT_PERMANENT_HTTP = "permanent_http_streak"

_TRANSIENT_STATUSES = frozenset({408, 429})
_RETRY_AFTER_STATUSES = frozenset({429, 503})
CONFIG_STATUSES = frozenset({401, 403, 404})


class ConfigStatus(Exception):
    """Lexora rejected the configuration (credentials, endpoint, tier). Never retried."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http_{status}")
        self.status = status
        self.error = f"http_{status}"


class Transient(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


class Unwaitable(Exception):
    def __init__(self, error: str, retry_after_s: int) -> None:
        super().__init__(error)
        self.error = error
        self.retry_after_s = retry_after_s


class Permanent(Exception):
    """A failure of this one request. ``status`` is set for an HTTP 4xx, None otherwise (``schema`` ...)."""

    def __init__(self, error: str, status: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.status = status


@dataclass
class Streaks:
    """The two cross-unit counters a stage aborts on.

    ``transient``: units (chunks / claims) that failed transiently in a row;
    the stage resets it when a unit succeeds.

    ``permanent_http``: permanent HTTP 4xx failures in a row. Reset only by a
    Lexora call that succeeded (any 2xx, whatever its body held) -- a
    transient failure in between does not reset it, and failures Lexora did
    not answer with a 4xx (``schema``, ``oversized_line``) are not counted.
    Three 4xx in a row with a correct setup mean the request building or the
    configuration (an unknown tier answered with 400, say) is broken, and
    every further request would fail the same way.
    """

    transient: int = 0
    permanent_http: int = 0


def request(
    client: "LLMClient",
    messages: list[dict[str, str]],
    tier: str,
    *,
    retry_after_max_s: float,
    sleep: Callable[[float], None],
    streaks: Streaks,
) -> "Completion":
    """One logical request: a transient failure is retried once, then raised as ``Transient``.

    Raises ``ConfigStatus`` / ``Transient`` / ``Unwaitable`` / ``Permanent``
    (with ``status``). A success resets ``streaks.permanent_http``.
    """
    from verimend.extractor.client import LLMHTTPError, LLMTransportError

    for attempt in (1, 2):
        try:
            completion = client.complete(messages, tier)
        except LLMTransportError as exc:
            last: Exception = exc
            error, wait = ERROR_TRANSPORT, DEFAULT_WAIT_S
        except LLMHTTPError as exc:
            last = exc
            error = f"http_{exc.status}"
            if exc.status in CONFIG_STATUSES:
                raise ConfigStatus(exc.status) from exc
            if not (exc.status in _TRANSIENT_STATUSES or exc.status >= 500):
                if 400 <= exc.status < 500:
                    raise Permanent(error, exc.status) from exc
                raise Transient(error) from exc  # 1xx/3xx surfacing as an error: not ours to judge
            wait = DEFAULT_WAIT_S
            if exc.status in _RETRY_AFTER_STATUSES:
                seconds = parse_retry_after(exc.retry_after)
                if seconds is not None:
                    if seconds > retry_after_max_s:
                        raise Unwaitable(error, seconds) from exc
                    wait = float(seconds)
        else:
            streaks.permanent_http = 0
            return completion
        if attempt == 2:
            raise Transient(error) from last  # the second attempt's client error, as with the other raises
        sleep(wait)
    raise AssertionError("unreachable")


def count_permanent(streaks: Streaks, exc: Permanent) -> bool:
    """Count ``exc`` if it is an HTTP 4xx. True when the streak reached the abort limit."""
    if exc.status is None:
        return False
    streaks.permanent_http += 1
    return streaks.permanent_http >= PERMANENT_HTTP_STREAK_LIMIT


def parse_retry_after(value: str | None) -> int | None:
    """Integer seconds (>= 0) or None. HTTP-date and anything else is None (wait the default)."""
    if value is None:
        return None
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() else None
