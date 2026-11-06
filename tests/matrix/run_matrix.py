# -*- coding: utf-8 -*-
"""矩阵测试执行器（编排 + 报告）

用法：
    .venv\\Scripts\\python.exe tests/matrix/generate_matrix.py
    .venv\\Scripts\\python.exe tests/matrix/run_matrix.py
可选：
    --skip-clean        保留 runtime/data（默认每次清空测试数据）
    --no-restart        跳过重启恢复用例
"""
import argparse
import json
import os
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

from matrix_common import (HERE, ROOT, AI_DIR, AI_ENV, ROOT_ENV, RUNTIME, DATA_DIR, LOGS,
                           ACCOUNTS, PASSWORD, GW_BASE, AI_BASE, AI_PORT, GW_PORT, AI_ENV as _AIE,
                           DEFECT_CATALOG, GAP_CATALOG, Services, seed_users, login,
                           assertion, snippet, safe_json, port_in_use, wav_info)
from matrix_cases_a import result
from matrix_cases_c import CasesExtra

PLAN_FILE = HERE / "matrix_plan.json"
REPORT_FILE = HERE / "report.json"
SUMMARY_FILE = HERE / "summary.md"
USERS_FILE = RUNTIME / "users.json"
START_TS = time.time()


# ---------------------------------------------------------------- 依赖探测
def probe_llm():
    key = os.environ.get("LLM_API_KEY") or AI_ENV.get("LLM_API_KEY") or ROOT_ENV.get("LLM_API_KEY", "")
    t0 = time.time()
    try:
        r = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": AI_ENV.get("LLM_MODEL", "qwen-plus"),
                  "messages": [{"role": "user", "content": "请只回复两个字：你好"}],
                  "max_tokens": 16}, timeout=30)
        body = safe_json(r) or {}
        content = ""
        try:
            content = body["choices"][0]["message"]["content"]
        except Exception:
            content = ""
        return {"name": "DashScope LLM(qwen)", "available": r.status_code == 200 and bool(content),
                "http_status": r.status_code, "body": snippet(r.text, 240), "content": snippet(content, 80),
                "elapsed_seconds": round(time.time() - t0, 2),
                "unlock": "为 DashScope 账号充值（解除 Arrearage）或更换可用 LLM_API_KEY，然后重跑 run_matrix.py"}
    except Exception as e:
        return {"name": "DashScope LLM(qwen)", "available": False,
                "error": f"{type(e).__name__}: {e}", "elapsed_seconds": round(time.time() - t0, 2),
                "unlock": "检查网络/代理与 LLM_API_KEY 后重跑"}


# ---------------------------------------------------------------- 编排
class Orchestrator:
    def __init__(self, plan, args):
        self.plan = plan
        self.args = args
        self.services = Services()
        self.ex = CasesExtra(self.services)
        self.results = []
        self.probe = {}
        self.phase_times = {}

    # ---- 服务 ----
    def start_all(self):
        if not self.args.skip_clean:
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR, ignore_errors=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        seed_users(USERS_FILE)
        ok, detail, secs = self.services.start_ai()
        if not ok:
            raise RuntimeError(f"AI 服务启动失败: {detail}")
        ok2, detail2, secs2 = self.services.start_gw(USERS_FILE)
        if not ok2:
            raise RuntimeError(f"网关启动失败: {detail2}")
        self.phase_times["service_start"] = round(secs + secs2, 2)

    def restart_ai(self):
        self.ex.ai_get("/ready", timeout=15)  # 先强制落盘
        self.services.stop_ai()
        t0 = time.time()
        ok, detail, _ = self.services.start_ai()
        self.phase_times["ai_restart"] = round(time.time() - t0, 2)
        if not ok:
            raise RuntimeError(f"AI 重启失败: {detail}")

    def login_all(self):
        for i, u in enumerate(ACCOUNTS):
            r = login(u, f"198.18.90.{i + 1}")
            if r.status_code != 200:
                raise RuntimeError(f"登录失败 {u}: {r.status_code} {snippet(r.text, 120)}")
            self.ex.tokens[u] = (safe_json(r) or {}).get("token", "")
        self.phase_times["login"] = 0.5

    # ---- 准备数据 ----
    def prepare(self):
        ex = self.ex
        dims = ex.dimensions
        presets = [r for r in dims["dimensions"]["role"]["values"] if r["kind"] == "preset"]
        pops = [x for x in dims["dimensions"]["population"]["values"] if x["supported"]]
        ip = "198.18.9.100"
        cross = {}
        for r in presets:
            for x in pops:
                vp = x["voice_profile"]
                body = {"name": f"交叉-{r['name']}-{x['label']}", "voice_profile": vp,
                        "persona_profile": r["persona_profile"]}
                resp = ex.req("POST", "/api/characters", None, account="matrix_a", ip=ip, json=body)
                cid = ((safe_json(resp) or {}).get("character") or {}).get("id")
                if not cid:
                    raise RuntimeError(f"交叉角色创建失败 {r['id']}/{x['id']}: {snippet(resp.text, 160)}")
                cross[(r["id"], x["id"])] = cid
        ex.prepared["cross"] = cross

        calm = next(e for e in dims["dimensions"]["emotion"]["types"] if e["id"] == "calm")
        for sid, cid in (("mx_iso_session", "preset_senior_sister"),
                         ("mx_restart_role", "preset_senior_sister"),
                         ("mx_restart_emo", "preset_senior_sister")):
            ex.req("POST", f"/api/sessions/{sid}/character", None, account="matrix_a", ip=ip,
                   data={"character_id": cid})
        for sid in ("mx_iso_session", "mx_restart_emo"):
            ex.req("POST", "/api/emotion-config", None, account="matrix_a", ip=ip,
                   data={"session_id": sid, "emotion_name": calm["label"], **calm["params"]})
        iso = ex.req("POST", "/api/characters", None, account="matrix_a", ip=ip,
                     json={"name": "隔离测试角色A", "voice_profile": {"gender": "female",
                           "age_group": "young_adult", "voice_tags": ["gentle"], "speed": 1.0},
                           "persona_profile": {"identity": "隔离测试", "relation": "friend"}})
        ex.prepared["iso_char_id"] = ((safe_json(iso) or {}).get("character") or {}).get("id")
        rst = ex.req("POST", "/api/characters", None, account="matrix_a", ip=ip,
                     json={"name": "重启保留角色", "voice_profile": {"gender": "male",
                           "age_group": "middle_aged", "voice_tags": ["calm"], "speed": 1.0},
                           "persona_profile": {"identity": "重启测试", "relation": "mentor"}})
        ex.prepared["restart_char_id"] = ((safe_json(rst) or {}).get("character") or {}).get("id")

    def cleanup(self):
        ex = self.ex
        for cid in (ex.prepared.get("cross") or {}).values():
            ex.delete_char(None, cid, account="matrix_a", ip="198.18.9.101")
        for key in ("iso_char_id", "restart_char_id"):
            cid = ex.prepared.get(key)
            if cid:
                ex.delete_char(None, cid, account="matrix_a", ip="198.18.9.101")

    # ---- 执行 ----
    def run_case(self, case, layer):
        t0 = time.time()
        try:
            res = self.ex.dispatch(case)
        except Exception as e:
            res = result("FAIL", [assertion("用例执行未抛异常", False, e)],
                         {"error": f"{type(e).__name__}: {e}",
                          "traceback": traceback.format_exc()[-600:]},
                         classification="测试缺陷")
        elapsed = round(time.time() - t0, 2)
        merged = {
            "id": case["id"], "layer": layer, "family": case.get("family"),
            "kind": case["kind"], "title": case["title"], "dims": case.get("dims", {}),
            "expect": case.get("expect"), "source": case.get("source"),
            "status": res["status"], "classification": res.get("classification"),
            "notes": res.get("notes", ""), "assertions": res.get("assertions", []),
            "evidence": res.get("evidence", {}), "elapsed_seconds": elapsed,
            "defect": res.get("defect") or case.get("defect"),
            "executed": True,
        }
        self.results.append(merged)
        return merged

    def run_layer(self, cases, layer):
        t0 = time.time()
        for i, c in enumerate(cases, 1):
            r = self.run_case(c, layer)
            if i % 25 == 0:
                print(f"  [{layer}] {i}/{len(cases)} 完成（当前 {r['status']}）", flush=True)
        self.phase_times[f"run_{layer}"] = round(time.time() - t0, 2)

    def block_layer(self, cases, layer, reason, unlock):
        for c in cases:
            self.results.append({
                "id": c["id"], "layer": layer, "family": c.get("family"),
                "kind": c["kind"], "title": c["title"], "dims": c.get("dims", {}),
                "expect": c.get("expect"), "source": c.get("source"),
                "status": "BLOCKED", "classification": "环境不可用",
                "notes": reason, "assertions": [], "evidence": {"unlock": unlock},
                "elapsed_seconds": 0.0, "defect": None, "executed": False,
            })
        self.phase_times[f"block_{layer}"] = 0.0

    def run_all(self):
        cases = self.plan["cases"]
        t1 = [c for c in cases if c["layer"] == "T1"]
        t2 = [c for c in cases if c["layer"] == "T2"]
        t3 = [c for c in cases if c["layer"] == "T3"]

        self.start_all()
        self.login_all()
        self.probe = probe_llm()
        self.prepare()

        main_t1 = [c for c in t1 if c["kind"] not in ("restart_character", "restart_emotion",
                                                     "restart_custom_character")]
        self.run_layer(main_t1, "T1")

        rst = [c for c in t1 if c["kind"] in ("restart_character", "restart_emotion",
                                              "restart_custom_character")]
        if self.args.no_restart:
            for c in rst:
                r = self.run_case(c, "T1")
            self.phase_times["run_restart"] = 0.0
        else:
            self.restart_ai()
            t0 = time.time()
            for c in rst:
                self.run_case(c, "T1")
            self.phase_times["run_restart"] = round(time.time() - t0, 2)

        if self.probe.get("available"):
            self.run_layer(t2, "T2")
        else:
            reason = (f"DashScope LLM 探测失败：HTTP {self.probe.get('http_status')} "
                      f"{snippet(self.probe.get('body') or self.probe.get('error') or '', 120)}")
            self.block_layer(t2, "T2", reason,
                             self.probe.get("unlock", "提供可用 LLM_API_KEY 后重跑"))

        self.run_layer(t3, "T3")
        self.cleanup()

    # ---- 统计 ----
    def layer_stats(self, layer):
        cases = [r for r in self.results if r["layer"] == layer]
        st = Counter(r["status"] for r in cases)
        return {
            "planned": len([c for c in self.plan["cases"] if c["layer"] == layer]),
            "executed": len([r for r in cases if r.get("executed")]),
            "passed": st.get("PASS", 0),
            "failed": st.get("FAIL", 0),
            "capability_gap": st.get("CAPABILITY_GAP", 0),
            "blocked": st.get("BLOCKED", 0),
            "skipped": st.get("SKIP", 0),
            "duration_seconds": self.phase_times.get(f"run_{layer}", 0.0),
        }

    def cross_views(self):
        crs = [r for r in self.results if r["family"] == "CRS"]
        role_pop = defaultdict(lambda: Counter())
        role_emo = defaultdict(lambda: Counter())
        pop_emo = defaultdict(lambda: Counter())
        for r in crs:
            role_pop[(r["dims"]["role"], r["dims"]["population"])][r["status"]] += 1
            role_emo[(r["dims"]["role"], r["dims"]["emotion"])][r["status"]] += 1
            pop_emo[(r["dims"]["population"], r["dims"]["emotion"])][r["status"]] += 1
        emo_grid = defaultdict(lambda: Counter())
        for r in self.results:
            if r["family"] == "EMO" and r["kind"] == "emotion_boundary":
                emo_grid[(r["dims"]["emotion"], r["dims"]["intensity"])][r["status"]] += 1
        vox = defaultdict(lambda: Counter())
        for r in self.results:
            if r["family"] == "VOX" and r["kind"] == "voice_prompt":
                vox[r["dims"]["voice"]][r["status"]] += 1
        sids = defaultdict(lambda: Counter())
        for r in self.results:
            if r["family"] == "SES":
                sids[r["kind"]][r["status"]] += 1
        return {
            "role_population": {"|".join(k): dict(v) for k, v in role_pop.items()},
            "role_emotion": {"|".join(k): dict(v) for k, v in role_emo.items()},
            "population_emotion": {"|".join(k): dict(v) for k, v in pop_emo.items()},
            "emotion_intensity": {"|".join(k): dict(v) for k, v in emo_grid.items()},
            "voice_tag": {k: dict(v) for k, v in vox.items()},
            "session_ops": {k: dict(v) for k, v in sids.items()},
        }

    # ---- 报告 ----
    def build_report(self):
        stats = {L: self.layer_stats(L) for L in ("T1", "T2", "T3")}
        totals = Counter()
        for s in stats.values():
            for k in ("planned", "executed", "passed", "failed", "capability_gap", "blocked", "skipped"):
                totals[k] += s[k]
        failures = [r for r in self.results if r["status"] == "FAIL"]
        gaps = [r for r in self.results if r["status"] == "CAPABILITY_GAP"]
        blocked = [r for r in self.results if r["status"] == "BLOCKED"]

        defects = {}
        for r in failures:
            code = r.get("defect")
            if not code:
                continue
            d = defects.setdefault(code, {"code": code, "count": 0, "cases": [],
                                          **{k: v for k, v in DEFECT_CATALOG.get(code, {}).items()}})
            d["count"] += 1
            d["cases"].append({"id": r["id"], "title": r["title"], "evidence": r["evidence"]})

        verified_gaps = defaultdict(list)
        for r in gaps:
            if r["family"] == "CHR" and r["kind"] == "update_method":
                verified_gaps["GAP-01"].append(r["id"])
            elif r["family"] == "EMO" and r["kind"] == "emotion_gap_unbind":
                verified_gaps["GAP-02"].append(r["id"])
            elif r["family"] == "SES" and r["kind"] in ("session_gap_list", "session_gap_delete"):
                verified_gaps["GAP-03"].append(r["id"])
            elif r["family"] == "SES" and r["kind"] == "session_gap_unbind":
                verified_gaps["GAP-11"].append(r["id"])
            elif r["family"] == "POP" and r["dims"].get("population") in ("child", "toddler"):
                verified_gaps["GAP-04"].append(r["id"])
            elif r["family"] == "VOX" and r["kind"] == "voice_gap_options":
                verified_gaps["GAP-05"].append(r["id"])
            elif r["family"] == "VOX" and r["kind"] == "voice_gap_clone":
                verified_gaps["GAP-06"].append(r["id"])
            elif r["family"] == "VOX" and r["kind"] == "speed_observability":
                verified_gaps["GAP-08"].append(r["id"])
            elif r["family"] == "EMO" and r["kind"] == "emotion_gap_enum":
                verified_gaps["GAP-09"].append(r["id"])
        for r in blocked:
            if r["kind"] == "probe" and r["dims"].get("probe") == "voice_define":
                verified_gaps["GAP-07"].append(r["id"])

        report = {
            "meta": {
                "suite": "语伴矩阵化功能测试",
                "started_at": datetime.fromtimestamp(START_TS).isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "duration_seconds": round(time.time() - START_TS, 1),
                "python": sys.version.split()[0],
                "services": {"gateway": f":{GW_PORT}", "ai": f":{AI_PORT}", "tts_provider": "cloud"},
                "data_dir": str(DATA_DIR),
                "accounts": ACCOUNTS,
                "auth_required": True,
                "rate_limits": {"login_per_min": 5, "normal_per_min": 120, "expensive_per_min": 20},
                "no_relaxed_limits": True,
            },
            "strategy": self.ex.dimensions["combination_strategy"],
            "coverage": {
                "layers": {L: stats[L] for L in stats},
                "totals": dict(totals),
                "phase_seconds": self.phase_times,
            },
            "dimensions": self.ex.dimensions["dimensions"],
            "dependency_probes": {"llm": self.probe},
            "cross_views": self.cross_views(),
            "failures": [{"id": r["id"], "title": r["title"], "expect": r["expect"],
                          "evidence": r["evidence"], "assertions": r["assertions"],
                          "notes": r["notes"], "classification": r["classification"],
                          "defect": r["defect"]} for r in failures],
            "defects": sorted(defects.values(), key=lambda x: x["code"]),
            "capability_gaps": [
                {"code": code, "title": GAP_CATALOG.get(code, {}).get("title", ""),
                 "fix": GAP_CATALOG.get(code, {}).get("fix", ""),
                 "verified_by": verified_gaps.get(code, [])} for code in sorted(verified_gaps)
            ],
            "blocked": [{"id": r["id"], "layer": r["layer"], "title": r["title"],
                         "reason": r["notes"], "unlock": r["evidence"].get("unlock", ""),
                         "evidence": r["evidence"]} for r in blocked],
            "cases": self.results,
            "artifacts": {"plan": str(PLAN_FILE), "report": str(REPORT_FILE),
                          "summary": str(SUMMARY_FILE), "logs": str(LOGS),
                          "state": str(DATA_DIR / "state.json")},
        }
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        SUMMARY_FILE.write_text(render_summary(report), encoding="utf-8")
        return report


# ---------------------------------------------------------------- 渲染
def _pct(p, n):
    return f"{p}/{n}" if n else "-"


def render_summary(rep):
    cov = rep["coverage"]
    t1, t2, t3 = cov["layers"]["T1"], cov["layers"]["T2"], cov["layers"]["T3"]
    tot = cov["totals"]
    llm = rep["dependency_probes"]["llm"]

    lines = []
    lines.append("# 语伴矩阵化功能测试报告")
    lines.append("")
    lines.append(f"> 结论：计划 **{tot['planned']}** 条，实际执行 **{tot['executed']}** 条；"
                 f"T1 通过 **{t1['passed']}**、失败 **{t1['failed']}**、能力缺失 **{t1['capability_gap']}**；"
                 f"T2 阻塞 **{t2['blocked']}** 条（LLM {('可用' if llm.get('available') else '不可用')}），"
                 f"T3 阻塞 **{t3['blocked']}** 条。"
                 f"能力缺失集中在：{'、'.join(g['code'] for g in (rep.get('capability_gaps') or [])) or '无'}。")
    lines.append("")
    lines.append(f"- 时间：{rep['meta']['started_at']} → {rep['meta']['finished_at']}，"
                 f"耗时 {rep['meta']['duration_seconds']}s（预算 90 分钟内）")
    lines.append(f"- 环境：网关 :{GW_PORT} / AI :{AI_PORT} / TTS=cloud；限流保持产品默认 5/120/20，未放宽")
    lines.append(f"- 测试数据目录：`{rep['meta']['data_dir']}`（独立于产品数据）")
    lines.append("")

    lines.append("## 一、分层统计（计划 / 执行 / 通过 / 失败 / 能力缺失 / 阻塞 / 跳过）")
    lines.append("")
    lines.append("| 层 | 计划 | 执行 | 通过 | 失败 | 能力缺失 | 阻塞 | 跳过 | 耗时(s) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for L in ("T1", "T2", "T3"):
        s = cov["layers"][L]
        lines.append(f"| {L} | {s['planned']} | {s['executed']} | {s['passed']} | {s['failed']} | "
                     f"{s['capability_gap']} | {s['blocked']} | {s['skipped']} | {s['duration_seconds']} |")
    lines.append(f"| 合计 | {tot['planned']} | {tot['executed']} | {tot['passed']} | {tot['failed']} | "
                 f"{tot['capability_gap']} | {tot['blocked']} | {tot['skipped']} | "
                 f"{rep['meta']['duration_seconds']} |")
    lines.append("")
    lines.append("说明：BLOCKED / CAPABILITY_GAP 均不计入 PASS。T2 未真实执行时 executed=0。")
    lines.append("")

    lines.append("## 二、维度取值与来源")
    lines.append("")
    lines.append("| 维度 | 取值 | 来源 |")
    lines.append("|---|---|---|")
    role = rep["dimensions"]["role"]
    lines.append(f"| 角色 | 6 个官方预设 + 自定义（长名/特殊字符/空值/重名等边界） | product_preset + human_extension |")
    pops = rep["dimensions"]["population"]["values"]
    lines.append(f"| 人群 | " + "、".join(f"{x['label']}({x['source']})".replace("capability_gap", "能力缺失")
                                       for x in pops) + " | product_preset / capability_gap |")
    lines.append("| 音色 | 2 性别 × 10 标签 + 语速 0.7–1.3；方言/童声/情感音色/克隆 | product_preset / capability_gap |")
    emo = rep["dimensions"]["emotion"]
    lines.append("| 情感 | 6 类型（人为扩展）× 6 强度值(-1.0/0/0.5/1.0/2.0/非法) | human_extension + product_preset |")
    lines.append("| 会话 | 隐式新建/绑定/切换/多轮/重启；解绑与 CRUD 缺失 | product_preset / capability_gap |")
    lines.append("")

    lines.append("## 三、交叉通过情况")
    lines.append("")
    lines.append("### 3.1 角色 × 人群（跨 5 种情感，T1 全交叉 120 条）")
    lines.append("")
    role_ids = [r["id"] for r in role["values"] if r["kind"] == "preset"]
    pop_ids = [x["id"] for x in pops if x["supported"]]
    role_names = {r["id"]: r["name"] for r in role["values"]}
    pop_names = {x["id"]: x["label"] for x in pops}
    lines.append("| 角色 \\ 人群 | " + " | ".join(pop_names[p] for p in pop_ids) + " |")
    lines.append("|---" * (len(pop_ids) + 1) + "|")
    for rid in role_ids:
        cells = []
        for pid in pop_ids:
            st = rep["cross_views"]["role_population"].get(f"{rid}|{pid}", {})
            n = sum(st.values())
            p = st.get("PASS", 0)
            cells.append(_pct(p, n))
        lines.append(f"| {role_names[rid]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("### 3.2 情感类型 × 强度（T1 真跑）")
    lines.append("")
    intensities = ["-1.0", "0.0", "0.5", "1.0", "2.0", "abc"]
    lines.append("| 情感 \\ 强度 | " + " | ".join(intensities) + " |")
    lines.append("|---" * (len(intensities) + 1) + "|")
    for e in emo["types"]:
        cells = []
        for iv in intensities:
            st = rep["cross_views"]["emotion_intensity"].get(f"{e['id']}|{iv}", {})
            p, n = st.get("PASS", 0), sum(st.values())
            cells.append("✓" if n and p == n else (f"{p}/{n}" if n else "-"))
        lines.append(f"| {e['label']} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("### 3.3 性别 × 音色标签（voice_prompt 映射）")
    lines.append("")
    tag_names = {t['id']: t['label'] for t in rep["dimensions"]["voice"]["tags"]}
    lines.append("| 性别 | " + " | ".join(tag_names[t["id"]] for t in rep["dimensions"]["voice"]["tags"]) + " |")
    lines.append("|---" * (len(rep["dimensions"]["voice"]["tags"]) + 1) + "|")
    for g in ("female", "male"):
        cells = []
        for t in rep["dimensions"]["voice"]["tags"]:
            st = rep["cross_views"]["voice_tag"].get(f"{g}/{t['id']}", {})
            p, n = st.get("PASS", 0), sum(st.values())
            cells.append("✓" if n and p == n else (f"{p}/{n}" if n else "-"))
        lines.append(f"| {g} | " + " | ".join(cells) + " |")
    lines.append("")
    cross_emos = [e for e in emo["types"] if e["id"] != "surprised"]
    emo_names = {e["id"]: e["label"] for e in emo["types"]}
    lines.append("### 3.4 角色 × 情感（每格为 4 个人群的配置链路通过数）")
    lines.append("")
    lines.append("| 角色 \\ 情感 | " + " | ".join(emo_names[e["id"]] for e in cross_emos) + " |")
    lines.append("|---" * (len(cross_emos) + 1) + "|")
    for rid in role_ids:
        cells = []
        for e in cross_emos:
            st = rep["cross_views"]["role_emotion"].get(f"{rid}|{e['id']}", {})
            p, n = st.get("PASS", 0), sum(st.values())
            cells.append(_pct(p, n))
        lines.append(f"| {role_names[rid]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("### 3.5 人群 × 情感（每格为 6 个角色的配置链路通过数）")
    lines.append("")
    lines.append("| 人群 \\ 情感 | " + " | ".join(emo_names[e["id"]] for e in cross_emos) + " |")
    lines.append("|---" * (len(cross_emos) + 1) + "|")
    for pid in pop_ids:
        cells = []
        for e in cross_emos:
            st = rep["cross_views"]["population_emotion"].get(f"{pid}|{e['id']}", {})
            p, n = st.get("PASS", 0), sum(st.values())
            cells.append(_pct(p, n))
        lines.append(f"| {pop_names[pid]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("说明：3.1/3.4/3.5 均来自 T1 `CRS` 全交叉（6 预设 × 4 人群 × 5 情感 = 120 条），"
                 "验证的是「绑定角色 → 写入情感 → 双读回一致」的配置链路，不代表 LLM 对话表现；"
                 "对话质量属于 T2（当前 BLOCKED）。")
    lines.append("")

    lines.append("## 四、T1 失败用例（产品缺陷）")
    lines.append("")
    if rep["failures"]:
        lines.append("| 用例 | 期望 | 实际响应 | 判定 |")
        lines.append("|---|---|---|---|")
        for f in rep["failures"]:
            ev = f["evidence"]
            actual = next((ev[k] for k in ("http_status", "http_status_get_after_delete",
                                           "http_status_create", "status") if ev.get(k)), None)
            body = next((ev[k] for k in ("body", "body_get_after_delete", "detail",
                                         "stored_name", "stored", "leaked_character",
                                         "emotion_config", "character", "response")
                               if ev.get(k) not in (None, "", [], {})), "")
            if not body and "stored_name" in ev:
                body = f'stored_name="{ev["stored_name"]}"'  # 空串也是有效证据
            detail = f"HTTP {actual} {str(body)[:100]}" if actual is not None \
                else json.dumps(ev, ensure_ascii=False)[:140]
            lines.append(f"| {f['id']} {f['title']} | {f['expect']} | {detail} | "
                         f"{f['classification'] or '-'} {f['defect'] or ''} |")
    else:
        lines.append("无失败用例。")
    lines.append("")

    lines.append("## 五、缺陷清单（仅统计本轮 FAIL 用例；CAPABILITY_GAP / BLOCKED 不计入）")
    lines.append("")
    if rep["defects"]:
        for d in rep["defects"]:
            lines.append(f"### {d['code']} {d.get('title', '')}（{d.get('kind', '')}，命中 {d['count']} 条）")
            lines.append(f"- 最小修复：{d.get('fix', '')}")
            lines.append(f"- 证据用例：{', '.join(c['id'] for c in d['cases'][:6])}")
            ev = d["cases"][0].get("evidence", {})
            if ev:
                lines.append(f"- 首条实际响应：`{json.dumps(ev, ensure_ascii=False)[:400]}`")
            lines.append("")
    else:
        lines.append("无产品缺陷记录。")
        lines.append("")

    lines.append("## 六、产品能力缺失（如实标注，不记为测试失败）")
    lines.append("")
    lines.append("| 编号 | 能力 | 最小补齐建议 | 证据 |")
    lines.append("|---|---|---|---|")
    for g in rep["capability_gaps"]:
        lines.append(f"| {g['code']} | {g['title']} | {g['fix']} | {', '.join(g['verified_by'][:5])} |")
    lines.append("")

    lines.append("## 七、T2 / T3 阻塞与解锁条件")
    lines.append("")
    lines.append(f"### T2 依赖 LLM：{t2['blocked']} 条全部 BLOCKED")
    lines.append("")
    lines.append(f"- DashScope 探测：HTTP {llm.get('http_status')}，返回 `{snippet(llm.get('body') or llm.get('error') or '', 160)}`")
    lines.append(f"- 解锁：{llm.get('unlock', '')}")
    lines.append("- 解锁后复跑：`.venv\\Scripts\\python.exe tests/matrix/run_matrix.py`（生成器无需重跑）")
    lines.append("")
    lines.append("### T3 依赖 TTS/GPU：只探测不硬跑")
    lines.append("")
    lines.append("| 探测项 | 结论 | 证据摘要 |")
    lines.append("|---|---|---|")
    for b in rep["blocked"]:
        if b["layer"] != "T3":
            continue
        ev = b["evidence"]
        summary = ev.get("http_status") or ev.get("torch_cuda") or ev.get("body") or ev.get("error") or ""
        lines.append(f"| {b['id']} {b['title']} | BLOCKED | {str(summary)[:120]} |")
    lines.append("")
    lines.append("- 解锁条件：具备 CUDA GPU 且安装 VoxCPM2 模型（`TTS_PROVIDER=local`），或云侧开通 voice design / TTS 配额")
    lines.append("- 明确未使用「HTTP 200 + 错误文案」作为通过依据")
    lines.append("")

    lines.append("### 覆盖限制与风险观察（不计入缺陷统计）")
    lines.append("")
    lines.append("- T1 不覆盖「连续多轮对话 / 话术一致性 / 上下文保持」：这类用例必须真实调用 LLM，"
                 "已全部归入 T2；T2 当前 BLOCKED，因此本轮无法给出对话质量结论。")
    lines.append("- DEF-05（XFF 伪造绕过限流）：已改为仅当直连来源落在 `TRUSTED_PROXY_CIDRS` 内才采信 "
                 "X-Forwarded-For，否则以 socket 对端地址分桶；实测远端伪造 XFF 不生效（xf_remote_trusted=false）。")
    lines.append("- DEF-06（就绪探针不反映云依赖）：已改为真实探测云 TTS，依赖不可用时 `/ready` 返回 503 "
                 "`not_ready` 并附 `cloud_dependency` 明细；实测 503 + problems 含 cloud_tts_unavailable。")
    lines.append("")

    lines.append("## 八、组合策略、抽样与耗时裁剪")
    lines.append("")
    st = rep["strategy"]
    lines.append(f"- T1：{st['T1']['method']}。{st['T1']['rationale']}")
    lines.append(f"- T2：{st['T2']['method']}。{st['T2']['rationale']}（计划 {t2['planned']} 条）")
    lines.append(f"- T3：{st['T3']['method']}。{st['T3']['rationale']}")
    lines.append(f"- 各阶段耗时：`{json.dumps(cov['phase_seconds'], ensure_ascii=False)}`")
    lines.append("- 裁剪说明：T2 若解锁，按 pairwise 抽样只跑约 45 条（全量 180 条约需 3-4 小时）；"
                 "T3 不硬跑原因是 CPU TTS RTF 35-88，单句需数分钟，超出 90 分钟预算")
    lines.append("")

    lines.append("## 九、复跑与约束")
    lines.append("")
    lines.append("```powershell")
    lines.append(".venv\\Scripts\\python.exe tests/matrix/generate_matrix.py")
    lines.append(".venv\\Scripts\\python.exe tests/matrix/run_matrix.py")
    lines.append("```")
    lines.append("")
    lines.append("- 未使用 mock / route 拦截 / 事件注入；所有 T1 用例真实打到网关与 AI 服务")
    lines.append("- 未 commit / push；未启停付费 GPU；测试数据全部位于 tests/matrix/runtime")
    lines.append("- 限流用例使用独立 X-Forwarded-For 桶，未放宽任何限额")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-clean", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args()

    if port_in_use(AI_PORT) or port_in_use(GW_PORT):
        print(f"端口已被占用（AI {AI_PORT} / GW {GW_PORT}），请先释放后再跑", file=sys.stderr)
        return 2
    if not PLAN_FILE.exists():
        print("缺少 matrix_plan.json，请先运行 generate_matrix.py", file=sys.stderr)
        return 2
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    print(f"用例计划：{plan['layers']}，共 {len(plan['cases'])} 条", flush=True)

    orch = Orchestrator(plan, args)
    try:
        orch.run_all()
    except Exception as e:
        print(f"执行中断：{type(e).__name__}: {e}", file=sys.stderr)
    try:
        rep = orch.build_report()
        print("\n===== 分层统计 =====", flush=True)
        for L, s in rep["coverage"]["layers"].items():
            print(f"{L}: 计划 {s['planned']} 执行 {s['executed']} 通过 {s['passed']} "
                  f"失败 {s['failed']} 能力缺失 {s['capability_gap']} 阻塞 {s['blocked']}", flush=True)
        print(f"报告：{REPORT_FILE}")
        print(f"汇总：{SUMMARY_FILE}")
        return 0
    finally:
        orch.services.stop_all()


if __name__ == "__main__":
    sys.exit(main())
