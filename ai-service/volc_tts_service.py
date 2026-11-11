"""
火山引擎（豆包）声音复刻服务 —— 国内直连的克隆后端

背景：Fish Audio 域名在当前网络被 DNS 污染（TCP 超时），改用火山「声音复刻2.0」。
免费额度：声音复刻2.0 赠送 20000 字符/半年，创建应用后赠送音色位（可 15 次训练）。

接口事实（2026-11 官方文档）：
- 训练：POST https://openspeech.bytedance.com/api/v1/mega_tts/audio/upload
  Header: Authorization: "Bearer; {AccessToken}"（分号分隔）、Resource-Id: seed-icl-2.0
  Body: appid / speaker_id（控制台音色ID，S_xxx）/ audios[{audio_bytes(base64), audio_format}]
        / source=2 / language=0 / model_type=4（ICL2.0）/ extra_params="{}"
- 状态：POST /api/v1/mega_tts/status {appid, speaker_id}
  status: 0 未找到 / 1 训练中 / 2 成功 / 3 失败 / 4 可用（2、4 可合成）
- 合成：POST /api/v1/tts（HTTP 非流式，operation=query）
  Body: app{appid, token, cluster=volcano_icl} / user{uid} / audio{voice_type=speaker_id, encoding=mp3}
        / request{reqid, text, operation=query}
  单次 text ≤ 1024 字节（UTF-8），超长需按句切分后拼接 mp3

限制说明：音色位（speaker_id）需在控制台预先创建；同一音色位支持 10 次上传，
即「重新克隆」会覆盖上一次的音色内容。MVP 阶段一个音色位够用。
"""
import base64
import json
import os
import uuid
from pathlib import Path

import httpx

VOLC_HOST = "https://openspeech.bytedance.com"
RESOURCE_ID = os.environ.get("VOLC_TTS_RESOURCE_ID", "seed-icl-2.0").strip()
CLUSTER = os.environ.get("VOLC_TTS_CLUSTER", "volcano_icl").strip()
MAX_TEXT_BYTES = 900  # 官方上限 1024 字节，留安全余量
SAMPLE_RATE = 24000

ERROR_HINTS = {
    "voice_define_unsupported": "未配置火山引擎 AppID / Access Token / 音色ID，声音克隆不可用；日常合成不受影响",
    "voice_quota_unavailable": "火山引擎额度不足（免费额度 20000 字符/半年）",
    "voice_quota_exhausted": "火山引擎请求过于频繁，请稍后重试",
    "voice_define_timeout": "火山引擎服务超时，请稍后重试",
    "voice_upstream_error": "火山引擎服务暂时不可用，请稍后重试",
    "voice_audio_invalid": "参考音频不符合要求（需 10 秒以上清晰单人中文语音，单文件 ≤10MB）",
    "voice_train_failed": "音色训练失败，请更换更清晰的参考音频后重试",
}

# 训练上传返回码（官方文档）
_UPLOAD_STATUS = {
    1101: "音频上传失败",
    1102: "无法识别人声内容（ASR 失败）",
    1103: "声纹检测失败",
    1104: "声纹与名人相似度过高，请更换音频",
    1105: "获取音频数据失败",
    1106: "音色ID重复",
    1107: "音色ID未找到，请检查 VOLC_TTS_SPEAKER_ID",
    1108: "音频转码失败",
    1109: "音频与参考文本差异过大",
    1111: "音频不含说话声",
    1112: "信噪比异常",
    1114: "音频质量过低",
    1122: "未检测到人声",
    1123: "同一音色已达 10 次上传上限",
}


class VolcTTSError(Exception):
    """与 FishAudioError / CloudVoiceError 对齐的错误结构"""

    def __init__(self, code, message, status=502, hint=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint or ERROR_HINTS.get(code, message)
        self.details = details or {}

    def to_dict(self):
        return {"error": self.message, "code": self.code, "hint": self.hint, "details": self.details}


def _guess_format(data: bytes) -> str:
    head = (data or b"")[:12]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    return "wav"


def _split_text(text: str):
    """按 UTF-8 字节上限切分，优先在标点处断句"""
    chunks = []
    buf = ""
    for ch in text:
        if len((buf + ch).encode("utf-8")) > MAX_TEXT_BYTES:
            cut = max(buf.rfind("。"), buf.rfind("！"), buf.rfind("？"), buf.rfind("；"), buf.rfind("，"))
            if cut > 0 and cut >= len(buf) // 2:
                chunks.append(buf[: cut + 1])
                buf = buf[cut + 1 :] + ch
            else:
                chunks.append(buf)
                buf = ch
        else:
            buf += ch
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


class VolcTTSService:
    prefix = "volc"  # voice_id 形如 "volc:S_xxxxx"

    def __init__(self):
        self.appid = os.environ.get("VOLC_TTS_APPID", "").strip()
        self.token = os.environ.get("VOLC_TTS_ACCESS_TOKEN", "").strip()
        self.speaker_id = os.environ.get("VOLC_TTS_SPEAKER_ID", "").strip()

    @property
    def enabled(self):
        return bool(self.appid and self.token and self.speaker_id)

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer; {self.token}",
            "Resource-Id": RESOURCE_ID,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _body(resp):
        try:
            return resp.json()
        except ValueError:
            return {}

    def _check(self, resp, data, action):
        code = data.get("BaseResp", {}).get("StatusCode", data.get("code"))
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                raise VolcTTSError("voice_define_unsupported", f"火山引擎鉴权失败（HTTP {resp.status_code}）", status=501)
            if resp.status_code == 429:
                raise VolcTTSError("voice_quota_exhausted", "火山引擎限流（HTTP 429）", status=429)
            raise VolcTTSError("voice_upstream_error", f"火山引擎 {action} 失败（HTTP {resp.status_code}）", status=502)
        if code not in (0, None):
            msg = data.get("BaseResp", {}).get("StatusMessage") or data.get("message") or str(code)
            hint = _UPLOAD_STATUS.get(code)
            if code in (1101, 1102, 1111, 1114, 1122):
                raise VolcTTSError("voice_audio_invalid", f"{hint or msg}（{code}）", status=422)
            if code == 1123:
                raise VolcTTSError("voice_quota_unavailable", f"{hint}（{code}）", status=402)
            if code in (1104, 1106, 1107, 1108, 1109, 1112, 1113, 1121):
                raise VolcTTSError("voice_train_failed", f"{hint or msg}（{code}）", status=422)
            raise VolcTTSError("voice_upstream_error", f"{msg}（{code}）", status=502, details={"upstream_code": code})
        return data

    async def create_voice(self, title: str, audio: bytes) -> str:
        """上传参考音频训练到控制台预置音色位，返回 speaker_id"""
        if not self.enabled:
            raise VolcTTSError("voice_define_unsupported", "未配置火山引擎凭证", status=501)
        if not audio:
            raise VolcTTSError("voice_audio_invalid", "缺少参考音频", status=422)
        payload = {
            "appid": self.appid,
            "speaker_id": self.speaker_id,
            "audios": [{
                "audio_bytes": base64.b64encode(audio).decode(),
                "audio_format": _guess_format(audio),
            }],
            "source": 2,
            "language": 0,
            "model_type": 4,  # 声音复刻 ICL2.0
            "extra_params": json.dumps({"enable_audio_denoise": False}),
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{VOLC_HOST}/api/v1/mega_tts/audio/upload",
                    headers=self._auth_headers(),
                    json=payload,
                )
                data = self._check(resp, self._body(resp), "upload")
                speaker_id = data.get("speaker_id") or self.speaker_id
                await self._wait_ready(client, speaker_id)
        except httpx.TimeoutException as e:
            raise VolcTTSError("voice_define_timeout", f"火山引擎训练超时: {e}", status=504)
        except httpx.HTTPError as e:
            raise VolcTTSError("voice_upstream_error", f"火山引擎连接失败: {e}", status=502)
        return speaker_id

    async def _wait_ready(self, client: httpx.AsyncClient, speaker_id: str, timeout_s: int = 90):
        """轮询训练状态直到成功（2/4）或失败（3）"""
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            resp = await client.post(
                f"{VOLC_HOST}/api/v1/mega_tts/status",
                headers=self._auth_headers(),
                json={"appid": self.appid, "speaker_id": speaker_id},
            )
            data = self._check(resp, self._body(resp), "status")
            status = data.get("status")
            if status in (2, 4):
                return True
            if status == 3:
                raise VolcTTSError("voice_train_failed", "音色训练失败，请更换参考音频", status=422)
            if asyncio.get_event_loop().time() > deadline:
                raise VolcTTSError("voice_define_timeout", "音色训练超时（90s 内未完成）", status=504)
            await asyncio.sleep(2)

    async def synthesize(self, text: str, reference_id: str = None, speed: float = 1.0):
        """用克隆音色合成，返回 (mp3_bytes, 24000)。超长文本按 900 字节切分后拼接。"""
        if not self.enabled:
            raise VolcTTSError("voice_define_unsupported", "未配置火山引擎凭证", status=501)
        speaker = (reference_id or self.speaker_id).strip()
        if not speaker:
            raise VolcTTSError("voice_define_unsupported", "缺少音色ID", status=501)
        text = (text or "").strip()
        if not text:
            raise VolcTTSError("voice_audio_invalid", "合成文本为空", status=422)
        parts = []
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                for chunk in _split_text(text):
                    payload = {
                        "app": {"appid": self.appid, "token": self.token, "cluster": CLUSTER},
                        "user": {"uid": "yuban"},
                        "audio": {
                            "voice_type": speaker,
                            "encoding": "mp3",
                            "rate": SAMPLE_RATE,
                            "speed_ratio": round(max(0.5, min(2.0, float(speed))), 2),
                        },
                        "request": {
                            "reqid": uuid.uuid4().hex,
                            "text": chunk,
                            "operation": "query",
                        },
                    }
                    resp = await client.post(f"{VOLC_HOST}/api/v1/tts", headers=self._auth_headers(), json=payload)
                    data = self._check(resp, self._body(resp), "tts")
                    audio_b64 = data.get("data") or ""
                    if not audio_b64:
                        raise VolcTTSError("voice_upstream_error", "火山引擎返回空音频", status=502)
                    parts.append(base64.b64decode(audio_b64))
        except httpx.TimeoutException as e:
            raise VolcTTSError("voice_define_timeout", f"火山引擎合成超时: {e}", status=504)
        except httpx.HTTPError as e:
            raise VolcTTSError("voice_upstream_error", f"火山引擎连接失败: {e}", status=502)
        return b"".join(parts), SAMPLE_RATE


volc_tts_service = VolcTTSService()
