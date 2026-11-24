"""Flask 核查页应用工厂与路由。"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import time

from flask import Flask, Response, jsonify, render_template, request

from . import db as dbmod
from . import export as exportmod
from .csv_loader import CsvError, parse_events_csv
from .export import ExportError
from .reconcile import MIN_ANCHORS, verify


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "inspection.db")
    )
    # 取数下载：每批处理/输出的事件条数；批次间人为延时（毫秒，演示慢速下载用）
    app.config["EXPORT_BATCH_SIZE"] = int(os.environ.get("EXPORT_BATCH_SIZE", "500"))
    app.config["EXPORT_BATCH_DELAY_MS"] = int(
        os.environ.get("EXPORT_BATCH_DELAY_MS", "0")
    )

    def get_db() -> sqlite3.Connection:
        conn = dbmod.connect(app.config["DB_PATH"])
        return conn

    def ok(**payload):
        return jsonify({"ok": True, **payload})

    def fail(message: str, status: int = 400, **extra):
        return jsonify({"ok": False, "error": message, **extra}), status

    # ---------- 页面 ----------
    @app.get("/")
    def index():
        return render_template("index.html", min_anchors=MIN_ANCHORS)

    # ---------- 状态 ----------
    @app.get("/api/state")
    def state():
        conn = get_db()
        try:
            meta = dbmod.get_dataset_meta(conn)
            return ok(
                dataset=meta,
                anchors=dbmod.list_anchors(conn),
                min_anchors=MIN_ANCHORS,
            )
        finally:
            conn.close()

    @app.get("/api/events")
    def events():
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("尚未上传事件 CSV", 404)
            limit = request.args.get("limit", type=int)
            return ok(events=dbmod.list_events(conn, limit=limit))
        finally:
            conn.close()

    # ---------- 上传事件 CSV（重建账） ----------
    @app.post("/api/dataset")
    def upload_dataset():
        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            return fail("请选择 CSV 文件")
        try:
            text = upload.read().decode("utf-8-sig")
            parsed = parse_events_csv(text)
        except UnicodeDecodeError:
            return fail("CSV 需为 UTF-8 编码")
        except CsvError as exc:
            return fail(f"CSV 校验失败：{exc}")

        conn = get_db()
        try:
            meta = dbmod.replace_dataset(
                conn, upload.filename, [ev["count"] for ev in parsed]
            )
            return ok(
                dataset=meta,
                anchors=dbmod.list_anchors(conn),
                message="事件账已重建，旧锚点已清空",
            )
        finally:
            conn.close()

    # ---------- 锚点登记（核查期间可随时补登记） ----------
    @app.post("/api/anchors")
    def create_anchor():
        payload = request.get_json(silent=True) or {}
        try:
            event_no = int(payload["event_no"])
            utc_ms = int(payload["utc_ms"])
        except (KeyError, TypeError, ValueError):
            return fail("event_no 与 utc_ms 必须是整数")
        if utc_ms < 0:
            return fail("UTC 毫秒不能为负")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            if not dbmod.event_exists(conn, event_no):
                return fail(f"事件号 {event_no} 不在事件账内")
            try:
                with conn:
                    anchor = dbmod.add_anchor(conn, event_no, utc_ms)
            except sqlite3.IntegrityError:
                return fail(f"事件号 {event_no} 已登记锚点，原账未改动", 409)
            return ok(anchor=anchor, anchors=dbmod.list_anchors(conn))
        finally:
            conn.close()

    @app.delete("/api/anchors/<int:anchor_id>")
    def remove_anchor(anchor_id: int):
        conn = get_db()
        try:
            removed = dbmod.delete_anchor(conn, anchor_id)
            conn.commit()
            if not removed:
                return fail("锚点不存在", 404)
            return ok(anchors=dbmod.list_anchors(conn))
        finally:
            conn.close()

    # ---------- 核查 ----------
    @app.post("/api/verify")
    def run_verify():
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            records = dbmod.load_records(conn)
            anchors = dbmod.list_anchors(conn)
            result = verify(records, anchors)
            return ok(result=result, anchors=anchors)
        finally:
            conn.close()

    # ---------- 确认更正：按当时全部锚点复核，冲突/失败保留原账 ----------
    @app.post("/api/correct")
    def confirm_correction():
        payload = request.get_json(silent=True) or {}
        try:
            anchor_id = int(payload["anchor_id"])
            new_utc = int(payload["new_utc_ms"])
        except (KeyError, TypeError, ValueError):
            return fail("anchor_id 与 new_utc_ms 必须是整数")
        if new_utc < 0:
            return fail("UTC 毫秒不能为负")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            anchor = conn.execute(
                "SELECT id, event_no, utc_ms FROM anchor WHERE id = ?", (anchor_id,)
            ).fetchone()
            if anchor is None:
                return fail("锚点不存在或已被删除", 404)

            records = dbmod.load_records(conn)
            try:
                # 单事务：改写 -> 按当时全部锚点复核 -> 不一致/异常则回滚
                with conn:
                    dbmod.update_anchor_utc(conn, anchor_id, new_utc)
                    anchors_now = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id, event_no, utc_ms FROM anchor ORDER BY event_no"
                        )
                    ]
                    result = verify(records, anchors_now)
                    if result["status"] != "consistent":
                        raise RuntimeError("复核未通过：新时刻无法让全账一致")
            except sqlite3.Error as exc:
                return fail(f"保存失败，已保留原账：{exc}", 500)
            except RuntimeError as exc:
                return fail(
                    f"{exc}，更正已撤销",
                    409,
                    result=result,
                    original_utc_ms=anchor["utc_ms"],
                )
            return ok(
                result=result,
                anchors=dbmod.list_anchors(conn),
                corrected={
                    "anchor_id": anchor_id,
                    "event_no": anchor["event_no"],
                    "old_utc_ms": anchor["utc_ms"],
                    "new_utc_ms": new_utc,
                },
                message="更正已保存，全账复核一致",
            )
        finally:
            conn.close()

    # ---------- 录像时段取数：预检（冲突即拒绝下载） ----------
    @app.post("/api/export/check")
    def export_check():
        payload = request.get_json(silent=True) or {}
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            try:
                from_id, to_id, win_start, win_end = exportmod.parse_params(payload)
                anchors = dbmod.list_anchors(conn)
                segment = exportmod.resolve_segment(anchors, from_id, to_id)
                seg_records = dbmod.get_prefixes(
                    conn, [a["event_no"] for a in segment]
                )
                pairs = exportmod.check_segment(seg_records, segment)
                if not all(p["ok"] for p in pairs):
                    raise ExportError("段内锚点冲突，已拒绝下载", 409, pairs)
            except ExportError as exc:
                return fail(str(exc), exc.status, pairs=exc.pairs)
            query = (
                f"from_anchor_id={from_id}&to_anchor_id={to_id}"
                f"&win_start={win_start}&win_end={win_end}"
            )
            return ok(
                segment=segment,
                pairs=pairs,
                window={"start": win_start, "end": win_end},
                download=f"/api/export/events.csv?{query}",
                message=f"段内 {len(segment)} 条锚点全部对得上，开始下载",
            )
        finally:
            conn.close()

    # ---------- 录像时段取数：CSV 流式下载 ----------
    @app.get("/api/export/events.csv")
    def export_events():
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                raise ExportError("请先上传事件 CSV")
            from_id, to_id, win_start, win_end = exportmod.parse_params(request.args)
            # 快照读事务：锚点与事件都取自下载开始时的同一个已提交状态；
            # WAL 下本事务不阻塞补登/删除锚点与换账。
            conn.execute("BEGIN")
            anchors = dbmod.list_anchors(conn)
            segment = exportmod.resolve_segment(anchors, from_id, to_id)
            seg_records = dbmod.get_prefixes(conn, [a["event_no"] for a in segment])
            pairs = exportmod.check_segment(seg_records, segment)
            if not all(p["ok"] for p in pairs):
                raise ExportError("段内锚点冲突，已拒绝下载", 409, pairs)
        except ExportError as exc:
            conn.rollback()
            conn.close()
            return fail(str(exc), exc.status, pairs=exc.pairs)

        lo_ev = segment[0]["event_no"]
        hi_ev = segment[-1]["event_no"]
        batch_size = max(1, app.config["EXPORT_BATCH_SIZE"])
        delay = max(0, app.config["EXPORT_BATCH_DELAY_MS"]) / 1000.0
        filename = f"events_e{lo_ev}_e{hi_ev}_{win_start}_{win_end}.csv"

        def generate():
            """分批推算、分批写出：内存只与批大小有关，不随选段事件条数增长。"""
            try:
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(exportmod.CSV_HEADER)
                yield buf.getvalue()
                pending = 0
                rows = exportmod.iter_export_rows(
                    dbmod.iter_events_between(conn, lo_ev, hi_ev, batch_size),
                    segment,
                    seg_records,
                    win_start,
                    win_end,
                )
                for ev, count, first, last, step, flag in rows:
                    if pending == 0:
                        buf.seek(0)
                        buf.truncate(0)
                        writer = csv.writer(buf)
                    writer.writerow(
                        [ev, count, first, last, step, exportmod.FLAG_LABELS[flag]]
                    )
                    pending += 1
                    if pending >= batch_size:
                        if delay:
                            time.sleep(delay)
                        yield buf.getvalue()
                        pending = 0
                if pending:
                    yield buf.getvalue()
            finally:
                conn.rollback()
                conn.close()

        return Response(
            generate(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return app
