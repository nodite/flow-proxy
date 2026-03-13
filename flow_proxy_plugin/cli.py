"""Command line interface for Flow Proxy Plugin."""

import argparse
import logging
import multiprocessing
import os
import sys
from pathlib import Path

from proxy.proxy import Proxy, sleep_loop

from .utils.logging import setup_logging

try:
    from importlib.metadata import version

    __version__ = version("flow-proxy-plugin")
except Exception:
    __version__ = "unknown"


def _resolve_runtime_config(
    args: argparse.Namespace, logger: logging.Logger
) -> tuple[int, bool, int]:
    """Resolve num_workers, threaded, and client_timeout (clamped); log clamp warning."""
    num_workers = args.num_workers
    if num_workers is None:
        num_workers = (
            int(os.getenv("FLOW_PROXY_NUM_WORKERS", "0")) or multiprocessing.cpu_count()
        )
    threaded = not args.no_threaded and os.getenv("FLOW_PROXY_THREADED", "1") == "1"
    client_timeout = max(1, min(86400, int(args.client_timeout)))
    if client_timeout != int(args.client_timeout):
        logger.warning(
            "Client timeout %s clamped to %ds (valid range 1–86400)",
            args.client_timeout,
            client_timeout,
        )
    return (num_workers, threaded, client_timeout)


def _build_proxy_args(
    args: argparse.Namespace,
    num_workers: int,
    threaded: bool,
    client_timeout: int,
) -> list[str]:
    """Build proxy.py command-line argument list."""
    proxy_args = [
        "--hostname",
        args.host,
        "--port",
        str(args.port),
        "--num-workers",
        str(num_workers),
        "--timeout",
        str(client_timeout),
        "--plugins",
        "flow_proxy_plugin.plugins.proxy_plugin.FlowProxyPlugin,flow_proxy_plugin.plugins.web_server_plugin.FlowProxyWebServerPlugin",
        "--enable-web-server",
    ]
    if threaded:
        proxy_args.append("--threaded")
    return proxy_args


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(description="Flow Proxy Plugin for proxy.py")

    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FLOW_PROXY_PORT", "8899")),
        help="Port to listen on (default: 8899, env: FLOW_PROXY_PORT)",
    )

    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("FLOW_PROXY_HOST", "127.0.0.1"),
        help="Host to bind to (default: 127.0.0.1, env: FLOW_PROXY_HOST)",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default=os.getenv("FLOW_PROXY_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO, env: FLOW_PROXY_LOG_LEVEL)",
    )

    parser.add_argument(
        "--secrets-file",
        type=str,
        default=os.getenv("FLOW_PROXY_SECRETS_FILE", "secrets.json"),
        help="Path to secrets.json file (default: secrets.json, env: FLOW_PROXY_SECRETS_FILE)",
    )

    parser.add_argument(
        "--log-dir",
        type=str,
        default=os.getenv("FLOW_PROXY_LOG_DIR", "logs"),
        help="Log directory path (default: logs, env: FLOW_PROXY_LOG_DIR)",
    )

    parser.add_argument(
        "--num-workers",
        type=int,
        default=None,
        help="Number of worker processes (default: CPU count, env: FLOW_PROXY_NUM_WORKERS)",
    )

    parser.add_argument(
        "--no-threaded",
        action="store_true",
        help="Disable threaded mode (default: threaded enabled, env: FLOW_PROXY_THREADED=0)",
    )

    parser.add_argument(
        "--client-timeout",
        type=float,
        default=float(os.getenv("FLOW_PROXY_CLIENT_TIMEOUT", "600")),
        help="Client connection inactivity timeout in seconds (default: 600, env: FLOW_PROXY_CLIENT_TIMEOUT). "
        "Must be >= backend TTFB for streaming; proxy.py default is 10s which can close before first byte.",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level, args.log_dir)
    logger = logging.getLogger(__name__)

    # Check if secrets file exists
    secrets_path = Path(args.secrets_file)
    if not secrets_path.exists():
        logger.error(f"Secrets file not found: {args.secrets_file}")
        logger.error(f"Please create {args.secrets_file} from secrets.json.template")
        sys.exit(1)

    num_workers, threaded, client_timeout = _resolve_runtime_config(args, logger)

    # Log startup information
    logger.info("=" * 60)
    logger.info(f"Flow Proxy Plugin v{__version__}")
    logger.info("=" * 60)
    logger.info(f"  Host: {args.host}")
    logger.info(f"  Port: {args.port}")
    logger.info(f"  Workers: {num_workers}")
    logger.info(f"  Threaded: {'enabled' if threaded else 'disabled'}")
    logger.info(f"  Client timeout: {client_timeout}s")
    logger.info(f"  Secrets: {args.secrets_file}")
    logger.info(f"  Log level: {args.log_level}")
    logger.info("=" * 60)

    # Store secrets file path, log level, and log dir in environment for plugin to access
    os.environ["FLOW_PROXY_SECRETS_FILE"] = args.secrets_file
    os.environ["FLOW_PROXY_LOG_LEVEL"] = args.log_level
    os.environ["FLOW_PROXY_LOG_DIR"] = args.log_dir

    proxy_args = _build_proxy_args(args, num_workers, threaded, client_timeout)

    # Start proxy with plugin
    try:
        logger.info("Starting proxy server...")
        with Proxy(input_args=proxy_args) as proxy:
            sleep_loop(proxy)
    except KeyboardInterrupt:
        logger.info("Shutting down Flow Proxy Plugin")
    except Exception as e:
        logger.error(f"Error starting proxy: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
