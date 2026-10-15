#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoxCPM2 微调效果测试脚本 - 模拟语伴APP语音定义场景

测试场景：
1. 语音设计（Voice Design）：通过自然语言描述生成新语音
2. 基础克隆（Basic Cloning）：使用参考音频克隆声音
3. 可控克隆（Controllable Cloning）：克隆声音并控制风格
4. 高级克隆（Ultimate Cloning）：参考音频+转写文本的高质量克隆
5. 情感语音生成：测试情感参数对语音的影响
6. 参数组合测试：测试pitch/speed/volume等参数的微调效果
"""

import os
import sys
import time
import json
import argparse
import numpy as np
import torch
from pathlib import Path
from datetime import datetime

# 语伴项目配置
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "voxcpm2")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "test_outputs", "voxcpm2_finetune")

# 测试用例配置
TEST_CASES = {
    "voice_design": [
        {
            "name": "温柔女声",
            "description": "A young woman, gentle and sweet voice",
            "text": "你好，欢迎使用语伴智能语音助手。今天天气不错，我们一起来聊聊天吧。"
        },
        {
            "name": "低沉男声",
            "description": "A middle-aged man, deep and magnetic voice",
            "text": "我是语伴，您的智能语音伙伴。有什么我可以帮您的吗？"
        },
        {
            "name": "活力少年",
            "description": "A young boy, energetic and lively voice",
            "text": "哇！太棒了！我们一起来玩吧！语伴功能真强大！"
        },
        {
            "name": "知性女声",
            "description": "A mature woman, intellectual and elegant voice",
            "text": "语伴可以根据您的需求调整语音风格，让交流更加自然舒适。"
        },
        {
            "name": "亲切长辈",
            "description": "An elderly person, warm and kind voice",
            "text": "孩子，有什么心事都可以跟我说，语伴会一直陪着你。"
        }
    ],
    "controllable_cloning": [
        {
            "name": "稍快语速-愉悦",
            "style": "slightly faster, cheerful tone",
            "text": "这是一个克隆语音的测试，语速稍快，带着愉悦的心情。"
        },
        {
            "name": "温柔缓慢-平静",
            "style": "slower and gentle, calm tone",
            "text": "克隆语音可以保留原音特色，同时调整说话风格。"
        },
        {
            "name": "有力强调-严肃",
            "style": "emphatic and serious tone",
            "text": "请注意，重要的事情需要强调说明，这是关键信息。"
        }
    ],
    "emotion_test": [
        {
            "name": "高兴",
            "emotion": "happy and excited",
            "text": "太好了！语伴帮我找到了我想要的声音，真开心！"
        },
        {
            "name": "悲伤",
            "emotion": "sad and sympathetic",
            "text": "听到这个消息我很难过，语伴会一直陪伴着你。"
        },
        {
            "name": "生气",
            "emotion": "angry and firm",
            "text": "这是最后一次提醒，请务必按时完成任务。"
        },
        {
            "name": "恐惧",
            "emotion": "fearful and trembling",
            "text": "外面的雷声好大，我有点害怕，语伴能陪我聊聊吗？"
        },
        {
            "name": "惊讶",
            "emotion": "surprised and amazed",
            "text": "天哪！这是真的吗？语伴居然能做到这种程度！"
        }
    ],
    "param_tuning": [
        {
            "name": "高音调-快语速",
            "params": "higher pitch, faster speed",
            "text": "测试高音调和快语速的组合效果。"
        },
        {
            "name": "低音调-慢语速",
            "params": "lower pitch, slower speed",
            "text": "测试低音调和慢语速的组合效果。"
        },
        {
            "name": "中等参数",
            "params": "normal pitch and speed",
            "text": "测试默认参数下的语音生成效果。"
        }
    ]
}


class VoxCPM2Tester:
    """VoxCPM2 微调效果测试器"""
    
    def __init__(self, model_path=None, output_dir=None):
        self.model_path = model_path or MODEL_PATH
        self.output_dir = output_dir or OUTPUT_DIR
        self.model = None
        self.device = None
        self.results = []
        self._setup_output_dir()
        
    def _setup_output_dir(self):
        """设置输出目录"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.test_output_dir = os.path.join(self.output_dir, timestamp)
        os.makedirs(self.test_output_dir, exist_ok=True)
        print(f"[目录] 测试结果将保存至: {self.test_output_dir}")
        
    def load_model(self, use_denoiser=True):
        """加载 VoxCPM2 模型"""
        print(f"\n{'='*60}")
        print(f"[加载] 正在加载 VoxCPM2 模型...")
        print(f"[路径] {self.model_path}")
        
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"模型路径不存在: {self.model_path}")
        
        # 检查是否安装 voxcpm 库
        try:
            from voxcpm import VoxCPM
            print("[依赖] voxcpm 库已安装")
        except ImportError:
            print("[依赖] 尝试安装 voxcpm 库...")
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "voxcpm"])
            from voxcpm import VoxCPM
        
        # 加载模型
        start_time = time.time()
        try:
            self.model = VoxCPM.from_pretrained(
                self.model_path,
                load_denoiser=use_denoiser,
                optimize=False  # CPU 上 torch.compile 预热会触发 SDPA 维度错误，且编译耗时过长
            )
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            load_time = time.time() - start_time
            print(f"[成功] 模型加载完成，耗时 {load_time:.1f}s")
            print(f"[设备] 使用 {self.device}")
            return True
        except Exception as e:
            print(f"[错误] 模型加载失败: {e}")
            print("[提示] 如果没有GPU，尝试使用较小模型或CPU推理")
            raise
    
    def test_voice_design(self):
        """测试语音设计功能"""
        print(f"\n{'='*60}")
        print("[测试] 语音设计（Voice Design）- 通过文本描述生成语音")
        print(f"{'='*60}")
        
        results = []
        for i, case in enumerate(TEST_CASES["voice_design"]):
            print(f"\n--- 测试 {i+1}/{len(TEST_CASES['voice_design'])}: {case['name']} ---")
            print(f"  描述: {case['description']}")
            print(f"  文本: {case['text']}")
            
            try:
                # VoxCPM2 语音设计：在文本开头用括号描述语音特征
                prompt = f"({case['description']}){case['text']}"
                
                start_time = time.time()
                wav = self.model.generate(
                    text=prompt,
                    cfg_value=2.0,
                    inference_timesteps=10
                )
                gen_time = time.time() - start_time
                
                # 保存音频
                filename = f"voice_design_{case['name'].replace(' ', '_')}.wav"
                filepath = os.path.join(self.test_output_dir, filename)
                self._save_audio(wav, filepath)
                
                # 记录结果
                result = {
                    "test_type": "voice_design",
                    "name": case["name"],
                    "description": case["description"],
                    "text": case["text"],
                    "prompt": prompt,
                    "output_file": filename,
                    "gen_time": gen_time,
                    "audio_duration": len(wav) / self.model.tts_model.sample_rate,
                    "success": True
                }
                results.append(result)
                
                print(f"  [成功] 生成耗时: {gen_time:.2f}s, 音频时长: {result['audio_duration']:.2f}s")
                print(f"  [输出] {filename}")
                
            except Exception as e:
                print(f"  [失败] {e}")
                results.append({
                    "test_type": "voice_design",
                    "name": case["name"],
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_controllable_cloning(self, reference_audio_path=None):
        """测试可控克隆功能"""
        print(f"\n{'='*60}")
        print("[测试] 可控克隆（Controllable Cloning）- 克隆并控制风格")
        print(f"{'='*60}")
        
        if not reference_audio_path:
            print("[跳过] 需要提供参考音频文件路径")
            print("[提示] 可以使用 test_voice_define.ps1 生成的音频或自己录制的音频")
            return []
        
        if not os.path.exists(reference_audio_path):
            print(f"[警告] 参考音频不存在: {reference_audio_path}")
            return []
        
        results = []
        for i, case in enumerate(TEST_CASES["controllable_cloning"]):
            print(f"\n--- 测试 {i+1}/{len(TEST_CASES['controllable_cloning'])}: {case['name']} ---")
            print(f"  风格: {case['style']}")
            print(f"  文本: {case['text']}")
            
            try:
                # VoxCPM2 可控克隆：参考音频 + 风格描述
                prompt = f"({case['style']}){case['text']}"
                
                start_time = time.time()
                wav = self.model.generate(
                    text=prompt,
                    reference_wav_path=reference_audio_path,
                    cfg_value=2.0,
                    inference_timesteps=10
                )
                gen_time = time.time() - start_time
                
                # 保存音频
                filename = f"clone_controllable_{case['name'].replace(' ', '_')}.wav"
                filepath = os.path.join(self.test_output_dir, filename)
                self._save_audio(wav, filepath)
                
                # 记录结果
                result = {
                    "test_type": "controllable_cloning",
                    "name": case["name"],
                    "style": case["style"],
                    "text": case["text"],
                    "reference_audio": reference_audio_path,
                    "output_file": filename,
                    "gen_time": gen_time,
                    "audio_duration": len(wav) / self.model.tts_model.sample_rate,
                    "success": True
                }
                results.append(result)
                
                print(f"  [成功] 生成耗时: {gen_time:.2f}s, 音频时长: {result['audio_duration']:.2f}s")
                print(f"  [输出] {filename}")
                
            except Exception as e:
                print(f"  [失败] {e}")
                results.append({
                    "test_type": "controllable_cloning",
                    "name": case["name"],
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_emotion_voice(self):
        """测试情感语音生成"""
        print(f"\n{'='*60}")
        print("[测试] 情感语音生成 - 测试情感参数对语音的影响")
        print(f"{'='*60}")
        
        results = []
        for i, case in enumerate(TEST_CASES["emotion_test"]):
            print(f"\n--- 测试 {i+1}/{len(TEST_CASES['emotion_test'])}: {case['name']} ---")
            print(f"  情感: {case['emotion']}")
            print(f"  文本: {case['text']}")
            
            try:
                # 将情感作为语音描述的一部分
                prompt = f"({case['emotion']}){case['text']}"
                
                start_time = time.time()
                wav = self.model.generate(
                    text=prompt,
                    cfg_value=2.5,  # 更高的CFG值以强调情感
                    inference_timesteps=12
                )
                gen_time = time.time() - start_time
                
                # 保存音频
                filename = f"emotion_{case['name'].replace(' ', '_')}.wav"
                filepath = os.path.join(self.test_output_dir, filename)
                self._save_audio(wav, filepath)
                
                # 记录结果
                result = {
                    "test_type": "emotion_voice",
                    "name": case["name"],
                    "emotion": case["emotion"],
                    "text": case["text"],
                    "prompt": prompt,
                    "cfg_value": 2.5,
                    "inference_timesteps": 12,
                    "output_file": filename,
                    "gen_time": gen_time,
                    "audio_duration": len(wav) / self.model.tts_model.sample_rate,
                    "success": True
                }
                results.append(result)
                
                print(f"  [成功] 生成耗时: {gen_time:.2f}s, 音频时长: {result['audio_duration']:.2f}s")
                print(f"  [输出] {filename}")
                
            except Exception as e:
                print(f"  [失败] {e}")
                results.append({
                    "test_type": "emotion_voice",
                    "name": case["name"],
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_param_tuning(self):
        """测试参数微调效果"""
        print(f"\n{'='*60}")
        print("[测试] 参数微调 - 测试不同参数组合的效果")
        print(f"{'='*60}")
        
        results = []
        for i, case in enumerate(TEST_CASES["param_tuning"]):
            print(f"\n--- 测试 {i+1}/{len(TEST_CASES['param_tuning'])}: {case['name']} ---")
            print(f"  参数: {case['params']}")
            print(f"  文本: {case['text']}")
            
            try:
                prompt = f"({case['params']}){case['text']}"
                
                # 测试不同的推理参数组合
                param_configs = [
                    {"cfg_value": 2.0, "inference_timesteps": 10},  # 默认
                    {"cfg_value": 3.0, "inference_timesteps": 15},  # 更高CFG
                ]
                
                for j, config in enumerate(param_configs):
                    start_time = time.time()
                    wav = self.model.generate(
                        text=prompt,
                        cfg_value=config["cfg_value"],
                        inference_timesteps=config["inference_timesteps"]
                    )
                    gen_time = time.time() - start_time
                    
                    # 保存音频
                    filename = f"param_tune_{case['name'].replace(' ', '_')}_cfg{config['cfg_value']}_steps{config['inference_timesteps']}.wav"
                    filepath = os.path.join(self.test_output_dir, filename)
                    self._save_audio(wav, filepath)
                    
                    result = {
                        "test_type": "param_tuning",
                        "name": case["name"],
                        "params": case["params"],
                        "text": case["text"],
                        "cfg_value": config["cfg_value"],
                        "inference_timesteps": config["inference_timesteps"],
                        "output_file": filename,
                        "gen_time": gen_time,
                        "audio_duration": len(wav) / self.model.tts_model.sample_rate,
                        "success": True
                    }
                    results.append(result)
                    
                    print(f"  [CFG={config['cfg_value']}, Steps={config['inference_timesteps']}] 耗时: {gen_time:.2f}s")
                
            except Exception as e:
                print(f"  [失败] {e}")
                results.append({
                    "test_type": "param_tuning",
                    "name": case["name"],
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_voxcpm2_api(self):
        """测试 VoxCPM2 API 兼容性 - 调用语伴项目的 API"""
        print(f"\n{'='*60}")
        print("[测试] API 兼容性 - 测试与语伴后端 API 的集成")
        print(f"{'='*60}")
        
        import requests
        
        # 测试配置
        api_base_urls = [
            "http://localhost:8003",  # VoxCPM2 独立服务
            "http://localhost:8084",  # AI 服务
            "http://localhost:8005",  # API Gateway
        ]
        
        results = []
        for url in api_base_urls:
            print(f"\n--- 测试 API: {url} ---")
            
            # 健康检查
            try:
                response = requests.get(f"{url}/health", timeout=2)
                if response.status_code == 200:
                    print(f"  [健康检查] {url}/health OK")
                    results.append({
                        "api_url": url,
                        "endpoint": "/health",
                        "status": "ok",
                        "status_code": 200
                    })
                else:
                    print(f"  [健康检查] {url}/health 返回 {response.status_code}")
            except Exception as e:
                print(f"  [健康检查] {url}/health 不可达: {e}")
                results.append({
                    "api_url": url,
                    "endpoint": "/health",
                    "status": "unreachable",
                    "error": str(e)
                })
            
            # 语音定义接口测试
            test_payload = {
                "session_id": "test_session",
                "description": "温柔女声测试",
                "pitch": 1.0,
                "speed": 1.0,
                "volume": 1.0,
                "emotion_intensity": 0.5
            }
            
            try:
                # 使用 multipart/form-data 上传测试音频
                test_audio = np.zeros(16000, dtype=np.float32)  # 1秒静音
                
                files = {
                    'audio': ('test.wav', test_audio.tobytes(), 'audio/wav')
                }
                data = {
                    'session_id': 'test_session',
                    'description': '温柔女声测试',
                    'pitch': '1.0',
                    'speed': '1.0',
                    'volume': '1.0',
                    'emotion_intensity': '0.5'
                }
                
                response = requests.post(f"{url}/api/voice/define", 
                                        files=files, data=data, timeout=5)
                print(f"  [语音定义] {url}/api/voice/define -> {response.status_code}")
                
                if response.status_code == 200:
                    result_data = response.json()
                    print(f"    响应: {json.dumps(result_data, ensure_ascii=False, indent=2)[:200]}...")
                    
            except Exception as e:
                print(f"  [语音定义] {url}/api/voice/define 失败: {e}")
        
        return results
    
    def run_all_tests(self, reference_audio_path=None, skip_api=False):
        """运行所有测试"""
        print(f"\n{'#'*60}")
        print(f"# VoxCPM2 微调效果测试 - 开始")
        print(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"# 模型路径: {self.model_path}")
        print(f"# 输出目录: {self.test_output_dir}")
        print(f"{'#'*60}")
        
        all_results = {
            "test_info": {
                "start_time": datetime.now().isoformat(),
                "model_path": self.model_path,
                "output_dir": self.test_output_dir
            }
        }
        
        try:
            # 1. 加载模型
            self.load_model()
        except Exception as e:
            print(f"\n[致命错误] 模型加载失败，无法继续测试: {e}")
            return all_results
        
        # 2. 语音设计测试
        try:
            all_results["voice_design"] = self.test_voice_design()
        except Exception as e:
            print(f"\n[错误] 语音设计测试异常: {e}")
            all_results["voice_design"] = [{"success": False, "error": str(e)}]
        
        # 3. 可控克隆测试（需要参考音频）
        if reference_audio_path:
            try:
                all_results["controllable_cloning"] = self.test_controllable_cloning(reference_audio_path)
            except Exception as e:
                print(f"\n[错误] 可控克隆测试异常: {e}")
                all_results["controllable_cloning"] = [{"success": False, "error": str(e)}]
        
        # 4. 情感语音测试
        try:
            all_results["emotion_voice"] = self.test_emotion_voice()
        except Exception as e:
            print(f"\n[错误] 情感语音测试异常: {e}")
            all_results["emotion_voice"] = [{"success": False, "error": str(e)}]
        
        # 5. 参数微调测试
        try:
            all_results["param_tuning"] = self.test_param_tuning()
        except Exception as e:
            print(f"\n[错误] 参数微调测试异常: {e}")
            all_results["param_tuning"] = [{"success": False, "error": str(e)}]
        
        # 6. API 兼容性测试
        if not skip_api:
            try:
                all_results["api_compatibility"] = self.test_voxcpm2_api()
            except Exception as e:
                print(f"\n[错误] API 兼容性测试异常: {e}")
                all_results["api_compatibility"] = [{"success": False, "error": str(e)}]
        
        # 7. 保存测试报告
        all_results["test_info"]["end_time"] = datetime.now().isoformat()
        self._save_report(all_results)
        
        return all_results
    
    def _save_audio(self, wav_data, filepath):
        """保存音频文件"""
        try:
            import soundfile as sf
            sf.write(filepath, wav_data, self.model.tts_model.sample_rate)
            print(f"  [保存] {filepath}")
        except ImportError:
            # 使用 wave 模块作为备选
            import wave
            import struct
            
            # 确保音频数据为 float32
            if isinstance(wav_data, np.ndarray) and wav_data.dtype != np.float32:
                wav_data = wav_data.astype(np.float32)
            
            # 将 float32 转换为 int16
            wav_int16 = (wav_data * 32767).astype(np.int16)
            
            with wave.open(filepath, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(self.model.tts_model.sample_rate)
                wf.writeframes(wav_int16.tobytes())
            
            print(f"  [保存] {filepath} (使用 wave 模块)")
    
    def _save_report(self, results):
        """保存测试报告"""
        report_path = os.path.join(self.test_output_dir, "test_report.json")
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[报告] 测试报告已保存: {report_path}")
        
        # 打印摘要
        self._print_summary(results)
    
    def _print_summary(self, results):
        """打印测试摘要"""
        print(f"\n{'#'*60}")
        print(f"# 测试结果摘要")
        print(f"{'#'*60}")
        
        for test_type, test_results in results.items():
            if test_type == "test_info":
                continue
            
            if not isinstance(test_results, list):
                continue
            
            total = len(test_results)
            success = sum(1 for r in test_results if r.get("success", False))
            failed = total - success
            
            print(f"\n[{test_type}]")
            print(f"  总计: {total}, 成功: {success}, 失败: {failed}")
            
            if success > 0:
                # 计算平均生成时间
                success_results = [r for r in test_results if r.get("success", False)]
                avg_time = sum(r["gen_time"] for r in success_results) / len(success_results)
                avg_duration = sum(r["audio_duration"] for r in success_results) / len(success_results)
                print(f"  平均生成时间: {avg_time:.2f}s")
                print(f"  平均音频时长: {avg_duration:.2f}s")
                
                # 计算 RTF (Real-Time Factor)
                rtf = avg_time / avg_duration if avg_duration > 0 else float('inf')
                print(f"  RTF (实时率): {rtf:.3f} (越低越好)")
            
            if failed > 0:
                print(f"  失败详情:")
                for r in test_results:
                    if not r.get("success", False):
                        print(f"    - {r.get('name', 'Unknown')}: {r.get('error', 'Unknown error')}")


def main():
    parser = argparse.ArgumentParser(
        description="VoxCPM2 微调效果测试脚本 - 模拟语伴APP语音定义场景",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 运行所有测试
  python test_voxcpm2_finetune.py
  
  # 指定参考音频路径（用于克隆测试）
  python test_voxcpm2_finetune.py --ref-audio reference.wav
  
  # 跳过 API 兼容性测试
  python test_voxcpm2_finetune.py --skip-api
  
  # 指定模型路径
  python test_voxcpm2_finetune.py --model-path /path/to/voxcpm2
        """
    )
    
    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="VoxCPM2 模型路径 (默认: models/voxcpm2)"
    )
    
    parser.add_argument(
        "--ref-audio",
        type=str,
        default=None,
        help="参考音频文件路径 (用于可控克隆测试)"
    )
    
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="测试输出目录 (默认: test_outputs/voxcpm2_finetune)"
    )
    
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="跳过 API 兼容性测试"
    )
    
    parser.add_argument(
        "--cfg-value",
        type=float,
        default=2.0,
        help="默认 CFG (Classifier-Free Guidance) 值 (默认: 2.0)"
    )
    
    parser.add_argument(
        "--timesteps",
        type=int,
        default=10,
        help="默认推理步数 (默认: 10)"
    )
    
    args = parser.parse_args()
    
    # 创建测试器
    tester = VoxCPM2Tester(
        model_path=args.model_path,
        output_dir=args.output_dir
    )
    
    # 运行测试
    results = tester.run_all_tests(
        reference_audio_path=args.ref_audio,
        skip_api=args.skip_api
    )
    
    return 0


if __name__ == "__main__":
    sys.exit(main())