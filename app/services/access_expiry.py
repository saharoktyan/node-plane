"""Bounded access expiry parser supporting YYYY-MM-DD dates and duration tokens."""
from datetime import datetime, timezone
import re


UNIT_SECONDS = {
    'h': 3600,
    'd': 86400,
    'm': 30 * 86400,
    'y': 365 * 86400,
}


def parse_expiry(raw: str, now: datetime | None = None) -> str:
    """Parse custom expiry input into UTC ISO-8601 string.

    Accepts:
    1. Full date: YYYY-MM-DD (ends at 23:59:59 UTC). Today or future dates allowed; past dates rejected.
    2. Combined durations: e.g. '12h', '7d', '3d5m1y', case-insensitive, arbitrary token order.
       H = hours, D = days, M = 30 days, Y = 365 days.
       Duplicate units, zero/negative, malformed, or overflow inputs are rejected.
    """
    if not isinstance(raw, str):
        raise ValueError("Expiry input must be a string")
    text = raw.strip()
    if not text or not text.isascii():
        raise ValueError("Invalid expiry format")

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    # Check for YYYY-MM-DD format
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            parsed_date = datetime.strptime(text, '%Y-%m-%d').date()
        except ValueError:
            raise ValueError("Invalid calendar date")
        if parsed_date < now.date():
            raise ValueError("Expiry date cannot be in the past")
        return f"{parsed_date.year:04d}-{parsed_date.month:02d}-{parsed_date.day:02d}T23:59:59+00:00"

    # Parse combined duration tokens
    # e.g. 3d5m1y or 12h
    token_matches = list(re.finditer(r'(\d+)([a-zA-Z]+)', text))
    if not token_matches:
        raise ValueError("Invalid duration format")

    # Ensure the entire string is accounted for by the matched tokens
    reconstructed = "".join(m.group(0) for m in token_matches)
    if reconstructed.lower() != text.lower():
        raise ValueError("Invalid characters in duration")

    seen_units = set()
    total_seconds = 0

    for match in token_matches:
        amount_str, unit_str = match.groups()
        unit = unit_str.lower()
        if unit not in UNIT_SECONDS:
            raise ValueError(f"Unknown duration unit: {unit_str}")
        if unit in seen_units:
            raise ValueError(f"Duplicate duration unit: {unit_str}")
        seen_units.add(unit)

        amount = int(amount_str)
        if amount <= 0:
            raise ValueError("Duration component must be positive")

        seconds_for_unit = amount * UNIT_SECONDS[unit]
        total_seconds += seconds_for_unit

    if total_seconds <= 0:
        raise ValueError("Total duration must be positive")

    # Positive checked arithmetic
    current_timestamp = int(now.timestamp())
    target_timestamp = current_timestamp + total_seconds
    try:
        target_dt = datetime.fromtimestamp(target_timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("Duration overflow")

    if target_dt.year > 9999:
        raise ValueError("Expiry year exceeds 9999")

    return target_dt.isoformat()
