"""
语伴 API 网关（接入层）

安全策略（对应生产加固清单）：
- 认证：Bearer JWT 严格校验，非法/过期凭证一律 401（不再静默放行）
- 登录：POST /api/auth/login 校验账号口令并签发带 exp 的 JWT（账号由 auth_seed.py 离线播种）
- 授权：AUTH_REQUIRED=true 时强制所有 /api、/ws 携带有效凭证
- CORS：白名单制，禁止 allow_credentials 与 "*" 混用
- 限流：按客户端 IP + 路由分桶的滑动窗口限流，昂贵接口单独降额
- 上传：Content-Length 预检 + 魔数校验，拒绝超限与伪造格式
- 输入：session_id / character_id 白名单校验，阻断上游 URL 注入
- 错误：对外仅返回通用错误，异常详情只进服务端日志
- 响应头：补充 nosniff / frame / referrer 等安全头
"""
import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

# ===== 加载项目根目录 .env（不覆盖已存在的环境变量）=====
def _load_root_dotenv():
    """网关从项目根目录 .env 读取配置；compose/生产环境由容器注入，不冲突。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
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

_load_root_dotenv()

import requests
import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jose import jwt, JWTError

from auth_store import dummy_verify, load_users, users_file_path, verify_password

# ===== Prometheus 指标 =====
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

METRICS_REQUESTS = Counter(
    "gateway_requests_total", "HTTP 请求总数", ["method", "path", "status"]
)
METRICS_LATENCY = Histogram(
    "gateway_request_duration_seconds", "HTTP 请求耗时", ["path"],
    buckets=[0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)
METRICS_AUTH_FAILURES = Counter(
    "gateway_auth_failures_total", "鉴权失败总数", ["reason"]  # 401/429
)

# ===== 结构化日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("yuban.gateway")

# ===== 优雅退出说明 =====
# Uvicorn 内置处理 SIGINT/SIGTERM，server.serve() 会在收到信号后优雅返回。
# 无需自定义信号处理器，避免与 Uvicorn 冲突及事件循环未就绪时的竞态。

_ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "false").lower() == "true"
# 生产关闭 /docs 与 /openapi.json，避免暴露接口面
app = FastAPI(
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)

# ===== 服务地址配置（容器化后由环境变量注入，默认值兼容本地开发）=====
AI_SERVICE_URL = os.environ.get("AI_SERVICE_URL", "http://localhost:8084")
AI_SERVICE_WS_URL = os.environ.get("AI_SERVICE_WS_URL", "ws://localhost:8084")
AI_SERVICE_TIMEOUT = float(os.environ.get("AI_SERVICE_TIMEOUT", "60"))
AI_SERVICE_TTS_TIMEOUT = float(os.environ.get("AI_SERVICE_TTS_TIMEOUT", "600"))

# ===== 鉴权配置 =====
JWT_SECRET = os.environ.get("JWT_SECRET", "secret")
JWT_ALGORITHM = "HS256"
# 强制鉴权开关：生产务必置 true；本地无登录入口时可置 false 以兼容前端
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"
# 签发令牌有效期（分钟）与登录接口独立限流（防口令爆破）
AUTH_TOKEN_TTL_MINUTES = int(os.environ.get("AUTH_TOKEN_TTL_MINUTES", "720"))
LOGIN_RATE_LIMIT_PER_MINUTE = int(os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE", "5"))

# 账号表在启动时加载（由 auth_seed.py 离线播种，网关不提供注册接口）
_USER_STORE = load_users()
if not _USER_STORE:
    logger.warning(
        "账号表为空（%s），登录接口将无法通过校验；请用 auth_seed.py 播种账号",
        users_file_path(),
    )

if JWT_SECRET == "secret":
    logger.warning("JWT_SECRET 仍为默认值，存在凭证伪造风险，请通过环境变量注入强随机密钥")
if AUTH_REQUIRED and JWT_SECRET == "secret":
    raise RuntimeError("AUTH_REQUIRED=true 但 JWT_SECRET 未配置，拒绝以不安全配置启动")
if AUTH_REQUIRED and not _USER_STORE:
    raise RuntimeError(
        f"AUTH_REQUIRED=true 但账号表为空（{users_file_path()}），"
        "拒绝启动以免所有请求被 401；请先执行 auth_seed.py add <账号>"
    )

# ===== CORS 白名单（逗号分隔）=====
_CORS_ENV = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
CORS_ALLOW_ORIGINS = [
    o.strip()
    for o in (
        _CORS_ENV
        or "http://localhost:8080,http://127.0.0.1:8080,http://localhost:8899"
    ).split(",")
    if o.strip()
]
if not CORS_ALLOW_ORIGINS or "*" in CORS_ALLOW_ORIGINS:
    logger.warning("CORS 白名单为空或含通配符，生产请显式配置 CORS_ALLOW_ORIGINS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

# ===== 限流配置 =====
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))
EXPENSIVE_RATE_LIMIT_PER_MINUTE = int(os.environ.get("EXPENSIVE_RATE_LIMIT_PER_MINUTE", "20"))

# 昂贵接口（TTS/ASR 走 GPU 或云 API，单独降额）
EXPENSIVE_PATHS = (
    "/api/voice/tts",
    "/api/voice/transcribe",
    "/api/voice/define",
    "/api/voice/process",
)
# 上传接口（做 Content-Length 预检）
UPLOAD_PATHS = ("/api/voice/process", "/api/voice/transcribe", "/api/voice/define")

# ===== 上传与输入校验 =====
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
# 只允许字母/数字开头，中间可含 _ - . 三种分隔符（但不允许纯分隔符）
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")

# 常见音频容器魔数（含 m4a/mp4 的 ftyp 分支单独判断）
_AUDIO_MAGIC = (
    b"RIFF",      # wav
    b"OggS",      # ogg
    b"ID3",       # mp3(带 ID3 标签)
    b"fLaC",      # flac
    b"\xff\xfb",  # mp3 frame
    b"\xff\xf3",
    b"\xff\xf2",
    b"\xff\xfa",
    b"\xff\xe3",
)

LOGIN_PATH = "/api/auth/login"
# 匿名可达路径（登录接口必须公开，否则无法换取令牌）
# /metrics 供 Prometheus 抓取；生产环境应在 nginx 层限制仅内网可达
PUBLIC_PATHS = ("/", "/health", LOGIN_PATH, "/metrics")


def _safe_id(value: str) -> bool:
    """校验 session_id / character_id，阻断拼接进上游 URL 的注入"""
    return bool(value) and bool(_SAFE_ID_RE.match(value))


def _validate_audio(audio_bytes: bytes) -> str | None:
    """返回错误消息；校验通过返回 None"""
    if not audio_bytes:
        return "音频内容为空"
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        return f"音频文件超过上限（{MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"
    if any(audio_bytes.startswith(sig) for sig in _AUDIO_MAGIC):
        return None
    # m4a / mp4 容器：第 4-8 字节为 'ftyp'
    if len(audio_bytes) >= 12 and audio_bytes[4:8] == b"ftyp":
        return None
    return "不支持的音频格式（仅支持 wav / mp3 / ogg / flac / m4a）"


# ===== 限流实现（进程内滑动窗口）=====
_rate_buckets: dict[tuple, deque] = defaultdict(deque)
_req_counter = 0


def _client_ip(request: Request) -> str:
    """nginx 反代场景取 X-Forwarded-For 首个地址，否则回退到直连地址"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sweep_rate_buckets(now: float) -> None:
    for key, q in list(_rate_buckets.items()):
        while q and now - q[0] > 60:
            q.popleft()
        if not q:
            del _rate_buckets[key]


def _hit_limit(key: tuple, limit: int) -> bool:
    """通用滑动窗口计数：True 表示该 key 在 60 秒内已超过 limit 次"""
    now = time.time()
    q = _rate_buckets[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def _rate_limited(request: Request) -> bool:
    """True 表示超限。按 IP + 路由档次分桶，避免内存无限增长"""
    global _req_counter
    _req_counter += 1
    if _req_counter % 1000 == 0:
        _sweep_rate_buckets(time.time())

    path = request.url.path
    ip = _client_ip(request)
    if path == LOGIN_PATH:
        # 登录接口单独降额：防口令爆破与账号枚举
        return _hit_limit((ip, "login"), LOGIN_RATE_LIMIT_PER_MINUTE)

    is_expensive = path.startswith(EXPENSIVE_PATHS)
    limit = EXPENSIVE_RATE_LIMIT_PER_MINUTE if is_expensive else RATE_LIMIT_PER_MINUTE
    return _hit_limit((ip, "expensive" if is_expensive else "normal"), limit)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _upstream_error(endpoint: str, exc: Exception) -> JSONResponse:
    """对外隐藏异常细节，仅记录内部日志"""
    logger.error("[%s] 调用 AI 服务失败: %s", endpoint, exc)
    return JSONResponse({"error": "服务暂时不可用，请稍后重试"}, status_code=502)


async def _async_request(method: str, url: str, **kwargs):
    """把同步 requests 包进线程池，避免在 async def 里阻塞事件循环。
    网关现有 15 处 requests.get/post 统一走这里——逐步迁移到 httpx.AsyncClient 前的过渡层。"""
    loop = asyncio.get_running_loop()
    kwargs.setdefault("timeout", AI_SERVICE_TIMEOUT)
    return await loop.run_in_executor(None, lambda: getattr(requests, method.lower())(url, **kwargs))


# ===== 安全中间件：限流 → 鉴权 → 上传预检 → 安全响应头 =====
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CORS 预检由 CORSMiddleware 处理，鉴权/限流不应拦截 OPTIONS
    if request.method == "OPTIONS":
        return await call_next(request)

    _start = time.time()
    if _rate_limited(request):
        METRICS_AUTH_FAILURES.labels(reason="rate_limited").inc()
        METRICS_REQUESTS.labels(method=request.method, path=request.url.path, status="429").inc()
        return JSONResponse(
            {"error": "请求过于频繁，请稍后重试"},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    path = request.url.path
    if path not in PUBLIC_PATHS:
        token = _extract_token(request)
        if token:
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                if "exp" not in payload:
                    raise JWTError("token 缺少 exp 声明")
                request.state.user_id = payload.get("sub")
            except JWTError:
                METRICS_AUTH_FAILURES.labels(reason="invalid_token").inc()
                METRICS_REQUESTS.labels(method=request.method, path=path, status="401").inc()
                return JSONResponse({"error": "无效或已过期的访问凭证"}, status_code=401)
        elif AUTH_REQUIRED:
            METRICS_AUTH_FAILURES.labels(reason="missing_token").inc()
            METRICS_REQUESTS.labels(method=request.method, path=path, status="401").inc()
            return JSONResponse({"error": "缺少访问凭证"}, status_code=401)

    if path.startswith(UPLOAD_PATHS):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES + 4096:
            METRICS_REQUESTS.labels(method=request.method, path=path, status="413").inc()
            return JSONResponse(
                {"error": f"请求体超过上限（{MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"},
                status_code=413,
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"

    # 指标记录
    METRICS_REQUESTS.labels(method=request.method, path=path, status=str(response.status_code)).inc()
    METRICS_LATENCY.labels(path=path).observe(time.time() - _start)
    return response


# WebRTC连接处理
@app.post("/api/webrtc/connect")
async def connect(session_id: str):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    # WebRTC连接处理
    return {"status": "ok", "session_id": session_id}


# 语音处理接口
@app.post("/api/voice/process")
async def process_voice(session_id: str = Form(...), audio: UploadFile = File(...)):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    audio_data = await audio.read()
    invalid = _validate_audio(audio_data)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)

    # 转发到核心AI服务
    try:
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/process",
            json={"session_id": session_id, "audio_data": audio_data.hex()},
        )
        return response.json()
    except Exception as e:
        return _upstream_error("voice/process", e)


# TTS语音合成接口（透传到核心AI服务）
@app.post("/api/voice/tts")
async def tts(session_id: str = Form(...), text: str = Form(...), description: str = Form(None), quick_mode: str = Form("false")):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if not text.strip():
        return JSONResponse({"error": "text 不能为空"}, status_code=400)
    if len(text) > 1000:
        return JSONResponse({"error": "text 过长（上限 1000 字）"}, status_code=400)
    try:
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/tts",
            data={
                "session_id": session_id,
                "text": text,
                "description": description or "",
                "quick_mode": "true" if quick_mode in ("true", "True", "1", True) else "false",
            },
            timeout=AI_SERVICE_TTS_TIMEOUT,
        )
        if response.headers.get("content-type") == "audio/wav":
            return Response(
                content=response.content,
                media_type="audio/wav",
                headers={"X-Sample-Rate": response.headers.get("X-Sample-Rate", "16000")},
            )
        return response.json()
    except Exception as e:
        return _upstream_error("voice/tts", e)


# 语音转写（语音输入：wav → 文本），转发 multipart 到核心AI服务
@app.post("/api/voice/transcribe")
async def transcribe_voice(session_id: str = Form("default"), audio: UploadFile = File(...)):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    audio_bytes = await audio.read()
    invalid = _validate_audio(audio_bytes)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)
    try:
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/transcribe",
            data={"session_id": session_id},
            files={"audio": ("recording.wav", audio_bytes, "audio/wav")},
        )
        return response.json()
    except Exception as e:
        return _upstream_error("voice/transcribe", e)


# 获取AI响应
@app.get("/api/ai/response/{session_id}")
async def get_response(session_id: str):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        response = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/response/{session_id}",
        )
        return response.json()
    except Exception as e:
        return _upstream_error("ai/response", e)


# 语音定义接口
@app.post("/api/voice/define")
async def define_voice(
    session_id: str = Form(...),
    description: str = Form(...),
    audio: UploadFile = File(...),
    pitch: float = Form(1.0),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    emotion_intensity: float = Form(0.5),
):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if not description.strip():
        return JSONResponse({"error": "description 不能为空"}, status_code=400)
    if len(description) > 200:
        return JSONResponse({"error": "description 过长（上限 200 字）"}, status_code=400)
    try:
        audio_data = await audio.read()
        invalid = _validate_audio(audio_data)
        if invalid:
            return JSONResponse({"error": invalid}, status_code=400)

        # 转发到核心AI服务（包进线程池，避免同步 requests 阻塞 asyncio 事件循环）
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/define-voice",
            data={
                "session_id": session_id,
                "description": description,
                "pitch": pitch,
                "speed": speed,
                "volume": volume,
                "emotion_intensity": emotion_intensity,
            },
            files={"audio": (audio.filename, audio_data, audio.content_type or "application/octet-stream")},
        )
        return response.json()
    except Exception as e:
        return _upstream_error("voice/define", e)


# 情感配置接口
@app.post("/api/emotion-config")
async def emotion_config(
    session_id: str = Form(...),
    emotion_name: str = Form(...),
    emotion_description: str = Form(""),
    happiness: float = Form(0.5),
    sadness: float = Form(0.1),
    anger: float = Form(0.1),
    fear: float = Form(0.1),
    disgust: float = Form(0.1),
    surprise: float = Form(0.3),
):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        # 转发到核心AI服务
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/emotion-config",
            data={
                "session_id": session_id,
                "emotion_name": emotion_name,
                "emotion_description": emotion_description,
                "happiness": happiness,
                "sadness": sadness,
                "anger": anger,
                "fear": fear,
                "disgust": disgust,
                "surprise": surprise,
            },
        )
        return response.json()
    except Exception as e:
        return _upstream_error("emotion-config", e)


# 获取情感配置接口
@app.get("/api/emotion-config/{session_id}")
async def get_emotion_config(session_id: str):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        response = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/emotion-config/{session_id}",
        )
        return response.json()
    except Exception as e:
        return _upstream_error("emotion-config/get", e)


# ===== 角色管理接口（透传到核心AI服务）=====
@app.get("/api/characters/options")
async def character_options():
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/characters/options")
        return resp.json()
    except Exception as e:
        return _upstream_error("characters/options", e)


@app.get("/api/characters")
async def list_characters():
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/characters")
        return resp.json()
    except Exception as e:
        return _upstream_error("characters/list", e)


@app.get("/api/characters/{character_id}")
async def get_character(character_id: str):
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/characters/{character_id}",
        )
        return resp.json()
    except Exception as e:
        return _upstream_error("characters/get", e)


@app.post("/api/characters")
async def create_character(request: Request):
    try:
        payload = await request.json()
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/characters",
            json=payload,
        )
        return resp.json()
    except Exception as e:
        return _upstream_error("characters/create", e)


@app.post("/api/sessions/{session_id}/character")
async def bind_character(session_id: str, character_id: str = Form(...)):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/character",
            data={"character_id": character_id},
        )
        return resp.json()
    except Exception as e:
        return _upstream_error("sessions/bind-character", e)


@app.get("/api/sessions/{session_id}/character")
async def get_session_character(session_id: str):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/character",
        )
        return resp.json()
    except Exception as e:
        return _upstream_error("sessions/get-character", e)


@app.post("/api/ai/sessions/{session_id}/voice-style")
async def bind_voice_style(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/voice-style",
            json=body,
        )
        return resp.json()
    except Exception as e:
        return _upstream_error("sessions/bind-voice-style", e)


# ===== 鉴权接口（登录换取令牌）=====
@app.post(LOGIN_PATH)
async def auth_login(request: Request):
    """校验账号口令并签发带 exp 的 JWT；失败一律同一文案，避免账号枚举"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体格式不正确"}, status_code=400)

    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not password:
        return JSONResponse({"error": "账号和密码不能为空"}, status_code=400)
    if len(username) > 64 or len(password) > 256:
        return JSONResponse({"error": "账号或密码格式不正确"}, status_code=400)

    record = _USER_STORE.get(username)
    if record is None:
        dummy_verify(password)  # 对不存在的账号做等价耗时计算，抗枚举
        passed = False
    else:
        passed = verify_password(password, record)
    if not passed:
        logger.warning("[auth] 登录失败 user=%s ip=%s", username, _client_ip(request))
        return JSONResponse({"error": "账号或密码错误"}, status_code=401)

    now = int(time.time())
    ttl_seconds = AUTH_TOKEN_TTL_MINUTES * 60
    token = jwt.encode(
        {"sub": username, "iat": now, "exp": now + ttl_seconds, "typ": "access"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    logger.info("[auth] 登录成功 user=%s ip=%s", username, _client_ip(request))
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": ttl_seconds,
        "username": username,
    }


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """返回当前令牌对应的账号，供前端校验会话是否仍然有效"""
    username = getattr(request.state, "user_id", None)
    if not username:
        return JSONResponse({"error": "缺少访问凭证"}, status_code=401)
    return {"username": username, "auth_required": AUTH_REQUIRED}


# 健康检查
@app.get("/health")
async def health_check():
    try:
        resp = await _async_request(
            "get", f"{AI_SERVICE_URL}/health", timeout=3
        )
        ai_status = resp.json()
    except Exception:
        ai_status = {"status": "unreachable"}
    return {
        "status": "healthy",
        # 前端据此决定是否弹出登录门禁（AUTH_REQUIRED=false 时不锁死界面）
        "auth_required": AUTH_REQUIRED,
        "ai_service": ai_status,
    }


# Prometheus 指标端点（生产应在 nginx 层限制仅内网可达）
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# WebSocket 实时对话透传（转发到 ai-service/ws/chat/{session_id}）
@app.websocket("/ws/chat/{session_id}")
async def ws_chat_proxy(ws: WebSocket, session_id: str):
    """WebSocket 代理：手机 ↔ 网关 ↔ AI 服务"""
    import asyncio
    import websockets

    # 输入校验：session_id 白名单
    if not _safe_id(session_id):
        await ws.close(code=1008, reason="session_id invalid")
        return

    # WebSocket 无法自定义 Header，鉴权场景下通过 ?token= 传递
    if AUTH_REQUIRED:
        token = ws.query_params.get("token")
        try:
            jwt.decode(token or "", JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except JWTError:
            await ws.close(code=1008, reason="unauthorized")
            return

    target_url = f"{AI_SERVICE_WS_URL}/ws/chat/{session_id}"
    await ws.accept()

    try:
        async with websockets.connect(target_url) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive()
                        if data.get("type") == "websocket.disconnect":
                            break
                        if "text" in data:
                            await upstream.send(data["text"])
                        elif "bytes" in data:
                            await upstream.send(data["bytes"])
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        if isinstance(msg, str):
                            await ws.send_text(msg)
                        else:
                            await ws.send_bytes(msg)
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as e:
        logger.error("[ws/chat] 代理失败 session=%s err=%s", session_id, e)
        try:
            await ws.send_json({"type": "error", "message": "实时对话服务暂时不可用"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# 根路径处理（精简返回，避免暴露接口清单）
@app.get("/")
async def root():
    return {"message": "Welcome to 语伴 API Gateway", "status": "running"}


if __name__ == "__main__":
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8005,
        log_config=None,  # 使用自定义 logging 配置
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
    logger.info("服务已完全关闭")
