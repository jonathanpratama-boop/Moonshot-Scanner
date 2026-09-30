"""Clock, identifiers, canonical hashing and timestamp handling.

Every operational timestamp is assigned here by software from the system clock.
Publication times supplied by sources are parsed separately and never substituted
for observation or detection times.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class AstraError(Exception):
    """Base error for expected, reportable failures."""


class ValidationError(AstraError):
    pass


class _SystemClock:
    source = "system_utc"

    def now(self) -> datetime:
        return datetime.now(UTC)


# Tests replace CLOCK with a controllable clock. Production code must call now().
CLOCK: Any = _SystemClock()


def now() -> datetime:
    return CLOCK.now().replace(microsecond=0)


def now_str() -> str:
    return fmt_utc(now())


def clock_source() -> str:
    return getattr(CLOCK, "source", "system_utc")


def fmt_utc(dt: datetime | None) -> str | None:
    """Canonical storage form: whole seconds, floor, 'Z' suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValidationError("refusing to format naive datetime as UTC")
    return dt.astimezone(UTC).replace(microsecond=0).strftime(TS_FORMAT)


def parse_utc(s: str | None) -> datetime | None:
    """Parse a canonical stored UTC string."""
    if s is None:
        return None
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=UTC)


_OFFSET_RE = re.compile(r"([+-]\d{2}:?\d{2}|Z)$")


def parse_source_time(raw: str, declared_tz: str | None = None) -> tuple[datetime, str]:
    """Parse a source-supplied timestamp.

    Returns (utc datetime, timezone label preserved from the source).
    A string with an explicit offset/Z keeps that offset as its label.
    A naive string requires ``declared_tz`` (an IANA zone); otherwise it is rejected
    rather than guessed.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("empty timestamp")
    text = raw.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValidationError(f"unparseable timestamp {raw!r}") from exc
    if dt.tzinfo is not None:
        m = _OFFSET_RE.search(text)
        label = "UTC" if text.endswith("Z") else (m.group(1) if m else str(dt.tzinfo))
        if declared_tz:
            label = f"{label} (declared {declared_tz})"
        return dt.astimezone(UTC), label
    if not declared_tz:
        raise ValidationError(f"timestamp {raw!r} has no offset and no declared time zone")
    zone = get_zone(declared_tz)
    return dt.replace(tzinfo=zone).astimezone(UTC), declared_tz


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"unknown IANA time zone {name!r}") from exc


def to_zone_str(utc_s: str | None, zone: str) -> str:
    if not utc_s:
        return "UNKNOWN"
    dt = parse_utc(utc_s).astimezone(get_zone(zone))
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return fmt_utc(o)
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)!r}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_duration(spec: str) -> timedelta:
    """Minimal ISO-8601 duration parser (PnDTnHnMnS) used for relative recheck/expiry."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", spec or "")
    if not m or spec in ("P", "PT"):
        raise ValidationError(f"unsupported duration {spec!r}; use e.g. P2D, PT4H, PT0S")
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=s)
