"""Tests for log_context utilities."""

import threading

from flow_proxy_plugin.utils.log_context import (
    clear_request_context,
    component_context,
    get_request_prefix,
    set_request_context,
)


class TestGetRequestPrefix:
    def test_returns_empty_when_no_context(self) -> None:
        clear_request_context()
        assert get_request_prefix() == ""

    def test_returns_prefix_with_req_id_and_component(self) -> None:
        set_request_context("a1b2c3", "WS")
        assert get_request_prefix() == "[a1b2c3][WS] "
        clear_request_context()

    def test_returns_prefix_without_component(self) -> None:
        set_request_context("a1b2c3", "")
        assert get_request_prefix() == "[a1b2c3] "
        clear_request_context()

    def test_clear_resets_prefix(self) -> None:
        set_request_context("a1b2c3", "WS")
        clear_request_context()
        assert get_request_prefix() == ""


class TestComponentContext:
    def test_overrides_component_temporarily(self) -> None:
        set_request_context("abc123", "WS")
        with component_context("JWT"):
            assert get_request_prefix() == "[abc123][JWT] "
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_restores_component_on_exception(self) -> None:
        set_request_context("abc123", "WS")
        try:
            with component_context("JWT"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_nesting(self) -> None:
        set_request_context("abc123", "WS")
        with component_context("LB"):
            assert get_request_prefix() == "[abc123][LB] "
            with component_context("JWT"):
                assert get_request_prefix() == "[abc123][JWT] "
            assert get_request_prefix() == "[abc123][LB] "
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_no_req_id_returns_empty(self) -> None:
        clear_request_context()
        with component_context("JWT"):
            assert get_request_prefix() == ""


class TestThreadIsolation:
    def test_contexts_do_not_bleed_across_threads(self) -> None:
        results: dict[str, str] = {}

        def thread_a() -> None:
            set_request_context("aaaaaa", "WS")
            threading.Event().wait(0.05)  # let thread_b run
            results["a"] = get_request_prefix()
            clear_request_context()

        def thread_b() -> None:
            set_request_context("bbbbbb", "PROXY")
            results["b"] = get_request_prefix()
            clear_request_context()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        assert results["a"] == "[aaaaaa][WS] "
        assert results["b"] == "[bbbbbb][PROXY] "
