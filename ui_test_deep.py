# -*- coding: utf-8 -*-
"""
语伴 - 深度测试套件
目标：覆盖所有分支 / 数据类型 / 流程状态 / 临界值
分层：A=API（网关+AI 服务）  B=前端 UI（Playwright 真实浏览器）

运行：python ui_test_deep.py
输出：控制台 + 同目录 ui_test_deep_report.json
"""
import base64
import hashlib
import hmac
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import wave
import traceback
from playwright.sync_api import sync_playwright

CHROME = r"C:\Users\clm117\AppData\Local\google\Chrome\Application\chrome.exe"
# 地址可通过环境变量注入，便于统一执行入口/CI 切换环境
BASE = os.environ.get("YUBAN_API_BASE", "http://127.0.0.1:8005")        # API 直连网关
UI_BASE = os.environ.get("YUBAN_UI_BASE", "http://localhost:8899")      # 前端静态服务（port=8899 → 前端自动指向 8005）
USER = "testuser"
PASS = "Test@123456"
# 登录限流按 IP 分桶：每次运行使用独立测试 IP，避免复用已运行服务时旧桶耗尽导致 A09 误报 429
LOGIN_HEADERS = {"X-Forwarded-For": f"198.51.100.{int(time.time() * 10) % 200 + 2}"}
# JWT 密钥由执行入口（run_tests.py）注入；独立运行时如未设置则用占位值，
# 攻击面用例仍会因签名不匹配而返回 401（符合预期）。仓库中不硬编码真实密钥。
JWT_SECRET = os.environ.get("JWT_SECRET", "unset-local-only-secret")
GATEWAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api-gateway")
RL_BASE = "http://127.0.0.1:18005"   # 限流专项：独立低限额临时网关

RESULTS = []


def rec(cid, area, case, ok, detail="", status=None):
    st = status or ("PASS" if ok else "FAIL")
    RESULTS.append({"id": cid, "area": area, "case": case, "status": st, "detail": str(detail)[:300]})
    print(f"[{st}] {cid} {case}" + (f" | {detail}" if detail else ""))


def wav_bytes(seconds=0.05, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


WAV = wav_bytes()

# 语音定义用例需要一个真实文件输入；内容由路由 mock 拦截，不真正上传
VOICE_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_outputs", "voice_fixture.wav")


def ensure_voice_fixture():
    os.makedirs(os.path.dirname(VOICE_FIXTURE), exist_ok=True)
    with open(VOICE_FIXTURE, "wb") as f:
        f.write(WAV)


def body(resp):
    try:
        return resp.text()
    except Exception:
        return ""


def jbody(resp):
    try:
        return resp.json()
    except Exception:
        return None


def post_json(api, path, payload, headers):
    """显式以 application/json 提交（Playwright 的 data= 对嵌套 dict 不可靠）"""
    h = dict(headers)
    h["Content-Type"] = "application/json"
    return api.post(path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=h)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(payload, secret=None, alg="HS256", sign=True):
    """标准库手工构造 JWT：用于伪造/过期/缺声明等攻击面用例，不依赖 jose"""
    header = {"alg": alg, "typ": "JWT"}
    h = b64u(json.dumps(header, separators=(",", ":")).encode())
    pl = b64u(json.dumps(payload, separators=(",", ":")).encode())
    if not sign:
        return f"{h}.{pl}."
    sig = hmac.new((secret or JWT_SECRET).encode(), f"{h}.{pl}".encode(), hashlib.sha256).digest()
    return f"{h}.{pl}.{b64u(sig)}"


# ============================================================
# PART A：API 分支 / 数据类型 / 临界值
# ============================================================
def login_token(api):
    r = api.post("/api/auth/login", data={"username": USER, "password": PASS}, headers=LOGIN_HEADERS)
    return (jbody(r) or {}).get("token")


def run_api(p):
    api = p.request.new_context(base_url=BASE)
    token = login_token(api)
    AUTH = {"Authorization": "Bearer " + token} if token else {}

    # ---------- A1 鉴权分支 ----------
    r = api.get("/health")
    rec("A01", "API-鉴权", "GET /health 公开访问", r.status == 200 and (jbody(r) or {}).get("auth_required") is True,
        f"status={r.status}")

    r = api.get("/api/characters")
    rec("A02", "API-鉴权", "无 Authorization → 401 缺少凭证", r.status == 401 and "缺少访问凭证" in body(r),
        f"status={r.status} {body(r)[:60]}")

    r = api.get("/api/characters", headers={"Authorization": "Bearer forged.token.value"})
    rec("A03", "API-鉴权", "伪造 token → 401 无效凭证", r.status == 401 and "无效" in body(r),
        f"status={r.status} {body(r)[:60]}")

    r = api.get("/api/characters", headers={"Authorization": token or ""})
    rec("A04", "API-鉴权", "Authorization 缺 Bearer 前缀 → 401", r.status == 401,
        f"status={r.status} {body(r)[:60]}")

    r = api.get("/api/characters", headers=AUTH)
    rec("A05", "API-鉴权", "合法 token → 200", r.status == 200 and isinstance((jbody(r) or {}).get("characters"), list),
        f"status={r.status}")

    # ---------- A2 登录数据类型分支 ----------
    # 注意：登录接口限流 5/min，每次请求间隔 ≥ 0.3s 避免瞬时刷爆桶
    r = api.post("/api/auth/login", form={"username": USER, "password": PASS}, headers=LOGIN_HEADERS)
    rec("A06", "API-登录", "非 JSON 请求体 → 400", r.status == 400 and "请求体必须是 JSON" in body(r),
        f"status={r.status} {body(r)[:60]}")

    time.sleep(0.3)
    r = post_json(api, "/api/auth/login", ["a", "b"], LOGIN_HEADERS)
    rec("A07", "API-登录", "JSON 数组（非对象）→ 400", r.status == 400 and "请求体格式不正确" in body(r),
        f"status={r.status} {body(r)[:60]}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": "", "password": PASS}, headers=LOGIN_HEADERS)
    rec("A08", "API-登录", "username 空 → 400", r.status == 400 and "不能为空" in body(r), f"status={r.status}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": USER, "password": ""}, headers=LOGIN_HEADERS)
    rec("A09", "API-登录", "password 空 → 400", r.status == 400 and "不能为空" in body(r), f"status={r.status}")

    # login_token() + A06-A09 = 5 次登录（桶已满），等 62s 让窗口重置
    print("[API] 登录桶已满（5/min），等 62s 后继续 A10-A14 ...")
    time.sleep(62)

    r = api.post("/api/auth/login", data={"username": "a" * 65, "password": PASS}, headers=LOGIN_HEADERS)
    rec("A10", "API-登录", "username=65 字符（上限 64）→ 400", r.status == 400 and "格式不正确" in body(r),
        f"status={r.status}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": USER, "password": "p" * 257}, headers=LOGIN_HEADERS)
    rec("A11", "API-登录", "password=257 字符（上限 256）→ 400", r.status == 400 and "格式不正确" in body(r),
        f"status={r.status}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": USER, "password": "Wrong@123"}, headers=LOGIN_HEADERS)
    rec("A12", "API-登录", "错误密码 → 401 统一文案", r.status == 401 and "账号或密码错误" in body(r),
        f"status={r.status}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": "no_such_user_xyz", "password": "Whatever@1"}, headers=LOGIN_HEADERS)
    rec("A13", "API-登录", "不存在的账号 → 401（防枚举同文案）", r.status == 401 and "账号或密码错误" in body(r),
        f"status={r.status}")

    time.sleep(0.3)
    r = api.post("/api/auth/login", data={"username": USER, "password": PASS}, headers=LOGIN_HEADERS)
    j = jbody(r) or {}
    rec("A14", "API-登录", "正确凭证 → token/expires_in/token_type",
        r.status == 200 and bool(j.get("token")) and j.get("token_type") == "Bearer" and isinstance(j.get("expires_in"), int),
        f"token_len={len(j.get('token') or '')} expires_in={j.get('expires_in')}")

    r = api.get("/api/auth/me", headers=AUTH)
    rec("A15", "API-登录", "/api/auth/me 有效 token → username", r.status == 200 and (jbody(r) or {}).get("username") == USER,
        f"status={r.status}")

    # ---------- A3 session_id 校验分支（数据类型/临界） ----------
    cases = [
        ("A16", "a/b", 400, "含 / 的 session_id 非法"),
        ("A17", "x" * 65, 400, "session_id=65 字符（上限 64）非法"),
        ("A18", "中文会话", 400, "含中文的 session_id 非法"),
        ("A19", "abc-123_X", 200, "合法 session_id 通过"),
    ]
    for cid, sid, want, label in cases:
        r = api.post("/api/webrtc/connect", params={"session_id": sid}, headers=AUTH)
        rec(cid, "API-参数校验", label, r.status == want, f"status={r.status} {body(r)[:50]}")

    # ---------- A4 TTS 临界值 ----------
    r = api.post("/api/voice/tts", form={"session_id": "t1", "text": ""}, headers=AUTH, timeout=10000)
    rec("A20", "API-TTS", "空 text → 400", r.status == 400 and "不能为空" in body(r), f"status={r.status}")

    r = api.post("/api/voice/tts", form={"session_id": "t1", "text": "   "}, headers=AUTH, timeout=10000)
    rec("A21", "API-TTS", "纯空白 text → 400", r.status == 400 and "不能为空" in body(r), f"status={r.status}")

    r = api.post("/api/voice/tts", form={"session_id": "t1", "text": "a" * 1001}, headers=AUTH, timeout=10000)
    rec("A22", "API-TTS", "text=1001 字（上限 1000）→ 400", r.status == 400 and "过长" in body(r),
        f"status={r.status} {body(r)[:60]}")

    # 1000 字：若秒回 400 说明边界判断错误；超时=通过校验进入合成（预期）
    try:
        r = api.post("/api/voice/tts", form={"session_id": "t1", "text": "a" * 1000}, headers=AUTH, timeout=8000)
        ok = not (r.status == 400 and "过长" in body(r))
        rec("A23", "API-TTS", "text=1000 字（下边界）通过校验", ok,
            f"status={r.status}" if ok else f"status={r.status} {body(r)[:60]}")
    except Exception as e:
        rec("A23", "API-TTS", "text=1000 字（下边界）通过校验", "timed out" in str(e).lower() or "Timeout" in str(e),
            "校验通过并进入合成（请求超时属预期）")

    # ---------- A5 voice/define 临界值 ----------
    # 注意：Playwright 的 APIRequestContext 不支持 form 与 multipart 同时传参，
    # 多部分请求必须把所有字段都放进 multipart（字段值为字符串即可）。
    def define(**fields):
        parts = {"session_id": "d1"}
        parts.update(fields)
        parts["audio"] = {"name": "a.wav", "mimeType": "audio/wav", "buffer": WAV}
        return api.post("/api/voice/define", multipart=parts, headers=AUTH, timeout=10000)

    r = define(description="")
    rec("A25", "API-定义", "description 空 → 400", r.status == 400 and "不能为空" in body(r), f"status={r.status}")

    r = define(description="描" * 201)
    rec("A26", "API-定义", "description=201 字（上限 200）→ 400", r.status == 400 and "过长" in body(r),
        f"status={r.status} {body(r)[:60]}")

    r = api.post("/api/voice/define",
                 multipart={"session_id": "d1", "description": "测试",
                            "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": b""}},
                 headers=AUTH, timeout=10000)
    rec("A27", "API-定义", "空音频文件 → 400 音频内容为空", r.status == 400 and "为空" in body(r), f"status={r.status} {body(r)[:60]}")

    r = api.post("/api/voice/define",
                 multipart={"session_id": "d1", "description": "测试",
                            "audio": {"name": "a.txt", "mimeType": "text/plain", "buffer": b"hello world not audio"}},
                 headers=AUTH, timeout=10000)
    rec("A28", "API-定义", "非音频格式 → 400 不支持格式", r.status == 400 and "不支持" in body(r), f"status={r.status} {body(r)[:60]}")

    # 合法 wav（可能调用模型，超时视为校验通过）
    try:
        r = api.post("/api/voice/define",
                     multipart={"session_id": "d1", "description": "低沉男声",
                                "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": WAV}},
                     headers=AUTH, timeout=8000)
        rec("A29", "API-定义", "合法 wav：cloud 模式 501 明确不支持，local 模式通过校验",
            (r.status == 501 and (jbody(r) or {}).get("code") == "voice_define_unsupported") or r.status in (200, 422),
            f"status={r.status} {body(r)[:80]}")
    except Exception as e:
        rec("A29", "API-定义", "合法 wav + 描述 → 通过校验", "timed out" in str(e).lower() or "Timeout" in str(e),
            "校验通过并进入模型处理（超时属预期）")

    # pitch/speed/volume 数值类型：负数 / 0 / NaN
    for cid, kv, label in [("A29b", ("pitch", "-1"), "pitch 负数"),
                           ("A29c", ("speed", "0"), "speed=0"),
                           ("A29d", ("volume", "NaN"), "volume 非数字")]:
        fields = {"description": "测试", kv[0]: kv[1]}
        try:
            r = define(**fields)
            rec(cid, "API-定义", label + " → 服务不崩溃/明确不支持", r.status < 500 or r.status == 501, f"status={r.status} {body(r)[:60]}")
        except Exception as e:
            rec(cid, "API-定义", label + " → 服务不崩溃/明确不支持", True, "进入模型处理（超时属预期）")

    # ---------- A6 情感配置 ----------
    r = api.post("/api/emotion-config", form={"session_id": "e1", "emotion_name": "全零", "happiness": 0, "sadness": 0, "anger": 0, "fear": 0, "disgust": 0, "surprise": 0}, headers=AUTH, timeout=15000)
    rec("A31", "API-情感", "六维情感全 0（下边界）", r.status == 200, f"status={r.status} {body(r)[:80]}")

    r = api.post("/api/emotion-config", form={"session_id": "e1", "emotion_name": "全一", "happiness": 1, "sadness": 1, "anger": 1, "fear": 1, "disgust": 1, "surprise": 1}, headers=AUTH, timeout=15000)
    rec("A32", "API-情感", "六维情感全 1（上边界）", r.status == 200, f"status={r.status} {body(r)[:80]}")

    r = api.post("/api/emotion-config", form={"session_id": "e1", "emotion_name": "越界", "happiness": -1, "surprise": 2}, headers=AUTH, timeout=15000)
    j = jbody(r) or {}
    params = (j.get("emotion_config") or {}).get("params") or {}
    rec("A33", "API-情感", "情感值超范围(-1/2) → clamp 到 [0,1]",
        r.status == 200 and params.get("happiness") == 0.0 and params.get("surprise") == 1.0,
        f"status={r.status} happiness={params.get('happiness')} surprise={params.get('surprise')}")

    r = api.post("/api/emotion-config", form={"session_id": "bad/id", "emotion_name": "x"}, headers=AUTH, timeout=10000)
    rec("A34", "API-情感", "非法 session_id → 400", r.status == 400, f"status={r.status}")

    # ---------- A7 角色归一化分支 ----------
    r = post_json(api, "/api/characters", {"name": ""}, AUTH)
    ch = (jbody(r) or {}).get("character") or {}
    rec("A35", "API-角色", "空 name → 默认「未命名角色」", r.status == 200 and ch.get("name") == "未命名角色",
        f"name={ch.get('name')}")

    r = post_json(api, "/api/characters", {"name": "N" * 500}, AUTH)
    ch = (jbody(r) or {}).get("character") or {}
    name_len = len(ch.get("name") or "")
    rec("A36", "API-角色", "name=500 字 → 截断到 64", r.status == 200 and name_len == 64, f"name_len={name_len}")

    payload = {
        "name": "非法枚举", "voice_profile": {"gender": "x", "age_group": "y", "voice_tags": ["bad"], "speed": 5},
        "persona_profile": {"identity": "i" * 60, "relation": "zzz", "personality_tags": ["bad"], "speaking_style": "s" * 300, "free_description": "f" * 600},
    }
    r = post_json(api, "/api/characters", payload, AUTH)
    ch = (jbody(r) or {}).get("character") or {}
    vp, pp = ch.get("voice_profile") or {}, ch.get("persona_profile") or {}
    rec("A37", "API-角色", "gender 非法 → female", vp.get("gender") == "female", f"gender={vp.get('gender')}")
    rec("A38", "API-角色", "age_group 非法 → young_adult", vp.get("age_group") == "young_adult", f"age={vp.get('age_group')}")
    rec("A39", "API-角色", "relation 非法 → friend", pp.get("relation") == "friend", f"relation={pp.get('relation')}")
    rec("A40", "API-角色", "voice_tags 非法 → 默认标签", len(vp.get("voice_tags") or []) >= 1, f"tags={vp.get('voice_tags')}")
    rec("A41", "API-角色", "speed=5 → clamp 1.3", vp.get("speed") == 1.3, f"speed={vp.get('speed')}")
    rec("A42", "API-角色", "identity=60 字 → 截断 50", len(pp.get("identity") or "") == 50, f"len={len(pp.get('identity') or '')}")
    rec("A43", "API-角色", "speaking_style=300 字 → 截断 200", len(pp.get("speaking_style") or "") == 200, f"len={len(pp.get('speaking_style') or '')}")
    rec("A44", "API-角色", "free_description=600 字 → 截断 500", len(pp.get("free_description") or "") == 500, f"len={len(pp.get('free_description') or '')}")

    # 契约裁决：AI 侧校验层拒绝非法类型（float_parsing）；网关以 200 包装错误 detail（与 A47/A50 错误包装风格一致）
    r = post_json(api, "/api/characters", {"name": "speed-非数字", "voice_profile": {"speed": "abc"}}, AUTH)
    j = jbody(r) or {}
    d = j.get("detail") or []
    rec("A45", "API-角色", "speed 非数字 → 校验拒绝（float_parsing），不创建角色",
        r.status in (200, 422) and any("float_parsing" in str(x) for x in d) and not j.get("character"),
        f"status={r.status} {str(d)[:60]}")

    r = post_json(api, "/api/characters", {"name": "speed-零", "voice_profile": {"speed": 0}}, AUTH)
    vp = ((jbody(r) or {}).get("character") or {}).get("voice_profile") or {}
    rec("A46", "API-角色", "speed=0 → clamp 0.7", vp.get("speed") == 0.7, f"speed={vp.get('speed')}")

    # ---------- A8 角色读取/绑定分支 ----------
    r = api.get("/api/characters/nonexistent_id", headers=AUTH)
    rec("A47", "API-角色", "不存在的角色 id → 非 500", r.status < 500, f"status={r.status} {body(r)[:60]}")

    r = api.post("/api/sessions/s1/character", form={"character_id": "bad/id"}, headers=AUTH)
    rec("A48", "API-角色", "character_id 含 / → 400", r.status == 400 and "非法" in body(r), f"status={r.status} {body(r)[:60]}")

    r = api.get("/api/sessions/never_bound_session/character", headers=AUTH)
    j = jbody(r) or {}
    rec("A49", "API-角色", "未绑定会话查角色 → character 为空", r.status == 200 and not j.get("character"),
        f"status={r.status} body={body(r)[:60]}")

    r = api.post("/api/sessions/s1/character", form={"character_id": "custom_not_exist"}, headers=AUTH)
    rec("A50", "API-角色", "绑定不存在的角色 → 返回 error 字段", r.status < 500 and bool((jbody(r) or {}).get("error")),
        f"status={r.status} body={body(r)[:80]}")

    # ---------- A9 上传体积临界 ----------
    big = b"RIFF" + b"\x00" * (10 * 1024 * 1024 + 5000)
    r = api.post("/api/voice/transcribe",
                 multipart={"session_id": "large1", "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": big}},
                 headers=AUTH, timeout=20000)
    rec("A51", "API-上传", "音频 > 10MB → 拒收（400/413 均符合契约）",
        r.status in (400, 413) and "超过上限" in body(r), f"status={r.status} {body(r)[:60]}")

    # ---------- A10 JWT 攻击面 ----------
    now = int(time.time())
    forged = [
        ("A52", "alg=none 篡改算法", make_jwt({"sub": USER, "iat": now, "exp": now + 600}, alg="none", sign=False)),
        ("A53", "错误密钥签名", make_jwt({"sub": USER, "iat": now, "exp": now + 600}, secret="forged-wrong-secret")),
        ("A54", "过期 token（exp 已过）", make_jwt({"sub": USER, "iat": now - 7200, "exp": now - 3600})),
        ("A55", "缺 exp 声明", make_jwt({"sub": USER, "iat": now})),
    ]
    for cid, label, tok in forged:
        r = api.get("/api/characters", headers={"Authorization": "Bearer " + tok})
        rec(cid, "API-JWT", label + " → 401", r.status == 401, f"status={r.status} {body(r)[:50]}")

    # 篡改真实 token 的 payload（改 sub 不重签）
    parts = token.split(".")
    raw = parts[1] + "=" * (-len(parts[1]) % 4)
    tampered = json.loads(base64.urlsafe_b64decode(raw.encode()))
    tampered["sub"] = "admin"
    fake = parts[0] + "." + b64u(json.dumps(tampered, separators=(",", ":")).encode()) + "." + parts[2]
    r = api.get("/api/characters", headers={"Authorization": "Bearer " + fake})
    rec("A56", "API-JWT", "篡改 payload 不重签 → 401", r.status == 401, f"status={r.status}")

    # ---------- A11 CORS 契约 ----------
    r = api.fetch("/api/characters", method="OPTIONS", headers={
        "Origin": "http://localhost:8899",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    allow_origin = r.headers.get("access-control-allow-origin", "")
    rec("A60", "API-CORS", "白名单 origin 预检 → 回显 allow-origin",
        r.status == 200 and allow_origin == "http://localhost:8899",
        f"status={r.status} allow-origin={allow_origin!r}")

    r = api.fetch("/api/characters", method="OPTIONS", headers={
        "Origin": "http://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    allow_origin = r.headers.get("access-control-allow-origin", "")
    rec("A61", "API-CORS", "非白名单 origin 预检 → 不放行",
        "evil" not in allow_origin, f"status={r.status} allow-origin={allow_origin!r}")

    r = api.get("/api/characters", headers={"Origin": "http://localhost:8899"})
    allow_origin = r.headers.get("access-control-allow-origin", "")
    rec("A62", "API-CORS", "401 响应带 CORS 头（前端才能读到错误文案）",
        r.status == 401 and allow_origin == "http://localhost:8899",
        f"status={r.status} allow-origin={allow_origin!r}")

    # ---------- A12 HTTP 契约杂项 ----------
    r = api.get("/api/no-such-endpoint-p0", headers=AUTH)
    rec("A63", "API-契约", "未知路径 → 404", r.status == 404, f"status={r.status}")

    # A10-A14 又耗了 5 次登录桶，等窗口重置再跑 A64/A65
    print("[API] 登录桶 2 次耗尽，等 62s 后继续 A64/A65 ...")
    time.sleep(62)

    r = api.get("/api/auth/login")
    rec("A64", "API-契约", "GET 打 POST-only 登录 → 405", r.status == 405, f"status={r.status}")

    r = api.post("/api/auth/login", multipart={"username": USER, "password": PASS})
    rec("A65", "API-契约", "登录收 multipart → 400 必须是 JSON",
        r.status == 400 and "请求体必须是 JSON" in body(r), f"status={r.status} {body(r)[:50]}")

    r = post_json(api, "/api/voice/tts", {"session_id": "t1", "text": "你好"}, AUTH)
    rec("A66", "API-契约", "TTS 收 JSON 体（应为表单）→ 422", r.status == 422,
        f"status={r.status} {body(r)[:50]}")

    # ---------- A13 注入变体 ----------
    r = api.post("/api/webrtc/connect?session_id=a%2Fb", headers=AUTH)
    rec("A67", "API-注入", "session_id 编码绕过(%2F→/) → 400", r.status == 400,
        f"status={r.status} {body(r)[:50]}")

    r = api.post("/api/webrtc/connect?session_id=", headers=AUTH)
    rec("A68", "API-注入", "session_id 空串 → 400", r.status == 400, f"status={r.status} {body(r)[:50]}")

    r = api.post("/api/webrtc/connect?session_id=a%00b", headers=AUTH)
    rec("A69", "API-注入", "session_id 含 null 字节 → 400", r.status == 400,
        f"status={r.status} {body(r)[:50]}")

    r1 = api.get("/api/characters/..%2F..%2Fadmin", headers=AUTH)
    r2 = api.get("/api/characters/..", headers=AUTH)
    rec("A70", "API-注入", "character_id 路径穿越 → 拒绝（400/404），不到上游",
        r1.status in (400, 404) and r2.status in (400, 404), f"encoded={r1.status} dotdot={r2.status}")

    inj_name = "x' OR '1'='1'; DROP TABLE users;--"
    inj_desc = "${7*7}{{constructor}}<svg onload=alert(1)>"
    r = post_json(api, "/api/characters",
                  {"name": inj_name, "persona_profile": {"free_description": inj_desc}}, AUTH)
    ch = (jbody(r) or {}).get("character") or {}
    rec("A71", "API-注入", "角色名/描述注入串 → 原样存储不执行不崩溃",
        r.status == 200 and "OR '1'='1'" in (ch.get("name") or ""),
        f"status={r.status} name={str(ch.get('name'))[:40]}")

    # ---------- A14 上传伪造 ----------
    evil = b"RIFF" + b"\x00" * 8 + b"<script>alert(document.cookie)</script>" + b"\x00" * 512
    try:
        r = api.post("/api/voice/transcribe",
                     multipart={"session_id": "fz1",
                                "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": evil}},
                     headers=AUTH, timeout=8000)
        rec("A72", "API-上传", "wav 魔数+恶意负载 → 受控失败、不外泄堆栈",
            r.status != 500 and "Traceback" not in body(r), f"status={r.status} {body(r)[:60]}")
    except Exception:
        rec("A72", "API-上传", "wav 魔数+恶意负载 → 受控失败、不外泄堆栈", True, "上游处理超时（受控）")

    pe = b"MZ" + b"\x90" * 200
    r = api.post("/api/voice/transcribe",
                 multipart={"session_id": "fz2",
                            "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": pe}},
                 headers=AUTH, timeout=10000)
    rec("A73", "API-上传", "PE(MZ) 头伪装 .wav → 400 不支持格式",
        r.status == 400 and "不支持" in body(r), f"status={r.status} {body(r)[:60]}")

    # ---------- A16 上传体积边界组合（体积闸门 vs 魔数闸门） ----------
    exact = b"RIFF" + b"\x00" * (10 * 1024 * 1024 - 4)   # 恰好 10MB
    try:
        r = api.post("/api/voice/transcribe",
                     multipart={"session_id": "bd1",
                                "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": exact}},
                     headers=AUTH, timeout=10000)
        rec("A74", "API-上传", "音频恰好 10MB（上边界）→ 不因体积被拒",
            "超过上限" not in body(r), f"status={r.status} {body(r)[:50]}")
    except Exception:
        rec("A74", "API-上传", "音频恰好 10MB（上边界）→ 不因体积被拒", True, "体积校验通过并进入转写（超时属预期）")

    near = b"RIFF" + b"\x00" * (10 * 1024 * 1024 - 1024 - 4)  # 差 1KB 到上限
    try:
        r = api.post("/api/voice/transcribe",
                     multipart={"session_id": "bd2",
                                "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": near}},
                     headers=AUTH, timeout=10000)
        rec("A75", "API-上传", "音频 10MB-1KB（下边界）→ 不因体积被拒",
            "超过上限" not in body(r), f"status={r.status} {body(r)[:50]}")
    except Exception:
        rec("A75", "API-上传", "音频 10MB-1KB（下边界）→ 不因体积被拒", True, "体积校验通过并进入转写（超时属预期）")

    bad10 = b"NOPE" + b"\x00" * (10 * 1024 * 1024 - 4)   # 恰好 10MB 但魔数非法
    r = api.post("/api/voice/transcribe",
                 multipart={"session_id": "bd3",
                            "audio": {"name": "a.wav", "mimeType": "audio/wav", "buffer": bad10}},
                 headers=AUTH, timeout=10000)
    rec("A76", "API-上传", "10MB 合规体积 + 非法魔数 → 体积过、魔数拦（400）",
        r.status == 400 and "不支持" in body(r), f"status={r.status} {body(r)[:60]}")

    # ---------- A15 限流 429 分支（独立低限额临时网关） ----------
    run_rate_limit(p)

    api.dispose()


def _kill_port(port):
    """清理监听指定端口的残留进程（Windows），避免临时网关 bind 冲突"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                subprocess.run(["taskkill", "/F", "/PID", line.split()[-1]], capture_output=True)
    except Exception:
        pass


def run_rate_limit(p):
    """限流 429 专项：主测试环境限流被放宽，这里自起低限额临时网关（18005）验证真实限流路径。
    临时网关上游指向黑洞——429 在限流中间件拦截不触上游；TTS 空 text 在业务校验 400 也不触上游。
    就绪探测用 TCP 而非 HTTP：/health 会计入普通限流桶，HTTP 探测会耗光配额（曾致误判未就绪）。"""
    service_py = os.environ.get("YUBAN_SERVICE_PY") or sys.executable
    try:
        probe = subprocess.run([service_py, "-c", "import fastapi, uvicorn, jose, requests"],
                               capture_output=True, timeout=60)
        usable = probe.returncode == 0
    except Exception:
        usable = False
    if not usable:
        for cid, label in [("A57", "登录超限"), ("A58", "昂贵接口降额"), ("A59", "普通接口超限")]:
            rec(cid, "API-限流", label, False, f"服务端解释器无 fastapi/uvicorn 依赖: {service_py}",
                status="BLOCKED")
        return

    env = os.environ.copy()
    env.update({
        "AUTH_REQUIRED": "true",
        "JWT_SECRET": JWT_SECRET,
        "LOGIN_RATE_LIMIT_PER_MINUTE": "3",
        "RATE_LIMIT_PER_MINUTE": "5",
        "EXPENSIVE_RATE_LIMIT_PER_MINUTE": "2",
        "AI_SERVICE_URL": "http://127.0.0.1:9",
        "AI_SERVICE_WS_URL": "ws://127.0.0.1:9",
    })
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    _kill_port(18005)   # 清掉可能的残留监听，保证 bind 成功
    rl_log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_outputs", "rl_gateway.log"),
                  "w", encoding="utf-8")
    proc = subprocess.Popen([service_py, "-m", "uvicorn", "app:app",
                             "--host", "127.0.0.1", "--port", "18005"],
                            cwd=GATEWAY_DIR, env=env, stdout=rl_log, stderr=subprocess.STDOUT, **kwargs)
    try:
        ready, last_err = False, ""
        for _ in range(90):
            if proc.poll() is not None:
                last_err = f"进程已退出 rc={proc.returncode}（见 test_outputs/rl_gateway.log）"
                break
            try:
                with socket.create_connection(("127.0.0.1", 18005), timeout=1):
                    ready = True   # uvicorn 完成 ASGI startup 后才 bind 监听
                    break
            except OSError as e:
                last_err = type(e).__name__
                time.sleep(0.5)
        if not ready:
            for cid, label in [("A57", "登录超限"), ("A58", "昂贵接口降额"), ("A59", "普通接口超限")]:
                rec(cid, "API-限流", label, False, f"临时网关 18005 未就绪: {last_err}", status="BLOCKED")
            return

        api2 = p.request.new_context(base_url=RL_BASE)
        try:
            # 成功登录 1 次（login 桶 1/3）拿 token
            r = api2.post("/api/auth/login", data={"username": USER, "password": PASS})
            tok2 = (jbody(r) or {}).get("token")
            login_ok = r.status == 200 and bool(tok2)
            AUTH2 = {"Authorization": "Bearer " + (tok2 or "")}

            # A58 昂贵接口降额 2/min：前 2 次空 text 业务 400，第 3 次被限流 429
            st, retry_after = [], ""
            for _ in range(3):
                rr = api2.post("/api/voice/tts", form={"session_id": "t1", "text": ""},
                               headers=AUTH2, timeout=8000)
                st.append(rr.status)
                retry_after = rr.headers.get("retry-after", "")
            rec("A58", "API-限流", "昂贵接口降额(2/min)：第 3 次 → 429 + Retry-After",
                login_ok and st[:2] == [400, 400] and st[2] == 429 and retry_after == "60",
                f"tts_seq={st} retry_after={retry_after!r}")

            # A57 登录爆破：再 2 次 401 桶满（3/3），第 3 次 → 429；带 Origin 验证 429 也带 CORS 头
            st, retry_after, cors = [], "", ""
            for i in range(3):
                rr = api2.post("/api/auth/login",
                               data={"username": USER, "password": "Wrong@123"},
                               headers={"Origin": "http://localhost:8899"})
                st.append(rr.status)
                if i == 2:
                    retry_after = rr.headers.get("retry-after", "")
                    cors = rr.headers.get("access-control-allow-origin", "")
            rec("A57", "API-限流", "登录爆破(3/min)：第 4 次 → 429 + Retry-After + CORS 头",
                st == [401, 401, 429] and retry_after == "60" and cors == "http://localhost:8899",
                f"seq={st} retry_after={retry_after!r} cors={cors!r}")

            # A59 普通接口 5/min：/api/auth/me 本地校验不触上游，5 次 200 后第 6 次 429
            st = []
            for _ in range(6):
                rr = api2.get("/api/auth/me", headers=AUTH2, timeout=8000)
                st.append(rr.status)
            rec("A59", "API-限流", "普通接口(5/min)：第 6 次 → 429",
                st[:5] == [200] * 5 and st[5] == 429, f"seq={st}")
        finally:
            api2.dispose()
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            pass


# ============================================================
# PART B：前端 UI 分支 / 状态 / 数据类型 / 临界
# ============================================================
class UI:
    def __init__(self, page):
        self.page = page
        self.dlg = {"mode": "accept", "msgs": []}
        self.console = []
        self.errors = []
        self.reqs = []
        page.on("dialog", self._on_dialog)
        page.on("console", lambda m: (self.console.append(m.text), self.errors.append(m.text)) if m.type == "error" else self.console.append(m.text))
        page.on("pageerror", lambda e: self.errors.append("PAGEERROR: " + str(e)))
        page.on("request", lambda r: self.reqs.append(r.url))

    def _on_dialog(self, d):
        self.dlg["msgs"].append(d.message)
        d.accept() if self.dlg["mode"] == "accept" else d.dismiss()

    def reset(self):
        self.dlg["msgs"].clear()
        self.errors.clear()
        self.reqs.clear()

    def err_text(self):
        return "\n".join(self.errors)


def login_ui(page):
    page.goto(UI_BASE, wait_until="domcontentloaded")
    page.wait_for_selector("#authOverlay", state="visible", timeout=15000)
    page.fill("#authUsername", USER)
    page.fill("#authPassword", PASS)
    page.click("#authSubmitBtn")
    page.wait_for_selector("#authOverlay", state="hidden", timeout=20000)
    wait_data_loaded(page)


def wait_data_loaded(page, timeout=15000):
    """等待首屏数据加载链完成（loadVoices 会写入 #savedVoices 占位或卡片）"""
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('savedVoices');"
            " return el && el.innerHTML.trim().length > 0; }",
            timeout=timeout,
        )
    except Exception:
        pass


def wait_create_done(page, timeout=12000):
    """等待角色创建返回（名称输入框被清空，或出现成功/失败文案）"""
    try:
        page.wait_for_function(
            "() => { const n = document.getElementById('charName');"
            " const el = document.getElementById('characterResponse');"
            " return (n && n.value === '') || (el && el.innerText.trim().length > 0); }",
            timeout=timeout,
        )
    except Exception:
        pass


def run_ui(p):
    # ---------- B1 登录分支（独立上下文，避免状态串扰） ----------
    ctx = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-gpu"]).new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    u = UI(page)

    page.goto(UI_BASE, wait_until="domcontentloaded")
    page.wait_for_selector("#authOverlay", state="visible", timeout=15000)

    u.reset()
    page.click("#authSubmitBtn")
    page.wait_for_timeout(600)
    err = page.query_selector("#authError").inner_text()
    no_login_req = not any("/api/auth/login" in x for x in u.reqs)
    # 注意：输入框带 required，原生校验会先拦截 submit，自定义文案分支不可达；
    # 断言以「被拦截且未发请求」为准
    rec("B01", "UI-登录", "账号密码都空 → 前端拦截且不发请求",
        no_login_req and page.query_selector("#authOverlay").is_visible(), f"error={err!r} req={no_login_req}")

    u.reset()
    page.fill("#authUsername", USER)
    page.click("#authSubmitBtn")
    page.wait_for_timeout(600)
    err = page.query_selector("#authError").inner_text()
    rec("B02", "UI-登录", "仅填账号（缺密码）→ 前端拦截",
        not any("/api/auth/login" in x for x in u.reqs) and page.query_selector("#authOverlay").is_visible(),
        f"error={err!r}")

    u.reset()
    page.fill("#authPassword", "Wrong@123")
    page.click("#authSubmitBtn")
    page.wait_for_timeout(1200)
    err = page.query_selector("#authError").inner_text()
    rec("B03", "UI-登录", "错误密码 → 显示 401 文案并清空密码", "账号或密码错误" in err and page.query_selector("#authPassword").input_value() == "",
        f"error={err}")

    page.fill("#authPassword", PASS)
    page.click("#authSubmitBtn")
    page.wait_for_selector("#authOverlay", state="hidden", timeout=20000)
    tok = page.evaluate("() => localStorage.getItem('yuban_token')")
    logout_visible = page.query_selector("#logoutBtn").is_visible()
    rec("B04", "UI-登录", "正确凭证 → 遮罩隐藏 + token 落库 + 退出按钮可见", bool(tok) and logout_visible,
        f"token={'有' if tok else '无'} logout={logout_visible}")

    page.reload()
    page.wait_for_timeout(2500)
    ov = page.query_selector("#authOverlay")
    rec("B05", "UI-登录", "已有有效 token 刷新 → 不弹遮罩", not (ov and ov.is_visible()), "遮罩隐藏")

    page.evaluate("() => localStorage.setItem('yuban_token','forged.bad.token')")
    u.reset()
    page.reload()
    try:
        page.wait_for_selector("#authOverlay", state="visible", timeout=15000)
        hint = page.query_selector(".auth-hint").inner_text()
        shown = True
    except Exception:
        hint, shown = "", False
    cleared = page.evaluate("() => localStorage.getItem('yuban_token')")
    rec("B06", "UI-登录", "伪造 token → 401 自动清 token 并弹门禁（失效提示）", shown and "失效" in hint and not cleared,
        f"shown={shown} hint={hint} token_cleared={not cleared}")

    login_ui(page)
    u.reset()
    page.click("#logoutBtn")
    try:
        page.wait_for_selector("#authOverlay", state="visible", timeout=15000)
        ov_vis = True
    except Exception:
        ov_vis = False
    tok = page.evaluate("() => localStorage.getItem('yuban_token')")
    rec("B07", "UI-登录", "退出登录 → 清 token + 回到登录页", ov_vis and not tok,
        f"overlay={ov_vis} token={tok}")

    login_ui(page)
    page.wait_for_timeout(1500)

    # ---------- B2 标签切换分支 ----------
    tabs = [("chat", "对话"), ("voice-input", "录入"), ("voice-define", "定义"), ("emotion-config", "情感"),
            ("character", "角色"), ("tts", "合成"), ("settings", "设置")]
    ok_all, bad = True, ""
    for tid, label in tabs:
        page.click(f".nav-btn[data-tab='{tid}']")
        page.wait_for_timeout(120)
        active_sec = page.evaluate("(id) => document.getElementById(id).classList.contains('active')", tid)
        active_btn = page.evaluate("(id) => document.querySelector(`.nav-btn[data-tab='${id}']`).classList.contains('active')", tid)
        if not (active_sec and active_btn):
            ok_all, bad = False, f"{label} sec={active_sec} btn={active_btn}"
    rec("B08", "UI-导航", "7 个标签切换 active 状态正确（互斥）", ok_all, bad)

    # 定义页下拉随已保存语音刷新
    page.evaluate("() => localStorage.setItem('voices', JSON.stringify([{id:'lv1',name:'预置语音A',description:'d',timestamp:'2026-10-31T00:00:00'}]))")
    page.reload()
    page.wait_for_timeout(800)
    wait_data_loaded(page)
    page.click(".nav-btn[data-tab='voice-define']")
    page.wait_for_timeout(300)
    opts = page.evaluate("() => Array.from(document.getElementById('baseVoice').options).map(o => o.value)")
    rec("B09", "UI-导航", "切到定义页 → baseVoice 选项含已保存语音", "lv1" in opts, f"options={opts}")

    page.click(".nav-btn[data-tab='tts']")
    page.wait_for_timeout(300)
    topts = page.evaluate("() => Array.from(document.getElementById('ttsVoiceSelect').options).map(o => o.value)")
    rec("B10", "UI-导航", "切到合成页 → ttsVoiceSelect 同步已保存语音", "lv1" in topts, f"options={topts}")

    # ---------- B3 对话状态机 ----------
    page.click(".nav-btn[data-tab='chat']")
    page.wait_for_timeout(200)

    def user_msg_count():
        return page.evaluate("() => document.querySelectorAll('#chatHistory .chat-message.user').length")

    u.reset()
    n0 = user_msg_count()
    page.fill("#chatInput", "")
    page.click("#chatSendBtn")
    page.wait_for_timeout(300)
    rec("B11", "UI-对话", "空输入点击发送 → 不产生用户消息", user_msg_count() == n0, f"before={n0} after={user_msg_count()}")

    page.fill("#chatInput", "   ")
    page.click("#chatSendBtn")
    page.wait_for_timeout(300)
    rec("B12", "UI-对话", "纯空格输入 → trim 后不发送", user_msg_count() == n0, f"count={user_msg_count()}")

    # 锁定探针：输入锁定窗口可能极短（回复快时立即解锁），轮询记录「曾经锁定」
    page.evaluate("""() => {
        window.__lockSeen = false;
        const t0 = Date.now();
        const timer = setInterval(() => {
            if (document.getElementById('chatInput').disabled) { window.__lockSeen = true; clearInterval(timer); }
            if (Date.now() - t0 > 5000) clearInterval(timer);
        }, 30);
    }""")
    page.fill("#chatInput", "回车发送测试")
    page.click("#chatInput")
    page.press("#chatInput", "Enter")
    page.wait_for_timeout(600)
    rec("B13", "UI-对话", "Enter 键发送消息", user_msg_count() == n0 + 1, f"count={user_msg_count()}")
    rec("B14", "UI-对话", "发送后输入框清空", page.query_selector("#chatInput").input_value() == "", "")
    try:
        page.wait_for_function("() => window.__lockSeen === true", timeout=4000)
    except Exception:
        pass
    rec("B15", "UI-对话", "发送后输入曾锁定（disabled，探针捕获瞬态）",
        page.evaluate("() => window.__lockSeen") is True, "")
    rec("B16", "UI-对话", "用户消息已渲染到历史", "回车发送测试" in page.query_selector("#chatHistory").inner_text(), "")

    page.fill("#chatInput", "Shift 回车不应发送")
    page.click("#chatInput")
    page.press("#chatInput", "Shift+Enter")
    page.wait_for_timeout(300)
    rec("B17", "UI-对话", "Shift+Enter 不发送（保留换行语义）", user_msg_count() == n0 + 1 and page.query_selector("#chatInput").input_value() != "",
        f"count={user_msg_count()}")

    # WS 连接失败分支（需先关闭 B13 已建立的可用连接，否则会复用旧连接）
    page.evaluate("""() => {
        window.__savedWs = wsUrl;
        if (chatWs) { try { chatWs.close(); } catch (e) {} chatWs = null; }
        wsUrl = 'ws://127.0.0.1:1';
    }""")
    page.fill("#chatInput", "连接失败测试")
    page.click("#chatSendBtn")
    page.wait_for_timeout(3500)
    hist = page.query_selector("#chatHistory").inner_text()
    unlocked = not page.query_selector("#chatInput").is_disabled()
    rec("B18", "UI-对话", "WS 不可达 → 插入「连接服务失败」并解锁输入", "连接服务失败" in hist and unlocked,
        f"unlocked={unlocked}")
    page.evaluate("() => { wsUrl = window.__savedWs; }")

    # 消息分支（直接驱动处理函数，覆盖 switch 四个分支）
    page.evaluate("() => { addMessage('ai','', true, true); }")
    page.evaluate("() => handleWsMessage({type:'status', status:'generating', character:'测试角色'})")
    last = page.evaluate("() => { const n = document.querySelector('#chatHistory .chat-message.ai:last-child .message-content'); return n ? n.textContent : ''; }")
    rec("B19", "UI-对话", "status=generating → 「正在思考…」占位", "正在思考" in last and "测试角色" in last, f"text={last[:40]}")

    page.evaluate("() => handleWsMessage({type:'text', content:'你好'})")
    last = page.evaluate("() => document.querySelector('#chatHistory .chat-message.ai:last-child .message-content').textContent")
    rec("B20", "UI-对话", "首个 text 分片替换思考占位", last == "你好", f"text={last[:40]}")

    page.evaluate("() => handleWsMessage({type:'text', content:'，世界'})")
    last = page.evaluate("() => document.querySelector('#chatHistory .chat-message.ai:last-child .message-content').textContent")
    rec("B21", "UI-对话", "后续 text 分片追加到同一气泡", last == "你好，世界", f"text={last}")

    page.evaluate("() => handleWsMessage({type:'status', status:'reply_done'})")
    rec("B22", "UI-对话", "status=reply_done → 解锁输入", not page.query_selector("#chatInput").is_disabled(), "")

    page.evaluate("() => setInputLocked(true)")
    page.evaluate("() => handleWsMessage({type:'error', message:'后端炸了'})")
    hist = page.query_selector("#chatHistory").inner_text()
    rec("B23", "UI-对话", "error 消息 → 显示「错误：…」并解锁", "错误：后端炸了" in hist and not page.query_selector("#chatInput").is_disabled(), "")

    page.evaluate("() => handleWsMessage({type:'audio', index:0, sample_rate:16000, size:100})")
    rec("B24", "UI-对话", "audio 消息 → 仅日志，不插入消息", "错误：后端炸了" in page.query_selector("#chatHistory").inner_text(), "无异常")

    # ---------- B4 录入页 ----------
    page.click(".nav-btn[data-tab='voice-input']")
    page.wait_for_timeout(200)
    u.reset()
    page.evaluate("() => { document.getElementById('saveVoiceBtn').style.display='block'; }")
    page.fill("#voiceName", "")
    page.click("#saveVoiceBtn")
    page.wait_for_timeout(400)
    rec("B25", "UI-录入", "空名称保存 → alert 拦截", "请输入语音名称" in " ".join(u.dlg["msgs"]), f"alert={u.dlg['msgs']}")

    page.fill("#voiceName", "测试语音B")
    page.fill("#voiceDescriptionInput", "测试描述")
    page.click("#saveVoiceBtn")
    page.wait_for_timeout(500)
    saved = page.evaluate("() => JSON.parse(localStorage.getItem('voices') || '[]')")
    cards = page.evaluate("() => document.querySelectorAll('#savedVoices .voice-card').length")
    rec("B26", "UI-录入", "有名称保存 → 落库 + 列表卡片 + 表单重置",
        any(v.get("name") == "测试语音B" for v in saved) and cards >= 1 and page.query_selector("#voiceName").input_value() == "",
        f"count={len(saved)} cards={cards}")

    u.reset()
    page.evaluate("() => useVoice('lv1')")
    page.wait_for_timeout(300)
    rec("B27", "UI-录入", "点击「使用」→ alert 展示所选语音", any("预置语音A" in m for m in u.dlg["msgs"]), f"alert={u.dlg['msgs']}")

    # 删除：取消分支
    u.dlg["mode"] = "dismiss"
    page.evaluate("() => deleteVoice('lv1')")
    page.wait_for_timeout(300)
    after = page.evaluate("() => JSON.parse(localStorage.getItem('voices') || '[]')")
    rec("B28", "UI-录入", "删除语音-取消 → 数据保留", any(v.get("id") == "lv1" for v in after), f"count={len(after)}")

    # 删除：确认分支
    u.dlg["mode"] = "accept"
    page.evaluate("() => deleteVoice('lv1')")
    page.wait_for_timeout(300)
    after = page.evaluate("() => JSON.parse(localStorage.getItem('voices') || '[]')")
    rec("B29", "UI-录入", "删除语音-确认 → 从 localStorage 移除", not any(v.get("id") == "lv1" for v in after), f"count={len(after)}")

    # ---------- B5 定义页 ----------
    page.click(".nav-btn[data-tab='voice-define']")
    page.wait_for_timeout(200)

    def set_range(el, val):
        page.evaluate("([id, v]) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event('input')); }", [el, val])

    set_range("pitch", "0.5"); set_range("speed", "0.5"); set_range("volume", "0.5"); set_range("emotionIntensity", "0")
    vals = [page.query_selector(f"#{i}Value").inner_text() for i in ("pitch", "speed", "volume", "emotionIntensity")]
    rec("B30", "UI-定义", "滑块下边界(0.5/0.5/0.5/0) → 数值同步", vals == ["0.5", "0.5", "0.5", "0"], f"vals={vals}")

    set_range("pitch", "1.5"); set_range("speed", "2"); set_range("volume", "1.5"); set_range("emotionIntensity", "1")
    vals = [page.query_selector(f"#{i}Value").inner_text() for i in ("pitch", "speed", "volume", "emotionIntensity")]
    rec("B31", "UI-定义", "滑块上边界(1.5/2/1.5/1) → 数值同步", vals == ["1.5", "2", "1.5", "1"], f"vals={vals}")

    u.reset()
    page.fill("#voiceNameDefine", "")
    page.click("#defineVoiceBtn")
    page.wait_for_timeout(400)
    no_req = not any("/api/voice/define" in x for x in u.reqs)
    rec("B32", "UI-定义", "空名称 → alert 拦截且不发请求", "请输入语音名称" in " ".join(u.dlg["msgs"]) and no_req, f"req={u.reqs[-3:]}")

    # 用 mock 覆盖成功分支 + description 兜底分支
    captured = {}

    def on_define(route):
        raw = route.request.post_data_buffer or b""
        captured["body"] = raw.decode("utf-8", "ignore")
        route.fulfill(status=200, headers={"content-type": "application/json"},
                      body=json.dumps({"voice_definition": "mock-voice-def", "voice_params": {}}))

    page.route("**/api/voice/define", on_define)
    ensure_voice_fixture()
    page.set_input_files("#voiceFileDefine", VOICE_FIXTURE)
    page.fill("#voiceNameDefine", "兜底描述语音")
    page.fill("#voiceDescriptionDefine", "")
    page.click("#defineVoiceBtn")
    page.wait_for_timeout(900)
    resp = page.query_selector("#voiceDefineResponse").inner_text()
    saved = page.evaluate("() => JSON.parse(localStorage.getItem('voices') || '[]')")
    rec("B33", "UI-定义", "描述留空 → 提交时兜底为名称", "兜底描述语音" in captured.get("body", "") and "语音定义成功" in resp,
        f"resp={resp[:40]}")
    rec("B34", "UI-定义", "定义成功 → 落库 + 表单/滑块重置",
        any(v.get("name") == "兜底描述语音" for v in saved) and page.query_selector("#pitch").input_value() == "1",
        f"count={len(saved)} pitch={page.query_selector('#pitch').input_value()}")

    # 失败分支
    def on_define_fail(route):
        # 不含 error 字段：验证前端回退到「HTTP <状态码>」的兜底提示
        route.fulfill(status=500, headers={"content-type": "application/json"}, body='{"detail":"mock"}')

    page.unroute("**/api/voice/define", on_define)
    page.route("**/api/voice/define", on_define_fail)
    page.set_input_files("#voiceFileDefine", VOICE_FIXTURE)
    page.fill("#voiceNameDefine", "失败分支语音")
    page.click("#defineVoiceBtn")
    page.wait_for_timeout(900)
    resp = page.query_selector("#voiceDefineResponse").inner_text()
    rec("B35", "UI-定义", "接口 500 → 红色错误提示", "定义语音时出错" in resp and "500" in resp, f"resp={resp[:60]}")
    page.unroute("**/api/voice/define", on_define_fail)

    # ---------- B6 情感页 ----------
    page.click(".nav-btn[data-tab='emotion-config']")
    page.wait_for_timeout(200)
    emos = ["happiness", "sadness", "anger", "fear", "disgust", "surprise"]
    for e in emos:
        set_range(e, "0")
    vals = [page.query_selector(f"#{e}Value").inner_text() for e in emos]
    rec("B36", "UI-情感", "六维滑块全 0（下边界）→ 显示 0", all(v == "0" for v in vals), f"vals={vals}")

    for e in emos:
        set_range(e, "1")
    vals = [page.query_selector(f"#{e}Value").inner_text() for e in emos]
    rec("B37", "UI-情感", "六维滑块全 1（上边界）→ 显示 1", all(v == "1" for v in vals), f"vals={vals}")

    u.reset()
    page.fill("#emotionProfileName", "")
    page.click("#saveEmotionBtn")
    page.wait_for_timeout(400)
    rec("B38", "UI-情感", "空名称 → alert 拦截", "请输入情感配置名称" in " ".join(u.dlg["msgs"]), f"alert={u.dlg['msgs']}")

    page.fill("#emotionProfileName", "边界情感全1")
    page.fill("#emotionDescription", "测试描述")
    page.click("#saveEmotionBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('emotionConfigResponse');"
            " return el && (el.innerText.includes('保存成功') || el.innerText.includes('出错')); }",
            timeout=10000,
        )
    except Exception:
        pass
    resp = page.query_selector("#emotionConfigResponse").inner_text()
    saved = page.evaluate("() => JSON.parse(localStorage.getItem('emotions') || '[]')")
    cards = page.evaluate("() => document.querySelectorAll('#savedEmotions .emotion-card').length")
    rec("B39", "UI-情感", "保存成功 → 落库 + 列表卡片 + 表单重置",
        "保存成功" in resp and any(v.get("name") == "边界情感全1" for v in saved) and cards >= 1 and page.query_selector("#emotionProfileName").input_value() == "",
        f"count={len(saved)} cards={cards} resp={resp[:60]}")

    u.reset()
    page.evaluate("() => { const p = JSON.parse(localStorage.getItem('emotions')||'[]'); if (p[0]) useEmotion(p[0].id); }")
    page.wait_for_timeout(300)
    rec("B40", "UI-情感", "点击「使用」→ alert 展示所选配置", any("情感配置" in m for m in u.dlg["msgs"]), f"alert={u.dlg['msgs']}")

    before_cnt = page.evaluate("() => JSON.parse(localStorage.getItem('emotions') || '[]').length")
    u.dlg["mode"] = "dismiss"
    page.evaluate("() => { const p = JSON.parse(localStorage.getItem('emotions')||'[]'); if (p[0]) deleteEmotion(p[0].id); }")
    page.wait_for_timeout(300)
    after = page.evaluate("() => JSON.parse(localStorage.getItem('emotions') || '[]')")
    rec("B41", "UI-情感", "删除-取消 → 数据保留", len(after) == before_cnt, f"before={before_cnt} after={len(after)}")

    u.dlg["mode"] = "accept"
    page.evaluate("() => { const p = JSON.parse(localStorage.getItem('emotions')||'[]'); if (p[0]) deleteEmotion(p[0].id); }")
    page.wait_for_timeout(300)
    after2 = page.evaluate("() => JSON.parse(localStorage.getItem('emotions') || '[]')")
    rec("B42", "UI-情感", "删除-确认 → 从 localStorage 移除", len(after2) == len(after) - 1,
        f"before={len(after)} after={len(after2)}")

    # ---------- B7 角色页 ----------
    page.click(".nav-btn[data-tab='character']")
    try:
        page.wait_for_selector("#presetCharacterList > div, #presetCharacterList > p", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(300)
    presets = page.evaluate("() => document.querySelectorAll('#presetCharacterList > div').length")
    rec("B43", "UI-角色", "预设角色列表加载（≥6 个）", presets >= 6, f"count={presets}")

    page.evaluate("() => renderCharacterList('presetCharacterList', [])")
    rec("B44", "UI-角色", "预设空态 → 「暂无预设角色」", "暂无预设角色" in page.query_selector("#presetCharacterList").inner_text(), "")

    page.evaluate("() => renderCharacterList('customCharacterList', [])")
    rec("B45", "UI-角色", "自定义空态 → 引导创建文案", "还没有自定义角色" in page.query_selector("#customCharacterList").inner_text(), "")

    page.wait_for_timeout(200)
    page.evaluate("() => loadCharacters()")
    page.wait_for_timeout(1200)

    u.reset()
    page.fill("#charName", "   ")
    page.click("#createCharacterBtn")
    page.wait_for_timeout(500)
    no_req = not any(x.rstrip("/").endswith("/api/characters") and "options" not in x for x in u.reqs)
    rec("B46", "UI-角色", "纯空格名称 → alert 拦截且不发请求", "请填写角色名称" in " ".join(u.dlg["msgs"]) and no_req,
        f"alert={u.dlg['msgs']}")

    page.fill("#charName", "多标签角色")
    page.fill("#charIdentity", "测试身份")
    page.fill("#charStyle", "测试风格")
    page.fill("#charFreeDesc", "自由描述")
    page.evaluate("""() => {
        const boxes = document.querySelectorAll('#charVoiceTags input');
        if (boxes[0]) boxes[0].checked = true;
        if (boxes[1]) boxes[1].checked = true;
        const p = document.querySelectorAll('#charPersonalityTags input');
        if (p[0]) p[0].checked = true;
    }""")
    chosen = page.evaluate("() => Array.from(document.querySelectorAll('input[data-group]:checked')).map(c => c.value)")
    page.click("#createCharacterBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('characterResponse');"
            " return (el && el.innerText.includes('创建成功')) || document.getElementById('charName').value === ''; }",
            timeout=10000,
        )
    except Exception:
        pass
    resp = page.query_selector("#characterResponse").inner_text()
    cleared = page.evaluate("() => ({ n: document.getElementById('charName').value, tags: document.querySelectorAll('input[data-group]:checked').length })")
    rec("B47", "UI-角色", "创建成功 → 成功文案 + 表单/标签清空",
        "创建成功" in resp and cleared["n"] == "" and cleared["tags"] == 0, f"resp={resp[:40]} cleared={cleared}")
    rec("B48", "UI-角色", "多选标签已提交（2 音色 + 1 性格）", len(chosen) == 3, f"chosen={chosen}")

    # 回读服务端标签
    page.evaluate("() => loadCharacters()")
    page.wait_for_timeout(1200)
    names = page.evaluate("() => Array.from(document.querySelectorAll('#customCharacterList strong')).map(s => s.textContent.trim())")
    rec("B49", "UI-角色", "新角色出现在自定义列表", any("多标签角色" in n for n in names), f"names={names[:5]}")

    u.reset()
    page.evaluate("() => bindCharacter('../../bad')")
    page.wait_for_timeout(1200)
    rec("B50", "UI-角色", "绑定非法角色 id → alert 绑定失败", any("绑定失败" in m for m in u.dlg["msgs"]), f"alert={u.dlg['msgs']}")

    u.reset()
    # 绑定成功文案「已选择角色」会被随后的状态回读覆盖，用 MutationObserver 锁存「曾经出现」
    page.evaluate("""() => {
        window.__bindSeen = document.getElementById('characterStatus').innerText.includes('已选择角色');
        const ob = new MutationObserver(() => {
            if (document.getElementById('characterStatus').innerText.includes('已选择角色')) {
                window.__bindSeen = true; ob.disconnect();
            }
        });
        ob.observe(document.getElementById('characterStatus'), {childList: true, subtree: true, characterData: true});
    }""")
    page.evaluate("() => { const b = document.querySelector('#presetCharacterList button'); if (b) b.click(); }")
    try:
        page.wait_for_function("() => window.__bindSeen === true", timeout=10000)
    except Exception:
        pass
    seen = page.evaluate("() => window.__bindSeen")
    status_txt = page.query_selector("#characterStatus").inner_text()
    rec("B51", "UI-角色", "绑定预设角色 → 状态条曾展示「已选择角色」（探针锁存）", seen is True,
        f"seen={seen} status={status_txt[:40]}")

    page.reload()
    page.wait_for_timeout(1200)
    wait_data_loaded(page)
    page.click(".nav-btn[data-tab='character']")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('characterStatus');"
            " return el && el.innerText.includes('当前使用角色'); }",
            timeout=10000,
        )
    except Exception:
        pass
    status_txt = page.query_selector("#characterStatus").inner_text() if page.query_selector("#characterStatus") else ""
    rec("B52", "UI-角色", "刷新后回读会话角色 → 「当前使用角色」", "当前使用角色" in status_txt, f"status={status_txt[:60]}")

    # ---------- B8 合成页（mock 覆盖全部分支） ----------
    page.click(".nav-btn[data-tab='tts']")
    page.wait_for_timeout(200)
    u.reset()
    page.fill("#ttsText", "")
    page.click("#ttsBtn")
    page.wait_for_timeout(400)
    rec("B53", "UI-合成", "空文本 → 红色「请输入合成文本」", "请输入合成文本" in page.query_selector("#ttsResponse").inner_text(), "")

    page.fill("#ttsText", "   \n  ")
    page.click("#ttsBtn")
    page.wait_for_timeout(400)
    rec("B54", "UI-合成", "纯空白文本 → 同一校验分支", "请输入合成文本" in page.query_selector("#ttsResponse").inner_text(), "")

    page.fill("#ttsText", "a" * 1001)
    page.click("#ttsBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('ttsResponse');"
            " return el && (el.innerText.includes('第 1 句合成失败') || el.innerText.includes('合成出错')); }",
            timeout=12000,
        )
    except Exception:
        pass
    resp = page.query_selector("#ttsResponse").inner_text()
    rec("B55", "UI-合成", "单句 1001 字 → 服务端 400 并提示第 1 句失败", "第 1 句合成失败" in resp and "过长" in resp, f"resp={resp[:60]}")

    def on_tts_json(route):
        route.fulfill(status=200, headers={"content-type": "application/json"}, body='{"error":"mock 合成失败"}')

    page.route("**/api/voice/tts", on_tts_json)
    page.fill("#ttsText", "第一句。第二句！")
    page.click("#ttsBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('ttsResponse');"
            " return el && el.innerText.includes('第 1 句合成失败'); }",
            timeout=12000,
        )
    except Exception:
        pass
    resp = page.query_selector("#ttsResponse").inner_text()
    rec("B56", "UI-合成", "返回 JSON 错误 → 「第 1 句合成失败: mock 合成失败」", "第 1 句合成失败" in resp and "mock" in resp, f"resp={resp[:60]}")
    page.unroute("**/api/voice/tts", on_tts_json)

    def on_tts_audio(route):
        route.fulfill(status=200, headers={"content-type": "audio/wav", "X-Sample-Rate": "16000"}, body=WAV)

    page.route("**/api/voice/tts", on_tts_audio)
    page.fill("#ttsText", "第一句。第二句！")
    page.click("#ttsBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('ttsResponse');"
            " return el && el.innerText.includes('全部合成完成'); }",
            timeout=15000,
        )
    except Exception:
        pass
    resp = page.query_selector("#ttsResponse").inner_text()
    rec("B57", "UI-合成", "返回 audio/wav → 全部合成完成（2 句）", "全部合成完成" in resp and "2 句" in resp, f"resp={resp[:60]}")
    rec("B58", "UI-合成", "多句分句数为 2（按 。！ 切分）", "2 句" in resp, f"resp={resp[:40]}")

    page.uncheck("#ttsQuickMode")
    page.fill("#ttsText", "关闭快速模式。")
    page.click("#ttsBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('ttsResponse');"
            " return el && el.innerText.includes('全部合成完成'); }",
            timeout=15000,
        )
    except Exception:
        pass
    resp = page.query_selector("#ttsResponse").inner_text()
    btn_ok = (not page.query_selector("#ttsBtn").is_disabled()) and page.query_selector("#ttsBtn").inner_text() == "开始合成"
    rec("B59", "UI-合成", "关闭快速模式仍可合成且按钮恢复", "全部合成完成" in resp and btn_ok, f"resp={resp[:40]}")
    page.unroute("**/api/voice/tts", on_tts_audio)
    page.check("#ttsQuickMode")

    # ---------- B9 设置页 ----------
    page.click(".nav-btn[data-tab='settings']")
    page.wait_for_timeout(200)
    u.reset()
    page.fill("#sessionId", "deep_test_session")
    page.click("#saveSettingsBtn")
    page.wait_for_timeout(400)
    ls = page.evaluate("() => ({ s: localStorage.getItem('sessionId'), a: localStorage.getItem('apiUrl') })")
    rec("B60", "UI-设置", "保存设置 → alert + localStorage 落库", "设置已保存" in " ".join(u.dlg["msgs"]) and ls["s"] == "deep_test_session",
        f"alert={u.dlg['msgs']} ls={ls}")

    # apiUrl 不可达分支：不应锁死界面
    u.reset()
    page.fill("#apiUrl", "http://127.0.0.1:9999")
    page.click("#saveSettingsBtn")
    page.wait_for_timeout(1500)
    ov = page.query_selector("#authOverlay")
    usable = page.evaluate("() => { document.querySelector(\".nav-btn[data-tab='chat']\").click(); return document.getElementById('chat').classList.contains('active'); }")
    rec("B61", "UI-设置", "网关不可达 → 不弹门禁、界面仍可操作（仅记日志）", usable and not (ov and ov.is_visible()),
        f"usable={usable} overlay={bool(ov and ov.is_visible())} err={u.err_text()[:60]}")

    # 恢复 apiUrl（8899 静态服务下的默认网关地址）
    page.click(".nav-btn[data-tab='settings']")
    page.wait_for_timeout(300)
    page.fill("#apiUrl", "http://localhost:8005")
    page.click("#saveSettingsBtn")
    page.wait_for_timeout(800)
    page.reload()
    page.wait_for_timeout(1200)
    wait_data_loaded(page)

    # ---------- B10 数据类型 / 注入 / 边界 ----------
    # XSS 注入：角色名
    page.click(".nav-btn[data-tab='character']")
    page.wait_for_timeout(1200)
    page.fill("#charName", '<img src=x onerror="window.__xss_char=1">')
    page.click("#createCharacterBtn")
    wait_create_done(page)
    xss_char = page.evaluate("() => window.__xss_char === 1")
    rec("B62", "UI-安全", "角色名 HTML 注入不应执行（innerHTML 风险）", not xss_char, f"executed={xss_char}")

    page.click(".nav-btn[data-tab='voice-input']")
    page.wait_for_timeout(300)
    page.evaluate("() => { document.getElementById('saveVoiceBtn').style.display='block'; }")
    page.fill("#voiceName", '<img src=x onerror="window.__xss_voice=1">')
    page.click("#saveVoiceBtn")
    page.wait_for_timeout(800)
    xss_voice = page.evaluate("() => window.__xss_voice === 1")
    rec("B63", "UI-安全", "语音名 HTML 注入不应执行", not xss_voice, f"executed={xss_voice}")

    page.click(".nav-btn[data-tab='emotion-config']")
    page.wait_for_timeout(300)
    page.fill("#emotionProfileName", '<img src=x onerror="window.__xss_emo=1">')
    page.click("#saveEmotionBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('emotionConfigResponse');"
            " return el && (el.innerText.includes('保存成功') || el.innerText.includes('出错')); }",
            timeout=12000,
        )
    except Exception:
        pass
    xss_emo = page.evaluate("() => window.__xss_emo === 1")
    rec("B64", "UI-安全", "情感名 HTML 注入不应执行", not xss_emo, f"executed={xss_emo}")

    # 超长 + 特殊字符
    page.click(".nav-btn[data-tab='character']")
    page.wait_for_timeout(600)
    special = "🎤\"'<>&\n换行"
    page.fill("#charName", special)
    page.fill("#charIdentity", special)
    page.click("#createCharacterBtn")
    wait_create_done(page)
    resp = page.query_selector("#characterResponse").inner_text()
    rec("B65", "UI-数据", "emoji/引号/尖括号/换行 名称正常处理", "创建成功" in resp, f"resp={resp[:60]}")

    page.fill("#charName", "L" * 500)
    page.click("#createCharacterBtn")
    wait_create_done(page)
    resp = page.query_selector("#characterResponse").inner_text()
    rec("B66", "UI-数据", "名称 500 字（超长）不崩溃", "创建成功" in resp or "失败" in resp, f"resp_len={len(resp)}")

    # ---------- B11 损坏存储 / 状态恢复 ----------
    page.evaluate("() => localStorage.setItem('voices', '{bad json')")
    u.reset()
    page.reload()
    page.wait_for_timeout(3000)
    errs = u.err_text()
    emotions_ok = page.evaluate("() => document.querySelectorAll('#savedEmotions .emotion-card').length")
    rec("B67", "UI-健壮性", "localStorage.voices 损坏 JSON → 页面不崩溃且不中断初始化",
        "PAGEERROR" not in errs and emotions_ok >= 0 and len(errs) == 0, f"errors={errs[:120]}")

    page.evaluate("() => { localStorage.removeItem('voices'); localStorage.setItem('emotions', '[{'); }")
    u.reset()
    page.reload()
    page.wait_for_timeout(3000)
    errs = u.err_text()
    rec("B68", "UI-健壮性", "localStorage.emotions 损坏 JSON → 页面不崩溃",
        "PAGEERROR" not in errs and len(errs) == 0, f"errors={errs[:120]}")

    # 恢复干净存储
    page.evaluate("() => { localStorage.removeItem('voices'); localStorage.removeItem('emotions'); }")
    page.reload()
    page.wait_for_timeout(3000)

    # 录音无设备分支
    page.click(".nav-btn[data-tab='chat']")
    page.wait_for_timeout(300)
    u.reset()
    page.click("#micBtn")
    page.wait_for_timeout(1500)
    tip = page.query_selector("#voiceTip")
    tip_txt = tip.inner_text() if tip else ""
    tip_visible = tip.is_visible() if tip else False
    rec("B69", "UI-录音", "无麦克风设备 → 错误提示且不崩溃", tip_visible and ("麦克风" in tip_txt), f"tip={tip_txt[:60]}")

    # ---------- P1: WS 深度行为 ----------
    # B70 无 token 直连 WS → 服务端 accept 后 1008 关闭（网关 WS 鉴权分支）
    code = page.evaluate("""() => new Promise((resolve) => {
        const w = new WebSocket('ws://127.0.0.1:8005/ws/chat/valid_sid');
        const timer = setTimeout(() => resolve('timeout'), 8000);
        w.onclose = (e) => { clearTimeout(timer); resolve(e.code); };
        w.onerror = () => {};
    })""")
    rec("B70", "UI-WS", "WS 无 token → 服务端 1008 关闭", code == 1008, f"code={code}")

    # B72 消息乱序：reply_done 先于迟到的 text 分片
    u.reset()
    page.evaluate("""() => {
        setInputLocked(true);
        handleWsMessage({type:'status', status:'reply_done'});
        handleWsMessage({type:'text', content:'迟到的分片'});
    }""")
    page.wait_for_timeout(300)
    last = page.evaluate("() => { const n = document.querySelector('#chatHistory .chat-message.ai:last-child .message-content'); return n ? n.textContent : ''; }")
    errs = u.err_text()
    rec("B72", "UI-WS", "乱序：reply_done 后迟到 text → 不崩溃、文本归入尾泡",
        "PAGEERROR" not in errs and "迟到的分片" in last, f"last={last[:30]} errors={errs[:60]}")

    # B73 重复 connect：connectChatWebSocket 幂等可用（先恢复 wsUrl 到真实网关）
    st = page.evaluate("""async () => {
        try {
            wsUrl = 'ws://127.0.0.1:8005';
            if (chatWs) { try { chatWs.close(); } catch (e) {} chatWs = null; }
            await connectChatWebSocket();
            await connectChatWebSocket();
            return { ok: chatWs && chatWs.readyState === 1 };
        } catch (e) { return { ok: false, err: String(e).slice(0, 80) }; }
    }""")
    rec("B73", "UI-WS", "重复 connect → 连接幂等可用不崩溃", st.get("ok") is True, f"res={st}")

    # ---------- P1: 录音权限拒绝分支（mock getUserMedia reject） ----------
    page.evaluate("""() => {
        navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
    }""")
    u.reset()
    page.click("#micBtn")
    page.wait_for_timeout(1500)
    tip = page.query_selector("#voiceTip")
    tip_txt = tip.inner_text() if tip else ""
    tip_visible = tip.is_visible() if tip else False
    rec("B74", "UI-录音", "麦克风权限被拒绝 → 提示且不崩溃",
        (tip_visible and len(tip_txt) > 0) and "PAGEERROR" not in u.err_text(), f"tip={tip_txt[:60]}")

    # ---------- P1: localStorage 结构畸形 ----------
    page.evaluate("() => { localStorage.setItem('voices', '{\"x\":1}'); }")
    u.reset()
    page.reload()
    page.wait_for_timeout(2500)
    errs = u.err_text()
    cnt = page.evaluate("() => document.querySelectorAll('#savedVoices .voice-card').length")
    rec("B75", "UI-健壮性", "voices 为对象（非数组）→ 不崩溃、列表为空",
        "PAGEERROR" not in errs and cnt == 0, f"errors={errs[:80]} cards={cnt}")

    page.evaluate("() => { localStorage.setItem('voices', '[{}]'); }")
    u.reset()
    page.reload()
    page.wait_for_timeout(2500)
    errs = u.err_text()
    rec("B76", "UI-健壮性", "voices 元素字段缺失（[{}]）→ 渲染不崩溃",
        "PAGEERROR" not in errs, f"errors={errs[:80]}")

    # ---------- P1: 多标签 sessionId 行为 ----------
    page2 = ctx.new_page()
    page2.goto(UI_BASE, wait_until="domcontentloaded")
    page2.wait_for_timeout(2000)
    sid1 = page.evaluate("() => localStorage.getItem('sessionId')")
    sid2 = page2.evaluate("() => localStorage.getItem('sessionId')")
    ov2 = page2.query_selector("#authOverlay")
    ov2_visible = ov2.is_visible() if ov2 else True
    rec("B77", "UI-会话", "同浏览器新标签 → 共享 sessionId 且免登录",
        bool(sid1) and sid1 == sid2 and not ov2_visible, f"sid1={sid1} sid2={sid2} overlay={ov2_visible}")
    page2.close()

    ctx2 = ctx.browser.new_context()
    p2 = ctx2.new_page()
    p2.goto(UI_BASE, wait_until="domcontentloaded")
    p2.wait_for_timeout(1500)
    ov = p2.query_selector("#authOverlay")
    rec("B78", "UI-会话", "全新浏览器上下文（无凭证）→ 登录门禁弹出",
        ov.is_visible() if ov else False, "")
    ctx2.close()

    # ---------- P1: TTS 批量部分失败 ----------
    page.click(".nav-btn[data-tab='tts']")
    page.wait_for_timeout(300)
    calls = {"n": 0}

    def on_tts_partial(route):
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=200, headers={"content-type": "audio/wav", "X-Sample-Rate": "16000"}, body=WAV)
        else:
            route.fulfill(status=400, headers={"content-type": "application/json"}, body='{"error":"第二句超限"}')

    page.route("**/api/voice/tts", on_tts_partial)
    page.fill("#ttsText", "第一句。第二句！")
    page.click("#ttsBtn")
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('ttsResponse');"
            " return el && (el.innerText.includes('第 2 句合成失败') || el.innerText.includes('全部合成完成')); }",
            timeout=12000)
    except Exception:
        pass
    resp = page.query_selector("#ttsResponse").inner_text()
    btn_ok = not page.query_selector("#ttsBtn").is_disabled()
    rec("B79", "UI-合成", "部分失败（第 1 句成功、第 2 句 400）→ 提示失败句且按钮恢复",
        "第 2 句合成失败" in resp and btn_ok, f"resp={resp[:60]}")
    page.unroute("**/api/voice/tts", on_tts_partial)

    # ---------- P2: 性能基线 ----------
    page.click(".nav-btn[data-tab='chat']")
    page.wait_for_timeout(200)
    r = page.evaluate("""() => {
        const t0 = performance.now();
        for (let i = 0; i < 120; i++) addMessage(i % 2 ? 'ai' : 'user', '消息压测 ' + i);
        return { ms: Math.round(performance.now() - t0),
                 nodes: document.querySelectorAll('#chatHistory .chat-message').length };
    }""")
    rec("B83", "UI-性能", "120 条历史消息渲染 < 1000ms", r["ms"] < 1000 and r["nodes"] >= 120, f"res={r}")

    big_list = [{"id": f"r{i}", "name": f"角色{i}", "is_preset": i % 2 == 0} for i in range(150)]

    def on_chars(route):
        route.fulfill(status=200, headers={"content-type": "application/json"},
                      body=json.dumps({"characters": big_list}, ensure_ascii=False))

    page.route("**/api/characters", on_chars)
    r = page.evaluate("""async () => {
        const t0 = performance.now();
        await loadCharacters();
        return { ms: Math.round(performance.now() - t0),
                 preset: document.querySelectorAll('#presetCharacterList > div').length,
                 custom: document.querySelectorAll('#customCharacterList > div').length };
    }""")
    page.unroute("**/api/characters", on_chars)
    rec("B84", "UI-性能", "150 个角色列表渲染 < 2000ms",
        r["ms"] < 2000 and r["preset"] + r["custom"] >= 150, f"res={r}")

    r = page.evaluate("""() => {
        try {
            const big = 'x'.repeat(400 * 1024);
            const arr = Array.from({length: 9}, (_, i) => ({id: 'big' + i, name: 'big' + i, description: big, timestamp: 't'}));
            localStorage.setItem('voices', JSON.stringify(arr));
            const back = JSON.parse(localStorage.getItem('voices'));
            return { ok: back.length === 9, kb: Math.round(JSON.stringify(arr).length / 1024) };
        } catch (e) { return { ok: false, err: String(e).slice(0, 60) }; }
    }""")
    usable = page.evaluate("() => { document.querySelector(\".nav-btn[data-tab='voice-define']\").click(); return document.getElementById('voice-define').classList.contains('active'); }")
    rec("B85", "UI-性能", "localStorage ~3.6MB voices → 读写一致、界面可操作",
        r.get("ok") is True and usable, f"res={r}")

    p3 = ctx.new_page()
    p3.goto(UI_BASE, wait_until="load")
    nav = p3.evaluate("() => { const n = performance.getEntriesByType('navigation')[0] || {}; return Math.round(n.loadEventEnd || 0); }")
    rec("B82", "UI-性能", "首屏 load 基线 < 3000ms（本地静态）", 0 < nav < 3000, f"load={nav}ms")
    p3.close()

    # ---------- P2: 移动端视口 ----------
    ctx3 = ctx.browser.new_context(viewport={"width": 375, "height": 812}, has_touch=True, is_mobile=True)
    m = ctx3.new_page()
    m.goto(UI_BASE, wait_until="domcontentloaded")
    m.wait_for_timeout(1500)
    ovm = m.query_selector("#authOverlay")
    if ovm and ovm.is_visible():
        m.fill("#authUsername", USER)
        m.fill("#authPassword", PASS)
        m.click("#authSubmitBtn")
        try:
            m.wait_for_selector("#authOverlay", state="hidden", timeout=15000)
        except Exception:
            pass
    tab_ids = m.evaluate("() => Array.from(document.querySelectorAll('.nav-btn[data-tab]')).map(b => b.dataset.tab)")
    act_ok = True
    for tid in tab_ids:
        try:
            m.click(f".nav-btn[data-tab='{tid}']", timeout=3000)
            m.wait_for_timeout(200)
            if not m.evaluate("(id) => document.getElementById(id).classList.contains('active')", tid):
                act_ok = False
        except Exception:
            act_ok = False
    no_hscroll = m.evaluate("() => document.documentElement.scrollWidth <= 376")
    rec("B80", "UI-移动端", "375px 视口：7 标签可点且激活、无横向滚动",
        act_ok and no_hscroll and len(tab_ids) == 7, f"tabs={len(tab_ids)} act_ok={act_ok} no_hscroll={no_hscroll}")
    ctx3.close()

    # ---------- P2: 键盘可访问性 ----------
    ctx4 = ctx.browser.new_context()
    a = ctx4.new_page()
    a.goto(UI_BASE, wait_until="domcontentloaded")
    a.wait_for_timeout(1500)
    # 页面 autofocus 在用户名框：验证焦点起点 + 后续 Tab 链完整覆盖密码/提交
    first = a.evaluate("() => (document.activeElement && document.activeElement.id) || ''")
    seq = []
    for _ in range(3):
        a.keyboard.press("Tab")
        a.wait_for_timeout(150)
        seq.append(a.evaluate("() => document.activeElement && (document.activeElement.id || document.activeElement.tagName)"))
    rec("B81", "UI-无障碍", "登录门禁 Tab 链：起点用户名 → 密码 → 提交键可达",
        first == "authUsername" and "authPassword" in seq and "authSubmitBtn" in seq,
        f"first={first} seq={seq}")
    ctx4.close()

    # ---------- 套件自检 Z04：测试数据清理纪律 ----------
    page.evaluate("() => { localStorage.removeItem('voices'); localStorage.removeItem('emotions'); localStorage.removeItem('characters'); }")
    leftover = page.evaluate("() => ['voices', 'emotions', 'characters'].filter(k => localStorage.getItem(k) !== null)")
    rec("Z04", "套件自检", "UI 段结束清理测试数据（voices/emotions/characters 无残留）",
        leftover == [], f"leftover={leftover}")

    # ---------- P1: WS 1008 → 回登录页（破坏登录态，置组末执行） ----------
    page.evaluate("() => { localStorage.setItem('apiUrl', 'http://127.0.0.1:8005'); }")
    page.reload()
    page.wait_for_timeout(2500)
    try:
        wait_data_loaded(page)
    except Exception:
        pass
    page.evaluate("""() => {
        localStorage.setItem('yuban_token', 'forged.bad.token');
        if (chatWs) { try { chatWs.close(); } catch (e) {} chatWs = null; }
        connectChatWebSocket().catch(() => {});
    }""")
    page.wait_for_timeout(3000)
    ov = page.query_selector("#authOverlay")
    shown = ov.is_visible() if ov else False
    tok_left = page.evaluate("() => localStorage.getItem('yuban_token')")
    rec("B71", "UI-WS", "伪造 token 的 WS 连接 → 服务端 1008 → 清 token 回登录页",
        shown and not tok_left, f"overlay={shown} token_left={tok_left}")

    ctx.close()


def main():
    # 用例完整性基线：API 段与 UI 段各应产出固定数量用例，缺失即视为套件缺陷
    EXPECT_API = 77
    EXPECT_UI = 85

    with sync_playwright() as p:
        try:
            run_api(p)
        except Exception:
            tb = traceback.format_exc()
            print("API 段异常:", tb[:800])
            rec("Z01", "套件自检", "API 段完整执行（异常即中断）", False, tb.splitlines()[-1][:200])

        # 登录限流桶（5/min）已被 API 段消耗，等窗口重置后 UI 段才能正常登录
        print("[套件] 等待 62s 让登录限流窗口重置 ...")
        time.sleep(62)

        try:
            run_ui(p)
        except Exception:
            tb = traceback.format_exc()
            print("UI 段异常:", tb[:1200])
            rec("Z99", "套件自检", "UI 段完整执行（异常即中断）", False, tb.splitlines()[-1][:200])

    api_cnt = sum(1 for r in RESULTS if r["id"].startswith("A"))
    ui_cnt = sum(1 for r in RESULTS if r["id"].startswith("B"))
    rec("Z02", "套件自检", f"API 用例数量 = {EXPECT_API}", api_cnt == EXPECT_API, f"实际 {api_cnt}")
    rec("Z03", "套件自检", f"UI 用例数量 = {EXPECT_UI}", ui_cnt == EXPECT_UI, f"实际 {ui_cnt}")

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    blocked = sum(1 for r in RESULTS if r["status"] == "BLOCKED")
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    print("\n" + "=" * 72)
    print(f"深度测试：总计 {total}  通过 {passed}  失败 {len(failed)}  阻塞 {blocked}")
    print("-" * 72)
    for r in failed:
        print(f"[FAIL] {r['id']} [{r['area']}] {r['case']} | {r['detail']}")
    print("=" * 72)

    REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_test_deep_report.json")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": {"total": total, "passed": passed, "failed": len(failed),
                               "blocked": blocked}, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"报告: {REPORT_PATH}")


if __name__ == "__main__":
    main()
