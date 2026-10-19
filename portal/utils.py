"""Small helpers for cleaning input and reading query-string values."""
from datetime import datetime


def clean(value):
    return (value or "").strip()


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def like_pattern(text):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def page_number(args):
    return to_int(args.get("page")) or 1
