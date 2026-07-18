from __future__ import annotations

from monitube_api.cache import DerivedCache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, _: int, value: str) -> None:
        self.values[key] = value

    def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value


def cache() -> DerivedCache:
    instance = DerivedCache(None, enabled=False)
    instance.enabled = True
    instance._client = FakeRedis()
    return instance


def test_cache_is_owner_filter_and_generation_scoped() -> None:
    instance = cache()
    first = instance.owner_explore_key("owner-a", instance.filter_hash({"channel": "a"}), "v1")
    assert first != instance.owner_explore_key("owner-b", instance.filter_hash({"channel": "a"}), "v1")
    assert first != instance.owner_explore_key("owner-a", instance.filter_hash({"channel": "b"}), "v1")
    assert first != instance.owner_explore_key("owner-a", instance.filter_hash({"channel": "a"}), "v2")


def test_cache_round_trip_and_single_flight_write() -> None:
    instance = cache()
    calls = 0

    def load() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"count": 3}

    assert instance.get_or_load("derived", load, ttl_seconds=30) == {"count": 3}
    assert instance.get_or_load("derived", load, ttl_seconds=30) == {"count": 3}
    assert calls == 1
    assert instance.metrics["hit"] == 1


def test_cache_failure_is_fail_open() -> None:
    instance = cache()

    class BrokenRedis(FakeRedis):
        def get(self, key: str) -> str | None:
            raise TimeoutError(key)

        def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
            raise TimeoutError(key)

    instance._client = BrokenRedis()
    assert instance.get_or_load("derived", lambda: {"database": "truth"}) == {
        "database": "truth"
    }
    assert instance.metrics["error"] >= 1
