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
  - Hook: `handle_client_request()` - processes direct HTTP requests
  - Provides web server endpoints for direct access

### Utilities (`flow_proxy_plugin/utils/`)

- **logging.py**: Centralized logging configuration
  - Sets up file and console handlers
  - Configures log rotation and formatting
  - Initializes log cleanup scheduler

- **log_cleaner.py**: Automatic log file cleanup
  - Runs on a scheduled interval (configurable via env vars)
  - Removes logs older than retention period
  - Monitors total log directory size

- **log_filter.py**: Log message filtering
  - Filters out noisy third-party library logs
  - Customizable via environment variables

### Error Handling

- **ErrorHandler** (`error_handler.py`): Structured error responses
  - Defines ErrorCode enum for different error types
  - Creates standardized error response structures

- **NetworkErrorHandler** (`network_error_handler.py`): Network-specific error handling
  - Wraps network operations with retry logic
  - Handles timeouts and connection errors

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

### Thread Safety
All shared state components use `threading.Lock`:
- JWTGenerator: Class-level lock for cache access
- LoadBalancer: Instance lock for index/state modifications

### Failover and Retry
LoadBalancer provides automatic failover:
```python
with load_balancer.get_next_config_context() as config:
    # If this block raises an exception, config is marked as failed
    # Next call to get_next_config() will skip this config
    jwt_token = jwt_generator.generate_token(config)
```

### Plugin Architecture
The plugin system uses proxy.py's plugin hooks:
- **Forward proxy**: Intercepts `before_upstream_connection()`
- **Reverse proxy**: Intercepts `handle_client_request()`

Both modes share the same core components through `BaseFlowProxyPlugin`.

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
