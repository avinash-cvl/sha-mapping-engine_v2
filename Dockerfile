# ==============================================================================
# Dockerfile for sha-mapping-engine_v2
# Base: Python 3.12 (Debian Bookworm) + uv + Microsoft ODBC Driver 18
# ==============================================================================
FROM python:3.12-slim-bookworm

# Install uv directly from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Configure Python, uv virtualenv, and runtime settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    CONSOLE_HOST=0.0.0.0 \
    CONSOLE_PORT=8099 \
    CONSOLE_RELOAD=false

# ------------------------------------------------------------------------------
# Install system build dependencies & Microsoft ODBC Driver 18 for SQL Server
# ------------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        ca-certificates \
        gcc \
        g++ \
        unixodbc \
        unixodbc-dev && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | \
        gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------------
# Install Python dependencies using uv (exact, locked via uv.lock)
# ------------------------------------------------------------------------------
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# ------------------------------------------------------------------------------
# Copy application code
# ------------------------------------------------------------------------------
COPY . .

# Ensure storage directories exist for runs, caches, and logs
RUN mkdir -p /app/data /app/cache /app/logs

# Expose API and web console port
EXPOSE 8099

# Container Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8099/health || exit 1

# Start the FastAPI engine console application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8099"]
