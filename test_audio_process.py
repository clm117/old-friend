import requests
import base64

# 测试API Gateway的语音处理接口
def test_api_gateway():
    print("Testing API Gateway /api/voice/process...")
    try:
        # 创建一个简单的测试音频数据（1秒的静音）
        audio_data = bytes([0x00] * 44100 * 2)  # 简单的静音音频
        
        files = {
            'session_id': (None, 'test_session'),
            'audio': ('test.wav', audio_data, 'audio/wav')
        }
        
        response = requests.post('http://localhost:8005/api/voice/process', files=files)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.json()}")
        return response.json()
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}

# 测试AI服务的语音处理接口
def test_ai_service():
    print("\nTesting AI Service /api/ai/process...")
    try:
        # 创建一个简单的测试音频数据
        audio_data = bytes([0x00] * 44100 * 2)
        audio_hex = audio_data.hex()
        
        response = requests.post('http://localhost:8084/api/ai/process', json={
            'session_id': 'test_session',
            'audio_data': audio_hex
        })
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.json()}")
        return response.json()
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}

if __name__ == "__main__":
    test_api_gateway()
    test_ai_service()