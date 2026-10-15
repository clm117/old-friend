"""
LLM 对话服务 —— 通义千问 / GLM / DeepSeek API 流式调用

支持多 LLM 后端，流式输出文本，按句切分喂给 TTS。
配置通过 .env 文件或环境变量加载。
"""
import json
import os
import re
from pathlib import Path
from typing import AsyncGenerator

import httpx

# ===== 从 .env 文件加载配置（无需 python-dotenv 依赖）=====
def _load_dotenv():
    """读取 ai-service/.env 文件，注入环境变量"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and val and key not in os.environ:
            os.environ[key] = val

_load_dotenv()

# ===== LLM API 配置 =====
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "qwen")  # qwen | glm | deepseek

LLM_CONFIGS = {
    "qwen": {
        "api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key": os.environ.get("LLM_API_KEY", ""),  # 阿里云 DashScope API Key
        "model": os.environ.get("LLM_MODEL", "qwen-plus"),
    },
    "glm": {
        "api_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "api_key": os.environ.get("LLM_API_KEY", ""),  # 智谱 API Key
        "model": os.environ.get("LLM_MODEL", "glm-4-flash"),
    },
    "deepseek": {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": os.environ.get("LLM_API_KEY", ""),  # DeepSeek API Key
        "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
    },
}

# 系统提示词：语伴 AI 助手角色设定
SYSTEM_PROMPT = (
    "你是语伴，一个温暖、有亲和力的智能语音交互助手。"
    "回答要简洁自然，适合语音播报，避免使用 Markdown 格式和过长的段落。"
    "每次回复控制在 2-3 句话以内，像朋友聊天一样。"
)


class LLMService:
    """LLM 流式对话服务"""

    def __init__(self, provider=None):
        self.provider = provider or LLM_PROVIDER
        self.config = LLM_CONFIGS[self.provider]
        if not self.config["api_key"]:
            print(f"[LLM] 警告: 未配置 API Key（LLM_PROVIDER={self.provider}），"
                  f"请设置环境变量 LLM_API_KEY")

    async def chat_stream(self, messages: list, system_prompt: str = None) -> AsyncGenerator[str, None]:
        """
        流式对话，逐 token 产出文本
        messages: [{"role": "system/user/assistant", "content": "..."}]
        system_prompt: 可选，自定义系统提示词（来自角色 persona_profile）；缺省用默认语伴人设
        yield: 逐个 token 的文本片段
        """
        # 注入系统提示词（角色人设优先于默认人设）；过滤调用方自带的 system 消息
        conversation = [m for m in messages if m.get("role") != "system"]
        full_messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}] + conversation

        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config["model"],
            "messages": full_messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 512,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream("POST", self.config["api_url"],
                                     headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def chat_stream_sentences(self, messages: list, system_prompt: str = None) -> AsyncGenerator[str, None]:
        """
        流式对话，按句产出（遇到句号/问号/感叹号时 yield 一个完整句子）
        专为 TTS pipeline 设计：每个 yield 的句子可直接送合成
        system_prompt: 可选，角色 persona 生成的系统提示词
        """
        buffer = ""
        async for token in self.chat_stream(messages, system_prompt=system_prompt):
            buffer += token
            # 按中英文句末标点切分
            match = re.search(r'[。！？.!?；;]', buffer)
            while match:
                end = match.end()
                sentence = buffer[:end].strip()
                buffer = buffer[end:]
                if sentence:
                    yield sentence
                match = re.search(r'[。！？.!?；;]', buffer)
        # 剩余文本作为最后一句
        if buffer.strip():
            yield buffer.strip()

    async def chat(self, messages: list, system_prompt: str = None) -> str:
        """非流式对话，返回完整文本（简单场景用）"""
        result = ""
        async for token in self.chat_stream(messages, system_prompt=system_prompt):
            result += token
        return result
