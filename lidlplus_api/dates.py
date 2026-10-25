"""Inclusive receipt calendar-date ranges, shared by downloads and conversion."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta


def parse_date(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise ValueError("Dates must use YYYY-MM-DD (or datetime.date in Python).")


def receipt_date(receipt):
    value = receipt.get("date")
    try:
        if not isinstance(value, str):
            raise ValueError
        return datetime.fromisoformat(value).date()
    except ValueError:
        raise ValueError(
            f"Receipt {receipt.get('id', '(unknown)')} has a missing or invalid date."
        ) from None


@dataclass(frozen=True)
class DateRange:
    since: date | None = None
    until: date | None = None

    def matches(self, receipt):
        if self.since is None and self.until is None:
            return True
        purchased = receipt_date(receipt)
        # Preserve the calendar date supplied by Lidl, including for offset timestamps.
        return (self.since is None or purchased >= self.since) and (
            self.until is None or purchased <= self.until
        )


def resolve_date_range(*, period=None, since=None, until=None, today=None):
    """Resolve a preset or explicit inclusive endpoints; no inputs means all dates."""
    if period is not None:
        if since is not None or until is not None:
            raise ValueError("Use either period or since/until, not both.")
        today = today if today is not None else datetime.now().astimezone().date()
        if period == "this-month":
            since = today.replace(day=1)
            until = today.replace(day=monthrange(today.year, today.month)[1])
        elif period == "last-month":
            until = today.replace(day=1) - timedelta(days=1)
            since = until.replace(day=1)
        elif period == "this-year":
            since = date(today.year, 1, 1)
            until = date(today.year, 12, 31)
        else:
            raise ValueError("Period must be this-month, last-month, or this-year.")
    since = parse_date(since) if since is not None else None
    until = parse_date(until) if until is not None else None
    if since is not None and until is not None and since > until:
        raise ValueError("since must be on or before until.")
    return DateRange(since, until)
