# NTRIP Caster Docker image
FROM python:3.12-slim AS builder

# Set build environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Production image
FROM python:3.12-slim AS production

# Set metadata
LABEL maintainer="2rtk <i@jia.by>" \
      version="2.2.0" \
      description="High-performance NTRIP Caster with RTCM parsing"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    TZ=UTC

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    ca-certificates \
    tzdata \
    tini \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r -g 1000 ntrip && \
    useradd -r -u 1000 -g ntrip -d /app -s /bin/bash ntrip

WORKDIR /app

# Copy virtualenv and app from builder
COPY --from=builder --chown=ntrip:ntrip /app/.venv /app/.venv
COPY --chown=ntrip:ntrip . .

# Create directories and set permissions
RUN mkdir -p /app/logs /app/data /app/config && \
    chown -R ntrip:ntrip /app && \
    chmod -R 755 /app

# Volume mounts
VOLUME ["/app/logs", "/app/data", "/app/config"]

# Ports
EXPOSE 2101 5757

# Healthcheck
HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=3 \
    CMD python healthcheck.py || exit 1

# Entrypoint script
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
mkdir -p /app/logs /app/data /app/config\n\
chown -R ntrip:ntrip /app/logs /app/data /app/config\n\
\n\
if [ ! -f "/app/config/config.ini" ]; then\n\
    cp /app/config.ini.example /app/config/config.ini\n\
    chown ntrip:ntrip /app/config/config.ini\n\
fi\n\
\n\
export NTRIP_CONFIG_FILE="/app/config/config.ini"\n\
\n\
exec gosu ntrip ntrip-caster' > /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh && \
    chown ntrip:ntrip /app/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
