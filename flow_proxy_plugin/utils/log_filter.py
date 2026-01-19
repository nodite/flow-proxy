"""Log filters for suppressing expected warnings."""

import logging


class BrokenPipeFilter(logging.Filter):
    """Filter to suppress BrokenPipeError warnings from proxy.py.

    BrokenPipeError is a common occurrence in streaming responses when:
    - Client disconnects early (e.g., curl timeout, user cancellation)
    - Client has received all needed data and closes connection
    - Network interruption occurs

    These are expected behaviors in a proxy server and should not be
    logged as warnings since they're handled gracefully in our code.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log records.

        Args:
            record: Log record to filter

        Returns:
            False to suppress the log, True to allow it
        """
        # Suppress BrokenPipeError warnings from proxy.http.handler
        if (
            record.name == "proxy.http.handler"
            and record.levelno == logging.WARNING
            and "BrokenPipeError" in record.getMessage()
        ):
            return False

        # Suppress ConnectionResetError warnings from proxy.http.handler
        if (
            record.name == "proxy.http.handler"
            and record.levelno == logging.WARNING
            and "ConnectionResetError" in record.getMessage()
        ):
            return False

        # Allow all other logs
        return True


class ProxyNoiseFilter(logging.Filter):
    """Filter to suppress noisy INFO logs from proxy.py.

    This filter can be optionally applied to reduce verbosity from
    the proxy.py library while keeping important warnings and errors.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log records.

        Args:
            record: Log record to filter

        Returns:
            False to suppress the log, True to allow it
        """
        # Suppress INFO level logs from proxy.http.server.web
        # These are the request logs like "127.0.0.1:56798 - GET / - curl/8.14.1 - 11775.52ms"
        # We already log these ourselves with better formatting
        if record.name == "proxy.http.server.web" and record.levelno == logging.INFO:
            return False

        # Allow all other logs
        return True


def setup_proxy_log_filters(
    suppress_broken_pipe: bool = True,
    suppress_proxy_noise: bool = False,
) -> None:
    """Set up log filters for proxy.py library.

    Args:
        suppress_broken_pipe: If True, suppress BrokenPipeError warnings
        suppress_proxy_noise: If True, suppress verbose INFO logs from proxy.py
    """
    if suppress_broken_pipe:
        # Apply BrokenPipeFilter to proxy.http.handler logger
        handler_logger = logging.getLogger("proxy.http.handler")
        handler_logger.addFilter(BrokenPipeFilter())

    if suppress_proxy_noise:
        # Apply ProxyNoiseFilter to proxy.http.server.web logger
        web_logger = logging.getLogger("proxy.http.server.web")
        web_logger.addFilter(ProxyNoiseFilter())
