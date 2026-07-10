# Geke Custom MCP Deploy

此目录存放格科定制版 MemOS MCP 服务的部署文件，从运行机 `/var/lib/memos-data/custom/` 同步。

## 文件说明
- `mcp_bridge.py` — MCP bridge（stdio server），mcp-proxy 调用的核心脚本
- `mcp_sse_server.py` — 旧版独立 SSE server（已被 mcp-proxy 模式替代，保留备用）
- `start-*.sh` — 各服务启动脚本
- `systemd/` — systemd user 服务文件

## 部署位置（运行机 172.16.100.100）
- 脚本部署：`/var/lib/memos-data/custom/`
- systemd：`~/.config/systemd/user/`

## Transport
- 2026-07-11: 从 SSE 切换到 Streamable HTTP (stateless)，mcp-proxy 加 `--stateless`，端点 `/mcp`
