# -*- coding: utf-8 -*-
"""矩阵测试公共设施：环境读取、服务启停、HTTP 客户端、断言与缺陷目录"""
import io
import json
import os
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
AI_DIR = ROOT / "ai-service"
GW_DIR = ROOT / "api-gateway"
RUNTIME = HERE / "runtime"
LOGS = RUNTIME / "logs"
DATA_DIR = RUNTIME / "data"
RUNTIME.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

AI_PORT = 8084
GW_PORT = 8005
AI_BASE = f"http://127.0.0.1:{AI_PORT}"
GW_BASE = f"http://127.0.0.1:{GW_PORT}"
GW_WS = f"ws://127.0.0.1:{GW_PORT}"

ACCOUNTS = ["matrix_a", "matrix_b", "matrix_c", "matrix_d", "matrix_e"]
PASSWORD = "Matrix@2026test"

# 缺陷目录：code -> {title, kind(产品缺陷|能力缺失|环境不可用), minimal_fix}
DEFECT_CATALOG = {
    "DEF-01": {"title": "会话无归属校验，跨账号可读写他人会话", "kind": "产品缺陷",
               "fix": "会话状态增加 owner(sub)；网关在 /api/sessions/{sid}/character、/api/emotion-config/{sid}、/api/ai/response/{sid} 与 WS 建连时比对 sub，不一致返回 403 / close 1008"},
    "DEF-02": {"title": "不存在/失败场景返回 HTTP 200 + {\"error\"}", "kind": "产品缺陷",
               "fix": "上游统一用 HTTPException(404/409/503)；网关透传上游状态码而不是恒返回 200"},
    "DEF-03": {"title": "非 JSON 请求体返回 502 而非 400", "kind": "产品缺陷",
               "fix": "在 create_character 中 try/except JSONDecodeError 并返回 400 语义响应"},
    "DEF-04": {"title": "自定义角色全局共享，无用户作用域", "kind": "产品缺陷",
               "fix": "custom_characters 增加 owner 字段，列表按 sub 过滤，预设保持共享；删除校验 owner"},
    "DEF-05": {"title": "限流无条件信任 X-Forwarded-For，可伪造 IP 绕过", "kind": "产品缺陷",
               "fix": "仅当直连来源在 TRUSTED_PROXIES 白名单内才采用 XFF；否则使用 request.client.host"},
    "DEF-06": {"title": "就绪探针不反映云依赖可用性（欠费仍 ready）", "kind": "可观测性缺陷",
               "fix": "/ready 增加云 TTS/LLM 轻量探测或读取最近失败标记，配额失败时返回 503 + problems"},
    "DEF-07": {"title": "情感名称/描述无长度校验", "kind": "产品缺陷（轻微）",
               "fix": "emotion_name ≤64 字、description ≤500 字，空名称返回 400"},
    "DEF-08": {"title": "角色名称为纯空白时落库为空串（未回退默认名）", "kind": "产品缺陷（轻微）",
               "fix": "create_character 中改为 (name or '').strip() or '未命名角色'"},
}

GAP_CATALOG = {
    "GAP-01": {"title": "角色无修改端点（第 1 批已修复：PUT/PATCH）", "fix": "PUT/PATCH /api/characters/{id}，owner 校验 403、重名 409、缺失 404"},
    "GAP-02": {"title": "情感配置无解绑端点（第 1 批已修复：DELETE）", "fix": "DELETE /api/emotion-config/{sid} 返回 204，GET 仍返回空配置"},
    "GAP-03": {"title": "会话无列表/删除/重命名 API（第 1 批已修复）", "fix": "GET/PATCH/DELETE /api/sessions，按 owner 过滤并分页"},
    "GAP-04": {"title": "age_group 无儿童/幼儿年龄段（产品决策不做，出列）", "fix": "不排期：合规与年龄段扩展取消"},
    "GAP-05": {"title": "音色谱无方言/童声/老年音/情感音色（仅批准 3 种方言）", "fix": "第 2 批目录重构；第 3 批仅粤语/四川话/东北话"},
    "GAP-06": {"title": "无声音克隆端点（P3 仅需求验证，不排实施）", "fix": "不排实施；如需再做需 GPU + 声纹合规评审"},
    "GAP-07": {"title": "cloud 模式声音定义不可用（第 1 批降级为显式 501）", "fix": "cloud 模式 501 + code=voice_define_unsupported；云能力属第 3 批"},
    "GAP-08": {"title": "speed 仅落库、不进入 voice_prompt/TTS 入参", "fix": "voice_profile_to_prompt 增加语速描述，或将 speed 传入 TTS"},
    "GAP-09": {"title": "情感类型无枚举/无预设", "fix": "提供 emotion 预设枚举（平静/开心/悲伤…）映射到 6 轴向量"},
    "GAP-10": {"title": "无用户人群画像能力（产品决策不采集，出列）", "fix": "不排期：数据模型与隐私告知不做"},
    "GAP-11": {"title": "会话角色解绑（未排期，不属于第 1 批）", "fix": "新增 DELETE /api/sessions/{sid}/character 或在绑定接口支持 character_id 为空"},
}


def read_env(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        data[k.strip()] = v.strip().strip("'\"")
    return data


ROOT_ENV = read_env(ROOT / ".env")
AI_ENV = read_env(AI_DIR / ".env")


def assertion(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:400]}


def snippet(text, n=300):
    return (text or "")[:n]


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def find_gateway_python() -> str:
    for p in [sys.executable, r"D:\Program Files (x86)\Python 3.11\python.exe"]:
        if not Path(p).exists():
            continue
        try:
            r = subprocess.run([p, "-c", "import jwt, fastapi, uvicorn, requests"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return sys.executable


def wait_http(url: str, timeout=90, expect=200):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            r = requests.get(url, timeout=3)
            last = r.status_code
            if r.status_code == expect:
                return True, r.status_code, round(time.time() - t0, 2)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1)
    return False, last, round(time.time() - t0, 2)


def wav_info(raw: bytes) -> dict:
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return {"channels": wf.getnchannels(), "sample_rate": rate, "frames": frames,
                    "duration_seconds": round(frames / float(rate), 3) if rate else 0,
                    "bytes": len(raw)}
    except Exception as e:
        return {"decode_error": f"{type(e).__name__}: {e}", "bytes": len(raw)}


def tiny_wav(seconds=0.2, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


def ai_env():
    env = os.environ.copy()
    env.update({
        "TTS_PROVIDER": "cloud",
        "YUBAN_DATA_DIR": str(DATA_DIR),
        "PYTHONUNBUFFERED": "1",
        "REDIS_URL": "",
        "MAX_UPLOAD_MB": "10",
        "AI_INTERNAL_TOKEN": ROOT_ENV.get("AI_INTERNAL_TOKEN") or
                             ROOT_ENV.get("JWT_SECRET") or "matrix-only-secret-2026",
    })
    env.setdefault("LLM_API_KEY", AI_ENV.get("LLM_API_KEY", ""))
    return env


def gw_env(users_file: Path):
    env = os.environ.copy()
    env.update({
        "AI_SERVICE_URL": AI_BASE,
        "AI_SERVICE_WS_URL": f"ws://127.0.0.1:{AI_PORT}",
        "JWT_SECRET": ROOT_ENV.get("JWT_SECRET") or "matrix-only-secret-2026",
        "AI_INTERNAL_TOKEN": ROOT_ENV.get("AI_INTERNAL_TOKEN") or
                             ROOT_ENV.get("JWT_SECRET") or "matrix-only-secret-2026",
        "AUTH_REQUIRED": "true",
        "AUTH_USERS_FILE": str(users_file),
        "AUTH_TOKEN_TTL_MINUTES": "720",
        # 保持产品默认限额，不为测试放宽
        "LOGIN_RATE_LIMIT_PER_MINUTE": "5",
        "RATE_LIMIT_PER_MINUTE": "120",
        "EXPENSIVE_RATE_LIMIT_PER_MINUTE": "20",
        "MAX_UPLOAD_MB": "10",
        "AI_SERVICE_TIMEOUT": "60",
        "AI_SERVICE_TTS_TIMEOUT": "600",
        "ENABLE_DOCS": "false",
        "CORS_ALLOW_ORIGINS": ROOT_ENV.get("CORS_ALLOW_ORIGINS") or
                              "http://localhost:8080,http://127.0.0.1:8080,http://localhost:8899",
    })
    return env


class Services:
    def __init__(self):
        self.ai = None
        self.gw = None
        self.gw_python = find_gateway_python()
        self._logs = []

    def _spawn(self, name, cmd, cwd, env):
        log = open(LOGS / f"{name}.log", "ab")
        log.write(f"\n===== start {time.strftime('%Y-%m-%dT%H:%M:%S')} =====\n".encode())
        log.flush()
        self._logs.append(log)
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=log,
                                stderr=subprocess.STDOUT, bufsize=0), log

    def start_ai(self):
        self.ai, _ = self._spawn("ai", [sys.executable, "main.py"], AI_DIR, ai_env())
        return wait_http(f"{AI_BASE}/health", timeout=120)

    def start_gw(self, users_file):
        self.gw, _ = self._spawn("gateway", [self.gw_python, "app.py"], GW_DIR, gw_env(users_file))
        return wait_http(f"{GW_BASE}/health", timeout=90)

    def stop_ai(self):
        if self.ai and self.ai.poll() is None:
            self.ai.terminate()
            try:
                self.ai.wait(timeout=20)
            except Exception:
                self.ai.kill()
                self.ai.wait()
        self.ai = None

    def stop_all(self):
        self.stop_ai()
        if self.gw and self.gw.poll() is None:
            self.gw.terminate()
            try:
                self.gw.wait(timeout=20)
            except Exception:
                self.gw.kill()
                self.gw.wait()
        self.gw = None
        for log in self._logs:
            try:
                log.close()
            except Exception:
                pass
        self._logs = []


def seed_users(users_file: Path):
    if users_file.exists():
        users_file.unlink()
    env = os.environ.copy()
    env["AUTH_USERS_FILE"] = str(users_file)
    for u in ACCOUNTS:
        r = subprocess.run([sys.executable, "auth_seed.py", "add", u, "-p", PASSWORD],
                           cwd=str(GW_DIR), env=env, capture_output=True, text=True, timeout=40)
        if r.returncode != 0:
            raise RuntimeError(f"播种账号失败 {u}: {r.stderr[-300:]}")
    return users_file


def login(user, ip):
    return requests.post(f"{GW_BASE}/api/auth/login",
                         json={"username": user, "password": PASSWORD},
                         headers={"X-Forwarded-For": ip}, timeout=15)
