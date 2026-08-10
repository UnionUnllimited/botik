from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_MSK = ZoneInfo("Europe/Moscow")


def subscription_end_to_utc_iso(value: str | datetime | None) -> str | None:

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MSK)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
