#!/bin/bash
export PATH=/home/sunjie/.nvm/versions/node/v22.21.0/bin:"$HOME/.local/bin:$PATH"
export PYTHONPATH=/home/sunjie/work/my_apps/MemOS/src
cd /home/sunjie/work/my_apps/memos-data
exec /home/sunjie/.nvm/versions/node/v22.21.0/bin/mcp-proxy --port 48001 --host 0.0.0.0 --sseEndpoint /sse -- python3 mcp_bridge.py
