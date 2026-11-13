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
from asr_service import asr_service
from state_store import PersistentStateStore
from pydantic import BaseModel
from typing import Optional
import io
import wave
import threading
import re
from pathlib import Path
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


# ===== 孩子档案 API（育儿教练核心）=====
_CHILD_AGE_STAGES = {"toddler", "preschool", "early", "middle", "preteen", "teen"}
_CHILD_TEMPERAMENTS = {"sensitive", "active", "calm", "stubborn", "social"}
_MAX_CHILDREN_PER_USER = 5
_CHILD_STORE: dict[str, list[dict]] = {}  # owner -> [child dict]

# 持久化：默认落盘到 YUBAN_DATA_DIR/children.json（默认 ai-service/data/children.json）
_CHILD_DATA_DIR = Path(os.environ.get("YUBAN_DATA_DIR", Path(__file__).parent / "data"))
_CHILD_FILE = _CHILD_DATA_DIR / "children.json"
_CHILD_LOCK = threading.Lock()


def _load_children():
    """启动时从磁盘加载；文件缺失或损坏时安全降级为空，不影响启动"""
    global _CHILD_STORE
    try:
        if _CHILD_FILE.exists():
            with open(_CHILD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _CHILD_STORE = {k: v for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[children] 档案加载失败，降级为空: {e}")


def _save_children():
    """原子写：先写临时文件再 rename，避免中途崩溃丢数据"""
    try:
        _CHILD_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _CHILD_LOCK:
            tmp = _CHILD_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_CHILD_STORE, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _CHILD_FILE)
    except OSError as e:
        print(f"[children] 档案持久化失败: {e}")


_load_children()


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
    _save_children()
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
    _save_children()
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
            _save_children()
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
        "- 为什么 必须以年龄段开头，格式为「N岁孩子…」，再解释 say/not_say 的依据；禁止只写'避免指责''给孩子安全感'这类结论\n"
        "- 后续步骤 每条必须有'家长+动作+时间点'如'今晚睡前和孩子聊聊，10分钟'\n"
        "- 紧急情况（孩子自伤/自杀/严重心理危机）在 为什么 开头加'⚠️紧急：'\n"
        "- 只输出上述四行，不要复述或改写家长给出的场景，不要加开场白、总结、括号批注\n"
        "\n"
        "示例（场景：5岁孩子在超市要买玩具被拒后大哭）：\n"
        "此刻可以说：妈妈知道你很想要这个玩具，现在不能买，你很难过，对不对？我们先抱一下，然后一起想想怎么攒零花钱。\n"
        "不要说：再哭我就走了，你自己在这儿哭吧\n"
        "为什么：5岁孩子处于情绪调节能力尚未成熟的阶段，需要通过被理解来学会安抚自己；威胁抛弃会破坏安全感，让哭闹升级。\n"
        "后续步骤：当场蹲下平视并拥抱1分钟；当晚睡前用5分钟回顾这件事，教他用'我想要'表达；一周内出门前先约定今天不买玩具。\n"
    )

    user_msg = f"场景：{scene}"
    if child_ctx:
        user_msg += f"\n\n孩子情况：\n{child_ctx}"
    user_msg += (
        "\n\n输出前在心里自检（不要把检查过程、修改说明、括号补充写进输出，只输出四行正文）：\n"
        "1. 为什么 一栏必须以「数字+岁孩子」开头，例如「4岁孩子…」；"
        "如果是「避免指责」「保持积极沟通」这类结论，必须改写成以年龄段开头的因果解释。\n"
        "2. 后续步骤 每条都必须带具体时间点（X分钟后/今晚/明天/本周）。\n"
        "3. 不要说 必须是家长真的会脱口而出的句子，不是形容词。"
    )

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

    if not isinstance(advice, dict):
        advice = _parse_plain_advice(advice_text)

    # 统一清洗：去掉行首列表符号、包裹引号、多余空白（JSON 与纯文本两条路径都走这里）
    for k in ("say", "not_say", "why", "next_steps"):
        advice[k] = _clean_field(advice.get(k, ""))
    if not advice["say"]:
        # 兜底只取首个非空行，避免把整段（含其他字段标签）塞进 say
        first_line = next((ln for ln in advice_text.splitlines() if ln.strip()), "")
        advice["say"] = _clean_field(first_line)[:120]
    advice["emergency"] = bool(
        advice.get("emergency")
        or "⚠️" in advice_text
        or "紧急" in advice["why"]
    )

    return {"status": "ok", "advice": advice}


# 行首列表符号：- * • · 1. 1、1) (1) 等
_BULLET_RE = re.compile(r'^[\s\u3000]*(?:[-*•·]+|\d+[.、)）]|[（(]\d+[）)])\s*')
_QUOTE_PAIRS = ("“", "”"), ("\"", "\""), ("「", "」"), ("『", "』"), ("'", "'")
# 句尾括号里以这些词开头时，视为模型的点评/自检，不属于正文
_META_WORDS = r'(?:此处|避免|保持|注意|建议|提示|说明|补充|备注|原因|目的|这样|为了|注|ps|PS)'
_META_PAREN_RE = re.compile(r'[（(]\s*' + _META_WORDS + r'[^（）()]{0,30}[）)]\s*$')
_META_PAREN_OPEN_RE = re.compile(r'[（(]\s*' + _META_WORDS + r'[^（）()]*$')


def _clean_field(value) -> str:
    """清理 LLM 格式漂移：行首列表符号、包裹引号、多余空行；多条合并为分号分隔"""
    if not value:
        return ""
    items = []
    for line in str(value).replace("\r", "").split("\n"):
        line = _BULLET_RE.sub("", line).strip()
        if not line:
            continue
        # 先去掉句末标点，否则「“原话”，」这种收尾会让包裹引号判断失效
        line = line.rstrip("。.；;，,：:")
        # 去掉整行包裹的引号（可能多次嵌套）
        changed = True
        while changed and len(line) >= 2:
            changed = False
            for left, right in _QUOTE_PAIRS:
                if line.startswith(left) and line.endswith(right) and len(line) > 2:
                    line = line[1:-1].strip()
                    changed = True
        if line:
            items.append(line.rstrip("。.；;，,").strip())
    items = [x for x in items if x]
    # 行内编号（如「…感受。2. 明天…」）拆成独立条目后再去编号
    expanded = []
    for it in items:
        for part in re.split(r'(?<=[。；;])\s*(?=\d+[.、)）])', it):
            part = _BULLET_RE.sub("", part).strip().rstrip("。.；;，,")
            if part:
                expanded.append(part)
    items = expanded
    if not items:
        return ""
    text = items[0] if len(items) == 1 else "；".join(items)
    # 清掉模型自检泄漏进正文的括号说明（括号内含其他字段标签）
    text = re.sub(r'[（(][^（）()]*?(?:为什么|不要说|此刻可以说|后续步骤)[：:][^（）()]*[）)]', '', text)
    # 只清句尾的点评式括号（如「（避免指责）」「（注：语气温和）」），括号内是实质内容的保留
    text = re.sub(_META_PAREN_RE, '', text)
    # 未闭合的尾部括号残片同样只在以点评词开头时才清
    text = re.sub(_META_PAREN_OPEN_RE, '', text)
    return text.strip().rstrip("（(、，,；;：:。.").strip()


def _parse_plain_advice(text: str) -> dict:
    """
    LLM 偶尔不输出 JSON 而是输出纯文本格式：
      此刻可以说：xxx
      不要说：xxx
      为什么：xxx
      后续步骤：xxx
    按关键词切分，提取各字段（字段内容后续由 _clean_field 统一清洗）。
    """
    result = {"say": "", "not_say": "", "why": "", "next_steps": ""}
    # 标签 → 字段；长标签在前，避免「此刻可以说」被「可以说」抢先匹配
    labels = [
        ("此刻可以说", "say"), ("可以说", "say"),
        ("不要这样说", "not_say"), ("不要说", "not_say"), ("禁止说", "not_say"),
        ("为什么", "why"), ("原因", "why"),
        ("后续步骤", "next_steps"), ("事后可以做", "next_steps"), ("之后可以做", "next_steps"),
    ]
    # 标签出现位置：行首、句末标点后、逗号/冒号后、右引号后或空白前（兼容列表符号）
    label_re = re.compile(
        r'(?:^|(?<=[\n。；;！!？?，,、）)：:\s]))'
        r'(?:' + "|".join(p for p, _ in labels) + r')'
        r'\s*(?:[：:]\s*|(?=[“"「『])|\s+)'
    )
    key_of = dict(labels)
    spans = []
    seen = set()
    for m in label_re.finditer(text):
        key = key_of[m.group(0).strip().rstrip("：:").strip()]
        if key in seen:
            continue
        seen.add(key)
        spans.append((m.start(), m.end(), key))
    # 按起止位置切分
    for i, (start, sep_end, key) in enumerate(spans):
        field_start = sep_end
        field_end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        value = text[field_start:field_end].strip()
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


class VoiceStyleBind(BaseModel):
    description: str                       # 语音风格描述（英文 prompt，供 TTS 使用）
    voice_definition: Optional[str] = None  # 可选：VoxCPM2 声音定义字符串
    voice_params: Optional[dict] = None     # 可选：VoxCPM2 声音参数（pitch/speed 等）


@app.post("/api/ai/sessions/{session_id}/voice-style")
async def bind_voice_style(session_id: str, payload: VoiceStyleBind, request: Request):
    """为会话绑定语音风格（覆盖会话已保存的 voice_description）"""
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
    3. 会话中直接保存的 voice_description（旧版语音定义）
    """
    if explicit_description:
        return explicit_description
    preset_id = memory_storage.get_short_term_memory(session_id, "voice_preset_id")
    if preset_id:
        prompt = voice_catalog.preset_prompt(preset_id)
        if prompt:
            return prompt
    return memory_storage.get_short_term_memory(session_id, "voice_description")


def _resolve_session_voice(session_id: str):
    """
    解析 voice_id，返回 (voice_id, notice)：
    - edge 模式：无克隆音色，返回 None（edge 默认音色）
    - cloud/voxcpm2：预设音色 → cloud_voice 映射
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

    # 风格优先级：显式 description > 预设音色 > 旧版语音定义
    style = _resolve_session_style(session_id, description)
    speed = 1.0
    voice_id, voice_notice = _resolve_session_voice(session_id)

    # 统一 TTS 入口（云/本地 GPU/CPU 分派见 _synthesize_speech）
    try:
        wav_bytes, sample_rate = await _synthesize_speech(
            text, style, None, quick_mode, speed=speed, voice_id=voice_id
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
        # 已是容器格式（WAV/MP3/OGG/FLAC/M4A）直接上传；裸 PCM 才补 WAV 头
        detected = asr_service._detect_container(raw)
        if detected:
            text = await asr_service.transcribe_any(raw)
        else:
            # 裸 PCM：按 16kHz/16bit/单声道 封装后上传
            text = await asr_service.transcribe_pcm(raw, sample_rate=16000)
        if not text:
            return {"status": "ok", "text": "", "warning": "未识别到语音内容，请靠近麦克风重试"}
        return {"status": "ok", "text": text}
    except Exception as e:
        print(f"[ASR] 转写失败: {e}")
        return _internal_error("asr/transcribe", e)


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

    # 加载会话上下文（育儿教练默认 system prompt，不再有角色覆盖）
    style = memory_storage.get_short_term_memory(session_id, "voice_description")
    voice_id, voice_notice = _resolve_session_voice(session_id)

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

            # 每轮重新解析音色（用户可能在对话中途切换预设音色）
            voice_id, voice_notice = _resolve_session_voice(session_id)

            # 推送状态：开始生成（含方言回退提示，前端可展示）
            await safe_send_json({
                "type": "status", "status": "generating",
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
                        None,
                        quick_mode=not _USE_GPU,
                        speed=1.0,
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
            async for sentence in llm_service.chat_stream_sentences(messages_for_llm):
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
