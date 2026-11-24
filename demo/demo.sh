#!/usr/bin/env bash
# 端到端演示脚本：对本地运行的网页（默认 127.0.0.1:8000）走完整核查流程。
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "$0")" && pwd)"

post_anchors() {
  local file="$1"
  while IFS=, read -r ev utc; do
    [ "$ev" = "事件号" ] && continue
    curl -s -X POST "$BASE/api/anchors" -H 'Content-Type: application/json' \
      -d "{\"event_no\":$ev,\"utc_ms\":$utc}" >/dev/null
  done < "$file"
}

echo "== 上传事件 CSV =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" | python3 -m json.tool | head -8

echo; echo "== 正确锚点账：期望 consistent =="
post_anchors "$DIR/anchors_good.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

echo; echo "== 重建并登记单错账：期望 inconsistent + 候选首值/末值/步长 =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_bad_single.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

echo; echo "== 双错账：期望“单处更正无法对平” =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_bad_double.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

anchor_id_of() {  # 按事件号取锚点 ID
  curl -s "$BASE/api/state" | python3 -c "
import json, sys
for a in json.load(sys.stdin)['anchors']:
    if a['event_no'] == $1:
        print(a['id']); break"
}

echo; echo "== 录像时段取数：正确账，选锚点(事件1, 事件5)，窗口 [T0+500, T0+90000) =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_good.csv"
FROM_ID=$(anchor_id_of 1); TO_ID=$(anchor_id_of 5)
echo "-- 预检 /api/export/check --"
curl -s -X POST "$BASE/api/export/check" -H 'Content-Type: application/json' \
  -d "{\"from_anchor_id\":$FROM_ID,\"to_anchor_id\":$TO_ID,\"win_start\":1700000000500,\"win_end\":1700000090000}" \
  | python3 -m json.tool
echo "-- 下载 /api/export/events.csv（事件2/3 必在窗内，事件4 仅可能在窗内）--"
curl -s "$BASE/api/export/events.csv?from_anchor_id=$FROM_ID&to_anchor_id=$TO_ID&win_start=1700000000500&win_end=1700000090000"

echo; echo "== 录像时段取数：段内锚点冲突，期望 409 拒绝下载 =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_bad_single.csv"
FROM_ID=$(anchor_id_of 1); TO_ID=$(anchor_id_of 5)
curl -s -o /dev/null -w "check HTTP %{http_code}\n" -X POST "$BASE/api/export/check" \
  -H 'Content-Type: application/json' \
  -d "{\"from_anchor_id\":$FROM_ID,\"to_anchor_id\":$TO_ID,\"win_start\":0,\"win_end\":2000000000000}"
curl -s -X POST "$BASE/api/export/check" -H 'Content-Type: application/json' \
  -d "{\"from_anchor_id\":$FROM_ID,\"to_anchor_id\":$TO_ID,\"win_start\":0,\"win_end\":2000000000000}" \
  | python3 -m json.tool
