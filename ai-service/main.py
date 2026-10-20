from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi import WebSocket, WebSocketDisconnect
import uvicorn
import base64
import json
import logging
import sys
import os
import asyncio
import time
import torch

# 添加当前目录和项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fun_audio_chat.fun_audio_chat_service import FunAudioChatService

# ===== GPU/CPU 自动切换 =====
# 有 CUDA → 加载 GPU 版服务（optimize=True, 高质量参数, 异步合成）
# 无 CUDA → 加载 CPU 版服务（optimize=False, 快速模式, 同步合成）
_USE_GPU = torch.cuda.is_available()

if _USE_GPU:
    from voxcpm2.voxcpm2_service_gpu import VoxCPM2ServiceGPU as _ServiceClass, \
        DEFAULT_CFG_VALUE, DEFAULT_TIMESTEPS, QUICK_CFG_VALUE, QUICK_TIMESTEPS
    print("[启动] 检测到 CUDA，使用 GPU 版 VoxCPM2 服务")
else:
    from voxcpm2.voxcpm2_service import VoxCPM2Service as _ServiceClass, \
        DEFAULT_CFG_VALUE, DEFAULT_TIMESTEPS, QUICK_CFG_VALUE, QUICK_TIMESTEPS
    print("[启动] 未检测到 CUDA，使用 CPU 版 VoxCPM2 服务")

from llm_service import LLMService
from character_engine.character_engine import CharacterEngine
from asr_service import asr_service
from pydantic import BaseModel
from typing import Optional, List
import io
import wave

# ===== Prometheus 指标 =====
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

AI_REQUESTS = Counter(
    "ai_requests_total", "AI 请求总数", ["endpoint", "status"]
)
AI_LATENCY = Histogram(
    "ai_request_duration_seconds", "AI 请求耗时", ["endpoint"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)
GPU_MEMORY_USED = Gauge(
    "ai_gpu_memory_used_bytes", "GPU 显存已用字节数", ["device"]
)
GPU_MEMORY_TOTAL = Gauge(
    "ai_gpu_memory_total_bytes", "GPU 显存总字节数", ["device"]
)

# 简单的内存存储实现
class InMemoryStorage:
    def __init__(self):
        self.dialogue_history = {}
        self.short_term_memory = {}
    
    def update_dialogue_history(self, session_id, message):
        if session_id not in self.dialogue_history:
            self.dialogue_history[session_id] = []
        self.dialogue_history[session_id].insert(0, message.encode('utf-8'))
    
    def get_dialogue_history(self, session_id):
        return self.dialogue_history.get(session_id, [])
    
    def set_short_term_memory(self, session_id, key, value):
        if session_id not in self.short_term_memory:
            self.short_term_memory[session_id] = {}
        self.short_term_memory[session_id][key] = value
    
    def get_short_term_memory(self, session_id, key):
        if session_id not in self.short_term_memory:
            return None
        return self.short_term_memory[session_id].get(key, None)

# ===== 结构化日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("yuban.ai")

# ===== 优雅退出说明 =====
# Uvicorn 内置处理 SIGINT/SIGTERM，server.serve() 会在收到信号后优雅返回。
# 退出后在下方统一释放 GPU 显存等资源。

app = FastAPI()

# ===== 安全配置 =====
# CORS 白名单：默认仅放行本机前端；生产经 nginx 同源代理时无需放开
_CORS_ENV = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
_ALLOW_ORIGINS = [
    o.strip()
    for o in (
        _CORS_ENV
        or "http://localhost:8080,http://127.0.0.1:8080,http://localhost:8899"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# 上传大小上限（与网关保持一致，防磁盘/内存打满）
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024


def _too_large(data: bytes) -> bool:
    return len(data) > _MAX_UPLOAD_BYTES


def _internal_error(endpoint: str, exc: Exception) -> dict:
    """对外隐藏异常细节，详情仅写服务端日志"""
    logger.error("[%s] %s", endpoint, exc)
    return {"error": "服务内部错误"}

# 初始化服务
aio_service = FunAudioChatService()
voxcpm2_service = _ServiceClass()
llm_service = LLMService()
character_engine = CharacterEngine()
memory_storage = InMemoryStorage()


# ===== 角色管理 =====

class VoiceProfile(BaseModel):
    gender: str = "female"          # female | male
    age_group: str = "young_adult"  # teen | young_adult | middle_aged | elderly
    voice_tags: List[str] = []      # gentle/sweet/magnetic/...
    speed: float = 1.0

class PersonaProfile(BaseModel):
    identity: str = ""              # 身份/职业
    relation: str = "friend"        # friend | mentor | partner | family | companion
    personality_tags: List[str] = []
    speaking_style: str = ""
    free_description: str = ""

class CharacterCreate(BaseModel):
    name: str
    voice_profile: Optional[VoiceProfile] = None
    persona_profile: Optional[PersonaProfile] = None


@app.get("/api/ai/characters/options")
async def get_character_options():
    """获取角色创建向导所需的枚举选项"""
    return {"status": "ok", "options": character_engine.list_options()}


@app.get("/api/ai/characters")
async def list_characters():
    """列出全部角色（官方预设 + 用户自定义）"""
    return {"status": "ok", "characters": character_engine.list_characters()}


@app.get("/api/ai/characters/{character_id}")
async def get_character(character_id: str):
    ch = character_engine.get_character(character_id)
    if not ch:
        return {"error": "角色不存在"}
    return {"status": "ok", "character": ch}


@app.post("/api/ai/characters")
async def create_character(payload: CharacterCreate):
    """创建自定义角色"""
    ch = character_engine.create_character(
        name=payload.name,
        voice_profile=payload.voice_profile.dict() if payload.voice_profile else None,
        persona_profile=payload.persona_profile.dict() if payload.persona_profile else None,
    )
    return {"status": "ok", "character": ch}


@app.post("/api/ai/sessions/{session_id}/character")
async def bind_character(session_id: str, character_id: str = Form(...)):
    """为会话绑定角色（对话和合成均使用该角色的声音与人设）"""
    ch = character_engine.get_character(character_id)
    if not ch:
        return {"error": "角色不存在"}
    memory_storage.set_short_term_memory(session_id, "character_id", character_id)
    # 同步刷新会话语音描述（voice_profile → 英文 prompt），供 TTS 使用
    voice_prompt = character_engine.build_voice_prompt(character_id)
    memory_storage.set_short_term_memory(session_id, "voice_description", voice_prompt)
    return {
        "status": "ok",
        "character_id": character_id,
        "character_name": ch["name"],
        "voice_prompt": voice_prompt,
        "system_prompt": character_engine.build_system_prompt(character_id),
    }


@app.get("/api/ai/sessions/{session_id}/character")
async def get_session_character(session_id: str):
    """获取会话当前绑定的角色"""
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    if not character_id:
        return {"status": "ok", "character": None}
    ch = character_engine.get_character(character_id)
    return {"status": "ok", "character": ch}


def _resolve_session_style(session_id: str, explicit_description: str = None):
    """
    解析 TTS 语音风格，优先级：
    1. 本次请求显式传入的 description
    2. 会话绑定角色的 voice_profile（英文 prompt）
    3. 会话中直接保存的 voice_description（旧版语音定义）
    """
    if explicit_description:
        return explicit_description
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    if character_id:
        voice_prompt = character_engine.build_voice_prompt(character_id)
        if voice_prompt:
            return voice_prompt
    return memory_storage.get_short_term_memory(session_id, "voice_description")

# 语音处理接口
@app.post("/api/ai/process")
async def process_voice(request: Request):
    data = await request.json()
    session_id = data.get("session_id")
    audio_data_hex = data.get("audio_data")
    
    if not session_id or not audio_data_hex:
        return {"error": "Missing session_id or audio_data"}
    
    try:
        # 解码音频数据
        audio_data = bytes.fromhex(audio_data_hex)
        if _too_large(audio_data):
            return {"error": f"音频文件超过上限（{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"}
        
        # 处理音频
        response = aio_service.process_audio(audio_data)
        
        # 保存对话历史
        memory_storage.update_dialogue_history(session_id, f"User: [audio]\nAI: {response}")
        
        # 生成语音响应
        voice_response = aio_service.get_voice_response(response)
        voice_response_hex = voice_response.hex()
        
        return {
            "status": "ok",
            "response": response,
            "voice_response": voice_response_hex
        }
    except Exception as e:
        return _internal_error("api-handler", e)

# 获取AI响应
@app.get("/api/ai/response/{session_id}")
async def get_response(session_id: str):
    try:
        # 获取对话历史
        dialogue_history = memory_storage.get_dialogue_history(session_id)
        
        # 获取最新的AI响应
        if dialogue_history:
            last_message = dialogue_history[0].decode('utf-8')
            if last_message.startswith("AI: "):
                return {"response": last_message[4:]}
        
        return {"response": ""}
    except Exception as e:
        return _internal_error("api-handler", e)

# 语音定义接口
@app.post("/api/ai/define-voice")
async def define_voice(
    session_id: str = Form(...),
    description: str = Form(...),
    audio: UploadFile = File(...),
    pitch: float = Form(1.0),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    emotion_intensity: float = Form(0.5)
):
    """
    使用VoxCPM2定义语音
    session_id: 会话ID
    description: 语音描述（如"温暖的女声"、"低沉的男声"等）
    audio: 参考音频文件
    pitch: 音调（范围：0.5-1.5，默认：1.0）
    speed: 语速（范围：0.5-2.0，默认：1.0）
    volume: 音量（范围：0.5-1.5，默认：1.0）
    emotion_intensity: 情感强度（范围：0.0-1.0，默认：0.5）
    """
    try:
        # 读取音频文件
        audio_data = await audio.read()
        if _too_large(audio_data):
            return {"error": f"音频文件超过上限（{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"}

        # 构建语音参数（cfg_value/inference_timesteps 为微调测试固化的推理参数）
        voice_params = {
            "pitch": pitch,
            "speed": speed,
            "volume": volume,
            "emotionIntensity": emotion_intensity,
            "cfg_value": DEFAULT_CFG_VALUE,
            "inference_timesteps": DEFAULT_TIMESTEPS
        }

        # 使用VoxCPM2定义语音
        voice_definition = voxcpm2_service.define_voice(audio_data, description, voice_params)

        # 生成语音嵌入
        voice_embedding = voxcpm2_service.generate_voice_embedding(voice_definition)

        # 保存语音定义和嵌入到内存存储
        memory_storage.set_short_term_memory(session_id, "voice_definition", voice_definition)
        memory_storage.set_short_term_memory(session_id, "voice_embedding", voice_embedding.tolist())
        memory_storage.set_short_term_memory(session_id, "voice_params", voice_params)
        memory_storage.set_short_term_memory(session_id, "voice_description", description)

        return {
            "status": "ok",
            "voice_definition": voice_definition,
            "voice_params": voice_params
        }
    except Exception as e:
        return _internal_error("api-handler", e)

# 为了兼容API Gateway的调用，添加/api/voice/define端点
@app.post("/api/voice/define")
async def define_voice_compat(
    session_id: str = Form(...),
    description: str = Form(...),
    audio: UploadFile = File(...),
    pitch: float = Form(1.0),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    emotion_intensity: float = Form(0.5)
):
    """
    使用VoxCPM2定义语音
    session_id: 会话ID
    description: 语音描述（如"温暖的女声"、"低沉的男声"等）
    audio: 参考音频文件
    pitch: 音调（范围：0.5-1.5，默认：1.0）
    speed: 语速（范围：0.5-2.0，默认：1.0）
    volume: 音量（范围：0.5-1.5，默认：1.0）
    emotion_intensity: 情感强度（范围：0.0-1.0，默认：0.5）
    """
    try:
        # 读取音频文件
        audio_data = await audio.read()
        if _too_large(audio_data):
            return {"error": f"音频文件超过上限（{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"}

        # 构建语音参数（cfg_value/inference_timesteps 为微调测试固化的推理参数）
        voice_params = {
            "pitch": pitch,
            "speed": speed,
            "volume": volume,
            "emotionIntensity": emotion_intensity,
            "cfg_value": DEFAULT_CFG_VALUE,
            "inference_timesteps": DEFAULT_TIMESTEPS
        }

        # 使用VoxCPM2定义语音
        voice_definition = voxcpm2_service.define_voice(audio_data, description, voice_params)

        # 生成语音嵌入
        voice_embedding = voxcpm2_service.generate_voice_embedding(voice_definition)

        # 保存语音定义和嵌入到内存存储
        memory_storage.set_short_term_memory(session_id, "voice_definition", voice_definition)
        memory_storage.set_short_term_memory(session_id, "voice_embedding", voice_embedding.tolist())
        memory_storage.set_short_term_memory(session_id, "voice_params", voice_params)
        memory_storage.set_short_term_memory(session_id, "voice_description", description)

        return {
            "status": "ok",
            "voice_definition": voice_definition,
            "voice_params": voice_params
        }
    except Exception as e:
        return _internal_error("api-handler", e)

# TTS语音合成接口（使用固化的VoxCPM2推理参数）
@app.post("/api/ai/tts")
async def tts(
    session_id: str = Form(...),
    text: str = Form(...),
    description: str = Form(None),
    quick_mode: bool = Form(False)
):
    """
    合成语音
    session_id: 会话ID
    text: 要合成的文本
    description: 可选，本次合成的语音风格描述；缺省时使用会话已定义的语音，否则按普通语音合成
    quick_mode: 可选，快速模式（CFG=1.5/Steps=6，速度提升约40%）
    返回: audio/wav 音频流
    """
    _start = time.time()
    if not text.strip():
        AI_REQUESTS.labels(endpoint="tts", status="400").inc()
        return {"error": "text 不能为空"}

    # 风格优先级：显式 description > 会话绑定角色 voice_profile > 旧版语音定义
    style = _resolve_session_style(session_id, description)
    emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
    emotion_params = emotion_config.get("params") if emotion_config else None

    # GPU 版 synthesize 是 async，CPU 版同步 → 统一放线程池/await，避免阻塞事件循环
    loop = asyncio.get_running_loop()
    if _USE_GPU:
        wav_bytes, sample_rate = await voxcpm2_service.synthesize(
            text,
            style_description=style,
            emotion_params=emotion_params if not style else None,
            quick_mode=quick_mode
        )
    else:
        wav_bytes, sample_rate = await loop.run_in_executor(
            None,
            lambda: voxcpm2_service.synthesize(
                text,
                style_description=style,
                emotion_params=emotion_params if not style else None,
                quick_mode=quick_mode
            )
        )

    if wav_bytes is None:
        AI_REQUESTS.labels(endpoint="tts", status="503").inc()
        AI_LATENCY.labels(endpoint="tts").observe(time.time() - _start)
        return {"error": "VoxCPM2 模型不可用"}

    AI_REQUESTS.labels(endpoint="tts", status="200").inc()
    AI_LATENCY.labels(endpoint="tts").observe(time.time() - _start)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"X-Sample-Rate": str(sample_rate)}
    )


# 语音转写接口（语音输入：wav → 文本）
@app.post("/api/ai/transcribe")
async def transcribe_audio(session_id: str = Form("default"),
                           audio: UploadFile = File(...)):
    """
    接收前端录音 WAV（16kHz/16bit/单声道），调用 DashScope Paraformer 转写。
    返回 {"status":"ok","text":"..."}
    """
    if not asr_service.available():
        return {"error": "语音识别未配置（缺少 LLM_API_KEY）"}

    raw = await audio.read()
    if _too_large(raw):
        return {"error": f"音频文件超过上限（{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"}
    if not raw:
        return {"error": "音频为空"}

    try:
        # 解析 WAV，提取 PCM（前端已按 16kHz/单声道编码；这里按实际头信息读取）
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())

        # 非 16k/16bit/单声道时做简单处理提示（前端应保证格式）
        if framerate != 16000 or channels != 1 or sampwidth != 2:
            return {"error": f"音频格式不支持（需要 16kHz/16bit/单声道，当前 {framerate}Hz/{channels}ch/{sampwidth*8}bit）"}

        text = await asr_service.transcribe_pcm(pcm, sample_rate=16000)
        if not text:
            return {"status": "ok", "text": "", "warning": "未识别到语音内容，请靠近麦克风重试"}
        return {"status": "ok", "text": text}
    except Exception as e:
        print(f"[ASR] 转写失败: {e}")
        return _internal_error("asr/transcribe", e)

# 情感配置接口
@app.post("/api/ai/emotion-config")
async def emotion_config(
    session_id: str = Form(...),
    emotion_name: str = Form(...),
    emotion_description: str = Form(""),
    happiness: float = Form(0.5),
    sadness: float = Form(0.1),
    anger: float = Form(0.1),
    fear: float = Form(0.1),
    disgust: float = Form(0.1),
    surprise: float = Form(0.3)
):
    """
    配置情感参数
    session_id: 会话ID
    emotion_name: 情感配置名称
    emotion_description: 情感描述
    happiness: 愉悦程度（范围：0.0-1.0，默认：0.5）
    sadness: 悲伤程度（范围：0.0-1.0，默认：0.1）
    anger: 愤怒程度（范围：0.0-1.0，默认：0.1）
    fear: 恐惧程度（范围：0.0-1.0，默认：0.1）
    disgust: 厌恶程度（范围：0.0-1.0，默认：0.1）
    surprise: 惊讶程度（范围：0.0-1.0，默认：0.3）
    """
    try:
        # 构建情感参数
        emotion_params = {
            "happiness": happiness,
            "sadness": sadness,
            "anger": anger,
            "fear": fear,
            "disgust": disgust,
            "surprise": surprise
        }
        
        # 保存情感配置到内存存储
        emotion_config = {
            "name": emotion_name,
            "description": emotion_description,
            "params": emotion_params
        }
        
        memory_storage.set_short_term_memory(session_id, "emotion_config", emotion_config)
        
        return {
            "status": "ok",
            "emotion_config": emotion_config
        }
    except Exception as e:
        return _internal_error("api-handler", e)

# 获取情感配置接口
@app.get("/api/ai/emotion-config/{session_id}")
async def get_emotion_config(session_id: str):
    """
    获取情感配置
    session_id: 会话ID
    """
    try:
        emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
        
        return {
            "status": "ok",
            "emotion_config": emotion_config
        }
    except Exception as e:
        return _internal_error("api-handler", e)

# 健康检查
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "device": "cuda" if _USE_GPU else "cpu",
        "tts_service": "gpu" if _USE_GPU else "cpu",
        "llm_service": llm_service.provider if llm_service.config["api_key"] else "unconfigured"
    }


# ===== Prometheus 指标端点（生产应在 nginx 层限制仅内网可达）=====
@app.get("/metrics")
async def metrics():
    # 抓取时刷新 GPU 显存指标
    if _USE_GPU and torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            used = torch.cuda.memory_allocated(i)
            total = torch.cuda.get_device_properties(i).total_memory
            GPU_MEMORY_USED.labels(device=f"cuda:{i}").set(used)
            GPU_MEMORY_TOTAL.labels(device=f"cuda:{i}").set(total)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# ===== WebSocket 实时对话端点（GPU 模式专属，全链路流式）=====
# 流程：用户文本 → LLM 流式生成 → 按句切 → TTS 合成 → 推音频块
# 仅 GPU 模式有实际意义（CPU 太慢），但 CPU 模式也可用（会慢）
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str):
    await ws.accept()

    # 加载会话上下文
    emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
    emotion_params = emotion_config.get("params") if emotion_config else None

    # 角色：voice_profile → 声音 prompt；persona_profile → LLM system prompt
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    style = None
    system_prompt = None
    character_name = None
    if character_id:
        style = character_engine.build_voice_prompt(character_id)
        system_prompt = character_engine.build_system_prompt(character_id)
        ch = character_engine.get_character(character_id)
        character_name = ch["name"] if ch else None
    if not style:
        style = memory_storage.get_short_term_memory(session_id, "voice_description")

    # 对话历史
    chat_history = memory_storage.get_short_term_memory(session_id, "chat_history") or []

    # WebSocket 并发发送锁（TTS 后台任务与主循环都会 send）
    send_lock = asyncio.Lock()

    async def safe_send_json(payload):
        async with send_lock:
            await ws.send_json(payload)

    async def safe_send_bytes(data):
        async with send_lock:
            await ws.send_bytes(data)

    try:
        while True:
            # 等待用户消息
            data = await ws.receive_json()
            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            chat_history.append({"role": "user", "content": user_text})

            # 每轮重新解析角色（用户可能在对话中途切换角色）
            emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
            emotion_params = emotion_config.get("params") if emotion_config else None
            character_id = memory_storage.get_short_term_memory(session_id, "character_id")
            style = None
            system_prompt = None
            character_name = None
            if character_id:
                style = character_engine.build_voice_prompt(character_id)
                system_prompt = character_engine.build_system_prompt(character_id)
                ch = character_engine.get_character(character_id)
                character_name = ch["name"] if ch else None
            if not style:
                style = memory_storage.get_short_term_memory(session_id, "voice_description")

            # 推送状态：开始生成（含当前角色名，前端可展示）
            await safe_send_json({
                "type": "status", "status": "generating",
                "character": character_name
            })

            assistant_reply = ""
            messages_for_llm = [{"role": h["role"], "content": h["content"]} for h in chat_history[-20:]]
            tts_tasks = []

            async def tts_task(sentence_text, idx):
                """单句 TTS 合成并推送（后台并行，不阻塞文本流/事件循环）"""
                try:
                    loop = asyncio.get_running_loop()
                    if _USE_GPU:
                        result = voxcpm2_service.synthesize(
                            sentence_text,
                            style_description=style,
                            emotion_params=emotion_params if not style else None,
                            quick_mode=False
                        )
                        wav_bytes, sample_rate = await result
                    else:
                        # CPU 同步推理放线程池，避免阻塞事件循环（WS ping/其他请求）
                        wav_bytes, sample_rate = await loop.run_in_executor(
                            None,
                            lambda: voxcpm2_service.synthesize(
                                sentence_text,
                                style_description=style,
                                emotion_params=emotion_params if not style else None,
                                quick_mode=True
                            )
                        )
                    if wav_bytes:
                        await safe_send_bytes(wav_bytes)
                        await safe_send_json({
                            "type": "audio", "index": idx,
                            "sample_rate": sample_rate, "size": len(wav_bytes)
                        })
                except Exception as e:
                    print(f"[WS] 第{idx}句 TTS 失败: {e}")

            # LLM 流式生成 → 推文本；TTS 后台并行
            sentence_count = 0
            async for sentence in llm_service.chat_stream_sentences(
                messages_for_llm, system_prompt=system_prompt
            ):
                sentence_count += 1
                assistant_reply += sentence
                await safe_send_json({"type": "text", "content": sentence, "index": sentence_count})
                tts_tasks.append(asyncio.create_task(tts_task(sentence, sentence_count)))

            # 文本回复已完整 → 立即通知前端恢复输入（不必等慢速 TTS）
            await safe_send_json({"type": "status", "status": "reply_done", "sentences": sentence_count})

            # 保存 LLM 回复全文到对话历史
            if assistant_reply:
                chat_history.append({"role": "assistant", "content": assistant_reply})
                memory_storage.set_short_term_memory(session_id, "chat_history", chat_history)

            # 等待所有 TTS 音频推送完成
            if tts_tasks:
                await asyncio.gather(*tts_tasks)
            await safe_send_json({"type": "status", "status": "done", "sentences": sentence_count})

    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {session_id}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await safe_send_json({"type": "error", "message": "服务内部错误"})
        except Exception:
            pass

if __name__ == "__main__":
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8084,
        log_config=None,  # 使用自定义 logging 配置
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())

    # 服务完全关闭后清理资源
    logger.info("正在释放资源...")
    if _USE_GPU and torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.info("GPU 显存已释放")
    logger.info("AI 服务已完全关闭")
