# Deployment Guide

## Overview

This guide covers deploying Flow Proxy Plugin in various environments, from development to production.

## Quick Start

### Development Environment

1. **Clone and setup**:

   ```bash
   git clone <repository-url>
   cd flow-proxy-plugin
   poetry install
   ```

2. **Configure secrets**:

   ```bash
   cp secrets.json.template secrets.json
   # Edit secrets.json with your credentials
   ```

3. **Start the service**:

   ```bash
   ./scripts/start.sh
   ```

4. **Check status**:

   ```bash
   ./scripts/status.sh
   ```

5. **Stop the service**:
   ```bash
   ./scripts/stop.sh
   ```

## Deployment Methods

### Method 1: Manual Deployment (Development/Testing)

Best for: Development, testing, small deployments

**Steps**:

1. Install dependencies:

   ```bash
   poetry install
   ```

2. Configure environment:

   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

3. Setup secrets:

   ```bash
   cp secrets.json.template secrets.json
   chmod 600 secrets.json
   # Edit secrets.json with credentials
   ```

4. Start service:
   ```bash
   ./scripts/start.sh
   ```

**Pros**: Simple, quick setup
**Cons**: No automatic restart, manual management

### Method 2: Systemd Service (Production Linux)

Best for: Production Linux servers, automatic restart, system integration

**Steps**:

1. Create service user:

   ```bash
   sudo useradd -r -s /bin/false flow-proxy
   ```

2. Install application:

   ```bash
   sudo mkdir -p /opt/flow-proxy-plugin
   sudo cp -r . /opt/flow-proxy-plugin/
   sudo chown -R flow-proxy:flow-proxy /opt/flow-proxy-plugin
   ```

3. Setup configuration:

   ```bash
   sudo mkdir -p /etc/flow-proxy
   sudo cp secrets.json /etc/flow-proxy/
   sudo chmod 600 /etc/flow-proxy/secrets.json
   sudo chown flow-proxy:flow-proxy /etc/flow-proxy/secrets.json
   ```

4. Create environment file:

   ```bash
   sudo tee /etc/flow-proxy/flow-proxy.env << EOF
   FLOW_PROXY_PORT=8899
   FLOW_PROXY_HOST=127.0.0.1
   FLOW_PROXY_LOG_LEVEL=WARNING
   FLOW_PROXY_SECRETS_FILE=/etc/flow-proxy/secrets.json
   EOF
   ```

5. Install systemd service:

   ```bash
   sudo cp scripts/flow-proxy-plugin.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

6. Enable and start service:

   ```bash
   sudo systemctl enable flow-proxy-plugin
   sudo systemctl start flow-proxy-plugin
   ```

7. Check status:
   ```bash
   sudo systemctl status flow-proxy-plugin
   sudo journalctl -u flow-proxy-plugin -f
   ```

**Pros**: Automatic restart, system integration, proper logging
**Cons**: Requires root access, Linux-specific

### Method 3: Docker Container (Cross-platform)

Best for: Containerized environments, Kubernetes, cloud deployments

**Create Dockerfile**:

```dockerfile
FROM python:3.10-slim

# Install Poetry
RUN pip install poetry

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml poetry.lock ./
COPY flow_proxy_plugin ./flow_proxy_plugin

# Install dependencies
RUN poetry config virtualenvs.create false \
    && poetry install --no-dev --no-interaction --no-ansi

# Create non-root user
RUN useradd -m -u 1000 flow-proxy && \
    chown -R flow-proxy:flow-proxy /app

USER flow-proxy

# Expose port
EXPOSE 8899

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8899/ || exit 1

# Start command
CMD ["poetry", "run", "flow-proxy"]
```

**Build and run**:

```bash
# Build image
docker build -t flow-proxy-plugin:latest .

# Run container
docker run -d \
    --name flow-proxy \
    -p 8899:8899 \
    -v $(pwd)/secrets.json:/app/secrets.json:ro \
    -e FLOW_PROXY_LOG_LEVEL=INFO \
    flow-proxy-plugin:latest
```

**Docker Compose**:

```yaml
version: "3.8"

services:
  flow-proxy:
    build: .
    container_name: flow-proxy-plugin
    ports:
      - "8899:8899"
    volumes:
      - ./secrets.json:/app/secrets.json:ro
      - ./logs:/app/logs
    environment:
      - FLOW_PROXY_PORT=8899
      - FLOW_PROXY_HOST=0.0.0.0
      - FLOW_PROXY_LOG_LEVEL=INFO
      - FLOW_PROXY_SECRETS_FILE=/app/secrets.json
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8899/"]
      interval: 30s
      timeout: 3s
      retries: 3
```

**Pros**: Portable, isolated, easy scaling
**Cons**: Requires Docker knowledge, additional overhead

### Method 4: Process Manager (PM2, Supervisor)

Best for: Node.js environments, multiple services

**Using PM2**:

```bash
# Install PM2
npm install -g pm2

# Create ecosystem file
cat > ecosystem.config.js << EOF
module.exports = {
  apps: [{
    name: 'flow-proxy-plugin',
    script: 'poetry',
    args: 'run flow-proxy',
    cwd: '/path/to/flow-proxy-plugin',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      FLOW_PROXY_PORT: 8899,
      FLOW_PROXY_HOST: '127.0.0.1',
      FLOW_PROXY_LOG_LEVEL: 'INFO'
    }
  }]
}
EOF

# Start with PM2
pm2 start ecosystem.config.js

# Save PM2 configuration
pm2 save

# Setup PM2 startup
pm2 startup
```

**Pros**: Easy management, monitoring, auto-restart
**Cons**: Requires PM2/Node.js

## Environment-Specific Configurations

### Development

```bash
# .env
FLOW_PROXY_PORT=9000
FLOW_PROXY_HOST=127.0.0.1
FLOW_PROXY_LOG_LEVEL=DEBUG
FLOW_PROXY_SECRETS_FILE=secrets.dev.json
```

**Characteristics**:

- Debug logging enabled
- Local binding only
- Development credentials
- Single configuration

### Staging

```bash
# .env
FLOW_PROXY_PORT=8899
FLOW_PROXY_HOST=0.0.0.0
FLOW_PROXY_LOG_LEVEL=INFO
FLOW_PROXY_SECRETS_FILE=/etc/flow-proxy/secrets.staging.json
```

**Characteristics**:

- Info logging
- Network accessible
- Staging credentials
- Multiple configurations for testing load balancing

### Production

```bash
# .env
FLOW_PROXY_PORT=8899
FLOW_PROXY_HOST=0.0.0.0
FLOW_PROXY_LOG_LEVEL=WARNING
FLOW_PROXY_SECRETS_FILE=/etc/flow-proxy/secrets.json
FLOW_PROXY_LOG_FILE=/var/log/flow-proxy/flow-proxy.log
```

**Characteristics**:

- Warning/Error logging only
- Network accessible
- Production credentials
- Multiple configurations for high availability
- Secure file permissions
- Monitoring and alerting

## Security Considerations

### File Permissions

```bash
# Secrets file
chmod 600 secrets.json
chown flow-proxy:flow-proxy secrets.json

# Configuration directory
chmod 750 /etc/flow-proxy
chown flow-proxy:flow-proxy /etc/flow-proxy

# Log directory
chmod 755 /var/log/flow-proxy
chown flow-proxy:flow-proxy /var/log/flow-proxy
```

### Network Security

1. **Firewall rules**:

   ```bash
   # Allow only from specific IPs
   sudo ufw allow from 10.0.0.0/8 to any port 8899

   # Or use iptables
   sudo iptables -A INPUT -p tcp -s 10.0.0.0/8 --dport 8899 -j ACCEPT
   sudo iptables -A INPUT -p tcp --dport 8899 -j DROP
   ```

2. **Reverse proxy** (nginx):

   ```nginx
   upstream flow_proxy {
       server 127.0.0.1:8899;
   }

   server {
       listen 443 ssl;
       server_name flow-proxy.example.com;

       ssl_certificate /etc/ssl/certs/flow-proxy.crt;
       ssl_certificate_key /etc/ssl/private/flow-proxy.key;

       location / {
           proxy_pass http://flow_proxy;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

### Secrets Management

**Option 1: Environment Variables**

```bash
# Don't store in secrets.json, use env vars
export FLOW_CLIENT_ID="..."
export FLOW_CLIENT_SECRET="..."
export FLOW_TENANT="..."
```

**Option 2: HashiCorp Vault**

```bash
# Fetch secrets from Vault
vault kv get -field=clientId secret/flow-proxy/config1
```

**Option 3: AWS Secrets Manager**

```bash
# Fetch from AWS
aws secretsmanager get-secret-value --secret-id flow-proxy/secrets
```

## Monitoring and Logging

### Log Management

1. **Log rotation** (logrotate):

   ```bash
   sudo tee /etc/logrotate.d/flow-proxy << EOF
   /var/log/flow-proxy/*.log {
       daily
       rotate 14
       compress
       delaycompress
       notifempty
       create 0640 flow-proxy flow-proxy
       sharedscripts
       postrotate
           systemctl reload flow-proxy-plugin
       endscript
   }
   EOF
   ```

2. **Centralized logging** (rsyslog):
   ```bash
   # Forward logs to central server
   *.* @@log-server.example.com:514
   ```

### Monitoring

1. **Health check endpoint**:

   ```bash
   curl http://localhost:8899/health
   ```

2. **Prometheus metrics** (if implemented):

   ```bash
   curl http://localhost:8899/metrics
   ```

3. **Log monitoring**:

   ```bash
   # Watch for errors
   tail -f /var/log/flow-proxy/flow-proxy.log | grep ERROR

   # Count requests per config
   grep "Using configuration" flow_proxy_plugin.log | \
       awk '{print $NF}' | sort | uniq -c
   ```

## Scaling and High Availability

### Horizontal Scaling

1. **Load balancer** (HAProxy):

   ```
   frontend flow_proxy_frontend
       bind *:8899
       default_backend flow_proxy_backend

   backend flow_proxy_backend
       balance roundrobin
       server proxy1 10.0.1.10:8899 check
       server proxy2 10.0.1.11:8899 check
       server proxy3 10.0.1.12:8899 check
   ```

2. **Multiple instances**:

   ```bash
   # Instance 1
   FLOW_PROXY_PORT=8899 ./scripts/start.sh

   # Instance 2
   FLOW_PROXY_PORT=8900 ./scripts/start.sh

   # Instance 3
   FLOW_PROXY_PORT=8901 ./scripts/start.sh
   ```

### Configuration Redundancy

Use multiple authentication configurations in secrets.json:

```json
[
    {"name": "primary-1", ...},
    {"name": "primary-2", ...},
    {"name": "primary-3", ...},
    {"name": "backup-1", ...},
    {"name": "backup-2", ...}
]
```

## Troubleshooting

### Service Won't Start

1. Check logs:

   ```bash
   tail -f flow_proxy_plugin.log
   # or
   sudo journalctl -u flow-proxy-plugin -f
   ```

2. Verify configuration:

   ```bash
   jq . secrets.json
   ```

3. Check port availability:
   ```bash
   netstat -tuln | grep 8899
   ```

### High Memory Usage

1. Check process:

   ```bash
   ps aux | grep flow-proxy
   ```

2. Restart service:
   ```bash
   ./scripts/restart.sh
   # or
   sudo systemctl restart flow-proxy-plugin
   ```

### Authentication Failures

1. Verify credentials in secrets.json
2. Check Flow LLM Proxy connectivity
3. Review logs for specific errors
4. Test with single configuration first

## Backup and Recovery

### Backup Configuration

```bash
# Backup secrets
sudo cp /etc/flow-proxy/secrets.json /backup/secrets.json.$(date +%Y%m%d)

# Backup environment
sudo cp /etc/flow-proxy/flow-proxy.env /backup/flow-proxy.env.$(date +%Y%m%d)
```

### Recovery

```bash
# Restore secrets
sudo cp /backup/secrets.json.20240115 /etc/flow-proxy/secrets.json
sudo chmod 600 /etc/flow-proxy/secrets.json
sudo chown flow-proxy:flow-proxy /etc/flow-proxy/secrets.json

# Restart service
sudo systemctl restart flow-proxy-plugin
```

## Upgrade Procedure

1. **Backup current version**:

   ```bash
   cp -r /opt/flow-proxy-plugin /opt/flow-proxy-plugin.backup
   ```

2. **Stop service**:

   ```bash
   sudo systemctl stop flow-proxy-plugin
   ```

3. **Update code**:

   ```bash
   cd /opt/flow-proxy-plugin
   git pull
   poetry install
   ```

4. **Test configuration**:

   ```bash
   poetry run flow-proxy --help
   ```

5. **Start service**:

   ```bash
   sudo systemctl start flow-proxy-plugin
   ```

6. **Verify**:
   ```bash
   sudo systemctl status flow-proxy-plugin
   ```

## Support and Maintenance

### Regular Maintenance Tasks

1. **Weekly**:

   - Review logs for errors
   - Check disk space
   - Verify all configurations working

2. **Monthly**:

   - Rotate credentials
   - Update dependencies
   - Review performance metrics

3. **Quarterly**:
   - Security audit
   - Capacity planning
   - Disaster recovery test

### Getting Help

1. Check logs with DEBUG level
2. Review configuration files
3. Test network connectivity
4. Consult documentation
5. Open issue on GitHub
