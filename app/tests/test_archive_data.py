"""历史数据归档测试（SDD 验证）。

核心原则：**不伪造时长**。归档只挪走 + 标注原因，不改任何时间戳。

覆盖点：
1. 扫描只识别「零秒/负时长」记录，不误伤正常记录
2. 归档后 entries 里不再有缺陷记录，归档表里一条不少
3. 归档表保留全部字段（数据不丢）
4. 可逆：恢复后原样回到 entries
5. dry_run 不动数据
6. 统计口径：归档后覆盖率应能反映真实数据
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import archive_data
from store import RecordStore


class TestArchiveData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "arch.db"))
        # 造数据：3 条零秒（缺陷）+ 2 条正常
        self.zero_ids = []
        for i in range(3):
            eid = self.store.insert_entry({
                "started_at": 1000 + i, "ended_at": 1000 + i,  # 零秒
                "activity": f"活动{i}", "content": f"内容{i}",
                "category": "代码开发",
            })
            self.zero_ids.append(eid)
        self.good_ids = []
        for i in range(2):
            eid = self.store.insert_entry({
                "started_at": 2000 + i * 100, "ended_at": 2000 + i * 100 + 60,
                "activity": f"正常{i}", "content": f"正常内容{i}",
                "category": "资料研究",
            })
            self.good_ids.append(eid)

    def tearDown(self):
        self.store.close()

    def _all_entries(self):
        return self.store.entries_in_range(0, 10**9)

    def _archived(self):
        return archive_data.list_archived(self.store, limit=1000)["entries"]

    # ---------- 扫描 ----------
    def test_scan_identifies_only_zero_duration(self):
        scan = archive_data.scan_defective(self.store)
        self.assertEqual(scan["total_scanned"], 5)
        self.assertEqual(scan["defective"], 3)
        self.assertEqual(scan["healthy"], 2)
        self.assertAlmostEqual(scan["defective_pct"], 60.0, places=1)

    def test_scan_does_not_modify(self):
        before = len(self._all_entries())
        archive_data.scan_defective(self.store)
        self.assertEqual(len(self._all_entries()), before)

    def test_scan_with_before_ts_filter(self):
        # 只扫 2000 之前的 -> 3 条缺陷全在范围内，2 条正常在 2000+
        scan = archive_data.scan_defective(self.store, before_ts=2000)
        self.assertEqual(scan["total_scanned"], 3)
        self.assertEqual(scan["defective"], 3)

    # ---------- 归档 ----------
    def test_archive_moves_defective_only(self):
        result = archive_data.archive_defective(self.store)
        self.assertEqual(result["archived"], 3)
        remaining = self._all_entries()
        self.assertEqual(len(remaining), 2)
        for e in remaining:
            self.assertGreater(e["ended_at"] - e["started_at"], 0)

    def test_archived_records_preserved_fully(self):
        archive_data.archive_defective(self.store)
        archived = self._archived()
        self.assertEqual(len(archived), 3)
        for a in archived:
            self.assertIn(a["activity"], ("活动0", "活动1", "活动2"))
            self.assertTrue(a["content"])
            self.assertEqual(a["category"], "代码开发")
            self.assertEqual(a["archive_reason"], archive_data.REASON_ZERO_DURATION)
            # 时间戳未被篡改
            self.assertEqual(a["ended_at"] - a["started_at"], 0)

    def test_archive_dry_run_changes_nothing(self):
        result = archive_data.archive_defective(self.store, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["archived"], 0)
        self.assertEqual(len(self._all_entries()), 5)
        self.assertEqual(len(self._archived()), 0)

    def test_archive_preserves_healthy_records(self):
        archive_data.archive_defective(self.store)
        remaining = {e["id"] for e in self._all_entries()}
        self.assertEqual(remaining, set(self.good_ids))

    # ---------- 恢复 ----------
    def test_restore_is_reversible(self):
        archive_data.archive_defective(self.store)
        result = archive_data.restore_archived(self.store)
        self.assertEqual(result["restored"], 3)
        self.assertEqual(len(self._archived()), 0)
        self.assertEqual(len(self._all_entries()), 5)

    def test_restore_preserves_content(self):
        archive_data.archive_defective(self.store)
        archive_data.restore_archived(self.store)
        entries = {e["id"]: e for e in self._all_entries()}
        for eid in self.zero_ids:
            self.assertIn(eid, entries)
            self.assertEqual(entries[eid]["ended_at"] - entries[eid]["started_at"], 0)

    def test_restore_specific_ids(self):
        archive_data.archive_defective(self.store)
        result = archive_data.restore_archived(self.store, [self.zero_ids[0]])
        self.assertEqual(result["restored"], 1)
        self.assertEqual(len(self._archived()), 2)

    def test_restore_skips_id_conflicts(self):
        """id 冲突时应跳过而不是崩溃。"""
        archive_data.archive_defective(self.store)
        # 手工往 entries 塞一条同 id 的记录，制造冲突
        with self.store._lock:  # noqa: SLF001
            self.store._conn.execute(  # noqa: SLF001
                """INSERT INTO entries (id, started_at, ended_at, activity,
                   monitors, content, source, model, category)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (self.zero_ids[0], 1, 2, "冲突", "", "", "screenshot", "m", "其他"))
            self.store._conn.commit()  # noqa: SLF001
        result = archive_data.restore_archived(self.store)
        self.assertEqual(result["restored"], 2, "冲突的那条应被跳过")
        self.assertEqual(result["skipped"], 1)

    def test_restore_twice_is_safe(self):
        archive_data.archive_defective(self.store)
        archive_data.restore_archived(self.store)
        second = archive_data.restore_archived(self.store)
        self.assertEqual(second["restored"], 0)

    # ---------- 导出 ----------
    def test_export_creates_json(self):
        archive_data.archive_defective(self.store)
        out = str(Path(self.tmp) / "out" / "archived.json")
        result = archive_data.export_archived(self.store, out)
        self.assertEqual(result["count"], 3)
        self.assertTrue(Path(result["path"]).exists())

        import json
        with open(result["path"], encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["entries"]), 3)

    # ---------- 统计口径 ----------
    def test_coverage_after_archive_reflects_real_data(self):
        """归档后，覆盖率应只由健康记录决定。"""
        # 造出「跨度 10 分钟，其中 2 分钟有记录」的场景
        store2 = RecordStore(str(Path(self.tmp) / "cov.db"))
        try:
            base = 1_700_000_000
            for i in range(4):
                store2.insert_entry({
                    "started_at": base, "ended_at": base,
                    "activity": f"零{i}", "content": "", "category": "其他",
                })
            store2.insert_entry({
                "started_at": base, "ended_at": base + 120,
                "activity": "正常", "content": "", "category": "其他",
            })
            eff_before = store2.interval_effectiveness(days=1)
            archive_data.archive_defective(store2)
            eff_after = store2.interval_effectiveness(days=1)
            # 归档后零秒记录不再干扰分段统计
            self.assertLessEqual(eff_after["segments"], eff_before["segments"])
        finally:
            store2.close()


if __name__ == "__main__":
    unittest.main()
