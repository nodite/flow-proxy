# Secrets Configuration Template

## Quick Start

1. Copy the template file:

   ```bash
   cp secrets.json.template secrets.json
   ```

2. Edit `secrets.json` with your actual credentials

3. Secure the file:

   ```bash
   chmod 600 secrets.json
   ```

4. Add to .gitignore (already included):
   ```bash
   echo "secrets.json" >> .gitignore
   ```

## Configuration Structure

The `secrets.json` file must contain a JSON array of configuration objects. Each configuration represents a set of credentials for Flow LLM Proxy authentication.

### Minimum Configuration (Single Config)

```json
[
  {
    "clientId": "your-client-id",
    "clientSecret": "your-client-secret",
    "tenant": "your-tenant"
  }
]
```

### Recommended Configuration (Multiple Configs for Load Balancing)

```json
[
  {
    "name": "primary-config",
    "agent": "simple_agent",
    "appToAccess": "llm-api",
    "clientId": "your-client-id-1",
    "clientSecret": "your-client-secret-1",
    "tenant": "your-tenant"
  },
  {
    "name": "backup-config",
    "agent": "simple_agent",
    "appToAccess": "llm-api",
    "clientId": "your-client-id-2",
    "clientSecret": "your-client-secret-2",
    "tenant": "your-tenant"
  }
]
```

## Field Descriptions

### Required Fields (Used in JWT Token)

These fields are required and will be included in the JWT token sent to Flow LLM Proxy:

| Field          | Type   | Description                               | Example             |
| -------------- | ------ | ----------------------------------------- | ------------------- |
| `clientId`     | string | Client identifier for Flow authentication | `"abc123xyz"`       |
| `clientSecret` | string | Client secret for Flow authentication     | `"secret-key-here"` |
| `tenant`       | string | Tenant identifier for Flow authentication | `"my-tenant"`       |

### Optional Fields (For Management Only)

These fields are optional and used for logging and configuration management. They are NOT included in JWT tokens:

| Field         | Type   | Description                                                 | Example                |
| ------------- | ------ | ----------------------------------------------------------- | ---------------------- |
| `name`        | string | Human-readable name for the configuration (appears in logs) | `"production-primary"` |
| `agent`       | string | Agent identifier (organizational purposes)                  | `"simple_agent"`       |
| `appToAccess` | string | Application identifier (organizational purposes)            | `"llm-api"`            |

## Load Balancing Configuration

### Round-Robin Strategy

The plugin uses round-robin load balancing to distribute requests across all configurations:

1. **First request** → Uses first configuration
2. **Second request** → Uses second configuration
3. **Third request** → Uses third configuration
4. **Fourth request** → Cycles back to first configuration

### Configuration Examples

#### Example 1: Equal Distribution (2 Configs)

```json
[
  {
    "name": "config-a",
    "clientId": "client-a",
    "clientSecret": "secret-a",
    "tenant": "tenant-a"
  },
  {
    "name": "config-b",
    "clientId": "client-b",
    "clientSecret": "secret-b",
    "tenant": "tenant-b"
  }
]
```

**Result**: 50% of requests use config-a, 50% use config-b

#### Example 2: Weighted Distribution (3:1 Ratio)

```json
[
  {
    "name": "primary-1",
    "clientId": "primary-client-1",
    "clientSecret": "primary-secret-1",
    "tenant": "production"
  },
  {
    "name": "primary-2",
    "clientId": "primary-client-2",
    "clientSecret": "primary-secret-2",
    "tenant": "production"
  },
  {
    "name": "primary-3",
    "clientId": "primary-client-3",
    "clientSecret": "primary-secret-3",
    "tenant": "production"
  },
  {
    "name": "backup",
    "clientId": "backup-client",
    "clientSecret": "backup-secret",
    "tenant": "production"
  }
]
```

**Result**: 75% of requests use primary configs, 25% use backup

#### Example 3: High Availability (5 Configs)

```json
[
  {
    "name": "ha-config-1",
    "clientId": "ha-client-1",
    "clientSecret": "ha-secret-1",
    "tenant": "production"
  },
  {
    "name": "ha-config-2",
    "clientId": "ha-client-2",
    "clientSecret": "ha-secret-2",
    "tenant": "production"
  },
  {
    "name": "ha-config-3",
    "clientId": "ha-client-3",
    "clientSecret": "ha-secret-3",
    "tenant": "production"
  },
  {
    "name": "ha-config-4",
    "clientId": "ha-client-4",
    "clientSecret": "ha-secret-4",
    "tenant": "production"
  },
  {
    "name": "ha-config-5",
    "clientId": "ha-client-5",
    "clientSecret": "ha-secret-5",
    "tenant": "production"
  }
]
```

**Result**: Each config handles 20% of requests, high redundancy

## Failure Handling

### Automatic Failover

When a configuration fails (e.g., invalid credentials, network error):

1. The plugin marks the configuration as failed
2. The next request automatically uses the next available configuration
3. Failed configurations are skipped in subsequent rounds
4. The failure is logged with details

### Example Scenario

**Configuration**:

```json
[
    {"name": "config-1", ...},
    {"name": "config-2", ...},
    {"name": "config-3", ...}
]
```

**Behavior**:

- Request 1 → config-1 (success)
- Request 2 → config-2 (fails)
- Request 3 → config-3 (success, config-2 skipped)
- Request 4 → config-1 (success, config-2 still skipped)

## Environment-Specific Configurations

### Development Environment

```json
[
  {
    "name": "dev-local",
    "agent": "dev_agent",
    "appToAccess": "llm-api",
    "clientId": "dev-client-id",
    "clientSecret": "dev-client-secret",
    "tenant": "development"
  }
]
```

**Characteristics**:

- Single configuration
- Development credentials
- Verbose logging enabled

### Staging Environment

```json
[
  {
    "name": "staging-primary",
    "agent": "staging_agent",
    "appToAccess": "llm-api",
    "clientId": "staging-client-1",
    "clientSecret": "staging-secret-1",
    "tenant": "staging"
  },
  {
    "name": "staging-backup",
    "agent": "staging_agent",
    "appToAccess": "llm-api",
    "clientId": "staging-client-2",
    "clientSecret": "staging-secret-2",
    "tenant": "staging"
  }
]
```

**Characteristics**:

- Two configurations for redundancy
- Staging credentials
- Moderate logging

### Production Environment

```json
[
  {
    "name": "prod-primary-1",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-1",
    "clientSecret": "prod-secret-1",
    "tenant": "production"
  },
  {
    "name": "prod-primary-2",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-2",
    "clientSecret": "prod-secret-2",
    "tenant": "production"
  },
  {
    "name": "prod-primary-3",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-3",
    "clientSecret": "prod-secret-3",
    "tenant": "production"
  },
  {
    "name": "prod-backup",
    "agent": "prod_agent",
    "appToAccess": "llm-api",
    "clientId": "prod-client-backup",
    "clientSecret": "prod-secret-backup",
    "tenant": "production"
  }
]
```

**Characteristics**:

- Multiple configurations for high availability
- Production credentials
- Minimal logging (WARNING/ERROR only)

## Security Best Practices

### File Permissions

Always restrict access to secrets.json:

```bash
# Owner read/write only
chmod 600 secrets.json

# Verify permissions
ls -la secrets.json
# Should show: -rw------- 1 user group ... secrets.json
```

### Version Control

Never commit secrets.json to version control:

```bash
# Verify it's in .gitignore
grep secrets.json .gitignore

# Check git status
git status
# secrets.json should NOT appear
```

### Credential Rotation

Regularly rotate credentials:

1. Generate new credentials in Flow system
2. Add new configuration to secrets.json
3. Restart plugin
4. Verify new configuration works
5. Remove old configuration
6. Restart plugin again
7. Revoke old credentials in Flow system

### Encryption at Rest

For production environments:

1. Store secrets.json on encrypted file system
2. Use secrets management tools (HashiCorp Vault, AWS Secrets Manager)
3. Implement access auditing
4. Use environment-specific credentials

## Validation

The plugin validates your configuration on startup. Common validation errors:

### Error: File Not Found

```
ERROR - Secrets file not found: secrets.json
```

**Solution**: Create secrets.json from template

### Error: Invalid JSON

```
ERROR - Invalid JSON format in secrets.json
```

**Solution**: Validate JSON syntax using `jq` or online validator

```bash
jq . secrets.json
```

### Error: Not an Array

```
ERROR - secrets.json must contain an array of configurations
```

**Solution**: Ensure root element is an array `[...]`, not an object `{...}`

### Error: Missing Required Field

```
ERROR - Configuration missing required field: clientId
```

**Solution**: Ensure all configurations have clientId, clientSecret, and tenant

### Error: Empty Array

```
ERROR - secrets.json must contain at least one configuration
```

**Solution**: Add at least one configuration to the array

## Testing Your Configuration

### Validate JSON Syntax

```bash
# Using jq
jq . secrets.json

# Using Python
python -m json.tool secrets.json
```

### Test with Single Configuration

Start with a single configuration to verify credentials:

```json
[
  {
    "name": "test-config",
    "clientId": "your-client-id",
    "clientSecret": "your-client-secret",
    "tenant": "your-tenant"
  }
]
```

### Test Load Balancing

Add multiple configurations and monitor logs:

```bash
# Start with debug logging
poetry run flow-proxy --log-level DEBUG

# Watch logs for configuration rotation
tail -f flow_proxy_plugin.log | grep "Using configuration"
```

## Troubleshooting

### Configuration Not Loading

1. Check file exists: `ls -la secrets.json`
2. Check file permissions: `ls -la secrets.json`
3. Validate JSON: `jq . secrets.json`
4. Check logs: `tail -f flow_proxy_plugin.log`

### Authentication Failures

1. Verify credentials are correct
2. Check credentials haven't expired
3. Verify tenant is correct
4. Test with single configuration first

### Load Balancing Not Working

1. Ensure multiple configurations exist
2. Check logs show configuration rotation
3. Verify all configurations have unique names
4. Monitor with DEBUG logging

## Additional Resources

- [Configuration Guide](CONFIG.md) - Comprehensive configuration documentation
- [README](README.md) - General usage and setup
- [Flow LLM Proxy Documentation](https://flow.ciandt.com/docs) - Flow authentication details
