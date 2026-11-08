"""
音色目录（GAP-05）

把原先散落在 character_engine 里的音色枚举抽成可查询的目录结构，供方言/音色扩展使用。
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

CATALOG_VERSION = "1.1"

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
}

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
        "default_dialect": DEFAULT_DIALECT,
        "dialect_notice": {d["id"]: dialect_notice(d["id"]) for d in _DIALECT_ENTRIES},
        "defaults": DEFAULT_TAGS,
        "fallback": FALLBACK,
        "capabilities": {
            "dialect": True,          # 方言类型/结构位已接入
            "dialect_ready": any(dialect_available(i) for i in DIALECTS),  # 真实音色是否就绪
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
