from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType

class MilvusMemory:
    def __init__(self):
        # 连接Milvus
        connections.connect("default", host="localhost", port="19530")
        
        # 定义集合 schema
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=128),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=1000)
        ]
        schema = CollectionSchema(fields, "Long term memory storage")
        
        # 创建集合
        self.collection = Collection("long_term_memory", schema)
    
    def store_long_term_memory(self, session_id, embedding, content):
        # 存储长期记忆
        entities = [
            [session_id],
            [embedding],
            [content]
        ]
        self.collection.insert(entities)
    
    def search_long_term_memory(self, session_id, query_embedding, top_k=5):
        # 搜索长期记忆
        search_params = {"metric_type": "L2", "params": {"nprobe": 10}}
        results = self.collection.search(
            [query_embedding],
            "embedding",
            search_params,
            limit=top_k,
            expr=f"session_id == '{session_id}'"
        )
        return results
