"""重编号 kr_csi_interface / kr_wire_coil_consumption 的主键，消除跳号。

用法：
    python scripts/renumber_ids.py            # dry-run
    python scripts/renumber_ids.py --apply    # 备份后执行

规则：按原 id 升序重排为 1..N（保持处理顺序），同步更新 kr_scrap_reason.consumption_id，
最后重建表把 AUTO_INCREMENT 落到 N+1。
"""
import json
import os
import sys
from datetime import datetime

import pymysql

DB = dict(host='10.0.6.86', port=33306, user='root', password='root07',
          database='materialpickup', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor)
APPLY = '--apply' in sys.argv
TEMP_OFFSET = 10_000_000          # 两阶段重编号的临时偏移，避免与旧 id 撞主键

TABLES = [
    ('kr_csi_interface', None),
    ('kr_wire_coil_consumption', None),
]
# 注意：kr_scrap_reason 由外部系统（WIP Track）写入，本项目无写代码，
# 其 consumption_id 实测为外部 id 体系（如 318/321，与本地消耗表 id 不对应），
# 因此重编号时不做映射，避免误改外部数据。


def plan(cur, table):
    cur.execute(f'SELECT id FROM {table} ORDER BY id')
    old_ids = [r['id'] for r in cur.fetchall()]
    mapping = {old: new for new, old in enumerate(old_ids, start=1)}
    return old_ids, mapping


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()

    plans = {}
    print('=== 重编号方案 ===')
    for table, ref in TABLES:
        old_ids, mapping = plan(cur, table)
        gaps = (max(old_ids) - min(old_ids) + 1 - len(old_ids)) if old_ids else 0
        print(f'{table}: {len(old_ids)} 行, id {min(old_ids)}~{max(old_ids)} → 1~{len(old_ids)} (当前缺失 {gaps} 个)')
        plans[table] = mapping

    cur.execute("SELECT COUNT(*) n FROM kr_scrap_reason WHERE consumption_id IS NOT NULL")
    ref_n = cur.fetchone()['n']
    print(f'kr_scrap_reason 引用 consumption_id 的行数: {ref_n}')

    if not APPLY:
        print('\n[dry-run] 未做任何修改。加 --apply 执行。')
        cur.close()
        conn.close()
        return

    # ---- 备份 ----
    bdir = os.path.join(os.path.dirname(__file__), 'backups')
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = {'mapping': {t: {str(k): v for k, v in m.items()} for t, m in plans.items()}, 'tables': {}}
    for table, _ in TABLES:
        cur.execute(f'SELECT * FROM {table}')
        backup['tables'][table] = cur.fetchall()
    cur.execute('SELECT * FROM kr_scrap_reason')
    backup['tables']['kr_scrap_reason'] = cur.fetchall()
    bpath = os.path.join(bdir, f'renumber_ids_{stamp}.json')
    with open(bpath, 'w', encoding='utf-8') as f:
        json.dump(backup, f, ensure_ascii=False, default=str, indent=2)
    print(f'\n[备份] {bpath}')

    try:
        conn.begin()
        for table, _ in TABLES:
            mapping = plans[table]
            # 阶段 1：整体挪到临时区（负数或大偏移），避免与目标 id 撞
            cur.execute(f'UPDATE {table} SET id = id + %s', (TEMP_OFFSET,))
            # 阶段 2：按映射落位（此时目标区间空闲）
            for old, new in mapping.items():
                cur.execute(f'UPDATE {table} SET id = %s WHERE id = %s', (new, old + TEMP_OFFSET))

        # 同步引用列（当前 TABLES 未配置引用列，保留逻辑便于以后扩展）
        for table, ref in TABLES:
            if not ref:
                continue
            ref_table, ref_col = ref.split('.')
            for old, new in plans[table].items():
                cur.execute(f'UPDATE {ref_table} SET {ref_col} = %s WHERE {ref_col} = %s', (new, old))

        conn.commit()
        print('[OK] 重编号完成')
    except Exception as e:
        conn.rollback()
        print(f'[FAIL] 已回滚：{e}')
        raise
    finally:
        cur.close()
        conn.close()

    # ---- 重置 AUTO_INCREMENT ----
    # 两阶段重编号会临时把 id 顶到 1000 万+，InnoDB 自增计数器随之变高；
    # MySQL 8 下 ALTER TABLE ... AUTO_INCREMENT = N 可显式降回 MAX+1（重建 ENGINE 无效）。
    conn = pymysql.connect(**DB)
    conn.autocommit(True)
    cur = conn.cursor()
    for table, _ in TABLES:
        cur.execute(f'SELECT COUNT(*) n, MIN(id) mn, MAX(id) mx FROM {table}')
        s = cur.fetchone()
        cur.execute(f'ALTER TABLE {table} AUTO_INCREMENT = %s', (s['mx'] + 1,))
        print(f'[校验] {table}: {s["n"]} 行, id {s["mn"]}~{s["mx"]}, 下一号 {s["mx"] + 1}')
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
