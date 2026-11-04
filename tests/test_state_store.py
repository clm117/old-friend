"""状态持久化单元测试（标准库 unittest，无需 GPU/模型依赖）。

运行：python -m unittest tests/test_state_store.py -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))

from state_store import PersistentStateStore  # noqa: E402


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yuban-state-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_persists_across_reload(self):
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        store.set_short_term_memory("s1", "character_id", "char-1")
        store.set_short_term_memory("s1", "emotion_config", {"name": "开心", "params": {"happiness": 0.8}})
        store.update_dialogue_history("s1", "你好".encode("utf-8"))
        store.update_dialogue_history("s1", b"hello")
        store.flush(force=True)

        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertEqual(reloaded.get_short_term_memory("s1", "character_id"), "char-1")
        self.assertEqual(reloaded.get_short_term_memory("s1", "emotion_config")["name"], "开心")
        history = reloaded.get_dialogue_history("s1")
        self.assertEqual(history[0], b"hello")
        self.assertEqual(history[1], "你好".encode("utf-8"))

    def test_dialogue_history_is_capped(self):
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        for i in range(250):
            store.update_dialogue_history("s2", f"m{i}")
        store.flush(force=True)
        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        history = reloaded.get_dialogue_history("s2")
        self.assertEqual(len(history), 200)
        self.assertEqual(history[0], b"m249")

    def test_stats_and_sessions(self):
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        store.set_short_term_memory("s3", "voice_description", "低沉男声")
        store.flush(force=True)
        stats = store.stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["backend"], "file")


if __name__ == "__main__":
    unittest.main()
