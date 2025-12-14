# Quick Start Guide

## Installation

1. **Install dependencies**:

   ```bash
   poetry install
   ```

2. **Create configuration**:

   ```bash
   cp secrets.json.template secrets.json
   # Edit secrets.json with your actual credentials
   ```

3. **Secure the secrets file**:
   ```bash
   chmod 600 secrets.json
   ```

## Running the Plugin

### Basic Usage

Start with default settings (port 8899, localhost):

```bash
poetry run flow-proxy
```

### Custom Configuration

Using command-line arguments:

```bash
poetry run flow-proxy --port 9000 --host 0.0.0.0 --log-level DEBUG
```

Using environment variables:

```bash
export FLOW_PROXY_PORT=9000
export FLOW_PROXY_HOST=0.0.0.0
export FLOW_PROXY_LOG_LEVEL=DEBUG
poetry run flow-proxy
```

Using .env file:

```bash
# Create .env from template
cp .env.example .env
# Edit .env with your settings
poetry run flow-proxy
```

## Testing the Plugin

Once running, test with curl:

```bash
# Test GET request
curl -X GET http://localhost:8899/v1/models

# Test POST request
curl -X POST http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Monitoring

View logs in real-time:

```bash
tail -f flow_proxy_plugin.log
```

Check for errors:

```bash
grep ERROR flow_proxy_plugin.log
```

Monitor configuration usage:

```bash
grep "Using configuration" flow_proxy_plugin.log
```

## Stopping the Plugin

Press `Ctrl+C` to stop the plugin gracefully.

## Next Steps

- Read [CONFIG.md](CONFIG.md) for detailed configuration options
- Read [DEPLOYMENT.md](DEPLOYMENT.md) for production deployment
- Read [SECRETS_TEMPLATE.md](SECRETS_TEMPLATE.md) for secrets configuration details
