"""Command line interface for Flow Proxy Plugin."""

import argparse
import logging
import os
import sys
from pathlib import Path

from proxy.proxy import Proxy


def setup_logging(level: str = "INFO", log_file: str = "flow_proxy_plugin.log") -> None:
    """Setup logging configuration.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Path to log file
    """
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


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
        "--log-file",
        type=str,
        default=os.getenv("FLOW_PROXY_LOG_FILE", "flow_proxy_plugin.log"),
        help="Path to log file (default: flow_proxy_plugin.log, env: FLOW_PROXY_LOG_FILE)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level, args.log_file)
    logger = logging.getLogger(__name__)

    # Check if secrets file exists
    secrets_path = Path(args.secrets_file)
    if not secrets_path.exists():
        logger.error(f"Secrets file not found: {args.secrets_file}")
        logger.error(f"Please create {args.secrets_file} from secrets.json.template")
        sys.exit(1)

    logger.info(f"Starting Flow Proxy Plugin on {args.host}:{args.port}")
    logger.info(f"Using secrets file: {args.secrets_file}")
    logger.info(f"Log level: {args.log_level}")
    logger.info(f"Log file: {args.log_file}")

    # Store secrets file path in environment for plugin to access
    os.environ["FLOW_PROXY_SECRETS_FILE"] = args.secrets_file

    # Start proxy with plugin
    try:
        with Proxy(
            hostname=args.host,
            port=args.port,
            plugins=["flow_proxy_plugin.plugin.FlowProxyPlugin"],
        ) as proxy:
            proxy.accept_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Flow Proxy Plugin")
    except Exception as e:
        logger.error(f"Error starting proxy: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
