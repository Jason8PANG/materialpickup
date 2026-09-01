# -*- coding: utf-8 -*-
"""
恢复被删除的卷标（审计 + 尽力重建）。

背景：删除功能曾为物理删除（DELETE），已删除卷标无法 100% 还原；
     本次已改为软删除（is_deleted=1），此脚本用于：
     1. 审计：列出所有历史 COIL_DELETE 日志（被删卷标、申请单、操作人、时间）；
     2. 恢复：对可确定归属与物料的被删卷标重建到 kr_wire_coil（status=in_stock）。
     3. 输出无法自动确定字段的清单，供人工确认后补录。

用法：python scripts/recover_deleted_coils.py
"""
import sys
import os
import re
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from app.config import Config

conn = pymysql.connect(
    host=Config.MYSQL_HOST, port=Config.MYSQL_PORT,
    user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
    database=Config.MYSQL_DB, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=10,
)
cur = conn.cursor()

# 1. 审计：删除日志
cur.execute(
    "SELECT request_id, operator, detail, created_at FROM kr_operation_log "
    "WHERE action = 'COIL_DELETE' ORDER BY created_at"
)
logs = cur.fetchall()
print(f'== 历史删除记录 {len(logs)} 条 ==')
deleted = {}  # coil_id -> info
for lg in logs:
    m = re.search(r'(?:软删除|删除)卷标:\s*(\d+)', lg['detail'] or '')
    if not m:
        continue
    cid = m.group(1)
    deleted.setdefault(cid, {'request_id': lg['request_id'], 'operator': lg['operator'],
                             'first_del': lg['created_at'], 'count': 0})
    deleted[cid]['count'] += 1

for cid, info in deleted.items():
    print(f"  {cid}  申请单{info['request_id']}  {info['operator']}  删除{info['count']}次  首次{info['first_del']}")

# 2. 检查每个被删卷标当前状态（是否已存在/已软删）
print('\n== 恢复可行性 ==')
restorable = []
for cid, info in deleted.items():
    cur.execute("SELECT id, status, is_deleted FROM kr_wire_coil WHERE coil_id = %s", (cid,))
    row = cur.fetchone()
    if row:
        print(f"  {cid}: 已存在(id={row['id']}, status={row['status']}, is_deleted={row['is_deleted']}) → 无需恢复")
        if row['is_deleted'] == 1:
            restorable.append((cid, info, 'soft_delete'))  # 可反删除
    else:
        print(f"  {cid}: 物理已删除 → 尝试重建")
        restorable.append((cid, info, 'rebuild'))

# 3. 恢复
print('\n== 执行恢复 ==')
recovered = 0
for cid, info, mode in restorable:
    rid = info['request_id']
    # 该申请单物料（可能多个，取唯一或第一）
    cur.execute(
        "SELECT part_number, unit FROM kr_request_item WHERE request_id = %s AND part_number != ''",
        (rid,)
    )
    items = cur.fetchall()
    if not items:
        print(f"  {cid}: 申请单{rid}无物料记录，无法确定物料，跳过")
        continue
    parts = list(dict.fromkeys([i['part_number'] for i in items]))
    unit = next((i['unit'] or '' for i in items if i.get('unit')), '')

    if mode == 'soft_delete':
        # 反软删除
        cur.execute("UPDATE kr_wire_coil SET is_deleted = 0 WHERE coil_id = %s", (cid,))
        if cur.rowcount:
            recovered += 1
            print(f"  {cid}: 已反软删除恢复（申请单{rid}）")
        conn.commit()
        continue

    # 重建（物理已删除）
    # 支持 --force-part <料号>：人工确认物料后强制按该物料恢复（多物料申请单用）
    force_part = None
    for i, a in enumerate(sys.argv):
        if a == '--force-part' and i + 1 < len(sys.argv):
            force_part = sys.argv[i + 1]
    if len(parts) > 1 and not force_part:
        print(f"  {cid}: 申请单{rid}有多个物料 {parts}，无法唯一确定，需人工确认后补录")
        continue
    part = force_part if force_part else parts[0]
    if force_part and force_part not in parts:
        print(f"  {cid}: 指定物料 {force_part} 不在申请单{rid}物料 {parts} 中，跳过")
        continue
    # 站点 + 对应申请单行 item_id + 操作人
    cur.execute("SELECT siteref FROM kr_material_request WHERE id = %s", (rid,))
    rq = cur.fetchone()
    siteref = rq['siteref'] if rq else ''
    cur.execute(
        "SELECT id FROM kr_request_item WHERE request_id = %s AND part_number = %s ORDER BY id LIMIT 1",
        (rid, part)
    )
    item_row = cur.fetchone()
    item_id = item_row['id'] if item_row else 0
    # coil_length 物理删除后未知 → 用 0 占位，输出清单提醒人工补录真实长度后再使用
    cur.execute(
        "INSERT INTO kr_wire_coil (coil_id, request_id, siteref, part_number, unit, coil_length, status, item_id, operator) "
        "VALUES (%s, %s, %s, %s, %s, 0, 'in_stock', %s, 'system_recover')",
        (cid, rid, siteref, part, unit, item_id)
    )
    conn.commit()
    recovered += 1
    print(f"  {cid}: 已重建（申请单{rid}, 物料{part}），coil_length=0 待人工补录")

conn.close()
print(f'\n完成：恢复 {recovered} 个，其余需人工确认（见上）。')
print('注意：重建的卷标 coil_length 未知，需在界面/脚本补录真实长度后再使用。')
