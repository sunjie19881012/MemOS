#!/bin/bash
exec /home/sunjie/work/my_apps/llama.cpp/build/bin/llama-server \
  -m /home/sunjie/work/my_apps/memos-data/bge-m3-Q8_0.gguf \
  --host 127.0.0.1 --port 38081 -ngl 0 --embeddings -c 8192 -b 8192 -ub 8192 --no-webui
