from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _tz(name):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def to_local(dt, tz_name):
    """Naive-UTC datetime -> aware datetime in the group's timezone."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_tz(tz_name))


def local_today(tz_name) -> date:
    return datetime.now(timezone.utc).astimezone(_tz(tz_name)).date()


def resolve_range(args, tz_name):
    """Dashboard filter: today | week | month | year | custom | all -> (start, end, key)."""
    today = local_today(tz_name)
    key = (args.get("range") or "month").lower()
    if key == "today":
        return today, today, key
    if key == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6), key
    if key == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31), key
    if key == "all":
        return None, None, key
    if key == "custom":
        try:
            s = date.fromisoformat(args.get("start", ""))
            e = date.fromisoformat(args.get("end", ""))
            if s <= e:
                return s, e, key
        except ValueError:
            pass
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return date(today.year, today.month, 1), nxt - timedelta(days=1), "month"


def parse_date(value, default=None):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return default
