import faiss
import numpy as np

class FAISSMemory:
    def __init__(self):
        # 创建FAISS索引
        self.dimension = 128
        self.index = faiss.IndexFlatL2(self.dimension)
        self.character_embeddings = {}
    
    def add_character_embedding(self, character_id, embedding):
        # 添加性格向量
        self.character_embeddings[character_id] = embedding
        self.index.add(np.array([embedding], dtype=np.float32))
    
    def search_similar_characters(self, query_embedding, top_k=5):
        # 搜索相似性格
        distances, indices = self.index.search(np.array([query_embedding], dtype=np.float32), top_k)
        return distances, indices
    
    def get_character_embedding(self, character_id):
        # 获取性格向量
        return self.character_embeddings.get(character_id)
