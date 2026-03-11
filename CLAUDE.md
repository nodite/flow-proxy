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

- **BaseFlowProxyPlugin** (`base_plugin.py`): Shared initialization and component setup
  - Initializes SecretsManager, LoadBalancer, JWTGenerator, RequestForwarder
  - Handles reverse-to-forward proxy conversion
  - Shared by both plugin implementations

- **FlowProxyPlugin** (`proxy_plugin.py`): Forward proxy mode implementation
  - Extends `HttpProxyBasePlugin` from proxy.py
  - Hook: `before_upstream_connection()` - processes requests with `-x` proxy flag
  - Handles authentication for requests sent through the proxy

- **FlowProxyWebServerPlugin** (`web_server_plugin.py`): Reverse proxy mode implementation
  - Extends `HttpWebServerBasePlugin` from proxy.py
  - Hook: `handle_request()` - processes direct HTTP requests
  - Provides web server endpoints for direct access

### Request Filtering (`flow_proxy_plugin/plugins/request_filter.py`)

- **RequestFilter**: Transforms requests before forwarding based on configurable rules
  - **FilterRule**: Dataclass defining matcher + params/headers to strip
  - Currently strips `context_management` body field and `anthropic-beta` header from Anthropic Messages API requests (`/v1/messages` with Anthropic headers)
  - Supports dotted path deletion (e.g. `tools.0.custom.field`) and wildcard paths (`tools.*.custom.field`)
  - To add a new rule: add a `FilterRule` to `_initialize_rules()` with a matcher function

### Utilities (`flow_proxy_plugin/utils/`)

- **plugin_base.py**: `SharedComponentManager` singleton — ensures `LoadBalancer` and `configs` are shared across all plugin instances in multi-threaded mode. `initialize_plugin_components()` is the entry point called by both plugins.
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
- `FLOW_PROXY_LOG_LEVEL=INFO`: Logging level
- `FLOW_PROXY_SECRETS_FILE=secrets.json`: Path to secrets file
- `FLOW_PROXY_LOG_DIR=logs`: Log directory
- `FLOW_PROXY_LOG_CLEANUP_ENABLED=true`: Enable automatic log cleanup
- `FLOW_PROXY_LOG_RETENTION_DAYS=7`: Log retention period
- `FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS=24`: Cleanup check interval
- `FLOW_PROXY_LOG_MAX_SIZE_MB=100`: Max log directory size before forced cleanup

## Testing Guidelines

### Test Organization
Tests are organized by component in `tests/`:
- `test_cli.py`: CLI argument parsing and startup
- `test_config.py`: SecretsManager validation
- `test_jwt_generator.py`: JWT generation and caching
- `test_load_balancer.py`: Round-robin logic
- `test_plugin.py`: FlowProxyPlugin behavior
- `test_web_server_plugin.py`: FlowProxyWebServerPlugin behavior
- `test_thread_safety.py`: Concurrent access tests
- `test_shared_state.py`: Multi-process state management

### Test Fixtures
Common fixtures are defined in `conftest.py`:
- `mock_config`: Sample auth configuration
- `mock_configs`: List of multiple configs for load balancing tests
- `secrets_file`: Temporary secrets.json file

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

### Shared State Across Plugin Instances
`SharedComponentManager` (in `utils/plugin_base.py`) is a thread-safe singleton that ensures `LoadBalancer` and `configs` are shared across all plugin instances. Both proxy modes call `initialize_plugin_components()` which returns the same `LoadBalancer` instance — critical for correct round-robin behavior across threads/processes.

### Thread Safety
- `JWTGenerator`: Class-level `threading.Lock` protecting the `_cache` dict
- `LoadBalancer`: Instance lock for index and failed-config state
- `SharedComponentManager`: Double-checked locking for singleton initialization

### Failover
`BaseFlowProxyPlugin._get_config_and_token()` handles failover: if JWT generation fails, the config is marked failed via `load_balancer.mark_config_failed()` and the next config is tried automatically.

### Multi-process File Logging
After `fork()`, file handlers from the parent process stop working. `setup_file_handler_for_child_process()` creates a fresh file handler in each child process.

### Plugin Architecture
The plugin system uses proxy.py's plugin hooks:
- **Forward proxy** (`FlowProxyPlugin`): Intercepts `before_upstream_connection()` — handles both forward and reverse proxy requests by converting path-only URLs to full URLs before validation
- **Reverse proxy** (`FlowProxyWebServerPlugin`): Intercepts `handle_request()` — uses `httpx.Client().stream()` for zero-buffering SSE streaming (connect=30s, read=600s); new `httpx.Client` per request call for fork safety

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
3. Initialize in `BaseFlowProxyPlugin._initialize_components()`
4. Add tests in `tests/test_<component>.py`

### Modifying Request Processing
1. Update `RequestForwarder` for header/URL modifications
2. Update `FlowProxyPlugin.before_upstream_connection()` for forward proxy changes
3. Update `FlowProxyWebServerPlugin.handle_client_request()` for reverse proxy changes
4. Add tests to verify both proxy modes work correctly

### Environment Variable Configuration
All environment variables use the `FLOW_PROXY_` prefix and have:
- Default value defined in code
- Example in `.env.example`
- CLI argument equivalent (if applicable)
