import hashlib
import io
import os
import wave

import numpy as np
import torch

# ===== 推理参数（固化自 test_outputs/voxcpm2_finetune test_report.json，16/16 样本 100% 成功）=====
# cfg=2.0/steps=10 为标准组合（RTF≈67），情感场景用更高 CFG 强调情感表现力
DEFAULT_CFG_VALUE = 2.0
DEFAULT_TIMESTEPS = 10
EMOTION_CFG_VALUE = 2.5
EMOTION_TIMESTEPS = 12
# 快速模式：减步数+降CFG，速度提升约40%，轻微质量下降，适合交互场景
QUICK_CFG_VALUE = 1.5
QUICK_TIMESTEPS = 6

# 情感参数 → 英文语音描述映射（与微调测试用例一致）
EMOTION_DESCRIPTIONS = {
    "happiness": "happy and excited",
    "sadness": "sad and sympathetic",
    "anger": "angry and firm",
    "fear": "fearful and trembling",
    "surprise": "surprised and amazed",
}

# CPU 上必须关闭 torch.compile：预热阶段触发新版 torch SDPA 维度错误，且编译耗时过长
_OPTIMIZE = False


class VoxCPM2Service:
    def __init__(self):
        self.model_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "models", "voxcpm2"
        )
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # 懒加载：首次合成时才加载模型（约 60s / 4.5GB 内存），避免拖慢服务启动
        self.model = None
        self._load_failed = False

    def _ensure_model(self):
        if self.model is not None:
            return True
        if self._load_failed:
            return False
        try:
            from voxcpm import VoxCPM
            print(f"Loading VoxCPM2 from: {self.model_path}")
            # load_denoiser=False：语伴场景无需降噪，省去 modelscope 降噪模型加载
            self.model = VoxCPM.from_pretrained(
                self.model_path,
                load_denoiser=False,
                optimize=_OPTIMIZE,
            )
            print(f"VoxCPM2 loaded on {self.device}")
            return True
        except Exception as e:
            print(f"Error loading VoxCPM2 model: {e}")
            self._load_failed = True
            return False

    def define_voice(self, audio_data, description, voice_params=None):
        """
        定义语音（元数据级，快速返回；实际音频合成走 synthesize）
        audio_data: 参考音频数据
        description: 语音描述（如"温暖的女声"、"低沉的男声"等）
        voice_params: 语音参数（pitch/speed/volume/emotionIntensity）
        返回: 语音定义描述字符串
        """
        if voice_params is None:
            voice_params = {}
        params_str = ", ".join(f"{k}: {v}" for k, v in voice_params.items())
        if params_str:
            return f"{description}（{params_str}），推理参数: CFG={DEFAULT_CFG_VALUE}, Steps={DEFAULT_TIMESTEPS}"
        return f"{description}，推理参数: CFG={DEFAULT_CFG_VALUE}, Steps={DEFAULT_TIMESTEPS}"

    @staticmethod
    def emotion_to_description(emotion_params):
        """从情感配置参数中取主导情感，映射为测试验证过的英文语音描述"""
        if not emotion_params:
            return None
        dominant = max(emotion_params, key=lambda k: emotion_params.get(k, 0) or 0)
        return EMOTION_DESCRIPTIONS.get(dominant)

    def synthesize(self, text, style_description=None, emotion_params=None, quick_mode=False):
        """
        合成语音，返回 (wav_bytes, sample_rate)；模型不可用时返回 (None, None)

        prompt 模板与推理参数均固化自微调测试：
        - 语音设计: ({description}){text}，CFG=2.0, Steps=10
        - 情感语音: ({emotion_en}){text}，CFG=2.5, Steps=12
        - 快速模式: CFG=1.5, Steps=6（速度提升约40%）
        """
        if not self._ensure_model():
            return None, None

        if quick_mode:
            cfg, steps = QUICK_CFG_VALUE, QUICK_TIMESTEPS
        else:
            cfg, steps = DEFAULT_CFG_VALUE, DEFAULT_TIMESTEPS

        if style_description:
            prompt = f"({style_description}){text}"
        else:
            emotion_desc = self.emotion_to_description(emotion_params)
            if emotion_desc:
                prompt = f"({emotion_desc}){text}"
                if not quick_mode:
                    cfg, steps = EMOTION_CFG_VALUE, EMOTION_TIMESTEPS
            else:
                prompt = text

        wav = self.model.generate(text=prompt, cfg_value=cfg, inference_timesteps=steps)
        sample_rate = getattr(self.model.tts_model, "sample_rate", 16000)

        # float32 → int16 PCM wav（标准库实现，无 soundfile 依赖）
        wav = np.asarray(wav, dtype=np.float32)
        wav = np.clip(wav, -1.0, 1.0)
        pcm = (wav * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())
        return buf.getvalue(), sample_rate

    def generate_voice_embedding(self, voice_definition):
        """
        根据语音定义生成语音嵌入（占位实现：VoxCPM2 管线无独立嵌入接口，
        返回基于定义文本的确定性伪嵌入，供记忆存储与检索链路使用）
        """
        import hashlib, secrets
        seed = int(hashlib.sha256(str(voice_definition).encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        return rng.rand(1, 768).astype(np.float32)
