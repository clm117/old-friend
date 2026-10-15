import os
import subprocess
import sys

# 使用魔塔社区镜像源
os.environ['HF_ENDPOINT'] = 'https://modelscope.cn'
print(f"使用镜像源: {os.environ.get('HF_ENDPOINT')}")

def download_model(model_id, save_path, model_type):
    print(f"\n{'='*50}")
    print(f"下载 {model_type}...")
    print(f"模型ID: {model_id}")
    print(f"保存路径: {save_path}")
    print(f"{'='*50}")

    python_exe = r"D:\Program Files (x86)\Python 3.11\python.exe"
    print(f"使用Python: {python_exe}")

    # 创建一个简单的下载脚本
    script_content = f'''
import os
import traceback
print("开始执行下载脚本...")
try:
    os.makedirs(r"{save_path}", exist_ok=True)
    print("创建目录成功")
    
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch
    print("导入库成功")

    print("正在下载模型...")
    model = AutoModelForCausalLM.from_pretrained(
        "{model_id}",
        torch_dtype=torch.float32,
        low_cpu_mem_usage=True
    )
    print("模型加载成功")
    
    tokenizer = AutoTokenizer.from_pretrained("{model_id}")
    print("分词器加载成功")

    print("正在保存模型...")
    model.save_pretrained(r"{save_path}")
    tokenizer.save_pretrained(r"{save_path}")
    print("模型保存成功")
except Exception as e:
    print("错误:", str(e))
    print("详细错误:")
    print(traceback.format_exc())
'''

    # 为Fun-Audio-Chat-8B使用不同的模型类
    if model_type == "Fun-Audio-Chat-8B":
        script_content = f'''
import os
import traceback
print("开始执行下载脚本...")
try:
    os.makedirs(r"{save_path}", exist_ok=True)
    print("创建目录成功")
    
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
    import torch
    print("导入库成功")

    print("正在下载模型...")
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        "{model_id}",
        torch_dtype=torch.float32,
        low_cpu_mem_usage=True
    )
    print("模型加载成功")
    
    processor = AutoProcessor.from_pretrained("{model_id}")
    print("处理器加载成功")

    print("正在保存模型...")
    model.save_pretrained(r"{save_path}")
    processor.save_pretrained(r"{save_path}")
    print("模型保存成功")
except Exception as e:
    print("错误:", str(e))
    print("详细错误:")
    print(traceback.format_exc())
'''

    temp_script = "temp_download.py"
    with open(temp_script, "w", encoding="utf-8") as f:
        f.write(script_content)
    print("创建临时脚本成功")

    try:
        print("开始执行临时脚本...")
        result = subprocess.run([python_exe, temp_script], capture_output=True, text=True)
        print("临时脚本输出:")
        print(result.stdout)
        if result.stderr:
            print("临时脚本错误:")
            print(result.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"执行临时脚本时出错: {e}")
        return False
    finally:
        if os.path.exists(temp_script):
            os.remove(temp_script)
            print("临时脚本已删除")

if __name__ == "__main__":
    print("开始执行模型下载...")
    base_path = r"D:\ProgramFile\old_people\models"
    print(f"模型保存目录: {base_path}")

    # 使用魔塔社区的模型ID
    models = [
        ("damo/audio_fun-audio-chat-8b", os.path.join(base_path, "fun-audio-chat-8b"), "Fun-Audio-Chat-8B"),
        ("microsoft/voxcpm2", os.path.join(base_path, "voxcpm2"), "VoxCPM2"),
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
