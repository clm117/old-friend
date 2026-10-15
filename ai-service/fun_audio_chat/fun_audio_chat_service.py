import torch
import numpy as np
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, __version__
import os

class FunAudioChatService:
    def __init__(self):
        print(f"Transformers version: {__version__}")
        # 使用本地模型路径
        self.model_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models", "fun-audio-chat-8b")
        print(f"Loading Fun-Audio-Chat-8B from: {self.model_path}")
        print(f"Model path exists: {os.path.exists(self.model_path)}")
        if os.path.exists(self.model_path):
            print(f"Model path contents: {os.listdir(self.model_path)[:10]}")
        
        try:
            # 加载Fun-Audio-Chat-8B模型
            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
                self.model_path,
                torch_dtype=torch.float16,
                low_cpu_mem_usage=True,
                use_safetensors=True
            )
            self.processor = AutoProcessor.from_pretrained(self.model_path)
            
            # 检查是否有GPU
            if torch.cuda.is_available():
                self.device = "cuda"
                self.model.to(self.device)
                print("Using CUDA device")
            else:
                self.device = "cpu"
                print("Using CPU device")
        except Exception as e:
            print(f"Error loading model: {e}")
            # 模拟模型加载成功，以便服务能够启动
            self.model = None
            self.processor = None
            self.device = "cpu"
            print("Using mock model for testing")
    
    def process_audio(self, audio_data, sample_rate=44100):
        if self.model is None or self.processor is None:
            # 模拟模型响应
            return "This is a mock response from Fun-Audio-Chat-8B. The model failed to load, but the service is running."
        
        # 处理音频数据
        inputs = self.processor(
            audio_data, 
            sampling_rate=sample_rate, 
            return_tensors="pt"
        ).to(self.device)
        
        # 生成响应
        with torch.no_grad():
            generated_ids = self.model.generate(
                inputs["input_features"],
                attention_mask=inputs.get("attention_mask"),
                max_new_tokens=1024,
                do_sample=True,
                temperature=0.7
            )
        
        response = self.processor.batch_decode(
            generated_ids, 
            skip_special_tokens=True
        )[0]
        return response
    
    def streaming_process(self, audio_chunk):
        # 流式处理音频
        # 这里简化实现，实际需要使用模型的流式生成功能
        return "Streaming response"
    
    def get_voice_response(self, text, voice_style="default"):
        # 生成语音响应
        # 这里简化实现，实际需要调用TTS模型
        return b"voice_audio_data"
