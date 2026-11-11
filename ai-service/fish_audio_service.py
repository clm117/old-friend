"""
Fish Audio 服务 —— 声音克隆 + 克隆音色合成（降级 MVP 克隆提供方）

定位：TTS_PROVIDER=edge 时承担「声音克隆」；日常合成走 edge-tts（免费无限量），
仅当会话绑定克隆音色（voice_id 以 "fish:" 前缀标识）时才消耗 Fish 额度。

官方接口事实（2026-11 调研）：
- 克隆：POST https://api.fish.audio/model
  multipart 表单：type=tts、train_mode=fast、title、voices[]（参考音频文件）
  即时可用（fast 模式无需轮询）；免费档克隆音色强制公开（visibility=public）
- 合成：POST https://api.fish.audio/v1/tts
  JSON：text、reference_id（克隆产物 model_id）、format=mp3、model
- 鉴权：Authorization: Bearer FISH_AUDIO_API_KEY
- 免费档约束：约 8000 积分/月（≈7 分钟音频）、单次合成 ≤500 字符

隐私提示：免费档克隆音色在 Fish Audio 平台公开可见，前端克隆页已同步提示。
"""
import os
from pathlib import Path

import httpx


def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and val and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

FISH_API_BASE = os.environ.get("FISH_AUDIO_BASE_URL", "https://api.fish.audio").rstrip("/")
FISH_MODEL = os.environ.get("FISH_AUDIO_MODEL", "s2.1-pro-free").strip()
FISH_MAX_CHARS = 500
SAMPLE_RATE = 44100  # Fish mp3 输出采样率（44.1kHz 档）

ERROR_HINTS = {
    "voice_define_unsupported": "未配置 FISH_AUDIO_API_KEY，声音克隆不可用；日常合成不受影响",
    "voice_quota_unavailable": "Fish Audio 额度不足（免费档约 7 分钟/月），请下月重置或升级",
    "voice_quota_exhausted": "Fish Audio 请求过于频繁，请稍后重试",
    "voice_define_timeout": "Fish Audio 服务超时，请稍后重试",
    "voice_upstream_error": "Fish Audio 服务暂时不可用，请稍后重试",
    "voice_audio_invalid": "参考音频格式不支持或过短（建议 10 秒以上清晰人声）",
    "voice_text_too_long": f"单次合成文本超过 {FISH_MAX_CHARS} 字符上限，已回退默认音色",
}


class FishAudioError(Exception):
    """带语义 code / HTTP 状态 / 用户提示的失败分支（与 CloudVoiceError 对齐）"""

    def __init__(self, code, message, status=502, hint=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint or ERROR_HINTS.get(code, message)
        self.details = details or {}

    def to_dict(self):
        return {"error": self.message, "code": self.code, "hint": self.hint, "details": self.details}


def _guess_mime(data: bytes) -> str:
    """按魔数判定参考音频 MIME（克隆表单用）"""
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


_MIME_EXT = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/webm": "webm",
}


class FishAudioService:
    """Fish Audio 克隆与克隆音色合成"""

    prefix = "fish"  # voice_id 形如 "fish:<model_id>"

    def __init__(self):
        self.api_key = os.environ.get("FISH_AUDIO_API_KEY", "").strip()

    @property
    def enabled(self):
        return bool(self.api_key)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _body(resp):
        try:
            return resp.json()
        except ValueError:
            return {}

    def _raise_for(self, resp, action):
        body = self._body(resp)
        message = body.get("message") or body.get("error") or f"HTTP {resp.status_code}"
        lower = str(message).lower()
        if resp.status_code in (401, 403):
            raise FishAudioError("voice_define_unsupported",
                                 f"Fish Audio Key 无效或无权限: {message}", status=501)
        if resp.status_code == 402 or "credit" in lower or "balance" in lower or "quota" in lower:
            raise FishAudioError("voice_quota_unavailable",
                                 f"Fish Audio 额度不足: {message}", status=402)
        if resp.status_code == 429:
            raise FishAudioError("voice_quota_exhausted",
                                 f"Fish Audio 限流: {message}", status=429)
        if resp.status_code == 400:
            raise FishAudioError("voice_audio_invalid",
                                 f"Fish Audio 参数不合法: {message}", status=422)
        raise FishAudioError("voice_upstream_error",
                             f"Fish Audio {action} 失败: {message}", status=502,
                             details={"upstream_status": resp.status_code})

    async def create_voice(self, title: str, audio: bytes, description: str = "") -> str:
        """
        声音克隆：参考音频 → model_id（即时可用）。
        免费档克隆音色强制公开（visibility=public），调用方需自行向用户提示。
        """
        if not self.enabled:
            raise FishAudioError("voice_define_unsupported", "未配置 FISH_AUDIO_API_KEY", status=501)
        if not audio:
            raise FishAudioError("voice_audio_invalid", "缺少参考音频", status=422)
        title = (title or "语伴克隆音色").strip()[:64] or "语伴克隆音色"
        mime = _guess_mime(audio)
        filename = f"voice.{_MIME_EXT.get(mime, 'wav')}"
        data = {
            "type": "tts",
            "train_mode": "fast",
            "title": title,
            "visibility": "public",  # 免费档强制公开，显式声明避免歧义
        }
        if description:
            data["description"] = description[:500]
        files = {"voices": (filename, audio, mime)}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{FISH_API_BASE}/model",
                    headers=self._headers(),
                    data=data,
                    files=files,
                )
        except httpx.TimeoutException as e:
            raise FishAudioError("voice_define_timeout", f"Fish Audio 克隆超时: {e}", status=504)
        except httpx.HTTPError as e:
            raise FishAudioError("voice_upstream_error", f"Fish Audio 连接失败: {e}", status=502)
        if resp.status_code not in (200, 201):
            self._raise_for(resp, "create_voice")
        body = self._body(resp)
        model_id = body.get("_id") or body.get("id")
        if not model_id:
            raise FishAudioError("voice_upstream_error", "Fish Audio 未返回 model_id", status=502,
                                 details={"body": str(body)[:200]})
        return str(model_id)

    async def synthesize(self, text: str, reference_id: str, speed: float = 1.0):
        """
        克隆音色合成，返回 (mp3_bytes, 44100)。
        单次 ≤500 字符（免费档上限）；超长抛 FishAudioError(422)，由调用方回退。
        """
        if not self.enabled:
            raise FishAudioError("voice_define_unsupported", "未配置 FISH_AUDIO_API_KEY", status=501)
        if not reference_id:
            raise FishAudioError("voice_upstream_error", "缺少 reference_id", status=502)
        text = (text or "").strip()
        if not text:
            raise FishAudioError("voice_audio_invalid", "合成文本为空", status=422)
        if len(text) > FISH_MAX_CHARS:
            raise FishAudioError("voice_text_too_long",
                                 f"文本 {len(text)} 字符超过上限 {FISH_MAX_CHARS}", status=422)
        payload = {
            "text": text,
            "reference_id": reference_id,
            "format": "mp3",
        }
        if FISH_MODEL:
            payload["model"] = FISH_MODEL
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{FISH_API_BASE}/v1/tts",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as e:
            raise FishAudioError("voice_define_timeout", f"Fish Audio 合成超时: {e}", status=504)
        except httpx.HTTPError as e:
            raise FishAudioError("voice_upstream_error", f"Fish Audio 连接失败: {e}", status=502)
        if resp.status_code != 200:
            self._raise_for(resp, "synthesize")
        audio = resp.content
        if not audio:
            raise FishAudioError("voice_upstream_error", "Fish Audio 返回空音频", status=502)
        return audio, SAMPLE_RATE


# 单例（无 Key 时 enabled=False，克隆不可用、日常合成不受影响）
fish_audio_service = FishAudioService()
