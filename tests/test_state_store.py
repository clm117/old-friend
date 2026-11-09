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

    def test_corrupted_state_file_recovers(self):
        state = self.tmp / "state.json"
        state.write_text('{"sessions": {', encoding="utf-8")
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertEqual(store.stats()["sessions"], 0)
        self.assertTrue(state.exists(), "损坏原文件在首次落盘前应保留")
        sid = store.create_session("u1", "恢复后会话")
        store.flush(force=True)
        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        items, total = reloaded.list_sessions("u1")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["session_id"], sid)

    def test_truncated_state_and_stale_tmp_ignored(self):
        state = self.tmp / "state.json"
        state.write_text('{"version": 1, "sessions": {"s_x": {"short_term":', encoding="utf-8")
        stale = self.tmp / "state.json.tmp"
        stale.write_text('{"sessions": {"zombie": {"short_term": {"owner": "u1"}}}}',
                         encoding="utf-8")
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertEqual(store.stats()["sessions"], 0, "残留 .tmp 不应被加载")
        sid = store.create_session("u1")
        store.flush(force=True)
        self.assertFalse(stale.exists(), "flush 后 .tmp 应被原子替换清理")
        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertEqual(reloaded.stats()["sessions"], 1)
        self.assertFalse(reloaded.has_session("zombie"))
        self.assertTrue(reloaded.has_session(sid))

    def test_concurrent_writes_and_reload(self):
        import threading
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        sid = store.create_session("u1")
        errors = []

        def worker(i):
            try:
                store.set_short_term_memory(sid, f"k{i}", i)
                store.update_dialogue_history(sid, f"m{i}")
            except Exception as e:  # pragma: no cover - 失败时收集
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        store.flush(force=True)
        self.assertEqual(errors, [])
        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        for i in (0, 25, 49):
            self.assertEqual(reloaded.get_short_term_memory(sid, f"k{i}"), i)
        self.assertEqual(len(reloaded.get_dialogue_history(sid)), 50)

    def test_prune_caps_and_index_consistent(self):
        from state_store import _MAX_SESSIONS, _MAX_TOMBSTONES
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        now = 1700000000.0
        with store._lock:
            for i in range(_MAX_TOMBSTONES + 1):
                store._deleted[f"t{i}"] = now + i
            store._prune_tombstones()
            self.assertEqual(len(store._deleted), _MAX_TOMBSTONES)
            self.assertNotIn("t0", store._deleted)
            self.assertIn(f"t{_MAX_TOMBSTONES}", store._deleted)
            for i in range(_MAX_SESSIONS + 1):
                store._sessions[f"p{i}"] = {
                    "short_term": {"owner": "u1"},
                    "dialogue_history": [],
                    "created_at": now,
                    "updated_at": now + i,
                }
            store._rebuild_index()
            store._prune_sessions()
        self.assertEqual(len(store._sessions), _MAX_SESSIONS)
        self.assertNotIn("p0", store._sessions)
        self.assertNotIn("p0", store._owner_index.get("u1", set()))
        self.assertEqual(len(store._owner_index.get("u1", set())), _MAX_SESSIONS)

    def test_claim_legacy_and_index_rebuild(self):
        store = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        store.set_short_term_memory("legacy_1", "character_id", "c1")
        store.flush(force=True)
        reloaded = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertIsNone(reloaded.get_session_owner("legacy_1"))
        self.assertEqual(reloaded.claim_session("legacy_1", "u1"), "u1")
        self.assertEqual(reloaded.claim_session("legacy_1", "u2"), "u1", "已归属会话不应被抢占")
        self.assertEqual(reloaded.list_sessions("u1")[1], 1)
        self.assertEqual(reloaded.list_sessions("u2")[1], 0)
        reloaded.flush(force=True)
        again = PersistentStateStore(data_dir=str(self.tmp), redis_url="")
        self.assertEqual(again.get_session_owner("legacy_1"), "u1")
        self.assertEqual(again.list_sessions("u1")[1], 1, "重启后归属索引应可重建")
        self.assertEqual(again.list_sessions("u2")[1], 0)


if __name__ == "__main__":
    unittest.main()
