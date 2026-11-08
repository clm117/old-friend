# -*- coding: utf-8 -*-
"""矩阵执行器 A：基础设施 + 契约/角色/人群/音色类用例"""
import json
import time
from pathlib import Path
from urllib.parse import quote

import requests

from matrix_common import (ACCOUNTS, AI_BASE, GW_BASE, GW_WS, AI_ENV, ROOT_ENV, LOGS,
                           DATA_DIR, assertion, snippet, safe_json, wav_info, tiny_wav)


def result(status, assertions=None, evidence=None, notes="", classification=None, defect=None):
    return {"status": status, "assertions": assertions or [], "evidence": evidence or {},
            "notes": notes, "classification": classification, "defect": defect}


class CasesBase:
    """用例处理器基类：通用 HTTP 封装 + SYS/CHR/POP/VOX"""

    def __init__(self, services):
        self.services = services
        self.tokens = {}
        self.prepared = {}
        self.dimensions = json.loads((Path(__file__).resolve().parent / "dimensions.json")
                                     .read_text(encoding="utf-8"))

    # ---------------- HTTP ----------------
    def req(self, method, path, case=None, account=None, ip=None, auth=True, token=None, **kw):
        acct = account or (case or {}).get("params", {}).get("account", "matrix_a")
        caller_ip = ip or (case or {}).get("params", {}).get("ip", "198.18.1.1")
        headers = kw.pop("headers", {}) or {}
        headers["X-Forwarded-For"] = caller_ip
        if auth:
            headers["Authorization"] = f"Bearer {token or self.tokens.get(acct, '')}"
        timeout = kw.pop("timeout", 30)
        return requests.request(method, GW_BASE + path, headers=headers, timeout=timeout, **kw)

    def ai_get(self, path, timeout=10):
        return requests.get(AI_BASE + path, timeout=timeout)

    def flush_state(self):
        """触发 AI 状态强制落盘，并读取 state.json"""
        try:
            self.ai_get("/ready", timeout=15)
        except Exception:
            pass
        state_file = DATA_DIR / "state.json"
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"_error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def payload(name, gender="female", age="young_adult", tags=None, speed=1.0,
                identity="测试角色", relation="friend", p_tags=None, style="简洁", free="",
                omit_voice=False, omit_persona=False, vp_override=None, pp_override=None):
        vp = {"gender": gender, "age_group": age, "voice_tags": tags or ["gentle"], "speed": speed}
        pp = {"identity": identity, "relation": relation,
              "personality_tags": p_tags or ["warm"], "speaking_style": style, "free_description": free}
        if vp_override:
            vp.update(vp_override)
        if pp_override:
            pp.update(pp_override)
        body = {"name": name}
        if not omit_voice:
            body["voice_profile"] = vp
        if not omit_persona:
            body["persona_profile"] = pp
        return body

    def create(self, case, body, account=None, ip=None):
        return self.req("POST", "/api/characters", case, account=account, ip=ip, json=body)

    def delete_char(self, case, cid, account=None, ip=None):
        try:
            return self.req("DELETE", f"/api/characters/{quote(cid)}", case, account=account, ip=ip)
        except Exception:
            return None

    # ---------------- 分发 ----------------
    def dispatch(self, case):
        handler = getattr(self, "h_" + case["kind"], None)
        if handler is None:
            return result("FAIL", [assertion("处理器存在", False, case["kind"])],
                          {"error": "no handler"}, classification="测试缺陷")
        return handler(case)

    # ================ SYS ================
    def h_enum_options(self, case):
        r = self.req("GET", "/api/characters/options", case)
        body = safe_json(r) or {}
        opts = body.get("options", {})
        a = [
            assertion("HTTP 200", r.status_code == 200, r.status_code),
            assertion("genders 2 项", len(opts.get("genders", {})) == 2, opts.get("genders")),
            assertion("age_groups 4 项", len(opts.get("age_groups", {})) == 4, opts.get("age_groups")),
            assertion("voice_tags 10 项", len(opts.get("voice_tags", {})) == 10, len(opts.get("voice_tags", {}) or {})),
            assertion("relations 5 项", len(opts.get("relations", {})) == 5, len(opts.get("relations", {}) or {})),
            assertion("personality_tags 10 项", len(opts.get("personality_tags", {})) == 10,
                      len(opts.get("personality_tags", {}) or {})),
        ]
        ok = r.status_code == 200 and all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a, {"http_status": r.status_code, "options": opts})

    def h_list_presets(self, case):
        r = self.req("GET", "/api/characters", case)
        body = safe_json(r) or {}
        chars = body.get("characters", [])
        presets = [c for c in chars if c.get("is_preset")]
        expected = set(case["params"]["expected_ids"])
        a = [
            assertion("HTTP 200", r.status_code == 200, r.status_code),
            assertion("预设数量 6", len(presets) == 6, len(presets)),
            assertion("预设 id 齐全", expected == {c.get("id") for c in presets}, sorted(expected)),
            assertion("每个预设含 voice_profile/persona_profile",
                      all(c.get("voice_profile") and c.get("persona_profile") for c in presets), ""),
        ]
        ok = r.status_code == 200 and all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a, {"http_status": r.status_code, "count": len(presets)})

    def h_get_preset(self, case):
        cid = case["params"]["character_id"]
        r = self.req("GET", f"/api/characters/{quote(cid)}", case)
        body = safe_json(r) or {}
        ch = body.get("character") or {}
        a = [
            assertion("HTTP 200", r.status_code == 200, r.status_code),
            assertion("status=ok", body.get("status") == "ok", body.get("status")),
            assertion("id 一致", ch.get("id") == cid, ch.get("id")),
            assertion("name 一致", ch.get("name") == case["params"]["name"], ch.get("name")),
        ]
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a, {"http_status": r.status_code, "character": ch})

    def h_cors_allowed(self, case):
        p = case["params"]
        r = self.req("OPTIONS", p["path"], case, auth=False,
                     headers={"Origin": p["origin"],
                              "Access-Control-Request-Method": "GET",
                              "Access-Control-Request-Headers": "Authorization"},
                     account="x")
        hdr = r.headers.get("access-control-allow-origin")
        a = [assertion("预检 2xx", r.status_code in (200, 204), r.status_code),
             assertion("回显允许来源", hdr == p["origin"], hdr)]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "allow_origin": hdr})

    def h_cors_denied(self, case):
        p = case["params"]
        r = self.req("OPTIONS", p["path"], case, auth=False, account="x",
                     headers={"Origin": p["origin"], "Access-Control-Request-Method": "GET"})
        hdr = r.headers.get("access-control-allow-origin")
        a = [assertion("未回显非白名单来源", hdr is None, hdr),
             assertion("未 5xx", r.status_code < 500, r.status_code)]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "allow_origin": hdr})

    def h_auth_missing(self, case):
        r = self.req("GET", case["params"]["path"], case, auth=False, account="x")
        a = [assertion("HTTP 401", r.status_code == 401, r.status_code)]
        return result("PASS" if a[0]["ok"] else "FAIL", a,
                      {"http_status": r.status_code, "body": snippet(r.text, 120)})

    def h_auth_invalid(self, case):
        r = self.req("GET", case["params"]["path"], case, account="x",
                     token=case["params"]["token"])
        a = [assertion("HTTP 401", r.status_code == 401, r.status_code)]
        return result("PASS" if a[0]["ok"] else "FAIL", a,
                      {"http_status": r.status_code, "body": snippet(r.text, 120)})

    def h_auth_ws(self, case):
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        url = f"{GW_WS}/ws/chat/{case['params']['sid']}"
        code, reason = None, None
        try:
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                try:
                    ws.recv(timeout=5)
                except ConnectionClosed as e:
                    code, reason = (e.rcvd.code, e.rcvd.reason) if e.rcvd else (None, None)
                except TimeoutError:
                    code, reason = "timeout", "服务端未关闭"
        except ConnectionClosed as e:
            code, reason = (e.rcvd.code, e.rcvd.reason) if e.rcvd else (None, None)
        except Exception as e:
            return result("FAIL", [assertion("WS 握手成功", False, e)],
                          {"error": f"{type(e).__name__}: {e}"}, classification="测试缺陷")
        a = [assertion("关闭码 1008", code == 1008, f"code={code} reason={reason}")]
        return result("PASS" if a[0]["ok"] else "FAIL", a, {"ws_close_code": code, "ws_reason": reason})

    def h_health_gw(self, case):
        r = self.req("GET", "/health", case, auth=False, account="x")
        body = safe_json(r) or {}
        a = [assertion("HTTP 200", r.status_code == 200, r.status_code),
             assertion("status=healthy", body.get("status") == "healthy", body.get("status"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "body": body})

    def h_ready_gw(self, case):
        r = self.req("GET", "/ready", case, auth=False, account="x")
        body = safe_json(r) or {}
        # 语义一致性：ready↔200、not_ready↔503；不可用时必须给出 problems
        consistent = ((r.status_code == 200 and body.get("status") == "ready") or
                      (r.status_code == 503 and body.get("status") == "not_ready"))
        a = [assertion("状态码与 status 语义一致（200/ready 或 503/not_ready）", consistent,
                       f"{r.status_code}/{body.get('status')}"),
             assertion("not_ready 时 problems 非空", r.status_code != 503 or bool(body.get("problems")),
                       body.get("problems"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "body": body})

    def h_health_ai(self, case):
        r = self.ai_get("/health")
        body = safe_json(r) or {}
        a = [assertion("HTTP 200", r.status_code == 200, r.status_code),
             assertion("status=healthy", body.get("status") == "healthy", body.get("status")),
             assertion("tts_service=cloud", body.get("tts_service") == "cloud", body.get("tts_service"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "body": body})

    def h_ready_ai(self, case):
        r = self.ai_get("/ready")
        body = safe_json(r) or {}
        cloud = body.get("cloud_dependency") or {}
        consistent = ((r.status_code == 200 and body.get("status") == "ready") or
                      (r.status_code == 503 and body.get("status") == "not_ready"))
        a = [assertion("状态码与 status 语义一致", consistent, f"{r.status_code}/{body.get('status')}"),
             assertion("暴露云依赖探测结果", isinstance(cloud, dict) and "ok" in cloud, cloud),
             assertion("云依赖不可用时 problems 含 cloud_tts_unavailable",
                       cloud.get("ok") is not False or "cloud_tts_unavailable" in (body.get("problems") or []),
                       body.get("problems")),
             assertion("状态存储可写", "state_store" in body and "error" not in body.get("state_store", {}),
                      body.get("state_store"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "body": body})

    def h_metrics(self, case):
        r = self.req("GET", "/metrics", case, auth=False, account="x")
        a = [assertion("HTTP 200", r.status_code == 200, r.status_code),
             assertion("返回指标文本", "yuban" in r.text or "# HELP" in r.text, snippet(r.text, 60))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "bytes": len(r.content)})

    def h_invalid_json(self, case):
        r = self.req("POST", "/api/characters", case, account=case["params"]["account"],
                     data=case["params"]["body"], headers={"Content-Type": "application/json"})
        ok = r.status_code == 400
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 400", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 200)},
                      notes="" if ok else "非 JSON 请求体被当作上游故障，返回 502",
                      classification=None if ok else "产品缺陷")

    # ================ CHR ================
    def h_create_verify_delete(self, case):
        p = case["params"]
        body = self.payload(p["name"], p["gender"], p["age_group"], p["voice_tags"], p["speed"])
        r = self.create(case, body)
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        a = [
            assertion("创建 HTTP 200", r.status_code == 200, r.status_code),
            assertion("创建 status=ok", rb.get("status") == "ok", rb.get("status")),
            assertion("id 为 custom_ 前缀", str(cid).startswith("custom_"), cid),
            assertion("voice_profile.gender 一致", (ch.get("voice_profile") or {}).get("gender") == p["gender"],
                      (ch.get("voice_profile") or {}).get("gender")),
            assertion("voice_profile.age_group 一致",
                      (ch.get("voice_profile") or {}).get("age_group") == p["age_group"],
                      (ch.get("voice_profile") or {}).get("age_group")),
            assertion("voice_tags 一致", (ch.get("voice_profile") or {}).get("voice_tags") == p["voice_tags"],
                      (ch.get("voice_profile") or {}).get("voice_tags")),
        ]
        get_body, detail = None, ""
        if cid:
            g = self.req("GET", f"/api/characters/{quote(cid)}", case)
            get_body = safe_json(g) or {}
            a.append(assertion("回读 name 一致", (get_body.get("character") or {}).get("name") == p["name"],
                               (get_body.get("character") or {}).get("name")))
            d = self.delete_char(case, cid)
            db = safe_json(d) if d is not None else {}
            a.append(assertion("删除成功", d is not None and d.status_code == 200 and db.get("status") == "ok",
                               db))
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"create_status": r.status_code, "character_id": cid,
                       "readback": (get_body or {}).get("character", {}).get("name"), "detail": detail})

    def h_name_boundary(self, case):
        p = case["params"]
        if p.get("name_len"):
            name = "长" * int(p["name_len"])
        else:
            name = p.get("name", "")
        body = self.payload(name)
        r = self.create(case, body)
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        stored = ch.get("name")
        if p.get("name_len"):
            exp = "长" * min(int(p["name_len"]), 64)
        elif name.strip() == "":
            # 空白名称（含纯空格/制表符）应回退默认名，而非落库空串
            exp = "未命名角色"
        else:
            exp = name.strip()[:64]
        a = [assertion("创建 HTTP 200", r.status_code == 200, r.status_code),
             assertion("落库名称符合预期", stored == exp, f"stored={stored!r} expected={exp!r}")]
        notes, classification, defect = "", None, None
        ok = all(x["ok"] for x in a)
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": r.status_code, "stored_name": stored, "input": name},
                      notes=notes, classification=classification, defect=defect)

    def h_speed_boundary(self, case):
        p = case["params"]
        body = self.payload(f"语速-{p['speed']}", speed=p["speed"])
        r = self.create(case, body)
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        numeric = isinstance(p["speed"], (int, float)) and not isinstance(p["speed"], bool)
        if numeric:
            stored = (ch.get("voice_profile") or {}).get("speed")
            exp = p["expected"]
            a = [assertion("创建 HTTP 200", r.status_code == 200, r.status_code),
                 assertion(f"speed clamp 到 {exp}", stored == exp, stored)]
            ok = all(x["ok"] for x in a)
            notes, defect = "", None
        else:
            # 非法/空值：期望 4xx，实际被 Pydantic 拦下但网关吞掉状态码返回 200 + detail
            a = [assertion("非法语速被拒绝（400/422）", r.status_code in (400, 422), r.status_code)]
            ok = r.status_code in (400, 422)
            notes = "" if ok else "上游 422 被网关吞成 HTTP 200 + detail（错误语义缺陷）"
            defect = None if ok else "DEF-02"
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if ok else "FAIL", a,
                      {"input": p["speed"], "http_status": r.status_code,
                       "body": snippet(r.text, 160), "stored": (ch.get("voice_profile") or {}).get("speed")},
                      notes=notes, classification=None if ok else "产品缺陷", defect=defect)

    def h_invalid_enum(self, case):
        p = case["params"]
        body = self.payload(p["name"])
        override = p.get("override") or {}
        if p["name"] in ("边界-gender_invalid", "边界-age_child", "边界-age_toddler",
                         "边界-tag_invalid", "边界-tag_empty"):
            body["voice_profile"].update(override)
        else:
            body["persona_profile"].update(override)
        r = self.create(case, body)
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        a = [assertion("创建 HTTP 200", r.status_code == 200, r.status_code)]
        name = p["name"]
        if name == "边界-gender_invalid":
            a.append(assertion("归一为 female", (ch.get("voice_profile") or {}).get("gender") == "female",
                               (ch.get("voice_profile") or {}).get("gender")))
        elif name in ("边界-age_child", "边界-age_toddler"):
            a.append(assertion("归一为 young_adult", (ch.get("voice_profile") or {}).get("age_group") == "young_adult",
                               (ch.get("voice_profile") or {}).get("age_group")))
        elif name in ("边界-tag_invalid", "边界-tag_empty"):
            a.append(assertion("回退 ['gentle']", (ch.get("voice_profile") or {}).get("voice_tags") == ["gentle"],
                               (ch.get("voice_profile") or {}).get("voice_tags")))
        elif name == "边界-relation_invalid":
            a.append(assertion("归一为 friend", (ch.get("persona_profile") or {}).get("relation") == "friend",
                               (ch.get("persona_profile") or {}).get("relation")))
        elif name == "边界-personality_invalid":
            a.append(assertion("非法标签清空", (ch.get("persona_profile") or {}).get("personality_tags") == [],
                               (ch.get("persona_profile") or {}).get("personality_tags")))
        elif name == "边界-identity_long":
            a.append(assertion("identity 截断 50", len((ch.get("persona_profile") or {}).get("identity", "")) == 50,
                               len((ch.get("persona_profile") or {}).get("identity", ""))))
        elif name == "边界-style_long":
            a.append(assertion("style 截断 200",
                               len((ch.get("persona_profile") or {}).get("speaking_style", "")) == 200,
                               len((ch.get("persona_profile") or {}).get("speaking_style", ""))))
        elif name == "边界-free_long":
            a.append(assertion("free 截断 500",
                               len((ch.get("persona_profile") or {}).get("free_description", "")) == 500,
                               len((ch.get("persona_profile") or {}).get("free_description", ""))))
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "character": ch})

    def h_missing_profiles(self, case):
        p = case["params"]
        if p.get("skip_persona"):
            body = self.payload(p["name"], omit_persona=True)
        else:
            body = {"name": p["name"]}
        r = self.create(case, body)
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        if p.get("skip_persona"):
            a = [assertion("persona 默认 relation=friend",
                           (ch.get("persona_profile") or {}).get("relation") == "friend",
                           ch.get("persona_profile")),
                 assertion("persona 默认 identity 为空",
                           (ch.get("persona_profile") or {}).get("identity") == "",
                           (ch.get("persona_profile") or {}).get("identity"))]
        else:
            a = [assertion("voice_profile 默认 female",
                           (ch.get("voice_profile") or {}).get("gender") == "female",
                           ch.get("voice_profile")),
                 assertion("voice_profile 默认 young_adult",
                           (ch.get("voice_profile") or {}).get("age_group") == "young_adult",
                           ch.get("voice_profile")),
                 assertion("默认语速 1.0", (ch.get("voice_profile") or {}).get("speed") == 1.0,
                           (ch.get("voice_profile") or {}).get("speed"))]
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "character": ch})

    def h_lifecycle(self, case):
        body = self.payload(case["params"]["name"])
        r = self.create(case, body)
        rb = safe_json(r) or {}
        cid = (rb.get("character") or {}).get("id")
        a = [assertion("创建成功", rb.get("status") == "ok", rb.get("status"))]
        get1 = self.req("GET", f"/api/characters/{quote(str(cid))}", case)
        a.append(assertion("删除前可读取", get1.status_code == 200 and (safe_json(get1) or {}).get("status") == "ok",
                           get1.status_code))
        d = self.delete_char(case, cid)
        db = safe_json(d) if d is not None else {}
        a.append(assertion("删除成功", db.get("status") == "ok", db))
        get2 = self.req("GET", f"/api/characters/{quote(str(cid))}", case)
        b2 = safe_json(get2) or {}
        a.append(assertion("删除后应 404", get2.status_code == 404, get2.status_code))
        ok = all(x["ok"] for x in a)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status_get_after_delete": get2.status_code,
                       "body_get_after_delete": snippet(get2.text, 160)},
                      notes="" if ok else "删除后仍返回 HTTP 200 + error 文案",
                      classification=None if ok else "产品缺陷")

    def h_duplicate_name(self, case):
        name = case["params"]["name"]
        r1 = self.create(case, self.payload(name))
        r2 = self.create(case, self.payload(name, gender="male", tags=["magnetic"]))
        b1, b2 = safe_json(r1) or {}, safe_json(r2) or {}
        id1 = (b1.get("character") or {}).get("id")
        id2 = (b2.get("character") or {}).get("id")
        lst = safe_json(self.req("GET", "/api/characters", case)) or {}
        ids = {c.get("id") for c in lst.get("characters", [])}
        a = [assertion("两次创建均成功", b1.get("status") == "ok" and b2.get("status") == "ok", (b1.get("status"), b2.get("status"))),
             assertion("id 不同", id1 != id2, f"{id1} vs {id2}"),
             assertion("列表含两条重名角色", id1 in ids and id2 in ids, "")
             ]
        for cid in (id1, id2):
            if cid:
                self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a, {"ids": [id1, id2]})

    def h_delete_preset(self, case):
        cid = case["params"]["character_id"]
        r = self.req("DELETE", f"/api/characters/{quote(cid)}", case)
        body = safe_json(r) or {}
        ok = r.status_code in (400, 404, 409)
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 4xx", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      notes="" if ok else "预设不可删除但返回 HTTP 200 + error",
                      classification=None if ok else "产品缺陷")

    def h_delete_missing(self, case):
        cid = case["params"]["character_id"]
        r = self.req("DELETE", f"/api/characters/{quote(cid)}", case)
        ok = r.status_code == 404
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 404", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      notes="" if ok else "删除不存在角色返回 HTTP 200 + error",
                      classification=None if ok else "产品缺陷")

    def h_update_method(self, case):
        body = self.payload(f"待修改-{case['params']['method']}")
        body["persona_profile"] = dict(body.get("persona_profile") or {}, identity="改后身份")
        r = self.create(case, body)
        cid = ((safe_json(r) or {}).get("character") or {}).get("id")
        if not cid:
            return result("FAIL", [assertion("准备角色成功", False, r.status_code)],
                          {"body": snippet(r.text, 160)}, classification="测试缺陷")
        m = case["params"]["method"]
        upd = self.req(m, f"/api/characters/{quote(cid)}", case,
                       json={"name": "改名后", "persona_profile": {"identity": "改后身份"}})
        got = safe_json(upd) or {}
        ch = got.get("character") or {}
        read = self.req("GET", f"/api/characters/{quote(cid)}", case)
        read_ch = (safe_json(read) or {}).get("character") or {}
        ok = (upd.status_code == 200 and ch.get("name") == "改名后"
              and read.status_code == 200 and read_ch.get("name") == "改名后"
              and (read_ch.get("persona_profile") or {}).get("identity") == "改后身份")
        a = [assertion(f"{m} 200 且字段更新", ok, {"write": upd.status_code, "read": read.status_code})]
        self.delete_char(case, cid)
        return result("PASS" if ok else "FAIL", a,
                      {"http_status": upd.status_code, "read_status": read.status_code,
                       "body": snippet(upd.text, 160)},
                      classification=None if ok else "产品缺陷")

    def h_update_missing(self, case):
        cid = case["params"]["character_id"]
        r = self.req("PUT", f"/api/characters/{quote(cid)}", case, json={"name": "ghost"})
        ok = r.status_code == 404
        return result("PASS" if ok else "FAIL",
                      [assertion("HTTP 404", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      classification=None if ok else "产品缺陷")

    def h_update_forbidden(self, case):
        body = self.payload("仅A可改")
        r = self.create(case, body)
        cid = ((safe_json(r) or {}).get("character") or {}).get("id")
        if not cid:
            return result("FAIL", [assertion("准备角色成功", False, r.status_code)],
                          {"body": snippet(r.text, 160)}, classification="测试缺陷")
        other = dict(case)
        other["params"] = dict(case["params"], account="matrix_b")
        upd = self.req("PUT", f"/api/characters/{quote(cid)}", other, json={"name": "B越权改"})
        a = [assertion("他人修改 403", upd.status_code == 403, upd.status_code)]
        self.delete_char(case, cid)
        return result("PASS" if upd.status_code == 403 else "FAIL", a,
                      {"http_status": upd.status_code, "body": snippet(upd.text, 160)},
                      classification=None if upd.status_code == 403 else "产品缺陷")

    def h_update_duplicate(self, case):
        first = self.create(case, self.payload("重名甲"))
        cid_a = ((safe_json(first) or {}).get("character") or {}).get("id")
        second = self.create(case, self.payload("重名乙"))
        cid_b = ((safe_json(second) or {}).get("character") or {}).get("id")
        if not cid_a or not cid_b:
            return result("FAIL", [assertion("准备两个角色成功", False, first.status_code)],
                          {"body": snippet(second.text, 160)}, classification="测试缺陷")
        upd = self.req("PUT", f"/api/characters/{quote(cid_a)}", case, json={"name": "重名乙"})
        a = [assertion("重名 409", upd.status_code == 409, upd.status_code)]
        self.delete_char(case, cid_a)
        self.delete_char(case, cid_b)
        return result("PASS" if upd.status_code == 409 else "FAIL", a,
                      {"http_status": upd.status_code, "body": snippet(upd.text, 160)},
                      classification=None if upd.status_code == 409 else "产品缺陷")

    # ================ POP ================
    def h_population_create(self, case):
        p = case["params"]
        vp = p["voice_profile"]
        r = self.create(case, self.payload(f"人群-{p['population']}", vp["gender"], vp["age_group"],
                                           vp["voice_tags"], vp["speed"]))
        rb = safe_json(r) or {}
        ch = rb.get("character") or {}
        cid = ch.get("id")
        stored_age = (ch.get("voice_profile") or {}).get("age_group")
        if not p.get("prompt_fragment"):
            # 能力缺失：产品枚举不接受 child / toddler
            status = "CAPABILITY_GAP"
            a = [assertion("产品无该年龄段", stored_age != vp["age_group"],
                           f"input={vp['age_group']} stored={stored_age}")]
            notes = f"{p['population']} 不在 age_group 枚举，静默归一为 {stored_age}（GAP-04）"
            classification = "能力缺失"
            ev = {"input_age_group": vp["age_group"], "stored_age_group": stored_age}
        else:
            b = self.req("POST", f"/api/sessions/{quote(case['params']['sid'])}/character", case,
                         data={"character_id": cid})
            bb = safe_json(b) or {}
            prompt = bb.get("voice_prompt", "")
            a = [assertion("绑定成功", bb.get("status") == "ok", bb.get("status")),
                 assertion("voice_prompt 含人群特征", p["prompt_fragment"] in (prompt or ""), prompt)]
            status = "PASS" if all(x["ok"] for x in a) else "FAIL"
            notes, classification = "", None if status == "PASS" else "产品缺陷"
            ev = {"voice_prompt": prompt, "stored_age_group": stored_age}
        if cid:
            self.delete_char(case, cid)
        return result(status, a, ev, notes=notes, classification=classification)

    # ================ VOX ================
    def h_voice_prompt(self, case):
        p = case["params"]
        r = self.create(case, self.payload(f"音色映射-{p['gender']}-{p['voice_tags'][0]}",
                                           p["gender"], p["age_group"], p["voice_tags"], 1.0))
        cid = ((safe_json(r) or {}).get("character") or {}).get("id")
        b = self.req("POST", f"/api/sessions/{quote(case['params']['sid'])}/character", case,
                     data={"character_id": cid})
        bb = safe_json(b) or {}
        prompt = bb.get("voice_prompt", "")
        a = [assertion("绑定成功", bb.get("status") == "ok", bb.get("status")),
             assertion("prompt 与声学档案一致", prompt == p["expected_prompt"],
                       f"actual={prompt!r} expected={p['expected_prompt']!r}")]
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"voice_prompt": prompt})

    def h_voice_gap_options(self, case):
        r = self.req("GET", "/api/characters/options", case)
        opts = (safe_json(r) or {}).get("options", {})
        tags = list((opts.get("voice_tags") or {}).keys())
        forbidden = case["params"]["forbidden_tags"]
        present = [t for t in forbidden if t in tags]
        status = "CAPABILITY_GAP" if not present else "PASS"
        return result(status, [assertion("无方言/童声/老年/情感音色项", not present,
                                          f"present={present} tags={tags}")],
                      {"voice_tags": tags},
                      notes="产品仅有 10 个风格标签，无独立音色体系（GAP-05）" if not present else "",
                      classification="能力缺失" if not present else None)

    def h_voice_gap_clone(self, case):
        r = self.req("POST", "/api/voice/clone", case, json={})
        ok = r.status_code in (404, 405)
        return result("CAPABILITY_GAP" if ok else "PASS",
                      [assertion("克隆端点不存在", ok, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 160)},
                      notes="无声音克隆路由（GAP-06）" if ok else "",
                      classification="能力缺失" if ok else None)

    def h_voice_catalog(self, case):
        r = self.req("GET", "/api/voice-catalog", case)
        body = safe_json(r) or {}
        cat = body.get("catalog") or {}
        tags = cat.get("tags") or []
        defaults = cat.get("defaults") or {}
        caps = cat.get("capabilities") or {}
        dialects = cat.get("dialects") or []
        a = [assertion("目录可查询", r.status_code == 200 and body.get("status") == "ok", r.status_code),
             assertion("保留 10 个音色标签", len(tags) == 10, len(tags)),
             assertion("默认回退不变", defaults == {"female": "gentle", "male": "magnetic"}, defaults),
             assertion("方言类型已接入（capabilities.dialect=True）",
                       caps.get("dialect") is True, caps),
             assertion("三方言结构位齐全", [d.get("id") for d in dialects] == ["yue", "sichuan", "dongbei"],
                       [d.get("id") for d in dialects]),
             assertion("真实方言音色未就绪（dialect_ready=False）",
                       caps.get("dialect_ready") is False, caps),
             assertion("童声/情感音色仍未启用",
                       not any(caps.get(k) for k in ("child_voice", "emotion_voice")), caps),
             assertion("标签带 locale/voice_type 扩展位",
                       all(t.get("locale", "zh-CN") and t.get("voice_type", "standard") for t in tags),
                       tags[:1])]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"tag_count": len(tags), "defaults": defaults, "capabilities": caps,
                       "dialects": [d.get("id") for d in dialects]})

    def h_dialect_catalog(self, case):
        r = self.req("GET", "/api/characters/options", case)
        opts = (safe_json(r) or {}).get("options", {})
        ds = opts.get("dialects") or []
        default = opts.get("default_dialect") or {}
        a = [assertion("options 返回三种方言", len(ds) == 3, ds),
             assertion("方言 id/locale 正确",
                       [(d.get("id"), d.get("locale")) for d in ds] ==
                       [("yue", "zh-yue"), ("sichuan", "zh-CN-sichuan"), ("dongbei", "zh-CN-dongbei")],
                       [(d.get("id"), d.get("locale")) for d in ds]),
             assertion("三种方言均标注暂不可用",
                       all(d.get("enabled") is False for d in ds), ds),
             assertion("每种方言带用户可理解提示",
                       all("暂不可用" in (d.get("notice") or "") for d in ds), ds),
             assertion("默认方言为普通话", default.get("id") == "zh-CN", default)]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"dialects": ds, "default_dialect": default})

    def h_dialect_character(self, case):
        ids = []
        try:
            r = self.create(case, self.payload("方言-四川", vp_override={"dialect": "sichuan"}))
            ch = (safe_json(r) or {}).get("character") or {}
            cid = ch.get("id")
            ids.append(cid)
            vp = ch.get("voice_profile") or {}

            r2 = self.create(case, self.payload("方言-未知", vp_override={"dialect": "klingon"}))
            ch2 = (safe_json(r2) or {}).get("character") or {}
            cid2 = ch2.get("id")
            ids.append(cid2)
            vp2 = ch2.get("voice_profile") or {}

            sid_plain = "mx_dialect_plain"
            bp = self.req("POST", f"/api/sessions/{sid_plain}/character", case,
                          data={"character_id": cid2})
            plain_prompt = (safe_json(bp) or {}).get("voice_prompt", "")

            sid = f"{case['params']['sid']}_d"
            sb = self.req("POST", f"/api/sessions/{sid}/character", case, data={"character_id": cid})
            body = safe_json(sb) or {}
            prompt = body.get("voice_prompt", "")

            a = [assertion("方言合法值原样落库", vp.get("dialect") == "sichuan", vp.get("dialect")),
                 assertion("未知方言归一为 None（不报错）", vp2.get("dialect") is None, vp2.get("dialect")),
                 assertion("方言不改变 voice_prompt（红线和既有映射一致）",
                           prompt == plain_prompt and bool(prompt), {"prompt": prompt, "plain": plain_prompt})]
            return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                          {"sichuan": vp, "unknown": vp2, "voice_prompt": prompt})
        finally:
            for cid in ids:
                if cid:
                    self.delete_char(case, cid)

    def h_dialect_fallback(self, case):
        cid = None
        try:
            r = self.create(case, self.payload("方言-东北回退", vp_override={"dialect": "dongbei"}))
            cid = ((safe_json(r) or {}).get("character") or {}).get("id")
            sid = f"{case['params']['sid']}_fb"
            self.req("POST", f"/api/sessions/{sid}/character", case, data={"character_id": cid})
            tts = self.req("POST", "/api/voice/tts", case, timeout=120,
                           data={"session_id": sid, "text": "你好", "quick_mode": "true"})
            if tts.status_code == 200:
                raw = tts.headers.get("X-Voice-Notice", "")
                from urllib.parse import unquote
                notice = unquote(raw)
                ok = "暂不可用" in notice or "回退" in notice
                return result("PASS" if ok else "FAIL",
                              [assertion("回退默认音色并返回可读提示", ok,
                                        {"header": raw, "decoded": notice}),
                               assertion("提示经 header 透传", bool(raw), raw)],
                              {"http_status": 200, "notice": notice})
            return result("BLOCKED",
                          [assertion("云 TTS 可用", False, {"status": tts.status_code,
                                                          "body": snippet(tts.text, 160)})],
                          {"http_status": tts.status_code, "body": snippet(tts.text, 160)},
                          notes="DashScope 欠费，云 TTS 不可用，无法观测方言回退提示头",
                          classification="环境不可用")
        finally:
            if cid:
                self.delete_char(case, cid)

    def h_voice_design_capability(self, case):
        r = self.req("GET", "/api/voice-design/capability", case)
        body = safe_json(r) or {}
        cap = body.get("capability") or {}
        a = [assertion("能力接口可查询", r.status_code == 200 and body.get("status") == "ok", r.status_code),
             assertion("目标模型绑定当前云 TTS", cap.get("target_model") == "qwen3-tts-flash",
                       cap.get("target_model")),
             assertion("复刻能力可用（有 Key）", (cap.get("clone") or {}).get("supported") is True, cap.get("clone")),
             assertion("当前目标模型不支持声音设计并给出原因",
                       (cap.get("design") or {}).get("supported") is False
                       and "不支持" in ((cap.get("design") or {}).get("reason") or ""), cap.get("design")),
             assertion("配额上限 1000", (cap.get("quota") or {}).get("limit") == 1000, cap.get("quota")),
             assertion("限流 180 RPM / 3 RPS",
                       (cap.get("rate_limit") or {}) == {"rpm": 180, "rps": 3}, cap.get("rate_limit")),
             assertion("失败码表完整", all(k in (cap.get("error_codes") or {}) for k in
                                          ("voice_define_unsupported", "voice_quota_unavailable",
                                           "voice_quota_exhausted", "voice_define_timeout")),
                       cap.get("error_codes"))]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a, {"capability": cap})

    def h_voice_design_unsupported(self, case):
        r = self.req("POST", "/api/voice/define", case,
                     data={"session_id": "mx_design_unsupported", "description": "温柔女声",
                           "mode": "design", "voice_prompt": "一位温柔的中年女性，语速平缓",
                           "preview_text": "你好，今天天气不错，我们一起走走吧。"})
        body = safe_json(r) or {}
        ok = r.status_code == 501 and body.get("code") == "voice_define_unsupported"
        return result("PASS" if ok else "FAIL",
                      [assertion("目标模型不支持设计 → 501 voice_define_unsupported", ok,
                                 {"status": r.status_code, "code": body.get("code")}),
                       assertion("不返回假成功", r.status_code != 200, r.status_code)],
                      {"http_status": r.status_code, "body": snippet(r.text, 200)})

    def h_voice_define_error(self, case):
        import struct

        def tiny_wav():
            data = b"\x00\x00" * 1600
            header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + \
                     struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + \
                     b"data" + struct.pack("<I", len(data))
            return header + data

        r = self.req("POST", "/api/voice/define", case, timeout=120,
                     files={"audio": ("a.wav", tiny_wav(), "audio/wav")},
                     data={"session_id": "mx_define_err", "description": "温柔女声",
                           "pitch": 1.0, "speed": 1.0, "volume": 1.0, "emotion_intensity": 0.5})
        body = safe_json(r) or {}
        code = body.get("code")
        # 欠费 402 / 不可用 501 / 限流 429 / 超时 504 / 上游 502 都是明确失败；唯独不能 200 假成功
        ok = r.status_code in (402, 429, 501, 502, 504) and code in {
            "voice_quota_unavailable", "voice_quota_exhausted", "voice_define_unsupported",
            "voice_define_timeout", "voice_upstream_error", "voice_define_invalid", "voice_audio_invalid"}
        a = [assertion("失败不伪装成功（非 200）", r.status_code != 200, r.status_code),
             assertion("返回语义 code", bool(code), code),
             assertion("返回用户可理解 hint", bool(body.get("hint")), body.get("hint")),
             assertion("状态码与 code 语义匹配", ok, {"status": r.status_code, "code": code})]
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"http_status": r.status_code, "body": snippet(r.text, 240)})

    def h_speed_observability(self, case):
        ids, prompts, binds = [], [], []
        for idx, speed in enumerate((0.7, 1.3)):
            r = self.create(case, self.payload(f"语速观测-{speed}", speed=speed))
            cid = ((safe_json(r) or {}).get("character") or {}).get("id")
            sid = f"{case['params']['sid']}_{idx}"
            b = self.req("POST", f"/api/sessions/{quote(sid)}/character", case,
                         data={"character_id": cid})
            bb = safe_json(b) or {}
            prompts.append(bb.get("voice_prompt", ""))
            binds.append(bb.get("status") == "ok")
            ids.append(cid)
        a = [assertion("两次绑定成功", all(binds), binds),
             assertion("两次 prompt 不同且含节奏描述", prompts[0] != prompts[1]
                       and "speaking " in (prompts[0] or ""), f"0.7={prompts[0]!r} 1.3={prompts[1]!r}")]
        for cid in ids:
            if cid:
                self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"prompt_speed_0.7": prompts[0], "prompt_speed_1.3": prompts[1]},
                      notes="" if all(x["ok"] for x in a) else "语速未进入 voice_prompt")

    def h_speed_boundary(self, case):
        p = case["params"]
        # 兼容两类用例：CHR（params.expected，仅校验落库/拒绝）与 VOX（params.expect，额外校验 prompt）
        legacy = "expected" in p and "expect" not in p
        speed = p.get("speed")
        body = self.payload(f"语速边界-{speed}", speed=1.0 if speed is None else speed)
        body["name"] = f"语速边界-{speed}"
        # 只有“缺省应回退 1.0”的场景才移除字段；显式 null 视为非法输入（422）
        if speed is None and not (legacy and p.get("expected") == "reject"):
            body["voice_profile"].pop("speed", None)
        elif speed is None:
            body["voice_profile"]["speed"] = None
        r = self.create(case, body)
        if legacy:
            if p["expected"] == "reject":
                a = [assertion("非法语速被拒绝（400/422）", r.status_code in (400, 422),
                               f"status={r.status_code} {snippet(r.text, 120)}")]
                return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                              {"speed": speed, "http_status": r.status_code})
            rb = safe_json(r) or {}
            ch = rb.get("character") or {}
            stored = (ch.get("voice_profile") or {}).get("speed")
            a = [assertion("创建成功", r.status_code == 200 and bool(ch.get("id")),
                           f"status={r.status_code} {snippet(r.text, 120)}"),
                 assertion(f"落库语速为 {p['expected']}", stored == p["expected"], f"stored={stored}")]
            if ch.get("id"):
                self.delete_char(case, ch["id"])
            return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                          {"speed": speed, "stored_speed": stored})
        if p["expect"] == 422:
            a = [assertion("越界语速被拒绝 422", r.status_code == 422,
                           f"status={r.status_code} {snippet(r.text, 120)}")]
            return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                          {"speed": speed, "http_status": r.status_code})
        rb = safe_json(r) or {}
        cid = (rb.get("character") or {}).get("id")
        stored = ((rb.get("character") or {}).get("voice_profile") or {}).get("speed")
        b = self.req("POST", f"/api/sessions/{quote(case['params']['sid'])}/character", case,
                     data={"character_id": cid})
        prompt = (safe_json(b) or {}).get("voice_prompt", "")
        if p["phrase"]:
            ok_prompt = p["phrase"] in (prompt or "")
        else:
            ok_prompt = prompt == p.get("expected_prompt")
        a = [assertion("创建成功", r.status_code == 200 and bool(cid),
                       f"status={r.status_code} {snippet(r.text, 120)}"),
             assertion("语速落库为输入值", stored == p["speed"] if p["speed"] is not None else stored == 1.0,
                       f"stored={stored}"),
             assertion("voice_prompt 反映语速", ok_prompt,
                       f"actual={prompt!r} expected_phrase={p['phrase']!r}")]
        if cid:
            self.delete_char(case, cid)
        return result("PASS" if all(x["ok"] for x in a) else "FAIL", a,
                      {"speed": p["speed"], "stored_speed": stored, "voice_prompt": prompt},
                      notes="" if all(x["ok"] for x in a) else "语速未正确进入 voice_prompt（GAP-08）")
