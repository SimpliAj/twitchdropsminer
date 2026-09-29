FROM python:3.12-slim

# Build arguments for metadata
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION

# Labels following OCI Image Format Specification
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.authors="SimpliAj" \
      org.opencontainers.image.url="https://github.com/SimpliAj/twitchdropsminer" \
      org.opencontainers.image.documentation="https://github.com/SimpliAj/twitchdropsminer/blob/main/README.md" \
      org.opencontainers.image.source="https://github.com/SimpliAj/twitchdropsminer" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="SimpliAj" \
      org.opencontainers.image.title="Twitch Drops Miner (SimpliAj Fork)" \
      org.opencontainers.image.description="TwitchDropsMiner fork with channel points auto-claimer, idle watch, multi-account support and Discord webhooks"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Set working directory
WORKDIR /app

# Install system dependencies:
# - tzdata: unchanged from before
# - xvfb: virtual X display the real-browser login runs under (see
#   src/auth/browser_login.py)
# - x11vnc: exposes that virtual display over VNC for the dashboard's
#   embedded noVNC viewer
# - websockify: bridges x11vnc's raw VNC protocol to a WebSocket noVNC's
#   JS client can consume directly
# - procps: provides pgrep, which sweep_orphaned_processes() shells out to on
#   every startup. Debian slim images do NOT ship it, and without it that
#   sweep raises FileNotFoundError.
# - Playwright's own --with-deps (below) pulls in Chromium's shared-library
#   requirements; this base image change (alpine -> slim) is what makes
#   that possible at all, since Chromium needs glibc and alpine ships musl
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    xvfb \
    x11vnc \
    websockify \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy project metadata and install dependencies
COPY pyproject.toml .

# Install Python dependencies (playwright is now a base dependency, see
# pyproject.toml)
RUN pip install --no-cache-dir .

# Install Playwright's bundled Chromium and its remaining native deps
RUN playwright install --with-deps chromium

# Copy application code
COPY main.py ./
COPY src/ ./src/
COPY lang/ ./lang/
COPY icons/ ./icons/
COPY web/ ./web/

# Create data directory for persistent storage
RUN mkdir -p /app/data && chmod 777 /app/data
RUN mkdir -p /app/logs && chmod 777 /app/logs

# Expose web port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=2)" || exit 1

# Run the application (web GUI is now default)
CMD ["python", "main.py"]
