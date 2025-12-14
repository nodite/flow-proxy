# Configuration Guide

## Overview

FlowProxyPlugin uses a JSON-based configuration system with support for multiple authentication configurations and round-robin load balancing. This guide explains all configuration options and best practices.

## Configuration Files

### secrets.json

The main configuration file containing authentication credentials for Flow LLM Proxy. This file should be kept secure and never committed to version control.

**Location**: Project root directory (default: `secrets.json`)

**Format**: JSON array of configuration objects

**Example**:

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

### Configuration Fields

#### Required Fields

These fields are required for JWT token generation:

- **clientId** (string): Client identifier for Flow authentication
- **clientSecret** (string): Client secret for Flow authentication
- **tenant** (string): Tenant identifier for Flow authentication

#### Optional Fields

These fields are used for logging and configuration management but are not included in JWT tokens:

- **name** (string): Human-readable name for the configuration (used in logs)
- **agent** (string): Agent identifier (for organizational purposes)
- **appToAccess** (string): Application identifier (for organizational purposes)

### Environment Variables

You can override default configuration using environment variables:

- **FLOW_PROXY_PORT**: Port to listen on (default: 8899)
- **FLOW_PROXY_HOST**: Host to bind to (default: 127.0.0.1)
- **FLOW_PROXY_LOG_LEVEL**: Logging level (DEBUG, INFO, WARNING, ERROR)
- **FLOW_PROXY_SECRETS_FILE**: Path to secrets.json file (default: secrets.json)

**Example**:

```bash
export FLOW_PROXY_PORT=9000
export FLOW_PROXY_HOST=0.0.0.0
export FLOW_PROXY_LOG_LEVEL=DEBUG
export FLOW_PROXY_SECRETS_FILE=/path/to/secrets.json
```

## Load Balancing Configuration

### Round-Robin Strategy

The plugin automatically implements round-robin load balancing across all configurations in the secrets.json array:

1. **Sequential Distribution**: Requests are distributed sequentially across configurations
2. **Automatic Cycling**: When the last configuration is reached, the cycle restarts from the first
3. **Failure Handling**: Failed configurations are automatically skipped
4. **Configuration Logging**: Each request logs which configuration is being used

### Configuration Order

Configurations are used in the order they appear in the secrets.json array. To prioritize certain configurations:

1. Place higher-priority configurations earlier in the array
2. Use multiple instances of the same configuration for weighted distribution

**Example - Weighted Distribution**:

```json
[
    {"name": "primary-1", "clientId": "...", ...},
    {"name": "primary-2", "clientId": "...", ...},
    {"name": "primary-3", "clientId": "...", ...},
    {"name": "backup", "clientId": "...", ...}
]
```

In this example, primary configurations will receive 75% of requests, backup will receive 25%.

## Security Best Practices

### Protecting Secrets

1. **Never commit secrets.json**: Add to .gitignore
2. **Use file permissions**: Restrict read access to secrets.json
   ```bash
   chmod 600 secrets.json
   ```
3. **Rotate credentials regularly**: Update clientId and clientSecret periodically
4. **Use separate configs per environment**: Different secrets.json for dev/staging/prod

### Secure Deployment

1. **Use environment variables**: Override sensitive paths in production
2. **Encrypt at rest**: Use encrypted file systems for secrets.json
3. **Audit access**: Monitor who accesses configuration files
4. **Use secrets management**: Consider using tools like HashiCorp Vault or AWS Secrets Manager

## Configuration Validation

The plugin validates configuration on startup:

### Validation Checks

1. **File exists**: secrets.json must be present
2. **Valid JSON**: File must contain valid JSON
3. **Array format**: Root element must be an array
4. **Non-empty**: Array must contain at least one configuration
5. **Required fields**: Each configuration must have clientId, clientSecret, and tenant
6. **Field types**: All fields must be strings

### Validation Errors

If validation fails, the plugin will:

1. Log detailed error messages
2. Refuse to start
3. Exit with error code 1

**Example Error Messages**:

```
ERROR - Secrets file not found: secrets.json
ERROR - Invalid JSON format in secrets.json
ERROR - secrets.json must contain an array of configurations
ERROR - Configuration missing required field: clientId
```

## Logging Configuration

### Log Levels

- **DEBUG**: Detailed information for debugging (includes all requests/responses)
- **INFO**: General information about operations (default)
- **WARNING**: Warning messages for potential issues
- **ERROR**: Error messages for failures

### Log Output

Logs are written to:

1. **Console**: stdout (for monitoring)
2. **File**: flow_proxy_plugin.log (for persistence)

### Log Format

```
%(asctime)s - %(name)s - %(levelname)s - %(message)s
```

**Example**:

```
2024-01-15 10:30:45,123 - flow_proxy_plugin.load_balancer - INFO - Using configuration: config1
2024-01-15 10:30:45,456 - flow_proxy_plugin.jwt_generator - INFO - Generated JWT token for config: config1
```

### What Gets Logged

- **Startup**: Plugin initialization and configuration loading
- **Load Balancing**: Which configuration is selected for each request
- **Authentication**: JWT token generation events
- **Requests**: Request forwarding operations
- **Errors**: All error conditions with context
- **Shutdown**: Plugin shutdown events

## Advanced Configuration

### Multiple Tenants

To support multiple tenants, create separate configurations for each:

```json
[
  {
    "name": "tenant-a-primary",
    "clientId": "tenant-a-client-1",
    "clientSecret": "tenant-a-secret-1",
    "tenant": "tenant-a"
  },
  {
    "name": "tenant-a-backup",
    "clientId": "tenant-a-client-2",
    "clientSecret": "tenant-a-secret-2",
    "tenant": "tenant-a"
  },
  {
    "name": "tenant-b-primary",
    "clientId": "tenant-b-client-1",
    "clientSecret": "tenant-b-secret-1",
    "tenant": "tenant-b"
  }
]
```

### High Availability Setup

For high availability:

1. **Multiple Configurations**: Use at least 3 configurations per tenant
2. **Health Monitoring**: Monitor logs for failed configurations
3. **Automatic Failover**: Plugin automatically skips failed configs
4. **Configuration Reset**: Failed configs are periodically retried

### Performance Tuning

1. **Configuration Count**: More configs = better load distribution
2. **Log Level**: Use INFO or WARNING in production for better performance
3. **File Location**: Place secrets.json on fast storage (SSD)

## Troubleshooting

### Common Issues

**Issue**: Plugin fails to start

- **Check**: secrets.json exists and is readable
- **Check**: JSON format is valid
- **Check**: All required fields are present

**Issue**: Authentication failures

- **Check**: clientId, clientSecret, and tenant are correct
- **Check**: Credentials haven't expired
- **Check**: Network connectivity to Flow LLM Proxy

**Issue**: Load balancing not working

- **Check**: Multiple configurations are defined
- **Check**: Configurations have unique names
- **Check**: Logs show configuration rotation

### Debug Mode

Enable debug logging for detailed troubleshooting:

```bash
poetry run flow-proxy --log-level DEBUG
```

Or with environment variable:

```bash
export FLOW_PROXY_LOG_LEVEL=DEBUG
poetry run flow-proxy
```

## Configuration Examples

### Development Environment

```json
[
  {
    "name": "dev-config",
    "agent": "dev_agent",
    "appToAccess": "llm-api",
    "clientId": "dev-client-id",
    "clientSecret": "dev-client-secret",
    "tenant": "dev-tenant"
  }
]
```

### Production Environment

```json
[
  {
    "name": "prod-primary-1",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-id-1",
    "clientSecret": "prod-client-secret-1",
    "tenant": "production"
  },
  {
    "name": "prod-primary-2",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-id-2",
    "clientSecret": "prod-client-secret-2",
    "tenant": "production"
  },
  {
    "name": "prod-backup",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-id-3",
    "clientSecret": "prod-client-secret-3",
    "tenant": "production"
  }
]
```

### Testing Environment

```json
[
  {
    "name": "test-config-1",
    "agent": "test_agent",
    "appToAccess": "llm-api",
    "clientId": "test-client-id-1",
    "clientSecret": "test-client-secret-1",
    "tenant": "test"
  },
  {
    "name": "test-config-2",
    "agent": "test_agent",
    "appToAccess": "llm-api",
    "clientId": "test-client-id-2",
    "clientSecret": "test-client-secret-2",
    "tenant": "test"
  }
]
```

## Migration Guide

### From Single Configuration

If you're migrating from a single configuration setup:

1. Convert your single config to an array:

   ```json
   {
     "clientId": "...",
     "clientSecret": "...",
     "tenant": "..."
   }
   ```

   Becomes:

   ```json
   [
     {
       "name": "config1",
       "clientId": "...",
       "clientSecret": "...",
       "tenant": "..."
     }
   ]
   ```

2. Add additional configurations for load balancing
3. Test with a single config first, then add more

### Updating Configurations

To update configurations without downtime:

1. Add new configurations to the array
2. Restart the plugin
3. Monitor logs to ensure new configs are working
4. Remove old configurations
5. Restart again to clean up

## Support

For issues or questions:

1. Check the troubleshooting section
2. Review logs with DEBUG level enabled
3. Verify configuration against examples
4. Check network connectivity to Flow LLM Proxy
