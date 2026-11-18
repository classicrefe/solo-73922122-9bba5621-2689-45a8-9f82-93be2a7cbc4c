#!/usr/bin/env bash
# 演示 5：变更轨迹（审计）。
#   1) API 补录、独立 SQL 更正/删除都留下变更前后完整行；
#   2) 按 reports.id + 变更编号查看当时生效的整行，删除后返回不存在；
#   3) 回滚不留记录；唯一索引未生效时同报告号多行按各自 id 区分。
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

echo "== 0. 清场（删除走 DELETE 留轨迹；演示环境再清空轨迹） =="
psql_exec -c "DROP INDEX IF EXISTS uq_reports_lab_no_report_no;" \
          -c "DELETE FROM reports;" \
          -c "TRUNCATE report_changes RESTART IDENTITY;" \
          -c "ALTER TABLE reports ALTER COLUMN id RESTART;" \
          -c "DELETE FROM index_build_rounds;" >/dev/null

echo "== 1. API 补录一条报告 =="
RID="$(c -X POST "$API_BASE/reports" \
        -d '{"lab_no":"LAB-A","report_no":"R-001","measured":100}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
echo "新报告 id = $RID"

echo "== 2. 独立 SQL 更正测值（模拟数仓/DBA 直接改库） =="
psql_exec -c "UPDATE reports SET measured=105 WHERE id=$RID;"

echo "== 3. 查询变更轨迹：补录(INSERT)与更正(UPDATE)的前后完整行 =="
c "$API_BASE/reports/$RID/changes" | python3 -m json.tool

echo "== 4. 按变更编号查看当时生效的整行 =="
echo "-- 变更 1（补录后）:"
c "$API_BASE/reports/$RID/changes/1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["row"])'
echo "-- 变更 2（更正后）:"
c "$API_BASE/reports/$RID/changes/2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["row"])'

echo "== 5. 回滚不留记录：事务内改后 ROLLBACK =="
psql_exec >/dev/null <<SQL
BEGIN;
UPDATE reports SET measured=999 WHERE id=$RID;
ROLLBACK;
SQL
N="$(c "$API_BASE/reports/$RID/changes" | python3 -c 'import sys,json;print(json.load(sys.stdin)["total"])')"
echo "回滚后轨迹条数 = $N （仍为 2，回滚的改动与轨迹一起消失）"
[[ "$N" == "2" ]] || { echo "ROLLBACK LEFT TRACE!"; exit 1; }

echo "== 6. 独立 SQL 删除：删除后按编号查返回不存在，删除前整行留档 =="
psql_exec -c "DELETE FROM reports WHERE id=$RID;"
echo "-- GET /reports/$RID/changes/3 :"
c "$API_BASE/reports/$RID/changes/3" || true
echo "-- 轨迹中 DELETE 条目仍带删除前完整行："
c "$API_BASE/reports/$RID/changes" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("total =",d["total"],"| last =",d["items"][-1]["op"],"| old_row =",d["items"][-1]["old_row"])'

echo "== 7. 唯一索引未生效：同报告号两行按各自 id 独立成轨 =="
A="$(c -X POST "$API_BASE/reports" -d '{"lab_no":"L","report_no":"DUP","measured":1}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
B="$(c -X POST "$API_BASE/reports" -d '{"lab_no":"L","report_no":"DUP","measured":2}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
psql_exec -c "UPDATE reports SET measured=11 WHERE id=$A; UPDATE reports SET measured=22 WHERE id=$B;"
echo "id=$A 变更2后测值: $(c "$API_BASE/reports/$A/changes/2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["row"]["measured"])')"
echo "id=$B 变更2后测值: $(c "$API_BASE/reports/$B/changes/2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["row"]["measured"])')"

echo "DEMO-5 OK"
