import os
from transformers import WhisperForConditionalGeneration, WhisperProcessor

model_id = "openai/whisper-medium"
save_path = r"D:\ProgramFile\old_people\models\whisper-medium"

print(f"下载 Whisper 模型到 {save_path}...")
print(f"模型ID: {model_id}")

os.makedirs(save_path, exist_ok=True)

print("正在下载模型，这可能需要几分钟...")

model = WhisperForConditionalGeneration.from_pretrained(model_id)
processor = WhisperProcessor.from_pretrained(model_id)

print("正在保存模型...")
model.save_pretrained(save_path)
processor.save_pretrained(save_path)

print(f"✓ Whisper 模型已成功保存到: {save_path}")
