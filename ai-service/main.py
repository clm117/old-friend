from fastapi import FastAPI, Request, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import Response
from fastapi import WebSocket, WebSocketDisconnect
import uvicorn
import base64
import json
import logging
import sys
import os
import asyncio
import hashlib
import hmac
import time
from urllib.parse import quote

# torch 仅在 voxcpm2 本地 TTS 模式需要；edge/cloud 模式免 GPU，延迟导入避免 CPU 环境缺 torch 报错
torch = None
_USE_GPU = False
_TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "edge").strip().lower()
if _TTS_PROVIDER == "voxcpm2":
    try:
        import torch
        _USE_GPU = torch.cuda.is_available()
    except ImportError:
        print("[启动] 警告：TTS_PROVIDER=voxcpm2 但 torch 未安装，VoxCPM2 本地模型不可用")

# 添加当前目录和项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 加载同目录 .env（TTS_PROVIDER / CLOUD_TTS_VOICE 等），不覆盖已存在的环境变量
def _load_dotenv():
    from pathlib import Path
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

# ===== TTS 提供方：edge（edge-tts 免费日常合成）| cloud（云 TTS）| voxcpm2（本地模型）=====
# 育儿教练场景：edge-tts 已足够，声音克隆整条链路移除
# _TTS_PROVIDER 已在文件顶部根据 .env 加载值初始化；此处无需再次赋值
_TTS_MEDIA_TYPE = "audio/mpeg" if _TTS_PROVIDER == "edge" else "audio/wav"

# FunAudioChatService（语音对话 ASR 入口）—— 仅在本地 TTS 模式下实例化
# cloud 模式跳过：该服务依赖本地 transformers，且 cloud 模式下 /api/ai/process 端点不会被前端调用

voxcpm2_service = None
if _TTS_PROVIDER == "voxcpm2" and torch is not None:
    if _USE_GPU:
        from voxcpm2.voxcpm2_service_gpu import VoxCPM2ServiceGPU as _ServiceClass, \
            DEFAULT_CFG_VALUE, DEFAULT_TIMESTEPS, QUICK_CFG_VALUE, QUICK_TIMESTEPS
        print("[启动] TTS_PROVIDER=voxcpm2，检测到 CUDA，使用 GPU 版 VoxCPM2 服务")
    else:
        from voxcpm2.voxcpm2_service import VoxCPM2Service as _ServiceClass, \
            DEFAULT_CFG_VALUE, DEFAULT_TIMESTEPS, QUICK_CFG_VALUE, QUICK_TIMESTEPS
        print("[启动] TTS_PROVIDER=voxcpm2，未检测到 CUDA，使用 CPU 版 VoxCPM2 服务")
    voxcpm2_service = _ServiceClass()
elif _TTS_PROVIDER == "voxcpm2":
    print("[启动] TTS_PROVIDER=voxcpm2 但 torch 未安装，跳过本地模型加载")

from llm_service import LLMService
from character_engine.character_engine import CharacterEngine
from asr_service import asr_service
from state_store import PersistentStateStore
from pydantic import BaseModel, Field
from typing import Optional, List
import io
import wave
import voice_catalog

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

# 状态持久化：会话 / 对话历史 / 情感配置 / 语音绑定（JSON 落盘 + Redis 可选）
memory_storage = PersistentStateStore()

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
if _TTS_PROVIDER in ("cloud", "edge"):
    # 云端 TTS 模式：跳过 FunAudioChatService（本地模型）实例化，省 import/加载时间
    class _MockAudioService:
        def process_audio(self, *a, **kw):
            return "云端 TTS 模式：Fun-Audio-Chat 未启用"
        def get_voice_response(self, *a, **kw):
            return b""
        def streaming_process(self, *a, **kw):
            return "云端 TTS 模式"
    aio_service = _MockAudioService()
    print(f"[启动] TTS_PROVIDER={_TTS_PROVIDER}，FunAudioChatService 跳过（mock 占位）")
else:
    from fun_audio_chat.fun_audio_chat_service import FunAudioChatService
    aio_service = FunAudioChatService()

cloud_tts_service = None
cloud_voice = None
edge_tts_service = None
# voice_clone 已移除（育儿教练场景合规红线 + 无业务必要）

if _TTS_PROVIDER == "cloud":
    from cloud_tts_service import cloud_tts_service
    from cloud_voice_service import cloud_voice_service as cloud_voice, CloudVoiceError
    print("[启动] TTS_PROVIDER=cloud，使用云 TTS")
elif _TTS_PROVIDER == "edge":
    from edge_tts_service import edge_tts_service
    print("[启动] TTS_PROVIDER=edge，edge-tts 日常合成（声音克隆已移除）")
else:
    print(f"[启动] TTS_PROVIDER={_TTS_PROVIDER}，使用 VoxCPM2 本地模型")
llm_service = LLMService()
character_engine = CharacterEngine()

# ===== 网关注入的可信用户身份 =====
# 网关用共享密钥对 "账号|时间戳" 做 HMAC-SHA256，AI 服务只认签名，避免直连 8084 伪造 X-Yuban-User
_INTERNAL_TOKEN = (os.environ.get("AI_INTERNAL_TOKEN") or os.environ.get("JWT_SECRET") or "").strip()
_INTERNAL_TTL_SECONDS = 300


def _trusted_user(request) -> Optional[str]:
    """校验网关签名头，返回账号；不通过返回 None"""
    user = (request.headers.get("x-yuban-user") or "").strip()
    ts = (request.headers.get("x-yuban-ts") or "").strip()
    sig = (request.headers.get("x-yuban-sig") or "").strip()
    if not _INTERNAL_TOKEN or not user or not ts or not sig:
        return None
    try:
        if abs(time.time() - int(ts)) > _INTERNAL_TTL_SECONDS:
            return None
    except ValueError:
        return None
    expected = hmac.new(
        _INTERNAL_TOKEN.encode(), f"{user}|{ts}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user


def _unauthorized(msg="缺少可信用户身份"):
    return JSONResponse({"error": msg}, status_code=401)


def _forbidden(msg="无权访问该会话"):
    return JSONResponse({"error": msg}, status_code=403)


def _session_owner_error(session_id: str, user: str):
    """会话归属校验：已删除会话 404；历史会话（无 owner）放行；有 owner 且不等于当前用户则 403"""
    if memory_storage.is_deleted(session_id):
        return JSONResponse({"error": "会话不存在或已删除"}, status_code=404)
    owner = memory_storage.get_session_owner(session_id)
    if owner is not None and owner != user:
        return _forbidden()
    return None


async def _synthesize_speech(text, style, emotion_params, quick_mode, speed=1.0, voice_id=None):
    """
    统一 TTS 入口：
    - cloud 模式：走 DashScope 云 TTS
    - edge 模式：走 edge-tts（声音克隆已移除）
    - voxcpm2 模式：走本地模型
    speed: 语速（0.5-2.0）
    返回 (audio_bytes, sample_rate)；audio_bytes 为 None 表示服务不可用
    """
    if _TTS_PROVIDER == "cloud":
        return await cloud_tts_service.synthesize(
            text, voice=voice_id, quick_mode=quick_mode, speed=speed
        )
    if _TTS_PROVIDER == "edge":
        return await edge_tts_service.synthesize(
            text, voice=voice_id, quick_mode=quick_mode, speed=speed
        )
    if _USE_GPU:
        return await voxcpm2_service.synthesize(
            text,
            style_description=style,
            emotion_params=emotion_params,
            quick_mode=quick_mode,
            speed=speed,
        )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: voxcpm2_service.synthesize(
            text,
            style_description=style,
            emotion_params=emotion_params,
            quick_mode=quick_mode,
            speed=speed,
        ),
    )


# ===== 角色管理 =====

class VoiceProfile(BaseModel):
    gender: str = "female"          # female | male
    age_group: str = "young_adult"  # teen | young_adult | middle_aged | elderly
    voice_tags: List[str] = []      # gentle/sweet/magnetic/...
    # 语速 0.5–2.0，默认 1.0；越界由 FastAPI 返回 422（GAP-08）
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # 方言结构位（GAP-05 第 3 批）：yue/sichuan/dongbei；未就绪时回退普通话音色并提示
    dialect: Optional[str] = None

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

class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    voice_profile: Optional[VoiceProfile] = None
    persona_profile: Optional[PersonaProfile] = None


@app.get("/api/ai/characters/options")
async def get_character_options():
    """获取角色创建向导所需的枚举选项"""
    return {"status": "ok", "options": character_engine.list_options()}


@app.get("/api/ai/voice-catalog")
async def get_voice_catalog():
    """音色目录（GAP-05）：标签/方言结构位/预设音色/默认回退，供前端与后续扩展使用"""
    return {"status": "ok", "catalog": voice_catalog.get_catalog()}


@app.get("/api/ai/voice-presets")
async def list_voice_presets():
    """12 条预设音色目录（含就绪状态与未就绪提示）"""
    presets = voice_catalog.preset_list()
    return {
        "status": "ok",
        "presets": presets,
        "count": len(presets),
        "ready": any(voice_catalog.preset_available(p["id"]) for p in presets),
    }


@app.get("/api/ai/voice-presets/{preset_id}/prompt")
async def get_voice_preset_prompt(preset_id: str):
    """预设音色 → voice_prompt；未绑定真实 voice_id 时显式给出回退提示"""
    status = voice_catalog.preset_status(preset_id)
    if not status:
        return JSONResponse({"error": "预设音色不存在"}, status_code=404)
    return {
        "status": "ok",
        "preset_id": preset_id,
        "name": status["name"],
        "voice_prompt": voice_catalog.preset_prompt(preset_id),
        "voice_id": status["voice_id"],
        "ready": status["ready"],
        "notice": status["notice"],
        "capabilities": status["capabilities"],
    }


@app.get("/api/ai/voice-design/capability")
async def voice_design_capability():
    """声音定义能力：edge 模式下声音克隆已移除，仅 cloud 模式支持"""
    if _TTS_PROVIDER == "cloud" and cloud_voice is not None:
        cap = cloud_voice.capability()
    else:
        cap = {
            "supported": False,
            "provider": _TTS_PROVIDER,
            "clone": {"supported": False, "reason": "声音克隆已移除（育儿教练场景合规要求）"},
            "design": {"supported": False, "reason": "仅 cloud 模式支持声音设计"},
            "quota": {"limit": None, "used": None},
            "rate_limit": None,
            "last_error": None,
            "verified": False,
        }
    return {"status": "ok", "capability": cap}


@app.get("/api/ai/characters")
async def list_characters(request: Request):
    """列出当前用户可见的角色（官方预设 + 历史共享 + 本人自定义）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    return {"status": "ok", "characters": character_engine.list_characters(owner=user)}


@app.get("/api/ai/characters/{character_id}")
async def get_character(character_id: str, request: Request):
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    ch = character_engine.get_character(character_id)
    if not ch:
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    if not character_engine.is_accessible(ch, user):
        # 不向非归属者泄露角色是否存在
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    return {"status": "ok", "character": ch}


@app.post("/api/ai/characters")
async def create_character(payload: CharacterCreate, request: Request):
    """创建自定义角色（归属当前登录账号）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    ch = character_engine.create_character(
        name=payload.name,
        voice_profile=payload.voice_profile.dict() if payload.voice_profile else None,
        persona_profile=payload.persona_profile.dict() if payload.persona_profile else None,
        owner=user,
    )
    return {"status": "ok", "character": ch}


@app.put("/api/ai/characters/{character_id}")
@app.patch("/api/ai/characters/{character_id}")
async def update_character(character_id: str, payload: CharacterUpdate, request: Request):
    """修改自定义角色：仅归属者可改；预设/历史共享角色只读；重名 409"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    ch = character_engine.get_character(character_id)
    if not ch:
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    if ch.get("is_preset"):
        return JSONResponse({"error": "预设角色不可修改"}, status_code=403)
    if ch.get("shared") or ch.get("owner") != user:
        return JSONResponse({"error": "无权修改该角色"}, status_code=403)
    if payload.name is not None and character_engine.name_conflict(character_id, payload.name, user):
        return JSONResponse({"error": "角色名称已存在"}, status_code=409)
    updated = character_engine.update_character(
        character_id,
        name=payload.name,
        voice_profile=payload.voice_profile.dict() if payload.voice_profile else None,
        persona_profile=payload.persona_profile.dict() if payload.persona_profile else None,
    )
    if not updated:
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    return {"status": "ok", "character": updated}


@app.delete("/api/ai/characters/{character_id}")
async def delete_character(character_id: str, request: Request):
    """删除自定义角色（预设角色不可删除；非归属者 403）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    ch = character_engine.get_character(character_id)
    if not ch:
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    if ch.get("is_preset"):
        return JSONResponse({"error": "预设角色不可删除"}, status_code=400)
    if not character_engine.is_accessible(ch, user) or ch.get("shared") or ch.get("owner") != user:
        return JSONResponse({"error": "无权删除该角色"}, status_code=403)
    character_engine.delete_character(character_id)
    return {"status": "ok", "deleted_id": character_id}


# ===== 孩子档案 API（育儿教练核心）=====
_CHILD_AGE_STAGES = {"toddler", "preschool", "early", "middle", "preteen", "teen"}
_CHILD_TEMPERAMENTS = {"sensitive", "active", "calm", "stubborn", "social"}
_MAX_CHILDREN_PER_USER = 5
_CHILD_STORE: dict[str, list[dict]] = {}  # owner -> [child dict]


def _children_of(owner: str) -> list[dict]:
    return _CHILD_STORE.setdefault(owner, [])


def _new_child_id() -> str:
    import uuid
    return "ch_" + uuid.uuid4().hex[:12]


@app.get("/api/ai/children")
async def list_children(request: Request):
    """列出当前用户的孩子档案"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    return {"status": "ok", "children": _children_of(user)}


@app.post("/api/ai/children")
async def create_child(request: Request):
    """创建孩子档案（最多 5 个）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "无效请求体"}, status_code=400)

    name = (body.get("name") or "").strip()[:20]
    age_stage = (body.get("age_stage") or "").strip()
    temperament = (body.get("temperament") or "").strip()
    sensitivities = (body.get("sensitivities") or "")[:200]

    if not name:
        return JSONResponse({"error": "昵称不能为空"}, status_code=400)
    if age_stage not in _CHILD_AGE_STAGES:
        return JSONResponse({"error": "年龄段无效"}, status_code=400)
    if temperament not in _CHILD_TEMPERAMENTS:
        return JSONResponse({"error": "气质类型无效"}, status_code=400)

    children = _children_of(user)
    if len(children) >= _MAX_CHILDREN_PER_USER:
        return JSONResponse({"error": f"最多 {_MAX_CHILDREN_PER_USER} 个孩子档案"}, status_code=400)

    child = {
        "id": _new_child_id(),
        "name": name,
        "age_stage": age_stage,
        "temperament": temperament,
        "sensitivities": sensitivities,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    children.append(child)
    return {"status": "ok", "child": child}


@app.put("/api/ai/children/{child_id}")
async def update_child(child_id: str, request: Request):
    """更新孩子档案"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "无效请求体"}, status_code=400)

    children = _children_of(user)
    target = None
    for c in children:
        if c["id"] == child_id:
            target = c
            break
    if target is None:
        return JSONResponse({"error": "孩子档案不存在"}, status_code=404)

    if "name" in body:
        target["name"] = (body["name"] or "").strip()[:20] or target["name"]
    if "age_stage" in body and body["age_stage"] in _CHILD_AGE_STAGES:
        target["age_stage"] = body["age_stage"]
    if "temperament" in body and body["temperament"] in _CHILD_TEMPERAMENTS:
        target["temperament"] = body["temperament"]
    if "sensitivities" in body:
        target["sensitivities"] = (body["sensitivities"] or "")[:200]
    target["updated_at"] = time.time()
    return {"status": "ok", "child": target}


@app.delete("/api/ai/children/{child_id}")
async def delete_child(child_id: str, request: Request):
    """删除孩子档案"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    children = _children_of(user)
    for i, c in enumerate(children):
        if c["id"] == child_id:
            children.pop(i)
            return {"status": "ok", "deleted_id": child_id}
    return JSONResponse({"error": "孩子档案不存在"}, status_code=404)


@app.post("/api/ai/parenting-advice")
async def parenting_advice(request: Request):
    """
    育儿急救：根据场景描述 + 孩子档案，生成话术卡
    请求体: { scene: str, child_id?: str, child_ids?: [str], tone_profile_id?: str }
    返回: { status, advice: { say, not_say, why, next_steps } }
    """
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "无效请求体"}, status_code=400)

    scene = (body.get("scene") or "").strip()
    if not scene:
        return JSONResponse({"error": "请描述一下场景"}, status_code=400)
    if len(scene) > 500:
        scene = scene[:500]

    # 组装孩子档案上下文
    child_ids = body.get("child_ids") or []
    if body.get("child_id") and body["child_id"] not in child_ids:
        child_ids.append(body["child_id"])
    all_children = _children_of(user)
    context_children = [c for c in all_children if c["id"] in child_ids] or []

    stage_map = {
        "toddler": "1-3岁学步期（自我意识萌芽、语言发展、安全探索）",
        "preschool": "3-6岁学龄前（规则意识建立、社交能力、情绪管理初学）",
        "early": "6-9岁低年级（入学适应、学习习惯、同伴关系）",
        "middle": "9-12岁高年级（独立意识、学业压力、同伴影响增强）",
        "preteen": "12-14岁青春期前（生理变化、自我认同、亲子冲突增多）",
        "teen": "14-18岁青春期（追求独立、情绪波动大、同伴价值高于家长）",
    }
    temp_map = {
        "sensitive": "敏感型（情绪细腻、容易被批评伤到、需要温柔引导）",
        "active": "活跃型（精力旺盛、坐不住、需要释放渠道）",
        "calm": "沉稳型（节奏慢、需要耐心等待、不喜欢被催促）",
        "stubborn": "执拗型（有主见、需要讲道理、不接受强压）",
        "social": "社交型（外向、在意朋友看法、需要在群体中给面子）",
    }

    child_ctx_parts = []
    for c in context_children:
        part = f"孩子「{c['name']}」：{stage_map.get(c['age_stage'], c['age_stage'])}，{temp_map.get(c['temperament'], c['temperament'])}"
        if c.get("sensitivities"):
            part += f"，敏感点：{c['sensitivities']}"
        child_ctx_parts.append(part)
    child_ctx = "\n".join(child_ctx_parts) if child_ctx_parts else ""

    system = (
        "你是家长的育儿沟通教练，精通正面管教和非暴力沟通。\n"
        "根据家长描述的现场和孩子情况，输出一张话术卡。\n"
        "严格按以下四行格式输出，每行开头必须有标签，不要额外解释：\n"
        "此刻可以说：（家长此刻能逐字说的1-3句话）\n"
        "不要说：（家长绝对不能说的1-2句具体原话，不是泛化标签）\n"
        "为什么：（先点出孩子行为背后的发展阶段需求，再解释 say/not_say）\n"
        "后续步骤：（1-3条有主语有动作有时间点的可执行指令）\n"
        "\n"
        "质量红线：\n"
        "- 不要说 必须是具体原话如'你怎么这么不懂事'，禁止'不要情绪化''不要翻旧账'这种标签\n"
        "- 为什么 必须先点出发展阶段如'4岁处于秩序敏感期''13岁青春期追求独立'，再解释\n"
        "- 后续步骤 每条必须有'家长+动作+时间点'如'今晚睡前和孩子聊聊，10分钟'\n"
        "- 紧急情况（孩子自伤/自杀/严重心理危机）在 为什么 开头加'⚠️紧急：'\n"
    )

    user_msg = f"场景：{scene}"
    if child_ctx:
        user_msg += f"\n\n孩子情况：\n{child_ctx}"
    user_msg += "\n\n再次提醒：为什么字段必须点出发展阶段需求，后续步骤每条必须有时间点。"

    try:
        advice_text = await llm_service.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=512,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning("[parenting-advice] LLM 调用失败: %s", e)
        return JSONResponse({"error": "AI 服务暂时不可用，请稍后再试"}, status_code=503)

    logger.info("[parenting-advice] LLM 原始输出前 500 字: %s", advice_text[:500])

    # 从可能包裹 markdown 的文本中提取 JSON
    import re
    advice_json = advice_text
    match = re.search(r'\{[\s\S]*\}', advice_text)
    if match:
        advice_json = match.group(0)
    try:
        advice = json.loads(advice_json)
    except json.JSONDecodeError as e:
        logger.warning("[parenting-advice] JSON 解析失败: %s, 尝试从纯文本提取", e)
        advice = _parse_plain_advice(advice_text)

    # 字段兜底 + 紧急标记检测
    advice.setdefault("say", advice_text[:120])
    advice.setdefault("not_say", "")
    advice.setdefault("why", "")
    advice.setdefault("next_steps", "")
    advice.setdefault("emergency", "⚠️紧急" in advice_text or "紧急" in advice.get("why", ""))

    return {"status": "ok", "advice": advice}


def _parse_plain_advice(text: str) -> dict:
    """
    LLM 偶尔不输出 JSON 而是输出纯文本格式：
      此刻可以说：xxx
      不要说：xxx
      为什么：xxx
      后续步骤：xxx
    按关键词切分，提取各字段。
    """
    import re
    result = {"say": "", "not_say": "", "why": "", "next_steps": ""}
    # 统一关键词到英文 key
    patterns = [
        ("say",         [r'此刻可以说[：:]', r'可以说[：:]', r'say[：:]']),
        ("not_say",     [r'不要说[：:]', r'不要这样说[：:]', r'禁止说[：:]', r'not[_\s]*say[：:]']),
        ("why",         [r'为什么[：:]', r'原因[：:]', r'why[：:]']),
        ("next_steps",  [r'后续步骤[：:]', r'事后可以做[：:]', r'之后可以做[：:]', r'next[_\s]*steps[：:]']),
    ]
    # 找到每个字段的起始位置
    spans = []
    for key, pats in patterns:
        for p in pats:
            m = re.search(p, text)
            if m:
                spans.append((m.start(), m.end(), key))
                break
    spans.sort()
    # 按起止位置切分
    for i, (start, sep_end, key) in enumerate(spans):
        field_start = sep_end
        field_end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        value = text[field_start:field_end].strip().strip('。.\n')
        result[key] = value
    return result


@app.post("/api/ai/sessions")
async def create_session(request: Request):
    """创建会话：session_id 由服务端随机生成并绑定归属账号"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "").strip()[:64] if isinstance(body, dict) else ""
    session_id = memory_storage.create_session(user, title=title)
    return {"status": "ok", "session_id": session_id, "owner": user}


@app.get("/api/ai/sessions")
async def list_sessions(request: Request, limit: int = Query(20, ge=1, le=100),
                        offset: int = Query(0, ge=0)):
    """列出当前账号的会话（分页，按最近更新倒序）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    items, total = memory_storage.list_sessions(user, limit=limit, offset=offset)
    return {
        "status": "ok",
        "sessions": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class SessionRename(BaseModel):
    title: str


@app.patch("/api/ai/sessions/{session_id}")
async def rename_session(session_id: str, payload: SessionRename, request: Request):
    """重命名会话（仅归属者）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    title = (payload.title or "").strip()
    if not title:
        return JSONResponse({"error": "会话标题不能为空"}, status_code=400)
    if len(title) > 64:
        return JSONResponse({"error": "会话标题过长（上限 64 字）"}, status_code=400)
    if memory_storage.is_deleted(session_id) or not memory_storage.has_session(session_id):
        return JSONResponse({"error": "会话不存在或已删除"}, status_code=404)
    owner = memory_storage.get_session_owner(session_id)
    if owner is not None and owner != user:
        return _forbidden()
    memory_storage.claim_session(session_id, user)
    memory_storage.rename_session(session_id, title)
    return {"status": "ok", "session": memory_storage.get_session_meta(session_id)}


@app.delete("/api/ai/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """删除会话（仅归属者）；删除后写墓碑，后续读写 404、WS 1008"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    if memory_storage.is_deleted(session_id) or not memory_storage.has_session(session_id):
        return JSONResponse({"error": "会话不存在或已删除"}, status_code=404)
    owner = memory_storage.get_session_owner(session_id)
    if owner is not None and owner != user:
        return _forbidden()
    memory_storage.delete_session(session_id)
    return Response(status_code=204)


@app.get("/api/ai/sessions/{session_id}/owner")
async def get_session_owner(session_id: str):
    """内部接口：供网关在 WS 建连前校验会话归属（不对外暴露）"""
    return {
        "status": "ok",
        "exists": memory_storage.has_session(session_id),
        "deleted": memory_storage.is_deleted(session_id),
        "owner": memory_storage.get_session_owner(session_id),
    }


@app.post("/api/ai/sessions/{session_id}/character")
async def bind_character(session_id: str, request: Request, character_id: str = Form(...)):
    """为会话绑定角色（对话和合成均使用该角色的声音与人设）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    ch = character_engine.get_character(character_id)
    if not ch:
        return JSONResponse({"error": "角色不存在"}, status_code=404)
    if not character_engine.is_accessible(ch, user):
        return JSONResponse({"error": "无权使用该角色"}, status_code=403)
    err = _session_owner_error(session_id, user)
    if err:
        return err
    # 历史会话无归属时由首次写入者接管，避免旧数据全部不可用
    memory_storage.claim_session(session_id, user)
    memory_storage.set_short_term_memory(session_id, "character_id", character_id)
    # 绑定角色即接管音色，清除此前选择的预设音色
    memory_storage.set_short_term_memory(session_id, "voice_preset_id", "")
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
async def get_session_character(session_id: str, request: Request):
    """获取会话当前绑定的角色"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    if not character_id:
        return {"status": "ok", "character": None}
    ch = character_engine.get_character(character_id)
    return {"status": "ok", "character": ch}


class VoiceStyleBind(BaseModel):
    description: str                       # 语音风格描述（英文 prompt，供 TTS 使用）
    voice_definition: Optional[str] = None  # 可选：VoxCPM2 声音定义字符串
    voice_params: Optional[dict] = None     # 可选：VoxCPM2 声音参数（pitch/speed 等）


@app.post("/api/ai/sessions/{session_id}/voice-style")
async def bind_voice_style(session_id: str, payload: VoiceStyleBind, request: Request):
    """为会话绑定语音风格（覆盖角色自带的 voice_prompt）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)
    if payload.description:
        memory_storage.set_short_term_memory(session_id, "voice_description", payload.description)
        # 显式语音风格优先于预设音色，清除预设选择
        memory_storage.set_short_term_memory(session_id, "voice_preset_id", "")
    if payload.voice_definition:
        memory_storage.set_short_term_memory(session_id, "voice_definition", payload.voice_definition)
    if payload.voice_params:
        memory_storage.set_short_term_memory(session_id, "voice_params", json.dumps(payload.voice_params))
    return {"status": "ok", "session_id": session_id, "voice_description": payload.description}


class VoicePresetBind(BaseModel):
    preset_id: str


@app.post("/api/ai/sessions/{session_id}/voice-preset")
async def bind_voice_preset(session_id: str, payload: VoicePresetBind, request: Request):
    """为会话选择预设音色：写入 voice_prompt；真实 voice_id 未绑定时显式提示默认音色"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    status = voice_catalog.preset_status(payload.preset_id)
    if not status:
        return JSONResponse({"error": "预设音色不存在"}, status_code=404)
    err = _session_owner_error(session_id, user)
    if err:
        return err
    prompt = voice_catalog.preset_prompt(payload.preset_id)
    memory_storage.claim_session(session_id, user)
    memory_storage.set_short_term_memory(session_id, "voice_preset_id", payload.preset_id)
    memory_storage.set_short_term_memory(session_id, "voice_description", prompt)
    # 清除旧版直接绑定，避免与预设 prompt 冲突
    memory_storage.set_short_term_memory(session_id, "voice_definition", "")
    return {
        "status": "ok",
        "session_id": session_id,
        "preset_id": payload.preset_id,
        "name": status["name"],
        "voice_prompt": prompt,
        "voice_id": status["voice_id"],
        "ready": status["ready"],
        "notice": status["notice"],
        "capabilities": status["capabilities"],
        "capability_note": status.get("capability_note"),
    }


def _resolve_session_style(session_id: str, explicit_description: str = None):
    """
    解析 TTS 语音风格，优先级：
    1. 本次请求显式传入的 description
    2. 会话选择的预设音色 voice_prompt（GAP-05）
    3. 会话绑定角色的 voice_profile（英文 prompt）
    4. 会话中直接保存的 voice_description（旧版语音定义）
    """
    if explicit_description:
        return explicit_description
    preset_id = memory_storage.get_short_term_memory(session_id, "voice_preset_id")
    if preset_id:
        prompt = voice_catalog.preset_prompt(preset_id)
        if prompt:
            return prompt
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    if character_id:
        voice_prompt = character_engine.build_voice_prompt(character_id)
        if voice_prompt:
            return voice_prompt
    return memory_storage.get_short_term_memory(session_id, "voice_description")


def _resolve_session_speed(session_id: str, character_id: str = None) -> float:
    """解析会话语速：绑定角色的 voice_profile.speed，缺省 1.0；越界兜底钳制（GAP-08）"""
    cid = character_id or memory_storage.get_short_term_memory(session_id, "character_id")
    if not cid:
        return 1.0
    ch = character_engine.get_character(cid)
    vp = (ch or {}).get("voice_profile") or {}
    try:
        return max(0.5, min(2.0, float(vp.get("speed", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def _resolve_session_voice(session_id: str, character_id: str = None):
    """
    解析 voice_id，返回 (voice_id, notice)：
    - edge 模式：无克隆音色，返回 None（edge 默认音色）
    - cloud/voxcpm2：0. 预设音色 → 1. cloud_voice 映射 → 2. 角色方言音色
    notice 为 None 表示无需提示；不静默失败
    """
    if _TTS_PROVIDER == "edge":
        return None, None
    notice = None
    preset_id = memory_storage.get_short_term_memory(session_id, "voice_preset_id")
    if preset_id:
        status = voice_catalog.preset_status(preset_id)
        if status and status["ready"]:
            return status["voice_id"], None
        if status:
            # 预设未绑定真实 voice_id：不沿用历史云音色，明确提示回退默认音色
            return None, status["notice"]
    if cloud_voice is not None:
        voice_id, note = cloud_voice.get_voice(session_id)
        notice = note
        if voice_id:
            return voice_id, notice
    cid = character_id or memory_storage.get_short_term_memory(session_id, "character_id")
    if cid:
        _dialect, ids, dialect_note = character_engine.resolve_dialect_voice(cid)
        if notice is None:
            notice = dialect_note
        if ids:
            return ids[0], notice
    return None, notice


def _parse_hints(raw: str):
    return [h.strip() for h in (raw or "").split(",") if h.strip()] or None


async def _define_voice_cloud(request, session_id, description, audio, mode,
                              voice_prompt, preview_text, language_hints):
    """云声音定义统一走 voice-enrollment：设计/复刻 → voice_id 映射落库"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)

    try:
        audio_data = await audio.read() if audio is not None else b""
        if audio_data and _too_large(audio_data):
            return JSONResponse(
                {"error": f"音频文件超过上限（{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"},
                status_code=413,
            )
        is_design = mode == "design" or (not audio_data and (voice_prompt or "").strip())
        if is_design:
            entry = await cloud_voice.create_design(
                session_id, voice_prompt, preview_text, description or voice_prompt
            )
            definition = f"design:{voice_prompt}"
        else:
            entry = await cloud_voice.create_clone(
                session_id, audio_data, description, _parse_hints(language_hints)
            )
            definition = f"cloud:{entry['voice_id']}"
        memory_storage.set_short_term_memory(session_id, "voice_definition", definition)
        memory_storage.set_short_term_memory(session_id, "voice_description", description)
        memory_storage.set_short_term_memory(session_id, "cloud_voice_id", entry["voice_id"])
        # 云声音定义优先，清除预设选择
        memory_storage.set_short_term_memory(session_id, "voice_preset_id", "")
        return {
            "status": "ok",
            "mode": "cloud",
            "voice_id": entry["voice_id"],
            "target_model": entry["target_model"],
            "definition_mode": entry["mode"],
            "voice_definition": definition,
            "voice_params": {"speed": 1.0},
        }
    except CloudVoiceError as e:
        logger.warning("[define-voice] 云声音定义失败: %s %s", e.code, e.message)
        return JSONResponse({**e.to_dict(), "mode": "cloud"}, status_code=e.status)
    except Exception as e:
        return _internal_error("define-voice-cloud", e)


async def _define_voice_edge(request, session_id, description, audio, mode, voice_prompt):
    """edge 模式声音定义：声音克隆已移除，统一返回 501"""
    return JSONResponse(
        {"error": "声音克隆已移除（育儿教练场景合规要求）；日常合成不受影响",
         "code": "voice_define_removed", "mode": "edge"},
        status_code=501,
    )

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
async def get_response(session_id: str, request: Request):
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
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
    request: Request,
    session_id: str = Form(...),
    description: str = Form(...),
    audio: Optional[UploadFile] = File(None),
    pitch: float = Form(1.0),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    emotion_intensity: float = Form(0.5),
    mode: str = Form("auto"),
    voice_prompt: str = Form(""),
    preview_text: str = Form(""),
    language_hints: str = Form(""),
):
    """
    定义语音（duplicate endpoint，见 /api/voice/define）
    - cloud 模式：走 voice-enrollment 复刻（上传音频）或设计（voice_prompt+preview_text）
    - 本地模式：使用 VoxCPM2 自定义音色
    失败分支统一返回明确 code（欠费 402 / 限流 429 / 超时 504 / 不可用 501）
    """
    if _TTS_PROVIDER == "edge":
        return await _define_voice_edge(request, session_id, description, audio, mode, voice_prompt)
    if _TTS_PROVIDER == "cloud":
        return await _define_voice_cloud(
            request, session_id, description, audio, mode, voice_prompt, preview_text, language_hints
        )

    if voxcpm2_service is None:
        return JSONResponse(
            {"error": "当前音色模式不支持自定义音色", "code": "voice_define_unsupported",
             "mode": _TTS_PROVIDER},
            status_code=501,
        )
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)

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
    request: Request,
    session_id: str = Form(...),
    description: str = Form(""),
    audio: Optional[UploadFile] = File(None),
    pitch: float = Form(1.0),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    emotion_intensity: float = Form(0.5),
    mode: str = Form("auto"),
    voice_prompt: str = Form(""),
    preview_text: str = Form(""),
    language_hints: str = Form(""),
):
    """
    定义语音（网关兼容端点）
    - cloud 模式：voice-enrollment 复刻/设计
    - 本地模式：VoxCPM2 自定义音色
    """
    if _TTS_PROVIDER == "edge":
        return await _define_voice_edge(request, session_id, description, audio, mode, voice_prompt)
    if _TTS_PROVIDER == "cloud":
        return await _define_voice_cloud(
            request, session_id, description, audio, mode, voice_prompt, preview_text, language_hints
        )

    if voxcpm2_service is None:
        return JSONResponse(
            {"error": "当前音色模式不支持自定义音色", "code": "voice_define_unsupported",
             "mode": _TTS_PROVIDER},
            status_code=501,
        )
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)

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
    text: str = Form(""),
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
    speed = _resolve_session_speed(session_id)
    voice_id, voice_notice = _resolve_session_voice(session_id)
    emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
    emotion_params = emotion_config.get("params") if emotion_config else None

    # 统一 TTS 入口（云/本地 GPU/CPU 分派见 _synthesize_speech）
    try:
        wav_bytes, sample_rate = await _synthesize_speech(
            text, style, emotion_params if not style else None, quick_mode, speed=speed, voice_id=voice_id
        )
    except Exception as e:
        # 云 TTS 欠费/鉴权失败/超时等：返回 503 而不是 500，前端可提示稍后重试
        logger.error("[tts] 合成失败: %s", e)
        AI_REQUESTS.labels(endpoint="tts", status="503").inc()
        AI_LATENCY.labels(endpoint="tts").observe(time.time() - _start)
        return JSONResponse({"error": "TTS 服务暂时不可用（请检查云端余额或稍后重试）"}, status_code=503)

    if wav_bytes is None:
        AI_REQUESTS.labels(endpoint="tts", status="503").inc()
        AI_LATENCY.labels(endpoint="tts").observe(time.time() - _start)
        return JSONResponse({"error": "TTS 服务不可用（模型加载失败）"}, status_code=503)

    AI_REQUESTS.labels(endpoint="tts", status="200").inc()
    AI_LATENCY.labels(endpoint="tts").observe(time.time() - _start)
    headers = {"X-Sample-Rate": str(sample_rate)}
    if voice_notice:
        # 中文提示放 header 需转义；前端 decodeURIComponent 还原
        headers["X-Voice-Notice"] = quote(voice_notice)
    return Response(
        content=wav_bytes,
        media_type=_TTS_MEDIA_TYPE,
        headers=headers
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

# 情感预设（GAP-09）：6 类预设映射到既有 6 轴向量；历史自由文本名称继续可存可读
EMOTION_PRESETS = [
    {"id": "calm",      "name": "平静", "description": "平和、稳定的语气",
     "params": {"happiness": 0.3, "sadness": 0.1, "anger": 0.1, "fear": 0.1, "disgust": 0.1, "surprise": 0.1}},
    {"id": "happy",     "name": "开心", "description": "轻快、愉悦的语气",
     "params": {"happiness": 0.9, "sadness": 0.05, "anger": 0.05, "fear": 0.05, "disgust": 0.05, "surprise": 0.5}},
    {"id": "sad",       "name": "悲伤", "description": "低落、柔和的语气",
     "params": {"happiness": 0.1, "sadness": 0.9, "anger": 0.1, "fear": 0.2, "disgust": 0.1, "surprise": 0.1}},
    {"id": "anxious",   "name": "焦虑", "description": "紧张、不安的语气",
     "params": {"happiness": 0.1, "sadness": 0.4, "anger": 0.2, "fear": 0.8, "disgust": 0.1, "surprise": 0.5}},
    {"id": "angry",     "name": "生气", "description": "强烈、急促的语气",
     "params": {"happiness": 0.05, "sadness": 0.1, "anger": 0.9, "fear": 0.1, "disgust": 0.3, "surprise": 0.1}},
    {"id": "surprised", "name": "惊讶", "description": "意外、上扬的语气",
     "params": {"happiness": 0.4, "sadness": 0.05, "anger": 0.05, "fear": 0.2, "disgust": 0.05, "surprise": 0.9}},
]


@app.get("/api/ai/emotion-presets")
async def list_emotion_presets():
    """情感预设列表（前端下拉只列这 6 项；自定义名称仍可自由保存）"""
    return {
        "status": "ok",
        "axes": ["happiness", "sadness", "anger", "fear", "disgust", "surprise"],
        "presets": EMOTION_PRESETS,
    }


# 情感配置接口
@app.post("/api/ai/emotion-config")
async def emotion_config(
    request: Request,
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
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    if len(emotion_name) > 64:
        return JSONResponse({"error": "emotion_name 过长（上限 64 字）"}, status_code=400)
    if len(emotion_description) > 500:
        return JSONResponse({"error": "emotion_description 过长（上限 500 字）"}, status_code=400)
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)
    try:
        # clamp 所有情感值到 [0, 1]，与文档注释一致
        _c = lambda x: max(0.0, min(1.0, float(x)))
        # 构建情感参数
        emotion_params = {
            "happiness": _c(happiness),
            "sadness": _c(sadness),
            "anger": _c(anger),
            "fear": _c(fear),
            "disgust": _c(disgust),
            "surprise": _c(surprise)
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
async def get_emotion_config(session_id: str, request: Request):
    """
    获取情感配置
    session_id: 会话ID
    """
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    try:
        emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
        
        return {
            "status": "ok",
            "emotion_config": emotion_config
        }
    except Exception as e:
        return _internal_error("api-handler", e)


@app.delete("/api/ai/emotion-config/{session_id}")
async def delete_emotion_config(session_id: str, request: Request):
    """解绑情感配置：清空 short_term.emotion_config（幂等，GET 仍返回空配置）"""
    user = _trusted_user(request)
    if not user:
        return _unauthorized()
    err = _session_owner_error(session_id, user)
    if err:
        return err
    memory_storage.claim_session(session_id, user)
    memory_storage.delete_short_term_memory(session_id, "emotion_config")
    return Response(status_code=204)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "device": "cuda" if _USE_GPU else "cpu",
        "tts_service": _TTS_PROVIDER if _TTS_PROVIDER != "voxcpm2" else ("gpu" if _USE_GPU else "cpu"),
        "llm_service": llm_service.provider if llm_service.config["api_key"] else "unconfigured"
    }


# 云 TTS 依赖探测结果缓存（避免每次就绪检查都发起合成请求）
_READY_CLOUD_CACHE = {"ts": 0.0, "ok": None, "detail": ""}
_READY_CLOUD_TTL = 120


async def _cloud_ready_probe():
    """真实探测远端 TTS 依赖（欠费/鉴权失败时返回不可用），结果缓存 120 秒；本地 voxcpm2 直接通过"""
    if _TTS_PROVIDER == "edge":
        if edge_tts_service is None:
            return False, "edge_tts_not_configured"
        if not edge_tts_service.available():
            return False, "edge_tts_not_installed"
        probe = lambda: edge_tts_service.synthesize("好", quick_mode=True)
    elif _TTS_PROVIDER == "cloud":
        if cloud_tts_service is None:
            return False, "cloud_tts_not_configured"
        if not cloud_tts_service.available():
            return False, "cloud_tts_api_key_missing"
        probe = lambda: cloud_tts_service.synthesize("好", quick_mode=True)
    else:
        return True, ""
    now = time.time()
    cached = _READY_CLOUD_CACHE
    if cached["ok"] is not None and now - cached["ts"] < _READY_CLOUD_TTL:
        return cached["ok"], cached["detail"]
    ok, detail = False, ""
    try:
        audio, _sr = await probe()
        ok = bool(audio)
        detail = "" if ok else "remote_tts_returned_empty"
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:160]}"
    _READY_CLOUD_CACHE.update({"ts": now, "ok": ok, "detail": detail})
    return ok, detail


# 就绪探针：TTS 提供方已初始化 + 云依赖可用 + 状态存储可写，供网关 /ready 与容器编排使用
@app.get("/ready")
async def readiness_check():
    problems: list[str] = []
    if _TTS_PROVIDER == "voxcpm2" and voxcpm2_service is None:
        problems.append("tts_model_not_loaded")
    cloud_ok, cloud_detail = await _cloud_ready_probe()
    if not cloud_ok:
        problems.append(f"{_TTS_PROVIDER}_tts_unavailable")
    try:
        memory_storage.flush(force=True)
        state_stats = memory_storage.stats()
    except Exception as e:
        problems.append("state_store_unwritable")
        state_stats = {"error": type(e).__name__}
    payload = {
        "status": "ready" if not problems else "not_ready",
        "problems": problems,
        "device": "cuda" if _USE_GPU else "cpu",
        "tts_service": _TTS_PROVIDER,
        "cloud_dependency": {"ok": cloud_ok, "detail": cloud_detail},
        "state_store": state_stats,
    }
    if problems:
        return JSONResponse(payload, status_code=503)
    return payload


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

# ===== WebSocket 实时对话端点（全链路流式）=====
# 流程：用户文本 → LLM 流式生成 → 按句切 → TTS 合成 → 推音频块
# TTS 提供方见 _TTS_PROVIDER：cloud（云，验证期免 GPU）或 voxcpm2（本地，实时需 GPU）
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str):
    await ws.accept()

    # 会话归属校验：先 accept 再带 1008 关闭，客户端才能收到明确鉴权失败语义
    user = _trusted_user(ws)
    if memory_storage.is_deleted(session_id):
        await ws.close(code=1008, reason="session_deleted")
        return
    owner = memory_storage.get_session_owner(session_id)
    if owner is not None and (not user or owner != user):
        await ws.close(code=1008, reason="forbidden")
        return
    if owner is None and user:
        memory_storage.claim_session(session_id, user)

    # 加载会话上下文
    emotion_config = memory_storage.get_short_term_memory(session_id, "emotion_config")
    emotion_params = emotion_config.get("params") if emotion_config else None

    # 角色：voice_profile → 声音 prompt；persona_profile → LLM system prompt
    character_id = memory_storage.get_short_term_memory(session_id, "character_id")
    style = None
    system_prompt = None
    character_name = None
    voice_speed = 1.0
    voice_id, voice_notice = _resolve_session_voice(session_id, character_id)
    if character_id:
        style = character_engine.build_voice_prompt(character_id)
        voice_speed = _resolve_session_speed(session_id, character_id)
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
            voice_speed = 1.0
            voice_id, voice_notice = _resolve_session_voice(session_id, character_id)
            if character_id:
                style = character_engine.build_voice_prompt(character_id)
                voice_speed = _resolve_session_speed(session_id, character_id)
                system_prompt = character_engine.build_system_prompt(character_id)
                ch = character_engine.get_character(character_id)
                character_name = ch["name"] if ch else None
            if not style:
                style = memory_storage.get_short_term_memory(session_id, "voice_description")

            # 推送状态：开始生成（含当前角色名与方言回退提示，前端可展示）
            await safe_send_json({
                "type": "status", "status": "generating",
                "character": character_name,
                "voice_notice": voice_notice,
            })

            assistant_reply = ""
            messages_for_llm = [{"role": h["role"], "content": h["content"]} for h in chat_history[-20:]]
            tts_tasks = []

            async def tts_task(sentence_text, idx):
                """单句 TTS 合成并推送（后台并行，不阻塞文本流/事件循环）"""
                try:
                    # 统一 TTS 入口（云/本地分派见 _synthesize_speech）
                    # 非 GPU 一律快速模式：CPU 提速；云侧映射为 speech_rate 略升
                    wav_bytes, sample_rate = await _synthesize_speech(
                        sentence_text,
                        style,
                        emotion_params if not style else None,
                        quick_mode=not _USE_GPU,
                        speed=voice_speed,
                        voice_id=voice_id,
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
    try:
        memory_storage.flush(force=True)
        logger.info("状态存储已落盘")
    except Exception as e:
        logger.error("状态落盘失败: %s", e)
    if _USE_GPU and torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.info("GPU 显存已释放")
    logger.info("AI 服务已完全关闭")
