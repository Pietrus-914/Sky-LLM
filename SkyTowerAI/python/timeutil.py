"""
Time helper for the whole codebase.

The system compares NAIVE UTC datetimes everywhere (server-side utcnow vs
event datetimes with tzinfo stripped). Python 3.12+ deprecates
datetime.utcnow(); this helper keeps the exact same naive-UTC semantics
without the DeprecationWarning spam. Do NOT switch callers to aware
datetime.now(timezone.utc) piecemeal — mixing aware and naive raises
TypeError on comparison.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC now — drop-in replacement for datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    """Any datetime -> naive UTC (the codebase convention). Aware values are
    CONVERTED (astimezone) before the tzinfo strip — a bare replace() would
    silently keep non-UTC wall-clock time. Naive values pass through."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utc_epoch(dt: datetime) -> int:
    """Epoch seconds of a datetime under naive-UTC semantics.
    (datetime.timestamp() on a naive value would wrongly apply the machine's
    LOCAL timezone.)"""
    return int(to_naive_utc(dt).replace(tzinfo=timezone.utc).timestamp())
