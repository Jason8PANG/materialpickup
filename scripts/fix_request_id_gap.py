"""修正 kr_material_request 的 id 跳号（自增值被历史测试数据顶高）。

用法：
    python scripts/fix_request_id_gap.py            # dry-run，只打印映射
    python scripts/fix_request_id_gap.py --apply    # 备份后执行

规则：把 id >= 990000 的单据，按 id 升序重编号为「正常段 MAX(id)+1」起的连续号，
并同步更新所有引用 request_id / prev_request_id 的表，最后重置 AUTO_INCREMENT。
"""
import json
import os
import sys
from datetime import datetime

import pymysql

DB = dict(host='10.0.6.86', port=33306, user='root', password='root07',
          database='materialpickup', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor)
GAP = 990000
REF_COLUMNS = [  # 表 -> 引用列
    ('kr_approval_token', ['request_id']),
    ('kr_csi_interface', ['request_id']),
    ('kr_operation_log', ['request_id']),
    ('kr_request_item', ['request_id']),
    ('kr_request_item_lot', ['request_id']),
    ('kr_return_item', ['request_id']),
    ('kr_wire_coil', ['request_id', 'prev_request_id']),
]
APPLY = '--apply' in sys.argv


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()

    cur.execute("SELECT id, request_type, status, requester, siteref, created_at "
                "FROM kr_material_request WHERE id >= %s ORDER BY id", (GAP,))
    rows = cur.fetchall()
    if not rows:
        print('[SKIP] 没有需要修正的 id（>=%s）' % GAP)
        return

    cur.execute("SELECT COALESCE(MAX(id),0) mx FROM kr_material_request WHERE id < %s", (GAP,))
    start = cur.fetchone()['mx'] + 1

    mapping = []
    for i, r in enumerate(rows):
        mapping.append({'old': r['id'], 'new': start + i, 'type': r['request_type'],
                        'requester': r['requester'], 'created_at': str(r['created_at'])})

    print('=== 映射方案 ===')
    for m in mapping:
        print(f"  {m['old']} -> {m['new']}  ({m['type']}, {m['requester']}, {m['created_at']})")
    print(f"=== AUTO_INCREMENT: 当前 -> {start + len(mapping)} ===")
    print('=== 关联表影响行数 ===')
    for table, cols in REF_COLUMNS:
        for col in cols:
            cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE {col} >= %s", (GAP,))
            n = cur.fetchone()['n']
            if n:
                print(f'  {table}.{col}: {n} 行')

    if not APPLY:
        print('\n[dry-run] 未做任何修改。加 --apply 执行。')
        cur.close()
        conn.close()
        return

    # ---- 备份 ----
    bdir = os.path.join(os.path.dirname(__file__), 'backups')
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = {'mapping': mapping, 'tables': {}}
    cur.execute("SELECT * FROM kr_material_request WHERE id >= %s", (GAP,))
    backup['tables']['kr_material_request'] = cur.fetchall()
    for table, cols in REF_COLUMNS:
        cond = ' OR '.join(f'{c} >= %s' for c in cols)
        cur.execute(f"SELECT * FROM {table} WHERE {cond}", tuple([GAP] * len(cols)))
        backup['tables'][table] = cur.fetchall()
    bpath = os.path.join(bdir, f'fix_request_id_gap_{stamp}.json')
    with open(bpath, 'w', encoding='utf-8') as f:
        json.dump(backup, f, ensure_ascii=False, default=str, indent=2)
    print(f'\n[备份] {bpath}')

    # ---- 执行（事务）----
    try:
        conn.begin()
        cur.execute("SELECT id FROM kr_material_request WHERE id >= %s ORDER BY id FOR UPDATE", (GAP,))
        locked = [r['id'] for r in cur.fetchall()]
        if locked != [m['old'] for m in mapping]:
            raise RuntimeError('加锁后 id 列表与 dry-run 不一致，已中止（请重跑 dry-run）')

        for m in mapping:
            cur.execute("UPDATE kr_material_request SET id = %s WHERE id = %s", (m['new'], m['old']))
            for table, cols in REF_COLUMNS:
                for col in cols:
                    cur.execute(f"UPDATE {table} SET {col} = %s WHERE {col} = %s", (m['new'], m['old']))
        cur.execute("ALTER TABLE kr_material_request AUTO_INCREMENT = %s", (start + len(mapping),))
        conn.commit()
        print(f'[OK] 已重编号 {len(mapping)} 条，AUTO_INCREMENT = {start + len(mapping)}')
    except Exception as e:
        conn.rollback()
        print(f'[FAIL] 已回滚：{e}')
        raise
    finally:
        cur.close()
        conn.close()

    # ---- 校验 ----
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) n, MAX(id) mx FROM kr_material_request WHERE id >= %s", (GAP,))
    chk = cur.fetchone()
    cur.execute("SELECT AUTO_INCREMENT ai FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA='materialpickup' AND TABLE_NAME='kr_material_request'")
    ai = cur.fetchone()['ai']
    print(f'[校验] 残留异常 id: {chk["n"]} 条, 当前 MAX(id)={chk["mx"]}, AUTO_INCREMENT={ai}')
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
