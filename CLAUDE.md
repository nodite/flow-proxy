# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flow Proxy Plugin is a proxy.py framework plugin that provides JWT authentication and request forwarding to Flow LLM Proxy service with round-robin load balancing. The project is written in Python 3.12+ using Poetry for dependency management.

**Key Features:**
- High-performance multi-process + multi-threaded architecture
- JWT token caching to reduce authentication overhead by ~95%
- Round-robin load balancing across multiple auth configurations
- Two proxy modes: forward proxy (curl -x) and reverse proxy (direct URL access)
- Automatic log cleanup and rotation

## Development Commands

### Setup and Installation
```bash
make setup              # Initialize project: install deps, setup pre-commit, create configs
poetry install          # Install dependencies only
```

### Running the Service
```bash
make run                # Run with DEBUG logging
poetry run flow-proxy   # Run with default settings (INFO level)

# Custom configuration
poetry run flow-proxy --port 8899 --host 0.0.0.0 --log-level DEBUG
poetry run flow-proxy --num-workers 8 --no-threaded
```

### Testing
```bash
make test               # Run all tests
make test-cov           # Run tests with coverage report (outputs to htmlcov/)
pytest tests/test_jwt_generator.py -v  # Run specific test file
pytest -k test_function_name           # Run specific test
```

### Code Quality
```bash
make lint               # Run all linters (Ruff + MyPy + Pylint)
make format             # Auto-format code with Ruff
make pre-commit         # Run pre-commit hooks manually
make check              # Run all checks and tests
```

### Docker Operations
```bash
make deploy             # Quick deploy with docker-compose
make docker-logs        # View container logs (follows)
make docker-restart     # Restart service
make docker-down        # Stop service
```

## Architecture

The codebase follows a modular architecture built on top of the proxy.py framework:

### Core Components (`flow_proxy_plugin/core/`)

1. **SecretsManager** (`config.py`): Loads and validates `secrets.json` configurations
   - Validates required fields (clientId, clientSecret, tenant)
   - Supports optional fields (name, agent, appToAccess)

2. **JWTGenerator** (`jwt_generator.py`): Generates and caches JWT tokens
   - **Thread-safe caching**: Uses class-level cache with threading.Lock
   - **TTL**: 1 hour with 5-minute refresh margin
   - **Cache key**: Based on clientId
   - **IMPORTANT**: The cache is shared across all instances (class variable `_cache`)

3. **LoadBalancer** (`load_balancer.py`): Round-robin request distribution
   - **Thread-safe**: All operations protected by `threading.Lock`
   - **Stateful**: Maintains current index and failed config tracking
   - **Failover**: Automatically skips failed configs
   - **Context manager**: Use `with lb.get_next_config_context()` for automatic failure handling

4. **RequestForwarder** (`request_forwarder.py`): Handles request modification and validation
   - Validates requests targeting Flow LLM Proxy
   - Modifies headers to include JWT authentication
   - Converts requests to HTTPS for Flow LLM Proxy endpoint

### Plugin Layer (`flow_proxy_plugin/plugins/`)

- **BaseFlowProxyPlugin** (`base_plugin.py`): Thin mixin providing service references and utilities
  - `_init_services()`: binds references from `ProcessServices.get()` — call once in first `__init__`
  - `_rebind()`: abstract — subclasses override to rebind proxy.py connection state on pool reuse
  - `_reset_request_state()`: empty hook — override if `handle_request()` assigns `self.*` request-scoped state
  - Keeps `_get_config_and_token()` (with failover), `_decode_bytes()`, `_extract_header_value()`

- **FlowProxyPlugin** (`proxy_plugin.py`): Forward proxy mode implementation
  - Extends `HttpProxyBasePlugin` from proxy.py
  - Hook: `before_upstream_connection()` — processes requests with `-x` proxy flag
  - Pool-enabled: `__new__` routes through `_proxy_pool`, `__init__` guarded by `if self._pooled: return`
  - Releases to pool in `on_upstream_connection_close()`

- **FlowProxyWebServerPlugin** (`web_server_plugin.py`): Reverse proxy mode implementation
  - Extends `HttpWebServerBasePlugin` from proxy.py
  - Hook: `handle_request()` — authenticates, builds params, launches `_streaming_worker` daemon thread, returns immediately (non-blocking); on setup failure logs ERROR and sends 500 with `clear_request_context()`
  - **B3 async pipe-mediated streaming**: worker → `chunk_queue` + `os.pipe()` notification → `read_from_descriptors()` (main thread) → `self.client.queue()`. Only the main thread ever calls `self.client.queue()`
  - `_ResponseHeaders` dataclass (plain Python types only — no httpx objects cross thread boundaries) carries `status_code`, `reason_phrase`, `headers`, `is_sse`; always the first item in `chunk_queue`
  - Worker queue protocol (strict order): `_ResponseHeaders` → `bytes` chunks → `None` sentinel (always last, even on error)
  - `StreamingState` dataclass holds `pipe_r/w`, `chunk_queue`, `cancel` event, `thread`, per-request metadata; stored as `self._streaming_state`
  - `get_descriptors()` registers `pipe_r` with proxy.py's selector while streaming is active
  - `_send_response_headers_from()` always strips `connection` and `transfer-encoding` to avoid chunked-encoding framing issues; adds SSE anti-buffering headers when `is_sse`
  - On `httpx.TransportError`, worker calls `mark_http_client_dirty()` for client rebuild
  - Pool-enabled: `__new__` routes through `_web_pool`, `__init__` guarded by `if self._pooled: return`
  - Releases to pool in `on_client_connection_close()`

### Request Filtering (`flow_proxy_plugin/plugins/request_filter.py`)

- **RequestFilter**: Transforms requests before forwarding based on configurable rules
  - **FilterRule**: Dataclass defining matcher + params/headers to strip
  - Currently strips `context_management` body field and `anthropic-beta` header from Anthropic Messages API requests (`/v1/messages` with Anthropic headers)
  - Supports dotted path deletion (e.g. `tools.0.custom.field`) and wildcard paths (`tools.*.custom.field`)
  - To add a new rule: add a `FilterRule` to `_initialize_rules()` with a matcher function

### Utilities (`flow_proxy_plugin/utils/`)

- **process_services.py**: `ProcessServices` singleton — lazily initialized per-process (fork-safe), owns all shared resources: `logger`, `secrets_manager`, `configs`, `load_balancer`, `jwt_generator`, `request_forwarder`, `request_filter`, and a persistent `httpx.Client`. Use `ProcessServices.get()` to access; `ProcessServices.reset()` in tests only.
- **plugin_pool.py**: `PluginPool[T]` — thread-safe generic instance pool. `acquire(*args)` returns a pooled instance (calling `_rebind()`) or creates a new one via `object.__new__()` to avoid recursion. `release(instance)` calls `_reset_request_state()` then returns to pool.
- **log_context.py**: Thread-local request context for log grouping. `set_request_context(req_id, component)` / `clear_request_context()` bracket each request; `component_context(name)` is a context manager for sub-component tagging. Logs emit `[req_id][COMPONENT] ` prefix automatically via log filter.
- **logging.py**: Sets up colored console + rotating file handlers; file handler must be re-created in child processes after `fork()`
- **log_cleaner.py**: Scheduled cleanup of old log files; configurable via env vars
- **log_filter.py**: Suppresses noisy third-party log messages

### Error Handling

- **ErrorHandler** (`flow_proxy_plugin/error_handler.py`): Structured error responses with `ErrorCode` enum
- **NetworkErrorHandler** (`flow_proxy_plugin/network_error_handler.py`): Retry logic for network operations

## Configuration

### Required Files
- **secrets.json**: Auth configurations (required)
  ```json
  [
    {
      "name": "config1",           // Optional: human-readable name
      "clientId": "...",            // Required
      "clientSecret": "...",        // Required
      "tenant": "..."               // Required
    }
  ]
  ```

### Environment Variables
All environment variables are prefixed with `FLOW_PROXY_`:
- `FLOW_PROXY_PORT=8899`: Server port
- `FLOW_PROXY_HOST=127.0.0.1`: Bind address
- `FLOW_PROXY_NUM_WORKERS`: Worker processes (default: CPU count)
- `FLOW_PROXY_THREADED=1`: Enable threaded mode (1=on, 0=off)
- `FLOW_PROXY_CLIENT_TIMEOUT=600`: Client inactivity timeout (seconds, clamped 1–86400); must be ≥ backend TTFB for streaming; proxy.py default of 10s closes connections before first byte arrives
- `FLOW_PROXY_LOG_LEVEL=INFO`: Logging level
- `FLOW_PROXY_SECRETS_FILE=secrets.json`: Path to secrets file
- `FLOW_PROXY_LOG_DIR=logs`: Log directory
- `FLOW_PROXY_LOG_CLEANUP_ENABLED=true`: Enable automatic log cleanup
- `FLOW_PROXY_LOG_RETENTION_DAYS=7`: Log retention period
- `FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=24`: Cleanup check interval
- `FLOW_PROXY_LOG_MAX_SIZE_MB=100`: Max log directory size before forced cleanup
- `FLOW_PROXY_PLUGIN_POOL_SIZE=64`: Max pooled plugin instances per plugin type

## Testing Guidelines

### Test Organization
Tests are organized by component in `tests/`:
- `test_cli.py`: CLI argument parsing and startup
- `test_config.py`: SecretsManager validation
- `test_jwt_generator.py`: JWT generation and caching
- `test_load_balancer.py`: Round-robin logic
- `test_plugin.py`: FlowProxyPlugin behavior
- `test_web_server_plugin.py`: FlowProxyWebServerPlugin behavior
- `test_thread_safety.py`: Concurrent access tests (includes PluginPool thread-safety)
- `test_shared_state.py`: Shared state via ProcessServices singleton
- `test_process_services.py`: ProcessServices singleton, http_client resilience
- `test_plugin_pool.py`: PluginPool acquire/release/reuse behavior

### Test Fixtures
Common fixtures are defined in `conftest.py`:
- `mock_config`: Sample auth configuration
- `mock_configs`: List of multiple configs for load balancing tests
- `secrets_file`: Temporary secrets.json file

Plugin tests (`test_plugin.py`, `test_web_server_plugin.py`) use a local `mock_svc` fixture that returns a `MagicMock` of `ProcessServices`, and an `autouse` `reset_state` fixture that calls `ProcessServices.reset()` and resets the module-level pool variable (`proxy_mod._proxy_pool = None` / `ws_mod._web_pool = None`) before and after each test.

### Running Tests
Use pytest markers and filters for targeted testing:
```bash
pytest -v                          # Verbose output
pytest --cov=flow_proxy_plugin     # With coverage
pytest -k jwt                      # Run tests matching "jwt"
pytest tests/test_load_balancer.py::test_round_robin  # Specific test
```

## Code Style

### Linting Configuration
- **Ruff**: Fast Python linter (configured in pyproject.toml)
  - Line length: 88 characters
  - Ignores E501 (line too long, handled by formatter)

- **MyPy**: Type checking with strict configuration
  - Requires type hints on all function definitions
  - `disallow_untyped_defs = true`
  - `check_untyped_defs = true`

- **Pylint**: Code quality analysis
  - Max args: 7, max locals: 15
  - Line length: 88 (consistent with Ruff)

### Pre-commit Hooks
Configured in `.pre-commit-config.yaml`:
- Ruff linting and formatting
- MyPy type checking
- Runs automatically on git commit after `make setup`

## Key Design Patterns

### ProcessServices Singleton
`ProcessServices` (in `utils/process_services.py`) is the single source of truth for all shared resources. It is lazily initialized the first time `ProcessServices.get()` is called after `fork()`, making it fork-safe. All plugin instances share the same `LoadBalancer`, `JWTGenerator`, `httpx.Client`, etc. — critical for correct round-robin behavior across threads/processes.

### PluginPool Instance Reuse
Both plugins intercept their own instantiation via `__new__` to route through a module-level `PluginPool`. On each connection, proxy.py calls `Plugin(uid, flags, client, event_queue)` — `__new__` returns a pooled instance, `_rebind()` re-runs `HttpXxxBasePlugin.__init__()` to reset connection state, and `__init__` is a no-op (guarded by `if self._pooled: return`). On connection close, the instance is returned to the pool. This eliminates per-connection initialization overhead.

**`__new__` recursion hazard**: `PluginPool.acquire()` uses `object.__new__(cls)` + `.__init__()` directly — never `PluginClass(*args)`, which would re-enter `__new__`.

**`_rebind` must call proxy.py parent explicitly**: `_rebind` calls `HttpWebServerBasePlugin.__init__(self, ...)` directly, not `super().__init__()`, because the MRO for the concrete class would resolve to `object.__init__`.

### Thread Safety
- `JWTGenerator`: Class-level `threading.Lock` protecting the `_cache` dict
- `LoadBalancer`: Instance lock for index and failed-config state
- `ProcessServices`: Double-checked locking for singleton initialization
- `PluginPool`: Per-pool lock protecting the `deque`; `_rebind()` called outside the lock

### Failover
`BaseFlowProxyPlugin._get_config_and_token()` handles failover: if JWT generation fails, the config is marked failed via `load_balancer.mark_config_failed()` and the next config is tried automatically.

### Multi-process File Logging
After `fork()`, file handlers from the parent process stop working. `ProcessServices._initialize()` calls `setup_file_handler_for_child_process()` — this runs once per process on first `get()` call after fork.

### httpx.Client Resilience
`ProcessServices` holds a single persistent `httpx.Client`. On `httpx.TransportError`, `handle_request()` calls `ProcessServices.get().mark_http_client_dirty()` which closes the old client and sets it to `None`. The next call to `get_http_client()` rebuilds it (double-checked lock on `_client_lock`).

## Deployment

### Docker Deployment
The service is containerized and configured for production:
- Memory limit: 512MB
- CPU limit: 2.0 cores
- Health check on port 8899
- Automatic restart unless stopped
- Log rotation configured

### Performance Tuning
Default configuration is optimized for performance:
- Multi-process workers = CPU count
- Multi-threaded mode enabled by default
- JWT token caching (1-hour TTL)
- Automatic log cleanup to manage disk space

## Common Patterns

### Adding a New Core Component
1. Create module in `flow_proxy_plugin/core/`
2. Export in `flow_proxy_plugin/core/__init__.py`
3. Initialize in `ProcessServices._initialize()` and expose as an instance attribute
4. Add tests in `tests/test_<component>.py`

### Modifying Request Processing
1. Update `RequestForwarder` for header/URL modifications
2. Update `FlowProxyPlugin.before_upstream_connection()` for forward proxy changes
3. Update `FlowProxyWebServerPlugin.handle_request()` for reverse proxy changes
4. Add tests to verify both proxy modes work correctly

### Environment Variable Configuration
All environment variables use the `FLOW_PROXY_` prefix and have:
- Default value defined in code
- Example in `.env.example`
- CLI argument equivalent (if applicable)
