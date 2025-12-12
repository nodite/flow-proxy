"""Command line interface for Flow Proxy Plugin."""

import argparse
import logging
import sys
from pathlib import Path

from proxy.proxy import Proxy


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("flow_proxy_plugin.log"),
        ],
    )


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(description="Flow Proxy Plugin for proxy.py")

    parser.add_argument(
        "--port", type=int, default=8899, help="Port to listen on (default: 8899)"
    )

    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--secrets-file",
        type=str,
        default="secrets.json",
        help="Path to secrets.json file (default: secrets.json)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Check if secrets file exists
    secrets_path = Path(args.secrets_file)
    if not secrets_path.exists():
        logger.error(f"Secrets file not found: {args.secrets_file}")
        sys.exit(1)

    logger.info(f"Starting Flow Proxy Plugin on {args.host}:{args.port}")
    logger.info(f"Using secrets file: {args.secrets_file}")

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
