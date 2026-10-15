import os
import subprocess

# 下载目录
download_dir = r"D:\ProgramFile\old people\models"
print(f"下载目录: {download_dir}")

# 确保目录存在
os.makedirs(download_dir, exist_ok=True)

# 下载命令
commands = [
    f"modelscope download --model OpenBMB/VoxCPM2 --local_dir {download_dir}\voxcpm2",
    f"modelscope download --model FunAudioLLM/Fun-Audio-Chat-8B --local_dir {download_dir}\fun-audio-chat-8b",
    f"modelscope download --model FunAudioLLM/Fun-Audio-Chat-8B README.md --local_dir {download_dir}\fun-audio-chat-8b",
    f"modelscope download --model OpenBMB/VoxCPM2 README.md --local_dir {download_dir}\voxcpm2"
]

# 执行下载命令
for cmd in commands:
    print(f"\n执行命令: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f"返回码: {result.returncode}")
    if result.stdout:
        print(f"输出: {result.stdout}")
    if result.stderr:
        print(f"错误: {result.stderr}")

print("\n下载完成！")
