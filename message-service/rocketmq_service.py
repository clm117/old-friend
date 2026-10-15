# 消息服务已简化，直接通过API网关与核心AI服务通信
# 保留此文件用于未来扩展

class MessageService:
    def __init__(self):
        pass
    
    def send_message(self, session_id, message):
        # 直接通过HTTP请求发送消息到核心AI服务
        import requests
        try:
            response = requests.post(
                "http://localhost:8000/api/ai/process",
                json={"session_id": session_id, "message": message}
            )
            return response.json()
        except Exception as e:
            print(f"Message sending failed: {e}")
            return {"error": str(e)}
    
    def get_response(self, session_id):
        # 获取AI服务的响应
        import requests
        try:
            response = requests.get(
                f"http://localhost:8000/api/ai/response/{session_id}"
            )
            return response.json()
        except Exception as e:
            print(f"Getting response failed: {e}")
            return {"error": str(e)}

