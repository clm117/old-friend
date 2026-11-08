# -*- coding: utf-8 -*-
"""矩阵执行器 B：情感 / 会话 / 限流"""
from urllib.parse import quote

import requests

from matrix_common import ACCOUNTS, GW_BASE, GW_WS, assertion, snippet, safe_json
from matrix_cases_a import CasesBase, result

AXES = ["happiness", "sadness", "anger", "fear", "disgust", "surprise"]


class CasesMid(CasesBase):

    # ================ EMO ================
    def h_emotion_boundary(self, case):
        p = case["params"]
        sid = case["params"]["sid"]
        params = dict(p["base"])
        params[p["dominant"]] = p["input"]
        data = {"session_id": sid, "emotion_name": p["emotion_name"],
                "emotion_description": f"{p['type']}-{p['input']}",
                **{k: params[k] for k in AXES}}
        r = self.req("POST", "/api/emotion-config", case, data=data)
        body = safe_json(r) or {}
        if p["kind"] == "illegal":
            ok = r.status_code == 422
            return result("PASS" if ok else "FAIL",
                          [assertion("非法数值返回 422", ok, r.status_code)],
                          {"http_status": r.status_code, "body": snippet(r.text, 200)},
                          classification=None if ok else "产品缺陷")
        got = safe_json(self.req("GET", f"/api/emotion-config/{quote(sid)}", case)) or {}
        cfg = got.get("emotion_config") or {}
        vals = cfg.get("params") or {}
        a = [assertion("写入 HTTP 200", r.status_code == 200, r.status_code),
             assertion("回读配置存在", isinstance(cfg, dict) and bool(vals), cfg),
             assertion(f"{p['dominant']} clamp 到 {p['expected']}",
                       abs(float(vals.get(p["dominant"], -99)) - float(p["expected"])) < 1e-6,
                       vals.get(p["dominant"])),
             assertion("所有轴落在 [0,1]", all(0.0 <= float(v) <= 1.0 for v in vals.values()), vals)]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "sent": params, "stored": vals},
                      classification=None if ok else "产品缺陷")

    def h_emotion_switch(self, case):
        p = case["params"]
        sid = case["params"]["sid"]
        types = {t["id"]: t for t in self.dim_emotions()}
        for key in ("first", "second"):
            e = types[p[key]]
            data = {"session_id": sid, "emotion_name": e["label"], "emotion_description": "",
                    **e["params"]}
            r1 = self.req("POST", "/api/emotion-config", case, data=data)
            if r1.status_code != 200:
                return result("FAIL", [assertion(f"写入 {p[key]} 成功", False, r1.status_code)],
                              {"http_status": r1.status_code, "body": snippet(r1.text, 160)},
                              classification="产品缺陷")
        got = safe_json(self.req("GET", f"/api/emotion-config/{quote(sid)}", case)) or {}
        cfg = got.get("emotion_config") or {}
        second = types[p["second"]]
        a = [assertion("名称切换为第二次写入", cfg.get("name") == second["label"], cfg.get("name")),
             assertion("参数为第二次写入", cfg.get("params") == second["params"], cfg.get("params"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"stored": cfg})

    def h_emotion_isolate_sessions(self, case):
        p = case["params"]
        e = self.dim_emotion(p.get("emotion") or "calm")
        r = self.req("POST", "/api/emotion-config", case,
                     data={"session_id": p["sid"], "emotion_name": e["label"], **e["params"]})
        got = safe_json(self.req("GET", f"/api/emotion-config/{quote(p['other'])}", case)) or {}
        a = [assertion("写入成功", r.status_code == 200, r.status_code),
             assertion("另一 session 无配置", got.get("emotion_config") is None, got.get("emotion_config"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a, {"other": got})

    def h_emotion_unknown_session(self, case):
        got = safe_json(self.req("GET", f"/api/emotion-config/{quote(case['params']['sid'])}", case)) or {}
        a = [assertion("emotion_config 为 null", got.get("emotion_config") is None, got.get("emotion_config"))]
        return result("PASS" if a[0]["ok"] else "FAIL", a, {"body": got})

    def h_emotion_gap_unbind(self, case):
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        if not sid:
            return result("FAIL", [assertion("创建会话成功", False, created.status_code)],
                          {"body": snippet(created.text, 160)}, classification="测试缺陷")
        saved = self.req("POST", "/api/emotion-config", case,
                         data={"session_id": sid, "emotion_name": "解绑前",
                               "happiness": 0.6, "sadness": 0.2, "anger": 0.1,
                               "fear": 0.1, "disgust": 0.1, "surprise": 0.2})
        before = self.req("GET", f"/api/emotion-config/{quote(sid)}", case)
        cleared = self.req("DELETE", f"/api/emotion-config/{quote(sid)}", case)
        after = self.req("GET", f"/api/emotion-config/{quote(sid)}", case)
        repeat = self.req("DELETE", f"/api/emotion-config/{quote(sid)}", case)
        # 换一个与用例当前账号不同的账号发起解绑（用例账号由生成器轮换，固定写 matrix_b 会撞车）
        base_acct = (case.get("params") or {}).get("account", "matrix_a")
        other_acct = next((a for a in ACCOUNTS if a != base_acct), "matrix_a")
        other = dict(case)
        other["params"] = dict(case["params"], account=other_acct)
        denied = self.req("DELETE", f"/api/emotion-config/{quote(sid)}", other)
        cfg_before = (safe_json(before) or {}).get("emotion_config")
        cfg_after = (safe_json(after) or {}).get("emotion_config")
        a = [
            assertion("预置情感配置 200", saved.status_code == 200, saved.status_code),
            assertion("解绑前有配置", bool(cfg_before), cfg_before is not None),
            assertion("解绑 204", cleared.status_code == 204, cleared.status_code),
            assertion("删后 GET 仍 200 且返回空配置（不是 404）",
                      after.status_code == 200 and cfg_after in (None, {}),
                      {"status": after.status_code, "config": cfg_after}),
            assertion("重复删除幂等 204", repeat.status_code == 204, repeat.status_code),
            assertion("他人解绑 403", denied.status_code == 403, denied.status_code),
        ]
        ok = all(x["ok"] for x in a)
        self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": cleared.status_code, "after_status": after.status_code,
                       "repeat": repeat.status_code, "session_id": sid},
                      classification=None if ok else "产品缺陷")

    def h_emotion_presets(self, case):
        r = self.req("GET", "/api/emotion-presets", case)
        body = safe_json(r) or {}
        presets = body.get("presets") or []
        axes = body.get("axes") or []
        ok_axes = all(
            all(0.0 <= float((p.get("params") or {}).get(a, -1)) <= 1.0 for a in AXES)
            for p in presets
        )
        a = [assertion("预设接口 200", r.status_code == 200 and body.get("status") == "ok", r.status_code),
             assertion("恰好 6 类预设", len(presets) == 6, len(presets)),
             assertion("预设覆盖 6 轴", set(axes) == set(AXES), axes),
             assertion("预设参数在 [0,1]", ok_axes, [p.get("params") for p in presets][:1]),
             assertion("预设 id 唯一", len({p.get("id") for p in presets}) == len(presets),
                       [p.get("id") for p in presets])]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"preset_ids": [p.get("id") for p in presets], "axes": axes})

    def h_emotion_custom_name(self, case):
        p = case["params"]
        sid = p["sid"]
        base = self.dim_emotion("calm")["params"]
        r = self.req("POST", "/api/emotion-config", case,
                     data={"session_id": sid, "emotion_name": p["name"], "emotion_description": "自定义历史名称",
                           **{k: base[k] for k in AXES}})
        got = safe_json(self.req("GET", f"/api/emotion-config/{quote(sid)}", case)) or {}
        cfg = got.get("emotion_config") or {}
        a = [assertion("自定义名称可保存 200", r.status_code == 200, r.status_code),
             assertion("读回同名（未被枚举拒绝）", cfg.get("name") == p["name"],
                       f"stored={cfg.get('name')!r} input={p['name']!r}"),
             assertion("6 轴参数完整", all(k in (cfg.get("params") or {}) for k in AXES),
                       cfg.get("params"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "stored_name": cfg.get("name")})

    def h_emotion_gap_enum(self, case):
        r = self.req("GET", "/api/characters/options", case)
        opts = (safe_json(r) or {}).get("options", {})
        has_enum = any(k for k in opts if "emotion" in k.lower())
        return result("CAPABILITY_GAP" if not has_enum else "PASS",
                      [assertion("options 无情感枚举", not has_enum, list(opts.keys()))],
                      {"option_keys": list(opts.keys())},
                      notes="情感类型是自由文本，无枚举/预设（GAP-09）" if not has_enum else "",
                      classification="能力缺失" if not has_enum else None)

    def h_emotion_long_name(self, case):
        p = case["params"]
        r = self.req("POST", "/api/emotion-config", case,
                     data={"session_id": p["sid"], "emotion_name": p["name"],
                           "emotion_description": "", **self.dim_emotion("calm")["params"]})
        ok = r.status_code == 400
        stored = ((safe_json(r) or {}).get("emotion_config") or {}).get("name", "")
        return result("PASS" if ok else "FAIL",
                      [assertion("超长名称被拒绝（400）", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160),
                       "name_len": len(p["name"]), "stored_name_len": len(stored)},
                      notes="" if ok else "1000 字情感名称被原样接受，缺少长度校验",
                      classification=None if ok else "产品缺陷", defect=None if ok else "DEF-07")

    # ================ SES ================
    def h_session_bind(self, case):
        p = case["params"]
        sid = case["params"]["sid"]
        r = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                     data={"character_id": p["character_id"]})
        body = safe_json(r) or {}
        a = [assertion("绑定 HTTP 200", r.status_code == 200, r.status_code),
             assertion("status=ok", body.get("status") == "ok", body.get("status")),
             assertion("返回 character_id", body.get("character_id") == p["character_id"],
                       body.get("character_id")),
             assertion("voice_prompt 符合声学档案", body.get("voice_prompt") == p["expected_prompt"],
                       f"actual={body.get('voice_prompt')!r} expected={p['expected_prompt']!r}"),
             assertion("system_prompt 非空", bool(body.get("system_prompt")), len(body.get("system_prompt") or ""))]
        got = safe_json(self.req("GET", f"/api/sessions/{quote(sid)}/character", case)) or {}
        a.append(assertion("GET 读回一致", (got.get("character") or {}).get("id") == p["character_id"],
                           (got.get("character") or {}).get("id")))
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "voice_prompt": body.get("voice_prompt")})

    def h_session_switch(self, case):
        p = case["params"]
        sid = case["params"]["sid"]
        r1 = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                      data={"character_id": p["first"]})
        r2 = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                      data={"character_id": p["second"]})
        got = safe_json(self.req("GET", f"/api/sessions/{quote(sid)}/character", case)) or {}
        a = [assertion("两次绑定成功", r1.status_code == 200 and r2.status_code == 200,
                       (r1.status_code, r2.status_code)),
             assertion("GET 返回第二个角色", (got.get("character") or {}).get("id") == p["second"],
                       (got.get("character") or {}).get("id"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a, {"readback": got.get("character")})

    def h_session_voice_style(self, case):
        p = case["params"]
        sid = case["params"]["sid"]
        r = self.req("POST", f"/api/ai/sessions/{quote(sid)}/voice-style", case,
                     json={"description": p["description"], "voice_definition": "test-def",
                           "voice_params": {"pitch": 1.1, "speed": 1.0}})
        body = safe_json(r) or {}
        state = self.flush_state()
        entry = (state.get("sessions") or {}).get(sid, {})
        stored = (entry.get("short_term") or {}).get("voice_description")
        a = [assertion("绑定 HTTP 200", r.status_code == 200, r.status_code),
             assertion("status=ok", body.get("status") == "ok", body.get("status")),
             assertion("落盘 voice_description 一致", stored == p["description"], stored)]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "stored": stored})

    def h_session_gap_list(self, case):
        p = case["params"]
        limit = p.get("limit", 5)
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        r = self.req("GET", f"/api/sessions?limit={limit}&offset=0", case)
        body = safe_json(r) or {}
        items = body.get("sessions") or []
        other = dict(case)
        other["params"] = dict(case["params"], account="matrix_b")
        r_b = self.req("GET", f"/api/sessions?limit={limit}&offset=0", other)
        sid_b = {x.get("session_id") for x in ((safe_json(r_b) or {}).get("sessions") or [])}
        a = [
            assertion("列表 200", r.status_code == 200, r.status_code),
            assertion("返回 sessions 列表", isinstance(items, list), len(items)),
            assertion("total/limit/offset 字段", all(k in body for k in ("total", "limit", "offset")),
                      {k: body.get(k) for k in ("total", "limit", "offset")}),
            assertion("包含本人新建会话", bool(sid) and any(x.get("session_id") == sid for x in items), sid),
            assertion("分页不超过 limit", len(items) <= limit, len(items)),
            assertion("他人账号看不到该会话", bool(sid) and sid not in sid_b, sid in sid_b),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "total": body.get("total"), "session_id": sid},
                      classification=None if ok else "产品缺陷")

    def h_session_gap_delete(self, case):
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        if not sid:
            return result("FAIL", [assertion("创建会话成功", False, created.status_code)],
                          {"body": snippet(created.text, 160)}, classification="测试缺陷")
        first = self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        read_after = self.req("GET", f"/api/sessions/{quote(sid)}/character", case)
        second = self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        listing = self.req("GET", "/api/sessions?limit=100&offset=0", case)
        listed = {x.get("session_id") for x in ((safe_json(listing) or {}).get("sessions") or [])}
        a = [
            assertion("首次删除 204", first.status_code == 204, first.status_code),
            assertion("删后读 404", read_after.status_code == 404, read_after.status_code),
            assertion("重复删除 404", second.status_code == 404, second.status_code),
            assertion("列表已排除", sid not in listed, sid in listed),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": first.status_code, "read_after": read_after.status_code,
                       "repeat": second.status_code, "session_id": sid},
                      classification=None if ok else "产品缺陷")

    def h_session_rename(self, case):
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        if not sid:
            return result("FAIL", [assertion("创建会话成功", False, created.status_code)],
                          {"body": snippet(created.text, 160)}, classification="测试缺陷")
        title = "晨间问候"
        upd = self.req("PATCH", f"/api/sessions/{quote(sid)}", case, json={"title": title})
        listing = self.req("GET", "/api/sessions?limit=100&offset=0", case)
        found = next((x for x in ((safe_json(listing) or {}).get("sessions") or [])
                      if x.get("session_id") == sid), None)
        empty = self.req("PATCH", f"/api/sessions/{quote(sid)}", case, json={"title": "  "})
        a = [
            assertion("PATCH 200", upd.status_code == 200, upd.status_code),
            assertion("列表标题已更新", bool(found) and found.get("title") == title,
                      (found or {}).get("title")),
            assertion("空标题 400", empty.status_code == 400, empty.status_code),
        ]
        ok = all(x["ok"] for x in a)
        self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": upd.status_code, "empty_status": empty.status_code},
                      classification=None if ok else "产品缺陷")

    def h_session_delete_forbidden(self, case):
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        if not sid:
            return result("FAIL", [assertion("创建会话成功", False, created.status_code)],
                          {"body": snippet(created.text, 160)}, classification="测试缺陷")
        other = dict(case)
        owner = case["params"].get("account", "matrix_a")
        other["params"] = dict(case["params"],
                                account="matrix_a" if owner != "matrix_a" else "matrix_b",
                                sid=sid)
        denied_delete = self.req("DELETE", f"/api/sessions/{quote(sid)}", other)
        denied_rename = self.req("PATCH", f"/api/sessions/{quote(sid)}", other, json={"title": "越权"})
        a = [
            assertion("他人删除 403", denied_delete.status_code == 403, denied_delete.status_code),
            assertion("他人重命名 403", denied_rename.status_code == 403, denied_rename.status_code),
        ]
        ok = all(x["ok"] for x in a)
        self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        return result("PASS" if ok else "FAIL", a,
                      {"delete": denied_delete.status_code, "rename": denied_rename.status_code},
                      classification=None if ok else "产品缺陷")

    def h_session_ws_deleted(self, case):
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        created = self.req("POST", "/api/sessions", case, json={})
        sid = (safe_json(created) or {}).get("session_id")
        if not sid:
            return result("FAIL", [assertion("创建会话成功", False, created.status_code)],
                          {"body": snippet(created.text, 160)}, classification="测试缺陷")
        self.req("DELETE", f"/api/sessions/{quote(sid)}", case)
        token = self.tokens.get(case["params"].get("account", "matrix_a"), "")
        url = f"{GW_WS}/ws/chat/{sid}?token={quote(token)}"
        code, reason = None, ""
        try:
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                try:
                    ws.recv(timeout=5)
                except ConnectionClosed as e:
                    code = e.rcvd.code if e.rcvd else None
                    reason = e.rcvd.reason if e.rcvd else ""
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            reason = e.rcvd.reason if e.rcvd else ""
        except Exception as e:
            reason = str(e)
        ok = code == 1008 and "session_deleted" in (reason or "")
        return result("PASS" if ok else "FAIL",
                      [assertion("WS 关闭 1008 + session_deleted", ok, {"code": code, "reason": reason})],
                      {"code": code, "reason": reason, "session_id": sid},
                      classification=None if ok else "产品缺陷")

    def h_session_invalid_id(self, case):
        bad = case["params"]["bad_id"]
        r = self.req("POST", f"/api/sessions/{quote(bad, safe='')}/character", case,
                     data={"character_id": "preset_senior_sister"})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("网关 400", ok, r.status_code)],
                      {"http_status": r.status_code, "bad_id": bad, "body": snippet(r.text, 120)},
                      classification=None if ok else "产品缺陷")

    def h_session_bind_missing(self, case):
        sid = case["params"]["sid"]
        r = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                     data={"character_id": case["params"]["character_id"]})
        ok = r.status_code == 404
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 404", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      notes="" if ok else "绑定不存在角色返回 HTTP 200 + error",
                      classification=None if ok else "产品缺陷")

    def h_session_bind_invalid(self, case):
        sid = case["params"]["sid"]
        r = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                     data={"character_id": case["params"]["character_id"]})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("网关 400", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 120)},
                      classification=None if ok else "产品缺陷")

    def h_session_gap_unbind(self, case):
        r = self.req("DELETE", f"/api/sessions/{quote(case['params']['sid'])}/character", case)
        ok = r.status_code in (404, 405)
        return result("CAPABILITY_GAP" if ok else "PASS",
                      [assertion("解绑端点不存在", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 120)},
                      notes="角色只能覆盖绑定、不能解绑（GAP-03）" if ok else "",
                      classification="能力缺失" if ok else None)

    # ================ RL ================
    def h_rate_login(self, case):
        p = case["params"]
        statuses = []
        for _ in range(p["limit"] + 1):
            r = requests.post(f"{GW_BASE}/api/auth/login",
                              json={"username": "matrix_a", "password": "wrong-pass"},
                              headers={"X-Forwarded-For": p["ip"]}, timeout=10)
            statuses.append(r.status_code)
        a = [assertion(f"前 {p['limit']} 次 401", statuses[:p["limit"]] == [401] * p["limit"],
                       statuses[:p["limit"]]),
             assertion(f"第 {p['limit'] + 1} 次 429", statuses[-1] == 429, statuses[-1])]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"statuses": statuses},
                      classification=None if all(x["ok"] for x in a) else "产品缺陷")

    def h_rate_normal(self, case):
        p = case["params"]
        token = self.tokens.get("matrix_a", "")
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {token}", "X-Forwarded-For": p["ip"]})
        statuses = []
        for i in range(p["limit"] + 1):
            r = s.post(f"{GW_BASE}/api/webrtc/connect", params={"session_id": f"mx_rl_{i}"}, timeout=10)
            statuses.append(r.status_code)
        a = [assertion(f"前 {p['limit']} 次 200", statuses[:p["limit"]] == [200] * p["limit"],
                       statuses[:5]),
             assertion(f"第 {p['limit'] + 1} 次 429", statuses[-1] == 429, statuses[-1])]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"last_status": statuses[-1], "count": len(statuses)},
                      classification=None if all(x["ok"] for x in a) else "产品缺陷")

    def h_rate_expensive(self, case):
        p = case["params"]
        token = self.tokens.get("matrix_a", "")
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {token}", "X-Forwarded-For": p["ip"]})
        statuses = []
        for i in range(p["limit"] + 1):
            r = s.post(f"{GW_BASE}/api/voice/tts",
                       data={"session_id": f"mx_rl_tts_{i}", "text": ""}, timeout=15)
            statuses.append(r.status_code)
        a = [assertion(f"前 {p['limit']} 次 400（空文本）",
                       statuses[:p["limit"]] == [400] * p["limit"], statuses[:5]),
             assertion(f"第 {p['limit'] + 1} 次 429", statuses[-1] == 429, statuses[-1])]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"last_status": statuses[-1], "count": len(statuses)},
                      classification=None if all(x["ok"] for x in a) else "产品缺陷")

    # ---------- 维度辅助 ----------
    def dim_emotions(self):
        return self.dimensions["dimensions"]["emotion"]["types"]

    def dim_emotion(self, eid):
        for e in self.dim_emotions():
            if e["id"] == eid:
                return e
        return self.dim_emotions()[0]
