"""
语音转写服务（ASR）—— 阿里云百炼 Paraformer 实时识别

复用 .env 中的 LLM_API_KEY（DashScope Key 对 ASR/LLM 通用）。
协议要点（官方文档）：
1. wss://dashscope.aliyuncs.com/api-ws/v1/inference/，Header 带 Authorization: Bearer <key>
2. 先发 run-task JSON → 等 task-started → 直接发二进制 PCM 帧（不要 continue-task）
3. 音频发完发 finish-task（payload.input 必须是空对象 {}）
4. 结果在 result-generated 事件 payload.output.sentence.text，sentence_end=True 为最终句
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

import websockets

# 复用 llm_service 的 .env 加载逻辑（LLM_API_KEY 即 DashScope Key）
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

ASR_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
ASR_MODEL = "paraformer-realtime-v2"
# 每帧 100ms 音频（16kHz × 16bit × 单声道 = 32000 字节/秒）
_FRAME_BYTES = 3200


class ASRService:
    """DashScope Paraformer 语音转写"""

    def __init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", "")

    def available(self):
        return bool(self.api_key)

    async def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """
        将 16kHz/16bit/单声道 PCM 转写为文本。
        返回识别到的完整文本；失败抛出异常。
        """
        if not self.api_key:
            raise RuntimeError("未配置 LLM_API_KEY（DashScope），无法使用语音识别")

        task_id = str(uuid.uuid4())
        headers = {"Authorization": f"Bearer {self.api_key}"}

        run_task = {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": ASR_MODEL,
                "parameters": {
                    "sample_rate": sample_rate,
                    "format": "pcm",
                    "language_hints": ["zh", "en"],
                },
                "input": {},
            },
        }
        finish_task = {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }

        final_sentences = []
        started = asyncio.Event()
        task_failed = {"msg": None}

        async with websockets.connect(
            ASR_WS_URL, additional_headers=headers, max_size=16 * 1024 * 1024
        ) as ws:

            async def receiver():
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    header = msg.get("header", {})
                    event = header.get("event")
                    if event == "task-started":
                        started.set()
                    elif event == "result-generated":
                        sentence = msg.get("payload", {}).get("output", {}).get("sentence", {})
                        # sentence_end=True 为该句最终结果
                        if sentence.get("sentence_end"):
                            text = (sentence.get("text") or "").strip()
                            if text:
                                final_sentences.append(text)
                    elif event == "task-finished":
                        return
                    elif event == "task-failed":
                        task_failed["msg"] = header.get("error_message", "ASR 任务失败")
                        return

            recv_task = asyncio.create_task(receiver())

            await ws.send(json.dumps(run_task))
            # 等待 task-started（最多 15 秒）
            try:
                await asyncio.wait_for(started.wait(), timeout=15)
            except asyncio.TimeoutError:
                recv_task.cancel()
                raise RuntimeError("ASR 服务启动超时（task-started 未到达）")

            # 按 100ms 分帧发送裸 PCM（协议要求二进制帧，不加 JSON 包裹）
            for i in range(0, len(pcm_bytes), _FRAME_BYTES):
                await ws.send(pcm_bytes[i:i + _FRAME_BYTES])
                await asyncio.sleep(0.02)  # 略微限速，避免瞬时刷满

            await ws.send(json.dumps(finish_task))
            await asyncio.wait_for(recv_task, timeout=30)

        if task_failed["msg"]:
            raise RuntimeError(f"ASR 失败: {task_failed['msg']}")

        return "".join(final_sentences).strip()


# 单例
asr_service = ASRService()
