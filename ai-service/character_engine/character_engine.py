"""
角色引擎 —— 三层角色档案模型

1. voice_profile（声学档案，结构化）：性别/年龄段/音色标签/语速 → 拼装 VoxCPM2 英文 prompt
2. persona_profile（人设档案，半结构化）：身份/关系/性格标签/说话风格/自由描述 → LLM system prompt
3. state（动态状态，运行时）：情绪/亲密度/场景 → 不预设，由 emotion_config 和会话上下文维护

设计原则：
- 静态档案预设，动态状态运行时更新
- 不设"精神状态"预设字段（合规红线），心理危机由内容安全护栏处理
- 官方预设角色解决冷启动，用户自定义走向导式（必填声学项给默认值）
"""
import time
import uuid

# ===== 枚举字典（前后端共用，前端向导下拉选项以此为准）=====

GENDERS = {
    "female": "女",
    "male": "男",
}

AGE_GROUPS = {
    # key: (中文展示, 年龄段英文词，按性别区分在 voice_profile_to_prompt 中处理)
    "teen":        ("少年/少女", "teenage"),
    "young_adult": ("青年",     "young"),
    "middle_aged": ("中年",     "middle-aged"),
    "elderly":     ("老年",     "elderly"),
}

# 音色标签：中文展示 → 英文 prompt 词（VoxCPM2 对英文描述响应更稳定，与微调测试一致）
VOICE_TAGS = {
    "gentle":   ("温柔", "gentle"),
    "sweet":    ("甜美", "sweet"),
    "magnetic": ("磁性", "magnetic"),
    "energetic":("元气", "energetic"),
    "deep":     ("低沉", "deep"),
    "calm":     ("沉稳", "calm"),
    "lively":   ("活泼", "lively"),
    "mature":   ("成熟", "mature"),
    "crisp":    ("清脆", "crisp"),
    "husky":    ("沙哑", "husky"),
}

RELATIONS = {
    "friend":  "朋友",
    "mentor":  "导师",
    "partner": "恋人",
    "family":  "家人",
    "companion": "陪伴者",
}

PERSONALITY_TAGS = {
    "warm": "温暖", "humorous": "幽默", "patient": "耐心",
    "rational": "理性", "playful": "俏皮", "reliable": "可靠",
    "knowledgeable": "博学", "encouraging": "鼓励人",
    "outspoken": "直率", "romantic": "浪漫",
}

# ===== 官方预设角色（冷启动）=====
PRESET_CHARACTERS = [
    {
        "id": "preset_senior_sister",
        "name": "温柔学姐",
        "is_preset": True,
        "voice_profile": {
            "gender": "female", "age_group": "young_adult",
            "voice_tags": ["gentle", "sweet"], "speed": 1.0,
        },
        "persona_profile": {
            "identity": "研究生学姐", "relation": "friend",
            "personality_tags": ["warm", "patient", "encouraging"],
            "speaking_style": "像朋友聊天，句子短，常用语气词，会主动关心对方",
            "free_description": "",
        },
    },
    {
        "id": "preset_calm_uncle",
        "name": "沉稳大叔",
        "is_preset": True,
        "voice_profile": {
            "gender": "male", "age_group": "middle_aged",
            "voice_tags": ["magnetic", "calm", "deep"], "speed": 0.95,
        },
        "persona_profile": {
            "identity": "阅历丰富的职场前辈", "relation": "mentor",
            "personality_tags": ["rational", "reliable", "knowledgeable"],
            "speaking_style": "语速平缓，言简意赅，习惯先给结论再解释",
            "free_description": "",
        },
    },
    {
        "id": "preset_energetic_classmate",
        "name": "元气同桌",
        "is_preset": True,
        "voice_profile": {
            "gender": "female", "age_group": "teen",
            "voice_tags": ["energetic", "lively", "crisp"], "speed": 1.1,
        },
        "persona_profile": {
            "identity": "高中同桌", "relation": "friend",
            "personality_tags": ["playful", "humorous", "outspoken"],
            "speaking_style": "活泼跳脱，爱用感叹句，经常开玩笑",
            "free_description": "",
        },
    },
    {
        "id": "preset_career_coach",
        "name": "职场前辈",
        "is_preset": True,
        "voice_profile": {
            "gender": "male", "age_group": "young_adult",
            "voice_tags": ["mature", "calm", "magnetic"], "speed": 1.0,
        },
        "persona_profile": {
            "identity": "互联网行业资深产品经理", "relation": "mentor",
            "personality_tags": ["rational", "encouraging", "reliable"],
            "speaking_style": "结构化表达，分点说明，务实不空谈",
            "free_description": "",
        },
    },
    {
        "id": "preset_warm_companion",
        "name": "暖心陪伴",
        "is_preset": True,
        "voice_profile": {
            "gender": "female", "age_group": "young_adult",
            "voice_tags": ["gentle", "calm", "sweet"], "speed": 0.95,
        },
        "persona_profile": {
            "identity": "专职情感陪伴师", "relation": "companion",
            "personality_tags": ["warm", "patient", "romantic"],
            "speaking_style": "轻声细语，善于倾听和共情，从不打断",
            "free_description": "",
        },
    },
    {
        "id": "preset_funny_buddy",
        "name": "搞笑兄弟",
        "is_preset": True,
        "voice_profile": {
            "gender": "male", "age_group": "young_adult",
            "voice_tags": ["energetic", "lively"], "speed": 1.1,
        },
        "persona_profile": {
            "identity": "大学室友", "relation": "friend",
            "personality_tags": ["humorous", "playful", "outspoken"],
            "speaking_style": "大大咧咧，爱接梗，说话像发弹幕",
            "free_description": "",
        },
    },
]


class CharacterEngine:
    """角色引擎：预设角色 + 用户自定义角色 + 档案 → prompt 转换"""

    def __init__(self):
        # 预设角色常驻内存
        self._presets = {c["id"]: dict(c) for c in PRESET_CHARACTERS}
        # 用户自定义角色（生产环境替换为 Redis/DB，接口不变）
        self._custom = {}

    # ---------- 查询 ----------

    def list_characters(self):
        """列出全部角色（预设在前）"""
        return list(self._presets.values()) + list(self._custom.values())

    def get_character(self, character_id):
        return self._presets.get(character_id) or self._custom.get(character_id)

    def list_options(self):
        """供前端下拉/向导使用的枚举字典"""
        return {
            "genders": GENDERS,
            "age_groups": {k: v[0] for k, v in AGE_GROUPS.items()},
            "voice_tags": {k: v[0] for k, v in VOICE_TAGS.items()},
            "relations": RELATIONS,
            "personality_tags": PERSONALITY_TAGS,
        }

    # ---------- 创建/更新 ----------

    def create_character(self, name, voice_profile=None, persona_profile=None):
        """
        创建自定义角色。声学项缺省时给安全默认值（保证 TTS 可控）。
        voice_profile: {gender, age_group, voice_tags[], speed}
        persona_profile: {identity, relation, personality_tags[], speaking_style, free_description}
        """
        character_id = f"custom_{uuid.uuid4().hex[:12]}"
        character = {
            "id": character_id,
            "name": name or "未命名角色",
            "is_preset": False,
            "created_at": time.time(),
            "voice_profile": self._normalize_voice_profile(voice_profile),
            "persona_profile": self._normalize_persona_profile(persona_profile),
        }
        self._custom[character_id] = character
        return character

    @staticmethod
    def _normalize_voice_profile(vp):
        vp = vp or {}
        gender = vp.get("gender", "female")
        if gender not in GENDERS:
            gender = "female"
        age_group = vp.get("age_group", "young_adult")
        if age_group not in AGE_GROUPS:
            age_group = "young_adult"
        tags = [t for t in (vp.get("voice_tags") or []) if t in VOICE_TAGS]
        if not tags:
            tags = ["gentle"] if gender == "female" else ["magnetic"]
        try:
            speed = float(vp.get("speed", 1.0))
            speed = max(0.7, min(1.3, speed))
        except (TypeError, ValueError):
            speed = 1.0
        return {"gender": gender, "age_group": age_group, "voice_tags": tags, "speed": speed}

    @staticmethod
    def _normalize_persona_profile(pp):
        pp = pp or {}
        tags = [t for t in (pp.get("personality_tags") or []) if t in PERSONALITY_TAGS]
        relation = pp.get("relation", "friend")
        if relation not in RELATIONS:
            relation = "friend"
        return {
            "identity": (pp.get("identity") or "").strip()[:50],
            "relation": relation,
            "personality_tags": tags,
            "speaking_style": (pp.get("speaking_style") or "").strip()[:200],
            "free_description": (pp.get("free_description") or "").strip()[:500],
        }

    # ---------- 档案 → prompt 转换 ----------

    def build_voice_prompt(self, character_id):
        """
        voice_profile → VoxCPM2 英文语音描述
        例: "A young woman, gentle and sweet voice"
        """
        ch = self.get_character(character_id)
        if not ch:
            return None
        return self.voice_profile_to_prompt(ch["voice_profile"])

    @staticmethod
    def voice_profile_to_prompt(vp):
        """voice_profile → VoxCPM2 英文描述，例: A young woman, gentle and sweet voice"""
        is_female = vp["gender"] == "female"
        age_key = AGE_GROUPS.get(vp["age_group"], ("", "young"))[1]
        # 年龄段 × 性别 → 人物名词
        person = {
            "teenage":     ("teenage girl", "teenage boy"),
            "young":       ("young woman", "young man"),
            "middle-aged": ("middle-aged woman", "middle-aged man"),
            "elderly":     ("elderly woman", "elderly man"),
        }.get(age_key, ("young woman", "young man"))[0 if is_female else 1]
        article = "An" if person.startswith(("elderly",)) else "A"
        tag_words = [VOICE_TAGS[t][1] for t in vp["voice_tags"] if t in VOICE_TAGS]
        tail = ", ".join(tag_words) + " voice" if tag_words else "natural voice"
        return f"{article} {person}, {tail}"

    def build_system_prompt(self, character_id):
        """
        persona_profile → LLM system prompt（中文，与 llm_service 默认提示词风格一致）
        """
        ch = self.get_character(character_id)
        if not ch:
            return None
        return self.persona_profile_to_prompt(ch["persona_profile"])

    @staticmethod
    def persona_profile_to_prompt(pp):
        lines = ["你是语伴，一个温暖、有亲和力的智能语音交互助手。"]

        identity = pp.get("identity", "")
        relation = RELATIONS.get(pp.get("relation", "friend"), "朋友")
        if identity:
            lines.append(f"你的身份是{identity}，是用户的{relation}。")
        else:
            lines.append(f"你是用户的{relation}。")

        tags = [PERSONALITY_TAGS[t] for t in pp.get("personality_tags", []) if t in PERSONALITY_TAGS]
        if tags:
            lines.append(f"你的性格特点：{'、'.join(tags)}。")

        style = pp.get("speaking_style", "")
        if style:
            lines.append(f"说话风格：{style}。")

        free = pp.get("free_description", "")
        if free:
            lines.append(f"补充设定：{free}。")

        # 通用约束
        lines.append(
            "回答要简洁自然，适合语音播报，避免使用 Markdown 格式和过长的段落。"
            "每次回复控制在 2-3 句话以内，像朋友聊天一样。"
            "不冒充专业医生、律师或心理咨询师给出诊断或专业建议；"
            "如果用户表达自我伤害倾向，要温和劝阻并建议拨打心理援助热线。"
        )
        return "\n".join(lines)
