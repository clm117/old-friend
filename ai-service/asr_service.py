"""
语音转写服务（ASR）—— 智谱 GLM-ASR-2512

复用 .env 中的 LLM_API_KEY（智谱 Key 对 ASR/LLM 通用）。
接口：POST https://open.bigmodel.cn/api/paas/v4/audio/transcriptions
格式：multipart/form-data，model=glm-asr-2512，file=WAV/MP3（≤25MB，≤30秒）
返回：{"text": "识别结果"}
"""
import os
from pathlib import Path

import httpx

# 复用 llm_service 的 .env 加载逻辑（LLM_API_KEY 即智谱 Key）
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

ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
ASR_MODEL = "glm-asr-2512"


class ASRService:
    """智谱 GLM-ASR-2512 语音转写"""

    def __init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", "")

    def available(self):
        return bool(self.api_key)

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.wav",
                               mime: str = "audio/wav") -> str:
        """
        将音频文件（WAV/MP3/OGG/FLAC/M4A）转写为文本。
        返回识别到的完整文本；失败抛出异常。
        """
        if not self.api_key:
            raise RuntimeError("未配置 LLM_API_KEY，无法使用语音识别")

        async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
            files = {"file": (filename, audio_bytes, mime)}
            data = {"model": ASR_MODEL}
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = await client.post(ASR_URL, files=files, data=data, headers=headers)

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(f"ASR HTTP {resp.status_code}: {detail}")

        body = resp.json()
        text = body.get("text", "").strip()
        if not text:
            raise RuntimeError("ASR 返回空文本")
        return text

    # 容器格式签名 → (文件名, MIME)
    _MAGIC = (
        (b"RIFF", "audio.wav", "audio/wav"),
        (b"ID3", "audio.mp3", "audio/mpeg"),
        (b"OggS", "audio.ogg", "audio/ogg"),
        (b"fLaC", "audio.flac", "audio/flac"),
    )

    @classmethod
    def _detect_container(cls, raw: bytes):
        """识别音频容器格式；裸 PCM 返回 None"""
        for sig, name, mime in cls._MAGIC:
            if raw.startswith(sig):
                return name, mime
        if len(raw) >= 12 and raw[4:8] == b"ftyp":
            return "audio.m4a", "audio/mp4"
        # MP3 裸帧同步字
        if raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            return "audio.mp3", "audio/mpeg"
        return None

    async def transcribe_any(self, raw: bytes, sample_rate: int = 16000) -> str:
        """
        自动识别上传内容：已是容器格式（WAV/MP3/OGG/FLAC/M4A）则原样上传；
        裸 PCM 才补 WAV 头，避免把 MP3 当成 PCM 二次封装导致音频损坏。
        """
        detected = self._detect_container(raw)
        if detected:
            name, mime = detected
            return await self.transcribe_bytes(raw, name, mime)
        return await self.transcribe_pcm(raw, sample_rate)

    # 兼容旧接口（main.py 调 transcribe_pcm）
    async def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """
        PCM → WAV 再上传，因为智谱 API 只吃容器格式（WAV/MP3）。
        前端已经按 16kHz/16bit/单声道 录制，这里直接加 WAV 头即可。
        """
        import wave
        import io

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return await self.transcribe_bytes(buf.getvalue(), "audio.wav")


# 单例
asr_service = ASRService()
