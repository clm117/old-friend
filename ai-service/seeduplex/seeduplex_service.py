import torch

class SeeduplexService:
    def __init__(self):
        # 加载Seeduplex模型
        self.model = torch.load("seeduplex_model.pt", weights_only=True)
        self.model.eval()
    
    def process_audio(self, audio_data):
        # 处理音频数据
        # 这里是模拟实现，实际应该调用真实的Seeduplex模型
        return "This is a response from Seeduplex"
    
    def streaming_process(self, audio_chunk):
        # 流式处理音频
        return "Streaming response"
