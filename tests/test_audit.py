"""变更轨迹（审计）功能自测。

覆盖：独立 SQL 与 API 写入、回滚不留记录、并发改动按生效顺序编号、
旧库已有行首次改动前值还原、同报告号多行按 id 区分、删除后按编号查返回不存在、
未提交值不暴露、TRUNCATE 被禁止。
"""
from __future__ import annotations

import threading
import time

import psycopg
import pytest

from app.config import settings


def _sql(sql_text: str, params: tuple = ()):
    """独立 SQL（不经 API，模拟数仓/DBA 直接改库），自动提交。"""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        cur = conn.execute(sql_text, params)
        if cur.description is not None:
            return cur.fetchall()
        return []


def test_insert_update_delete_trail_via_api_and_sql(client):
    # 1) API 补录 → INSERT 轨迹（new_row 为完整行，old_row 为 null）
    rid = client.post("/reports", json={"lab_no": "L1", "report_no": "A",
                                        "measured": 10}).json()["id"]
    trail = client.get(f"/reports/{rid}/changes").json()
    assert trail["report_id"] == rid and trail["total"] == 1
    ins = trail["items"][0]
    assert ins["op"] == "INSERT" and ins["change_no"] == 1
    assert ins["old_row"] is None
    assert ins["new_row"]["measured"] == 10
    assert ins["new_row"]["lab_no"] == "L1"
    assert ins["new_row"]["report_no"] == "A"
    assert ins["new_row"]["id"] == rid
    assert ins["application_name"] == "reports-api"
    assert ins["changed_by"] and ins["txid"] > 0 and ins["changed_at"]

    # 2) 独立 SQL 更正 → UPDATE 轨迹含变更前后完整行
    _sql("UPDATE reports SET measured = 42 WHERE id = %s", (rid,))
    trail = client.get(f"/reports/{rid}/changes").json()
    assert trail["total"] == 2
    upd = trail["items"][1]
    assert upd["op"] == "UPDATE" and upd["change_no"] == 2
    assert upd["old_row"]["measured"] == 10
    assert upd["new_row"]["measured"] == 42
    assert upd["application_name"] != "reports-api"  # 独立 SQL 来源可区分

    # 按报告 ID + 变更编号查看当时生效的整行
    s1 = client.get(f"/reports/{rid}/changes/1").json()
    assert s1["op"] == "INSERT" and s1["row"]["measured"] == 10
    s2 = client.get(f"/reports/{rid}/changes/2").json()
    assert s2["op"] == "UPDATE" and s2["row"]["measured"] == 42

    # 3) 独立 SQL 删除 → DELETE 轨迹（old_row 完整、new_row 为 null）
    _sql("DELETE FROM reports WHERE id = %s", (rid,))
    trail = client.get(f"/reports/{rid}/changes").json()
    assert trail["total"] == 3
    dele = trail["items"][2]
    assert dele["op"] == "DELETE" and dele["change_no"] == 3
    assert dele["old_row"]["measured"] == 42
    assert dele["new_row"] is None

    # 删除后按该编号查看当时生效的行 → 不存在（404）
    r = client.get(f"/reports/{rid}/changes/3")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "report_deleted"
    # 删除之前的状态仍可查；不存在的编号 → 404
    assert client.get(f"/reports/{rid}/changes/2").json()["row"]["measured"] == 42
    assert client.get(f"/reports/{rid}/changes/99").status_code == 404


def test_batch_backfill_is_traced(client):
    b = client.post("/reports/batch", json={"items": [
        {"lab_no": "L", "report_no": "B1", "measured": 1},
        {"lab_no": "L", "report_no": "B2", "measured": 2},
    ]}).json()
    assert b["inserted_count"] == 2
    for row in b["inserted"]:
        trail = client.get(f"/reports/{row['id']}/changes").json()
        assert trail["total"] == 1
        assert trail["items"][0]["op"] == "INSERT"
        assert trail["items"][0]["new_row"]["measured"] == row["measured"]
        assert trail["items"][0]["application_name"] == "reports-api"


def test_rollback_leaves_no_trace(client):
    rid = client.post("/reports", json={"lab_no": "L", "report_no": "RB",
                                        "measured": 1}).json()["id"]
    # 更正后回滚：轨迹与原行一起消失
    conn = psycopg.connect(settings.database_url)
    conn.execute("UPDATE reports SET measured = 2 WHERE id = %s", (rid,))
    conn.rollback()
    conn.close()
    assert client.get(f"/reports/{rid}/changes").json()["total"] == 1

    # 删除后回滚：同样不留记录
    conn = psycopg.connect(settings.database_url)
    conn.execute("DELETE FROM reports WHERE id = %s", (rid,))
    conn.rollback()
    conn.close()
    assert client.get(f"/reports/{rid}/changes").json()["total"] == 1

    # 回滚不占号：下一条已提交改动仍取编号 2，无空洞
    _sql("UPDATE reports SET measured = 3 WHERE id = %s", (rid,))
    trail = client.get(f"/reports/{rid}/changes").json()
    assert [i["change_no"] for i in trail["items"]] == [1, 2]
    assert trail["items"][1]["new_row"]["measured"] == 3


def test_concurrent_updates_numbered_by_effective_order(client):
    """并发改动同一报告：编号顺序 == 实际生效顺序，且未提交的值不暴露。

    T1 先改（持行锁、轨迹未提交），T2 并发改同一行：数据库行锁保证 T2 只能在
    T1 提交后生效，触发器据此取到下一个编号。沙箱环境会丢失锁等待的唤醒信号，
    故 T2 用 FOR UPDATE NOWAIT 轮询代替阻塞等待（等价地驱动同一交错时序，
    且不会产生滞留的锁等待后端）。
    """
    rid = client.post("/reports", json={"lab_no": "L", "report_no": "C",
                                        "measured": 0}).json()["id"]

    conn1 = psycopg.connect(settings.database_url)
    done: dict[str, bool] = {}
    try:
        conn1.execute("BEGIN")
        conn1.execute("UPDATE reports SET measured = 100 WHERE id = %s", (rid,))

        def t2_update():
            # 同一事务内先 NOWAIT 抢行锁再 UPDATE：抢到即持锁，UPDATE 不再等待。
            with psycopg.connect(settings.database_url) as c2:
                deadline = time.time() + 20
                while time.time() < deadline:
                    try:
                        c2.execute(
                            "SELECT id FROM reports WHERE id = %s FOR UPDATE NOWAIT",
                            (rid,),
                        )
                        break
                    except psycopg.errors.LockNotAvailable:
                        c2.rollback()
                        time.sleep(0.05)
                else:
                    done["ok"] = False
                    return
                c2.execute("UPDATE reports SET measured = 200 WHERE id = %s", (rid,))
                c2.commit()
                done["ok"] = True

        th = threading.Thread(target=t2_update, daemon=True)
        th.start()
        time.sleep(1.0)

        # T1 未提交：T2 尚未生效，API 也只能看到已提交轨迹（不暴露未提交的值）
        assert not done.get("ok")
        assert client.get(f"/reports/{rid}/changes").json()["total"] == 1

        conn1.commit()          # T1 先生效
        th.join(timeout=25)     # T2 随后生效
    finally:
        conn1.close()
    assert done.get("ok") is True

    # 编号顺序 == 实际生效顺序：100 → 2，200 → 3；编号连续无重复
    trail = client.get(f"/reports/{rid}/changes").json()
    assert [i["change_no"] for i in trail["items"]] == [1, 2, 3]
    assert trail["items"][1]["new_row"]["measured"] == 100
    assert trail["items"][2]["new_row"]["measured"] == 200
    assert trail["items"][2]["old_row"]["measured"] == 100


def test_legacy_row_first_change_captures_before_value(client):
    """旧库已有报告（功能启用前入仓）首次改动前的值也可还原。"""
    # 模拟“功能启用前已存在的行”：暂时停用触发器写入旧行，再启用。
    _sql("ALTER TABLE reports DISABLE TRIGGER reports_audit")
    try:
        legacy_id = _sql(
            "INSERT INTO reports(lab_no, report_no, measured) "
            "VALUES ('OLD', 'R1', 7) RETURNING id"
        )[0][0]
    finally:
        _sql("ALTER TABLE reports ENABLE TRIGGER reports_audit")

    # 启用前无任何轨迹；首次改动（独立 SQL）记录 change_no=1 且带改动前整行
    assert client.get(f"/reports/{legacy_id}/changes").json()["total"] == 0
    _sql("UPDATE reports SET measured = 8 WHERE id = %s", (legacy_id,))
    trail = client.get(f"/reports/{legacy_id}/changes").json()
    assert trail["total"] == 1
    first = trail["items"][0]
    assert first["change_no"] == 1 and first["op"] == "UPDATE"
    assert first["old_row"]["measured"] == 7   # 首次改动前的值可还原
    assert first["new_row"]["measured"] == 8

    # 旧行被删除：删除前内容同样完整留档
    _sql("ALTER TABLE reports DISABLE TRIGGER reports_audit")
    try:
        legacy_id2 = _sql(
            "INSERT INTO reports(lab_no, report_no, measured) "
            "VALUES ('OLD', 'R2', 70) RETURNING id"
        )[0][0]
    finally:
        _sql("ALTER TABLE reports ENABLE TRIGGER reports_audit")
    _sql("DELETE FROM reports WHERE id = %s", (legacy_id2,))
    trail2 = client.get(f"/reports/{legacy_id2}/changes").json()
    assert trail2["total"] == 1
    assert trail2["items"][0]["op"] == "DELETE"
    assert trail2["items"][0]["old_row"]["measured"] == 70
    assert client.get(f"/reports/{legacy_id2}/changes/1").status_code == 404


def test_same_report_no_distinguished_by_id(client):
    """唯一索引未生效时，同报告号的多行按各自 id 独立成轨。"""
    a = client.post("/reports", json={"lab_no": "L", "report_no": "DUP",
                                      "measured": 1}).json()["id"]
    b = client.post("/reports", json={"lab_no": "L", "report_no": "DUP",
                                      "measured": 2}).json()["id"]
    assert a != b
    _sql("UPDATE reports SET measured = 11 WHERE id = %s", (a,))
    _sql("UPDATE reports SET measured = 22 WHERE id = %s", (b,))

    ta = client.get(f"/reports/{a}/changes").json()
    tb = client.get(f"/reports/{b}/changes").json()
    assert [i["change_no"] for i in ta["items"]] == [1, 2]
    assert [i["change_no"] for i in tb["items"]] == [1, 2]
    assert ta["items"][1]["new_row"]["measured"] == 11
    assert tb["items"][1]["new_row"]["measured"] == 22

    # 按 id + 编号查看当时生效行，互不串扰
    assert client.get(f"/reports/{a}/changes/2").json()["row"]["measured"] == 11
    assert client.get(f"/reports/{b}/changes/2").json()["row"]["measured"] == 22


def test_unknown_report_and_change(client):
    r = client.get("/reports/999999/changes")
    assert r.status_code == 200
    assert r.json() == {"report_id": 999999, "total": 0, "items": []}
    assert client.get("/reports/999999/changes/1").status_code == 404


def test_truncate_is_forbidden(client):
    client.post("/reports", json={"lab_no": "L", "report_no": "T", "measured": 1})
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("TRUNCATE reports")
    # 数据与轨迹均未被破坏
    assert client.get("/reports").json()["total"] == 1


def test_openapi_lists_change_trail_endpoints(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/reports/{report_id}/changes" in spec["paths"]
    assert "/reports/{report_id}/changes/{change_no}" in spec["paths"]
