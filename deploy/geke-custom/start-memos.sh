#!/bin/bash
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH=/home/sunjie/work/my_apps/MemOS/src
export HF_HUB_OFFLINE=1
cd /home/sunjie/work/my_apps/MemOS
cp /home/sunjie/work/my_apps/memos-data/.env /home/sunjie/work/my_apps/MemOS/.env
exec uvicorn memos.api.server_api:app --host 127.0.0.1 --port 38001
