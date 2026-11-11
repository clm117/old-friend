"""
云声音定义/复刻服务骨架（GAP-07）

官方接口事实（阿里云百炼，2026-11）：
- 统一入口：POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization
  model 固定 voice-enrollment
- 复刻：payload.audio（10-20s，≤10MB，≥16kHz），target_model 创建时绑定
- 设计：payload.voice_prompt（≤500 字）+ preview_text（15-200 字）+ target_model
  仅 cosyvoice-v3.5-plus/flash、cosyvoice-v3-plus/flash 支持设计
- voice_id：复刻 {target_model}-{prefix}-xxx；设计 {target_model}-vd-{prefix}-xxx
- 创建后异步部署，需 query_voice 轮询到 status=OK 才能合成
- 配额 1000 个/账号；限流 180 RPM / 3 RPS

本模块只依赖账号 Key 存在性即可初始化；真实调用是否可用由上游决定。
账号欠费时统一映射为 402 voice_quota_unavailable，不伪装成功。
"""
import base64
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

CUSTOMIZATION_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
ENROLLMENT_MODEL = "voice-enrollment"
DESIGN_MODELS = (
    "cosyvoice-v3.5-plus",
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3-plus",
    "cosyvoice-v3-flash",
)
MAX_VOICES = 1000
RATE_LIMIT_RPM = 180
RATE_LIMIT_RPS = 3
DEPLOY_TIMEOUT = float(os.environ.get("CLOUD_VOICE_DEPLOY_TIMEOUT", "60"))
POLL_INTERVAL = float(os.environ.get("CLOUD_VOICE_POLL_INTERVAL", "1"))

ERROR_HINTS = {
    "voice_define_unsupported": "当前音色模式不支持声音定义（缺少云 Key 或目标模型不支持）",
    "voice_quota_unavailable": "云语音账户余额不足（欠费），请充值后重试；本次未创建任何音色",
    "voice_quota_exhausted": "音色配额或调用频率已达上限，请稍后重试",
    "voice_define_timeout": "音色部署超时，请稍后在已保存音色中重试",
    "voice_upstream_error": "云语音服务暂时不可用，请稍后重试",
    "voice_audio_invalid": "参考音频格式不支持或过短（需 10-20 秒、≥16kHz）",
    "voice_define_invalid": "声音定义参数不合法，请检查描述与目标模型",
    "voice_target_model_mismatch": "音色与当前合成模型不匹配，已回退默认音色",
}

STATUS_READY = {"OK", "READY", "SUCCESS", "DEPLOYED"}
STATUS_FAILED = {"FAILED", "UNDEPLOYED", "ERROR"}


class CloudVoiceError(Exception):
    """带语义 code / HTTP 状态 / 用户提示的失败分支"""

    def __init__(self, code, message, status=502, hint=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint or ERROR_HINTS.get(code, message)
        self.details = details or {}

    def to_dict(self):
        return {"error": self.message, "code": self.code, "hint": self.hint, "details": self.details}


def audio_mime(data):
    """按魔数判定参考音频 MIME，避免声明类型与真实内容不一致"""
    head = (data or b"")[:12]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    return "audio/wav"


def audio_data_uri(data):
    return f"data:{audio_mime(data)};base64," + base64.b64encode(data).decode()


class CloudVoiceService:
    """voice_id 来源 / 映射 / 缓存 + customization 调用与失败分支"""

    def __init__(self, target_model=None, api_key=None, registry_path=None):
        self.target_model = (target_model or os.environ.get("CLOUD_TTS_MODEL", "qwen3-tts-flash")).strip()
        if api_key is not None:
            self.api_key = api_key.strip()
        elif os.environ.get("DASHSCOPE_API_KEY", "").strip():
            self.api_key = os.environ["DASHSCOPE_API_KEY"].strip()
        elif os.environ.get("LLM_PROVIDER", "qwen").strip().lower() == "qwen":
            # 仅当对话模型也跑在 qwen 上时才复用同一个 Key
            self.api_key = os.environ.get("LLM_API_KEY", "").strip()
        else:
            self.api_key = ""
        data_dir = Path(os.environ.get("YUBAN_DATA_DIR", Path(__file__).parent / "data"))
        self.registry_path = Path(registry_path) if registry_path else data_dir / "cloud_voice_registry.json"
        self._lock = threading.Lock()
        self._voices = {}
        self._calls = deque()
        self._last_error = None
        self._load()

    # ---------- 持久化映射 ----------

    def _load(self):
        try:
            if self.registry_path.exists():
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                self._voices = data.get("voices") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            self._voices = {}

    def _save(self):
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.registry_path.with_suffix(".tmp")
            payload = {"version": 1, "updated_at": time.time(), "voices": self._voices}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.registry_path)
        except OSError:
            pass

    # ---------- 能力与保护 ----------

    def capability(self):
        design_ok = self.target_model in DESIGN_MODELS
        return {
            "supported": bool(self.api_key),
            "provider": "cloud",
            "target_model": self.target_model,
            "clone": {
                "supported": bool(self.api_key),
                "reason": None if self.api_key else ERROR_HINTS["voice_define_unsupported"],
            },
            "design": {
                "supported": bool(self.api_key) and design_ok,
                "supported_models": list(DESIGN_MODELS),
                "reason": None if design_ok else f"目标模型 {self.target_model} 不支持声音设计（仅 cosyvoice-v3 系列）",
            },
            "quota": {"limit": MAX_VOICES, "used": self._used_count()},
            "rate_limit": {"rpm": RATE_LIMIT_RPM, "rps": RATE_LIMIT_RPS},
            "error_codes": {
                "voice_define_unsupported": 501,
                "voice_quota_unavailable": 402,
                "voice_quota_exhausted": 429,
                "voice_define_timeout": 504,
                "voice_upstream_error": 502,
                "voice_audio_invalid": 422,
                "voice_define_invalid": 422,
                "voice_target_model_mismatch": 502,
            },
            "last_error": self._last_error,
            "verified": False,  # 真实创建需账号可用，能力接口不做网络探测
        }

    def _used_count(self):
        return len(self._voices)

    def _guard_rate(self):
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        recent_1s = sum(1 for t in self._calls if now - t <= 1)
        if recent_1s >= RATE_LIMIT_RPS or len(self._calls) >= RATE_LIMIT_RPM:
            raise CloudVoiceError("voice_quota_exhausted", "云音色创建请求超过限流阈值",
                                 status=429, details={"rpm": RATE_LIMIT_RPM, "rps": RATE_LIMIT_RPS})
        self._calls.append(now)

    def _guard_quota(self):
        if self._used_count() >= MAX_VOICES:
            raise CloudVoiceError("voice_quota_exhausted", f"音色数量已达上限 {MAX_VOICES}",
                                 status=429, details={"limit": MAX_VOICES})

    # ---------- 上游调用 ----------

    async def _post_json(self, payload, timeout=60):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(CUSTOMIZATION_URL, headers=headers, json=payload)

    @staticmethod
    def _body(resp):
        try:
            return resp.json()
        except ValueError:
            return {}

    def _raise_for(self, resp, action):
        body = self._body(resp)
        code = body.get("code") or ""
        message = body.get("message") or body.get("error") or f"HTTP {resp.status_code}"
        if code == "Arrearage" or "overdue payment" in message.lower():
            self._last_error = {"code": "voice_quota_unavailable", "action": action, "upstream": code}
            raise CloudVoiceError("voice_quota_unavailable", "云语音账户欠费，无法创建音色",
                                 status=402, details={"upstream_code": code})
        if resp.status_code == 429 or code in ("Throttling", "Throttling.RateQuota", "limit_business_error"):
            self._last_error = {"code": "voice_quota_exhausted", "action": action, "upstream": code}
            raise CloudVoiceError("voice_quota_exhausted", "云音色创建被限流",
                                 status=429, details={"upstream_code": code})
        if resp.status_code in (401, 403):
            self._last_error = {"code": "voice_define_unsupported", "action": action, "upstream": code}
            raise CloudVoiceError("voice_define_unsupported", "云 Key 无声音定义权限",
                                 status=501, details={"upstream_code": code})
        if resp.status_code == 400:
            self._last_error = {"code": "voice_define_invalid", "action": action, "upstream": code}
            raise CloudVoiceError("voice_define_invalid", message, status=422,
                                 details={"upstream_code": code})
        self._last_error = {"code": "voice_upstream_error", "action": action, "upstream": code}
        raise CloudVoiceError("voice_upstream_error", message, status=502,
                             details={"upstream_code": code, "upstream_status": resp.status_code})

    async def _call(self, action, payload):
        if not self.api_key:
            raise CloudVoiceError("voice_define_unsupported", "未配置云语音 Key", status=501)
        req = {"model": ENROLLMENT_MODEL, "input": {"action": action, "payload": payload}}
        try:
            resp = await self._post_json(req)
        except httpx.TimeoutException as e:
            raise CloudVoiceError("voice_define_timeout", f"云声音服务超时: {e}", status=504)
        except httpx.HTTPError as e:
            raise CloudVoiceError("voice_upstream_error", f"云声音服务连接失败: {e}", status=502)
        if resp.status_code != 200:
            self._raise_for(resp, action)
        return self._body(resp)

    # ---------- voice_id 校验 / 轮询 ----------

    def _new_prefix(self):
        return ("yb" + uuid.uuid4().hex[:6])[:10]

    def _validate_voice_id(self, voice_id, prefix, design):
        if not voice_id or not isinstance(voice_id, str):
            raise CloudVoiceError("voice_upstream_error", "上游未返回 voice_id", status=502)
        if not voice_id.startswith(self.target_model + "-"):
            raise CloudVoiceError("voice_target_model_mismatch",
                                  f"voice_id 与目标模型不匹配: {voice_id}", status=502,
                                  details={"voice_id": voice_id, "target_model": self.target_model})
        if design and "-vd-" not in voice_id:
            raise CloudVoiceError("voice_upstream_error", f"设计音色 voice_id 缺少 -vd- 标记: {voice_id}",
                                  status=502, details={"voice_id": voice_id})
        if not design and "-vd-" in voice_id:
            raise CloudVoiceError("voice_upstream_error", f"复刻音色 voice_id 不应含 -vd-: {voice_id}",
                                  status=502, details={"voice_id": voice_id})
        if prefix not in voice_id:
            raise CloudVoiceError("voice_upstream_error", f"voice_id 缺少 prefix={prefix}: {voice_id}",
                                  status=502, details={"voice_id": voice_id})
        return voice_id

    def _voice_status(self, body):
        output = body.get("output") or {}
        return (output.get("status") or output.get("voice_status") or "").upper()

    async def _wait_ready(self, voice_id, timeout=None):
        """异步部署轮询：DEPLOYING 继续等待，OK 返回，FAILED 失败，超时 504"""
        deadline = time.monotonic() + (timeout or DEPLOY_TIMEOUT)
        last = "DEPLOYING"
        while time.monotonic() < deadline:
            body = await self._call("query_voice", {"voice_id": voice_id})
            last = self._voice_status(body) or last
            if last in STATUS_READY:
                return {"status": last}
            if last in STATUS_FAILED:
                raise CloudVoiceError("voice_upstream_error", f"音色部署失败: {last}", status=502,
                                      details={"voice_id": voice_id, "status": last})
            await _sleep(POLL_INTERVAL)
        raise CloudVoiceError("voice_define_timeout", f"音色部署超时（最后状态 {last}）",
                              status=504, details={"voice_id": voice_id, "status": last})

    # ---------- 创建 ----------

    async def _create(self, session_id, mode, prefix, payload, description, timeout=None):
        self._guard_rate()
        self._guard_quota()
        body = await self._call("create_voice", payload)
        output = body.get("output") or {}
        voice_id = output.get("voice_id")
        self._validate_voice_id(voice_id, prefix, design=(mode == "design"))
        ready = await self._wait_ready(voice_id, timeout=timeout)
        entry = {
            "session_id": session_id,
            "voice_id": voice_id,
            "target_model": self.target_model,
            "prefix": prefix,
            "mode": mode,
            "description": description,
            "status": ready["status"],
            "created_at": time.time(),
        }
        with self._lock:
            self._voices[session_id] = entry
            self._save()
        self._last_error = None
        return entry

    async def create_clone(self, session_id, audio, description="", language_hints=None, timeout=None):
        """声音复刻：10-20 秒参考音频 → voice_id"""
        if not audio:
            raise CloudVoiceError("voice_audio_invalid", "缺少参考音频", status=422)
        prefix = self._new_prefix()
        payload = {
            "target_model": self.target_model,
            "prefix": prefix,
            "audio": audio_data_uri(audio),
        }
        if language_hints:
            payload["language_hints"] = language_hints
        return await self._create(session_id, "clone", prefix, payload, description, timeout=timeout)

    async def create_design(self, session_id, voice_prompt, preview_text, description="", timeout=None):
        """声音设计：文字描述 → voice_id（目标模型须在 DESIGN_MODELS 内）"""
        if self.target_model not in DESIGN_MODELS:
            raise CloudVoiceError(
                "voice_define_unsupported",
                f"目标模型 {self.target_model} 不支持声音设计",
                status=501, details={"target_model": self.target_model, "supported_models": list(DESIGN_MODELS)})
        voice_prompt = (voice_prompt or "").strip()
        preview_text = (preview_text or "").strip()
        if not (1 <= len(voice_prompt) <= 500) or not (15 <= len(preview_text) <= 200):
            raise CloudVoiceError("voice_define_invalid",
                                  "voice_prompt 需 1-500 字，preview_text 需 15-200 字", status=422)
        prefix = self._new_prefix()
        payload = {
            "target_model": self.target_model,
            "prefix": prefix,
            "voice_prompt": voice_prompt,
            "preview_text": preview_text,
        }
        return await self._create(session_id, "design", prefix, payload,
                                  description or voice_prompt, timeout=timeout)

    async def query_voice(self, voice_id):
        body = await self._call("query_voice", {"voice_id": voice_id})
        output = body.get("output") or {}
        return {"voice_id": voice_id, "status": self._voice_status(body),
                "raw": output}

    async def list_voices(self, page_index=1, page_size=10):
        body = await self._call("list_voice", {"page_index": page_index, "page_size": page_size})
        return body.get("output") or {}

    async def delete_voice(self, voice_id):
        body = await self._call("delete_voice", {"voice_id": voice_id})
        with self._lock:
            for sid, entry in list(self._voices.items()):
                if entry.get("voice_id") == voice_id:
                    del self._voices[sid]
            self._save()
        return body

    # ---------- 映射 / 查询 ----------

    def get_voice(self, session_id):
        """session_id → 已就绪且与当前模型匹配的音色；不匹配返回 (None, notice)"""
        entry = self._voices.get(session_id)
        if not entry:
            return None, None
        if entry.get("target_model") != self.target_model:
            return None, ERROR_HINTS["voice_target_model_mismatch"]
        if entry.get("status") not in STATUS_READY:
            return None, ERROR_HINTS["voice_define_timeout"]
        return entry["voice_id"], None

    def bind(self, session_id, voice_id, target_model=None, mode="clone", description=""):
        """外部已存在 voice_id 时写入映射（真实成功链路/迁移用）"""
        entry = {
            "session_id": session_id, "voice_id": voice_id,
            "target_model": target_model or self.target_model,
            "prefix": "", "mode": mode, "description": description,
            "status": "OK", "created_at": time.time(),
        }
        with self._lock:
            self._voices[session_id] = entry
            self._save()
        return entry

    def unbind(self, session_id):
        with self._lock:
            existed = self._voices.pop(session_id, None)
            if existed:
                self._save()
        return existed


async def _sleep(seconds):
    import asyncio
    await asyncio.sleep(seconds)


# 单例（import 时读取已保存映射；无 Key 时 capability 返回不支持）
cloud_voice_service = CloudVoiceService()
