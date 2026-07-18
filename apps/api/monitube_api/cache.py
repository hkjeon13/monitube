"""Fail-open Redis cache for small, reproducible derived read models."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import random
from threading import Lock
from time import sleep
from typing import Any, TypeVar

try:
    import redis
except ImportError:  # pragma: no cover - optional in minimal local installs
    redis = None  # type: ignore[assignment]


T = TypeVar("T")


class DerivedCache:
    """A deliberately small cache facade; PostgreSQL remains authoritative."""

    SERIALIZATION_VERSION = 1

    def __init__(
        self,
        url: str | None,
        *,
        enabled: bool,
        connect_timeout_seconds: float = 0.2,
        read_timeout_seconds: float = 0.3,
    ) -> None:
        self.enabled = bool(enabled and url and redis is not None)
        self._client: Any | None = None
        self._metrics = {"hit": 0, "miss": 0, "error": 0, "write": 0}
        self._metric_lock = Lock()
        if self.enabled:
            self._client = redis.Redis.from_url(
                url,
                socket_connect_timeout=connect_timeout_seconds,
                socket_timeout=read_timeout_seconds,
                health_check_interval=30,
                decode_responses=True,
            )

    @staticmethod
    def filter_hash(filters: dict[str, Any]) -> str:
        normalized = json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def target_summary_key(target_id: str, data_version: int) -> str:
        return f"monitube:v1:target:{target_id}:summary:{max(0, data_version)}"

    @staticmethod
    def owner_explore_key(owner_id: str, filter_hash: str, generation: int | str) -> str:
        generation_token = hashlib.sha256(str(generation).encode("utf-8")).hexdigest()[:16]
        return f"monitube:v1:owner:{owner_id}:explore:{filter_hash}:data:{generation_token}"

    def _count(self, name: str) -> None:
        with self._metric_lock:
            self._metrics[name] += 1

    @property
    def metrics(self) -> dict[str, int]:
        with self._metric_lock:
            return dict(self._metrics)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:
            self._count("error")

    def health(self) -> dict[str, object]:
        if not self.enabled or self._client is None:
            return {"enabled": False, "status": "disabled"}
        try:
            return {
                "enabled": True,
                "status": "ok" if self._client.ping() else "unavailable",
                "metrics": self.metrics,
            }
        except Exception:
            self._count("error")
            return {"enabled": True, "status": "unavailable", "metrics": self.metrics}

    def get_json(self, key: str) -> Any | None:
        if not self.enabled or self._client is None:
            return None
        try:
            raw = self._client.get(key)
            if raw is None:
                self._count("miss")
                return None
            envelope = json.loads(raw)
            if envelope.get("version") != self.SERIALIZATION_VERSION:
                self._count("miss")
                return None
            self._count("hit")
            return envelope.get("value")
        except Exception:
            self._count("error")
            return None

    def set_json(self, key: str, value: Any, *, ttl_seconds: int = 45) -> None:
        if not self.enabled or self._client is None:
            return
        try:
            ttl = max(1, ttl_seconds + random.randint(0, max(1, ttl_seconds // 4)))
            self._client.setex(
                key,
                ttl,
                json.dumps(
                    {"version": self.SERIALIZATION_VERSION, "value": value},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._count("write")
        except Exception:
            self._count("error")

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], T],
        *,
        ttl_seconds: int = 45,
        lock_seconds: int = 5,
    ) -> T:
        cached = self.get_json(key)
        if cached is not None:
            return cached
        if not self.enabled or self._client is None:
            return loader()
        lock_key = f"{key}:lock"
        owns_lock = False
        try:
            owns_lock = bool(self._client.set(lock_key, "1", nx=True, ex=max(1, lock_seconds)))
        except Exception:
            self._count("error")
        if not owns_lock:
            # Give the current producer a short bounded head start. This merges
            # the common concurrent-miss burst without making Redis or a slow
            # producer a hard dependency; after 260 ms we fail open to SQL.
            for delay in (0.02, 0.04, 0.08, 0.12):
                sleep(delay)
                cached = self.get_json(key)
                if cached is not None:
                    return cached
        value = loader()
        if owns_lock:
            self.set_json(key, value, ttl_seconds=ttl_seconds)
            try:
                self._client.delete(lock_key)
            except Exception:
                self._count("error")
        return value

    def increment_owner_generation(self, owner_id: str) -> int | None:
        if not self.enabled or self._client is None:
            return None
        try:
            return int(self._client.incr(f"monitube:v1:owner:{owner_id}:explore:generation"))
        except Exception:
            self._count("error")
            return None

    def owner_generation(self, owner_id: str) -> int:
        if not self.enabled or self._client is None:
            return 0
        try:
            raw = self._client.get(f"monitube:v1:owner:{owner_id}:explore:generation")
            return int(raw or 0)
        except Exception:
            self._count("error")
            return 0
