# Python Docker Image for DevContainer Integration
# Purpose: Containerized Python development environment
# Architecture: Python 3.14 (latest stable slim-bookworm)
# Includes: Poetry, Node.js (for Playwright CLI), Playwright browsers

FROM python:3.14-slim-bookworm

# Build arguments for user management (project standard)
ARG DOCKER_UID=10101
ARG DOCKER_GID=10101
# Workspace folder name (passed from devcontainer.json via ${localWorkspaceFolderBasename})
ARG WORKSPACE_NAME=workspace

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VENV_IN_PROJECT=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache \
    PYTHONPATH=/home/appuser/${WORKSPACE_NAME}

# Install system dependencies + Node.js 22 (for Playwright CLI) + GitHub CLI
RUN apt-get update && apt-get install -y \
    bash-completion \
    build-essential \
    ca-certificates \
    curl \
    dnsutils \
    git \
    gnupg \
    iputils-ping \
    jq \
    make \
    nano \
    procps \
    ripgrep \
    sqlite3 \
    tmux \
    sudo \
    tree \
    wget \
    xxd \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright system dependencies (root; required for browser binaries)
RUN npx --yes playwright install-deps

# Install Docker CLI only (no daemon); use host Docker via mounted docker.sock
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with specific UID/GID; add to docker group for socket access on host
RUN groupadd -g ${DOCKER_GID} appuser \
    && useradd -u ${DOCKER_UID} -g ${DOCKER_GID} -m -s /bin/bash appuser \
    && groupadd -g 999 docker 2>/dev/null || true \
    && usermod -aG docker appuser 2>/dev/null || true \
    && echo 'appuser ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/appuser \
    && chmod 0440 /etc/sudoers.d/appuser

# Switch to non-root user and set working directory
USER appuser
WORKDIR /home/appuser/${WORKSPACE_NAME}

# Install Poetry for appuser
RUN curl -sSL https://install.python-poetry.org | python3 - && \
    echo 'export PATH="/home/appuser/.local/bin:$PATH"' >> ~/.bashrc && \
    rm -rf $POETRY_CACHE_DIR

# Configure Poetry
RUN /home/appuser/.local/bin/poetry config virtualenvs.create true && \
    /home/appuser/.local/bin/poetry config virtualenvs.in-project true && \
    /home/appuser/.local/bin/poetry config cache-dir /home/appuser/.cache/poetry && \
    /home/appuser/.local/bin/poetry config installer.max-workers 1

# Install Claude Code CLI for appuser (native install, goes to ~/.local/bin/claude)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install Playwright browsers for appuser (used by npx playwright / CLI)
RUN npx --yes playwright install

# Install skills CLI for appuser only (binary lands in ~/.local/bin, already in PATH)
RUN npm install -g skills

# Runtime PATH
ENV PATH="/home/appuser/${WORKSPACE_NAME}/.venv/bin:/home/appuser/.local/bin:${PATH}"

# Metadata labels
LABEL maintainer="devcontainer" \
    version="1.0.0" \
    description="Python Dev Container" \
    component="dev-container" \
    python.version="3.14" \
    node.version="22"
