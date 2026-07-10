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
