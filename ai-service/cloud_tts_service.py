"""
云 TTS 服务 —— 阿里云百炼 qwen3-tts-flash（验证期临时适配）

用途：无 GPU 环境下验证「ASR → LLM 流式 → TTS → WebSocket 音频流」全链路实时交互。
正式生产切回本地 VoxCPM2（TTS_PROVIDER=voxcpm2），本模块不再参与。

选型说明（2026-10-20 实测）：
- cosyvoice-v2 / cosyvoice-v3-flash（WS 协议）：本工作区 Key 未开通，引擎报错 418
- sambert 系列（WS 协议）：报 Request text is invalid
- qwen3-tts-flash（HTTP multimodal-generation）：可用，返回 24h 有效 WAV URL
故采用 HTTP 一次性合成（句级颗粒度由上游断句控制，时延可接受）。

与 VoxCPM2 的差异（验证期可接受）：
- 不支持 style_description / emotion_params 自由文本风格
- 音色由 CLOUD_TTS_VOICE 指定（默认 Cherry 女声），与角色 voice_profile 无关
"""
import io
import os
import wave
from pathlib import Path

import httpx

# 复用 asr_service 的 .env 加载逻辑（LLM_API_KEY 即 DashScope Key）
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

TTS_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
TTS_MODEL = os.environ.get("CLOUD_TTS_MODEL", "qwen3-tts-flash")
DEFAULT_VOICE = os.environ.get("CLOUD_TTS_VOICE", "Cherry")


class CloudTTSService:
    """DashScope qwen3-tts 云语音合成（接口与 VoxCPM2 对齐）"""

    def __init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.voice = DEFAULT_VOICE

    def available(self):
        return bool(self.api_key)

    async def synthesize(self, text: str, voice: str = None, quick_mode: bool = False):
        """
        合成单句文本，返回 (wav_bytes, sample_rate)。
        quick_mode 参数仅为与 VoxCPM2 接口对齐，云侧暂不支持变速。
        无 Key 返回 (None, None)；任务/网络失败抛异常（由调用方记录）。
        """
        if not self.api_key:
            return None, None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": TTS_MODEL,
            "input": {
                "text": text,
                "voice": voice or self.voice,
                # 2026-10-20 实测：Chinese + 中文逗号会导致引擎挂起（ReadTimeout），Auto 正常
                "language_type": "Auto",
            },
        }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(TTS_API_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"云 TTS 失败: HTTP {resp.status_code} {resp.text[:200]}")
            audio = resp.json().get("output", {}).get("audio", {})
            url = audio.get("url")
            if not url:
                raise RuntimeError(f"云 TTS 未返回音频地址: {resp.text[:200]}")

            wav_resp = await client.get(url)
            if wav_resp.status_code != 200:
                raise RuntimeError(f"云 TTS 音频下载失败: HTTP {wav_resp.status_code}")
            wav_bytes = wav_resp.content

        # 从 WAV 头解析真实采样率（qwen3-tts 输出 24kHz，不硬编码）
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()

        return wav_bytes, sample_rate


# 单例
cloud_tts_service = CloudTTSService()
