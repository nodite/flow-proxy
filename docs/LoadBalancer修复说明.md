# LoadBalancer 负载均衡修复说明

## 问题描述

LoadBalancer 在实际使用中始终使用第一个 secret，没有进行轮询切换。

## 问题原因

`proxy.py` 框架可能为每个请求或在多进程/多线程环境中创建新的插件实例。这导致每个插件实例都有自己独立的 `LoadBalancer` 实例，其内部状态（如 `_current_index`）无法在不同请求之间共享，导致负载均衡失效。

## 解决方案

采用**线程安全单例模式**，通过 `SharedComponentManager` 类管理共享组件：

### 核心设计

#### 1. SharedComponentManager 单例类

```python
class SharedComponentManager:
    """Thread-safe singleton manager for shared plugin components."""
    
    _instance: Optional['SharedComponentManager'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'SharedComponentManager':
        """Create or return the singleton instance (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def get_or_create_components(self, logger):
        """Get or create shared components (thread-safe)."""
        # 双重检查锁定模式
        if self._load_balancer is not None:
            return self._secrets_manager, self._configs, self._load_balancer
        
        with self._lock:
            if self._load_balancer is not None:
                return self._secrets_manager, self._configs, self._load_balancer
            
            # 初始化共享组件
            ...
```

**特点：**
- 使用 `__new__` 方法实现单例模式
- 双重检查锁定确保线程安全和性能
- 提供 `reset()` 方法方便测试清理

#### 2. LoadBalancer 操作级锁

```python
class LoadBalancer:
    """Thread-safe round-robin load balancer."""
    
    def __init__(self, configs, logger):
        self._lock = threading.Lock()
        ...
    
    def get_next_config(self):
        with self._lock:
            # 原子性操作
            config = self._available_configs[self._current_index]
            self._current_index = (self._current_index + 1) % len(...)
            return config
```

**特点：**
- 所有状态修改操作都受锁保护
- 确保并发场景下的数据一致性
- 方法级文档说明线程安全性

### 架构优势

1. **清晰的职责分离**
   - `SharedComponentManager`: 管理共享状态生命周期
   - `LoadBalancer`: 实现负载均衡逻辑
   - `initialize_plugin_components`: 简洁的初始化接口

2. **优雅的单例实现**
   - 符合 Python 惯用法的 `__new__` 单例
   - 避免全局变量污染
   - 易于理解和维护

3. **完整的线程安全**
   - 初始化锁（单例创建）
   - 操作锁（状态修改）
   - 无竞态条件

4. **测试友好**
   - 提供 `reset()` 方法清理状态
   - 单例管理器易于 mock 和测试
   - 清晰的生命周期管理

## 效果验证

### 测试覆盖

- **129 个测试全部通过**
- **共享状态测试**（2 个）：验证跨实例状态共享
- **线程安全测试**（5 个）：验证高并发场景
  - 10 线程并发获取配置
  - 5 线程并发标记失败
  - 5 线程并发重置
  - 20 线程混合操作
  - 100 线程 × 10 次压力测试

### 行为对比

**修复前**：
```
请求 1 → secret 1 (每次都是第一个)
请求 2 → secret 1 (没有轮询)
请求 3 → secret 1 (没有轮询)
```

**修复后**：
```
请求 1 → secret 1 (index 0)
请求 2 → secret 2 (index 1)
请求 3 → secret 1 (index 0，轮询)
请求 4 → secret 2 (index 1，轮询)
...持续轮询，支持高并发
```

## 线程安全机制

### 1. 单例创建锁

```python
_lock = threading.Lock()  # 类级别锁

def __new__(cls):
    if cls._instance is None:
        with cls._lock:  # 保护实例创建
            if cls._instance is None:
                cls._instance = super().__new__(cls)
    return cls._instance
```

### 2. 初始化锁

```python
def get_or_create_components(self, logger):
    # 快速路径：无锁检查
    if self._load_balancer is not None:
        return ...
    
    # 慢速路径：加锁初始化
    with self._lock:
        if self._load_balancer is not None:  # 双重检查
            return ...
        # 初始化...
```

### 3. 操作锁

```python
def get_next_config(self):
    with self._lock:  # 保护所有状态修改
        config = self._available_configs[self._current_index]
        self._current_index = (self._current_index + 1) % len(...)
        return config
```

## 代码示例

### 使用方式

```python
# 获取共享组件（自动单例化）
manager = SharedComponentManager()
secrets_manager, configs, load_balancer = manager.get_or_create_components(logger)

# 线程安全的轮询
config = load_balancer.get_next_config()  # 自动加锁
```

### 测试清理

```python
# 测试前清理状态
from flow_proxy_plugin.utils.plugin_base import SharedComponentManager
SharedComponentManager().reset()
```

## 性能考虑

1. **快速路径优化**：无锁检查已初始化状态
2. **最小锁粒度**：只在必要时持有锁
3. **无死锁风险**：锁的嵌套层级清晰
4. **高并发支持**：验证过 100 线程并发场景

## 相关文件

### 核心实现
- `flow_proxy_plugin/utils/plugin_base.py` - SharedComponentManager 单例管理器
- `flow_proxy_plugin/core/load_balancer.py` - 线程安全的 LoadBalancer

### 测试文件
- `tests/test_shared_state.py` - 共享状态测试
- `tests/test_thread_safety.py` - 并发安全测试
- `tests/test_plugin.py` - 插件集成测试
- `tests/test_web_server_plugin.py` - Web 服务器插件测试

## 最佳实践

1. **避免手动管理共享状态**：使用 `SharedComponentManager`
2. **测试前清理**：调用 `reset()` 方法
3. **并发场景**：信赖内置的线程锁保护
4. **多进程部署**：每个进程独立的共享状态（符合预期）

## 日期

2026-01-18