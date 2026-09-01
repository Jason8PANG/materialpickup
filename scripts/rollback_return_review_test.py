# -*- coding: utf-8 -*-
"""
回滚「退料复核功能」自验证时污染的生产数据（幂等，可重复执行）。

回滚内容（精确到行）：
  1. 卷标 260829002: in_stock → in_shop（coil_length 保持 50.00 不动，回写值与原值一致）
  2. 退料单 #1781: confirmed → pending_return，清空 confirmed_by / confirm_time
  3. 退料明细 (request_id=1781, coil_id='260829002'): 清空 review_status/location/reviewed_by/reviewed_at
  4. 删除两条测试日志：RETURN_REVIEW_CONFIRM(260829002) / RETURN_REVIEW_REJECT(260901002)

运行: python scripts/rollback_return_review_test.py
"""
import os

import pymysql
from pymysql.cursors import DictCursor


def db_config():
    """与 app/config.py 保持一致的连接参数（环境变量可覆盖）"""
    return {
        'host': os.environ.get('MYSQL_HOST', '10.0.6.86'),
        'port': int(os.environ.get('MYSQL_PORT', 33306)),
        'user': os.environ.get('MYSQL_USER', 'root'),
        'password': os.environ.get('MYSQL_PASSWORD', 'root07'),
        'database': os.environ.get('MYSQL_DB', 'materialpickup'),
    }


def main():
    conn = pymysql.connect(charset='utf8mb4', cursorclass=DictCursor, **db_config())
    try:
        cur = conn.cursor()

        # ---------- 1. 卷标 260829002 回到 in_shop ----------
        cur.execute(
            "SELECT coil_id, status, coil_length FROM kr_wire_coil "
            "WHERE coil_id='260829002' AND is_deleted=0"
        )
        print(f'[1] 卷标 260829002 当前: {cur.fetchone()}')
        cur.execute(
            "UPDATE kr_wire_coil SET status='in_shop' "
            "WHERE coil_id='260829002' AND status='in_stock'"
        )
        print(f'[1] 已回滚 {cur.rowcount} 行 → in_shop（0 行表示本就非 in_stock，幂等跳过）')

        # ---------- 2. 退料单 #1781 回到 pending_return ----------
        cur.execute(
            "SELECT id, status, confirmed_by, confirm_time FROM kr_material_request "
            "WHERE id=1781 AND is_deleted=0"
        )
        print(f'[2] 退料单 #1781 当前: {cur.fetchone()}')
        cur.execute(
            "UPDATE kr_material_request SET status='pending_return', confirmed_by=NULL, confirm_time=NULL "
            "WHERE id=1781 AND status='confirmed'"
        )
        print(f'[2] 已回滚 {cur.rowcount} 行 → pending_return（0 行表示本就非 confirmed，幂等跳过）')

        # ---------- 3. 退料明细 review 字段清空 ----------
        cur.execute(
            "SELECT request_id, coil_id, review_status, location, review_note, reviewed_by, reviewed_at "
            "FROM kr_return_item WHERE request_id=1781 AND coil_id='260829002'"
        )
        print(f'[3] 退料明细当前: {cur.fetchone()}')
        cur.execute(
            "UPDATE kr_return_item SET review_status=NULL, location=NULL, reviewed_by=NULL, reviewed_at=NULL "
            "WHERE request_id=1781 AND coil_id='260829002'"
        )
        print(f'[3] 已清空 review 字段 {cur.rowcount} 行')

        # ---------- 4. 删除测试日志（删前 SELECT 打印确认） ----------
        cur.execute(
            "SELECT id, request_id, operator, action, detail FROM kr_operation_log "
            "WHERE action='RETURN_REVIEW_CONFIRM' AND detail LIKE '%260829002%'"
        )
        logs1 = cur.fetchall()
        print(f'[4a] 待删 RETURN_REVIEW_CONFIRM 日志: {logs1}')
        cur.execute(
            "DELETE FROM kr_operation_log "
            "WHERE action='RETURN_REVIEW_CONFIRM' AND detail LIKE '%260829002%'"
        )
        print(f'[4a] 已删除 {cur.rowcount} 条')

        cur.execute(
            "SELECT id, request_id, operator, action, detail FROM kr_operation_log "
            "WHERE action='RETURN_REVIEW_REJECT' AND detail LIKE '%260901002%'"
        )
        logs2 = cur.fetchall()
        print(f'[4b] 待删 RETURN_REVIEW_REJECT 日志: {logs2}')
        cur.execute(
            "DELETE FROM kr_operation_log "
            "WHERE action='RETURN_REVIEW_REJECT' AND detail LIKE '%260901002%'"
        )
        print(f'[4b] 已删除 {cur.rowcount} 条')

        conn.commit()

        # ---------- 5. 回滚后验证 ----------
        print('=== 回滚后验证 ===')
        cur.execute("SELECT coil_id, status FROM kr_wire_coil WHERE coil_id='260829002' AND is_deleted=0")
        print('卷标 260829002:', cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
            "JOIN kr_inventory_count c ON c.id = i.count_id "
            "WHERE i.coil_id='260829002' AND c.status='counting'"
        )
        print('盘点中计数(应为 >0):', cur.fetchone())
        cur.execute("SELECT id, status, confirmed_by, confirm_time FROM kr_material_request WHERE id=1781 AND is_deleted=0")
        print('退料单 #1781:', cur.fetchone())
        cur.execute(
            "SELECT request_id, coil_id, review_status, location, reviewed_by, reviewed_at "
            "FROM kr_return_item WHERE request_id=1781 AND coil_id='260829002'"
        )
        print('退料明细:', cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_operation_log "
            "WHERE action IN ('RETURN_REVIEW_CONFIRM','RETURN_REVIEW_REJECT') "
            "AND (detail LIKE '%260829002%' OR detail LIKE '%260901002%')"
        )
        print('剩余测试日志条数(应为 0):', cur.fetchone())
        cur.close()
    except Exception as e:
        conn.rollback()
        print(f'[ERROR] 回滚失败: {e}')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
