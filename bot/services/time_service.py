from __future__ import annotations

import re
import datetime as dt
from typing import Optional, Union

# simple tz helpers based on fixed hour offsets

def _parse_offset(value: Union[int, float, str, None]) -> int:
    """
    parse tz value into hour offset
    accepts:
      - int/float: 3, -11
      - str: '6', '+3', '-11', 'UTC+2', 'GMT-4'
    returns int hours, defaults to 0 on failure
    """
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return 0

    if isinstance(value, str):
        s = value.strip()
        # plain int in string
        try:
            return int(s)
        except Exception:
            pass
        # extract first signed integer
        m = re.search(r"([+-]?\d{1,2})", s)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return 0

    return 0


def tzinfo_from(value: Union[int, float, str, None]) -> dt.tzinfo:
    """
    return a fixed-offset tzinfo from given value
    """
    hours = _parse_offset(value)
    return dt.timezone(dt.timedelta(hours=hours))


def now_in_tz(value: Union[int, float, str, None]) -> dt.datetime:
    """
    current time in given tz (aware dt)
    """
    return dt.datetime.now(dt.timezone.utc).astimezone(tzinfo_from(value))


def today_in_tz(value: Union[int, float, str, None]) -> dt.date:
    """
    current date in given tz
    """
    return now_in_tz(value).date()


def to_tz(dt_obj: dt.datetime, value: Union[int, float, str, None]) -> dt.datetime:
    """
    convert dt_obj to given tz. if dt naive, assume utc.
    """
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
    return dt_obj.astimezone(tzinfo_from(value))


# birthday helpers


def _safe_date(year: int, month: int, day: int) -> dt.date:
    """
    build date with fallback for 29 feb on non-leap years -> 28 feb
    """
    try:
        return dt.date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return dt.date(year, 2, 28)
        raise


def next_birthday_date(
    day: int,
    month: int,
    base: Optional[Union[dt.date, dt.datetime, int, str]] = None,
    *,
    tz: Optional[Union[int, str]] = None,
    include_today: bool = True,
) -> dt.date:
    """
    compute the next occurrence of (day, month) relative to base date
    - base: date/datetime to compare with. if None -> today (optionally in tz).
    - backward compat: if base is int/str and tz is None, treat base as tz offset.
    - tz: fixed offset (int/str) when base is None; ignored if base is date/datetime.
    - include_today: if true and today matches, return today; otherwise return next year.
    returns a calendar date (naive)
    """
    # backward compat: allow next_birthday_date(d, m, tz_int)
    if isinstance(base, (int, str)) and tz is None:
        tz = base
        base = None

    if base is None:
        today = today_in_tz(tz)
    else:
        if isinstance(base, dt.datetime):
            today = base.date()
        else:
            today = base

    year = today.year
    candidate = _safe_date(year, month, day)

    if include_today:
        if candidate < today:
            candidate = _safe_date(year + 1, month, day)
    else:
        if candidate <= today:
            candidate = _safe_date(year + 1, month, day)

    return candidate


# day boundary helpers


def local_midnight(
    when: Optional[Union[dt.date, dt.datetime, int, str]] = None,
    tz: Optional[Union[int, str]] = None,
) -> dt.datetime:
    """
    return tz-aware midnight (00:00) for the given date in the given fixed tz.
    - when: date/datetime to use; if None -> today in tz
    - backward compat: if when is int/str and tz is None, treat 'when' as tz offset
    - tz: int/str hour offset like 3, -5, 'UTC+2', 'GMT-4'
    result has tzinfo set to the fixed offset
    """
    # backward compat: local_midnight(tz_int)
    if isinstance(when, (int, str)) and tz is None:
        tz = when
        when = None

    if when is None:
        d = today_in_tz(tz)
    else:
        if isinstance(when, dt.datetime):
            d = to_tz(when, tz).date() if tz is not None else when.date()
        else:
            d = when

    tzinfo = tzinfo_from(tz)
    return dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tzinfo)
