"""Bounded in-memory log of read-only Groww historical market-data responses.

Diagnostic only: it records what the historical endpoints returned so the data shape can
be inspected. Credentials, tokens, headers, account data and order data are never logged,
and the log is never persisted.
"""

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

LOG_CAPACITY = 100
_HEAD_ITEMS = 25
_TAIL_ITEMS = 10
_MAX_SYMBOLS = 600
_MAX_DEPTH = 6
_MAX_STRING = 300
_MAX_KEYS = 40
_SENSITIVE_KEYS = ("token", "secret", "key", "authorization", "checksum", "totp", "password")
LOGGED_PATH_PREFIX = "/v1/historical/"


def sanitize(value: Any, depth: int = 0) -> Any:
    """JSON-safe, size-bounded copy of a response body with sensitive-looking keys redacted."""
    if depth > _MAX_DEPTH:
        return "[max depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_KEYS:
                out["_omitted_keys"] = len(value) - _MAX_KEYS
                break
            name = str(key)
            out[name] = "[redacted]" if any(part in name.lower() for part in _SENSITIVE_KEYS) else sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = list(value)
        # Symbol lists (contracts/expiries) are short strings; keep them whole so a missing symbol is visible.
        if len(items) <= _MAX_SYMBOLS and all(isinstance(item, str) and len(item) <= 80 for item in items):
            return list(items)
        if len(items) <= _HEAD_ITEMS + _TAIL_ITEMS:
            return [sanitize(item, depth + 1) for item in items]
        omitted = len(items) - _HEAD_ITEMS - _TAIL_ITEMS
        return (
            [sanitize(item, depth + 1) for item in items[:_HEAD_ITEMS]]
            + [f"... {omitted} item(s) omitted (total {len(items)}) ..."]
            + [sanitize(item, depth + 1) for item in items[-_TAIL_ITEMS:]]
        )
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"


def describe(value: Any) -> dict[str, Any]:
    """Structural facts about the payload: JSON types of the top level and of list elements."""
    info: dict[str, Any] = {"type": type(value).__name__}
    if isinstance(value, dict):
        payload = value.get("payload")
        info["keys"] = sorted(str(key) for key in value)[:_MAX_KEYS]
        if isinstance(payload, dict):
            info["payload_keys"] = sorted(str(key) for key in payload)[:_MAX_KEYS]
            for name, item in payload.items():
                if isinstance(item, list):
                    kinds = sorted({type(element).__name__ for element in item})
                    info.setdefault("lists", {})[str(name)] = {"count": len(item), "element_types": kinds}
    return info


class ResponseLog:
    def __init__(self, capacity: int = LOG_CAPACITY):
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 1
        self.capacity = capacity

    def record(self, path: str, query: dict[str, str], http_status: int, body: Any) -> None:
        if not path.startswith(LOGGED_PATH_PREFIX):
            return
        with self._lock:
            entry = {
                "id": self._next_id,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "path": path,
                "query": {str(k): str(v) for k, v in query.items()},
                "http_status": http_status,
                "structure": describe(body),
                "body": sanitize(body),
            }
            self._next_id += 1
            self._entries.append(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._entries))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


HISTORICAL_RESPONSE_LOG = ResponseLog()
