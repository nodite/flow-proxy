"""Tests for log cleaner module."""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from flow_proxy_plugin.utils.log_cleaner import (
    LogCleaner,
    get_log_cleaner,
    init_log_cleaner,
    stop_log_cleaner,
)


@pytest.fixture
def temp_log_dir(tmp_path: Path) -> Path:
    """Create a temporary log directory."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def log_cleaner(temp_log_dir: Path) -> LogCleaner:  # type: ignore[misc]
    """Create a log cleaner instance for testing."""
    cleaner = LogCleaner(
        log_dir=temp_log_dir,
        retention_days=1,
        cleanup_interval_hours=1,
        max_size_mb=1,
        enabled=False,  # Don't start automatically in tests
    )
    yield cleaner
    cleaner.stop()


def create_log_file(log_dir: Path, name: str, age_days: int = 0, size_mb: float = 0.1) -> Path:
    """Create a test log file with specific age and size.

    Args:
        log_dir: Directory to create the log file in
        name: Name of the log file
        age_days: Age of the file in days
        size_mb: Size of the file in MB

    Returns:
        Path to the created log file
    """
    log_file = log_dir / name

    # Create file with specified size
    content = "x" * int(size_mb * 1024 * 1024)
    log_file.write_text(content)

    # Set modification time
    if age_days > 0:
        old_time = datetime.now() - timedelta(days=age_days)
        timestamp = old_time.timestamp()
        log_file.touch()
        import os
        os.utime(log_file, (timestamp, timestamp))

    return log_file


class TestLogCleaner:
    """Test cases for LogCleaner class."""

    def test_init(self, temp_log_dir: Path) -> None:
        """Test log cleaner initialization."""
        cleaner = LogCleaner(
            log_dir=temp_log_dir,
            retention_days=7,
            cleanup_interval_hours=24,
            max_size_mb=100,
            enabled=True,
        )

        assert cleaner.log_dir == temp_log_dir
        assert cleaner.retention_days == 7
        assert cleaner.cleanup_interval_hours == 24
        assert cleaner.max_size_mb == 100
        assert cleaner.enabled is True

        cleaner.stop()

    def test_cleanup_old_logs(self, log_cleaner: LogCleaner, temp_log_dir: Path) -> None:
        """Test cleaning up old log files."""
        # Create test files
        old_log = create_log_file(temp_log_dir, "old.log", age_days=2)
        recent_log = create_log_file(temp_log_dir, "recent.log", age_days=0)

        # Run cleanup
        result = log_cleaner.cleanup_logs()

        # Old log should be deleted, recent log should remain
        assert not old_log.exists()
        assert recent_log.exists()
        assert result["deleted_files"] == 1
        assert result["freed_space_mb"] > 0

    def test_cleanup_by_size(self, temp_log_dir: Path) -> None:
        """Test cleaning up logs when total size exceeds limit."""
        # Create cleaner with 1MB limit
        cleaner = LogCleaner(
            log_dir=temp_log_dir,
            retention_days=365,  # Don't clean by age
            cleanup_interval_hours=1,
            max_size_mb=1,
            enabled=False,
        )

        # Create multiple log files totaling more than 1MB
        create_log_file(temp_log_dir, "log1.log", age_days=3, size_mb=0.5)
        create_log_file(temp_log_dir, "log2.log", age_days=2, size_mb=0.5)
        create_log_file(temp_log_dir, "log3.log", age_days=1, size_mb=0.5)

        # Run cleanup
        result = cleaner.cleanup_logs()

        # Should delete oldest files to get under 1MB
        assert result["deleted_files"] > 0

        # Calculate remaining size
        remaining_size = sum(f.stat().st_size for f in temp_log_dir.glob("*.log*"))
        assert remaining_size <= 1 * 1024 * 1024  # Should be under 1MB

    def test_no_cleanup_when_disabled(self, temp_log_dir: Path) -> None:
        """Test that cleanup doesn't run when disabled."""
        cleaner = LogCleaner(
            log_dir=temp_log_dir,
            retention_days=1,
            cleanup_interval_hours=1,
            max_size_mb=1,
            enabled=False,
        )

        # Create old log file
        old_log = create_log_file(temp_log_dir, "old.log", age_days=2)

        # Try to start (should not start)
        cleaner.start()

        # File should still exist
        assert old_log.exists()

        cleaner.stop()

    def test_get_log_stats(self, log_cleaner: LogCleaner, temp_log_dir: Path) -> None:
        """Test getting log statistics."""
        # Create some test files
        create_log_file(temp_log_dir, "log1.log", age_days=1, size_mb=0.5)
        create_log_file(temp_log_dir, "log2.log", age_days=0, size_mb=0.3)

        # Get stats
        stats = log_cleaner.get_log_stats()

        assert stats["total_files"] == 2
        assert stats["total_size_mb"] > 0
        assert stats["oldest_file"] is not None
        assert stats["newest_file"] is not None

    def test_cleanup_nonexistent_directory(self, tmp_path: Path) -> None:
        """Test cleanup with nonexistent directory."""
        nonexistent_dir = tmp_path / "nonexistent"
        cleaner = LogCleaner(
            log_dir=nonexistent_dir,
            retention_days=7,
            cleanup_interval_hours=1,
            max_size_mb=100,
            enabled=False,
        )

        result = cleaner.cleanup_logs()

        assert result["deleted_files"] == 0
        assert result["freed_space_mb"] == 0

    def test_start_stop(self, log_cleaner: LogCleaner, temp_log_dir: Path) -> None:
        """Test starting and stopping the cleaner."""
        log_cleaner.enabled = True

        # Start cleaner
        log_cleaner.start()
        assert log_cleaner._thread is not None
        assert log_cleaner._thread.is_alive()

        # Stop cleaner
        log_cleaner.stop()
        time.sleep(0.1)  # Give thread time to stop
        assert log_cleaner._thread is None or not log_cleaner._thread.is_alive()

    def test_multiple_start_calls(self, log_cleaner: LogCleaner) -> None:
        """Test that multiple start calls don't create multiple threads."""
        log_cleaner.enabled = True

        log_cleaner.start()
        thread1 = log_cleaner._thread

        log_cleaner.start()  # Try to start again
        thread2 = log_cleaner._thread

        assert thread1 is thread2  # Should be same thread

        log_cleaner.stop()


class TestGlobalLogCleaner:
    """Test cases for global log cleaner functions."""

    def test_init_log_cleaner(self, temp_log_dir: Path) -> None:
        """Test initializing global log cleaner."""
        cleaner = init_log_cleaner(
            log_dir=temp_log_dir,
            retention_days=7,
            cleanup_interval_hours=24,
            max_size_mb=100,
            enabled=False,
        )

        assert cleaner is not None
        assert get_log_cleaner() is cleaner

        stop_log_cleaner()
        assert get_log_cleaner() is None

    def test_reinit_stops_previous(self, temp_log_dir: Path) -> None:
        """Test that reinitializing stops the previous cleaner."""
        cleaner1 = init_log_cleaner(
            log_dir=temp_log_dir,
            retention_days=7,
            cleanup_interval_hours=24,
            enabled=False,
        )

        cleaner2 = init_log_cleaner(
            log_dir=temp_log_dir,
            retention_days=14,
            cleanup_interval_hours=12,
            enabled=False,
        )

        assert cleaner1 is not cleaner2
        assert get_log_cleaner() is cleaner2

        stop_log_cleaner()
