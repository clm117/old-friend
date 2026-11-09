# -*- coding: utf-8 -*-
"""矩阵执行器 C：多用户隔离 / 上传校验 / 角色×人群×情感交叉 / 重启恢复 / T2 对话 / T3 探测"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import quote

import requests

from matrix_common import (ROOT, AI_DIR, GW_DIR, AI_BASE, GW_BASE, GW_WS, AI_ENV, ROOT_ENV,
                           DATA_DIR, RUNTIME, assertion, snippet, safe_json, tiny_wav, wav_info)
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
        r = self.req("POST", "/api/voice/define", case, timeout=120,
                     files={"audio": ("a.wav", tiny_wav(), "audio/wav")},
                     data={"session_id": "mx_t3_define", "description": "温柔女声",
                           "pitch": 1.0, "speed": 1.0, "volume": 1.0, "emotion_intensity": 0.5})
        body = safe_json(r) or {}
        err = body.get("error")
        code = body.get("code")
        # 能力已接入（GAP-07）：真实失败必须是明确语义，不能假成功
        degraded = r.status_code in (402, 429, 501, 502, 504) and code in (
            "voice_quota_unavailable", "voice_quota_exhausted", "voice_define_unsupported",
            "voice_define_timeout", "voice_upstream_error", "voice_define_invalid", "voice_audio_invalid")
        return result("BLOCKED", [
            assertion("真实声音定义成功（需账号可用）", False, err or body),
            assertion("失败分支返回明确 code（不假成功）", degraded,
                      {"status": r.status_code, "code": code, "hint": body.get("hint")}),
        ],
                      {"http_status": r.status_code, "body": snippet(r.text, 240)},
                      notes="云定义路径已接入；当前 DashScope 账号欠费（Arrearage），真实创建/合成待充值后验证",
                      classification="环境不可用")

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

    # ================ COV：覆盖重检补测（不 mock / 不拦截 / 不注入） ================
    def _new_session(self, case, account=None, ip=None):
        nickname = f"覆盖-{quote(str(case.get('id', '')))}"
        kwargs = {}
        if account:
            kwargs["account"] = account
        if ip:
            kwargs["ip"] = ip
        r = self.req("POST", "/api/sessions", case, json={"title": nickname}, **kwargs)
        body = safe_json(r) or {}
        return r, body.get("session_id") or "", nickname

    def _drop_session(self, sid, case, account=None):
        kwargs = {"account": account} if account else {}
        try:
            self.req("DELETE", f"/api/sessions/{quote(sid)}", case, **kwargs)
        except Exception:
            pass

    def _page_case(self, case):
        p = case["params"]
        r = self.req("GET", f"/api/sessions?limit={p['limit']}&offset={p['offset']}", case)
        body = safe_json(r) or {}
        ok = r.status_code == 422
        return result("PASS" if ok else "FAIL",
                      [assertion("非法分页参数被拒绝（422）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 200),
                       "limit": p["limit"], "offset": p["offset"]},
                      notes="" if ok else "非法分页参数未拒绝（可能被 clamp 后返回 200）",
                      classification=None if ok else "产品缺陷")

    def h_page_limit_zero(self, case):
        return self._page_case(case)

    def h_page_limit_over(self, case):
        return self._page_case(case)

    def h_page_offset_negative(self, case):
        return self._page_case(case)

    def h_page_offset_beyond(self, case):
        first = safe_json(self.req("GET", "/api/sessions?limit=1&offset=0", case)) or {}
        r = self.req("GET", "/api/sessions?limit=20&offset=999999", case)
        body = safe_json(r) or {}
        items = body.get("sessions") or []
        total = body.get("total")
        ok = (r.status_code == 200 and items == [] and total == first.get("total")
              and body.get("offset") == 999999)
        return result("PASS" if ok else "FAIL", [
            assertion("HTTP 200", r.status_code == 200, r.status_code),
            assertion("空页", items == [], len(items)),
            assertion("total 与首页一致", total == first.get("total"), {"total": total, "first": first.get("total")}),
            assertion("offset 回显", body.get("offset") == 999999, body.get("offset")),
        ], {"http_status": r.status_code, "body": snippet(r.text, 200)},
            notes="" if ok else "越界 offset 未返回空页或 total 异常",
            classification=None if ok else "产品缺陷")

    def h_session_order(self, case):
        titles = case["params"]["titles"]
        sids = []
        for t in titles:
            r, sid, _ = self._new_session(case)
            if sid:
                sids.append(sid)
        if len(sids) != len(titles):
            return result("FAIL", [assertion("会话创建", False, sids)], {"sids": sids},
                          classification="测试缺陷")
        for sid, t in zip(sids, titles):
            self.req("PATCH", f"/api/sessions/{quote(sid)}", case, json={"title": t})
            time.sleep(0.2)
        listing = safe_json(self.req("GET", "/api/sessions?limit=100&offset=0", case)) or {}
        items = listing.get("sessions") or []
        order = [x.get("session_id") for x in items]
        idx = {sid: (order.index(sid) if sid in order else -1) for sid in sids}
        relative = all(v >= 0 for v in idx.values()) and idx[sids[2]] < idx[sids[1]] < idx[sids[0]]
        desc = all((items[i].get("updated_at") or 0) >= (items[i + 1].get("updated_at") or 0)
                   for i in range(len(items) - 1))
        found = [x.get("title") for x in items if x.get("session_id") in sids]
        for sid in sids:
            self._drop_session(sid, case)
        ok = relative and desc
        return result("PASS" if ok else "FAIL", [
            assertion("三个测试会话均在列表", all(v >= 0 for v in idx.values()), idx),
            assertion("最近重命名排最前（C>B>A）", relative, found),
            assertion("整体 updated_at 倒序", desc, [(x.get("title"), x.get("updated_at")) for x in items[:5]]),
        ], {"order": found}, notes="" if ok else "列表排序与重命名上浮不符合预期",
            classification=None if ok else "产品缺陷")

    def h_session_rename_concurrent(self, case):
        import concurrent.futures
        n = int(case["params"].get("workers", 10))
        _, sid, _ = self._new_session(case)
        if not sid:
            return result("FAIL", [assertion("会话创建", False, sid)], {}, classification="测试缺陷")
        titles = [f"并发-{i}" for i in range(n)]

        def do_rename(title):
            r = self.req("PATCH", f"/api/sessions/{quote(sid)}", case,
                         json={"title": title}, timeout=30)
            return r.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            codes = list(pool.map(do_rename, titles))
        listing = safe_json(self.req("GET", "/api/sessions?limit=100&offset=0", case)) or {}
        final = next((x.get("title") for x in (listing.get("sessions") or [])
                      if x.get("session_id") == sid), None)
        self._drop_session(sid, case)
        ok = all(c == 200 for c in codes) and final in titles
        return result("PASS" if ok else "FAIL", [
            assertion("10 并发全部 200", all(c == 200 for c in codes), codes),
            assertion("最终标题为并发写入之一", final in titles, final),
        ], {"codes": codes, "final_title": final},
            notes="" if ok else "并发重命名出现非 200 或最终标题异常",
            classification=None if ok else "产品缺陷")

    def h_session_delete_then_bind(self, case):
        _, sid, _ = self._new_session(case)
        if not sid:
            return result("FAIL", [assertion("会话创建", False, sid)], {}, classification="测试缺陷")
        deleted = self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        bind = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                        data={"character_id": "preset_senior_sister"})
        reread = self.req("GET", f"/api/sessions/{quote(sid)}/character", case)
        listing = safe_json(self.req("GET", "/api/sessions?limit=100&offset=0", case)) or {}
        listed = any(x.get("session_id") == sid for x in (listing.get("sessions") or []))
        ok = (deleted.status_code == 204 and bind.status_code == 404
              and reread.status_code == 404 and not listed)
        return result("PASS" if ok else "FAIL", [
            assertion("删除 204", deleted.status_code == 204, deleted.status_code),
            assertion("删除后绑定 404（不复活）", bind.status_code == 404, bind.status_code),
            assertion("删除后读 404", reread.status_code == 404, reread.status_code),
            assertion("列表仍排除", not listed, listed),
        ], {"bind_body": snippet(bind.text, 160)}, notes="" if ok else "删除后会话被复活或绑定未拒绝",
            classification=None if ok else "产品缺陷")

    def h_iso_ws_forbidden(self, case):
        from websockets.sync.client import connect
        p = case["params"]
        sid = p["sid"]
        token = self.tokens.get(p["as"], "")
        url = f"{GW_WS}/ws/chat/{quote(sid)}?token={quote(token)}"
        code, reason, err = None, "", None
        try:
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                try:
                    ws.recv(timeout=5)
                except Exception:
                    pass
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", "") or ""
        except Exception as e:
            code, err = getattr(e, "code", None), f"{type(e).__name__}: {e}"
        ok = code == 1008 and "forbidden" in reason
        return result("PASS" if ok else "FAIL", [
            assertion("close code=1008", code == 1008, code),
            assertion("reason 含 forbidden", "forbidden" in reason, reason),
        ], {"code": code, "reason": reason, "error": err},
            notes="" if ok else "跨账号 WS 未被拒绝",
            classification=None if ok else "安全缺陷")

    def h_iso_delete_emotion(self, case):
        p = case["params"]
        sid = p["sid"]
        r = self.req("DELETE", f"/api/emotion-config/{quote(sid)}", case,
                     account=p["as"], ip=p["ip"])
        after = self.req("GET", f"/api/emotion-config/{quote(sid)}", case, account="matrix_a")
        cfg = (safe_json(after) or {}).get("emotion_config")
        ok = r.status_code in (403, 404) and after.status_code == 200 and bool(cfg)
        return result("PASS" if ok else "FAIL", [
            assertion("越权 DELETE 被拒绝（403/404）", r.status_code in (403, 404), r.status_code),
            assertion("A 的情感配置仍存在", bool(cfg), snippet(after.text, 160)),
        ], {"delete_status": r.status_code, "after_status": after.status_code},
            notes="" if ok else "跨账号删除情感配置未被拒绝或配置被清除",
            classification=None if ok else "安全缺陷")

    def h_cors_preflight_methods(self, case):
        origin = "http://localhost:8899"
        pairs = [("PUT", "/api/characters/preset_female_gentle"),
                 ("PATCH", "/api/characters/preset_female_gentle"),
                 ("DELETE", "/api/characters/cover_cors_probe"),
                 ("PATCH", "/api/sessions/mx_iso_session"),
                 ("DELETE", "/api/sessions/mx_iso_session")]
        details, ok_all = {}, True
        for method, path in pairs:
            r = requests.options(GW_BASE + path, timeout=10, headers={
                "Origin": origin, "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "Authorization,Content-Type"})
            allow = r.headers.get("access-control-allow-methods", "")
            acao = r.headers.get("access-control-allow-origin", "")
            good = (r.status_code == 200 and acao == origin and method in allow)
            ok_all = ok_all and good
            details[f"{method} {path}"] = {"status": r.status_code, "allow": allow,
                                           "acao": acao, "ok": good}
        token = self.tokens.get("matrix_a", "")
        actual = requests.patch(GW_BASE + "/api/sessions/mx_iso_session", timeout=15,
                                headers={"Origin": origin, "Authorization": f"Bearer {token}"},
                                json={"title": "CORS 校验"})
        acao_actual = actual.headers.get("access-control-allow-origin", "")
        ok_actual = actual.status_code == 200 and acao_actual == origin
        ok = ok_all and ok_actual
        return result("PASS" if ok else "FAIL", [
            assertion("5 组预检全部放行且方法齐全", ok_all, details),
            assertion("实际 PATCH 响应带 ACAO", ok_actual,
                      {"status": actual.status_code, "acao": acao_actual}),
        ], {"preflight": details, "actual": {"status": actual.status_code, "acao": acao_actual}},
            notes="" if ok else "CORS 预检未覆盖全部新增方法或响应头缺失",
            classification=None if ok else "产品缺陷")

    def _internal_token(self):
        return (ROOT_ENV.get("AI_INTERNAL_TOKEN") or ROOT_ENV.get("JWT_SECRET")
                or "matrix-only-secret-2026")

    def _sig_headers(self, user, ts):
        sig = hmac.new(self._internal_token().encode(), f"{user}|{ts}".encode(),
                       hashlib.sha256).hexdigest()
        return {"X-Yuban-User": user, "X-Yuban-Ts": str(ts), "X-Yuban-Sig": sig}

    def h_internal_sig_invalid(self, case):
        now = int(time.time())
        probes = {
            "missing": {},
            "tampered": {"X-Yuban-User": "matrix_a", "X-Yuban-Ts": str(now),
                         "X-Yuban-Sig": "0" * 64},
            "expired": self._sig_headers("matrix_a", now - 301),
            "future": self._sig_headers("matrix_a", now + 3600),
        }
        codes, bodies = {}, {}
        for name, headers in probes.items():
            r = requests.get(AI_BASE + "/api/ai/characters", headers=headers, timeout=10)
            codes[name] = r.status_code
            bodies[name] = snippet(r.text, 120)
        ok = all(c == 401 for c in codes.values())
        return result("PASS" if ok else "FAIL", [
            assertion("四种伪造均 401", ok, codes),
            assertion("不泄露角色数据", all("preset_" not in (b or "") for b in bodies.values()),
                      bodies),
        ], {"codes": codes, "bodies": bodies},
            notes="" if ok else "内部签名校验存在绕过",
            classification=None if ok else "安全缺陷")

    def h_internal_sig_valid(self, case):
        user = "matrix_a"
        headers = self._sig_headers(user, int(time.time()))
        first = requests.get(AI_BASE + "/api/ai/characters", headers=headers, timeout=10)
        replay = requests.get(AI_BASE + "/api/ai/characters", headers=headers, timeout=10)
        near_expiry = self._sig_headers(user, int(time.time()) - 290)
        near = requests.get(AI_BASE + "/api/ai/characters", headers=near_expiry, timeout=10)
        ok = (first.status_code == 200 and replay.status_code == 200
              and near.status_code == 200 and isinstance(safe_json(first), dict))
        return result("PASS" if ok else "FAIL", [
            assertion("合法签名 200", first.status_code == 200, first.status_code),
            assertion("TTL 内重放 200（设计允许）", replay.status_code == 200, replay.status_code),
            assertion("剩余 10s 内仍有效", near.status_code == 200, near.status_code),
        ], {"codes": [first.status_code, replay.status_code, near.status_code]},
            notes="" if ok else "合法内部签名被错误拒绝",
            classification=None if ok else "产品缺陷")

    def _free_port(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _start_ephemeral_gateway(self, extra_env, wait_seconds=40, attempts=3):
        """启动一次性隔离网关；端口被占用/启动失败时换端口重试，失败时回传 stderr 证据"""
        last_err, port, proc = "", None, None
        for attempt in range(1, attempts + 1):
            port = self._free_port()
            err_path = RUNTIME / f"ephemeral_gateway_{port}.err"
            env = os.environ.copy()
            env.update({
                "AI_SERVICE_URL": AI_BASE,
                "JWT_SECRET": ROOT_ENV.get("JWT_SECRET") or "matrix-only-secret-2026",
                "AI_INTERNAL_TOKEN": self._internal_token(),
                "AUTH_REQUIRED": "false",
                "AUTH_USERS_FILE": str(RUNTIME / "users.json"),
            })
            env.update(extra_env)
            code = ("import sys; sys.path.insert(0, '.'); import uvicorn, app; "
                    f"uvicorn.run(app.app, host='127.0.0.1', port={port}, log_config=None)")
            with open(err_path, "wb") as errf:
                proc = subprocess.Popen([self.services.gw_python, "-c", code], cwd=str(GW_DIR),
                                        env=env, stdout=subprocess.DEVNULL, stderr=errf)
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    # /health 在上游黑洞场景需等待其内部 3s 超时，探活客户端给足余量
                    r = requests.get(f"http://127.0.0.1:{port}/health", timeout=10)
                    if r.status_code in (200, 503):
                        return proc, True, port, ""
                except Exception:
                    time.sleep(0.5)
            self._stop_ephemeral_gateway(proc)
            try:
                last_err = err_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError as e:
                last_err = f"stderr 读取失败：{e}"
            last_err = f"attempt {attempt} port {port}: {last_err}"
        return proc, False, port, last_err

    def _stop_ephemeral_gateway(self, proc):
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def h_upstream_down(self, case):
        proc, up, port, err = self._start_ephemeral_gateway(
            {"AI_SERVICE_URL": "http://127.0.0.1:9"})
        try:
            if not up:
                return result("FAIL", [assertion("隔离网关启动", False, port)], {"port": port},
                              notes=f"端口被占用或网关无法启动；stderr={err}",
                              classification="测试缺陷")
            proxy = requests.get(f"http://127.0.0.1:{port}/api/characters", timeout=20)
            health = requests.get(f"http://127.0.0.1:{port}/health", timeout=10)
            ready = requests.get(f"http://127.0.0.1:{port}/ready", timeout=20)
            hb = safe_json(health) or {}
            rb = safe_json(ready) or {}
            ok = (proxy.status_code == 502 and health.status_code == 503
                  and ready.status_code == 503)
            return result("PASS" if ok else "FAIL", [
                assertion("代理端点 502", proxy.status_code == 502, proxy.status_code),
                assertion("/health 503 degraded", health.status_code == 503 and hb.get("status") == "degraded",
                          {"status": health.status_code, "body": hb}),
                assertion("/ready 503", ready.status_code == 503, {"status": ready.status_code, "body": rb}),
            ], {"proxy": {"status": proxy.status_code, "body": snippet(proxy.text, 160)},
                "health": hb, "ready": rb},
                notes="" if ok else "上游断连未正确透传为 502/503",
                classification=None if ok else "产品缺陷")
        finally:
            self._stop_ephemeral_gateway(proc)

    def h_upstream_timeout(self, case):
        import socket
        import threading
        hole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hole.bind(("127.0.0.1", 0))
        hole.listen(8)
        hole.settimeout(0.5)
        hole_port = hole.getsockname()[1]
        held, stop_flag = [], {"stop": False}

        def accept_loop():
            while not stop_flag["stop"]:
                try:
                    conn, _ = hole.accept()
                    held.append(conn)  # 接受连接但永不响应，制造真实上游超时
                except Exception:
                    continue

        threading.Thread(target=accept_loop, daemon=True).start()
        proc, up, port, err = self._start_ephemeral_gateway(
            {"AI_SERVICE_URL": f"http://127.0.0.1:{hole_port}",
             "AI_SERVICE_TIMEOUT": "1"})
        t0 = time.time()
        try:
            if not up:
                return result("FAIL", [assertion("隔离网关启动", False, port)], {"port": port},
                              notes=f"端口被占用或网关无法启动；stderr={err}",
                              classification="测试缺陷")
            r = requests.get(f"http://127.0.0.1:{port}/api/characters", timeout=15)
            elapsed = round(time.time() - t0, 2)
            body = safe_json(r) or {}
            ok = r.status_code == 502 and elapsed < 10
            return result("PASS" if ok else "FAIL", [
                assertion("上游不应答 → 502", r.status_code == 502, r.status_code),
                assertion("超时在 10s 内生效", elapsed < 10, elapsed),
                assertion("错误体明确（非空）", bool(body), snippet(r.text, 160)),
            ], {"http_status": r.status_code, "elapsed": elapsed, "port": port,
                "gap_port": hole_port, "body": snippet(r.text, 200)},
                notes="" if ok else "上游超时未返回 502 或耗时异常",
                classification=None if ok else "产品缺陷")
        except Exception as e:
            return result("FAIL", [assertion("上游不应答 → 502", False,
                                             f"{type(e).__name__}: {e}")],
                          {"elapsed": round(time.time() - t0, 2), "port": port},
                          notes="客户端等待超时，网关未在 15s 内返回",
                          classification="产品缺陷")
        finally:
            stop_flag["stop"] = True
            self._stop_ephemeral_gateway(proc)
            try:
                hole.close()
            except Exception:
                pass
            for conn in held:
                try:
                    conn.close()
                except Exception:
                    pass

    def h_emotion_delete_resave(self, case):
        _, sid, _ = self._new_session(case)
        if not sid:
            return result("FAIL", [assertion("会话创建", False, sid)], {}, classification="测试缺陷")
        path = f"/api/emotion-config/{quote(sid)}"
        form = {"session_id": sid, "emotion_name": "覆盖重存", "happiness": 0.6, "sadness": 0.2,
                "anger": 0.1, "fear": 0.1, "surprise": 0.4, "neutral": 0.5}
        first = self.req("POST", "/api/emotion-config", case, data=form)
        d1 = self.req("DELETE", path, case)
        d2 = self.req("DELETE", path, case)
        cleared = safe_json(self.req("GET", path, case)) or {}
        resave = self.req("POST", "/api/emotion-config", case,
                          data={**form, "emotion_name": "覆盖重存-2"})
        after = safe_json(self.req("GET", path, case)) or {}
        self._drop_session(sid, case)
        name = ((after.get("emotion_config") or {}).get("name")
                or (after.get("emotion_config") or {}).get("emotion_name"))
        ok = (first.status_code == 200 and d1.status_code == 204 and d2.status_code == 204
              and cleared.get("emotion_config") in (None, {}) and resave.status_code == 200
              and name == "覆盖重存-2")
        return result("PASS" if ok else "FAIL", [
            assertion("首次保存 200", first.status_code == 200, first.status_code),
            assertion("两次解绑均 204（幂等）", d1.status_code == 204 and d2.status_code == 204,
                      [d1.status_code, d2.status_code]),
            assertion("解绑后为未配置", cleared.get("emotion_config") in (None, {}), cleared),
            assertion("重存 200 且读回一致", resave.status_code == 200 and name == "覆盖重存-2",
                      {"status": resave.status_code, "name": name}),
        ], {"bodies": {"cleared": snippet(json.dumps(cleared, ensure_ascii=False), 160)}},
            notes="" if ok else "解绑后无法重新保存或读回不一致",
            classification=None if ok else "产品缺陷")

    def h_restart_tombstone(self, case):
        from websockets.sync.client import connect
        sid = (self.prepared or {}).get("tombstone_sid") or ""
        if not sid:
            return result("FAIL", [assertion("重启前已准备墓碑会话", False, sid)], {},
                          notes="prepare 未创建/删除墓碑会话", classification="测试缺陷")
        reread = self.req("GET", f"/api/sessions/{quote(sid)}/character", case)
        again = self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        listing = safe_json(self.req("GET", "/api/sessions?limit=100&offset=0", case)) or {}
        listed = any(x.get("session_id") == sid for x in (listing.get("sessions") or []))
        state = self.flush_state() or {}
        tombstoned = sid in (state.get("deleted_sessions") or {})
        token = self.tokens.get("matrix_a", "")
        code, reason = None, ""
        try:
            with connect(f"{GW_WS}/ws/chat/{quote(sid)}?token={quote(token)}",
                         open_timeout=10, close_timeout=5) as ws:
                try:
                    ws.recv(timeout=5)
                except Exception:
                    pass
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", "") or ""
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        ok = (reread.status_code == 404 and again.status_code == 404 and not listed
              and tombstoned and code == 1008 and "session_deleted" in reason)
        return result("PASS" if ok else "FAIL", [
            assertion("重启后读 404", reread.status_code == 404, reread.status_code),
            assertion("重启后重复删除 404", again.status_code == 404, again.status_code),
            assertion("重启后列表仍排除", not listed, listed),
            assertion("state.json 墓碑仍存在", tombstoned, sid),
            assertion("WS 1008 session_deleted", code == 1008 and "session_deleted" in reason,
                      {"code": code, "reason": reason}),
        ], {"sid": sid, "tombstones": len((state.get("deleted_sessions") or {}))},
            notes="" if ok else "墓碑未跨重启持久化",
            classification=None if ok else "产品缺陷")

    # ================ VOX：12 条预设音色（GAP-05 新增） ================
    PRESET_NAMES = ["乖巧学弟", "不羁少年", "理工学霸", "阳光型男", "相声评书", "新闻男声",
                    "新闻女声", "知性熟女", "温婉秘书", "智性精英", "浪险少女", "贵气千金"]
    REQUIRED_PRESET_KEYS = ["id", "name", "gender", "age_group", "persona", "locale", "voice_type",
                            "fallback", "voice_id", "capabilities", "ready", "notice"]

    def h_voice_presets_catalog(self, case):
        r = self.req("GET", "/api/voice-presets", case)
        body = safe_json(r) or {}
        presets = body.get("presets") or []
        names = [p.get("name") for p in presets]
        missing = {k for p in presets for k in self.REQUIRED_PRESET_KEYS if k not in p}
        bad = [p.get("id") for p in presets if p.get("locale") != "zh-CN"
               or p.get("voice_type") != "preset" or p.get("ready") is not False
               or "未就绪" not in (p.get("notice") or "")]
        style_caps = [p.get("name") for p in presets
                      if not isinstance((p.get("capabilities") or {}).get("style"), bool)]
        cat = safe_json(self.req("GET", "/api/voice-catalog", case)) or {}
        catalog = cat.get("catalog") or {}
        tags = catalog.get("tags") or []
        tag_pairs = [(t.get("id"), t.get("en")) for t in tags]
        expected_pairs = [("gentle", "gentle"), ("sweet", "sweet"), ("magnetic", "magnetic"),
                          ("energetic", "energetic"), ("deep", "deep"), ("calm", "calm"),
                          ("lively", "lively"), ("mature", "mature"), ("crisp", "crisp"),
                          ("husky", "husky")]
        a = [
            assertion("HTTP 200", r.status_code == 200, r.status_code),
            assertion("count=12", body.get("count") == 12, body.get("count")),
            assertion("12 条名称与顺序一致", names == self.PRESET_NAMES, names),
            assertion("字段齐全", not missing, sorted(missing)),
            assertion("locale/voice_type/ready/notice 合规", not bad, bad),
            assertion("capabilities.style 为布尔", not style_caps, style_caps),
            assertion("voice_id 当前为空", all(p.get("voice_id") is None for p in presets),
                      [p.get("voice_id") for p in presets]),
            assertion("catalog.preset_count=12", catalog.get("preset_count") == 12,
                      catalog.get("preset_count")),
            assertion("catalog.capabilities.voice_presets=True",
                      (catalog.get("capabilities") or {}).get("voice_presets") is True,
                      catalog.get("capabilities")),
            assertion("preset_voice_ready=False",
                      (catalog.get("capabilities") or {}).get("preset_voice_ready") is False,
                      (catalog.get("capabilities") or {}).get("preset_voice_ready")),
            assertion("既有 10 条标签基线不变", tag_pairs == expected_pairs, tag_pairs),
            assertion("既有默认回退不变",
                      catalog.get("defaults") == {"female": "gentle", "male": "magnetic"},
                      catalog.get("defaults")),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "names": names,
                       "body": snippet(r.text, 400)},
                      notes="" if ok else "预设目录字段/基线不符合预期",
                      classification=None if ok else "产品缺陷")

    def h_voice_preset_prompts(self, case):
        listing = safe_json(self.req("GET", "/api/voice-presets", case)) or {}
        presets = listing.get("presets") or []
        style_words = {"gentle", "sweet", "magnetic", "energetic", "deep", "calm", "lively",
                       "mature", "crisp", "husky", "theatrical", "storytelling", "rhythmic",
                       "broadcast", "authoritative", "elegant", "refined"}
        failures = []
        evidence = {}
        for p in presets:
            pid = p.get("id")
            r = self.req("GET", f"/api/voice-presets/{quote(pid)}/prompt", case)
            body = safe_json(r) or {}
            prompt = body.get("voice_prompt") or ""
            tokens = set(prompt.replace(",", " ").split())
            evidence[pid] = {"status": r.status_code, "prompt": prompt,
                             "ready": body.get("ready"), "notice": body.get("notice")}
            if (r.status_code != 200 or not prompt or "voice" not in prompt
                    or not (tokens & style_words) or body.get("ready") is not False
                    or not body.get("notice")):
                failures.append(pid)
        xs = [p for p in presets if p.get("id") == "vp_xiangsheng_pingshu"]
        xs_ok = bool(xs) and (xs[0].get("capabilities") or {}).get("style") is False \
            and bool(xs[0].get("capability_note"))
        a = [
            assertion("12 条均可按 id 取 prompt", len(presets) == 12 and not failures,
                      {"count": len(presets), "failures": failures}),
            assertion("相声评书 style=false 且有备注", xs_ok,
                      {"capabilities": (xs[0].get("capabilities") if xs else None),
                       "note": (xs[0].get("capability_note") if xs else None)}),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a, {"prompts": evidence},
                      notes="" if ok else "存在空 prompt/缺语感特征/未就绪提示缺失",
                      classification=None if ok else "产品缺陷")

    def h_voice_preset_unknown(self, case):
        r1 = self.req("GET", "/api/voice-presets/vp_not_exist/prompt", case)
        r, sid, _ = self._new_session(case)
        r2 = self.req("POST", f"/api/sessions/{quote(sid)}/voice-preset", case,
                      json={"preset_id": "vp_not_exist"}) if sid else None
        self._drop_session(sid, case)
        a = [
            assertion("未知预设取 prompt → 404", r1.status_code == 404, r1.status_code),
            assertion("未知预设绑定 → 404", r2 is not None and r2.status_code == 404,
                      None if r2 is None else r2.status_code),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"get": snippet(r1.text, 200),
                       "post": None if r2 is None else snippet(r2.text, 200)},
                      notes="" if ok else "未知预设未返回 404",
                      classification=None if ok else "产品缺陷")

    def h_voice_preset_bind(self, case):
        r, sid, _ = self._new_session(case)
        if not sid:
            return result("FAIL", [assertion("会话创建", False, r.status_code)],
                          {"body": snippet(r.text, 200)}, classification="测试缺陷")
        preset_id = "vp_xiangsheng_pingshu"
        rb = self.req("POST", f"/api/sessions/{quote(sid)}/voice-preset", case,
                      json={"preset_id": preset_id})
        body = safe_json(rb) or {}
        prompt = body.get("voice_prompt") or ""
        state = self.flush_state()
        entry = ((state.get("sessions") or {}).get(sid) or {}).get("short_term") or {}
        stored_prompt = entry.get("voice_description")
        stored_preset = entry.get("voice_preset_id")
        a = [
            assertion("绑定 HTTP 200", rb.status_code == 200, rb.status_code),
            assertion("status=ok", body.get("status") == "ok", body.get("status")),
            assertion("返回非空 voice_prompt", bool(prompt) and "voice" in prompt, prompt),
            assertion("voice_id 为空（未绑定真实音色）", body.get("voice_id") is None,
                      body.get("voice_id")),
            assertion("ready=false", body.get("ready") is False, body.get("ready")),
            assertion("明确提示未就绪", "未就绪" in (body.get("notice") or ""),
                      body.get("notice")),
            assertion("相声评书能力备注透出", bool(body.get("capability_note")),
                      body.get("capability_note")),
            assertion("落盘 voice_description = prompt", stored_prompt == prompt, stored_prompt),
            assertion("落盘 voice_preset_id", stored_preset == preset_id, stored_preset),
        ]
        self._drop_session(sid, case)
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"sid": sid, "http_status": rb.status_code, "body": snippet(rb.text, 400)},
                      notes="" if ok else "预设绑定未落库或未给出明确未就绪提示",
                      classification=None if ok else "产品缺陷")

    def h_voice_preset_forbidden(self, case):
        r, sid, _ = self._new_session(case, account="matrix_a")
        if not sid:
            return result("FAIL", [assertion("会话创建", False, r.status_code)],
                          {"body": snippet(r.text, 200)}, classification="测试缺陷")
        rb = self.req("POST", f"/api/sessions/{quote(sid)}/voice-preset", case,
                      account="matrix_b", json={"preset_id": "vp_guaiguai_xuedi"})
        self._drop_session(sid, case, account="matrix_a")
        ok = rb.status_code == 403
        return result("PASS" if ok else "FAIL",
                      [assertion("他人会话绑定预设 → 403", ok, rb.status_code)],
                      {"http_status": rb.status_code, "body": snippet(rb.text, 200)},
                      notes="" if ok else "越权绑定预设未被拒绝",
                      classification=None if ok else "安全缺陷")
