# 日志自动清理功能

## 概述

Flow Proxy Plugin 提供了自动清理日志文件的功能，可以帮助管理磁盘空间，防止日志文件无限增长。

## 功能特性

### 1. 日志按天分文件
- 使用 `TimedRotatingFileHandler` 实现日志按天轮转
- 每天午夜自动创建新的日志文件
- 文件命名格式：
  - 当前日志：`flow_proxy_plugin.log`
  - 历史日志：`flow_proxy_plugin.log.2026-02-01`、`flow_proxy_plugin.log.2026-01-31` 等

### 2. 按时间清理
- 自动删除超过指定天数的日志文件
- 默认保留最近 7 天的日志

### 3. 按大小清理
- 当日志目录总大小超过限制时，自动删除最旧的文件
- 默认限制为 100 MB（可设置为 0 表示不限制）

### 4. 定期执行
- 在后台线程中定期执行清理任务
- 默认每 24 小时清理一次
- 应用启动时立即执行一次清理

### 5. 灵活配置
- 通过环境变量灵活配置清理策略
- 可以完全禁用自动清理功能

## 配置选项

所有配置选项都可以通过环境变量设置。在 `.env` 文件中添加以下配置：

### FLOW_PROXY_LOG_CLEANUP_ENABLED

是否启用日志自动清理功能。

- **类型**: boolean (true/false)
- **默认值**: `true`
- **示例**:
  ```bash
  FLOW_PROXY_LOG_CLEANUP_ENABLED=true
  ```

### FLOW_PROXY_LOG_RETENTION_DAYS

日志文件保留天数。超过此天数的日志文件将被删除。

- **类型**: integer
- **默认值**: `7`
- **示例**:
  ```bash
  FLOW_PROXY_LOG_RETENTION_DAYS=7
  ```

### FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS

日志清理任务执行间隔（小时）。

- **类型**: integer
- **默认值**: `24`
- **示例**:
  ```bash
  FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=24
  ```

### FLOW_PROXY_LOG_MAX_SIZE_MB

日志目录最大大小（MB）。设置为 0 表示不限制。

- **类型**: integer
- **默认值**: `100`
- **示例**:
  ```bash
  FLOW_PROXY_LOG_MAX_SIZE_MB=100
  ```

## 使用示例

### 默认配置

默认情况下，日志清理功能已启用，使用以下配置：

```bash
FLOW_PROXY_LOG_CLEANUP_ENABLED=true
FLOW_PROXY_LOG_RETENTION_DAYS=7
FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=24
FLOW_PROXY_LOG_MAX_SIZE_MB=100
```

### 保留更多天数的日志

如果需要保留更长时间的日志（例如 30 天）：

```bash
FLOW_PROXY_LOG_RETENTION_DAYS=30
```

### 增加日志目录大小限制

如果需要更大的日志存储空间（例如 500 MB）：

```bash
FLOW_PROXY_LOG_MAX_SIZE_MB=500
```

### 更频繁的清理

如果希望更频繁地清理日志（例如每 6 小时）：

```bash
FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=6
```

### 禁用大小限制

如果只想按时间清理，不限制总大小：

```bash
FLOW_PROXY_LOG_MAX_SIZE_MB=0
```

### 完全禁用自动清理

如果不需要自动清理功能：

```bash
FLOW_PROXY_LOG_CLEANUP_ENABLED=false
```

## 工作原理

### 启动流程

1. 应用启动时，`setup_logging()` 函数会初始化日志系统
2. 如果启用了日志清理功能，会自动创建并启动 `LogCleaner` 实例
3. 立即执行一次清理任务
4. 在后台线程中定期执行清理任务

### 清理逻辑

#### 按时间清理

- 扫描日志目录中的所有 `.log*` 文件
- 检查每个文件的修改时间
- 删除修改时间早于 `retention_days` 天前的文件

#### 按大小清理

- 如果设置了 `max_size_mb` 限制（非 0 值）
- 计算日志目录的总大小
- 如果超过限制，按修改时间排序（最旧的在前）
- 依次删除最旧的文件，直到总大小低于限制

### 线程安全

- 清理任务在独立的后台线程中运行
- 不会阻塞主应用程序
- 线程设置为 daemon 模式，应用退出时自动停止

## 监控和调试

### 日志输出

清理任务会输出详细的日志信息：

```
INFO  flow_proxy_plugin.utils.log_cleaner - 日志清理任务已启动，保留 7 天，每 24 小时清理一次
INFO  flow_proxy_plugin.utils.log_cleaner - 开始清理日志，删除 2026-01-25 12:00:00 之前的文件
DEBUG flow_proxy_plugin.utils.log_cleaner - 删除过期日志文件: old_log.log
INFO  flow_proxy_plugin.utils.log_cleaner - 日志清理完成: 删除 3 个文件，释放 15.23 MB 空间
```

### 获取统计信息

可以通过编程方式获取日志统计信息：

```python
from flow_proxy_plugin.utils.log_cleaner import get_log_cleaner

cleaner = get_log_cleaner()
if cleaner:
    stats = cleaner.get_log_stats()
    print(f"日志文件数量: {stats['total_files']}")
    print(f"总大小: {stats['total_size_mb']} MB")
    print(f"最旧文件: {stats['oldest_file']}")
    print(f"最新文件: {stats['newest_file']}")
```

### 手动触发清理

如果需要手动触发清理：

```python
from flow_proxy_plugin.utils.log_cleaner import get_log_cleaner

cleaner = get_log_cleaner()
if cleaner:
    result = cleaner.cleanup_logs()
    print(f"删除了 {result['deleted_files']} 个文件")
    print(f"释放了 {result['freed_space_mb']} MB 空间")
```

## 最佳实践

### 生产环境建议

1. **保留天数**: 根据合规要求设置，通常 7-30 天
2. **大小限制**: 根据可用磁盘空间设置，建议至少 100 MB
3. **清理间隔**: 每天清理一次通常足够

示例配置：

```bash
FLOW_PROXY_LOG_CLEANUP_ENABLED=true
FLOW_PROXY_LOG_RETENTION_DAYS=30
FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=24
FLOW_PROXY_LOG_MAX_SIZE_MB=500
```

### 开发环境建议

开发环境可以使用更宽松的设置：

```bash
FLOW_PROXY_LOG_CLEANUP_ENABLED=true
FLOW_PROXY_LOG_RETENTION_DAYS=3
FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=6
FLOW_PROXY_LOG_MAX_SIZE_MB=100
```

### 高负载环境

对于日志产生量大的环境：

```bash
FLOW_PROXY_LOG_CLEANUP_ENABLED=true
FLOW_PROXY_LOG_RETENTION_DAYS=7
FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=12  # 更频繁
FLOW_PROXY_LOG_MAX_SIZE_MB=1000  # 更大的空间
```

## 故障排查

### 日志文件没有被清理

1. 检查是否启用了清理功能：
   ```bash
   echo $FLOW_PROXY_LOG_CLEANUP_ENABLED
   ```

2. 检查日志文件的修改时间是否超过保留天数

3. 查看应用日志中的清理信息

### 清理过于激进

如果日志被过早删除：

1. 增加 `FLOW_PROXY_LOG_RETENTION_DAYS` 的值
2. 增加或禁用 `FLOW_PROXY_LOG_MAX_SIZE_MB` 限制

### 磁盘空间仍然不足

1. 检查是否有其他应用在同一目录产生日志
2. 减少 `FLOW_PROXY_LOG_RETENTION_DAYS` 的值
3. 减少 `FLOW_PROXY_LOG_MAX_SIZE_MB` 的值
4. 增加清理频率（减少 `FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS`）

## 技术细节

### 实现架构

- **LogCleaner 类**: 核心清理逻辑
- **后台线程**: 定期执行清理任务
- **全局实例**: 通过 `init_log_cleaner()` 创建全局实例
- **集成点**: 在 `setup_logging()` 中自动初始化

### 日志轮转机制

使用 Python 标准库的 `TimedRotatingFileHandler`：
- **轮转时机**: 每天午夜（`when='midnight'`）
- **日期后缀**: `%Y-%m-%d` 格式（如 `2026-02-01`）
- **编码**: UTF-8
- **备份数量**: 不限制（`backupCount=0`），由清理器管理

### 文件匹配模式

清理器使用 glob 模式 `*.log*` 匹配文件，包括：
- `flow_proxy_plugin.log` - 当前日志文件
- `flow_proxy_plugin.log.2026-02-01` - 按天轮转的历史日志
- `*.log.gz` - 如果手动压缩的日志文件

### 时间判断

使用文件的修改时间（mtime）来判断文件年龄，而不是创建时间。

## 相关资源

- [Python logging 文档](https://docs.python.org/3/library/logging.html)
- [日志过滤文档](./log-filtering.md)
- [配置管理文档](../README.md#配置)
