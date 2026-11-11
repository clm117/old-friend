"""按用户用量计量 + 日配额（网关层）

为什么放网关：只有网关知道"这次调用是谁发起的"（JWT sub / anonymous），
AI 服务收到的是网关注入的签名身份，天然适合按用户记账。

计量维度（usage:YYYY-MM-DD:<user> → Hash）：
- llm_chars    LLM 回复字符数（WS 文本消息累计；免费模型也计数，用于观察活跃与防滥用）
- tts_chars    TTS 合成文本字符数（HTTP /api/voice/tts 的 text 长度，按字符计费与云厂商对齐）
- voice_clones 音色克隆次数（训练接口单价高，单独限额）
- asr_seconds  ASR 转写音频时长（按上传文件大小 ÷ 16KB/s 估算，16kHz 16bit 单声道 ≈ 32KB/s，取保守值）

配额：
- 默认每用户每日：USAGE_DAILY_LLM_CHARS / USAGE_DAILY_TTS_CHARS / USAGE_DAILY_VOICE_CLONES / USAGE_DAILY_ASR_SECONDS
- 单用户覆盖：USAGE_QUOTA_OVERRIDES={"alice": {"tts_chars": 50000}}
- 超限返回 402（与上游 voice_quota_unavailable 语义对齐），HTTP 与 WS 语义一致
- AUTH_REQUIRED=false 的本地模式统一记到 "anonymous"，配额照常生效（防本地脚本刷爆余额）

存储：REDIS_URL 存在时用 Redis Hash + 48h TTL；否则进程内字典（重启清零，验证期可接受）。
"""
import json
import os
import time
from typing import Optional

DEFAULT_QUOTAS = {
    "llm_chars": int(os.environ.get("USAGE_DAILY_LLM_CHARS", "100000")),
    "tts_chars": int(os.environ.get("USAGE_DAILY_TTS_CHARS", "20000")),
    "voice_clones": int(os.environ.get("USAGE_DAILY_VOICE_CLONES", "1")),
    "asr_seconds": int(os.environ.get("USAGE_DAILY_ASR_SECONDS", "600")),
}


def _load_overrides() -> dict:
    raw = os.environ.get("USAGE_QUOTA_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


METRIC_LABELS = {
    "llm_chars": "对话额度",
    "tts_chars": "语音合成额度",
    "voice_clones": "音色克隆次数",
    "asr_seconds": "语音识别额度",
}


class UsageStore:
    def __init__(self, redis_url: Optional[str] = None):
        self._redis = None
        self._redis_broken = False
        self._memory: dict = {}  # key -> {"counts": {metric: int}, "expire_at": float}
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(
                    redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _key(self, user: str) -> str:
        return f"usage:{self._today()}:{user}"

    # ---------- 底层读写 ----------
    def _get_counts(self, key: str) -> dict:
        if self._redis is not None and not self._redis_broken:
            try:
                data = self._redis.hgetall(key)
                return {k: int(v) for k, v in data.items()}
            except Exception:
                self._redis_broken = True
        entry = self._memory.get(key)
        if entry and entry["expire_at"] > time.time():
            return dict(entry["counts"])
        return {}

    def _incr(self, key: str, metric: str, amount: int) -> int:
        if self._redis is not None and not self._redis_broken:
            try:
                val = self._redis.hincrby(key, metric, amount)
                self._redis.expire(key, 48 * 3600)
                return int(val)
            except Exception:
                self._redis_broken = True
        entry = self._memory.setdefault(
            key, {"counts": {}, "expire_at": time.time() + 48 * 3600}
        )
        entry["counts"][metric] = entry["counts"].get(metric, 0) + amount
        return entry["counts"][metric]

    # ---------- 对外接口 ----------
    def quota_for(self, user: str) -> dict:
        overrides = _load_overrides().get(user, {})
        quota = dict(DEFAULT_QUOTAS)
        for k, v in overrides.items():
            if k in quota and isinstance(v, int) and v >= 0:
                quota[k] = v
        return quota

    def check(self, user: str, metric: str) -> tuple:
        """预检：返回 (ok, remaining)。不扣减，先问能不能用。"""
        quota = self.quota_for(user)
        if metric not in quota:
            return True, -1
        used = self._get_counts(self._key(user)).get(metric, 0)
        remaining = quota[metric] - used
        return remaining > 0, max(0, remaining)

    def record(self, user: str, metric: str, amount: int = 1) -> int:
        """记账，返回当日累计值。amount<=0 时忽略。"""
        if amount <= 0 or metric not in DEFAULT_QUOTAS:
            return 0
        return self._incr(self._key(user), metric, amount)

    def snapshot(self, user: str) -> dict:
        """当日用量快照（供 /api/usage/me 返回）"""
        quota = self.quota_for(user)
        used = self._get_counts(self._key(user))
        return {
            "date": self._today(),
            "user": user,
            "usage": {m: used.get(m, 0) for m in quota},
            "quota": quota,
            "remaining": {m: max(0, quota[m] - used.get(m, 0)) for m in quota},
        }

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None and not self._redis_broken else "memory"


def quota_error_payload(metric: str, remaining: int) -> dict:
    return {
        "error": f"今日{METRIC_LABELS.get(metric, metric)}已用完",
        "code": "user_quota_exceeded",
        "metric": metric,
        "remaining": remaining,
    }
