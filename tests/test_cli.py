"""Tests for CLI module."""

import multiprocessing
import os
from unittest.mock import MagicMock, patch

import pytest


class TestCLI:
    """Test cases for CLI functionality."""

    def test_default_num_workers(self) -> None:
        """Test that default num_workers is CPU count."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch("sys.argv", ["flow-proxy-plugin"]),
        ):
            # Mock the Proxy to capture arguments
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                # Verify Proxy was called with correct arguments
                args = mock_proxy.call_args[1]["input_args"]

                # Should have --num-workers with CPU count
                assert "--num-workers" in args
                cpu_count = multiprocessing.cpu_count()
                assert str(cpu_count) in args

                # Should have --threaded
                assert "--threaded" in args

    def test_default_threaded_enabled(self) -> None:
        """Test that threaded mode is enabled by default."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch("sys.argv", ["flow-proxy-plugin"]),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]
                assert "--threaded" in args

    def test_custom_num_workers(self) -> None:
        """Test custom num_workers via command line."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch("sys.argv", ["flow-proxy-plugin", "--num-workers", "4"]),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]

                # Should have --num-workers 4
                assert "--num-workers" in args
                idx = args.index("--num-workers")
                assert args[idx + 1] == "4"

    def test_no_threaded_flag(self) -> None:
        """Test disabling threaded mode via --no-threaded."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch("sys.argv", ["flow-proxy-plugin", "--no-threaded"]),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]

                # Should NOT have --threaded
                assert "--threaded" not in args

    def test_env_num_workers(self) -> None:
        """Test num_workers from environment variable."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch.dict(os.environ, {"FLOW_PROXY_NUM_WORKERS": "6"}),
            patch("sys.argv", ["flow-proxy-plugin"]),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]

                # Should have --num-workers 6
                assert "--num-workers" in args
                idx = args.index("--num-workers")
                assert args[idx + 1] == "6"

    def test_env_threaded_disabled(self) -> None:
        """Test disabling threaded via environment variable."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch.dict(os.environ, {"FLOW_PROXY_THREADED": "0"}),
            patch("sys.argv", ["flow-proxy-plugin"]),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]

                # Should NOT have --threaded
                assert "--threaded" not in args

    def test_cli_args_override_env(self) -> None:
        """Test that CLI arguments override environment variables."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Proxy"),
            patch("flow_proxy_plugin.cli.sleep_loop"),
            patch("flow_proxy_plugin.cli.Path.exists", return_value=True),
            patch.dict(
                os.environ, {"FLOW_PROXY_NUM_WORKERS": "6", "FLOW_PROXY_THREADED": "1"}
            ),
            patch(
                "sys.argv", ["flow-proxy-plugin", "--num-workers", "8", "--no-threaded"]
            ),
        ):
            with patch("flow_proxy_plugin.cli.Proxy") as mock_proxy:
                mock_proxy_instance = MagicMock()
                mock_proxy.return_value.__enter__.return_value = mock_proxy_instance

                try:
                    main()
                except SystemExit:
                    pass

                args = mock_proxy.call_args[1]["input_args"]

                # Should use CLI arg values
                assert "--num-workers" in args
                idx = args.index("--num-workers")
                assert args[idx + 1] == "8"
                assert "--threaded" not in args

    def test_secrets_file_missing(self) -> None:
        """Test that missing secrets file causes exit."""
        from flow_proxy_plugin.cli import main

        with (
            patch("flow_proxy_plugin.cli.Path.exists", return_value=False),
            patch("sys.argv", ["flow-proxy-plugin"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
