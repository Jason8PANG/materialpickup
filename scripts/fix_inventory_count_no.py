# -*- coding: utf-8 -*-
"""盘点单号规则迁移：INV+YYYYMMDD+3位日序号 → INV+YY+4位年度流水

  例：INV20261017001（14 位） → INV260001（9 位）

规则：
  - 按 created_at 顺序重排该年度流水号；
  - 该年度已有新格式单号时，从最大流水之后续号（不冲突）；
  - 同步更新 kr_wire_coil_consumption.job_order 中引用的旧单号
    （consume_type='count_adjust'，盘点差异调整以盘点单号为工单号）；
  - kr_inventory_count_item 通过 count_id 关联，无需处理。

幂等：没有旧格式单号时输出 SKIP 并退出，重复执行不产生任何变更。

用法：
  python scripts/fix_inventory_count_no.py            # 执行
  python scripts/fix_inventory_count_no.py --dry-run  # 只看计划，不改库
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import get_db_connection

LEGACY_RE = re.compile(r'^INV(\d{4})(\d{2})(\d{2})(\d{3})$')   # INV20261017001
NEW_RE = re.compile(r'^INV(\d{2})(\d{4})$')                     # INV260001


def _build_plan(rows):
    """rows -> [(id, old_no, new_no)]，按年度、created_at 顺序编号"""
    # 各年度已用的最大流水（新格式单号）
    max_seq = {}
    for r in rows:
        m = NEW_RE.match(r['count_no'] or '')
        if m:
            yy, seq = m.group(1), int(m.group(2))
            max_seq[yy] = max(max_seq.get(yy, 0), seq)

    legacy = []
    for r in rows:
        m = LEGACY_RE.match(r['count_no'] or '')
        if not m:
            continue
        legacy.append((m.group(1)[2:], r['created_at'], r['id'], r['count_no']))

    # 同一秒创建的按 id 排，保证顺序稳定
    legacy.sort(key=lambda x: (x[0], str(x[1] or ''), x[2]))

    plan = []
    for yy, _created, rid, old_no in legacy:
        seq = max_seq.get(yy, 0) + 1
        max_seq[yy] = seq
        plan.append((rid, old_no, f'INV{yy}{seq:04d}'))
    return plan


def main(dry_run=False):
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT id, count_no, created_at FROM kr_inventory_count ORDER BY created_at, id"
        )
        rows = cur.fetchall()

    plan = _build_plan(rows)
    if not plan:
        print('SKIP：没有需要迁移的旧格式盘点单号')
        return 0

    print(f'待迁移 {len(plan)} 张盘点单：')
    for _rid, old_no, new_no in plan:
        print(f'  {old_no} → {new_no}')

    if dry_run:
        print('（--dry-run，未修改数据库）')
        return 0

    with get_db_connection() as db:
        cur = db.cursor()
        try:
            for rid, old_no, new_no in plan:
                cur.execute(
                    "UPDATE kr_inventory_count SET count_no = %s WHERE id = %s AND count_no = %s",
                    (new_no, rid, old_no)
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f'盘点单 id={rid} 单号更新失败（rowcount={cur.rowcount}）')
                cur.execute(
                    "UPDATE kr_wire_coil_consumption SET job_order = %s "
                    "WHERE consume_type = 'count_adjust' AND job_order = %s",
                    (new_no, old_no)
                )
                affected = cur.rowcount
                print(f'  {old_no} → {new_no}（同步消耗记录 {affected} 行）')
            db.commit()
        except Exception as e:
            db.rollback()
            cur.close()
            print(f'失败，已回滚：{e}')
            return 1
        cur.close()

    # 校验唯一性与新格式
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT count_no, COUNT(*) AS n FROM kr_inventory_count GROUP BY count_no HAVING n > 1")
        dup = cur.fetchall()
        cur.execute("SELECT count_no FROM kr_inventory_count")
        bad = [r['count_no'] for r in cur.fetchall() if not NEW_RE.match(r['count_no'] or '')]
        cur.close()
    if dup or bad:
        print(f'校验失败：重复单号 {dup}，不合规单号 {bad}')
        return 1
    print('完成：单号已全部符合 INV+YY+4位流水，重复执行会 SKIP')
    return 0


if __name__ == '__main__':
    sys.exit(main(dry_run='--dry-run' in sys.argv))
