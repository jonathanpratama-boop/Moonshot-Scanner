"""US equity exchange session calendar (NYSE/Nasdaq regular trading hours).

Rule-generated holidays and early closes for a bounded range. Outside that range the
calendar raises CalendarUnavailable so dependent calculations become UNAVAILABLE
instead of guessing.

Sources and verification (see docs/CALENDAR.md):
- Holiday rules: NYSE Rule 7.2 holiday practice (weekend observance: Saturday -> Friday,
  Sunday -> Monday; New Year's Day on Saturday is not observed).
- 2026 and 2027 full-day holidays and the unambiguous early closes were cross-checked
  against https://www.nyse.com/markets/hours-calendars retrieved 2026-09-30.
- Special closures: 2025-01-09 (National Day of Mourning, President Carter).
- Known conflict: the retrieved NYSE footnote text for Independence Day 2026 read
  "Monday, July 3, 2026", but 3 July 2026 is a Friday and a full holiday. This calendar
  does not mark 2 July 2026 as an early close. Recorded as DATA_CONFLICT for review.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from .core import UTC, AstraError, get_zone

EXCHANGE_TZ = "America/New_York"
CALENDAR_ID = "XNYS-RTH-RULES-2019-2027-v1"
VALID_FROM = date(2019, 1, 1)
VALID_TO = date(2027, 12, 31)
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PRE_OPEN = time(4, 0)
POST_CLOSE = time(20, 0)
SPECIAL_CLOSURES = {date(2025, 1, 9): "National Day of Mourning (President Carter)"}
KNOWN_CONFLICTS = [
    {
        "date": "2026-07-02",
        "note": "NYSE page footnote retrieved 2026-09-30 read 'Monday, July 3, 2026' for the "
        "Independence Day early close; 3 July 2026 is a Friday holiday. Not marked as an early close.",
    }
]


class CalendarUnavailable(AstraError):
    pass


@dataclass(frozen=True)
class Session:
    session_date: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool

    @property
    def minutes(self) -> int:
        return int((self.close_utc - self.open_utc).total_seconds() // 60)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        out[ny + timedelta(days=1)] = "New Year's Day (observed)"
    elif ny.weekday() != 5:  # Saturday New Year's Day is not observed on the prior Friday
        out[ny] = "New Year's Day"
    out[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    out[_easter(year) - timedelta(days=2)] = "Good Friday"
    out[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        out[_observed(date(year, 6, 19))] = "Juneteenth"
    out[_observed(date(year, 7, 4))] = "Independence Day"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    out[_observed(date(year, 12, 25))] = "Christmas Day"
    for d, name in SPECIAL_CLOSURES.items():
        if d.year == year:
            out[d] = name
    return out


@lru_cache(maxsize=None)
def early_closes(year: int) -> dict[date, str]:
    hol = holidays(year)
    out: dict[date, str] = {}
    j3 = date(year, 7, 3)
    if j3.weekday() < 4 and j3 not in hol:
        out[j3] = "Day before Independence Day"
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = "Day after Thanksgiving"
    c24 = date(year, 12, 24)
    if c24.weekday() < 4 and c24 not in hol:
        out[c24] = "Christmas Eve"
    return out


def _check_range(d: date) -> None:
    if d < VALID_FROM or d > VALID_TO:
        raise CalendarUnavailable(
            f"{d.isoformat()} outside calendar {CALENDAR_ID} range {VALID_FROM}..{VALID_TO}"
        )


def is_session(d: date) -> bool:
    _check_range(d)
    return d.weekday() < 5 and d not in holidays(d.year)


def session(d: date) -> Session | None:
    if not is_session(d):
        return None
    tz = get_zone(EXCHANGE_TZ)
    early = d in early_closes(d.year)
    close_t = EARLY_CLOSE if early else RTH_CLOSE
    return Session(
        session_date=d,
        open_utc=datetime.combine(d, RTH_OPEN, tzinfo=tz).astimezone(UTC),
        close_utc=datetime.combine(d, close_t, tzinfo=tz).astimezone(UTC),
        early_close=early,
    )


def next_session(d: date) -> date:
    cur = d + timedelta(days=1)
    while not is_session(cur):
        cur += timedelta(days=1)
    return cur


def previous_session(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_session(cur):
        cur -= timedelta(days=1)
    return cur


def add_sessions(d: date, n: int) -> date:
    """The n-th session after session date d (n >= 1)."""
    cur = d
    for _ in range(n):
        cur = next_session(cur)
    return cur


def sessions_between(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if is_session(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def local_date(ts_utc: datetime) -> date:
    return ts_utc.astimezone(get_zone(EXCHANGE_TZ)).date()


def latest_completed_session(cutoff_utc: datetime) -> date:
    """Most recent session whose regular close is at or before the cutoff."""
    d = local_date(cutoff_utc)
    s = session(d)
    if s and s.close_utc <= cutoff_utc:
        return d
    return previous_session(d)


def session_context(ts_utc: datetime) -> str:
    """PRE_MARKET / RTH / POST_MARKET / CLOSED relative to the exchange day of ts."""
    d = local_date(ts_utc)
    s = session(d)
    if s is None:
        return "CLOSED"
    tz = get_zone(EXCHANGE_TZ)
    pre = datetime.combine(d, PRE_OPEN, tzinfo=tz).astimezone(UTC)
    post = datetime.combine(d, POST_CLOSE, tzinfo=tz).astimezone(UTC)
    if s.open_utc <= ts_utc < s.close_utc:
        return "RTH"
    if pre <= ts_utc < s.open_utc:
        return "PRE_MARKET"
    if s.close_utc <= ts_utc < post:
        return "POST_MARKET"
    return "CLOSED"


def rth_slots(d: date, interval_seconds: int = 300) -> list[str]:
    """Exchange-local HH:MM start labels of every regular-session bar on session d."""
    s = session(d)
    if s is None:
        return []
    tz = get_zone(EXCHANGE_TZ)
    out, cur = [], s.open_utc
    while cur < s.close_utc:
        out.append(cur.astimezone(tz).strftime("%H:%M"))
        cur += timedelta(seconds=interval_seconds)
    return out


def classify_bar(start_utc: datetime, interval_seconds: int) -> tuple[str, str, str]:
    """Return (session_date ISO, session_class, clock_slot) for a bar start.

    RTH requires the whole bar to lie inside the regular session.
    """
    tz = get_zone(EXCHANGE_TZ)
    local = start_utc.astimezone(tz)
    d = local.date()
    slot = local.strftime("%H:%M")
    s = session(d)
    if s is None:
        return d.isoformat(), "OFF", slot
    end = start_utc + timedelta(seconds=interval_seconds)
    if s.open_utc <= start_utc and end <= s.close_utc:
        return d.isoformat(), "RTH", slot
    ctx = session_context(start_utc)
    return d.isoformat(), {"PRE_MARKET": "PRE", "POST_MARKET": "POST"}.get(ctx, "OFF"), slot


def describe() -> dict:
    return {
        "calendar_id": CALENDAR_ID,
        "exchange_timezone": EXCHANGE_TZ,
        "valid_from": VALID_FROM.isoformat(),
        "valid_to": VALID_TO.isoformat(),
        "regular_session": "09:30-16:00 America/New_York; early close 13:00",
        "extended_hours": "PRE 04:00-09:30 and POST 16:00-20:00 are labelled but never used by RTH detectors or outcomes",
        "special_closures": {d.isoformat(): n for d, n in SPECIAL_CLOSURES.items()},
        "known_conflicts": KNOWN_CONFLICTS,
    }
