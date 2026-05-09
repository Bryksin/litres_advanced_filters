#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Fix ownership and permissions
# =============================================================================

echo "Fixing ownership and permissions"
sudo chown -R appuser:appuser /home/appuser
sudo chmod 666 /var/run/docker.sock

echo "Syncing MCP servers from Cursor to Claude Code"
# =============================================================================
# Sync MCP servers from Cursor to Claude Code
#
# Cursor's mcp.json does not require a "type" field, but Claude Code does.
# This script merges Cursor's servers into Claude's config, adding the correct
# "type" automatically: "http" for URL-based servers, "stdio" for command-based.
# Existing Claude-only servers are preserved (merge, not replace).
# =============================================================================

CURSOR_MCP="$HOME/.cursor/mcp.json"
CLAUDE_CONFIG="$HOME/.claude.json"

if [ -f "$CURSOR_MCP" ]; then
  node -e "
    const fs = require('fs');
    const cursorCfg = JSON.parse(fs.readFileSync('$CURSOR_MCP', 'utf8'));
    const claudeCfg = fs.existsSync('$CLAUDE_CONFIG')
      ? JSON.parse(fs.readFileSync('$CLAUDE_CONFIG', 'utf8'))
      : {};

    claudeCfg.mcpServers = claudeCfg.mcpServers || {};

    for (const [name, server] of Object.entries(cursorCfg.mcpServers || {})) {
      if (!server.type) {
        if (server.url)     server.type = 'http';
        else if (server.command) server.type = 'stdio';
      }
      claudeCfg.mcpServers[name] = server;
    }

    fs.writeFileSync('$CLAUDE_CONFIG', JSON.stringify(claudeCfg, null, 2));
    console.log('MCP servers synced:', Object.keys(claudeCfg.mcpServers).join(', '));
  "
else
  echo "Warning: $CURSOR_MCP not found, skipping MCP sync"
fi

# =============================================================================
# Verify npm health (npm is managed at image build time)
# =============================================================================

echo "Verifying npm installation"
command -v npm >/dev/null 2>&1
npm --version >/dev/null 2>&1

# =============================================================================
# Update Claude Code to latest (official installer)
# =============================================================================

echo "Updating Claude Code"
curl -fsSL https://claude.ai/install.sh | bash
command -v claude >/dev/null 2>&1
claude --version >/dev/null 2>&1

# =============================================================================
# Install Python dependencies
# =============================================================================

echo "Installing Python dependencies"
poetry lock && poetry install
