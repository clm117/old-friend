"""
音色目录（GAP-05）

把原先散落的音色枚举抽成可查询的目录结构，供方言/音色扩展使用。
第 2 批只做底座；第 3 批在底座上接入 dialect 类型（粤语/四川话/东北话）。

目录结构约定：
- tags: 每个标签带 id / 中文名 / 英文 prompt 词 / locale / voice_type / enabled
- dialects: 方言结构位（id / label / locale / voice_type=dialect / enabled / voice_ids / note）
- locales / voice_types: 扩展位，当前普通话 + 三种方言 locale
- defaults: 性别 → 默认标签（与历史行为一致）；default_dialect=zh-CN
- fallback: 未知标签忽略、空标签回退性别默认；方言未就绪回退普通话并给出提示

红线：20 条标签映射（性别×标签）与默认回退行为逐字不变；方言不参与 voice_prompt 拼装。
"""
import copy

CATALOG_VERSION = "1.2"

GENDERS = {
    "female": "女",
    "male": "男",
}

AGE_GROUPS = {
    # key: (中文展示, 年龄段英文词，按性别区分在 voice_profile_to_prompt 中处理)
    "teen": ("少年/少女", "teenage"),
    "young_adult": ("青年", "young"),
    "middle_aged": ("中年", "middle-aged"),
    "elderly": ("老年", "elderly"),
}

# 音色标签条目：顺序与英文 prompt 词必须与重构前逐字一致（20 条标签映射的回归红线）
_TAG_ENTRIES = [
    {"id": "gentle", "label": "温柔", "en": "gentle"},
    {"id": "sweet", "label": "甜美", "en": "sweet"},
    {"id": "magnetic", "label": "磁性", "en": "magnetic"},
    {"id": "energetic", "label": "元气", "en": "energetic"},
    {"id": "deep", "label": "低沉", "en": "deep"},
    {"id": "calm", "label": "沉稳", "en": "calm"},
    {"id": "lively", "label": "活泼", "en": "lively"},
    {"id": "mature", "label": "成熟", "en": "mature"},
    {"id": "crisp", "label": "清脆", "en": "crisp"},
    {"id": "husky", "label": "沙哑", "en": "husky"},
]

# 兼容旧代码的二元组视图：id → (中文名, 英文 prompt 词)
VOICE_TAGS = {t["id"]: (t["label"], t["en"]) for t in _TAG_ENTRIES}

# 方言结构位（第 3 批）：真实音色未就绪，enabled=False、voice_ids 留空；
# 配额到位后只需填 enabled=True + voice_ids，无需改调用方。
_DIALECT_ENTRIES = [
    {"id": "yue", "label": "粤语", "locale": "zh-yue", "voice_type": "dialect",
     "enabled": False, "voice_ids": [], "voice_id": None,
     "note": "粤语音色暂不可用，已回退普通话音色"},
    {"id": "sichuan", "label": "四川话", "locale": "zh-CN-sichuan", "voice_type": "dialect",
     "enabled": False, "voice_ids": [], "voice_id": None,
     "note": "四川话音色暂不可用，已回退普通话音色"},
    {"id": "dongbei", "label": "东北话", "locale": "zh-CN-dongbei", "voice_type": "dialect",
     "enabled": False, "voice_ids": [], "voice_id": None,
     "note": "东北话音色暂不可用，已回退普通话音色"},
]

DIALECTS = {d["id"]: d for d in _DIALECT_ENTRIES}
DEFAULT_DIALECT = "zh-CN"

# 默认标签与回退策略（重构前行为：女声 gentle / 男声 magnetic）
DEFAULT_TAGS = {"female": "gentle", "male": "magnetic"}
FALLBACK = {
    "unknown_tag": "ignore",      # 未知标签直接忽略
    "empty_tags": "gender_default",  # 标签为空时回退性别默认
    "invalid_speed": 1.0,         # 语速非法时回退默认值
    "speed_range": [0.5, 2.0],    # 语速合法区间（越界由接口层 422）
    "unknown_dialect": "default",  # 未知方言回退普通话
    "unavailable_dialect": "default_with_notice",  # 方言未就绪：回退默认 + 提示
}

_LOCALES = {
    "zh-CN": {"label": "普通话", "enabled": True},
    "zh-yue": {"label": "粤语", "enabled": False},
    "zh-CN-sichuan": {"label": "四川话", "enabled": False},
    "zh-CN-dongbei": {"label": "东北话", "enabled": False},
}
_VOICE_TYPES = {
    "standard": {"label": "标准音色", "enabled": True},
    "dialect": {"label": "方言音色", "enabled": True},
    "preset": {"label": "预设音色", "enabled": True},
}

# ===== 12 个常用预设音色 =====
# 与 10 条标签并存：预设只新增可查询条目，不参与既有 20 条标签映射的拼装。
# 当前无真实 voice_id：enabled=False、voice_id=None，选择后生成 voice_prompt 并明确提示回退默认音色。
_PRESET_ENTRIES = [
    {"id": "vp_guaiguai_xuedi", "name": "乖巧学弟", "gender": "male", "age_group": "teen",
     "persona": "声音清亮偏软，带学生气与亲近感", "style_tags": ["sweet", "energetic"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_buji_shaonian", "name": "不羁少年", "gender": "male", "age_group": "teen",
     "persona": "略带沙哑、随性不羁的少年感", "style_tags": ["husky", "lively"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_ligong_xueba", "name": "理工学霸", "gender": "male", "age_group": "young_adult",
     "persona": "理性克制、吐字清晰，像讲题一样有条理", "style_tags": ["calm", "crisp"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_yangguang_xingnan", "name": "阳光型男", "gender": "male", "age_group": "young_adult",
     "persona": "明亮有朝气，笑起来有感染力的青年男声", "style_tags": ["energetic", "magnetic"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_xiangsheng_pingshu", "name": "相声评书", "gender": "male", "age_group": "middle_aged",
     "persona": "说书人的抑扬顿挫与节奏感，带舞台腔", "style_tags": ["lively", "husky"],
     "style_words": ["theatrical", "storytelling", "rhythmic"],
     "capabilities": {"emotion": False, "style": False, "dialect": False},
     "capability_note": "相声评书的腔调依赖真实音色，默认音色无法完整表达"},
    {"id": "vp_xinwen_nansheng", "name": "新闻男声", "gender": "male", "age_group": "middle_aged",
     "persona": "播报腔、字正腔圆，稳重可信", "style_tags": ["crisp", "calm"],
     "style_words": ["broadcast", "authoritative"],
     "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_xinwen_nvsheng", "name": "新闻女声", "gender": "female", "age_group": "middle_aged",
     "persona": "播报腔、清晰利落，专业稳重", "style_tags": ["crisp", "calm"],
     "style_words": ["broadcast", "authoritative"],
     "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_zhixing_shunv", "name": "知性熟女", "gender": "female", "age_group": "middle_aged",
     "persona": "成熟知性、温和从容，带阅历感", "style_tags": ["mature", "gentle"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_wenwan_mishu", "name": "温婉秘书", "gender": "female", "age_group": "young_adult",
     "persona": "轻柔克制、礼貌周到，像贴身助理", "style_tags": ["gentle", "sweet"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_zhixing_jingying", "name": "智性精英", "gender": "female", "age_group": "young_adult",
     "persona": "冷静自信、逻辑感强，带一点距离感", "style_tags": ["calm", "magnetic"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_langxian_shaonv", "name": "浪险少女", "gender": "female", "age_group": "teen",
     "persona": "俏皮张扬、语速轻快，带点小嚣张", "style_tags": ["lively", "crisp"],
     "style_words": [], "capabilities": {"emotion": False, "style": True, "dialect": False}},
    {"id": "vp_guiqi_qianjin", "name": "贵气千金", "gender": "female", "age_group": "young_adult",
     "persona": "优雅从容、尾音柔和，带矜贵气质", "style_tags": ["mature", "magnetic"],
     "style_words": ["elegant", "refined"],
     "capabilities": {"emotion": False, "style": True, "dialect": False}},
]

# 真实 voice_id 绑定位：当前全部为空，配额到位后仅需填 enabled=True + voice_id
for _p in _PRESET_ENTRIES:
    _p.update({
        "locale": "zh-CN",
        "voice_type": "preset",
        "fallback": DEFAULT_TAGS[_p["gender"]],
        "voice_id": None,
        "enabled": False,
        "note": f"{_p['name']}音色未就绪，使用默认音色",
    })

VOICE_PRESETS = {p["id"]: p for p in _PRESET_ENTRIES}


def preset_available(preset_id):
    p = VOICE_PRESETS.get(preset_id)
    return bool(p and p["enabled"] and p["voice_id"])


def preset_options():
    """id → 中文名（供选择器展示）"""
    return {p["id"]: p["name"] for p in _PRESET_ENTRIES}


def preset_list():
    """12 条预设的对外视图列表（含就绪状态与提示）"""
    return [preset_status(p["id"]) for p in _PRESET_ENTRIES]


def preset_status(preset_id):
    """单个预设的对外视图：含 fallback / capabilities / 就绪状态与提示"""
    p = VOICE_PRESETS.get(preset_id)
    if not p:
        return None
    ready = preset_available(preset_id)
    status = {
        "id": p["id"], "name": p["name"], "gender": p["gender"],
        "age_group": p["age_group"], "persona": p["persona"],
        "locale": p["locale"], "voice_type": p["voice_type"],
        "fallback": p["fallback"], "voice_id": p["voice_id"],
        "capabilities": dict(p["capabilities"]), "ready": ready,
        "notice": None if ready else p["note"],
    }
    if p.get("capability_note"):
        status["capability_note"] = p["capability_note"]
    return status


def preset_prompt(preset_id):
    """预设 → 与既有 20 条标签同格式的英文 prompt（供 TTS 使用）"""
    p = VOICE_PRESETS.get(preset_id)
    if not p:
        return None
    is_female = p["gender"] == "female"
    age_key = AGE_GROUPS.get(p["age_group"], ("", "young"))[1]
    person = {
        "teenage": ("teenage girl", "teenage boy"),
        "young": ("young woman", "young man"),
        "middle-aged": ("middle-aged woman", "middle-aged man"),
        "elderly": ("elderly woman", "elderly man"),
    }.get(age_key, ("young woman", "young man"))[0 if is_female else 1]
    article = "An" if person.startswith(("elderly",)) else "A"
    words = [VOICE_TAGS[t][1] for t in p["style_tags"] if t in VOICE_TAGS]
    words.extend(p.get("style_words") or [])
    tail = ", ".join(words) + " voice" if words else "natural voice"
    return f"{article} {person}, {tail}"

_DISPLAY_TAGS = [dict(t, locale="zh-CN", voice_type="standard", enabled=True) for t in _TAG_ENTRIES]


def dialect_options():
    """id → 中文名（供选择器展示）"""
    return {d["id"]: d["label"] for d in _DIALECT_ENTRIES}


def is_known_dialect(dialect_id):
    return dialect_id in DIALECTS


def resolve_dialect(value):
    """空值/未知方言返回 None（表示普通话默认）"""
    if not value:
        return None
    return value if value in DIALECTS else None


def dialect_available(dialect_id):
    """真实音色可用 = 开关打开且已配置 voice_id"""
    d = DIALECTS.get(dialect_id)
    if not d:
        return False
    return bool(d["enabled"] and (d["voice_ids"] or d["voice_id"]))


def dialect_notice(dialect_id):
    """未就绪方言的用户可理解提示；可用/未知识别返回 None"""
    d = DIALECTS.get(dialect_id)
    if not d or dialect_available(dialect_id):
        return None
    return d["note"]


def dialect_voice_ids(dialect_id):
    d = DIALECTS.get(dialect_id) or {}
    ids = list(d.get("voice_ids") or [])
    if d.get("voice_id") and d["voice_id"] not in ids:
        ids.insert(0, d["voice_id"])
    return ids if dialect_available(dialect_id) else []


def dialect_status():
    """前端下拉用：3 项方言 + 可用性/不可用提示"""
    return [{
        "id": d["id"], "label": d["label"], "locale": d["locale"],
        "enabled": dialect_available(d["id"]),
        "notice": dialect_notice(d["id"]),
    } for d in _DIALECT_ENTRIES]


def default_dialect():
    return {"id": DEFAULT_DIALECT, "label": _LOCALES[DEFAULT_DIALECT]["label"]}


def get_catalog():
    """完整目录（复制返回，调用方修改不会污染全局）"""
    return copy.deepcopy({
        "version": CATALOG_VERSION,
        "genders": GENDERS,
        "age_groups": {k: v[0] for k, v in AGE_GROUPS.items()},
        "locales": _LOCALES,
        "voice_types": _VOICE_TYPES,
        "tags": _DISPLAY_TAGS,
        "dialects": _DIALECT_ENTRIES,
        "voice_presets": [preset_status(p["id"]) for p in _PRESET_ENTRIES],
        "preset_count": len(_PRESET_ENTRIES),
        "default_dialect": DEFAULT_DIALECT,
        "dialect_notice": {d["id"]: dialect_notice(d["id"]) for d in _DIALECT_ENTRIES},
        "defaults": DEFAULT_TAGS,
        "fallback": FALLBACK,
        "capabilities": {
            "dialect": True,          # 方言类型/结构位已接入
            "dialect_ready": any(dialect_available(i) for i in DIALECTS),  # 真实音色是否就绪
            "voice_presets": True,    # 12 条预设目录已接入
            "preset_voice_ready": any(preset_available(i) for i in VOICE_PRESETS),
            "child_voice": False,
            "emotion_voice": False,
        },
    })


def tag_options():
    """id → 中文名，供角色选项接口使用（与重构前结构一致）"""
    return {t["id"]: t["label"] for t in _TAG_ENTRIES}


def tag_ids():
    return [t["id"] for t in _TAG_ENTRIES]


def resolve_tag_ids(tags):
    """过滤未知标签，保持传入顺序"""
    return [t for t in (tags or []) if t in VOICE_TAGS]


def default_tag_ids(gender):
    return [DEFAULT_TAGS.get(gender, DEFAULT_TAGS["female"])]
