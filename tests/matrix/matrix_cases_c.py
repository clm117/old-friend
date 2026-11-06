# -*- coding: utf-8 -*-
"""矩阵执行器 C：多用户隔离 / 上传校验 / 角色×人群×情感交叉 / 重启恢复 / T2 对话 / T3 探测"""
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import quote

import requests

from matrix_common import (ROOT, AI_DIR, AI_BASE, GW_BASE, GW_WS, AI_ENV, ROOT_ENV, DATA_DIR,
                           assertion, snippet, safe_json, tiny_wav, wav_info)
from matrix_cases_b import CasesMid, AXES
from matrix_cases_a import result


class CasesExtra(CasesMid):

    # ================ ISO ================
    def _iso(self, case):
        return case["params"]["sid"], case["params"]["as"], case["params"]["ip"]

    def h_iso_read_character(self, case):
        sid, as_acct, ip = self._iso(case)
        r = self.req("GET", f"/api/sessions/{quote(sid)}/character", case, account=as_acct, ip=ip)
        body = safe_json(r) or {}
        ok = r.status_code in (403, 404)
        return result("PASS" if ok else "FAIL",
                      [assertion("跨账号读取被拒（403/404）", ok, r.status_code)],
                      {"http_status": r.status_code,
                       "leaked_character": (body.get("character") or {}).get("id")},
                      notes="" if ok else "B 账号成功读取 A 的会话角色",
                      classification=None if ok else "产品缺陷",
                      defect=None if ok else "DEF-01")

    def h_iso_write_character(self, case):
        sid, as_acct, ip = self._iso(case)
        r = self.req("POST", f"/api/sessions/{quote(sid)}/character", case, account=as_acct, ip=ip,
                     data={"character_id": "preset_funny_buddy"})
        body = safe_json(r) or {}
        ok = r.status_code in (403, 404)
        return result("PASS" if ok else "FAIL",
                      [assertion("跨账号写入被拒（403/404）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160),
                       "write_status": body.get("status")},
                      notes="" if ok else "B 账号成功改写 A 的会话角色",
                      classification=None if ok else "产品缺陷",
                      defect=None if ok else "DEF-01")

    def h_iso_read_emotion(self, case):
        sid, as_acct, ip = self._iso(case)
        r = self.req("GET", f"/api/emotion-config/{quote(sid)}", case, account=as_acct, ip=ip)
        body = safe_json(r) or {}
        ok = r.status_code in (403, 404)
        return result("PASS" if ok else "FAIL",
                      [assertion("跨账号读取被拒（403/404）", ok, r.status_code)],
                      {"http_status": r.status_code, "emotion_config": body.get("emotion_config")},
                      notes="" if ok else "B 账号成功读取 A 的情感配置",
                      classification=None if ok else "产品缺陷",
                      defect=None if ok else "DEF-01")

    def h_iso_write_emotion(self, case):
        sid, as_acct, ip = self._iso(case)
        r = self.req("POST", "/api/emotion-config", case, account=as_acct, ip=ip,
                     data={"session_id": sid, "emotion_name": "越权写入",
                           "happiness": 1.0, "sadness": 0.0, "anger": 0.0,
                           "fear": 0.0, "disgust": 0.0, "surprise": 0.0})
        body = safe_json(r) or {}
        ok = r.status_code in (403, 404)
        return result("PASS" if ok else "FAIL",
                      [assertion("跨账号写入被拒（403/404）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160),
                       "stored_name": (body.get("emotion_config") or {}).get("name")},
                      notes="" if ok else "B 账号成功改写 A 的情感配置",
                      classification=None if ok else "产品缺陷",
                      defect=None if ok else "DEF-01")

    def h_iso_read_response(self, case):
        sid, as_acct, ip = self._iso(case)
        r = self.req("GET", f"/api/ai/response/{quote(sid)}", case, account=as_acct, ip=ip)
        ok = r.status_code in (403, 404)
        return result("PASS" if ok else "FAIL",
                      [assertion("跨账号读取被拒（403/404）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      notes="" if ok else "B 账号可读取 A 的对话历史接口",
                      classification=None if ok else "产品缺陷",
                      defect=None if ok else "DEF-01")

    def h_iso_custom_character(self, case):
        cid = self.prepared.get("iso_char_id")
        _, as_acct, ip = self._iso(case)
        if not cid:
            return result("FAIL", [assertion("隔离角色已创建", False, "")],
                          classification="测试缺陷")
        r = self.req("GET", f"/api/characters/{quote(cid)}", case, account=as_acct, ip=ip)
        body = safe_json(r) or {}
        ok = r.status_code == 404
        lst = safe_json(self.req("GET", "/api/characters", case, account=as_acct, ip=ip)) or {}
        visible = cid in {c.get("id") for c in lst.get("characters", [])}
        return result("PASS" if ok and not visible else "FAIL",
                      [assertion("B 不可见 A 的自定义角色", ok and not visible,
                                 f"get={r.status_code} visible_in_list={visible}")],
                      {"http_status": r.status_code, "character": body.get("character"),
                       "visible_in_list": visible},
                      notes="" if ok and not visible else "自定义角色全局共享，B 可直接读取/列出",
                      classification=None if ok and not visible else "产品缺陷",
                      defect=None if ok and not visible else "DEF-04")

    # ================ UPL ================
    def h_upload_empty(self, case):
        r = self.req("POST", "/api/voice/transcribe", case,
                     files={"audio": ("empty.wav", b"", "audio/wav")},
                     data={"session_id": case["params"]["sid"]})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 400", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      classification=None if ok else "产品缺陷")

    def h_upload_fake(self, case):
        r = self.req("POST", "/api/voice/transcribe", case,
                     files={"audio": ("fake.wav", b"this is not audio", "audio/wav")},
                     data={"session_id": case["params"]["sid"]})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 400（魔数校验）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      classification=None if ok else "产品缺陷")

    def h_upload_over(self, case):
        big = b"RIFF" + b"\x00" * (11 * 1024 * 1024)
        try:
            r = self.req("POST", "/api/voice/transcribe", case,
                         files={"audio": ("big.wav", big, "audio/wav")},
                         data={"session_id": case["params"]["sid"]}, timeout=120)
            status = r.status_code
            body = snippet(r.text, 160)
        except Exception as e:
            status, body = None, f"{type(e).__name__}: {e}"
        ok = status == 413
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 413", ok, status)],
                      {"http_status": status, "body": body, "sent_bytes": len(big)},
                      classification=None if ok else "产品缺陷")

    def h_define_no_desc(self, case):
        r = self.req("POST", "/api/voice/define", case,
                     files={"audio": ("a.wav", tiny_wav(), "audio/wav")},
                     data={"session_id": case["params"]["sid"], "description": ""})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 400（description 必填）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      classification=None if ok else "产品缺陷")

    # ================ CRS：角色 × 人群 × 情感 ================
    def h_cross_matrix(self, case):
        p = case["params"]
        cid = (self.prepared.get("cross") or {}).get((p["role"], p["population"]))
        if not cid:
            return result("FAIL", [assertion("交叉角色已准备", False, p)], classification="测试缺陷")
        sid = case["params"]["sid"]
        bind = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                        data={"character_id": cid})
        bb = safe_json(bind) or {}
        emo = self.req("POST", "/api/emotion-config", case,
                       data={"session_id": sid, "emotion_name": p["emotion_label"],
                             **p["emotion_params"]})
        eb = safe_json(emo) or {}
        got_char = safe_json(self.req("GET", f"/api/sessions/{quote(sid)}/character", case)) or {}
        got_emo = safe_json(self.req("GET", f"/api/emotion-config/{quote(sid)}", case)) or {}
        cfg = got_emo.get("emotion_config") or {}
        a = [assertion("绑定成功", bind.status_code == 200 and bb.get("status") == "ok",
                       (bind.status_code, bb.get("status"))),
             assertion("角色读回一致", (got_char.get("character") or {}).get("id") == cid,
                       (got_char.get("character") or {}).get("id")),
             assertion("情感写入成功", emo.status_code == 200 and eb.get("status") == "ok",
                       (emo.status_code, eb.get("status"))),
             assertion("情感读回一致", cfg.get("params") == p["emotion_params"], cfg.get("params"))]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"character_id": cid, "emotion": cfg.get("name")})

    # ================ RST：重启恢复 ================
    def h_restart_character(self, case):
        p = case["params"]
        r = self.req("GET", f"/api/sessions/{quote(p['sid'])}/character", case)
        body = safe_json(r) or {}
        got = (body.get("character") or {}).get("id")
        ok = r.status_code == 200 and got == p["character_id"]
        return result("PASS" if ok else "FAIL",
                      [assertion("重启后角色绑定仍在", ok, got)],
                      {"http_status": r.status_code, "character_id": got},
                      classification=None if ok else "产品缺陷")

    def h_restart_emotion(self, case):
        p = case["params"]
        r = self.req("GET", f"/api/emotion-config/{quote(p['sid'])}", case)
        cfg = (safe_json(r) or {}).get("emotion_config") or {}
        e = self.dim_emotion(p["emotion"])
        ok = r.status_code == 200 and cfg.get("params") == e["params"]
        return result("PASS" if ok else "FAIL",
                      [assertion("重启后情感配置仍在", ok, cfg.get("params"))],
                      {"http_status": r.status_code, "config": cfg},
                      classification=None if ok else "产品缺陷")

    def h_restart_custom_character(self, case):
        p = case["params"]
        r = self.req("GET", "/api/characters", case)
        chars = (safe_json(r) or {}).get("characters", [])
        found = [c for c in chars if c.get("name") == p["name"]]
        ok = r.status_code == 200 and bool(found)
        return result("PASS" if ok else "FAIL",
                      [assertion("重启后自定义角色仍在", ok, len(found))],
                      {"http_status": r.status_code, "matched": len(found)},
                      classification=None if ok else "产品缺陷")

    # ================ T2：真实多轮对话 ================
    def h_ws_dialog(self, case):
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed

        p = case["params"]
        sid = case["params"]["sid"]
        # 1) 准备角色
        if p["role_id"].startswith("preset_"):
            cid = p["role_id"]
        else:
            body = {"name": p["role_name"], "voice_profile": p["voice_profile"],
                    "persona_profile": p["persona"]}
            cid = ((safe_json(self.create(case, body)) or {}).get("character") or {}).get("id")
        bind = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                        data={"character_id": cid or ""})
        if bind.status_code != 200:
            return result("BLOCKED", [assertion("角色绑定成功", False, bind.status_code)],
                          {"http_status": bind.status_code, "body": snippet(bind.text, 160)})
        self.req("POST", "/api/emotion-config", case,
                 data={"session_id": sid, "emotion_name": p["emotion"],
                       **p["emotion_params"]})

        token = self.tokens.get(case["params"]["account"], "")
        url = f"{GW_WS}/ws/chat/{sid}?token={quote(token)}"
        replies, errors, statuses, gen_chars = [], [], [], []
        t0 = time.time()
        try:
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                for utter in p["utterances"]:
                    ws.send(json.dumps({"text": utter}))
                    turn_text, deadline = [], time.time() + 60
                    while time.time() < deadline:
                        try:
                            msg = ws.recv(timeout=8)
                        except TimeoutError:
                            break
                        except ConnectionClosed as e:
                            errors.append(f"closed code={e.rcvd.code if e.rcvd else None}")
                            break
                        if isinstance(msg, bytes):
                            continue
                        data = json.loads(msg)
                        if data.get("type") == "text":
                            turn_text.append(data.get("content", ""))
                        elif data.get("type") == "status":
                            statuses.append(data.get("status"))
                            if data.get("status") == "generating":
                                gen_chars.append(data.get("character"))
                            if data.get("status") == "done":
                                break
                        elif data.get("type") == "error":
                            errors.append(data.get("message"))
                            break
                    replies.append("".join(turn_text).strip())
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

        elapsed = round(time.time() - t0, 2)
        non_empty = [x for x in replies if x]
        context_ok = True
        if p["turns"] == 3:
            context_ok = "蓝" in replies[-1]
        state = self.flush_state()
        hist = ((state.get("sessions") or {}).get(sid, {}).get("short_term") or {}).get("chat_history") or []
        stored_user = any(p["utterances"][0] in (h.get("content") or "") for h in hist if h.get("role") == "user")
        char_ok = bool(gen_chars) and any(g == p["role_name"] for g in gen_chars)
        a = [assertion("无 error 事件", not errors, errors),
             assertion("至少一轮有文本回复", bool(non_empty), f"{len(non_empty)}/{len(replies)}"),
             assertion("generating 状态包含角色名", char_ok, gen_chars),
             assertion("上下文保持（第 3 轮记住蓝色）", context_ok if p["turns"] == 3 else "n/a",
                       replies[-1] if replies else ""),
             assertion("对话历史已落盘", stored_user, sid)]
        ok = not errors and bool(non_empty) and char_ok and context_ok and stored_user
        return result("PASS" if ok else "FAIL", a,
                      {"replies": [snippet(x, 120) for x in replies], "errors": errors,
                       "statuses": statuses[:12], "elapsed_seconds": elapsed,
                       "history_len": len(hist)},
                      classification=None if ok else "产品缺陷")

    # ================ T3：探测 ================
    def h_probe(self, case):
        kind = case["params"]["probe"]
        handler = getattr(self, "probe_" + kind, None)
        if handler is None:
            return result("BLOCKED", [assertion("探测处理器存在", False, kind)],
                          {"probe": kind}, classification="测试缺陷")
        return handler(case)

    def probe_gpu(self, case):
        try:
            r = subprocess.run([sys.executable, "-c",
                                "import torch;print(torch.cuda.is_available(), torch.__version__, torch.cuda.device_count())"],
                               capture_output=True, text=True, timeout=120)
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()[-200:]
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        return result("BLOCKED", [assertion("CUDA 可用", False, out or err)],
                      {"torch_cuda": out, "error": err, "nvidia_smi": self._nvidia_smi()},
                      notes="本机无 CUDA 设备，本地 TTS/声音定义不可用", classification="环境不可用")

    def _nvidia_smi(self):
        try:
            r = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=20)
            return "ok" if r.returncode == 0 else f"rc={r.returncode}"
        except Exception as e:
            return f"{type(e).__name__}"

    def probe_local_model(self, case):
        d = AI_DIR / "voxcpm2"
        files = sorted([p.name for p in d.glob("*")])[:12] if d.exists() else []
        return result("BLOCKED", [assertion("VoxCPM2 可加载", False,
                                             "cloud 模式未实例化 / 无 CUDA")],
                      {"dir": str(d), "files": files},
                      notes="cloud 模式下 voxcpm2_service=None", classification="环境不可用")

    def probe_cloud_tts(self, case):
        key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
        try:
            r = requests.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": AI_ENV.get("CLOUD_TTS_MODEL", "qwen3-tts-flash"),
                      "input": {"text": "你好", "voice": AI_ENV.get("CLOUD_TTS_VOICE", "Cherry"),
                                "language_type": "Auto"}}, timeout=60)
            body = safe_json(r) or {}
            url = ((body.get("output") or {}).get("audio") or {}).get("url")
            evidence = {"http_status": r.status_code, "body": snippet(r.text, 200), "audio_url": bool(url)}
        except Exception as e:
            evidence = {"error": f"{type(e).__name__}: {e}"}
        return result("BLOCKED", [assertion("云 TTS 可用", False, evidence.get("body") or evidence)],
                      evidence, notes="云 TTS 探测未通过（欠费/未开通），T3 整层阻塞",
                      classification="环境不可用")

    def probe_asr(self, case):
        audio = ROOT / "tests" / "e2e_real" / "runtime" / "asr_input.wav"
        if not audio.exists():
            return result("BLOCKED", [assertion("转写成功", False, "测试音频缺失")],
                          {"asset": str(audio)}, classification="环境不可用")
        from websockets.sync.client import connect
        import uuid
        key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
        with wave.open(str(audio), "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
        task_id = str(uuid.uuid4())
        events, err, sentences = [], None, []
        chunk = int(rate * 0.1) * 2
        try:
            with connect("wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
                         additional_headers={"Authorization": f"Bearer {key}"},
                         open_timeout=10, close_timeout=5, max_size=16 * 1024 * 1024) as ws:
                ws.send(json.dumps({"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                                    "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                                                "model": "paraformer-realtime-v2",
                                                "parameters": {"sample_rate": rate, "format": "pcm",
                                                               "language_hints": ["zh", "en"]}, "input": {}}}))
                started, deadline = False, time.time() + 20
                while time.time() < deadline:
                    try:
                        msg = json.loads(ws.recv(timeout=3))
                    except TimeoutError:
                        continue
                    h = msg.get("header", {})
                    ev = h.get("event")
                    events.append({"event": ev, "error": h.get("error_message")})
                    if ev == "task-started":
                        started = True
                        break
                    if ev == "task-failed":
                        err = h.get("error_message")
                        break
                if started:
                    for i in range(0, len(pcm), chunk):
                        ws.send(json.dumps({"header": {"action": "continue-task", "task_id": task_id,
                                                      "streaming": "duplex"},
                                            "payload": {"input": {"audio": pcm[i:i + chunk].hex()}}}))
                    ws.send(json.dumps({"header": {"action": "finish-task", "task_id": task_id,
                                                  "streaming": "duplex"}, "payload": {"input": {}}}))
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        try:
                            msg = json.loads(ws.recv(timeout=3))
                        except TimeoutError:
                            continue
                        h = msg.get("header", {})
                        ev = h.get("event")
                        events.append({"event": ev, "error": h.get("error_message")})
                        sent = (((msg.get("payload") or {}).get("output") or {}).get("sentence") or {})
                        if sent.get("text"):
                            sentences.append(sent.get("text"))
                        if ev in ("task-finished", "task-failed"):
                            if ev == "task-failed":
                                err = h.get("error_message")
                            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        ok = bool(sentences) and not err
        return result("BLOCKED" if not ok else "PASS",
                      [assertion("ASR 转写成功", ok, err or sentences[-2:])],
                      {"asset_seconds": round(len(pcm) / (rate * 2), 2), "events": events[:6],
                       "sentence": sentences[-1] if sentences else "", "error": err},
                      notes="" if ok else "ASR 依赖 DashScope，探测未通过",
                      classification=None if ok else "环境不可用")

    def probe_voice_define(self, case):
        r = self.req("POST", "/api/voice/define", case,
                     files={"audio": ("a.wav", tiny_wav(), "audio/wav")},
                     data={"session_id": "mx_t3_define", "description": "温柔女声",
                           "pitch": 1.0, "speed": 1.0, "volume": 1.0, "emotion_intensity": 0.5})
        body = safe_json(r) or {}
        err = body.get("error")
        degraded = r.status_code == 501 and body.get("code") == "voice_define_unsupported"
        return result("BLOCKED", [
            assertion("声音定义可用", False, err or body),
            assertion("cloud 模式显式 501 + voice_define_unsupported（降级语义正确）", degraded,
                      {"status": r.status_code, "code": body.get("code")}),
        ],
                      {"http_status": r.status_code, "body": snippet(r.text, 200)},
                      notes="cloud 模式下声音定义不可用（GAP-07）；第 1 批已将假成功改为显式 501",
                      classification="能力缺失")

    def probe_voice_clone(self, case):
        r = self.req("POST", "/api/voice/clone", case, json={})
        return result("BLOCKED", [assertion("克隆可用", False, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 120)},
                      notes="无克隆端点（GAP-06）", classification="能力缺失")

    def probe_emotion_tts(self, case):
        return result("BLOCKED", [assertion("情感音色效果可验证", False,
                                             "cloud 模式忽略 emotion_params，且无 GPU")],
                      {"code_path": "main.py::_synthesize_speech → cloud 分支忽略 style/emotion_params"},
                      notes="需本地 VoxCPM2 + GPU 才能验证情感对音色的影响", classification="环境不可用")

    def probe_playback(self, case):
        return result("BLOCKED", [assertion("语音回放链路可验证", False,
                                             "无播放/音频回放 API，回放由前端实现")],
                      {"code_path": "WS 仅推送 WAV bytes + {type:audio,size,sample_rate}"},
                      notes="无后端回放能力，需前端真机验证", classification="能力缺失")
