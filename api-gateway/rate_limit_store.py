"""网关限流存储：Redis 可选，进程内滑动窗口兜底。

设计目标：
- 不配置 REDIS_URL 时保持原有单机行为（内存滑动窗口）。
- 配置 REDIS_URL 后使用 Redis Sorted Set 实现跨实例共享的滑动窗口，
  便于网关水平扩容；Redis 不可用时自动降级到内存，不阻塞请求。
"""
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Optional

_WINDOW_SECONDS = 60


class RateLimitStore:
    def __init__(self, redis_url: Optional[str] = None, window: int = _WINDOW_SECONDS):
        self.window = window
        self._buckets: dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._counter = 0
        self._redis = None
        self._redis_broken = False
        if redis_url:
            self._redis = self._connect(redis_url)

    @staticmethod
    def _connect(redis_url: str):
        try:
            import redis  # 延迟导入：未安装时不影响网关启动

            client = redis.Redis.from_url(
                redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True
            )
            client.ping()
            return client
        except Exception:
            return None

    def hit(self, key: tuple, limit: int) -> bool:
        """返回 True 表示已超限。"""
        if self._redis is not None and not self._redis_broken:
            try:
                return self._hit_redis(key, limit)
            except Exception:
                # Redis 故障时降级内存，避免限流失效放大后端压力
                self._redis_broken = True
        return self._hit_memory(key, limit)

    def _hit_redis(self, key: tuple, limit: int) -> bool:
        now = time.time()
        redis_key = "yuban:rate:" + ":".join(str(part) for part in key)
        script = """
        local now = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        local limit = tonumber(ARGV[3])
        local member = ARGV[4]
        redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
        local count = redis.call('ZCARD', KEYS[1])
        if count >= limit then
            return 1
        end
        redis.call('ZADD', KEYS[1], now, member)
        redis.call('EXPIRE', KEYS[1], window + 2)
        return 0
        """
        result = self._redis.eval(
            script, 1, redis_key, now, self.window, limit, f"{now}-{uuid.uuid4().hex[:8]}"
        )
        return int(result) == 1

    def _hit_memory(self, key: tuple, limit: int) -> bool:
        now = time.time()
        with self._lock:
            self._counter += 1
            if self._counter % 1000 == 0:
                self._sweep(now)
            q = self._buckets[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= limit:
                return True
            q.append(now)
            return False

    def _sweep(self, now: float) -> None:
        for key, q in list(self._buckets.items()):
            while q and now - q[0] > self.window:
                q.popleft()
            if not q:
                del self._buckets[key]

    @property
    def backend(self) -> str:
        return "redis" if (self._redis is not None and not self._redis_broken) else "memory"
