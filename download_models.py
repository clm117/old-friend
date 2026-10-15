import os
import subprocess
import sys

# 添加国内镜像源
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

def download_model(model_id, save_path, model_type):
    print(f"\n{'='*50}")
    print(f"下载 {model_type}...")
    print(f"模型ID: {model_id}")
    print(f"保存路径: {save_path}")
    print(f"{'='*50}")

    python_exe = r"D:\Program Files (x86)\Python 3.11\python.exe"

    if model_type == "Fun-Audio-Chat-8B":
        code = f'''
import os
os.makedirs(r"{save_path}", exist_ok=True)
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
import torch

print("正在下载 Fun-Audio-Chat-8B 模型...")
model = AutoModelForSpeechSeq2Seq.from_pretrained(
    "{model_id}",
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)
processor = AutoProcessor.from_pretrained("{model_id}")
model.save_pretrained(r"{save_path}")
processor.save_pretrained(r"{save_path}")
print(f"模型已保存到: {save_path}")
'''
    elif model_type == "VoxCPM2":
        code = f'''
import os
os.makedirs(r"{save_path}", exist_ok=True)
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

print("正在下载 VoxCPM2 模型...")
model = AutoModelForCausalLM.from_pretrained(
    "{model_id}",
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)
tokenizer = AutoTokenizer.from_pretrained("{model_id}")
model.save_pretrained(r"{save_path}")
tokenizer.save_pretrained(r"{save_path}")
print(f"模型已保存到: {save_path}")
'''
    elif model_type == "Whisper":
        code = f'''
import os
os.makedirs(r"{save_path}", exist_ok=True)
from transformers import WhisperForConditionalGeneration, WhisperProcessor

print("正在下载 Whisper 模型...")
model = WhisperForConditionalGeneration.from_pretrained("{model_id}")
processor = WhisperProcessor.from_pretrained("{model_id}")
model.save_pretrained(r"{save_path}")
processor.save_pretrained(r"{save_path}")
print(f"模型已保存到: {save_path}")
'''

    with open("temp_download.py", "w", encoding="utf-8") as f:
        f.write(code)

    try:
        subprocess.check_call([python_exe, "temp_download.py"])
    except subprocess.CalledProcessError as e:
        print(f"下载失败: {e}")
        return False
    finally:
        if os.path.exists("temp_download.py"):
            os.remove("temp_download.py")

    return True

if __name__ == "__main__":
    base_path = r"D:\ProgramFile\old_people\models"

    models = [
        ("microsoft/voxcpm2", os.path.join(base_path, "voxcpm2"), "VoxCPM2"),
        ("openai/whisper-medium", os.path.join(base_path, "whisper-medium"), "Whisper"),
        ("alibaba-damo/audio_fun-audio-chat-8b", os.path.join(base_path, "fun-audio-chat-8b"), "Fun-Audio-Chat-8B"),
    ]

    success_count = 0
    fail_count = 0

    for model_id, save_path, name in models:
        if download_model(model_id, save_path, name):
            success_count += 1
        else:
            fail_count += 1

    print("\n" + "="*50)
    print("模型下载完成！")
    print(f"成功: {success_count}")
    print(f"失败: {fail_count}")
    print("="*50)
