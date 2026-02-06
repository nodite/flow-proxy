"""Request filtering module for handling API-specific request transformations."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from proxy.http.parser import HttpParser


@dataclass
class FilterRule:
    """Defines a request filtering rule.

    Attributes:
        name: Rule name for logging
        matcher: Function to check if rule applies to request
        query_params_to_remove: Query parameters to filter out
        body_params_to_remove: Request body parameters to filter out
        headers_to_remove: HTTP headers to filter out
    """

    name: str
    matcher: Callable[[HttpParser, str], bool]
    query_params_to_remove: list[str] = field(default_factory=list)
    body_params_to_remove: list[str] = field(default_factory=list)
    headers_to_remove: list[str] = field(default_factory=list)


class RequestFilter:
    """Handles request filtering based on configured rules."""

    def __init__(self, logger: logging.Logger):
        """Initialize request filter.

        Args:
            logger: Logger instance for debugging
        """
        self.logger = logger
        self.rules = self._initialize_rules()

    def _initialize_rules(self) -> list[FilterRule]:
        """Initialize filtering rules.

        Returns:
            List of filter rules
        """
        return [
            FilterRule(
                name="Anthropic Messages API",
                matcher=self._is_anthropic_messages_request,
                body_params_to_remove=["context_management"],
                headers_to_remove=["anthropic-beta"],
            )
        ]

    def _is_anthropic_messages_request(self, request: HttpParser, path: str) -> bool:
        """Check if request is for Anthropic Messages API.

        Args:
            request: HTTP request object
            path: Request path

        Returns:
            True if this is an Anthropic Messages API request
        """
        # Check path
        base_path = path.split("?")[0] if "?" in path else path
        if not base_path.startswith("/v1/messages"):
            return False

        # Check for anthropic-specific headers
        if not request.headers:
            return False

        anthropic_headers = {"anthropic-version", "anthropic-beta", "x-api-key"}
        for header_name, _ in request.headers.items():
            name = header_name.decode("utf-8", errors="replace").lower()
            if name in anthropic_headers:
                return True

        return False

    def find_matching_rule(self, request: HttpParser, path: str) -> FilterRule | None:
        """Find the first matching filter rule for the request.

        Args:
            request: HTTP request object
            path: Request path

        Returns:
            Matching filter rule or None
        """
        for rule in self.rules:
            if rule.matcher(request, path):
                self.logger.debug("Matched filter rule: %s", rule.name)
                return rule
        return None

    def filter_query_params(self, path: str, params_to_remove: list[str]) -> str:
        """Filter query parameters from path.

        Args:
            path: Original request path
            params_to_remove: List of parameter names to remove

        Returns:
            Filtered path
        """
        if "?" not in path or not params_to_remove:
            return path

        base_path, query_string = path.split("?", 1)
        params_to_remove_lower = {p.lower() for p in params_to_remove}

        # Filter parameters
        filtered_params = []
        for param in query_string.split("&"):
            if "=" in param:
                key, _ = param.split("=", 1)
                if key.lower() not in params_to_remove_lower:
                    filtered_params.append(param)
            elif param.lower() not in params_to_remove_lower:
                filtered_params.append(param)

        # Reconstruct path
        if filtered_params:
            filtered_path = f"{base_path}?{'&'.join(filtered_params)}"
        else:
            filtered_path = base_path

        if filtered_path != path:
            self.logger.debug("Filtered query params: %s -> %s", path, filtered_path)

        return filtered_path

    def filter_body_params(
        self, body: bytes | None, params_to_remove: list[str]
    ) -> bytes | None:
        """Filter parameters from JSON request body.

        Args:
            body: Original request body
            params_to_remove: List of parameter names to remove

        Returns:
            Filtered request body
        """
        if not body or not params_to_remove:
            return body

        try:
            # Parse JSON
            body_str = body.decode("utf-8")
            data = json.loads(body_str)

            # Remove parameters
            filtered = False
            for param in params_to_remove:
                if param in data:
                    del data[param]
                    filtered = True
                    self.logger.debug("Filtered body parameter: %s", param)

            # Return filtered body if changed
            if filtered:
                filtered_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.logger.debug(
                    "Body size: %d -> %d bytes", len(body), len(filtered_body)
                )
                return filtered_body

            return body

        except (json.JSONDecodeError, UnicodeDecodeError, Exception) as e:
            self.logger.debug("Could not filter request body: %s", e)
            return body

    def get_headers_to_skip(self, filter_rule: FilterRule | None) -> set[str]:
        """Get set of headers to skip when forwarding request.

        Args:
            filter_rule: Filter rule to apply, if any

        Returns:
            Set of lowercase header names to skip
        """
        # Base headers to always skip
        skip_headers = {"host", "connection", "content-length", "authorization"}

        # Add headers from filter rule
        if filter_rule and filter_rule.headers_to_remove:
            for header in filter_rule.headers_to_remove:
                skip_headers.add(header.lower())
                self.logger.debug("Will filter header: %s", header)

        return skip_headers
