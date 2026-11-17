"""Shared validation and resolution of scan refresh cutoffs."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

from flatlas.errors import FlatlasError

_DURATION = re.compile(r"^(?P<amount>[1-9]\d*)(?P<unit>[smhdw])$")
_UNIT_NS = {unit: seconds * 1_000_000_000 for unit, seconds in (
    ("s", 1), ("m", 60), ("h", 3600), ("d", 86400), ("w", 604800),
)}


def parse_refresh_before(value: str, *, current_ns: int | None = None) -> int:
    text = value.strip()
    now = time.time_ns() if current_ns is None else current_ns
    duration = _DURATION.fullmatch(text)
    if duration is not None:
        resolved = now - int(duration["amount"]) * _UNIT_NS[duration["unit"]]
    else:
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError as exc:
            raise FlatlasError("expected an ISO 8601 datetime or a duration such as 7d, 12h, or 30m") from exc
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        resolved = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    if resolved > now:
        raise FlatlasError("refresh time must not be in the future")
    if not -(2**63) <= resolved < 2**63:
        raise FlatlasError("refresh time is outside the supported SQLite timestamp range")
    return resolved
