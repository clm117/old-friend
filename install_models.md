# 模型安装脚本

## 环境要求
- 操作系统：Windows 10/11, Ubuntu 20.04+, macOS
- 内存：至少32GB（推荐64GB以上）
- 存储：至少200GB可用空间
- Python：3.8+

## 第一步：创建虚拟环境并安装依赖

```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 升级pip
python -m pip install --upgrade pip
```

## 第二步：安装核心依赖

```bash
# 安装PyTorch（CPU版本）
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# 安装Transformers和数据集
pip install transformers datasets

# 安装FastAPI和uvicorn
pip install fastapi uvicorn

# 安装数据库客户端
pip install redis pymilvus faiss-cpu

# 安装其他依赖
pip install numpy scipy soundfile librosa
pip install python-multipart aiofiles
pip install python-jose[cryptography] passlib
```

## 第三步：创建模型目录

```bash
mkdir -p models
cd models
```

## 第四步：下载模型

### 4.1 Fun-Audio-Chat-8B

```python
# 保存为 download_fun_audio_chat.py 并运行
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
import os

model_id = "alibaba-damo/audio_fun-audio-chat-8b"
save_path = "./models/fun-audio-chat-8b"

print(f"下载 Fun-Audio-Chat-8B 到 {save_path}...")

model = AutoModelForSpeechSeq2Seq.from_pretrained(
    model_id,
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)

processor = AutoProcessor.from_pretrained(model_id)

os.makedirs(save_path, exist_ok=True)
model.save_pretrained(save_path)
processor.save_pretrained(save_path)

print("Fun-Audio-Chat-8B 下载完成！")
```

### 4.2 VoxCPM2

```python
# 保存为 download_voxcpm2.py 并运行
from transformers import AutoModelForCausalLM, AutoTokenizer
import os

model_id = "microsoft/voxcpm2"
save_path = "./models/voxcpm2"

print(f"下载 VoxCPM2 到 {save_path}...")

model = AutoModelForCausalLM.from_pretrained(
    model_id,
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)

tokenizer = AutoTokenizer.from_pretrained(model_id)

os.makedirs(save_path, exist_ok=True)
model.save_pretrained(save_path)
tokenizer.save_pretrained(save_path)

print("VoxCPM2 下载完成！")
```

### 4.3 Whisper（备选方案）

```python
# 保存为 download_whisper.py 并运行
from transformers import WhisperForConditionalGeneration, WhisperProcessor

model_id = "openai/whisper-medium"
save_path = "./models/whisper-medium"

print(f"下载 Whisper 到 {save_path}...")

model = WhisperForConditionalGeneration.from_pretrained(model_id)
processor = WhisperProcessor.from_pretrained(model_id)

os.makedirs(save_path, exist_ok=True)
model.save_pretrained(save_path)
processor.save_pretrained(save_path)

print("Whisper 下载完成！")
```

### 4.4 Llama 3 8B（备选方案）

```python
# 保存为 download_llama3.py 并运行
# 注意：需要先申请Meta的访问权限
from transformers import AutoModelForCausalLM, AutoTokenizer

model_id = "meta-llama/Meta-Llama-3-8B"
save_path = "./models/llama-3-8b"

print(f"下载 Llama 3 8B 到 {save_path}...")

# 需要先登录Hugging Face并获取访问令牌
from huggingface_hub import login
login(token="your_huggingface_token")

model = AutoModelForCausalLM.from_pretrained(
    model_id,
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)

tokenizer = AutoTokenizer.from_pretrained(model_id)

os.makedirs(save_path, exist_ok=True)
model.save_pretrained(save_path)
tokenizer.save_pretrained(save_path)

print("Llama 3 8B 下载完成！")
```

## 第五步：验证安装

```python
# 保存为 verify_install.py 并运行
import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, AutoModelForCausalLM, AutoTokenizer

print("=" * 50)
print("验证模型安装")
print("=" * 50)

# 检查PyTorch
print(f"\nPyTorch版本: {torch.__version__}")
print(f"CUDA可用: {torch.cuda.is_available()}")

# 验证Fun-Audio-Chat-8B
print("\n验证 Fun-Audio-Chat-8B...")
try:
    model_path = "./models/fun-audio-chat-8b"
    model = AutoModelForSpeechSeq2Seq.from_pretrained(model_path, torch_dtype=torch.float32)
    processor = AutoProcessor.from_pretrained(model_path)
    print("✓ Fun-Audio-Chat-8B 加载成功！")
except Exception as e:
    print(f"✗ Fun-Audio-Chat-8B 加载失败: {e}")

# 验证VoxCPM2
print("\n验证 VoxCPM2...")
try:
    model_path = "./models/voxcpm2"
    model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.float32)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    print("✓ VoxCPM2 加载成功！")
except Exception as e:
    print(f"✗ VoxCPM2 加载失败: {e}")

print("\n" + "=" * 50)
print("验证完成")
print("=" * 50)
```

## 注意事项

1. **下载时间**：模型文件较大，建议使用稳定的网络连接
   - Fun-Audio-Chat-8B：约16-32GB
   - VoxCPM2：约10-30GB
   - Whisper：约3GB
   - Llama 3 8B：约16GB

2. **内存管理**：加载大模型时会占用大量内存，确保系统有足够的可用内存

3. **磁盘空间**：确保有足够的磁盘空间存储所有模型（建议200GB以上）

4. **Hugging Face访问**：
   - 部分模型需要登录Hugging Face并获取访问令牌
   - 请访问 https://huggingface.co/settings/tokens 创建令牌

5. **Llama 3访问权限**：
   - 需要向Meta申请访问权限
   - 请访问 https://ai.meta.com/resources/models-and-libraries/ 查看详情

## 快速下载脚本

创建一个完整的下载脚本 `download_all_models.py`：

```python
import os
import subprocess
import sys

def install_package(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-q"])

def download_model(model_id, save_path, model_type):
    print(f"\n{'='*50}")
    print(f"下载 {model_type}...")
    print(f"{'='*50}")

    code = f'''
from transformers import {"AutoModelForSpeechSeq2Seq" if "fun-audio" in model_id else "AutoModelForCausalLM" if "cpm" in model_id else "AutoModelForCausalLM"}, AutoProcessor if "fun-audio" in model_id else "AutoTokenizer"
import os

model_id = "{model_id}"
save_path = "{save_path}"

print(f"下载模型: {{model_id}}")

model = {"AutoModelForSpeechSeq2Seq" if "fun-audio" in model_id else "AutoModelForCausalLM"}.from_pretrained(
    model_id,
    torch_dtype=torch.float32,
    low_cpu_mem_usage=True
)

{"processor = AutoProcessor.from_pretrained(model_id)" if "fun-audio" in model_id else "tokenizer = AutoTokenizer.from_pretrained(model_id)"}

os.makedirs(save_path, exist_ok=True)
model.save_pretrained(save_path)
{"processor.save_pretrained(save_path)" if "fun-audio" in model_id else "tokenizer.save_pretrained(save_path)"}

print(f"模型已保存到: {{save_path}}")
'''

    os.makedirs(save_path, exist_ok=True)
    with open("temp_download.py", "w") as f:
        f.write(code)

    subprocess.check_call([sys.executable, "temp_download.py"])
    os.remove("temp_download.py")

if __name__ == "__main__":
    models = [
        ("alibaba-damo/audio_fun-audio-chat-8b", "./models/fun-audio-chat-8b", "Fun-Audio-Chat-8B"),
        ("microsoft/voxcpm2", "./models/voxcpm2", "VoxCPM2"),
        ("openai/whisper-medium", "./models/whisper-medium", "Whisper"),
    ]

    for model_id, save_path, name in models:
        try:
            download_model(model_id, save_path, name)
        except Exception as e:
            print(f"下载 {name} 失败: {e}")

    print("\n" + "="*50)
    print("所有模型下载完成！")
    print("="*50)
```
