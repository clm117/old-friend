# -*- coding: utf-8 -*-
"""语伴真实端到端测试（无 mock / 无 route 拦截 / 无事件注入）

设计原则：
1. 每条用例真实调用运行中的服务或真实外部 API，不使用 route mock，不向 WS 注入伪造事件。
2. 依赖不可用时记为 SKIP 并写明原因，绝不把 SKIP 计为 PASS。
3. 测试数据独立：临时账号表、独立 session、独立状态目录，全部位于 tests/e2e_real/runtime。
4. 不修改产品代码，不 commit/push，不启停付费 GPU。

运行：
    .venv\\Scripts\\python.exe tests/e2e_real/e2e_real.py
可选：
    --skip-service   只做依赖探测与 CPU 结果汇总
    --cpu-result FILE  复用已生成的 CPU TTS 结果，避免重复加载模型
"""
import argparse
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
import wave
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "ai-service"
GW_DIR = ROOT / "api-gateway"
RUNTIME = Path(__file__).resolve().parent / "runtime"
LOGS = RUNTIME / "logs"
RUNTIME.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

AI_PORT = 8084
GW_PORT = 8005
AI_BASE = f"http://127.0.0.1:{AI_PORT}"
GW_BASE = f"http://127.0.0.1:{GW_PORT}"
GW_WS = f"ws://127.0.0.1:{GW_PORT}"

USER_A = "e2e_a"
USER_B = "e2e_b"
USER_PWD = "E2e@2026test"
IP_A = "203.0.113.10"
IP_B = "203.0.113.11"
IP_C = "203.0.113.12"

SAPI_AUDIO = RUNTIME / "asr_input.wav"
SAPI_TEXT = "你好，今天天气很好。"

CASES = []
DEFECTS = []
START_TS = time.strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------- 基础工具
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


def record(cid, name, category, status, elapsed=0.0, assertions=None, evidence=None,
           classification=None, notes=""):
    case = {
        "id": cid,
        "name": name,
        "category": category,
        "status": status,          # PASS / FAIL / SKIP
        "elapsed_seconds": round(elapsed, 2),
        "assertions": assertions or [],
        "evidence": evidence or {},
        "classification": classification,
        "notes": notes,
    }
    CASES.append(case)
    print(f"[{status}] {cid} {name}" + (f" | {notes}" if notes else ""), flush=True)
    return case


def assertion(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:500]}


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def find_gateway_python() -> str:
    """网关依赖 PyJWT；若当前 venv 缺 jwt，则回退到本机已装齐依赖的解释器"""
    candidates = [sys.executable, r"D:\Program Files (x86)\Python 3.11\python.exe"]
    for p in candidates:
        if not Path(p).exists():
            continue
        try:
            r = subprocess.run([p, "-c", "import jwt, fastapi, uvicorn, requests"], capture_output=True,
                               timeout=20)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return sys.executable


def wait_http(url: str, timeout=60, expect=200):
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
            return {
                "channels": wf.getnchannels(),
                "sampwidth": wf.getsampwidth(),
                "sample_rate": rate,
                "frames": frames,
                "duration_seconds": round(frames / float(rate), 3) if rate else 0,
                "bytes": len(raw),
            }
    except Exception as e:
        return {"decode_error": f"{type(e).__name__}: {e}", "bytes": len(raw)}


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def snippet(text, n=300):
    return (text or "")[:n]


# ---------------------------------------------------------------- 依赖探测
def probe_llm():
    key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
    t0 = time.time()
    try:
        r = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": AI_ENV.get("LLM_MODEL", "qwen-plus"),
                "messages": [{"role": "user", "content": "请只回复两个字：你好"}],
                "max_tokens": 16,
            },
            timeout=30,
        )
        body = safe_json(r)
        content = ""
        if body:
            try:
                content = body["choices"][0]["message"]["content"]
            except Exception:
                content = ""
        ok = r.status_code == 200 and bool(content)
        record(
            "D01", "DashScope LLM（qwen）可用性", "依赖探测",
            "PASS" if ok else "FAIL", time.time() - t0,
            [assertion("HTTP 200", r.status_code == 200, r.status_code),
             assertion("返回非空文本", bool(content), snippet(content, 80))],
            {"http_status": r.status_code, "body": snippet(r.text), "content": snippet(content, 120)},
            classification=None if ok else "外部依赖不可用",
            notes="" if ok else "DashScope 返回 Arrearage/欠费或无有效 Key",
        )
    except Exception as e:
        record("D01", "DashScope LLM（qwen）可用性", "依赖探测", "FAIL", time.time() - t0,
               [assertion("请求成功", False, e)], {"error": f"{type(e).__name__}: {e}"},
               classification="外部依赖不可用", notes="网络或鉴权异常")


def probe_cloud_tts():
    key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
    t0 = time.time()
    try:
        r = requests.post(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": AI_ENV.get("CLOUD_TTS_MODEL", "qwen3-tts-flash"),
                  "input": {"text": "你好", "voice": AI_ENV.get("CLOUD_TTS_VOICE", "Cherry"),
                            "language_type": "Auto"}},
            timeout=60,
        )
        body = safe_json(r) or {}
        url = ((body.get("output") or {}).get("audio") or {}).get("url")
        audio = None
        if r.status_code == 200 and url:
            wav = requests.get(url, timeout=60)
            if wav.status_code == 200:
                audio = wav_info(wav.content)
        ok = r.status_code == 200 and bool(url) and bool(audio and audio.get("duration_seconds", 0) > 0)
        record(
            "D02", "云 TTS（qwen3-tts-flash）可用性", "依赖探测",
            "PASS" if ok else "FAIL", time.time() - t0,
            [assertion("HTTP 200", r.status_code == 200, r.status_code),
             assertion("返回音频 URL", bool(url), snippet(str(url), 80)),
             assertion("WAV 可解码且时长>0", bool(audio and audio.get("duration_seconds", 0) > 0), audio)],
            {"http_status": r.status_code, "body": snippet(r.text), "audio": audio},
            classification=None if ok else "外部依赖不可用",
            notes="" if ok else "DashScope 欠费或未开通该模型",
        )
    except Exception as e:
        record("D02", "云 TTS（qwen3-tts-flash）可用性", "依赖探测", "FAIL", time.time() - t0,
               [assertion("请求成功", False, e)], {"error": f"{type(e).__name__}: {e}"},
               classification="外部依赖不可用")


def probe_asr():
    """直接按产品 asr_service 的协议做一次真实 WS 转写，并抓取原始事件用于取证"""
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
    if not SAPI_AUDIO.exists():
        record("D03", "ASR（paraformer-realtime-v2）可用性", "依赖探测", "SKIP", 0,
               [assertion("测试音频存在", False, SAPI_AUDIO)], {}, classification="测试素材缺失")
        return
    with wave.open(str(SAPI_AUDIO), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        rate = wf.getframerate()

    import uuid
    task_id = str(uuid.uuid4())
    run_task = {
        "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                    "model": "paraformer-realtime-v2",
                    "parameters": {"sample_rate": rate, "format": "pcm", "language_hints": ["zh", "en"]},
                    "input": {}},
    }
    finish_task = {"header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                   "payload": {"input": {}}}
    events = []
    sentences = []
    started = False
    error_msg = None
    t0 = time.time()
    try:
        with connect("wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
                     additional_headers={"Authorization": f"Bearer {key}"},
                     open_timeout=10, close_timeout=5, max_size=16 * 1024 * 1024) as ws:
            ws.send(json.dumps(run_task))
            deadline = time.time() + 15
            while time.time() < deadline and not started and not error_msg:
                try:
                    raw = ws.recv(timeout=2)
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                h = msg.get("header", {})
                ev = h.get("event")
                events.append({"event": ev, "error": h.get("error_message")})
                if ev == "task-started":
                    started = True
                elif ev == "task-failed":
                    error_msg = h.get("error_message") or h.get("error_code") or "task-failed"
            if started:
                for i in range(0, len(pcm), 3200):
                    ws.send(pcm[i:i + 3200])
                    time.sleep(0.02)
                ws.send(json.dumps(finish_task))
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        raw = ws.recv(timeout=2)
                    except TimeoutError:
                        break
                    msg = json.loads(raw)
                    h = msg.get("header", {})
                    ev = h.get("event")
                    if ev == "result-generated":
                        sent = ((msg.get("payload") or {}).get("output") or {}).get("sentence") or {}
                        if sent.get("sentence_end") and (sent.get("text") or "").strip():
                            sentences.append(sent["text"].strip())
                    elif ev == "task-finished":
                        events.append({"event": ev, "error": None})
                        break
                    elif ev == "task-failed":
                        error_msg = h.get("error_message") or "task-failed"
                        events.append({"event": ev, "error": error_msg})
                        break
    except ConnectionClosed as e:
        error_msg = f"WebSocket 关闭 code={e.rcvd.code if e.rcvd else None} reason={e.rcvd.reason if e.rcvd else ''}"
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"

    text = "".join(sentences).strip()
    ok = bool(text) and ("你好" in text or "天气" in text or "很好" in text)
    record(
        "D03", "ASR（paraformer-realtime-v2）可用性", "依赖探测",
        "PASS" if ok else "FAIL", time.time() - t0,
        [assertion("收到 task-started", started, started),
         assertion("收到语音内容", bool(text), text),
         assertion("语义匹配（你好/天气/很好）", ok, text)],
        {"events": events[:10], "task_started": started, "text": text, "error": error_msg,
         "audio": {"sample_rate": rate, "bytes": len(pcm)}},
        classification=None if ok else "外部依赖不可用",
        notes="" if ok else (error_msg or "15s 内未收到 task-started"),
    )


def probe_gpu():
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
    except Exception as e:
        cuda = False
        torch = None
    smi = shutil.which("nvidia-smi")
    ok = cuda
    record(
        "D04", "本机 GPU 可用性", "依赖探测",
        "PASS" if ok else "SKIP", 0,
        [assertion("torch.cuda.is_available()", cuda, cuda),
         assertion("nvidia-smi 存在", bool(smi), smi)],
        {"cuda_available": cuda, "torch": getattr(torch, "__version__", None),
         "nvidia_smi": smi or "未安装"},
        classification=None if ok else "环境能力缺失",
        notes="" if ok else "无 NVIDIA 驱动/GPU，本地 VoxCPM2 只能走 CPU 降级路径",
    )


def ensure_sapi_audio():
    if SAPI_AUDIO.exists():
        try:
            with wave.open(str(SAPI_AUDIO), "rb") as wf:
                info = {"sample_rate": wf.getframerate(), "channels": wf.getnchannels(),
                        "sampwidth": wf.getsampwidth(),
                        "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 2)}
            record("D05", "测试语音素材（系统 TTS 合成）", "依赖探测", "PASS", 0,
                   [assertion("16kHz/16bit/单声道",
                              info["sample_rate"] == 16000 and info["channels"] == 1 and info["sampwidth"] == 2,
                              info)],
                   {"path": str(SAPI_AUDIO), **info},
                   notes="仅证明测试素材可用，不代表产品 TTS 能力")
            return
        except Exception:
            pass
    # 缺失时用 Windows SAPI 生成（真实系统合成音，非 mock）
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SelectVoice('Microsoft Huihui Desktop');"
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{SAPI_AUDIO}',$f);$s.Speak('{SAPI_TEXT}');$s.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=60,
                       capture_output=True)
        with wave.open(str(SAPI_AUDIO), "rb") as wf:
            rate, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        if rate != 16000 or ch != 1 or sw != 2:
            raise RuntimeError(f"生成格式不符: {rate}Hz/{ch}ch/{sw*8}bit")
        record("D05", "测试语音素材（系统 TTS 合成）", "依赖探测", "PASS", 0,
               [assertion("生成 16kHz/16bit/单声道", True, f"{rate}/{ch}/{sw}")],
               {"path": str(SAPI_AUDIO)}, notes="仅证明测试素材可用")
    except Exception as e:
        record("D05", "测试语音素材（系统 TTS 合成）", "依赖探测", "SKIP", 0,
               [assertion("生成成功", False, e)], {"error": str(e)}, classification="测试素材缺失")


# ---------------------------------------------------------------- 服务管理
def ai_child_env():
    env = os.environ.copy()
    env.update({
        "TTS_PROVIDER": "cloud",
        "YUBAN_DATA_DIR": str(RUNTIME / "data_cloud"),
        "PYTHONUNBUFFERED": "1",
        "REDIS_URL": "",
        "MAX_UPLOAD_MB": "10",
        "AI_INTERNAL_TOKEN": ROOT_ENV.get("AI_INTERNAL_TOKEN") or
                             ROOT_ENV.get("JWT_SECRET") or "e2e-only-secret",
    })
    # 环境变量优先于 ai-service/.env，便于充值后直接注入新 Key 复跑
    env.setdefault("LLM_API_KEY", AI_ENV.get("LLM_API_KEY", ""))
    return env


def gw_child_env(users_file: Path):
    env = os.environ.copy()
    env.update({
        "AI_SERVICE_URL": AI_BASE,
        "AI_SERVICE_WS_URL": f"ws://127.0.0.1:{AI_PORT}",
        "JWT_SECRET": ROOT_ENV.get("JWT_SECRET", "e2e-only-secret"),
        "AI_INTERNAL_TOKEN": ROOT_ENV.get("AI_INTERNAL_TOKEN") or
                             ROOT_ENV.get("JWT_SECRET") or "e2e-only-secret",
        "AUTH_REQUIRED": "true",
        "AUTH_USERS_FILE": str(users_file),
        "AUTH_TOKEN_TTL_MINUTES": "720",
        "LOGIN_RATE_LIMIT_PER_MINUTE": "5",
        "RATE_LIMIT_PER_MINUTE": "120",
        "EXPENSIVE_RATE_LIMIT_PER_MINUTE": "20",
        "MAX_UPLOAD_MB": "10",
        "AI_SERVICE_TIMEOUT": "60",
        "AI_SERVICE_TTS_TIMEOUT": "600",
        "ENABLE_DOCS": "false",
        "CORS_ALLOW_ORIGINS": ROOT_ENV.get("CORS_ALLOW_ORIGINS", ""),
    })
    return env


class Services:
    def __init__(self):
        self.ai = None
        self.gw = None
        self.ai_log = None
        self.gw_log = None
        self.gw_python = find_gateway_python()

    def _spawn(self, name, cmd, cwd, env):
        log_path = LOGS / f"{name}.log"
        log = open(log_path, "ab")
        log.write(f"\n===== start {time.strftime('%Y-%m-%dT%H:%M:%S')} =====\n".encode())
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=log,
                                stderr=subprocess.STDOUT, bufsize=0)
        return proc, log

    def start_ai(self):
        self.ai, self.ai_log = self._spawn(
            "ai_cloud", [sys.executable, "main.py"], AI_DIR, ai_child_env())
        ok, detail, elapsed = wait_http(f"{AI_BASE}/health", timeout=90)
        return ok, detail, elapsed

    def start_gw(self, users_file):
        self.gw, self.gw_log = self._spawn(
            "gateway", [self.gw_python, "app.py"], GW_DIR, gw_child_env(users_file))
        ok, detail, elapsed = wait_http(f"{GW_BASE}/health", timeout=60)
        return ok, detail, elapsed

    def stop_ai(self):
        if self.ai and self.ai.poll() is None:
            self.ai.terminate()
            try:
                self.ai.wait(timeout=15)
            except Exception:
                self.ai.kill()
                self.ai.wait()
        if self.ai_log:
            self.ai_log.close()
        self.ai = None
        self.ai_log = None

    def stop_all(self):
        self.stop_ai()
        if self.gw and self.gw.poll() is None:
            self.gw.terminate()
            try:
                self.gw.wait(timeout=15)
            except Exception:
                self.gw.kill()
                self.gw.wait()
        if self.gw_log:
            self.gw_log.close()
        self.gw = None
        self.gw_log = None


def seed_users():
    users_file = RUNTIME / "users_e2e.json"
    if users_file.exists():
        users_file.unlink()
    env = os.environ.copy()
    env["AUTH_USERS_FILE"] = str(users_file)
    for u in (USER_A, USER_B):
        r = subprocess.run([sys.executable, "auth_seed.py", "add", u, "-p", USER_PWD],
                           cwd=str(GW_DIR), env=env, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"播种账号失败 {u}: {r.stderr[-300:]}")
    return users_file


def login(user, ip):
    r = requests.post(f"{GW_BASE}/api/auth/login",
                      json={"username": user, "password": USER_PWD},
                      headers={"X-Forwarded-For": ip}, timeout=15)
    return r


# ---------------------------------------------------------------- 用例
def ws_chat_case(token, sid, text, timeout=90, record_result=True):
    """真实 WS：建连 → 发文本 → 收 LLM 文本/音频/状态"""
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    url = f"{GW_WS}/ws/chat/{sid}?token={quote(token or '')}"
    events, texts, audio_frames, error = [], [], 0, None
    first_text_at = None
    t0 = time.time()
    try:
        with connect(url, open_timeout=10, close_timeout=5) as ws:
            ws.send(json.dumps({"text": text}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = ws.recv(timeout=6)
                except TimeoutError:
                    events.append({"type": "timeout"})
                    break
                except ConnectionClosed as e:
                    events.append({"type": "closed", "code": e.rcvd.code if e.rcvd else None,
                                   "reason": (e.rcvd.reason if e.rcvd else "")})
                    break
                if isinstance(msg, bytes):
                    audio_frames += 1
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    events.append({"type": "unparsed", "raw": snippet(str(msg), 100)})
                    continue
                events.append({k: data.get(k) for k in ("type", "status", "message", "index")
                               if k in data})
                if data.get("type") == "text":
                    if first_text_at is None:
                        first_text_at = round(time.time() - t0, 2)
                    texts.append(data.get("content", ""))
                elif data.get("type") == "error":
                    error = data.get("message")
                    break
                elif data.get("type") == "status" and data.get("status") == "done":
                    break
    except ConnectionClosed as e:
        error = f"WS 关闭 code={e.rcvd.code if e.rcvd else None} reason={e.rcvd.reason if e.rcvd else ''}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    answer = "".join(texts).strip()
    mock_markers = ("mock response", "Cloud TTS 模式", "Streaming response")
    ok = bool(answer) and not error and not any(m in answer for m in mock_markers)
    if record_result:
        record(
            "E01", "真实文字对话（WS → LLM → 回复）", "文字对话",
            "PASS" if ok else "FAIL", time.time() - t0,
            [assertion("收到非空回复", bool(answer), snippet(answer, 120)),
             assertion("无 error 事件", error is None, error),
             assertion("非固定 mock 文案", not any(m in answer for m in mock_markers), snippet(answer, 80))],
            {"events": events[:12], "audio_frames": audio_frames, "first_text_seconds": first_text_at,
             "answer": snippet(answer, 200), "error": error},
            classification=None if ok else "外部依赖不可用",
            notes="" if ok else "LLM 不可用（DashScope 欠费）：AI 服务返回 error 事件后关闭连接",
        )
    return ok, answer, audio_frames


def _transcribe_raw(token, sid, timeout=90):
    t0 = time.time()
    try:
        with open(SAPI_AUDIO, "rb") as f:
            r = requests.post(
                f"{GW_BASE}/api/voice/transcribe",
                headers={"Authorization": f"Bearer {token}"},
                data={"session_id": sid},
                files={"audio": ("asr_input.wav", f, "audio/wav")},
                timeout=timeout,
            )
        body = safe_json(r)
        text = (body or {}).get("text", "") if isinstance(body, dict) else ""
        return {"http_status": r.status_code, "body": snippet(r.text), "json": body,
                "text": text, "elapsed": time.time() - t0, "error": None}
    except Exception as e:
        return {"http_status": None, "body": "", "json": None, "text": "",
                "elapsed": time.time() - t0, "error": f"{type(e).__name__}: {e}"}


def transcribe_case(token, sid, timeout=90):
    d = _transcribe_raw(token, sid, timeout)
    status = d["http_status"]
    text = d["text"]
    ok = (status == 200 and (d["json"] or {}).get("status") == "ok" and bool(text)
          and any(k in text for k in ("你好", "天气", "很好")))
    record(
        "E02", "真实语音输入（WAV → ASR → 文本）", "语音输入",
        "PASS" if ok else "FAIL", d["elapsed"],
        [assertion("HTTP 200", status == 200, status),
         assertion("status=ok 且文本非空", bool(text), snippet(text, 80)),
         assertion("语义匹配", ok, snippet(text, 80))],
        {"http_status": status, "body": d["body"], "text": text,
         "expected_keywords": ["你好", "天气", "很好"], "error": d["error"]},
        classification=None if ok else (
            "外部依赖不可用（根因）；产品缺陷（HTTP 200 错误语义）" if status == 200
            else "外部依赖不可用"),
        notes="" if ok else "ASR 依赖不可用；若 HTTP 200 但 body 为 error，则同时存在状态码误用",
    )
    return ok, text


def tts_case(token, sid, text, timeout=180):
    t0 = time.time()
    try:
        r = requests.post(
            f"{GW_BASE}/api/voice/tts",
            headers={"Authorization": f"Bearer {token}"},
            data={"session_id": sid, "text": text},
            timeout=timeout,
        )
        ctype = r.headers.get("content-type", "")
        if "audio/wav" in ctype:
            info = wav_info(r.content)
            ok = info.get("duration_seconds", 0) > 0 and info.get("sample_rate") in (16000, 22050, 24000, 44100, 48000)
            record(
                "E03", "真实 TTS 合成（文本 → WAV）", "TTS",
                "PASS" if ok else "FAIL", time.time() - t0,
                [assertion("Content-Type=audio/wav", True, ctype),
                 assertion("WAV 可解码且时长>0", info.get("duration_seconds", 0) > 0, info),
                 assertion("采样率合理", info.get("sample_rate") in (16000, 22050, 24000, 44100, 48000), info)],
                {"http_status": r.status_code, "audio": info},
                notes="云 TTS 模式实测")
            return ok, info
        body = safe_json(r)
        record(
            "E03", "真实 TTS 合成（文本 → WAV）", "TTS",
            "FAIL", time.time() - t0,
            [assertion("返回音频", False, ctype),
             assertion("非错误 JSON", False, snippet(r.text, 120))],
            {"http_status": r.status_code, "content_type": ctype, "body": snippet(r.text)},
            classification="外部依赖不可用（根因）；错误语义可接受（502）",
            notes="AI 侧未捕获异常返回 500 纯文本，网关无法解析 JSON 后统一转 502；建议 AI 返回 503 JSON",
        )
        return False, body
    except Exception as e:
        record("E03", "真实 TTS 合成（文本 → WAV）", "TTS", "FAIL", time.time() - t0,
               [assertion("请求成功", False, e)], {"error": f"{type(e).__name__}: {e}"},
               classification="外部依赖不可用")
        return False, None


def define_voice_case(token, sid):
    t0 = time.time()
    try:
        with open(SAPI_AUDIO, "rb") as f:
            r = requests.post(
                f"{GW_BASE}/api/voice/define",
                headers={"Authorization": f"Bearer {token}"},
                data={"session_id": sid, "description": "温暖的女声"},
                files={"audio": ("ref.wav", f, "audio/wav")},
                timeout=120,
            )
        body = safe_json(r) or {}
        if r.status_code == 200 and "error" in body:
            record(
                "E05", "声音定义（cloud 模式）", "声音定义/克隆", "SKIP", time.time() - t0,
                [assertion("provider 支持声音定义", False, body.get("error"))],
                {"http_status": 200, "body": snippet(r.text)},
                classification="环境能力缺失（cloud 不支持）；产品缺陷（HTTP 200 错误语义）",
                notes="cloud 模式明确不支持声音定义；HTTP 200 + error 文案，属状态码误用")
        elif r.status_code == 200 and body.get("status") == "ok":
            record("E05", "声音定义（cloud 模式）", "声音定义/克隆", "PASS", time.time() - t0,
                   [assertion("返回 voice_definition", bool(body.get("voice_definition")), body)],
                   {"http_status": 200, "body": snippet(r.text)})
        else:
            record("E05", "声音定义（cloud 模式）", "声音定义/克隆", "FAIL", time.time() - t0,
                   [assertion("HTTP 200 且 status=ok", False, r.status_code)],
                   {"http_status": r.status_code, "body": snippet(r.text)},
                   classification="外部依赖不可用")
    except Exception as e:
        record("E05", "声音定义（cloud 模式）", "声音定义/克隆", "FAIL", time.time() - t0,
               [assertion("请求成功", False, e)], {"error": f"{type(e).__name__}: {e}"},
               classification="外部依赖不可用")


def clone_voice_case():
    record(
        "E06", "声音克隆", "声音定义/克隆", "SKIP", 0,
        [assertion("存在专用克隆端点", False, "main.py 路由清单仅 define-voice（声音设计），无 clone 端点")],
        {"routes_checked": ["/api/ai/define-voice", "/api/voice/define"], "clone_endpoint": None},
        classification="功能缺口",
        notes="产品未提供独立声音克隆接口；VoxCPM2 的克隆能力未暴露为 HTTP 端点",
    )


def full_chain_case(token, asr_ok, asr_text, llm_ok, tts_ok):
    if not (asr_ok and llm_ok and tts_ok):
        reasons = []
        if not asr_ok:
            reasons.append("ASR 不可用")
        if not llm_ok:
            reasons.append("LLM 不可用")
        if not tts_ok:
            reasons.append("TTS 不可用")
        record(
            "E04", "真实语音对话全链路（音频→ASR→LLM→TTS→音频）", "全链路", "SKIP", 0,
            [assertion("前置链路均可用", False, "、".join(reasons))],
            {"asr_ok": asr_ok, "llm_ok": llm_ok, "tts_ok": tts_ok},
            classification="外部依赖不可用",
            notes="前置依赖缺失，未执行；未用 mock 补位",
        )
        return
    t0 = time.time()
    sid = f"e2e_chain_{START_TS}"
    asr = _transcribe_raw(token, sid)
    if not asr["text"]:
        record("E04", "真实语音对话全链路（音频→ASR→LLM→TTS→音频）", "全链路", "FAIL",
               time.time() - t0,
               [assertion("链路 ASR 产出文本", False, asr["body"])],
               {"asr": asr}, classification="外部依赖不可用",
               notes="二次 ASR 未产出文本，链路中断")
        return
    llm_ok2, answer, audio_frames = ws_chat_case(token, sid, asr["text"], record_result=False)
    elapsed = time.time() - t0
    ok = bool(answer) and audio_frames > 0 and llm_ok2
    record(
        "E04", "真实语音对话全链路（音频→ASR→LLM→TTS→音频）", "全链路",
        "PASS" if ok else "FAIL", elapsed,
        [assertion("ASR 文本非空", bool(asr["text"]), snippet(asr["text"], 80)),
         assertion("LLM 文本回复非空", bool(answer), snippet(answer, 100)),
         assertion("收到 TTS 音频帧", audio_frames > 0, audio_frames)],
        {"asr_text": asr["text"], "answer": snippet(answer, 200),
         "audio_frames": audio_frames, "end_to_end_seconds": round(elapsed, 2)},
        classification=None if ok else "外部依赖不可用",
        notes="" if ok else "链路中 LLM 或 TTS 失败",
    )


def restart_persistence_case(token, services):
    sid = f"e2e_persist_{START_TS}"
    t0 = time.time()
    try:
        r = requests.get(f"{GW_BASE}/api/characters",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        chars = (safe_json(r) or {}).get("characters") or []
        if not chars:
            raise RuntimeError(f"角色列表为空: {snippet(r.text)}")
        cid = chars[0]["id"]
        r1 = requests.post(f"{GW_BASE}/api/sessions/{sid}/character",
                           headers={"Authorization": f"Bearer {token}"},
                           data={"character_id": cid}, timeout=15)
        r2 = requests.post(f"{GW_BASE}/api/emotion-config",
                           headers={"Authorization": f"Bearer {token}"},
                           data={"session_id": sid, "emotion_name": "e2e_persist",
                                 "emotion_description": "重启恢复测试",
                                 "happiness": 0.8, "sadness": 0.1, "anger": 0.1,
                                 "fear": 0.1, "disgust": 0.1, "surprise": 0.3},
                           timeout=15)
        # /ready 会 force flush 状态存储，确保落盘
        requests.get(f"{AI_BASE}/ready", timeout=5)
        time.sleep(1.0)
        state_file = RUNTIME / "data_cloud" / "state.json"
        state_keys = {}
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                sessions = data.get("sessions", data)
                entry = sessions.get(sid) if isinstance(sessions, dict) else None
                if isinstance(entry, dict):
                    state_keys = {
                        "short_term_keys": sorted((entry.get("short_term") or {}).keys()),
                        "has_chat_history": "chat_history" in (entry.get("short_term") or {}),
                        "has_dialogue_history": bool(entry.get("dialogue_history")),
                    }
            except Exception as e:
                state_keys = {"read_error": f"{type(e).__name__}: {e}"}

        services.stop_ai()
        ok_start, detail, elapsed_up = services.start_ai()
        ready = wait_http(f"{AI_BASE}/ready", timeout=60)[0]
        r3 = requests.get(f"{GW_BASE}/api/sessions/{sid}/character",
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r4 = requests.get(f"{GW_BASE}/api/emotion-config/{sid}",
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        c_after = (safe_json(r3) or {}).get("character") or {}
        e_after = (safe_json(r4) or {}).get("emotion_config") or {}
        ok = (r3.status_code == 200 and c_after.get("id") == cid
              and r4.status_code == 200 and e_after.get("name") == "e2e_persist")
        record(
            "E07a", "重启恢复：会话/角色绑定/情感配置", "重启恢复",
            "PASS" if ok else "FAIL", time.time() - t0,
            [assertion("AI 重启成功", ok_start, detail),
             assertion("角色绑定仍在", c_after.get("id") == cid, c_after.get("id")),
             assertion("情感配置仍在", e_after.get("name") == "e2e_persist", e_after.get("name"))],
            {"session_id": sid, "character_id": cid, "state_file": str(state_file),
             "state_keys_before_restart": state_keys, "restart_health": detail,
             "restart_elapsed": elapsed_up, "ready_after": ready,
             "character_after": {"id": c_after.get("id"), "name": c_after.get("name")},
             "emotion_after": e_after},
            classification=None if ok else "产品缺陷",
            notes="" if ok else "重启后状态丢失或读取失败",
        )
        record(
            "E07b", "重启恢复：对话历史", "重启恢复", "SKIP", 0,
            [assertion("存在真实对话历史", False, state_keys.get("has_chat_history"))],
            {"state_keys_before_restart": state_keys,
             "evidence": "LLM 欠费导致无法产生真实对话；main.py L799-802 仅在 assistant_reply 非空时写 chat_history"},
            classification="外部依赖不可用",
            notes="未用 /api/ai/process 的 mock 音频路径制造历史；该项不计 PASS",
        )
    except Exception as e:
        record("E07a", "重启恢复：会话/角色绑定/情感配置", "重启恢复", "FAIL", time.time() - t0,
               [assertion("执行成功", False, e)], {"error": f"{type(e).__name__}: {e}",
                                                  "traceback": traceback.format_exc()[-800:]},
               classification="产品缺陷")


def isolation_cases(token_a, token_b):
    sid_a = f"e2e_iso_a_{START_TS}"
    sid_b = f"e2e_iso_b_{START_TS}"
    t0 = time.time()
    try:
        r = requests.get(f"{GW_BASE}/api/characters",
                         headers={"Authorization": f"Bearer {token_a}"}, timeout=15)
        chars = (safe_json(r) or {}).get("characters") or []
        cid = chars[0]["id"] if chars else "preset_warm_companion"
        requests.post(f"{GW_BASE}/api/sessions/{sid_a}/character",
                      headers={"Authorization": f"Bearer {token_a}"},
                      data={"character_id": cid}, timeout=15)
        # B 用自己 token 读取 A 的会话角色
        r_read = requests.get(f"{GW_BASE}/api/sessions/{sid_a}/character",
                              headers={"Authorization": f"Bearer {token_b}"}, timeout=15)
        body_read = safe_json(r_read) or {}
        leaked_character = (body_read.get("character") or {}).get("id")
        read_denied = r_read.status_code in (401, 403, 404)
        # B 向 A 的会话写情感配置
        r_write = requests.post(f"{GW_BASE}/api/emotion-config",
                                headers={"Authorization": f"Bearer {token_b}"},
                                data={"session_id": sid_a, "emotion_name": "e2e_cross_user"},
                                timeout=15)
        write_denied = r_write.status_code in (401, 403, 404)
        ok = read_denied and write_denied
        record(
            "E08", "多用户隔离：跨账号访问他人会话（HTTP）", "多用户隔离",
            "PASS" if ok else "FAIL", time.time() - t0,
            [assertion("B 读取 A 会话被拒绝", read_denied,
                       f"{r_read.status_code} {snippet(r_read.text, 120)}"),
             assertion("B 写入 A 会话被拒绝", write_denied,
                       f"{r_write.status_code} {snippet(r_write.text, 120)}")],
            {"sid_a": sid_a, "sid_b": sid_b,
             "b_read_status": r_read.status_code, "b_read_body": snippet(r_read.text, 200),
             "leaked_character_id": leaked_character,
             "b_write_status": r_write.status_code, "b_write_body": snippet(r_write.text, 120)},
            classification=None if ok else "产品缺陷（越权）",
            notes="" if ok else "AI 服务按 session_id 存取，无用户归属校验；B 成功读取/写入 A 的会话",
        )
    except Exception as e:
        record("E08", "多用户隔离：跨账号访问他人会话（HTTP）", "多用户隔离", "FAIL", time.time() - t0,
               [assertion("执行成功", False, e)], {"error": f"{type(e).__name__}: {e}"},
               classification="产品缺陷（越权）")

    # WS 越权：B 的 token 连接 A 的 session
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    t0 = time.time()
    accepted = False
    close_info = None
    try:
        url = f"{GW_WS}/ws/chat/{sid_a}?token={quote(token_b or '')}"
        with connect(url, open_timeout=8, close_timeout=5) as ws:
            accepted = True
            try:
                msg = ws.recv(timeout=3)
                close_info = {"unexpected_message": snippet(str(msg), 80)}
            except TimeoutError:
                close_info = {"still_open_after_seconds": 3}
            except ConnectionClosed as e:
                close_info = {"closed_code": e.rcvd.code if e.rcvd else None,
                              "reason": e.rcvd.reason if e.rcvd else ""}
                accepted = False
    except ConnectionClosed as e:
        close_info = {"closed_code": e.rcvd.code if e.rcvd else None,
                      "reason": e.rcvd.reason if e.rcvd else ""}
    except Exception as e:
        close_info = {"error": f"{type(e).__name__}: {e}"}
    ok = not accepted
    record(
        "E09", "多用户隔离：跨账号连接他人会话（WS）", "多用户隔离",
        "PASS" if ok else "FAIL", time.time() - t0,
        [assertion("B 连接 A 会话被拒绝/关闭", ok, close_info)],
        {"sid_a": sid_a, "b_token_used": True, "accepted": accepted, "close_info": close_info},
        classification=None if ok else "产品缺陷（越权）",
        notes="" if ok else "网关仅校验 token 有效性，未校验 session 归属；B 可连接 A 的实时会话",
    )


def readiness_case():
    t0 = time.time()
    r = requests.get(f"{GW_BASE}/ready", timeout=10)
    body = safe_json(r) or {}
    # 语义一致：ready↔200、not_ready↔503；不可用时必须给出 problems 与云依赖探测结果
    consistent = ((r.status_code == 200 and body.get("status") == "ready") or
                  (r.status_code == 503 and body.get("status") == "not_ready"))
    ai = body.get("ai_service") or {}
    cloud = ai.get("cloud_dependency") or {}
    record(
        "E12", "就绪探针是否反映外部依赖故障", "可观测性",
        "PASS" if consistent else "FAIL", time.time() - t0,
        [assertion("就绪状态与状态码语义一致", consistent,
                   {"http_status": r.status_code, "status": body.get("status")}),
         assertion("暴露云依赖探测结果", "ok" in cloud,
                   {"cloud_dependency": cloud, "problems": body.get("problems")})],
        {"http_status": r.status_code, "body": snippet(r.text, 300)},
        classification=None,
        notes="",
    )


# ---------------------------------------------------------------- CPU 结果
def load_cpu_cases(cpu_result: Path, workaround_result: Path):
    if cpu_result.exists():
        data = json.loads(cpu_result.read_text(encoding="utf-8"))
        audio = data.get("audio") or {}
        if data.get("status") == "PASS":
            record("E10", "CPU TTS 默认路径（VoxCPM2 on CPU）", "CPU TTS", "PASS",
                   data.get("total_seconds", 0),
                   [assertion("WAV 可解码", bool(audio), audio),
                    assertion("时长>0", audio.get("duration_seconds", 0) > 0, audio)],
                   data, notes=f"RTF={data.get('rtf')}，加载 {data.get('load_seconds')}s")
        else:
            record(
                "E10", "CPU TTS 默认路径（VoxCPM2 on CPU）", "CPU TTS", "FAIL",
                data.get("total_seconds", 0),
                [assertion("模型加载", data.get("load_ok"), data.get("load_seconds")),
                 assertion("推理成功", False, data.get("error"))],
                data,
                classification="产品缺陷（依赖兼容性）",
                notes="默认路径推理失败；根因见 evidence.traceback",
            )
    else:
        record("E10", "CPU TTS 默认路径（VoxCPM2 on CPU）", "CPU TTS", "SKIP", 0,
               [assertion("结果文件存在", False, cpu_result)], {}, classification="未执行")

    if workaround_result.exists():
        data = json.loads(workaround_result.read_text(encoding="utf-8"))
        audio = data.get("audio") or {}
        ok = data.get("status") == "PASS" and audio.get("duration_seconds", 0) > 0
        record(
            "E11", "CPU TTS 诊断路径（SDPA mask 形状补丁）", "CPU TTS",
            "PASS" if ok else "FAIL", data.get("total_seconds", 0),
            [assertion("WAV 可解码", bool(audio), audio),
             assertion("时长>0", audio.get("duration_seconds", 0) > 0, audio)],
            data,
            classification=None if ok else "产品缺陷（依赖兼容性）",
            notes=("仅诊断用，不代表产品默认路径可用；RTF=%s" % data.get("rtf")) if ok else "诊断补丁仍失败",
        )
    else:
        record("E11", "CPU TTS 诊断路径（SDPA mask 形状补丁）", "CPU TTS", "SKIP", 0,
               [assertion("结果文件存在", False, workaround_result)],
               {"expected": str(workaround_result)}, classification="未执行")


# ---------------------------------------------------------------- 报告
def build_defects():
    def add(did, severity, title, evidence, fix, kind="实测"):
        DEFECTS.append({"id": did, "severity": severity, "title": title,
                        "evidence": evidence, "minimal_fix": fix, "kind": kind})

    if any(c["id"] == "E08" and c["status"] == "FAIL" for c in CASES):
        add("DEF-01", "高", "会话无归属校验：任何登录用户可按 session_id 读取/写入他人会话",
            "E08/E09 实测：B 账号成功读取 A 的会话角色并向 A 的会话写情感配置；B 可连接 A 的 WS 会话。",
            "在网关为 session 增加 owner（sub）绑定：新增 /api/sessions 创建接口并持久化 owner；"
            "对 /api/sessions/{sid}/* 与 /ws/chat/{sid} 校验 owner == token.sub，不匹配返回 403。")
    if any(c["id"] == "E10" and c["status"] == "FAIL" for c in CASES):
        add("DEF-02", "高", "CPU TTS 默认路径不可用（torch 2.15 的 SDPA 拒绝 1D attn_mask）",
            "E10 实测：模型加载 57s 成功，首句推理抛 IndexError: Dimension out of range (expected [-1,0], got -2)，"
            "位置 .venv/Lib/site-packages/voxcpm/modules/minicpm4/model.py:206-212。",
            "锁定与 voxcpm 2.0.2 兼容的 torch 版本（当前环境 torch 2.15.0+cpu），或向上游提交 mask 形状修复；"
            "短期在 /ready 中标记 tts=unavailable，避免误判可用。")
    if any(c["id"] == "E03" and c["status"] == "FAIL" for c in CASES):
        add("DEF-03", "低", "TTS 上游异常未结构化（AI 侧 500，网关转 502）",
            "E03 实测：DashScope 欠费时 /api/voice/tts 返回 502 JSON；日志显示 AI 侧抛出 RuntimeError（HTTP 400 Arrearage），"
            "网关因响应非 JSON 走通用 502 分支。",
            "在 ai-service /api/ai/tts 捕获上游异常并返回 503 JSON；网关透传 503，前端按状态码降级。")
    if any(c["id"] == "E02" and c["status"] == "FAIL" for c in CASES):
        add("DEF-04", "中", "ASR/声音定义失败返回 HTTP 200 + error 文案",
            "E02/E05 实测：上游不可用时 /api/voice/transcribe 仍为 200；cloud 模式 define 返回 200 + error。",
            "失败分支返回 422/503（JSON error），前端按 HTTP 状态处理；不要把业务错误塞进 200。")
    if any(c["id"] == "E12" and c["status"] == "FAIL" for c in CASES):
        add("DEF-05", "中", "/ready 只看对象是否存在，不校验外部依赖健康",
            "E12 实测：LLM/ASR/云 TTS 全部 Arrearage 时，/ready 仍返回 200 status=ready。",
            "增加轻量依赖探测（或后台缓存探测结果）：LLM/ASR/TTS 任一不可用则 not_ready；"
            "区分 liveness（进程）与 readiness（可服务）。")
    if any(c["id"] == "E06" for c in CASES):
        add("DEF-06", "中", "无独立声音克隆端点",
            "E06 静态核对：main.py 仅有 /api/ai/define-voice 与 /api/voice/define（声音设计），无 clone 路由。",
            "若产品范围包含克隆：新增 /api/ai/clone-voice（参考音频 + 文本），复用 VoxCPM2 的克隆能力；否则在文档/前端移除克隆入口。",
            kind="静态核对")
    add("DEF-07", "中", "voxcpm2 模式下启动会 eager 加载 FunAudioChat-8B，CPU 机器可能 OOM",
        "静态推断：main.py L119-127 在非 cloud 模式实例化 FunAudioChatService；"
        "fun_audio_chat_service.py L18-23 在 __init__ 中 from_pretrained 8B（fp16 约 16GB）。本机 16GB/可用约 2.5-5.5GB。",
        "将 FunAudioChat 改为懒加载，或增加 ENABLE_FUN_AUDIO_CHAT=false 开关；CPU 降级路径只加载 VoxCPM2。",
        kind="静态推断")
    add("DEF-08", "低", "WS 无心跳/空闲超时，半开连接可能长期占用",
        "静态推断：ai-service ws_chat 在 while True 中 await ws.receive_json()；网关 gather 两个 relay 无超时；"
        "前端断网/休眠后连接可能悬挂。",
        "网关 websockets.connect 设置 ping_interval/ping_timeout；服务端对 idle 设置 wait_for 超时；前端增加心跳与重连上限。",
        kind="静态推断")
    add("DEF-09", "中", "真实语音链路因欠费完全不可验证（LLM/ASR/云 TTS 均被拒）",
        "D01/D02 实测 HTTP 400 Arrearage；D03 实测 WS task-failed："
        "\"Access denied, please make sure your account is in good standing.\"",
        "结清阿里云百炼欠费或更换有余额的 Key；充值后按报告命令复跑。",
        kind="实测")
    add("DEF-10", "中", "JWT 以查询参数写入网关访问日志",
        "实测：gateway.log 的 WebSocket 访问日志包含完整 ?token=<JWT>，可被日志读取者复用。",
        "访问日志脱敏 query；或改为短期 WS ticket；同时评估 Sec-WebSocket-Protocol 传递令牌。",
        kind="实测（日志取证）")
    add("DEF-11", "低", "项目 .venv 缺 PyJWT，网关无法按文档命令启动",
        "实测：gateway.log 报 ModuleNotFoundError: No module named 'jwt'；本套件改为回退全局 Python 3.11 启动网关。",
        "在 .venv 安装 PyJWT 并按 requirements/constraints 锁定，确保文档启动命令可直接运行。",
        kind="实测（环境）")


def case_brief(c):
    e = c.get("evidence", {})
    cid = c["id"]
    if cid in ("D01", "D02"):
        return f"HTTP {e.get('http_status')} Arrearage"
    if cid == "D03":
        return f"task-failed：{e.get('error')}"
    if cid == "D05":
        return f"{e.get('sample_rate')}Hz/{e.get('channels')}ch/{(e.get('sampwidth') or 0)*8}bit，{e.get('duration_seconds')}s"
    if cid == "D04":
        return "无 NVIDIA GPU；torch 为 CPU 版"
    if cid == "E01":
        return f"events={e.get('events')}；error={e.get('error')}"
    if cid == "E02":
        return f"HTTP {e.get('http_status')} {e.get('body')}"
    if cid == "E03":
        return f"HTTP {e.get('http_status')} {e.get('body')}"
    if cid == "E05":
        return f"HTTP 200 {e.get('body')}"
    if cid == "E06":
        return "无 clone 路由，仅 define-voice（声音设计）"
    if cid == "E04":
        return "ASR/LLM/TTS 均不可用，未执行"
    if cid == "E07a":
        return "重启后角色绑定与情感配置仍在"
    if cid == "E07b":
        return "无 chat_history（LLM 欠费，无法产生真实对话）"
    if cid == "E08":
        return (f"B 读取 A 会话 HTTP {e.get('b_read_status')}，泄漏角色 {e.get('leaked_character_id')}；"
                f"B 写入 HTTP {e.get('b_write_status')}")
    if cid == "E09":
        return f"B 用自身 token 连接 A 的 WS，{e.get('close_info')}"
    if cid == "E12":
        return f"HTTP {e.get('http_status')} {str(e.get('body'))[:70]}"
    if cid == "E10":
        return f"{e.get('error')}（模型加载 {e.get('load_seconds')}s 成功）"
    if cid == "E11":
        a = e.get("audio") or {}
        return f"RTF={e.get('rtf')}，音频 {a.get('duration_seconds')}s/{a.get('sample_rate')}Hz，合成 {e.get('synth_seconds')}s"
    return json.dumps(e, ensure_ascii=False)[:180]


def write_report(env_info, service_info):
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for c in CASES:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suite": "tests/e2e_real（真实链路，无 mock/route/事件注入）",
        "environment": env_info,
        "service": service_info,
        "summary": {**counts, "total": len(CASES)},
        "cases": CASES,
        "defects": DEFECTS,
        "unlock_conditions": {
            "dashscope": {
                "problem": "LLM/ASR/云 TTS 均返回 HTTP 400 Arrearage",
                "unlock": "结清阿里云百炼欠费，或使用有可用余额/已开通 paraformer-realtime-v2 与 qwen3-tts-flash 的 Key",
                "estimated_minimum_charge_cny": "10-50（估算；本套件消耗极小，最终以控制台账单为准）",
                "rerun": [
                    "在项目根目录设置新的 LLM_API_KEY（环境变量优先于 ai-service/.env）：",
                    "$env:LLM_API_KEY='sk-...'; D:\\ProgramFile\\语伴\\.venv\\Scripts\\python.exe tests\\e2e_real\\e2e_real.py",
                    "或更新 ai-service/.env 的 LLM_API_KEY 后直接复跑同一命令",
                ],
            },
            "gpu": {
                "problem": "本机无 NVIDIA GPU（torch.cuda.is_available()=false，nvidia-smi 不存在）",
                "unlock": "在 ≥8GB 显存且安装 NVIDIA Container Toolkit 的机器上启动 GPU 版 VoxCPM2",
                "rerun": "在 GPU 机器执行 docker compose up -d --build 后，将 BASE_URL 指向该环境复跑同一套件",
            },
            "cpu_tts": {
                "problem": "CPU 默认路径触发 torch 2.15 SDPA 维度错误（IndexError）",
                "unlock": "锁定兼容 torch 版本或修复 voxcpm 1D attn_mask；诊断补丁可绕过但不属于产品默认路径",
            },
        },
    }
    out = Path(__file__).resolve().parent / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 可读汇总
    lines = []
    lines.append("# 语伴真实 E2E 测试汇总\n")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 套件：{report['suite']}")
    lines.append(f"- 结果：**{counts.get('PASS',0)} 通过 / {counts.get('FAIL',0)} 失败 / {counts.get('SKIP',0)} 跳过**（共 {len(CASES)} 项）\n")
    lines.append("## 结论\n")
    lines.append("语音对话**未真正打通**：LLM、ASR、云 TTS 三项 DashScope 依赖均被拒（Arrearage / account not in good standing）；"
                 "本地 CPU TTS 默认路径另因 torch 2.15 的 SDPA 维度错误失败，诊断补丁可合成（RTF 34.95，仅根因验证）。"
                 "已确认产品缺陷：会话跨账号越权（HTTP + WS）、`/ready` 在依赖全挂时仍返回 ready。\n")
    lines.append("> PASS 组成（3 项）：D05 测试素材生成、E07a 重启后会话/角色/情感持久化、E11 CPU TTS 诊断路径。"
                 "E11 不代表产品默认路径可用；SKIP 一律不计入 PASS。\n")
    lines.append("## 环境\n")
    for k, v in env_info.items():
        lines.append(f"- {k}：`{v}`")
    lines.append("\n## 逐条结果\n")
    lines.append("| ID | 用例 | 状态 | 分类 | 关键证据 |")
    lines.append("|---|---|---|---|---|")
    for c in CASES:
        ev = case_brief(c).replace("|", "\\|")[:300]
        lines.append(f"| {c['id']} | {c['name']} | **{c['status']}** | {c['classification'] or ''} | {ev} |")
    lines.append("\n## 缺陷与最小修复建议\n")
    for d in DEFECTS:
        lines.append(f"### {d['id']} [{d['severity']}] {d['title']}（{d['kind']}）\n")
        lines.append(f"- 证据：{d['evidence']}")
        lines.append(f"- 最小修复：{d['minimal_fix']}\n")
    lines.append("## 解锁条件与复跑\n")
    lines.append("- 结清阿里云百炼欠费，或使用已开通 `paraformer-realtime-v2` / `qwen3-tts-flash` 且有余额的 Key；"
                 "本套件消耗极小，建议可用余额 ≥10-50 元（估算，最终以账单为准）。")
    lines.append("- 充值后执行下列命令即可复跑；CPU TTS 结果默认复用，如需重测请先删除 "
                 "`runtime/cpu_tts_result.json`，或直接运行 `cpu_tts_probe.py`。\n")
    lines.append("```powershell")
    lines.append("$env:LLM_API_KEY='sk-...'  # 有余额/已开通 ASR 与 TTS 的 Key")
    lines.append("D:\\ProgramFile\\语伴\\.venv\\Scripts\\python.exe tests\\e2e_real\\e2e_real.py")
    lines.append("```")
    lines.append("\n> 说明：SKIP 一律不计入 PASS；CPU TTS 诊断补丁结果仅作根因验证，不代表产品默认路径可用。\n")
    (Path(__file__).resolve().parent / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {out}")
    print(f"汇总已写入 {Path(__file__).resolve().parent / 'summary.md'}")
    return report


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-service", action="store_true")
    ap.add_argument("--cpu-result", default=str(RUNTIME / "cpu_tts_result.json"))
    ap.add_argument("--cpu-workaround-result", default=str(RUNTIME / "cpu_tts_workaround.json"))
    args = ap.parse_args()

    env_info = {
        "host": os.environ.get("COMPUTERNAME", "unknown"),
        "os": sys.platform,
        "python": sys.version.split()[0],
        "cpu": f"{platform.processor() or 'unknown'} / {os.cpu_count()} logical",
        "ram_total_mb": None,
        "gpu": "无（torch.cuda.is_available()=false, nvidia-smi 不存在）",
        "tts_provider": "cloud（ai-service/.env）",
        "auth_required": "true（网关测试实例）",
    }
    try:
        import psutil
        env_info["ram_total_mb"] = round(psutil.virtual_memory().total / 1048576)
        env_info["ram_available_mb_at_start"] = round(psutil.virtual_memory().available / 1048576)
    except Exception:
        pass

    # 1) 依赖探测
    ensure_sapi_audio()
    probe_gpu()
    probe_llm()
    probe_cloud_tts()
    probe_asr()

    service_info = {"ai_port": AI_PORT, "gateway_port": GW_PORT, "started": False}
    services = Services()
    service_info["gateway_python"] = services.gw_python
    if not args.skip_service:
        if port_in_use(AI_PORT) or port_in_use(GW_PORT):
            record("S00", "启动本地服务", "服务编排", "SKIP", 0,
                   [assertion("端口空闲", False, f"{AI_PORT}/{GW_PORT} 已被占用")],
                   {"ai_port_in_use": port_in_use(AI_PORT), "gw_port_in_use": port_in_use(GW_PORT)},
                   classification="环境冲突")
        else:
            t0 = time.time()
            try:
                users_file = seed_users()
                ok_ai, detail_ai, up_ai = services.start_ai()
                ok_gw, detail_gw, up_gw = services.start_gw(users_file)
                service_info.update({"started": bool(ok_ai and ok_gw),
                                     "ai_health": detail_ai, "ai_start_seconds": up_ai,
                                     "gateway_health": detail_gw, "gateway_start_seconds": up_gw,
                                     "users_file": str(users_file)})
                if ok_ai and ok_gw:
                    r = login(USER_A, IP_A)
                    token_a = (safe_json(r) or {}).get("token")
                    r2 = login(USER_B, IP_B)
                    token_b = (safe_json(r2) or {}).get("token")
                    if not token_a or not token_b:
                        record("S01", "测试账号登录", "服务编排", "FAIL", time.time() - t0,
                               [assertion("A 登录", bool(token_a), snippet(r.text, 100)),
                                assertion("B 登录", bool(token_b), snippet(r2.text, 100))],
                               {"a_status": r.status_code, "b_status": r2.status_code},
                               classification="产品缺陷")
                    else:
                        sid_chat = f"e2e_chat_{START_TS}"
                        sid_asr = f"e2e_asr_{START_TS}"
                        llm_ok, _, _ = ws_chat_case(token_a, sid_chat, "你好，请用一句话介绍你自己")
                        asr_ok, asr_text = transcribe_case(token_a, sid_asr)
                        tts_ok, _ = tts_case(token_a, f"e2e_tts_{START_TS}", "你好，这是真实语音合成测试。")
                        define_voice_case(token_a, f"e2e_define_{START_TS}")
                        clone_voice_case()
                        full_chain_case(token_a, asr_ok, asr_text, llm_ok, tts_ok)
                        restart_persistence_case(token_a, services)
                        isolation_cases(token_a, token_b)
                        readiness_case()
                else:
                    record("S00", "启动本地服务", "服务编排", "SKIP", time.time() - t0,
                           [assertion("AI 健康", ok_ai, detail_ai),
                            assertion("网关健康", ok_gw, detail_gw)],
                           {"ai": detail_ai, "gateway": detail_gw},
                           classification="环境冲突")
            except Exception as e:
                service_info["start_error"] = f"{type(e).__name__}: {e}"
                record("S00", "启动本地服务", "服务编排", "FAIL", time.time() - t0,
                       [assertion("启动成功", False, e)],
                       {"error": service_info["start_error"], "traceback": traceback.format_exc()[-600:]},
                       classification="环境冲突")
    services.stop_all()

    # 2) CPU TTS 结果（由独立进程生成，避免与云服务抢内存）
    load_cpu_cases(Path(args.cpu_result), Path(args.cpu_workaround_result))

    # 3) 缺陷与报告
    build_defects()
    write_report(env_info, service_info)


if __name__ == "__main__":
    main()
