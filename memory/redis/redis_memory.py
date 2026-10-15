import redis

class RedisMemory:
    def __init__(self):
        self.client = redis.Redis(host="localhost", port=6379, db=0)
    
    def set_short_term_memory(self, session_id, key, value, expire=3600):
        # 设置短期记忆，默认1小时过期
        self.client.setex(f"session:{session_id}:{key}", expire, value)
    
    def get_short_term_memory(self, session_id, key):
        # 获取短期记忆
        return self.client.get(f"session:{session_id}:{key}")
    
    def update_dialogue_history(self, session_id, dialogue):
        # 更新对话历史
        self.client.lpush(f"session:{session_id}:dialogue", dialogue)
        # 只保留最近100条对话
        self.client.ltrim(f"session:{session_id}:dialogue", 0, 99)
    
    def get_dialogue_history(self, session_id, limit=10):
        # 获取对话历史
        return self.client.lrange(f"session:{session_id}:dialogue", 0, limit-1)
