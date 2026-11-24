"""录像时段取数（CSV 下载）测试：python -m unittest tests.test_export -v

覆盖：
* classify_window：含起点不含终点、必在/仅可能/窗外三种判定；
* 下载内容：事件号、计数、首值、末值、步长、窗内判定；
* 段内锚点冲突时两个端点都拒绝下载；
* 参数校验（缺锚点、同一条锚点、窗口非法、未建账）；
* 快照隔离：慢速下载期间删锚点/换账不影响本次下载，新下载按新数据计算；
* 分批输出：批大小为 1 时结果仍完整有序。
"""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.reconcile import MOD, classify_window  # noqa: E402

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")
T0 = 1_700_000_000_000


def read_anchor_csv(name):
    import csv
    with open(os.path.join(DEMO_DIR, name), newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))[1:]
    return [(int(r[0]), int(r[1])) for r in rows]


class ClassifyWindowTest(unittest.TestCase):
    def test_must_when_all_values_inside(self):
        self.assertEqual(classify_window(1000, 1000, MOD, 500, 2000), "must")
        self.assertEqual(classify_window(1000, 1000 + 2 * MOD, MOD, 0, 10**15), "must")

    def test_maybe_when_partially_inside(self):
        # {1000, 66536} 只有 1000 在 [500, 66000)
        self.assertEqual(classify_window(1000, 1000 + MOD, MOD, 500, 66000), "maybe")
        # 首值在窗外、末值在窗内
        self.assertEqual(classify_window(100, 100 + MOD, MOD, 500, 10**15), "maybe")

    def test_none_when_outside(self):
        self.assertIsNone(classify_window(1000, 1000, MOD, 2000, 3000))
        self.assertIsNone(classify_window(1000, 1000 + MOD, MOD, 0, 500))

    def test_start_inclusive_end_exclusive(self):
        # 起点含：值正好等于 win_start -> 在窗内
        self.assertEqual(classify_window(1000, 1000, MOD, 1000, 2000), "must")
        # 终点不含：值正好等于 win_end -> 不在窗内
        self.assertIsNone(classify_window(1000, 1000, MOD, 0, 1000))
        # 等差数列中某一项正好等于 win_end 也不算
        self.assertIsNone(classify_window(1000, 1000 + MOD, MOD, 2000, 1000 + MOD))


class ExportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(db_path=os.path.join(self.tmp.name, "test.db"))
        self.client = self.app.test_client()
        with open(os.path.join(DEMO_DIR, "events.csv"), "rb") as fh:
            self.csv_bytes = fh.read()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, data: bytes | None = None, filename: str = "events.csv"):
        return self.client.post(
            "/api/dataset",
            data={"file": (io.BytesIO(data if data is not None else self.csv_bytes), filename)},
            content_type="multipart/form-data",
        )

    def add_anchor(self, event_no, utc_ms):
        return self.client.post(
            "/api/anchors", json={"event_no": event_no, "utc_ms": utc_ms}
        )

    def anchor_ids(self):
        anchors = self.client.get("/api/state").get_json()["anchors"]
        return {a["event_no"]: a["id"] for a in anchors}

    def setup_good_ledger(self):
        self.upload()
        for event_no, utc in read_anchor_csv("anchors_good.csv"):
            self.assertEqual(self.add_anchor(event_no, utc).status_code, 200)
        return self.anchor_ids()

    def download(self, ids, from_ev, to_ev, win_start, win_end, buffered=True):
        return self.client.get(
            f"/api/export/events.csv?from_anchor_id={ids[from_ev]}"
            f"&to_anchor_id={ids[to_ev]}&win_start={win_start}&win_end={win_end}",
            buffered=buffered,
        )

    @staticmethod
    def parse_csv(resp):
        import csv
        text = resp.get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(text)))
        return rows[0], rows[1:]


class ExportContentTest(ExportTestBase):
    def test_download_rows_and_flags(self):
        ids = self.setup_good_ledger()
        resp = self.download(ids, 1, 5, T0 + 500, T0 + 90000)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        header, rows = self.parse_csv(resp)
        self.assertEqual(
            header,
            ["事件号", "计数", "可取时刻首值(UTC毫秒)", "可取时刻末值(UTC毫秒)", "步长(毫秒)", "窗内判定"],
        )
        # 事件 1（T0）与事件 5（T0+95464）落在窗外，不导出
        self.assertEqual(
            rows,
            [
                ["2", "13345", str(T0 + 1000), str(T0 + 1000), str(MOD), "必在窗内"],
                ["3", "17809", str(T0 + 5464), str(T0 + 5464), str(MOD), "必在窗内"],
                ["4", "37809", str(T0 + 25464), str(T0 + 91000), str(MOD), "仅可能在窗内"],
            ],
        )

    def test_window_inclusive_start_exclusive_end(self):
        ids = self.setup_good_ledger()
        # [T0, T0+1000)：事件1 在窗内（含起点），事件2 的时刻 T0+1000 正好等于终点 -> 不含
        _, rows = self.parse_csv(self.download(ids, 1, 5, T0, T0 + 1000))
        self.assertEqual([r[0] for r in rows], ["1"])
        # [T0+1000, T0+1001)：事件2 在窗内
        _, rows = self.parse_csv(self.download(ids, 1, 5, T0 + 1000, T0 + 1001))
        self.assertEqual([r[0] for r in rows], ["2"])

    def test_anchor_event_exported_with_single_value(self):
        ids = self.setup_good_ledger()
        _, rows = self.parse_csv(self.download(ids, 1, 5, T0, T0 + 1))
        self.assertEqual(rows, [["1", "12345", str(T0), str(T0), str(MOD), "必在窗内"]])

    def test_two_anchor_segment_gives_wider_ranges(self):
        # 只登记事件 1 与 5 两条锚点：中间事件可取时刻更宽
        self.upload()
        good = read_anchor_csv("anchors_good.csv")
        self.add_anchor(*good[0])
        self.add_anchor(*good[2])
        ids = self.anchor_ids()
        _, rows = self.parse_csv(self.download(ids, 1, 5, 0, 10**15))
        by_ev = {int(r[0]): r for r in rows}
        self.assertEqual(set(by_ev), {1, 2, 3, 4, 5})
        # 事件 2：首值 T0+1000，末值 T0+1000+65536（两个候选）
        self.assertEqual(by_ev[2][2], str(T0 + 1000))
        self.assertEqual(by_ev[2][3], str(T0 + 1000 + MOD))
        self.assertEqual(by_ev[2][5], "必在窗内")  # 全窗覆盖 -> 必在

    def test_maybe_flag_when_window_cuts_progression(self):
        ids = self.setup_good_ledger()
        # 事件 4 的可取时刻 {T0+25464, T0+91000}，窗口只含前者
        _, rows = self.parse_csv(self.download(ids, 1, 5, T0 + 20000, T0 + 91000))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "4")
        self.assertEqual(rows[0][5], "仅可能在窗内")

    def test_events_outside_segment_not_exported(self):
        ids = self.setup_good_ledger()
        # 选锚点 1 与 3：段区间是事件 1..3，事件 4/5 不导出
        _, rows = self.parse_csv(self.download(ids, 1, 3, 0, 10**15))
        self.assertEqual([int(r[0]) for r in rows], [1, 2, 3])

    def test_check_endpoint_returns_segment_and_download_url(self):
        ids = self.setup_good_ledger()
        resp = self.client.post("/api/export/check", json={
            "from_anchor_id": ids[1], "to_anchor_id": ids[5],
            "win_start": T0 + 500, "win_end": T0 + 90000,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual([a["event_no"] for a in body["segment"]], [1, 3, 5])
        self.assertTrue(all(p["ok"] for p in body["pairs"]))
        self.assertIn("/api/export/events.csv?", body["download"])

    def test_anchor_order_irrelevant(self):
        ids = self.setup_good_ledger()
        resp = self.download(ids, 5, 1, T0 + 500, T0 + 90000)  # 终点/起点写反
        self.assertEqual(resp.status_code, 200)
        _, rows = self.parse_csv(resp)
        self.assertEqual([int(r[0]) for r in rows], [2, 3, 4])


class ExportConflictTest(ExportTestBase):
    def setup_bad_ledger(self):
        self.upload()
        for event_no, utc in read_anchor_csv("anchors_bad_single.csv"):
            self.add_anchor(event_no, utc)
        return self.anchor_ids()

    def test_check_reports_conflict_and_refuses(self):
        ids = self.setup_bad_ledger()
        resp = self.client.post("/api/export/check", json={
            "from_anchor_id": ids[1], "to_anchor_id": ids[5],
            "win_start": 0, "win_end": 10**15,
        })
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("冲突", body["error"])
        self.assertTrue(any(not p["ok"] for p in body["pairs"]))

    def test_download_refused_on_conflict(self):
        ids = self.setup_bad_ledger()
        resp = self.download(ids, 1, 5, 0, 10**15)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("冲突", resp.get_json()["error"])

    def test_conflict_outside_segment_still_downloads(self):
        # 段外锚点冲突不影响所选段：选 3..5（一致），事件 1 的锚点抄错
        self.upload()
        self.add_anchor(1, T0 + 7 * MOD)  # 与 3 对不上
        self.add_anchor(3, T0 + 5464)
        self.add_anchor(5, T0 + 95464)
        ids = self.anchor_ids()
        resp = self.download(ids, 3, 5, 0, 10**15)
        self.assertEqual(resp.status_code, 200)
        _, rows = self.parse_csv(resp)
        self.assertEqual([int(r[0]) for r in rows], [3, 4, 5])


class ExportValidationTest(ExportTestBase):
    def test_requires_dataset(self):
        resp = self.client.get(
            "/api/export/events.csv?from_anchor_id=1&to_anchor_id=2&win_start=0&win_end=1"
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/export/check", json={
            "from_anchor_id": 1, "to_anchor_id": 2, "win_start": 0, "win_end": 1,
        })
        self.assertEqual(resp.status_code, 400)

    def test_same_anchor_rejected(self):
        ids = self.setup_good_ledger()
        resp = self.client.get(
            f"/api/export/events.csv?from_anchor_id={ids[1]}"
            f"&to_anchor_id={ids[1]}&win_start=0&win_end=10"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不同", resp.get_json()["error"])

    def test_missing_anchor_rejected(self):
        ids = self.setup_good_ledger()
        resp = self.client.get(
            f"/api/export/events.csv?from_anchor_id={ids[1]}"
            f"&to_anchor_id=9999&win_start=0&win_end=10"
        )
        self.assertEqual(resp.status_code, 404)

    def test_bad_window_rejected(self):
        ids = self.setup_good_ledger()
        base = f"/api/export/events.csv?from_anchor_id={ids[1]}&to_anchor_id={ids[5]}"
        self.assertEqual(self.client.get(f"{base}&win_start=100&win_end=100").status_code, 400)
        self.assertEqual(self.client.get(f"{base}&win_start=100&win_end=50").status_code, 400)
        self.assertEqual(self.client.get(f"{base}&win_start=-1&win_end=50").status_code, 400)
        self.assertEqual(self.client.get(f"{base}&win_start=abc&win_end=50").status_code, 400)


class ExportSnapshotTest(ExportTestBase):
    def test_stream_isolated_from_concurrent_writes(self):
        ids = self.setup_good_ledger()
        self.app.config["EXPORT_BATCH_SIZE"] = 1  # 强制多批输出
        resp = self.download(ids, 1, 5, 0, 10**15, buffered=False)
        self.assertEqual(resp.status_code, 200)
        it = iter(resp.response)
        first = next(it)  # 表头：下载已开始，快照已固定

        # 慢速下载期间：删除中间锚点（写操作不应被读事务阻塞）
        del_resp = self.client.delete(f"/api/anchors/{ids[3]}")
        self.assertEqual(del_resp.status_code, 200)

        # 本次下载仍按下载开始时的快照：事件 2 只有单值（段内曾有 3 条锚点）
        rest = b"".join(it).decode("utf-8")
        text = first.decode("utf-8") + rest
        import csv as csvmod
        rows = list(csvmod.reader(io.StringIO(text)))[1:]
        by_ev = {int(r[0]): r for r in rows}
        self.assertEqual(by_ev[2][2], str(T0 + 1000))
        self.assertEqual(by_ev[2][3], str(T0 + 1000))  # 单值：快照里锚点 3 仍在

        # 新下载按届时已提交的数据：锚点 3 已删，事件 2 变为两个候选
        ids_now = self.anchor_ids()
        _, rows2 = self.parse_csv(self.download(ids_now, 1, 5, 0, 10**15))
        by_ev2 = {int(r[0]): r for r in rows2}
        self.assertEqual(by_ev2[2][2], str(T0 + 1000))
        self.assertEqual(by_ev2[2][3], str(T0 + 1000 + MOD))

    def test_dataset_switch_during_stream(self):
        ids = self.setup_good_ledger()
        self.app.config["EXPORT_BATCH_SIZE"] = 1
        resp = self.download(ids, 1, 5, 0, 10**15, buffered=False)
        self.assertEqual(resp.status_code, 200)
        it = iter(resp.response)
        next(it)

        # 换账：上传只有 2 个事件的新 CSV（写库不被阻塞）
        new_csv = "事件号,计数\n1,100\n2,200\n".encode()
        sw = self.upload(new_csv, "new.csv")
        self.assertEqual(sw.status_code, 200)

        # 旧下载流仍完整产出旧账的 5 个事件
        rest = b"".join(it).decode("utf-8")
        import csv as csvmod
        rows = list(csvmod.reader(io.StringIO(rest)))
        self.assertEqual(len(rows), 5)  # 事件 1..5（表头已消费）

    def test_add_anchor_during_stream_then_new_download(self):
        ids = self.setup_good_ledger()
        self.app.config["EXPORT_BATCH_SIZE"] = 1
        resp = self.download(ids, 1, 5, 0, 10**15, buffered=False)
        it = iter(resp.response)
        next(it)
        # 补登记事件 2 的锚点（与账一致的时刻）
        add = self.add_anchor(2, T0 + 1000)
        self.assertEqual(add.status_code, 200)
        b"".join(it)  # 旧流不受影响，直接读完

        ids_now = self.anchor_ids()
        self.assertIn(2, ids_now)
        _, rows = self.parse_csv(self.download(ids_now, 1, 5, 0, 10**15))
        self.assertEqual([int(r[0]) for r in rows], [1, 2, 3, 4, 5])


class ExportBatchTest(ExportTestBase):
    def test_batch_size_one_keeps_rows_complete_and_ordered(self):
        ids = self.setup_good_ledger()
        self.app.config["EXPORT_BATCH_SIZE"] = 1
        _, rows = self.parse_csv(self.download(ids, 1, 5, 0, 10**15))
        self.assertEqual([int(r[0]) for r in rows], [1, 2, 3, 4, 5])

    def test_large_ledger_pagination(self):
        # 300 个事件、批大小 7：跨批不漏不重
        n = 300
        gaps = [(i * 137) % 90000 + 1 for i in range(1, n)]
        counts, utcs = [5000], [T0]
        for g in gaps:
            counts.append((counts[-1] + g) % MOD)
            utcs.append(utcs[-1] + g)
        lines = ["事件号,计数"] + [f"{i + 1},{c}" for i, c in enumerate(counts)]
        self.upload("\n".join(lines).encode(), "big.csv")
        self.add_anchor(1, utcs[0])
        self.add_anchor(150, utcs[149])
        self.add_anchor(300, utcs[299])
        ids = self.anchor_ids()
        self.app.config["EXPORT_BATCH_SIZE"] = 7
        _, rows = self.parse_csv(self.download(ids, 1, 300, 0, 10**18))
        self.assertEqual([int(r[0]) for r in rows], list(range(1, 301)))
        # 端点锚点时刻精确，抽查中段锚点事件的时刻
        by_ev = {int(r[0]): r for r in rows}
        self.assertEqual(by_ev[1][2], str(T0))
        self.assertEqual(by_ev[150][2], str(utcs[149]))
        self.assertEqual(by_ev[150][2], by_ev[150][3])  # 锚点事件单值
        self.assertEqual(by_ev[300][2], str(utcs[299]))
        # 窗口过滤：只留前 1000ms（导出事件的首值必在窗内，因为首值 >= T0 且数列递增）
        _, rows2 = self.parse_csv(self.download(ids, 1, 300, T0, T0 + 1000))
        self.assertTrue(rows2)
        self.assertTrue(all(T0 <= int(r[2]) < T0 + 1000 for r in rows2))
        self.assertLess(len(rows2), 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
