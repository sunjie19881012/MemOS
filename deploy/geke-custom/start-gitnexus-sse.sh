#!/bin/bash
# GitNexus SSE MCP Server - wraps stdio MCP as SSE via mcp-proxy
# Exposes on port 48002 for remote/local MCP clients

export PATH="/home/sunjie/.nvm/versions/node/v22.21.0/bin:$PATH"

exec mcp-proxy \
  --port 48002 \
  --host 0.0.0.0 \
  --sseEndpoint /sse \
  -- \
  /home/sunjie/.nvm/versions/node/v22.22.2/bin/node \
  /home/sunjie/.nvm/versions/node/v22.22.2/lib/node_modules/gitnexus/dist/cli/index.js \
  mcp
