# tests/test_plugin_pool.py
import threading
from unittest.mock import MagicMock

from flow_proxy_plugin.utils.plugin_pool import PluginPool


class FakePlugin:
    """Minimal plugin stand-in for pool testing."""
    _pooled: bool = False

    def __init__(self, uid: str, **kwargs: object) -> None:
        self.uid = uid
        self._pooled = True

    def _rebind(self, uid: str, **kwargs: object) -> None:
        self.uid = uid

    def _reset_request_state(self) -> None:
        pass


def test_acquire_creates_new_when_pool_empty() -> None:
    pool = PluginPool(FakePlugin, max_size=4)
    instance = pool.acquire("uid-1")
    assert isinstance(instance, FakePlugin)
    assert instance.uid == "uid-1"


def test_acquire_reuses_released_instance() -> None:
    pool = PluginPool(FakePlugin, max_size=4)
    first = pool.acquire("uid-1")
    pool.release(first)
    second = pool.acquire("uid-2")
    assert second is first
    assert second.uid == "uid-2"


def test_release_discards_when_pool_full() -> None:
    pool = PluginPool(FakePlugin, max_size=2)
    instances = [pool.acquire(f"uid-{i}") for i in range(3)]
    for inst in instances:
        pool.release(inst)
    # Pool should only hold max_size instances
    assert len(pool._pool) == 2


def test_release_calls_reset_request_state() -> None:
    pool = PluginPool(FakePlugin, max_size=4)
    inst = pool.acquire("uid-1")
    inst._reset_request_state = MagicMock()  # type: ignore[method-assign]
    pool.release(inst)
    inst._reset_request_state.assert_called_once()


def test_acquire_calls_rebind_on_reuse() -> None:
    pool = PluginPool(FakePlugin, max_size=4)
    inst = pool.acquire("uid-1")
    inst._rebind = MagicMock()  # type: ignore[method-assign]
    pool.release(inst)
    pool.acquire("uid-2")
    inst._rebind.assert_called_once_with("uid-2")


def test_concurrent_acquire_release_thread_safe() -> None:
    pool = PluginPool(FakePlugin, max_size=32)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            inst = pool.acquire(f"uid-{n}")
            pool.release(inst)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_no_infinite_recursion_on_create() -> None:
    """Pool creates new instances without recursion (uses object.__new__)."""
    pool = PluginPool(FakePlugin, max_size=4)
    # Should not raise RecursionError
    for i in range(5):
        inst = pool.acquire(f"uid-{i}")
        pool.release(inst)
