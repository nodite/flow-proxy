# Flow Proxy Plugin

A proxy.py plugin for Flow LLM Proxy authentication and request forwarding with round-robin load balancing.

## Features

- **Authentication Management**: Automatically handles Flow LLM Proxy authentication using JWT tokens
- **Load Balancing**: Round-robin load balancing across multiple authentication configurations
- **Transparent Proxying**: Seamless request forwarding to Flow LLM Proxy service
- **Error Handling**: Comprehensive error handling and logging
- **Configuration Management**: JSON-based configuration with validation

## Installation

### Prerequisites

- Python 3.8+
- Poetry (for dependency management)

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd flow-proxy-plugin
```

2. Install dependencies using Poetry:
```bash
poetry install
```

3. Activate the virtual environment:
```bash
poetry shell
```

## Configuration

Create a `secrets.json` file in the project root with your authentication configurations:

```json
[
    {
        "name": "config1",
        "agent": "simple_agent",
        "appToAccess": "llm-api",
        "clientId": "your-client-id-1",
        "clientSecret": "your-client-secret-1",
        "tenant": "your-tenant-1"
    },
    {
        "name": "config2",
        "agent": "simple_agent",
        "appToAccess": "llm-api",
        "clientId": "your-client-id-2",
        "clientSecret": "your-client-secret-2",
        "tenant": "your-tenant-2"
    }
]
```

## Usage

### Using Poetry Script

```bash
poetry run flow-proxy --port 8899 --host 127.0.0.1
```

### Using CLI Module

```bash
python -m flow_proxy_plugin.cli --port 8899 --log-level INFO
```

### Command Line Options

- `--port`: Port to listen on (default: 8899)
- `--host`: Host to bind to (default: 127.0.0.1)
- `--log-level`: Logging level (DEBUG, INFO, WARNING, ERROR)
- `--secrets-file`: Path to secrets.json file (default: secrets.json)

## Development

### Code Quality Tools

This project uses several code quality tools configured via Poetry:

- **Ruff**: Fast Python linter and formatter
- **MyPy**: Static type checking
- **Pylint**: Code quality analysis
- **Commitizen**: Conventional commit messages

### Pre-commit Hooks

Install pre-commit hooks:

```bash
poetry run pre-commit install
```

Run pre-commit on all files:

```bash
poetry run pre-commit run --all-files
```

### Testing

Run tests:

```bash
poetry run pytest
```

Run tests with coverage:

```bash
poetry run pytest --cov=flow_proxy_plugin
```

### Commit Guidelines

This project uses Conventional Commits. Use commitizen for consistent commit messages:

```bash
poetry run cz commit
```

## Architecture

The plugin consists of several key components:

- **FlowProxyPlugin**: Main plugin class that integrates with proxy.py
- **SecretsManager**: Manages loading and validation of authentication configurations
- **LoadBalancer**: Implements round-robin load balancing strategy
- **JWTGenerator**: Generates JWT tokens for Flow LLM Proxy authentication
- **RequestForwarder**: Handles request modification and forwarding

## Load Balancing

The plugin uses a round-robin strategy to distribute requests across multiple authentication configurations:

1. Requests are processed sequentially using different configurations
2. Failed configurations are automatically marked and skipped
3. Configuration usage is logged for monitoring and debugging
4. Failed configurations can be reset and reused

## Logging

The plugin provides comprehensive logging:

- Configuration loading and validation
- Load balancing decisions and configuration usage
- JWT token generation events
- Request forwarding operations
- Error conditions and debugging information

## License

[Add your license information here]
