"""UTC helpers for API timestamps.

Application timestamps are written with SYSUTCDATETIME(), so SQL Server hands
them back as naive datetimes whose wall clock is UTC. Serializing those as-is
lets clients parse UTC as device-local time (a 5-hour shift for IST users), so
every timestamp leaving the API carries an explicit UTC designator.
"""

from datetime import datetime, timezone


def iso_utc(value: datetime | None) -> str | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
