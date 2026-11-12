"""
Edge TTS 服务 —— 微软 edge-tts 免费语音合成（育儿教练场景主力 TTS）

定位：TTS_PROVIDER=edge 时的日常合成提供方；声音克隆已移除（合规要求）。

事实（2026-11 调研）：
- 非官方接口，须 edge-tts>=7.3.0（旧版被微软 403）；输出 MP3 流（24kHz）
- 免 Key、不限量；存在周期性 403 风险，失败时自动重试一次
- 音色由 EDGE_TTS_VOICE 配置（默认 zh-CN-XiaoxiaoNeural 女声）
- 语速 rate 接受 "+0%"/"-25%"/"+50%" 形式，由 speed(0.5-2.0) 线性映射
"""
import os

DEFAULT_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip() or "zh-CN-XiaoxiaoNeural"
# edge-tts 输出固定为 24kHz MP3（audio-24khz-48kbitrate-mono-mp3）
SAMPLE_RATE = 24000
_MAX_RETRIES = 2


class EdgeTTSService:
    """edge-tts 合成（接口与 CloudTTSService 对齐）"""

    def __init__(self):
        self.voice = DEFAULT_VOICE

    def available(self):
        """edge-tts 免 Key，可用性即依赖已安装"""
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _rate(speed: float) -> str:
        try:
            pct = round((float(speed) - 1.0) * 100)
        except (TypeError, ValueError):
            pct = 0
        pct = max(-50, min(100, pct))
        return f"{pct:+d}%"

    async def _synthesize_once(self, text: str, voice: str, rate: str):
        import edge_tts
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        chunks = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    async def synthesize(self, text: str, voice: str = None, quick_mode: bool = False, speed: float = 1.0):
        """
        合成文本，返回 (mp3_bytes, 24000)。
        quick_mode 仅为与其他 TTS 提供方对齐，edge-tts 本身已是实时流式。
        依赖缺失返回 (None, None)；网络/上游失败重试一次后抛异常（由调用方记录）。
        """
        if not self.available():
            return None, None
        use_voice = (voice or self.voice).strip() or self.voice
        rate = self._rate(speed)
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                audio = await self._synthesize_once(text, use_voice, rate)
                if not audio:
                    raise RuntimeError("edge-tts 返回空音频")
                return audio, SAMPLE_RATE
            except Exception as e:
                last_exc = e
                if attempt + 1 >= _MAX_RETRIES:
                    break
        raise RuntimeError(f"edge-tts 合成失败: {last_exc}")


# 单例
edge_tts_service = EdgeTTSService()
