#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成测试用参考音频 - 用于 VoxCPM2 克隆测试

使用方法:
    python generate_test_audio.py
    python generate_test_audio.py --text "你好，这是一段测试音频"
"""

import os
import sys
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "test_outputs", "reference_audios")


def generate_reference_audio(
    text="你好，这是一段用于测试的参考音频。语伴APP智能语音助手。",
    output_path=None,
    model_path=None
):
    """
    使用 VoxCPM2 生成参考音频
    
    Args:
        text: 要转换的文本
        output_path: 输出音频路径
        model_path: VoxCPM2 模型路径
    
    Returns:
        生成的音频文件路径
    """
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(OUTPUT_DIR, f"reference_{timestamp}.wav")
    
    if model_path is None:
        model_path = os.path.join(PROJECT_ROOT, "models", "voxcpm2")
    
    print(f"[生成] 正在生成参考音频...")
    print(f"[文本] {text}")
    print(f"[模型] {model_path}")
    print(f"[输出] {output_path}")
    
    try:
        from voxcpm import VoxCPM
        
        # 加载模型
        print("[加载] 加载 VoxCPM2 模型...")
        model = VoxCPM.from_pretrained(model_path, load_denoiser=False)
        print("[加载] 模型加载完成")
        
        # 生成音频
        print("[生成] 正在生成音频...")
        wav = model.generate(
            text=text,
            cfg_value=2.0,
            inference_timesteps=10
        )
        
        # 保存音频
        try:
            import soundfile as sf
            sf.write(output_path, wav, model.tts_model.sample_rate)
        except ImportError:
            import wave
            wav_int16 = (wav * 32767).astype(np.int16)
            with wave.open(output_path, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(model.tts_model.sample_rate)
                wf.writeframes(wav_int16.tobytes())
        
        duration = len(wav) / model.tts_model.sample_rate
        print(f"[完成] 音频生成成功!")
        print(f"[时长] {duration:.2f} 秒")
        print(f"[路径] {output_path}")
        
        return output_path
        
    except ImportError:
        print("[错误] voxcpm 库未安装")
        print("[提示] 请运行: pip install voxcpm")
        return None
    except Exception as e:
        print(f"[错误] 音频生成失败: {e}")
        
        # 备选方案：生成简单的正弦波音频
        print("[备选] 生成简单正弦波测试音频...")
        return generate_sine_wave_audio(output_path)


def generate_sine_wave_audio(output_path, duration=3, sample_rate=16000):
    """
    生成正弦波音频作为备选参考音频
    """
    # 生成不同频率的正弦波（模拟语音）
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    
    # 组合多个频率以模拟语音特征
    audio = (
        0.3 * np.sin(2 * np.pi * 200 * t) +  # 低音
        0.2 * np.sin(2 * np.pi * 500 * t) +  # 中音
        0.1 * np.sin(2 * np.pi * 1000 * t)   # 高音
    )
    
    # 添加幅度变化（模拟语音节奏）
    envelope = np.sin(np.pi * t / duration) ** 2
    audio = audio * envelope
    
    # 归一化
    audio = audio / np.max(np.abs(audio)) * 0.8
    audio = audio.astype(np.float32)
    
    # 保存
    try:
        import soundfile as sf
        sf.write(output_path, audio, sample_rate)
    except ImportError:
        import wave
        wav_int16 = (audio * 32767).astype(np.int16)
        with wave.open(output_path, 'w') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(wav_int16.tobytes())
    
    print(f"[备选] 正弦波音频已生成: {output_path}")
    print(f"[时长] {duration:.2f} 秒")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="生成测试用参考音频"
    )
    
    parser.add_argument(
        "--text",
        type=str,
        default="你好，这是一段用于测试的参考音频。语伴APP智能语音助手。",
        help="要转换的文本"
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出音频路径"
    )
    
    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="VoxCPM2 模型路径"
    )
    
    parser.add_argument(
        "--sine-wave",
        action="store_true",
        help="直接生成正弦波音频（不使用模型）"
    )
    
    args = parser.parse_args()
    
    if args.sine_wave:
        output = args.output
        if output is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            output = os.path.join(OUTPUT_DIR, f"sine_wave_{timestamp}.wav")
        generate_sine_wave_audio(output)
    else:
        generate_reference_audio(
            text=args.text,
            output_path=args.output,
            model_path=args.model_path
        )
    
    return 0


if __name__ == "__main__":
    sys.exit(main())