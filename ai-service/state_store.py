"""AI 服务状态持久化：会话 / 对话历史 / 情感配置 / 语音绑定。

策略：
- 默认落盘到 ``YUBAN_DATA_DIR/state.json``（默认 ai-service/data/state.json），
  线程锁 + 临时文件原子替换，进程重启后自动恢复。
- 配置 ``REDIS_URL`` 时双写 Redis（键 ``yuban:ai:state``），便于多实例共享；
  Redis 不可用时自动降级为仅本地文件，不阻塞推理请求。
- 内存缓存 + 0.5s 防抖写盘，避免高频会话消息造成磁盘抖动。
"""
import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("yuban.state")

_STATE_VERSION = 1
_MAX_DIALOGUE_PER_SESSION = 200
_MAX_SESSIONS = 2000
_REDIS_KEY = "yuban:ai:state"


def _default_data_dir() -> Path:
    env = os.environ.get("YUBAN_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent / "data"


class PersistentStateStore:
    def __init__(
        self,
        data_dir: Optional[str] = None,
        state_file: Optional[str] = None,
        redis_url: Optional[str] = None,
    ):
        self.data_dir = Path(data_dir).expanduser() if data_dir else _default_data_dir()
        self.state_file = (
            Path(state_file).expanduser()
            if state_file
            else self.data_dir / "state.json"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._timer: Optional[threading.Timer] = None
        self._redis = None
        self._redis_url = redis_url if redis_url is not None else os.environ.get("REDIS_URL", "").strip()
        self._load()
        if self._redis_url:
            self._connect_redis()
        atexit.register(self.flush)

    # ---------- 持久化 ----------
    def _load(self) -> None:
        raw = None
        if self._redis_url:
            raw = self._load_redis()
        if raw is None:
            try:
                raw = self.state_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            except OSError as e:
                logger.warning("[state] 读取状态文件失败: %s", e)
        if not raw:
            return
        try:
            data = json.loads(raw)
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                # 只保留结构正确的会话，防止脏数据导致启动失败
                for sid, entry in sessions.items():
                    if isinstance(entry, dict):
                        entry.setdefault("short_term", {})
                        entry.setdefault("dialogue_history", [])
                        entry.setdefault("updated_at", time.time())
                        self._sessions[str(sid)] = entry
            logger.info("[state] 已恢复 %d 个会话（%s）", len(self._sessions), self.state_file)
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.error("[state] 状态文件损坏，已忽略并保留原文件: %s", e)

    def _connect_redis(self) -> None:
        try:
            import redis

            client = redis.Redis.from_url(
                self._redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
            )
            client.ping()
            self._redis = client
            logger.info("[state] Redis 已连接，状态将双写 %s", _REDIS_KEY)
        except Exception as e:
            logger.warning("[state] Redis 连接失败，降级为本地文件: %s", e)
            self._redis = None

    def _load_redis(self) -> Optional[str]:
        try:
            import redis

            client = redis.Redis.from_url(
                self._redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
            )
            return client.get(_REDIS_KEY)
        except Exception as e:
            logger.warning("[state] Redis 读取失败，回退本地文件: %s", e)
            return None

    def _schedule_save(self) -> None:
        with self._lock:
            self._dirty = True
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(0.5, self.flush)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            payload = json.dumps(
                {
                    "version": _STATE_VERSION,
                    "updated_at": time.time(),
                    "sessions": self._sessions,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._dirty = False
        try:
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.state_file)
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        except OSError as e:
            logger.error("[state] 写入状态文件失败: %s", e)
            with self._lock:
                self._dirty = True
        if self._redis is not None:
            try:
                self._redis.set(_REDIS_KEY, payload)
            except Exception as e:
                logger.warning("[state] Redis 写入失败，仅保留本地文件: %s", e)
                self._redis = None

    # ---------- 兼容原有 InMemoryStorage 接口 ----------
    def _session(self, session_id: str) -> dict[str, Any]:
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = {"short_term": {}, "dialogue_history": [], "updated_at": time.time()}
            self._sessions[session_id] = entry
            self._prune_sessions()
        return entry

    def _prune_sessions(self) -> None:
        if len(self._sessions) <= _MAX_SESSIONS:
            return
        overflow = len(self._sessions) - _MAX_SESSIONS
        oldest = sorted(self._sessions.items(), key=lambda item: item[1].get("updated_at", 0))[:overflow]
        for sid, _ in oldest:
            self._sessions.pop(sid, None)

    def set_short_term_memory(self, session_id: str, key: str, value: Any) -> None:
        with self._lock:
            entry = self._session(session_id)
            entry["short_term"][key] = value
            entry["updated_at"] = time.time()
        self._schedule_save()

    def get_short_term_memory(self, session_id: str, key: str) -> Any:
        with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            return entry.get("short_term", {}).get(key)

    def update_dialogue_history(self, session_id: str, message) -> None:
        text = message.decode("utf-8", errors="replace") if isinstance(message, (bytes, bytearray)) else str(message)
        with self._lock:
            entry = self._session(session_id)
            history = entry.setdefault("dialogue_history", [])
            history.insert(0, text)
            del history[_MAX_DIALOGUE_PER_SESSION:]
            entry["updated_at"] = time.time()
        self._schedule_save()

    def get_dialogue_history(self, session_id: str) -> list[bytes]:
        with self._lock:
            entry = self._sessions.get(session_id) or {}
            return [str(m).encode("utf-8") for m in entry.get("dialogue_history", [])]

    # ---------- 运维辅助 ----------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            sessions = len(self._sessions)
            messages = sum(len(s.get("dialogue_history", [])) for s in self._sessions.values())
        return {
            "backend": "redis+file" if self._redis is not None else "file",
            "state_file": str(self.state_file),
            "sessions": sessions,
            "dialogue_messages": messages,
        }
