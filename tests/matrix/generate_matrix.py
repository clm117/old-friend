# -*- coding: utf-8 -*-
"""矩阵用例生成器

读取 dimensions.json，输出 matrix_plan.json（数据驱动，执行器只认 kind + params）。

组合策略：
- T1：穷举 + 边界值（全部本地 HTTP/WS，成本低），角色×人群×情感做 6×4×5 全交叉
- T2：贪心 pairwise（role×population×emotion），并抽 1/8 扩展为 3 轮
- T3：只生成探测项，不生成执行用例
"""
import json
from itertools import product
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIM = json.loads((HERE / "dimensions.json").read_text(encoding="utf-8"))

ACCOUNTS = ["matrix_a", "matrix_b", "matrix_c", "matrix_d", "matrix_e"]

AGE_WORD = {"teen": "teenage", "young_adult": "young", "middle_aged": "middle-aged", "elderly": "elderly"}
PERSON = {
    "teenage": ("teenage girl", "teenage boy"),
    "young": ("young woman", "young man"),
    "middle-aged": ("middle-aged woman", "middle-aged man"),
    "elderly": ("elderly woman", "elderly man"),
}
TAG_EN = {t["id"]: t["en"] for t in DIM["dimensions"]["voice"]["tags"]}
TAG_CN = {t["id"]: t["label"] for t in DIM["dimensions"]["voice"]["tags"]}


def speed_phrase(speed):
    """与 character_engine.CharacterEngine.speed_to_phrase 保持一致"""
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return ""
    if abs(s - 1.0) < 1e-9:
        return ""
    if s <= 0.75:
        return "speaking very slowly"
    if s < 1.0:
        return "speaking slowly"
    if s >= 1.5:
        return "speaking very quickly"
    return "speaking quickly"


def expected_prompt(gender, age_group, tags, speed=1.0):
    age_key = AGE_WORD.get(age_group, "young")
    person = PERSON[age_key][0 if gender == "female" else 1]
    article = "An" if person.startswith("elderly") else "A"
    tail = ", ".join(TAG_EN[t] for t in tags if t in TAG_EN) + " voice"
    prompt = f"{article} {person}, {tail}"
    pace = speed_phrase(speed)
    return f"{prompt}, {pace}" if pace else prompt


class Plan:
    def __init__(self):
        self.cases = []
        self._ip = 0
        self._idx = 0

    def _next_ip(self):
        self._ip += 1
        return f"198.18.{1 + self._ip // 250}.{self._ip % 250 + 1}"

    def add(self, layer, family, kind, title, dims=None, params=None, expect="", source="product_preset",
            defect=None, account=None):
        self._idx += 1
        cid = f"{layer}-{family}-{self._idx:03d}"
        case = {
            "id": cid,
            "layer": layer,
            "kind": kind,
            "family": family,
            "title": title,
            "dims": dims or {},
            "params": dict(params or {}),
            "expect": expect,
            "source": source,
            "defect": defect,
        }
        case["params"].setdefault("sid", "mx_" + cid.lower().replace("-", "_"))
        case["params"].setdefault("account", account or ACCOUNTS[self._idx % len(ACCOUNTS)])
        case["params"].setdefault("ip", self._next_ip())
        self.cases.append(case)
        return case


def build_t1(p):
    roles = DIM["dimensions"]["role"]["values"]
    presets = [r for r in roles if r["kind"] == "preset"]
    pops = DIM["dimensions"]["population"]["values"]
    supported_pops = [x for x in pops if x["supported"]]
    genders = DIM["dimensions"]["voice"]["genders"]
    tags = DIM["dimensions"]["voice"]["tags"]
    speed_values = DIM["dimensions"]["voice"]["speed_values"]
    emotions = DIM["dimensions"]["emotion"]["types"]
    intensities = DIM["dimensions"]["emotion"]["intensity_values"]

    # ---------------- SYS：契约 / 鉴权 / CORS / 健康 ----------------
    p.add("T1", "SYS", "enum_options", "角色枚举字典与产品实现一致",
          {"role": "all", "voice": "all", "population": "all"},
          {"checks": {"genders": 2, "age_groups": 4, "voice_tags": 10, "relations": 5,
                      "personality_tags": 10}},
          "genders=2, age_groups=4(teen/young_adult/middle_aged/elderly), voice_tags=10, relations=5")
    p.add("T1", "SYS", "list_presets", "列出 6 个官方预设角色",
          {"role": "all"}, {"expected_ids": [r["id"] for r in presets], "count": 6},
          "characters 含 6 个 preset 且 is_preset=true")
    for r in presets:
        p.add("T1", "SYS", "get_preset", f"读取预设角色 {r['name']}",
              {"role": r["id"]}, {"character_id": r["id"], "name": r["name"]},
              "200 + status=ok + id/name 一致")
    p.add("T1", "SYS", "cors_allowed", "CORS 白名单来源可跨域",
          {}, {"origin": "http://localhost:8080", "path": "/api/characters"},
          "OPTIONS 200 且 Access-Control-Allow-Origin 回显")
    p.add("T1", "SYS", "cors_denied", "CORS 非白名单来源被拒",
          {}, {"origin": "http://evil.example", "path": "/api/characters"},
          "响应不含 Access-Control-Allow-Origin")
    p.add("T1", "SYS", "auth_missing", "无凭证访问受保护接口返回 401",
          {}, {"path": "/api/characters"}, "HTTP 401")
    p.add("T1", "SYS", "auth_invalid", "伪造 Bearer 返回 401",
          {}, {"path": "/api/characters", "token": "bad.token.value"},
          "HTTP 401")
    p.add("T1", "SYS", "auth_ws", "WS 无 token 连接被拒（1008）",
          {"session": "implicit_create"}, {"sid": "mx_ws_auth_probe"},
          "WS 先 accept 后 close(code=1008)")
    p.add("T1", "SYS", "health_gw", "网关 /health 可用", {}, {"url": "gw:/health"},
          "HTTP 200 + status=healthy")
    p.add("T1", "SYS", "ready_gw", "网关 /ready 可用", {}, {"url": "gw:/ready"},
          "HTTP 200 + status=ready（注：不校验云配额）")
    p.add("T1", "SYS", "health_ai", "AI /health 可用", {}, {"url": "ai:/health"},
          "HTTP 200 + status=healthy + tts_service=cloud")
    p.add("T1", "SYS", "ready_ai", "AI /ready 可用且状态存储可写", {}, {"url": "ai:/ready"},
          "HTTP 200 + status=ready")
    p.add("T1", "SYS", "metrics", "Prometheus /metrics 可抓取", {}, {"url": "gw:/metrics"},
          "HTTP 200 + text/plain 指标")
    p.add("T1", "SYS", "invalid_json", "角色创建非 JSON 请求体应返回 400",
          {"role": "custom"}, {"body": "{"},
          "HTTP 400（实测为 502 → 缺陷）", defect="DEF-03")

    # ---------------- CHR：角色 CRUD 穷举 ----------------
    for g in genders:
        for age in ["teen", "young_adult", "middle_aged", "elderly"]:
            for t in tags:
                p.add("T1", "CHR", "create_verify_delete",
                      f"创建/读取/删除音色角色 {g['label']}-{age}-{t['label']}",
                      {"role": "custom", "voice": f"{g['id']}/{age}/{t['id']}", "population": age},
                      {"name": f"声纹-{g['id']}-{age}-{t['id']}", "gender": g["id"],
                       "age_group": age, "voice_tags": [t["id"]], "speed": 1.0},
                      "创建返回 ok 且 voice_profile 与入参一致；GET 一致；DELETE 返回 deleted_id")
    for b in DIM["dimensions"]["role"]["boundary_values"]:
        params = {"name": b.get("name", ""), "name_len": b.get("name_len")}
        p.add("T1", "CHR", "name_boundary", f"角色名称边界：{b['id']}",
              {"role": "custom"}, params, b["expected"], source=b["source"])
    for s in speed_values:
        p.add("T1", "CHR", "speed_boundary", f"语速边界：{s['value']!r}",
              {"voice": f"speed/{s['value']}"},
              {"speed": s["value"], "expected": s["expected"]},
              ("拒绝非法语速（400/422）" if s["expected"] == "reject"
               else f"落库语速为 {s['expected']}"), source=s["source"])
    invalid_cases = [
        ("gender_invalid", {"gender": "other"}, {"gender": "female"}, "非法性别归一为 female"),
        ("age_child", {"age_group": "child"}, {"age_group": "young_adult"}, "child 不存在，归一为 young_adult"),
        ("age_toddler", {"age_group": "toddler"}, {"age_group": "young_adult"}, "toddler 不存在，归一为 young_adult"),
        ("tag_invalid", {"voice_tags": ["dialect", "child_voice", "robot"]}, {"voice_tags": ["gentle"]},
         "非法标签全部丢弃，女性回退 gentle"),
        ("tag_empty", {"voice_tags": []}, {"voice_tags": ["gentle"]}, "空标签回退默认"),
        ("relation_invalid", {"relation": "lover"}, {"relation": "friend"}, "非法关系归一为 friend"),
        ("personality_invalid", {"personality_tags": ["evil", "unknown"]}, {"personality_tags": []},
         "非法性格标签全部丢弃"),
        ("identity_long", {"identity": "身" * 60}, {"identity_len": 50}, "identity 截断为 50"),
        ("style_long", {"speaking_style": "风" * 220}, {"speaking_style_len": 200}, "speaking_style 截断为 200"),
        ("free_long", {"free_description": "描" * 520}, {"free_description_len": 500},
         "free_description 截断为 500"),
    ]
    for name, override, expect, desc in invalid_cases:
        params = {"name": f"边界-{name}", "override": override}
        p.add("T1", "CHR", "invalid_enum", f"非法枚举/超长文本：{name}",
              {"role": "custom"}, params, desc)
    p.add("T1", "CHR", "missing_profiles", "缺省 voice_profile 使用安全默认值",
          {"role": "custom"}, {"name": "缺省声学档案"},
          "gender=female, age_group=young_adult, voice_tags=[gentle], speed=1.0")
    p.add("T1", "CHR", "missing_profiles", "缺省 persona_profile 使用安全默认值",
          {"role": "custom"}, {"name": "缺省人设档案", "skip_persona": True},
          "identity='', relation=friend, tags=[], style='', free=''")
    p.add("T1", "CHR", "lifecycle", "自定义角色创建→读取→删除→再读取",
          {"role": "custom"}, {"name": "生命周期角色"},
          "删除后 GET 应 404；实测 200+error（缺陷）", defect="DEF-02")
    p.add("T1", "CHR", "duplicate_name", "重名角色允许创建且 id 不同",
          {"role": "custom"}, {"name": "温柔学姐"}, "两个角色 id 不同且都能被列出")
    p.add("T1", "CHR", "delete_preset", "删除预设角色应被拒绝（404/400）",
          {"role": "preset_senior_sister"}, {"character_id": "preset_senior_sister"},
          "HTTP 404/400；实测 200+error（缺陷）", defect="DEF-02")
    p.add("T1", "CHR", "delete_missing", "删除不存在角色应 404",
          {"role": "custom"}, {"character_id": "custom_doesnotexist"},
          "HTTP 404；实测 200+error（缺陷）", defect="DEF-02")
    p.add("T1", "CHR", "update_method", "角色修改端点 PUT",
          {"role": "custom"}, {"method": "PUT"}, "200；名称与资料更新；读回一致")
    p.add("T1", "CHR", "update_method", "角色修改端点 PATCH",
          {"role": "custom"}, {"method": "PATCH"}, "200；名称与资料更新；读回一致")
    p.add("T1", "CHR", "update_missing", "修改不存在角色应 404",
          {"role": "custom"}, {"character_id": "custom_doesnotexist"}, "HTTP 404")
    p.add("T1", "CHR", "update_forbidden", "修改他人角色应 403",
          {"role": "custom"}, {"as": "matrix_b"}, "HTTP 403（owner 不匹配）")
    p.add("T1", "CHR", "update_duplicate", "修改为重名应 409",
          {"role": "custom"}, {}, "HTTP 409（同名角色已存在）")

    # ---------------- POP：人群 → 声学档案映射 ----------------
    for x in pops:
        p.add("T1", "POP", "population_create", f"人群映射：{x['label']}",
              {"population": x["id"], "role": "custom"},
              {"population": x["id"], "voice_profile": x["voice_profile"],
               "prompt_fragment": x.get("prompt_fragment")},
              ("voice_prompt 含 " + x.get("prompt_fragment", "-")) if x["supported"]
              else "产品无该年龄段（静默归一）",
              source=x["source"])

    # ---------------- VOX：音色标签 × 性别 + 能力缺口 ----------------
    for g in genders:
        for t in tags:
            p.add("T1", "VOX", "voice_prompt", f"音色映射 {g['label']}-{t['label']}",
                  {"voice": f"{g['id']}/{t['id']}", "role": "custom"},
                  {"gender": g["id"], "age_group": "young_adult", "voice_tags": [t["id"]],
                   "expected_prompt": expected_prompt(g["id"], "young_adult", [t["id"]])},
                  "绑定返回的 voice_prompt 与声学档案一致")
    p.add("T1", "VOX", "voice_gap_options", "音色谱缺少方言/童声/老年音/情感音色",
          {"voice": "capability_gap"}, {"forbidden_tags": ["dialect", "child", "elderly_voice", "emotion"]},
          "options.voice_tags 不含这些项 → 能力缺失", source="capability_gap")
    p.add("T1", "VOX", "voice_gap_clone", "声音克隆端点不存在",
          {"voice": "voice_clone"}, {"path": "/api/voice/clone"},
          "404/405 → 能力缺失", source="capability_gap")
    p.add("T1", "VOX", "voice_catalog", "音色目录可查询（GAP-05 底座 + 方言结构位）",
          {"voice": "catalog"}, {"url": "gw:/api/voice-catalog"},
          "10 个标签 + 性别默认回退 + capabilities.dialect=True + 三方言未就绪")
    p.add("T1", "VOX", "dialect_catalog", "三方言目录结构与能力开关（GAP-05）",
          {"voice": "dialect_catalog"}, {"url": "gw:/api/characters/options"},
          "粤语/四川话/东北话：locale 正确、均标注暂不可用、默认普通话")
    p.add("T1", "VOX", "dialect_character", "角色方言落库、未知值归一与 prompt 红线",
          {"voice": "dialect_character"}, {"sid": None},
          "dialect=sichuan 原样保存；未知值归一为 None；voice_prompt 与无方言一致")
    p.add("T1", "VOX", "dialect_fallback", "方言未就绪回退默认音色并给出提示",
          {"voice": "dialect_fallback"}, {"sid": None},
          "TTS 成功时 X-Voice-Notice 含“暂不可用/回退”；TTS 不可用则 BLOCKED")
    p.add("T1", "VOX", "voice_design_capability", "云声音定义能力接口（GAP-07）",
          {"voice": "design_capability"}, {"url": "gw:/api/voice-design/capability"},
          "target_model 绑定、复刻可用、设计不支持原因、配额 1000、限流 180/3")
    p.add("T1", "VOX", "voice_design_unsupported", "目标模型不支持声音设计 → 501",
          {"voice": "design_unsupported"}, {"mode": "design"},
          "501 + code=voice_define_unsupported，不返回假成功")
    p.add("T1", "VOX", "voice_define_error", "声音定义欠费/限流失败返回明确 code",
          {"voice": "define_error"}, {"session": "upload"},
          "非 200 且 code/hint 明确（欠费→402 voice_quota_unavailable）")
    p.add("T1", "VOX", "speed_observability", "语速参数是否影响 voice_prompt",
          {"voice": "speed"}, {"sid_a": None},
          "0.7 与 1.3 的 voice_prompt 不同（含节奏描述）")
    # GAP-08：语速边界与越界（0.5/2.0 合法，0.49/2.01 → 422，缺省 1.0 逐字一致）
    p.add("T1", "VOX", "speed_boundary", "语速下边界 0.5",
          {"voice": "speed", "speed": 0.5}, {"speed": 0.5, "expect": "ok", "phrase": "speaking very slowly"},
          "创建成功且 voice_prompt 含 speaking very slowly")
    p.add("T1", "VOX", "speed_boundary", "语速上边界 2.0",
          {"voice": "speed", "speed": 2.0}, {"speed": 2.0, "expect": "ok", "phrase": "speaking very quickly"},
          "创建成功且 voice_prompt 含 speaking very quickly")
    p.add("T1", "VOX", "speed_boundary", "语速低于下界 0.49",
          {"voice": "speed", "speed": 0.49}, {"speed": 0.49, "expect": 422},
          "HTTP 422（越界拒绝，不静默钳制）")
    p.add("T1", "VOX", "speed_boundary", "语速高于上界 2.01",
          {"voice": "speed", "speed": 2.01}, {"speed": 2.01, "expect": 422},
          "HTTP 422（越界拒绝，不静默钳制）")
    p.add("T1", "VOX", "speed_boundary", "语速缺省 1.0",
          {"voice": "speed", "speed": 1.0},
          {"speed": None, "expect": "ok", "phrase": "",
           "expected_prompt": expected_prompt("female", "young_adult", ["gentle"])},
          "缺省 1.0，voice_prompt 与既有标签映射逐字一致")
    # GAP-05 新增：12 条预设音色（目录 + 描述驱动，真实 voice_id 暂空）
    p.add("T1", "VOX", "voice_presets_catalog", "12 条预设音色目录（字段/就绪状态/基线不变）",
          {"voice": "presets_catalog"}, {"url": "gw:/api/voice-presets"},
          "12 条名称与顺序一致；字段齐全；locale=zh-CN/voice_type=preset/ready=false/notice 明确；"
          "既有 10 标签与默认回退不变")
    p.add("T1", "VOX", "voice_preset_prompts", "12 条预设均可按 id 生成含语感的 voice_prompt",
          {"voice": "preset_prompts"}, {"url": "gw:/api/voice-presets/{id}/prompt"},
          "每条 prompt 非空且含风格词；相声评书 capabilities.style=false 且有 capability_note")
    p.add("T1", "VOX", "voice_preset_unknown", "未知预设 id → 404",
          {"voice": "preset_unknown"}, {"preset_id": "vp_not_exist"},
          "取 prompt 与绑定均返回 404，不静默回退")
    p.add("T1", "VOX", "voice_preset_bind", "预设音色绑定会话：未绑定真实音色时明确提示",
          {"voice": "preset_bind"}, {"sid": None, "preset_id": "vp_xiangsheng_pingshu"},
          "200 + voice_prompt 非空 + voice_id=null + ready=false + notice 含“未就绪”+ 落盘一致")
    p.add("T1", "VOX", "voice_preset_forbidden", "他人会话绑定预设 → 403",
          {"voice": "preset_forbidden"}, {"sid": None},
          "跨账号绑定预设被拒绝，不泄露/不写入他人会话")

    # ---------------- EMO：情感类型 × 强度边界 ----------------
    for e in emotions:
        dominant = max(e["params"], key=e["params"].get)
        for i in intensities:
            params = {"type": e["id"], "dominant": dominant, "input": i["input"],
                      "expected": i["expected"], "base": e["params"], "kind": i["kind"],
                      "emotion_name": f"{e['label']}-{i['input']}"}
            p.add("T1", "EMO", "emotion_boundary", f"情感 {e['label']} × 强度 {i['input']!r}",
                  {"emotion": e["id"], "intensity": str(i["input"])}, params,
                  "落库值 clamp 到 [0,1]" if i["kind"] != "illegal" else "HTTP 422（非法数值）",
                  source=i["source"])
    for a, b in [("calm", "happy"), ("sad", "angry"), ("anxious", "calm")]:
        p.add("T1", "EMO", "emotion_switch", f"情感切换 {a}→{b}",
              {"emotion": f"{a}->{b}"}, {"first": a, "second": b},
              "GET 返回后写入的配置（覆盖生效）")
    p.add("T1", "EMO", "emotion_isolate_sessions", "情感配置按 session 隔离",
          {"emotion": "calm"}, {"sid": "mx_emo_iso_a", "other": "mx_emo_iso_b", "emotion": "calm"},
          "另一 session 的 emotion_config 为 null")
    p.add("T1", "EMO", "emotion_unknown_session", "未配置情感的 session 返回 null",
          {"emotion": "none"}, {"sid": "mx_emo_never"}, "200 + emotion_config=null")
    p.add("T1", "EMO", "emotion_gap_unbind", "情感配置解绑（DELETE）",
          {"emotion": "unbind"}, {"sid": "mx_emo_unbind"},
          "204；删后 GET 200 且配置为空；重复删除幂等；他人 403")
    p.add("T1", "EMO", "emotion_presets", "情感类型预设枚举（6 类映射 6 轴）",
          {"emotion": "enum"}, {"url": "gw:/api/emotion-presets"},
          "6 个预设，每个含 6 轴参数且在 [0,1]")
    p.add("T1", "EMO", "emotion_custom_name", "历史自定义情感名称仍可读写",
          {"emotion": "custom"}, {"sid": "mx_emo_custom", "name": "小林专属情绪"},
          "POST 200 + GET 读回同名，未被枚举拒绝")
    p.add("T1", "EMO", "emotion_long_name", "超长情感名称被拒绝",
          {"emotion": "long_name"}, {"sid": "mx_emo_long", "name": "情" * 1000},
          "HTTP 400（名称长度上限 64 字，DEF-07 校验保持有效）")

    # ---------------- SES：会话与绑定 ----------------
    for r in presets:
        a = r["voice_profile"]
        p.add("T1", "SES", "session_bind", f"新会话绑定预设角色 {r['name']}",
              {"role": r["id"], "session": "implicit_create"},
              {"character_id": r["id"],
               "expected_prompt": expected_prompt(a["gender"], a["age_group"], a["voice_tags"], a["speed"])},
              "返回 ok + voice_prompt + system_prompt")
    pairs = [(presets[i]["id"], presets[(i + 1) % len(presets)]["id"]) for i in range(5)]
    for a, b in pairs:
        p.add("T1", "SES", "session_switch", f"会话切换角色 {a} → {b}",
              {"role": a, "session": "switch"}, {"first": a, "second": b},
              "GET 返回第二个角色")
    for desc in ["A young man, calm voice", "An elderly woman, gentle voice",
                 "A teenage girl, energetic voice"]:
        p.add("T1", "SES", "session_voice_style", f"绑定语音风格：{desc[:28]}",
              {"voice": "voice_style"}, {"description": desc},
              "state.json 中该 session 的 voice_description 等于该描述")
    p.add("T1", "SES", "session_gap_list", "会话列表（仅本人 + 分页）",
          {"session": "crud"}, {"limit": 5},
          "200；仅返回本人会话；total/limit/offset 正确；他人不可见")
    p.add("T1", "SES", "session_gap_delete", "删除会话后读写 404",
          {"session": "crud"}, {"method": "DELETE"},
          "204；删后 GET 404；重复删除 404；列表已排除")
    p.add("T1", "SES", "session_rename", "会话重命名（PATCH）",
          {"session": "crud"}, {"title": "晨间问候"},
          "200；列表标题同步；空标题 400")
    p.add("T1", "SES", "session_delete_forbidden", "他人会话删除/重命名 403",
          {"session": "crud"}, {}, "DELETE/PATCH 均 403")
    p.add("T1", "SES", "session_ws_deleted", "WS 连接已删会话关闭 1008",
          {"session": "crud"}, {}, "close code=1008，reason 含 session_deleted")
    invalid_ids = ["会话ID", "bad id", "-abc", "_", ".abc", "a" * 65, "a@b", "a#b"]
    for bad in invalid_ids:
        p.add("T1", "SES", "session_invalid_id", f"非法 session_id：{bad[:12]}",
              {"session": "validation"}, {"bad_id": bad, "expect": 400},
              "网关 400（白名单 ^[A-Za-z0-9][A-Za-z0-9_\\-.]{0,63}$）")
    p.add("T1", "SES", "session_bind_missing", "绑定不存在角色应 404",
          {"session": "validation"}, {"character_id": "ghost_char_404"},
          "HTTP 404；实测 200+error（缺陷）", defect="DEF-02")
    p.add("T1", "SES", "session_bind_invalid", "绑定非法字符 character_id",
          {"session": "validation"}, {"character_id": "bad id"},
          "网关 400")
    p.add("T1", "SES", "session_gap_unbind", "角色解绑端点不存在",
          {"session": "unbind"}, {}, "405 → 能力缺失", source="capability_gap")

    # ---------------- RATE：限流（独立 IP，不放宽限额） ----------------
    p.add("T1", "RL", "rate_login", "登录限流 5/min（第 6 次 429）",
          {"session": "rate"}, {"limit": 5, "ip": "198.18.77.1"}, "前 5 次 401，第 6 次 429")
    p.add("T1", "RL", "rate_normal", "普通接口限流 120/min（第 121 次 429）",
          {"session": "rate"}, {"limit": 120, "ip": "198.18.77.2"}, "前 120 次 200，第 121 次 429")
    p.add("T1", "RL", "rate_expensive", "昂贵接口限流 20/min（第 21 次 429）",
          {"session": "rate"}, {"limit": 20, "ip": "198.18.77.3"}, "前 20 次 400（空文本），第 21 次 429")

    # ---------------- ISO：多用户隔离 ----------------
    p.add("T1", "ISO", "iso_read_character", "B 读取 A 的会话角色（应拒绝）",
          {"session": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.1"},
          "403/404；实测 200 → DEF-01", defect="DEF-01")
    p.add("T1", "ISO", "iso_write_character", "B 改写 A 的会话角色（应拒绝）",
          {"session": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.2"},
          "403/404；实测 200 → DEF-01", defect="DEF-01")
    p.add("T1", "ISO", "iso_read_emotion", "B 读取 A 的情感配置（应拒绝）",
          {"emotion": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.3"},
          "403/404；实测 200 → DEF-01", defect="DEF-01")
    p.add("T1", "ISO", "iso_write_emotion", "B 改写 A 的情感配置（应拒绝）",
          {"emotion": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.4"},
          "403/404；实测 200 → DEF-01", defect="DEF-01")
    p.add("T1", "ISO", "iso_read_response", "B 读取 A 的对话响应（应拒绝）",
          {"session": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.5"},
          "403/404；实测 200 → DEF-01", defect="DEF-01")
    p.add("T1", "ISO", "iso_custom_character", "B 可见 A 创建的自定义角色（应隔离）",
          {"role": "isolation"}, {"as": "matrix_b", "ip": "198.18.88.6"},
          "404/不可见；实测 200 → DEF-04", defect="DEF-04")

    # ---------------- UPL：上传/输入校验（不触发真实 ASR） ----------------
    p.add("T1", "UPL", "upload_empty", "空音频上传返回 400", {"session": "upload"},
          {"kind": "empty"}, "HTTP 400")
    p.add("T1", "UPL", "upload_fake", "伪造音频（无魔数）返回 400", {"session": "upload"},
          {"kind": "fake"}, "HTTP 400")
    p.add("T1", "UPL", "upload_over", "超过 10MB 返回 413", {"session": "upload"},
          {"kind": "oversize"}, "HTTP 413")
    p.add("T1", "UPL", "define_no_desc", "声音定义缺 description 返回 400", {"session": "upload"},
          {"kind": "no_desc"}, "HTTP 400")

    # ---------------- CRS：角色 × 人群 × 情感 全交叉（T1 配置链路） ----------------
    cross_emos = [e["id"] for e in emotions if e["id"] != "surprised"]
    for r in presets:
        for x in supported_pops:
            for eid in cross_emos:
                emo = next(e for e in emotions if e["id"] == eid)
                p.add("T1", "CRS", "cross_matrix",
                      f"{r['name']} × {x['label']} × {emo['label']}",
                      {"role": r["id"], "population": x["id"], "emotion": eid},
                      {"role": r["id"], "population": x["id"], "emotion": eid,
                       "emotion_params": emo["params"], "emotion_label": emo["label"]},
                      "绑定角色 + 写入情感 + 双读回均一致", account="matrix_a")

    # ---------------- RST：重启恢复（执行器在 T1 末尾重启 AI） ----------------
    p.add("T1", "RST", "restart_character", "重启后会话角色绑定仍在",
          {"session": "restart"}, {"sid": "mx_restart_role", "character_id": "preset_senior_sister"},
          "重启后 GET 返回 preset_senior_sister", account="matrix_a")
    p.add("T1", "RST", "restart_emotion", "重启后情感配置仍在",
          {"emotion": "restart"}, {"sid": "mx_restart_emo", "emotion": "calm"},
          "重启后 GET 返回 calm 参数", account="matrix_a")
    p.add("T1", "RST", "restart_custom_character", "重启后自定义角色仍在",
          {"role": "restart"}, {"name": "重启保留角色"}, "重启后列表中仍存在该角色",
          account="matrix_a")

    # ---------------- COV：覆盖重检补测（分页/并发/墓碑/越权/网关/签名） ----------------
    p.add("T1", "SES", "page_limit_zero", "分页 limit=0 → 422",
          {"session": "pagination"}, {"limit": 0, "offset": 0},
          "网关透传 AI 422（Query ge=1）", account="matrix_a")
    p.add("T1", "SES", "page_limit_over", "分页 limit=101 → 422",
          {"session": "pagination"}, {"limit": 101, "offset": 0},
          "网关透传 AI 422（Query le=100）", account="matrix_a")
    p.add("T1", "SES", "page_offset_negative", "分页 offset=-1 → 422",
          {"session": "pagination"}, {"limit": 20, "offset": -1},
          "网关透传 AI 422（Query ge=0）", account="matrix_a")
    p.add("T1", "SES", "page_offset_beyond", "offset 超范围 → 200 空页且 total 不变",
          {"session": "pagination"}, {"limit": 20, "offset": 999999},
          "200；sessions=[]；total 与首页一致", account="matrix_a")
    p.add("T1", "SES", "session_order", "会话列表按 updated_at 倒序、重命名上浮",
          {"session": "crud"}, {"titles": ["排序-A", "排序-B", "排序-C"]},
          "最近重命名的会话排在三个测试会话最前", account="matrix_a")
    p.add("T1", "SES", "session_rename_concurrent", "同一会话 10 并发重命名一致性",
          {"session": "crud"}, {"workers": 10},
          "全部 200；最终标题为其中之一；无 500/数据损坏", account="matrix_a")
    p.add("T1", "SES", "session_delete_then_bind", "删除后绑定角色 → 404 且不复活",
          {"session": "crud"}, {}, "绑定 404；再读 404；列表仍排除", account="matrix_a")
    p.add("T1", "ISO", "iso_ws_forbidden", "B 账号 WS 连接 A 的会话 → 1008 forbidden",
          {"session": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.7"},
          "close code=1008，reason 含 forbidden", account="matrix_a")
    p.add("T1", "ISO", "iso_delete_emotion", "B 账号 DELETE A 的情感配置（应拒绝）",
          {"emotion": "ownership"}, {"sid": "mx_iso_session", "as": "matrix_b", "ip": "198.18.88.8"},
          "403/404；A 的情感配置仍存在", account="matrix_a")
    p.add("T1", "SYS", "cors_preflight_methods", "CORS 预检覆盖 PUT/PATCH/DELETE",
          {"session": "contract"}, {}, "5 组预检 200 且 Allow-Methods 含对应方法；实际 PATCH 带 ACAO",
          account="matrix_a")
    p.add("T1", "SYS", "internal_sig_invalid", "内部签名缺失/篡改/过期/未来 → 401",
          {"session": "contract"}, {}, "直连 AI 8084 四种伪造均 401 且不泄露数据",
          account="matrix_a")
    p.add("T1", "SYS", "internal_sig_valid", "内部签名合法 + TTL 内重放 → 200",
          {"session": "contract"}, {}, "合法签名 200；同一签名重放两次均 200（设计允许 300s 内）",
          account="matrix_a")
    p.add("T1", "SYS", "upstream_down", "上游连接中断 → 502 且探针 503 降级",
          {"session": "contract"}, {}, "代理端点 502；/health 503 degraded；/ready 503",
          account="matrix_a")
    p.add("T1", "SYS", "upstream_timeout", "上游不应答 → 超时 502",
          {"session": "contract"}, {}, "真实黑洞上游 + 1s 超时 → 代理端点 502 且 10s 内返回",
          account="matrix_a")
    p.add("T1", "EMO", "emotion_delete_resave", "情感解绑后可重新保存（幂等）",
          {"emotion": "crud"}, {}, "DELETE 204×2；GET null；重存 200 且读回一致",
          account="matrix_a")
    p.add("T1", "RST", "restart_tombstone", "重启后墓碑仍生效（404 + 列表排除 + WS 1008）",
          {"session": "restart"}, {}, "重启后读/删 404、列表排除、WS 1008 session_deleted",
          account="matrix_a")


def build_t2(p):
    roles = [r for r in DIM["dimensions"]["role"]["values"]]
    pops = [x for x in DIM["dimensions"]["population"]["values"] if x["supported"]]
    emotions = [e for e in DIM["dimensions"]["emotion"]["types"]]
    factors = [("role", roles), ("population", pops), ("emotion", emotions)]

    # 贪心 pairwise：覆盖任意两维的全部取值组合
    all_rows = list(product(*[[(f[0], v["id"]) for v in f[1]] for f in factors]))
    required = set()
    for i in range(len(factors)):
        for j in range(i + 1, len(factors)):
            for a in factors[i][1]:
                for b in factors[j][1]:
                    required.add((i, a["id"], j, b["id"]))

    def row_pairs(row):
        out = set()
        for i in range(len(factors)):
            for j in range(i + 1, len(factors)):
                out.add((i, row[i][1], j, row[j][1]))
        return out

    chosen = []
    while required:
        best, best_cover = None, -1
        for row in all_rows:
            cover = len(row_pairs(row) & required)
            if cover > best_cover:
                best, best_cover = row, cover
        if best is None:
            break
        chosen.append(best)
        required -= row_pairs(best)

    role_map = {r["id"]: r for r in roles}
    pop_map = {x["id"]: x for x in pops}
    emo_map = {e["id"]: e for e in emotions}
    for n, row in enumerate(chosen):
        rid, pid, eid = row[0][1], row[1][1], row[2][1]
        turns = 3 if n % 8 == 0 else 2
        role = role_map[rid]
        pop = pop_map[pid]
        emo = emo_map[eid]
        utterance = pop["utterance"]
        params = {
            "role_id": rid,
            "role_name": role["name"],
            "persona": role.get("persona_profile") or {},
            "population": pid,
            "voice_profile": pop["voice_profile"],
            "prompt_fragment": pop.get("prompt_fragment"),
            "emotion": eid,
            "emotion_params": emo["params"],
            "turns": turns,
            "utterances": [utterance, "我最喜欢的颜色是蓝色，请记住。", "我最喜欢什么颜色？"][:max(2, turns)],
        }
        p.add("T2", "DIA", "ws_dialog", f"真实多轮对话 {role['name']} × {pop['label']} × {emo['label']}",
              {"role": rid, "population": pid, "emotion": eid, "turns": str(turns)},
              params,
              "WS 无 error；文本非空；第 2 轮能引用第 1 轮记忆；角色名出现在 generating 状态",
              source="human_extension")
    return len(chosen)


def build_t3(p):
    for probe in DIM["t3_probes"]:
        p.add("T3", "PRB", "probe", probe["name"], {"probe": probe["kind"]},
              {"probe": probe["kind"]}, "只探测并记录，不硬跑", source="capability_gap")


def main():
    p = Plan()
    build_t1(p)
    t2_count = build_t2(p)
    build_t3(p)
    layers = {}
    for c in p.cases:
        layers[c["layer"]] = layers.get(c["layer"], 0) + 1
    plan = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": "dimensions.json",
        "accounts": ACCOUNTS,
        "layers": layers,
        "t2_pairwise_rows": t2_count,
        "cases": p.cases,
    }
    out = HERE / "matrix_plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {out}")
    for k, v in sorted(layers.items()):
        print(f"  {k}: {v} 条")
    print(f"  合计: {len(p.cases)} 条")
    fam = {}
    for c in p.cases:
        fam[(c['layer'], c['family'])] = fam.get((c['layer'], c['family']), 0) + 1
    print("  分层族：" + ", ".join(f"{k[0]}/{k[1]}={v}" for k, v in sorted(fam.items())))


if __name__ == "__main__":
    main()
