"""录像时段取数：按所选锚点段推算事件可取时刻，分批产出 CSV 行。

流程：
* 用所选两条锚点圈定事件号区间，区间内全部锚点（含所选两条）参与推算；
* 段内相邻锚点逐对沿用 pair_ok 核对，冲突即拒绝下载；
* 区间内每个事件的可取时刻由最近左右锚点约束求交（candidate_range），
  一致锚点的前缀量可累加，最近锚点的约束即全部锚点约束的交集；
* 只导出至少有一个可取时刻落在窗口 [win_start, win_end) 内的事件。

本模块不碰数据库与 HTTP，输入均为普通迭代器/字典，便于单测与流式复用。
"""

from __future__ import annotations

from .reconcile import MOD, candidate_range, classify_window, pair_ok

CSV_HEADER = ["事件号", "计数", "可取时刻首值(UTC毫秒)", "可取时刻末值(UTC毫秒)", "步长(毫秒)", "窗内判定"]
FLAG_LABELS = {"must": "必在窗内", "maybe": "仅可能在窗内"}


class ExportError(ValueError):
    """取数请求被拒绝（参数非法、锚点缺失、段内冲突等）。"""

    def __init__(self, message: str, status: int = 400, pairs: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.pairs = pairs or []


def parse_params(payload: dict) -> tuple[int, int, int, int]:
    """解析并校验 (起点锚点ID, 终点锚点ID, 窗口起点, 窗口终点)。"""
    try:
        from_id = int(payload["from_anchor_id"])
        to_id = int(payload["to_anchor_id"])
        win_start = int(payload["win_start"])
        win_end = int(payload["win_end"])
    except (KeyError, TypeError, ValueError):
        raise ExportError(
            "from_anchor_id、to_anchor_id、win_start、win_end 必须是整数"
        )
    if from_id == to_id:
        raise ExportError("请选择两条不同的锚点")
    if win_start < 0:
        raise ExportError("窗口起点不能为负")
    if win_end <= win_start:
        raise ExportError("窗口终点必须大于起点（窗口含起点、不含终点）")
    return from_id, to_id, win_start, win_end


def resolve_segment(all_anchors: list[dict], from_id: int, to_id: int) -> list[dict]:
    """按所选两条锚点圈定事件号区间，返回区间内全部锚点（按事件号升序，>=2 条）。"""
    by_id = {a["id"]: a for a in all_anchors}
    if from_id not in by_id or to_id not in by_id:
        raise ExportError("所选锚点不存在或已被删除", 404)
    lo_ev = min(by_id[from_id]["event_no"], by_id[to_id]["event_no"])
    hi_ev = max(by_id[from_id]["event_no"], by_id[to_id]["event_no"])
    segment = [a for a in all_anchors if lo_ev <= a["event_no"] <= hi_ev]
    segment.sort(key=lambda a: a["event_no"])
    return segment


def check_segment(
    seg_records: dict[int, tuple[int, int]],
    segment: list[dict],
) -> list[dict]:
    """段内相邻锚点逐对核对，返回与 verify 同格式的 pairs（含 ok 标记）。"""
    pairs: list[dict] = []
    for left, right in zip(segment, segment[1:]):
        ok, low, high = pair_ok(
            seg_records,
            left["event_no"], left["utc_ms"],
            right["event_no"], right["utc_ms"],
        )
        pairs.append(
            {
                "left_event": left["event_no"],
                "right_event": right["event_no"],
                "delta": right["utc_ms"] - left["utc_ms"],
                "low": low,
                "high": high,
                "step": MOD,
                "ok": ok,
            }
        )
    return pairs


def iter_export_rows(
    events,
    segment: list[dict],
    seg_records: dict[int, tuple[int, int]],
    win_start: int,
    win_end: int,
):
    """对按事件号升序给出的事件流，产出落在窗内的导出行。

    events: 迭代器，元素含 {"event_no", "count", "prefix_min", "prefix_amb"}，
    事件号必须全部位于段区间 [segment[0], segment[-1]] 内。
    产出 (event_no, count, first, last, step, flag)，flag 为 "must"/"maybe"。
    锚点指针随事件流单调前移，每条事件 O(1) 均摊，不缓存事件。
    """
    idx = 0  # segment 中第一个 event_no >= 当前事件的下标
    for row in events:
        ev = row["event_no"]
        while idx < len(segment) and segment[idx]["event_no"] < ev:
            idx += 1
        right = segment[idx] if idx < len(segment) else None
        if right is not None and right["event_no"] == ev:
            # 事件本身即锚点：时刻取纸本登记值（单值数列）
            first = last = right["utc_ms"]
            step = MOD
        else:
            left = segment[idx - 1] if idx > 0 else None
            if left is None or right is None:
                continue  # 段外事件（调用方已限定区间，防御性跳过）
            records = {
                left["event_no"]: seg_records[left["event_no"]],
                right["event_no"]: seg_records[right["event_no"]],
                ev: (row["prefix_min"], row["prefix_amb"]),
            }
            rng = candidate_range(
                records,
                ev,
                (
                    (left["event_no"], left["utc_ms"]),
                    (right["event_no"], right["utc_ms"]),
                ),
            )
            if rng is None:
                continue  # 一致段内不会出现，防御性跳过
            first, last, step = rng["first"], rng["last"], rng["step"]
        flag = classify_window(first, last, step, win_start, win_end)
        if flag is None:
            continue
        yield (ev, row["count"], first, last, step, flag)
