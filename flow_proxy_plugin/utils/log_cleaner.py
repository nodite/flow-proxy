"""日志清理工具模块。

提供自动清理过期日志文件的功能。
"""

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class LogCleaner:
    """日志清理器，负责定期清理过期的日志文件。"""

    def __init__(
        self,
        *,
        log_dir: Path,
        retention_days: int = 7,
        cleanup_interval_hours: int = 24,
        max_size_mb: int = 0,
        enabled: bool = True,
    ):
        """初始化日志清理器。

        Args:
            log_dir: 日志目录路径
            retention_days: 日志保留天数
            cleanup_interval_hours: 清理间隔（小时）
            max_size_mb: 日志目录最大大小（MB），0 表示不限制
            enabled: 是否启用自动清理
        """
        self.log_dir = Path(log_dir)
        self.retention_days = retention_days
        self.cleanup_interval_hours = cleanup_interval_hours
        self.max_size_mb = max_size_mb
        self.enabled = enabled
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动日志清理任务。"""
        if not self.enabled:
            logger.info("日志自动清理功能已禁用")
            return

        if self._thread is not None and self._thread.is_alive():
            logger.warning("日志清理任务已在运行中")
            return

        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 立即执行一次清理
        self.cleanup_logs()

        # 启动定期清理线程
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"日志清理任务已启动，保留 {self.retention_days} 天，"
            f"每 {self.cleanup_interval_hours} 小时清理一次"
        )

    def stop(self) -> None:
        """停止日志清理任务。"""
        if self._thread is None or not self._thread.is_alive():
            return

        logger.info("正在停止日志清理任务...")
        self._stop_event.set()
        self._thread.join(timeout=5)
        self._thread = None
        logger.info("日志清理任务已停止")

    def _cleanup_loop(self) -> None:
        """清理循环，定期执行日志清理。"""
        while not self._stop_event.is_set():
            # 等待指定的时间间隔
            if self._stop_event.wait(self.cleanup_interval_hours * 3600):
                break

            # 执行清理
            try:
                self.cleanup_logs()
            except Exception as e:
                logger.error(f"日志清理失败: {e}", exc_info=True)

    def cleanup_logs(self) -> dict:
        """清理过期的日志文件。

        Returns:
            清理结果统计，包含删除的文件数量和释放的空间
        """
        if not self.log_dir.exists():
            logger.warning(f"日志目录不存在: {self.log_dir}")
            return {"deleted_files": 0, "freed_space_mb": 0}

        deleted_files = 0
        freed_space = 0
        cutoff_time = datetime.now() - timedelta(days=self.retention_days)

        logger.info(f"开始清理日志，删除 {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')} 之前的文件")

        # 按修改时间清理
        for log_file in self.log_dir.glob("*.log*"):
            try:
                # 获取文件修改时间
                mtime = datetime.fromtimestamp(log_file.stat().st_mtime)

                if mtime < cutoff_time:
                    file_size = log_file.stat().st_size
                    log_file.unlink()
                    deleted_files += 1
                    freed_space += file_size
                    logger.debug(f"删除过期日志文件: {log_file.name}")
            except Exception as e:
                logger.error(f"删除日志文件 {log_file} 失败: {e}")

        # 按总大小清理（如果设置了限制）
        if self.max_size_mb > 0:
            deleted, freed = self._cleanup_by_size()
            deleted_files += deleted
            freed_space += freed

        freed_space_mb = freed_space / (1024 * 1024)
        logger.info(f"日志清理完成: 删除 {deleted_files} 个文件，释放 {freed_space_mb:.2f} MB 空间")

        return {
            "deleted_files": deleted_files,
            "freed_space_mb": round(freed_space_mb, 2),
        }

    def _cleanup_by_size(self) -> tuple[int, int]:
        """按总大小清理日志文件。

        如果日志目录总大小超过限制，删除最旧的文件直到满足限制。

        Returns:
            (删除的文件数量, 释放的空间字节数)
        """
        max_size_bytes = self.max_size_mb * 1024 * 1024
        deleted_files = 0
        freed_space = 0

        # 获取所有日志文件及其大小和修改时间
        log_files = []
        total_size = 0
        for log_file in self.log_dir.glob("*.log*"):
            try:
                stat = log_file.stat()
                log_files.append((log_file, stat.st_size, stat.st_mtime))
                total_size += stat.st_size
            except Exception as e:
                logger.error(f"获取文件信息失败 {log_file}: {e}")

        # 如果总大小未超过限制，无需清理
        if total_size <= max_size_bytes:
            return 0, 0

        # 按修改时间排序（最旧的在前）
        log_files.sort(key=lambda x: x[2])

        # 删除最旧的文件直到满足大小限制
        logger.info(
            f"日志目录大小 {total_size / (1024 * 1024):.2f} MB 超过限制 "
            f"{self.max_size_mb} MB，开始清理最旧的文件"
        )

        for log_file, size, _ in log_files:
            if total_size <= max_size_bytes:
                break

            try:
                log_file.unlink()
                deleted_files += 1
                freed_space += size
                total_size -= size
                logger.debug(f"删除日志文件以满足大小限制: {log_file.name}")
            except Exception as e:
                logger.error(f"删除日志文件 {log_file} 失败: {e}")

        return deleted_files, freed_space

    def get_log_stats(self) -> dict:
        """获取日志目录统计信息。

        Returns:
            日志统计信息，包含文件数量、总大小等
        """
        if not self.log_dir.exists():
            return {
                "total_files": 0,
                "total_size_mb": 0,
                "oldest_file": None,
                "newest_file": None,
            }

        log_files = list(self.log_dir.glob("*.log*"))
        total_size = 0
        oldest_time = None
        newest_time = None

        for log_file in log_files:
            try:
                stat = log_file.stat()
                total_size += stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime)

                if oldest_time is None or mtime < oldest_time:
                    oldest_time = mtime
                if newest_time is None or mtime > newest_time:
                    newest_time = mtime
            except Exception:
                pass

        return {
            "total_files": len(log_files),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "oldest_file": oldest_time.strftime("%Y-%m-%d %H:%M:%S") if oldest_time else None,
            "newest_file": newest_time.strftime("%Y-%m-%d %H:%M:%S") if newest_time else None,
        }


class _LogCleanerState:
    """日志清理器状态管理类，避免使用 global 语句。"""

    def __init__(self) -> None:
        self.cleaner: LogCleaner | None = None


# 全局日志清理器状态实例
_state = _LogCleanerState()


def init_log_cleaner(
    log_dir: Path,
    retention_days: int = 7,
    cleanup_interval_hours: int = 24,
    max_size_mb: int = 0,
    enabled: bool = True,
) -> LogCleaner:
    """初始化全局日志清理器。

    Args:
        log_dir: 日志目录路径
        retention_days: 日志保留天数
        cleanup_interval_hours: 清理间隔（小时）
        max_size_mb: 日志目录最大大小（MB）
        enabled: 是否启用自动清理

    Returns:
        日志清理器实例
    """
    if _state.cleaner is not None:
        _state.cleaner.stop()

    _state.cleaner = LogCleaner(
        log_dir=log_dir,
        retention_days=retention_days,
        cleanup_interval_hours=cleanup_interval_hours,
        max_size_mb=max_size_mb,
        enabled=enabled,
    )
    _state.cleaner.start()
    return _state.cleaner


def get_log_cleaner() -> LogCleaner | None:
    """获取全局日志清理器实例。

    Returns:
        日志清理器实例，如果未初始化则返回 None
    """
    return _state.cleaner


def stop_log_cleaner() -> None:
    """停止全局日志清理器。"""
    if _state.cleaner is not None:
        _state.cleaner.stop()
        _state.cleaner = None
