"""Tests for RequestFilter and body/query/header filtering."""

import json
import logging
from unittest.mock import Mock

import pytest
from proxy.http.parser import HttpParser

from flow_proxy_plugin.plugins.request_filter import FilterRule, RequestFilter


@pytest.fixture
def request_filter() -> RequestFilter:
    """RequestFilter instance with test logger."""
    return RequestFilter(logging.getLogger("test_request_filter"))


class TestFilterBodyParams:
    """Tests for filter_body_params."""

    def test_returns_unchanged_when_body_none(
        self, request_filter: RequestFilter
    ) -> None:
        """None body returns None."""
        assert request_filter.filter_body_params(None, ["x"]) is None

    def test_returns_unchanged_when_params_empty(
        self, request_filter: RequestFilter
    ) -> None:
        """Empty params_to_remove returns body unchanged."""
        body = b'{"a":1}'
        assert request_filter.filter_body_params(body, []) is body

    def test_returns_unchanged_when_body_empty_bytes(
        self, request_filter: RequestFilter
    ) -> None:
        """Empty body with params returns body unchanged."""
        assert request_filter.filter_body_params(b"", ["a"]) == b""

    def test_removes_top_level_key(self, request_filter: RequestFilter) -> None:
        """Top-level key is removed (e.g. context_management)."""
        body = json.dumps({"model": "x", "context_management": {"foo": 1}}).encode()
        out = request_filter.filter_body_params(body, ["context_management"])
        assert out is not None
        data = json.loads(out.decode())
        assert "context_management" not in data
        assert data["model"] == "x"

    def test_removes_dotted_path_explicit_index(
        self, request_filter: RequestFilter
    ) -> None:
        """Dotted path with list index removes nested key (tools.0.custom.defer_loading)."""
        body = json.dumps(
            {"tools": [{"name": "t1", "custom": {"defer_loading": True, "x": 1}}]}
        ).encode()
        out = request_filter.filter_body_params(body, ["tools.0.custom.defer_loading"])
        assert out is not None
        data = json.loads(out.decode())
        assert data["tools"][0]["custom"] == {"x": 1}
        assert "defer_loading" not in data["tools"][0]["custom"]

    def test_removes_wildcard_from_all_tools(
        self, request_filter: RequestFilter
    ) -> None:
        """Wildcard path tools.*.custom.defer_loading removes from every tool."""
        body = json.dumps(
            {
                "tools": [
                    {"name": "t1", "custom": {"defer_loading": True, "a": 1}},
                    {"name": "t2", "custom": {"defer_loading": False}},
                ]
            }
        ).encode()
        out = request_filter.filter_body_params(body, ["tools.*.custom.defer_loading"])
        assert out is not None
        data = json.loads(out.decode())
        assert data["tools"][0]["custom"] == {"a": 1}
        assert "defer_loading" not in data["tools"][0]["custom"]
        assert data["tools"][1]["custom"] == {}
        assert "defer_loading" not in data["tools"][1]["custom"]

    def test_mixed_top_level_and_dotted(self, request_filter: RequestFilter) -> None:
        """Multiple params: top-level and dotted path both applied."""
        body = json.dumps(
            {
                "context_management": {},
                "tools": [{"custom": {"defer_loading": True}}],
            }
        ).encode()
        out = request_filter.filter_body_params(
            body,
            ["context_management", "tools.0.custom.defer_loading"],
        )
        assert out is not None
        data = json.loads(out.decode())
        assert "context_management" not in data
        assert "defer_loading" not in data["tools"][0]["custom"]

    def test_invalid_json_returns_original_body(
        self, request_filter: RequestFilter
    ) -> None:
        """Invalid JSON body is returned unchanged."""
        body = b"not json"
        out = request_filter.filter_body_params(body, ["a"])
        assert out is body

    def test_param_not_present_leaves_body_unchanged(
        self, request_filter: RequestFilter
    ) -> None:
        """When no param matches, body is returned unchanged."""
        body = json.dumps({"model": "x"}).encode()
        out = request_filter.filter_body_params(body, ["nonexistent", "a.b.c"])
        assert out is body

    def test_dotted_path_not_present_no_effect(
        self, request_filter: RequestFilter
    ) -> None:
        """Dotted path that does not exist does not change body."""
        body = json.dumps({"tools": []}).encode()
        out = request_filter.filter_body_params(body, ["tools.0.custom.defer_loading"])
        assert out is body


class TestFilterQueryParams:
    """Tests for filter_query_params."""

    def test_no_query_string_unchanged(self, request_filter: RequestFilter) -> None:
        """Path without ? is returned unchanged."""
        path = "/v1/messages"
        assert request_filter.filter_query_params(path, ["foo"]) == path

    def test_empty_params_to_remove_unchanged(
        self, request_filter: RequestFilter
    ) -> None:
        """Empty params_to_remove leaves path unchanged."""
        path = "/v1?a=1"
        assert request_filter.filter_query_params(path, []) == path

    def test_removes_single_param(self, request_filter: RequestFilter) -> None:
        """One query param is removed."""
        path = "/v1?keep=1&remove=2"
        out = request_filter.filter_query_params(path, ["remove"])
        assert out == "/v1?keep=1"

    def test_removes_multiple_params(self, request_filter: RequestFilter) -> None:
        """Multiple params to remove are all removed."""
        path = "/v1?a=1&b=2&c=3"
        out = request_filter.filter_query_params(path, ["a", "c"])
        assert out == "/v1?b=2"

    def test_removes_all_params_leaves_base_path(
        self, request_filter: RequestFilter
    ) -> None:
        """When all params removed, only base path remains."""
        path = "/v1?x=1"
        out = request_filter.filter_query_params(path, ["x"])
        assert out == "/v1"


class TestGetHeadersToSkip:
    """Tests for get_headers_to_skip."""

    def test_without_rule_returns_base_headers(
        self, request_filter: RequestFilter
    ) -> None:
        """Without filter_rule, returns base skip set."""
        skip = request_filter.get_headers_to_skip(None)
        assert "host" in skip
        assert "connection" in skip
        assert "content-length" in skip
        assert "authorization" in skip

    def test_with_rule_adds_headers_to_remove(
        self, request_filter: RequestFilter
    ) -> None:
        """With filter_rule, headers_to_remove are added (lowercase)."""
        rule = FilterRule(
            name="test",
            matcher=lambda r, p: False,
            headers_to_remove=["anthropic-beta", "X-Custom"],
        )
        skip = request_filter.get_headers_to_skip(rule)
        assert "anthropic-beta" in skip
        assert "x-custom" in skip


class TestFindMatchingRule:
    """Tests for find_matching_rule and Anthropic matcher."""

    def test_path_not_messages_no_match(self, request_filter: RequestFilter) -> None:
        """Path not /v1/messages does not match Anthropic rule."""
        request = Mock(spec=HttpParser)
        request.headers = {b"anthropic-version": (b"2023-06-01", b"")}
        assert request_filter.find_matching_rule(request, "/v1/other") is None

    def test_no_anthropic_headers_no_match(self, request_filter: RequestFilter) -> None:
        """Path /v1/messages but no Anthropic headers does not match."""
        request = Mock(spec=HttpParser)
        request.headers = {b"content-type": (b"application/json", b"")}
        assert request_filter.find_matching_rule(request, "/v1/messages") is None

    def test_messages_with_anthropic_version_matches(
        self, request_filter: RequestFilter
    ) -> None:
        """Path /v1/messages and anthropic-version header matches."""
        request = Mock(spec=HttpParser)
        request.headers = {b"anthropic-version": (b"2023-06-01", b"")}
        rule = request_filter.find_matching_rule(request, "/v1/messages")
        assert rule is not None
        assert rule.name == "Anthropic Messages API"

    def test_messages_with_x_api_key_matches(
        self, request_filter: RequestFilter
    ) -> None:
        """Path /v1/messages and x-api-key header matches."""
        request = Mock(spec=HttpParser)
        request.headers = {b"x-api-key": (b"sk-xxx", b"")}
        rule = request_filter.find_matching_rule(request, "/v1/messages")
        assert rule is not None
        assert rule.name == "Anthropic Messages API"

    def test_messages_with_query_string_matches(
        self, request_filter: RequestFilter
    ) -> None:
        """Path /v1/messages?foo=1 with Anthropic header matches."""
        request = Mock(spec=HttpParser)
        request.headers = {b"anthropic-beta": (b"foo", b"")}
        rule = request_filter.find_matching_rule(request, "/v1/messages?foo=1")
        assert rule is not None
