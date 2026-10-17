# -*- coding: utf-8 -*-
"""CSI 接口表库位校正（一律以 CSI 实时库位为准）

成因：kr_wire_coil.loc 只在退料/维护时写入，物料领用出库到车间后不会更新，
接口表入队时抄了这个旧库位，提交时被平台按「货位无存货」拒绝。例：
  INV260001001 卷标 261016004 的来源库位写成 RM-P015-1，CSI 实际在 Floor-Core。

规则：
  - 只处理 is_sync=0 且 status='PENDING' 的行（尚未提交、或等待重试）；
  - sl-qty-move：用 CSI 实时库位覆盖 from_location；
    若 from_location == to_location（物料已在目标库位）→ is_sync=2 / status='NOOP'，
    不再提交、不再发失败邮件；
  - sl-misc-receipt / sl-misc-issue：用 CSI 实时库位覆盖 to_location（即接口的 location）；
  - 已过账（is_sync=1/SUCCESS）以及 CANCELLED / REVERSED / FAILED_MANUAL 一律不改动，
    只在报告里列出（已过账再改会导致账实不符）；
  - CSI 查不到库位时保留原值并提示，不猜测。

幂等：处理过的行不再是 PENDING，重复执行不会产生任何变更。

用法：
  python scripts/fix_csi_interface_location.py                     # dry-run（默认，只打印计划）
  python scripts/fix_csi_interface_location.py --apply             # 实际写库
  python scripts/fix_csi_interface_location.py --apply --limit 50  # 限制一次处理的行数
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config                # noqa: E402
from app.models import get_db_connection     # noqa: E402
from app.services.csi_service import CSIClient  # noqa: E402

API_QTY_MOVE = 'sl-qty-move'
MISC_APIS = ('sl-misc-receipt', 'sl-misc-issue')
NOOP_MESSAGE = '物料已在目标库位，无需移库'
OPERATOR = 'fix_csi_interface_location'

PENDING_SQL = (
    "SELECT id, request_id, api_name, siteRef, item, lot, coil_id, quantity, "
    "       warehouse, from_location, to_location, documentnumber, status, is_sync, retry_count "
    "FROM kr_csi_interface "
    "WHERE is_sync = 0 AND status = 'PENDING' AND api_name IN (%s, %s, %s) "
    "ORDER BY id LIMIT %s"
)


def _live_location(siteref, item, lot, cache):
    """CSI 实时库位（同 (item, lot) 只查一次）；查不到返回 None"""
    key = ((item or '').strip(), (lot or '').strip())
    if not key[0] or not key[1]:
        return None
    if key in cache:
        return cache[key]
    site = str(siteref or '410').strip() or '410'
    try:
        loc = str(CSIClient(site).get_lot_location(key[0], key[1]) or '').strip()
    except Exception as e:
        print(f'    ! CSI 查询失败 item={key[0]} lot={key[1]}: {e}')
        loc = ''
    cache[key] = loc or None
    return loc or None


def _plan(row, cache):
    """给一行出计划：{'action': UPDATE|NOOP|SKIP, 'column': ..., 'old': ..., 'new': ..., 'note': ...}"""
    api = (row.get('api_name') or '').strip() or API_QTY_MOVE
    live = _live_location(row.get('siteRef'), row.get('item'), row.get('lot'), cache)

    if api in MISC_APIS:
        column = 'to_location'
    else:
        column = 'from_location'
    old = (row.get(column) or '').strip()

    if not live:
        return {'action': 'SKIP', 'column': column, 'old': old, 'new': old,
                'note': 'CSI 未返回库位，保留原值'}

    new = live
    if api in MISC_APIS:
        if new.strip().lower() == old.lower():
            return {'action': 'SKIP', 'column': column, 'old': old, 'new': old,
                    'note': '与 CSI 实时库位一致，无需调整'}
        return {'action': 'UPDATE', 'column': column, 'old': old, 'new': new, 'note': ''}

    # sl-qty-move：源=目的 → 物料已在目标库位，标记 NOOP
    to_loc = (row.get('to_location') or Config.CSI_TO_LOCATION or '').strip()
    if to_loc and new.strip().lower() == to_loc.strip().lower():
        return {'action': 'NOOP', 'column': column, 'old': old, 'new': new,
                'note': f'源=目的({to_loc})，物料已在目标库位'}
    if new.strip().lower() == old.lower():
        return {'action': 'SKIP', 'column': column, 'old': old, 'new': old,
                'note': '与 CSI 实时库位一致，无需调整'}
    return {'action': 'UPDATE', 'column': column, 'old': old, 'new': new, 'note': ''}


def _print_row(row, plan):
    api = (row.get('api_name') or '').strip() or API_QTY_MOVE
    print(f"  id={row['id']}  单号={row.get('documentnumber') or '-'}  {api}  "
          f"物料={row.get('item')}  批号={row.get('lot')}  卷标={row.get('coil_id') or '-'}")
    print(f"    仓库={row.get('warehouse') or '-'}  数量={row.get('quantity')}  "
          f"重试次数={row.get('retry_count')}  "
          f"{'from' if plan['column'] == 'from_location' else 'to'}_location="
          f"{plan['old'] or '(空)'}")
    if plan['action'] == 'UPDATE':
        print(f"    动作：UPDATE {plan['column']}  {plan['old'] or '(空)'} → {plan['new']}")
    elif plan['action'] == 'NOOP':
        print(f"    动作：标记 NOOP（is_sync=2）  CSI 实时={plan['new']}  {plan['note']}")
    else:
        print(f"    动作：跳过（{plan['note']}）")


def _print_history(cur, pending_rows):
    """同物料/批号的其它接口行：只报告，不改动"""
    keys = [((r.get('item') or '').strip(), (r.get('lot') or '').strip()) for r in pending_rows]
    keys = sorted({k for k in keys if k[0] and k[1]})
    if not keys:
        return
    where = ' OR '.join(['(item = %s AND lot = %s)'] * len(keys))
    params = [x for k in keys for x in k]
    cur.execute(
        "SELECT id, request_id, api_name, item, lot, from_location, to_location, "
        "       documentnumber, is_sync, status "
        "FROM kr_csi_interface "
        f"WHERE ({where}) AND NOT (is_sync = 0 AND status = 'PENDING') "
        "ORDER BY id",
        tuple(params)
    )
    rows = cur.fetchall()
    if not rows:
        return
    print('\n参考：同物料/批号的其它接口行（已过账或已终结，均不改动）')
    for r in rows:
        print(f"  id={r['id']}  单号={r.get('documentnumber') or '-'}  "
              f"{(r.get('api_name') or '').strip()}  is_sync={r.get('is_sync')} "
              f"status={r.get('status')}  "
              f"{r.get('from_location') or '-'} → {r.get('to_location') or '-'}")


def main():
    parser = argparse.ArgumentParser(description='CSI 接口表库位校正（以 CSI 实时库位为准）')
    parser.add_argument('--apply', action='store_true', help='实际写库（默认只做 dry-run）')
    parser.add_argument('--limit', type=int, default=200, help='一次最多处理的行数，默认 200')
    args = parser.parse_args()

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f'== CSI 接口表库位校正（{mode}） ==')

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(PENDING_SQL, (API_QTY_MOVE,) + MISC_APIS + (args.limit,))
        rows = cur.fetchall()
        if not rows:
            print('没有 is_sync=0 / PENDING 的待处理行，无需校正。')
            _print_history(cur, [])
            cur.close()
            return 0

        cache = {}
        plans = []
        for row in rows:
            plan = _plan(row, cache)
            plans.append((row, plan))

        print(f'\n待处理行 {len(rows)} 条：\n')
        for row, plan in plans:
            _print_row(row, plan)

        todo = [(r, p) for r, p in plans if p['action'] in ('UPDATE', 'NOOP')]
        updated = noop = skipped = 0
        if args.apply and todo:
            print('\n开始写库 ...')
            for row, plan in todo:
                if plan['action'] == 'NOOP':
                    cur.execute(
                        "UPDATE kr_csi_interface "
                        "SET is_sync = 2, status = 'NOOP', errormessage = %s, "
                        "    last_poll_at = NULL, updateby = %s, updatedate = NOW() "
                        "WHERE id = %s AND is_sync = 0 AND status = 'PENDING'",
                        (NOOP_MESSAGE, OPERATOR, row['id'])
                    )
                    if cur.rowcount:
                        noop += 1
                        print(f"  id={row['id']} 单号={row.get('documentnumber')} → NOOP")
                    else:
                        skipped += 1
                        print(f"  id={row['id']} 单号={row.get('documentnumber')} 状态已变化，跳过")
                else:
                    cur.execute(
                        f"UPDATE kr_csi_interface SET {plan['column']} = %s, "
                        "       updateby = %s, updatedate = NOW() "
                        "WHERE id = %s AND is_sync = 0 AND status = 'PENDING'",
                        (plan['new'], OPERATOR, row['id'])
                    )
                    if cur.rowcount:
                        updated += 1
                        print(f"  id={row['id']} 单号={row.get('documentnumber')} "
                              f"{plan['column']} {plan['old'] or '(空)'} → {plan['new']}")
                    else:
                        skipped += 1
                        print(f"  id={row['id']} 单号={row.get('documentnumber')} 状态已变化，跳过")
            conn.commit()
        elif todo:
            print('\n（dry-run：未写库，加 --apply 执行）')

        print('\n== 汇总 ==')
        print(f'  待处理 {len(rows)} 行：需校正 {sum(1 for _, p in plans if p["action"] == "UPDATE")} 行，'
              f'需标记 NOOP {sum(1 for _, p in plans if p["action"] == "NOOP")} 行，'
              f'无需调整 {sum(1 for _, p in plans if p["action"] == "SKIP")} 行')
        if args.apply:
            print(f'  实际写入：库位校正 {updated} 行，NOOP {noop} 行，跳过 {skipped} 行')
        _print_history(cur, rows)
        cur.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
