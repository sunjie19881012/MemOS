"""审计日志缓冲。

首版为进程级内存实现,代码结构预留 Redis 升级位:
后续若需跨 worker 共享,实现 RedisAuditBuffer 替换 audit_buffer 单例即可。

环境变量:
    DASHBOARD_AUDIT_MAXLEN: 缓冲容量,默认 1000
    DASHBOARD_AUDIT_FILTER: 是否过滤 Dashboard/健康检查等自指路径,默认 false
"""

import os
import threading
from collections import deque
from typing import Any


# 缓冲容量:高频场景下 1000 条约可保留最近一段时间的请求
_MAXLEN = int(os.getenv("DASHBOARD_AUDIT_MAXLEN", "1000"))
_BUFFER: deque[dict[str, Any]] = deque(maxlen=_MAXLEN)
_LOCK = threading.Lock()

# Dashboard 自身流量与健康检查探活,高频但无审计价值。
# 预留过滤能力,首版默认关闭(纯个人使用频率极低,留着也不会刷屏)。
_EXCLUDE_PATHS = {"/health", "/product/requests", "/product/config", "/dashboard"}
_FILTER_ENABLED = os.getenv("DASHBOARD_AUDIT_FILTER", "false").lower() == "true"


class MemoryAuditBuffer:
    """进程级内存环形缓冲实现。

    CPython 下 deque.append 因 GIL 是原子的,这里加 Lock 主要是
    为后续扩展(批量写、读时清理)预留并发安全边界。
    """

    def append(self, record: dict[str, Any]) -> None:
        """追加一条审计记录。record 应为不可变快照,调用方负责构造完再传入。"""
        if _FILTER_ENABLED and record.get("path") in _EXCLUDE_PATHS:
            return
        with _LOCK:
            _BUFFER.append(record)

    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回最近 limit 条记录(按时间正序,最新的在末尾)。"""
        with _LOCK:
            snapshot = list(_BUFFER)
        return snapshot[-limit:] if limit < len(snapshot) else snapshot

    def clear(self) -> None:
        """清空缓冲(仅用于测试或手动重置)。"""
        with _LOCK:
            _BUFFER.clear()


# 预留:Redis 升级位。后续实现 RedisAuditBuffer 类(基于项目已有的 redis_client),
# 替换下方单例即可,中间件与端点代码无需改动。
#
# class RedisAuditBuffer:
#     def __init__(self, redis_client, key="memos:audit", maxlen=1000):
#         self.redis = redis_client
#         self.key = key
#         self.maxlen = maxlen
#     def append(self, record): self.redis.xadd(self.key, record, maxlen=self.maxlen)
#     def get_recent(self, limit=100): ...


# 模块级单例:中间件写入、端点读取,跨模块共享同一实例
audit_buffer = MemoryAuditBuffer()
