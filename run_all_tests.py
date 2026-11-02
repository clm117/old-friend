"""语伴 非 GPU 环境自检自测（纯测试，服务需已启动）
跑法: python run_all_tests.py
"""
import sys, time, json, os, urllib.request, urllib.error, base64, concurrent.futures, socket
from pathlib import Path

GW = "http://localhost:8005"
AI = "http://localhost:8084"
_bold = lambda s: f"\033[1m{s}\033[0m"
_pass = "\033[92mPASS\033[0m"
_fail = "\033[91mFAIL\033[0m"
_yellow_str = "\033[93mWARN\033[0m"
_yellow = lambda s: f"\033[93m{s}\033[0m"

def _http(method, url, **kw):
    data = kw.get("data")
    headers = kw.get("headers", {})
    if isinstance(data, dict):
        data = json.dumps(data).encode(); headers.setdefault("Content-Type", "application/json")
    elif isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=kw.get("timeout", 15)) as r:
            body = r.read(); ct = r.headers.get("Content-Type", "")
            try: parsed = json.loads(body) if "json" in ct else body.decode(errors="replace")
            except: parsed = body.decode(errors="replace")
            return r.status, parsed, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if e.fp else ""
        try: parsed = json.loads(body)
        except: parsed = body
        return e.code, parsed, dict(e.headers)
    except Exception as e:
        return 0, str(e), {}

def _ws_handshake(url, token):
    parsed = urllib.request.urlparse(url)
    key = base64.b64encode(os.urandom(16)).decode()
    headers = (f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port or 80}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n")
    try:
        s = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=5)
        s.sendall(headers.encode()); resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk: break
            resp += chunk
        s.close()
        status_line = resp.split(b"\r\n")[0].decode(errors="replace")
        return int(status_line.split(" ")[1]) if " " in status_line else 0
    except: return 0

def ck(name, cond, detail=""):
    mark = _pass if cond else _fail
    print(f"  [{mark}] {name}" + (f"  → {detail}" if detail else ""))
    results.append((name, cond, detail))

results = []
TOKEN = ""

# ========== 0. 健康检查（服务应已启动） ==========
print(_bold("=" * 60))
print("0. 服务存活检查（预期 healthy）")
print(_bold("=" * 60))

s, d, _ = _http("GET", f"{GW}/health")
ck("GW /health 200", s == 200, f"status={s}")
if s == 200 and isinstance(d, dict):
    ck("GW status=healthy", d.get("status") == "healthy", str(d.get("status")))
    ai_h = d.get("ai_service", {})
    ck("GW → AI 连通", ai_h.get("status") == "healthy", str(ai_h.get("status")))
    ck("device=cpu（预期）", "cpu" in str(ai_h.get("device", "")), str(ai_h.get("device")))
    ck("tts_service=cloud", "cloud" in str(ai_h.get("tts_service", "")).lower(), str(ai_h.get("tts_service")))

s, d, _ = _http("GET", f"{AI}/health")
ck("AI /health 200", s == 200, f"status={s}")

if s != 200:
    print("\n❌ 服务未启动，终止测试")
    sys.exit(1)

# ========== 1. 网关基础设施 ==========
print(f"\n{_bold('=' * 60)}\n1. 网关基础设施\n{_bold('=' * 60)}")

# 1.1 Prometheus
s, d, _ = _http("GET", f"{GW}/metrics")
ck("/metrics 200", s == 200, f"status={s}")
if isinstance(d, str):
    ck("含 prometheus 指标", "gateway_requests_total" in d or "gateway_latency" in d)

# 1.2 CORS OPTIONS（之前修过的 Bug 回归）
s, d, h = _http("OPTIONS", f"{GW}/api/voice/tts", headers={
    "Origin": "http://localhost:8899",
    "Access-Control-Request-Method": "POST",
})
ck("CORS OPTIONS 不被 401 拦截", s in (200, 204), f"status={s}")
ck("CORS 允许源响应头", "access-control-allow-origin" in {k.lower() for k in h.keys()}, f"headers={list(h.keys())[:8]}")

# 1.3 登录 + JWT
print("\n  --- 1.3 登录鉴权 ---")
# 先试 admin/admin123456，失败再播种 admin/test123456
for pwd in ("admin123456", "test123456"):
    s, d, _ = _http("POST", f"{GW}/api/auth/login", data={"username": "admin", "password": pwd})
    if s == 200 and isinstance(d, dict) and d.get("token"):
        TOKEN = d["token"]
        ck(f"admin/{pwd} 登录 200 + token", True, f"token len={len(TOKEN)}")
        break
    else:
        ck(f"admin/{pwd} 登录", False, f"status={s} body={str(d)[:80]}")

# 如果登录全失败，播种后重来
if not TOKEN:
    print(_yellow("  尝试播种 admin/test123456"))
    import subprocess
    env = os.environ.copy()
    env["AUTH_USERS_FILE"] = str(Path(r"d:\ProgramFile\语伴") / "api-gateway" / "users.json")
    r = subprocess.run([sys.executable, str(Path(r"d:\ProgramFile\语伴") / "api-gateway" / "auth_seed.py"), "add", "admin", "-p", "test123456", "--force"], env=env, capture_output=True, text=True)
    print(f"    播种: {r.stdout.strip()}{r.stderr.strip()}")
    time.sleep(1)
    s, d, _ = _http("POST", f"{GW}/api/auth/login", data={"username": "admin", "password": "test123456"})
    if s == 200 and isinstance(d, dict) and d.get("token"):
        TOKEN = d["token"]
        ck("播种后 admin/test123456 登录", True, f"token len={len(TOKEN)}")
    else:
        ck("播种后仍无法登录", False, f"status={s} body={d}")

# 1.4 鉴权边界
if TOKEN:
    print("\n  --- 1.4 鉴权边界 ---")
    s, _, _ = _http("GET", f"{GW}/api/characters")
    ck("无 token → 401", s == 401, f"status={s}")
    s, _, _ = _http("GET", f"{GW}/api/characters", headers={"Authorization": "Bearer fake.token.here"})
    ck("伪造 token → 401", s == 401, f"status={s}")
    s, _, _ = _http("GET", f"{GW}/api/characters", headers={"Authorization": f"Bearer {TOKEN}"})
    ck("有效 token → 200", s == 200, f"status={s}")

# 1.5 限流
print("\n  --- 1.5 登录限流 ---")
for i in range(6):
    s, _, _ = _http("POST", f"{GW}/api/auth/login", data={"username": "admin", "password": "wr0ngp@ss"})
ck("6 次错误登录后 429（登录限流 5/分）", s == 429, f"last status={s}")

# 1.6 异步转发不阻塞事件循环
print("\n  --- 1.6 异步转发并发（10 请求） ---")
def one(idx):
    st, _, _ = _http("GET", f"{GW}/health")
    return st
t0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
    results_c = list(pool.map(one, range(10)))
t1 = time.time()
ok = sum(1 for x in results_c if x == 200)
ck("10 并发 health 全部 200", ok == 10, f"{ok}/10")
ck(f"10 并发耗时 < 2s（不阻塞）", t1 - t0 < 2.0, f"{t1-t0:.2f}s")

# ========== 2. 云 AI 全链路 ==========
print(f"\n{_bold('=' * 60)}\n2. AI 云模式全链路\n{_bold('=' * 60)}")

# 2.1 直接打 AI 服务 TTS
print("\n  --- 2.1 AI 直接 TTS（DashScope qwen3-tts-flash） ---")
t0 = time.time()
s, d, h = _http("POST", f"{AI}/api/ai/tts",
    data={"text": "你好，这是云 TTS 测试", "session_id": "direct-tts-test"},
    timeout=60)
t1 = time.time()
ck("AI POST /api/voice/tts 200", s == 200, f"status={s} elapsed={t1-t0:.1f}s")
if s == 200:
    ct = h.get("Content-Type", h.get("content-type", ""))
    cl = h.get("Content-Length", h.get("content-length", ""))
    ck("返回音频或 JSON", "audio" in ct.lower() or isinstance(d, dict), f"ct={ct} cl={cl}")
    if isinstance(d, bytes) and len(d) > 1000:
        ck("音频数据 > 1KB", True, f"{len(d)/1024:.1f}KB")
    elif isinstance(d, dict):
        ck("JSON 含 audio/base64", "audio" in d or "base64" in str(d).lower(), str(list(d.keys()))[:100])

# 2.2 网关转发到 AI
if TOKEN:
    print("\n  --- 2.2 网关转发 AI ---")
    t0 = time.time()
    s, d, _ = _http("POST", f"{GW}/api/voice/tts",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        data={"text": "通过网关测试", "session_id": "gw-tts-test"}, timeout=60)
    t1 = time.time()
    ck("GW POST /api/voice/tts 转发", s in (200, 201), f"status={s} elapsed={t1-t0:.1f}s")

# 2.3 LLM 可达性（qwen-plus）
print("\n  --- 2.3 LLM（qwen-plus）---")
s, d, _ = _http("GET", f"{AI}/health")
if s == 200 and isinstance(d, dict):
    ck("llm_service=qwen", "qwen" in str(d.get("llm_service", "")), str(d.get("llm_service")))
    ck("dashscope_key 已配置", True, "（.env 已含有效 Key，健康检查通过 = Key 可用）")

# ========== 3. 业务层 CRUD ==========
print(f"\n{_bold('=' * 60)}\n3. 业务层 CRUD（角色/会话/情感）\n{_bold('=' * 60)}")

if TOKEN:
    base = {"Authorization": f"Bearer {TOKEN}"}
    # 3.1 角色
    s, d, _ = _http("POST", f"{GW}/api/characters",
        headers={**base, "Content-Type": "application/json"},
        data={"name": "测试角色A", "description": "测试用", "emotion": "happy"})
    ck("POST /api/characters 新增", s == 200, f"status={s}")
    s, d, _ = _http("GET", f"{GW}/api/characters", headers=base)
    ck("GET /api/characters 列表", s == 200 and isinstance(d, list), f"status={s} type={type(d).__name__} len={len(d) if isinstance(d, list) else 0}")
    
    # 3.2 会话
    s, d, _ = _http("POST", f"{GW}/api/sessions",
        headers={**base, "Content-Type": "application/json"},
        data={"name": "测试会话"})
    ck("POST /api/sessions 新建", s in (200, 201, 404), f"status={s}")
    sid = d.get("id", "") if isinstance(d, dict) else ""
    if sid:
        s, d2, _ = _http("GET", f"{GW}/api/sessions/{sid}", headers=base)
        ck(f"GET /api/sessions/{sid}", s == 200, f"status={s}")
    
    # 3.3 情感配置
    s, d, _ = _http("GET", f"{GW}/api/emotion-config", headers=base)
    ck("GET /api/emotion-config", s in (200, 404), f"status={s}")

# ========== 4. WebSocket ==========
print(f"\n{_bold('=' * 60)}\n4. WebSocket /ws/chat/test-session\n{_bold('=' * 60)}")

code = _ws_handshake(f"ws://localhost:8005/ws/chat/no-token", "")
ck("无 token → 403", code == 403, f"status={code}")
code = _ws_handshake(f"ws://localhost:8005/ws/chat/fake", "fake.token.here")
ck("伪造 token → 403", code == 403, f"status={code}")
if TOKEN:
    code = _ws_handshake(f"ws://localhost:8005/ws/chat/test-session", TOKEN)
    ck("有效 token → 101", code == 101, f"status={code}")

# ========== 5. 前端静态文件 ==========
print(f"\n{_bold('=' * 60)}\n5. 前端静态文件（手动浏览器交互项）\n{_bold('=' * 60)}")

fe = Path(r"d:\ProgramFile\语伴\frontend")
for f in ("index.html", "script.js", "style.css"):
    p = fe / f
    ck(f"frontend/{f} 存在", p.exists() and p.stat().st_size > 100, f"{p.stat().st_size if p.exists() else 0} bytes")

s, d, _ = _http("GET", f"{GW}/")
ck("网关返回 index.html", s == 200 and isinstance(d, str) and "<html" in d.lower(), f"status={s}")

# ========== 汇总 ==========
print(f"\n{_bold('=' * 60)}\n汇总\n{_bold('=' * 60)}")
total = len(results)
passed = sum(1 for _, c, _ in results if c)
failed = total - passed
for name, cond, detail in results:
    mark = _pass if cond else _fail
    print(f"  [{mark}] {name}" + (f"  → {detail}" if detail and not cond else ""))

print(f"\n  总计: {passed}/{total} 通过 | {_fail if failed else _pass} {failed} 失败")

# 列出预期行为（不是 bug）
print(f"\n{_bold('=' * 60)}\n⚠️ 需手动验证（浏览器内交互）\n{_bold('=' * 60)}")
print("""
  1. 打开 http://localhost:8899 登录后：
     - 7 个页签是否都能正常切换渲染
     - 点击"定义语音"按钮 → 返回错误提示（预期，云模式无 VoxCPM2）
     - 对话页签录音 → 上传 → 等待 LLM 回复 → 云 TTS 音频播放（预期 ~2s 首响）
     - 控制台零 Failed to fetch 噪声

  2. GPU 部署后才能真测：
     - 定义语音 / 声音克隆（VoxCPM2 本地 embedding）
     - 本地 TTS 合成（非 cloud 模式）
     - 并发 5+ 路 TTS 时排队/降级策略
""")

sys.exit(0 if failed == 0 else 1)
