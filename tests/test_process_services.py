# tests/test_process_services.py
import json
import os
import threading
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from flow_proxy_plugin.utils.process_services import ProcessServices


@pytest.fixture(autouse=True)
def reset_singleton(tmp_path: Any) -> Generator[None, None, None]:
    """Reset ProcessServices singleton between tests."""
    ProcessServices.reset()
    secrets = [{"name": "c1", "clientId": "id1", "clientSecret": "s1", "tenant": "t1"}]
    f = tmp_path / "secrets.json"
    f.write_text(json.dumps(secrets))
    os.environ["FLOW_PROXY_SECRETS_FILE"] = str(f)
    yield
    ProcessServices.reset()
    os.environ.pop("FLOW_PROXY_SECRETS_FILE", None)


def _make_services() -> ProcessServices:
    with patch("flow_proxy_plugin.utils.process_services.setup_colored_logger"):
        with patch("flow_proxy_plugin.utils.process_services.setup_file_handler_for_child_process"):
            with patch("flow_proxy_plugin.utils.process_services.setup_proxy_log_filters"):
                return ProcessServices.get()


def test_singleton_returns_same_instance() -> None:
    s1 = _make_services()
    s2 = _make_services()
    assert s1 is s2


def test_reset_clears_singleton() -> None:
    s1 = _make_services()
    ProcessServices.reset()
    s2 = _make_services()
    assert s1 is not s2


def test_thread_safe_singleton() -> None:
    results: list[ProcessServices] = []

    def get() -> None:
        results.append(_make_services())

    threads = [threading.Thread(target=get) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is results[0] for r in results)


def test_has_required_attributes() -> None:
    svc = _make_services()
    for attr in (
        "logger",
        "secrets_manager",
        "configs",
        "load_balancer",
        "jwt_generator",
        "request_forwarder",
        "request_filter",
        "http_client",
    ):
        assert hasattr(svc, attr), f"Missing attribute: {attr}"


def test_http_client_is_reused() -> None:
    svc = _make_services()
    client1 = svc.http_client
    client2 = _make_services().http_client
    assert client1 is client2


def test_reset_closes_http_client() -> None:
    svc = _make_services()
    with patch.object(svc.http_client, "close") as mock_close:
        ProcessServices.reset()
    mock_close.assert_called_once()


def test_get_http_client_rebuilds_after_mark_dirty() -> None:
    svc = _make_services()
    original = svc.http_client
    svc.mark_http_client_dirty()
    new_client = svc.get_http_client()
    assert new_client is not original
    assert svc.http_client is new_client


def test_get_http_client_returns_same_when_healthy() -> None:
    svc = _make_services()
    c1 = svc.get_http_client()
    c2 = svc.get_http_client()
    assert c1 is c2
