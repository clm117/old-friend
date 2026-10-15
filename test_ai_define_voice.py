import requests
import os

# 测试AI服务的语音定义端点
url = "http://localhost:8084/api/voice/define"
session_id = "test123"
description = "warm female voice"
audio_file = "D:/test_audio.wav"

# 创建测试音频文件（如果不存在）
if not os.path.exists(audio_file):
    with open(audio_file, 'wb') as f:
        f.write(b'\x00' * 100)

# 构建请求数据
files = {
    'audio': ('test_audio.wav', open(audio_file, 'rb'), 'audio/wav')
}
data = {
    'session_id': session_id,
    'description': description
}

# 发送请求
try:
    response = requests.post(url, files=files, data=data)
    print(f"Status code: {response.status_code}")
    print(f"Response: {response.json()}")
except Exception as e:
    print(f"Error: {e}")
