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
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
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
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import jwt
from jwt.exceptions import PyJWTError as JWTError

from auth_store import dummy_verify, load_users, users_file_path, verify_password
from rate_limit_store import RateLimitStore
from usage_store import UsageStore, quota_error_payload

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
# 网关 → AI 服务的内部身份签名密钥（AI 侧必须配置同值；未单独配置时退化用 JWT_SECRET）
AI_INTERNAL_TOKEN = (os.environ.get("AI_INTERNAL_TOKEN") or JWT_SECRET).strip()
# 仅信任受控反代来源的 X-Forwarded-For；直连客户端伪造的 XFF 一律忽略
TRUSTED_PROXY_CIDRS = os.environ.get(
    "TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128"
).strip()


def _parse_trusted_proxies(raw: str):
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXY_CIDRS 含非法网段，已忽略: %s", part)
    return nets


_TRUSTED_PROXY_NETS = _parse_trusted_proxies(TRUSTED_PROXY_CIDRS)
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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
PUBLIC_PATHS = ("/", "/health", "/ready", LOGIN_PATH, "/metrics")


def _with_cors(response: JSONResponse, request) -> JSONResponse:
    """给中间件短路返回的响应补 CORS 头（CORSMiddleware 只包住路由层响应）"""
    origin = request.headers.get("origin", "")
    if origin in CORS_ALLOW_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


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


# ===== 限流实现（Redis 可选，进程内滑动窗口兜底）=====
_rate_limiter = RateLimitStore(os.environ.get("GATEWAY_REDIS_URL", "").strip() or None)

# ===== 用量计量 + 日配额（Redis 可选，进程内兜底）=====
_usage_store = UsageStore(os.environ.get("GATEWAY_REDIS_URL", "").strip() or None)


def _quota_check(request: Request, metric: str):
    """配额预检：超限返回 402 JSONResponse，否则 None"""
    user = _user_of(request)
    if user is None:
        return None
    ok, remaining = _usage_store.check(user, metric)
    if ok:
        return None
    METRICS_REQUESTS.labels(method=request.method, path=request.url.path, status="402").inc()
    return JSONResponse(quota_error_payload(metric, remaining), status_code=402)


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETS)


def _client_ip(request: Request) -> str:
    """仅当直连来源是受控反代时才采信 X-Forwarded-For，否则用 TCP 对端地址"""
    client = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    if xff and _is_trusted_proxy(client):
        return xff.split(",")[0].strip()
    return client


def _user_of(request: Request) -> str | None:
    """当前请求的账号标识；关闭鉴权的本地开发环境退化为单一 anonymous 身份"""
    uid = getattr(request.state, "user_id", None)
    if uid:
        return uid
    return "anonymous" if not AUTH_REQUIRED else None


def _internal_headers(user_id: str | None) -> dict:
    """构造 AI 服务可信身份头：HMAC-SHA256(账号|时间戳)，防止直连伪造"""
    if not user_id:
        return {}
    ts = str(int(time.time()))
    sig = hmac.new(
        AI_INTERNAL_TOKEN.encode(), f"{user_id}|{ts}".encode(), hashlib.sha256
    ).hexdigest()
    return {"X-Yuban-User": user_id, "X-Yuban-Ts": ts, "X-Yuban-Sig": sig}


def _relay_json(resp):
    """透传上游状态码与 JSON 体，避免把 4xx/5xx 伪装成 200"""
    try:
        body = resp.json()
    except ValueError:
        body = {"error": "上游返回非 JSON", "upstream_status": resp.status_code}
    return JSONResponse(content=body, status_code=resp.status_code)


def _relay_or_empty(resp):
    """DELETE 等无响应体场景：204 直接透传，其它状态码仍走 JSON 透传"""
    if resp.status_code == 204 or not getattr(resp, "content", b""):
        return Response(status_code=resp.status_code)
    return _relay_json(resp)


def _rate_limited(request: Request) -> bool:
    """True 表示超限。按 IP + 路由档次分桶，Redis 配置后跨实例共享"""
    path = request.url.path
    ip = _client_ip(request)
    if path == LOGIN_PATH:
        # 登录接口单独降额：防口令爆破与账号枚举
        return _rate_limiter.hit((ip, "login"), LOGIN_RATE_LIMIT_PER_MINUTE)

    is_expensive = path.startswith(EXPENSIVE_PATHS)
    limit = EXPENSIVE_RATE_LIMIT_PER_MINUTE if is_expensive else RATE_LIMIT_PER_MINUTE
    return _rate_limiter.hit((ip, "expensive" if is_expensive else "normal"), limit)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _upstream_error(endpoint: str, exc: Exception) -> JSONResponse:
    """对外隐藏异常细节，仅记录内部日志"""
    logger.exception("[%s] 调用 AI 服务失败: %s", endpoint, exc)
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
        resp = JSONResponse(
            {"error": "请求过于频繁，请稍后重试"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
        return _with_cors(resp, request)

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
                resp = JSONResponse({"error": "无效或已过期的访问凭证"}, status_code=401)
                return _with_cors(resp, request)
        elif AUTH_REQUIRED:
            METRICS_AUTH_FAILURES.labels(reason="missing_token").inc()
            METRICS_REQUESTS.labels(method=request.method, path=path, status="401").inc()
            resp = JSONResponse({"error": "缺少访问凭证"}, status_code=401)
            return _with_cors(resp, request)

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
async def process_voice(request: Request, session_id: str = Form(...), audio: UploadFile = File(...)):
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
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("voice/process", e)


# TTS语音合成接口（透传到核心AI服务）
@app.post("/api/voice/tts")
async def tts(request: Request, session_id: str = Form(...), text: str = Form(""), description: str = Form(None), quick_mode: str = Form("false")):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if not text.strip():
        return JSONResponse({"error": "text 不能为空"}, status_code=400)
    if len(text) > 1000:
        return JSONResponse({"error": "text 过长（上限 1000 字）"}, status_code=400)
    quota_err = _quota_check(request, "tts_chars")
    if quota_err:
        return quota_err
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
            headers=_internal_headers(_user_of(request)),
            timeout=AI_SERVICE_TTS_TIMEOUT,
        )
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if content_type.startswith("audio/") and response.status_code == 200:
            _usage_store.record(_user_of(request) or "anonymous", "tts_chars", len(text))
            headers = {"X-Sample-Rate": response.headers.get("X-Sample-Rate", "16000")}
            notice = response.headers.get("X-Voice-Notice")
            if notice:
                headers["X-Voice-Notice"] = notice
            return Response(content=response.content, media_type=content_type, headers=headers)
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("voice/tts", e)


# 语音转写（语音输入：wav → 文本），转发 multipart 到核心AI服务
@app.post("/api/voice/transcribe")
async def transcribe_voice(request: Request, session_id: str = Form("default"), audio: UploadFile = File(...)):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    audio_bytes = await audio.read()
    invalid = _validate_audio(audio_bytes)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)
    quota_err = _quota_check(request, "asr_seconds")
    if quota_err:
        return quota_err
    try:
        response = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/transcribe",
            data={"session_id": session_id},
            files={"audio": ("recording.wav", audio_bytes, "audio/wav")},
            headers=_internal_headers(_user_of(request)),
        )
        if response.status_code == 200:
            # 16kHz/16bit/单声道 PCM ≈ 32KB/s，按体积保守估算时长
            est_seconds = max(1, len(audio_bytes) // 32000)
            _usage_store.record(_user_of(request) or "anonymous", "asr_seconds", est_seconds)
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("voice/transcribe", e)


# 获取AI响应
@app.get("/api/ai/response/{session_id}")
async def get_response(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        response = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/response/{session_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("ai/response", e)


# 语音定义接口 —— 声音克隆已移除（育儿教练场景合规要求）
@app.post("/api/voice/define")
async def define_voice_removed():
    return JSONResponse(
        {"error": "声音克隆已移除（育儿教练场景合规要求）；日常合成不受影响",
         "code": "voice_define_removed"},
        status_code=501,
    )


@app.get("/api/voice-design/capability")
async def voice_design_capability():
    """云声音定义能力（GAP-07）透传：支持范围/配额/限流/最近错误"""
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/voice-design/capability")
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("voice-design/capability", e)


# 情感配置接口
@app.get("/api/emotion-presets")
async def emotion_presets():
    """情感预设（GAP-09）透传"""
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/emotion-presets")
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("emotion-presets", e)


@app.post("/api/emotion-config")
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
    surprise: float = Form(0.3),
):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if len(emotion_name) > 64:
        return JSONResponse({"error": "emotion_name 过长（上限 64 字）"}, status_code=400)
    if len(emotion_description) > 500:
        return JSONResponse({"error": "emotion_description 过长（上限 500 字）"}, status_code=400)
    # 情感值 clamp 到 [0, 1]，与文档注释一致
    _clamp = lambda x: max(0.0, min(1.0, float(x)))
    happiness, sadness, anger = _clamp(happiness), _clamp(sadness), _clamp(anger)
    fear, disgust, surprise = _clamp(fear), _clamp(disgust), _clamp(surprise)
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
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("emotion-config", e)


# 获取情感配置接口
@app.get("/api/emotion-config/{session_id}")
async def get_emotion_config(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        response = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/emotion-config/{session_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(response)
    except Exception as e:
        return _upstream_error("emotion-config/get", e)


@app.delete("/api/emotion-config/{session_id}")
async def delete_emotion_config(session_id: str, request: Request):
    """解绑情感配置：清空该会话的情感配置（幂等）"""
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        response = await _async_request(
            "delete",
            f"{AI_SERVICE_URL}/api/ai/emotion-config/{session_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_or_empty(response)
    except Exception as e:
        return _upstream_error("emotion-config/delete", e)


# ===== 角色管理接口（透传到核心AI服务）=====
@app.get("/api/characters/options")
async def character_options():
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/characters/options")
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/options", e)


@app.get("/api/voice-catalog")
async def voice_catalog():
    """音色目录（GAP-05 底座）透传"""
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/voice-catalog")
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("voice/catalog", e)


@app.get("/api/voice-presets")
async def voice_presets():
    """12 条预设音色目录透传"""
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/voice-presets")
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("voice/presets", e)


@app.get("/api/voice-presets/{preset_id}/prompt")
async def voice_preset_prompt(preset_id: str):
    """预设音色 voice_prompt 透传"""
    if not _safe_id(preset_id):
        return JSONResponse({"error": "preset_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "get", f"{AI_SERVICE_URL}/api/ai/voice-presets/{preset_id}/prompt"
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("voice/preset-prompt", e)


@app.get("/api/characters")
async def list_characters(request: Request):
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/characters",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/list", e)


@app.get("/api/characters/{character_id}")
async def get_character(character_id: str, request: Request):
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/characters/{character_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/get", e)


@app.put("/api/characters/{character_id}")
@app.patch("/api/characters/{character_id}")
async def update_character(character_id: str, request: Request):
    """修改自定义角色：仅归属者；请求体按 JSON 透传，状态码原样返回"""
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是合法 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        resp = await _async_request(
            request.method.lower(),
            f"{AI_SERVICE_URL}/api/ai/characters/{character_id}",
            json=payload,
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/update", e)


@app.post("/api/characters")
async def create_character(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是合法 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/characters",
            json=payload,
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/create", e)


@app.delete("/api/characters/{character_id}")
async def delete_character(character_id: str, request: Request):
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "delete",
            f"{AI_SERVICE_URL}/api/ai/characters/{character_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("characters/delete", e)


# ===== 孩子档案 / 育儿急救透传 =====
@app.get("/api/children")
async def list_children(request: Request):
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/api/ai/children",
                                    headers=_internal_headers(_user_of(request)))
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("children/list", e)


@app.post("/api/children")
async def create_child(request: Request):
    try:
        payload = await request.json()
        resp = await _async_request("post", f"{AI_SERVICE_URL}/api/ai/children",
                                    json=payload, headers=_internal_headers(_user_of(request)))
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("children/create", e)


@app.put("/api/children/{child_id}")
@app.patch("/api/children/{child_id}")
async def update_child(child_id: str, request: Request):
    if not _safe_id(child_id):
        return JSONResponse({"error": "child_id 非法"}, status_code=400)
    try:
        payload = await request.json()
        resp = await _async_request("put", f"{AI_SERVICE_URL}/api/ai/children/{child_id}",
                                    json=payload, headers=_internal_headers(_user_of(request)))
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("children/update", e)


@app.delete("/api/children/{child_id}")
async def delete_child(child_id: str, request: Request):
    if not _safe_id(child_id):
        return JSONResponse({"error": "child_id 非法"}, status_code=400)
    try:
        resp = await _async_request("delete", f"{AI_SERVICE_URL}/api/ai/children/{child_id}",
                                    headers=_internal_headers(_user_of(request)))
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("children/delete", e)


@app.post("/api/parenting-advice")
async def parenting_advice(request: Request):
    """育儿急救话术卡（贵接口，纳入 LLM 字符配额）"""
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not (payload.get("scene") or "").strip():
            return JSONResponse({"error": "scene 不能为空"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "请求体必须是合法 JSON"}, status_code=400)

    quota_err = _quota_check(request, "llm_chars")
    if quota_err:
        return quota_err

    try:
        resp = await _async_request("post", f"{AI_SERVICE_URL}/api/ai/parenting-advice",
                                    json=payload, headers=_internal_headers(_user_of(request)))
        if resp.status_code == 200:
            _usage_store.record(_user_of(request) or "anonymous", "llm_calls", 1)
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("parenting-advice", e)


@app.post("/api/sessions")
async def create_session(request: Request):
    """创建会话：session_id 由服务端随机生成，前端不再自行拼随机数"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/sessions",
            json=body if isinstance(body, dict) else {},
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/create", e)


@app.get("/api/sessions")
async def list_sessions(request: Request, limit: int = 20, offset: int = 0):
    """会话列表：仅返回当前账号的会话，分页透传给 AI 服务"""
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/sessions",
            params={"limit": limit, "offset": offset},
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/list", e)


@app.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        resp = await _async_request(
            "patch",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}",
            json=body,
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/rename", e)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "delete",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_or_empty(resp)
    except Exception as e:
        return _upstream_error("sessions/delete", e)


@app.post("/api/sessions/{session_id}/character")
async def bind_character(session_id: str, request: Request, character_id: str = Form(...)):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    if not _safe_id(character_id):
        return JSONResponse({"error": "character_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/character",
            data={"character_id": character_id},
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/bind-character", e)


@app.get("/api/sessions/{session_id}/character")
async def get_session_character(session_id: str, request: Request):
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/character",
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
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
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/bind-voice-style", e)


@app.post("/api/sessions/{session_id}/voice-preset")
async def bind_voice_preset(session_id: str, request: Request):
    """为会话选择预设音色（未绑定真实 voice_id 时返回明确提示）"""
    if not _safe_id(session_id):
        return JSONResponse({"error": "session_id 非法"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    if not isinstance(body, dict) or not str(body.get("preset_id") or "").strip():
        return JSONResponse({"error": "preset_id 不能为空"}, status_code=400)
    try:
        resp = await _async_request(
            "post",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/voice-preset",
            json={"preset_id": str(body["preset_id"]).strip()},
            headers=_internal_headers(_user_of(request)),
        )
        return _relay_json(resp)
    except Exception as e:
        return _upstream_error("sessions/bind-voice-preset", e)


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
    username = _user_of(request)
    if not username:
        return JSONResponse({"error": "缺少访问凭证"}, status_code=401)
    return {"username": username, "auth_required": AUTH_REQUIRED}


# 健康检查：网关自身存活 + 上游 AI 服务可用性（上游不可达时返回 503，避免监控误判）
@app.get("/health")
async def health_check():
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/health", timeout=3)
        ai_status = resp.json()
    except Exception:
        ai_status = {"status": "unreachable"}
    ai_healthy = isinstance(ai_status, dict) and ai_status.get("status") in ("healthy", "ready")
    payload = {
        "status": "healthy" if ai_healthy else "degraded",
        # 前端据此决定是否弹出登录门禁（AUTH_REQUIRED=false 时不锁死界面）
        "auth_required": AUTH_REQUIRED,
        "ai_service": ai_status,
        "rate_limiter": _rate_limiter.backend,
    }
    if not ai_healthy:
        return JSONResponse(payload, status_code=503)
    return payload


# 就绪探针：账号表已播种 + 上游 AI 服务 /ready 通过，供容器/负载均衡使用
@app.get("/ready")
async def readiness_check():
    problems: list[str] = []
    if AUTH_REQUIRED and not _USER_STORE:
        problems.append("account_store_empty")
    ai_ready = False
    ai_status: dict = {}
    try:
        resp = await _async_request("get", f"{AI_SERVICE_URL}/ready", timeout=3)
        ai_status = resp.json()
        ai_ready = resp.status_code == 200 and isinstance(ai_status, dict) and ai_status.get("status") == "ready"
    except Exception as e:
        ai_status = {"status": "unreachable", "error": type(e).__name__}
    if not ai_ready:
        problems.append("ai_service_not_ready")
    payload = {
        "status": "ready" if not problems else "not_ready",
        "problems": problems,
        "ai_service": ai_status,
    }
    if problems:
        return JSONResponse(payload, status_code=503)
    return payload


# Prometheus 指标端点（生产应在 nginx 层限制仅内网可达）
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# 当日用量与配额（前端"设置"页展示；无需管理员权限，仅看自己的）
@app.get("/api/usage/me")
async def usage_me(request: Request):
    user = _user_of(request)
    if not user:
        return JSONResponse({"error": "缺少访问凭证"}, status_code=401)
    snap = _usage_store.snapshot(user)
    snap["backend"] = _usage_store.backend
    return snap


# WebSocket 实时对话透传（转发到 ai-service/ws/chat/{session_id}）
@app.websocket("/ws/chat/{session_id}")
async def ws_chat_proxy(ws: WebSocket, session_id: str):
    """WebSocket 代理：手机 ↔ 网关 ↔ AI 服务"""
    import asyncio
    import websockets

    # 输入校验：session_id 白名单
    # 注意：Starlette 在 accept 前 close 会退化为 HTTP 403，浏览器侧只能看到 1006；
    # 先 accept 再带业务关闭码 1008 结束，客户端才能收到明确的鉴权失败语义。
    if not _safe_id(session_id):
        await ws.accept()
        await ws.close(code=1008, reason="session_id invalid")
        return

    # WebSocket 无法自定义 Header，鉴权场景下通过 ?token= 传递
    user_id = "anonymous" if not AUTH_REQUIRED else None
    if AUTH_REQUIRED:
        token = ws.query_params.get("token")
        try:
            payload = jwt.decode(token or "", JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user_id = payload.get("sub")
        except JWTError:
            await ws.accept()
            await ws.close(code=1008, reason="unauthorized")
            return

    target_url = f"{AI_SERVICE_WS_URL}/ws/chat/{session_id}"
    await ws.accept()

    # 建连前校验会话归属：跨账号连接直接 1008 关闭，避免把越权连接转给上游
    try:
        info_resp = await _async_request(
            "get",
            f"{AI_SERVICE_URL}/api/ai/sessions/{session_id}/owner",
            headers=_internal_headers(user_id),
            timeout=3,
        )
        owner_info = info_resp.json()
    except Exception as e:
        logger.warning("[ws/chat] 归属校验失败 session=%s err=%s", session_id, e)
        owner_info = {}
    if owner_info.get("deleted"):
        await ws.close(code=1008, reason="session_deleted")
        return
    if owner_info.get("exists") and owner_info.get("owner") and owner_info.get("owner") != user_id:
        await ws.close(code=1008, reason="forbidden")
        return

    # 对话额度预检：llm_chars 当日已用完则拒绝建连（402 语义经 WS close code 传达）
    quota_user = user_id or "anonymous"
    llm_ok, llm_remaining = _usage_store.check(quota_user, "llm_chars")
    if not llm_ok:
        try:
            await ws.send_json({
                "type": "error",
                "code": "user_quota_exceeded",
                "message": "今日对话额度已用完，明天再来或联系管理员调整配额",
            })
        finally:
            await ws.close(code=1008, reason="quota_exceeded")
        return

    try:
        upstream_headers = _internal_headers(user_id)
        try:
            upstream_cm = websockets.connect(target_url, additional_headers=upstream_headers)
        except TypeError:
            upstream_cm = websockets.connect(target_url, extra_headers=upstream_headers)
        async with upstream_cm as upstream:

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
                # 计量：AI 文本帧按字符累计（LLM 用量），音频帧不重复计（TTS 字符已在文本侧覆盖）
                counted = 0
                try:
                    async for msg in upstream:
                        if isinstance(msg, str):
                            try:
                                frame = json.loads(msg)
                                if frame.get("type") == "text":
                                    counted += len(frame.get("content") or "")
                            except ValueError:
                                pass
                            # 流中实时熔断：超过建连时剩余额度即中断，防止长回复穿透配额
                            if counted > llm_remaining >= 0:
                                _usage_store.record(quota_user, "llm_chars", counted)
                                counted = 0
                                try:
                                    await ws.send_json({
                                        "type": "error",
                                        "code": "user_quota_exceeded",
                                        "message": "今日对话额度已用完",
                                    })
                                finally:
                                    await ws.close(code=1008, reason="quota_exceeded")
                                return
                            await ws.send_text(msg)
                        else:
                            await ws.send_bytes(msg)
                except Exception:
                    pass
                finally:
                    if counted > 0:
                        _usage_store.record(quota_user, "llm_chars", counted)

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
