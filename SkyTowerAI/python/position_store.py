"""Durable active-position snapshot.

MT5 remains the source of truth for whether a position is live. This store
keeps the server-only metadata needed to reconcile that broker position after
a process restart. Writes are atomic so a watchdog or power loss cannot leave
a partially-written JSON document that looks valid.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional


class PositionStoreError(RuntimeError):
    """The active-position snapshot exists but cannot be trusted."""


class PositionStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Optional[Dict]:
        if not self.path or not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            raise PositionStoreError(
                f"Cannot read active-position snapshot {self.path}: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise PositionStoreError("Active-position snapshot is not an object")
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise PositionStoreError(
                "Unsupported active-position snapshot schema "
                f"{payload.get('schema_version')!r}"
            )
        position = payload.get("position")
        if not isinstance(position, dict):
            raise PositionStoreError("Active-position snapshot has no position object")
        return payload

    def save(self, position: Dict, recovery_state: str = "open",
             close_record: Optional[Dict] = None) -> None:
        if not self.path:
            return
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "recovery_state": recovery_state,
            "position": position,
        }
        if close_record is not None:
            payload["close_record"] = close_record
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temp_path = f"{self.path}.tmp"

        with self._lock:
            try:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
            except Exception as exc:
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass
                raise PositionStoreError(
                    f"Cannot save active-position snapshot {self.path}: {exc}"
                ) from exc

    def clear(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise PositionStoreError(
                    f"Cannot clear active-position snapshot {self.path}: {exc}"
                ) from exc
