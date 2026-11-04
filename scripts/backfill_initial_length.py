# -*- coding: utf-8 -*-
"""回填 kr_wire_coil.initial_length（初始长度）。

背景
----
kr_wire_coil.coil_length 一个字段被两种语义共用：
  1. 登记时写入的是「初始长度」（原始单位，如 1000 FT）；
  2. 退料流程（return_bp 确认 / 退回复核）会把 coil_length 回写成「当前剩余长度」。

新增 initial_length 列后，消耗超限校验与剩余长度计算一律以 initial_length 为基准。
本脚本为历史行回填 initial_length。

判定「是否已被回写」的依据
--------------------------
coil_length 被回写的充要特征是：**存在一条 kr_return_item，其 remain_length
等于当前 coil_length**（退料时算出的剩余长度被原样写回 coil_length）。
据此反推初始长度：

    initial_length = coil_length + used_mm_at_writeback / factor

其中 used_mm_at_writeback 只统计**退料单创建之前**的消耗（consumption+count_adjust），
因为退料后可能继续消耗（换新的退料单），那部分消耗不属于被扣减的那一段。

判定「人工改过长度」的依据
--------------------------
kr_operation_log 中 action='COIL_UPDATE' 且 detail 含「长度」的记录。
这类卷标的 coil_length 已被人工改成任意值，无法反推初始长度，标为 low 不自动回填。

confidence 分级
--------------
  exact  = coil_length 仍是初始长度（未回写）→ initial_length = coil_length
           （与历史回退行为完全一致，零风险）
  high   = 已回写且反推出的初始长度接近标准整卷值 → 自动回填
  low    = 反推值偏离标准值 >2%，或卷标被人工改过长度 → 只列出，不自动回填
  skip   = 单位无换算系数（EA 等），无法换算 → 跳过

用法
----
    python scripts/backfill_initial_length.py            # dry-run（默认，只打印）
    python scripts/backfill_initial_length.py --apply    # 真正写库
"""
import argparse
import os
import sys

import pymysql
from pymysql.cursors import DictCursor

# 与 app/config.py 保持一致（不 import app 包，避免触发 Flask 初始化）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
except ImportError:
    pass

# 换算系数：与 Config.UNIT_CONVERT_FACTOR 一致（M=1000 / FT=304.8 / CM=10 / IN=25.4）
UNIT_CONVERT_FACTOR = {'M': 1000, 'FT': 304.8, 'CM': 10, 'IN': 25.4}

# 标准整卷长度（原始单位）。判定 high 用：反推值与最近标准值的相对偏差 <= 2%。
STANDARD_LENGTHS = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
                    1200, 1500, 1800, 2000, 2500, 3000, 3500, 4000, 5000]

# 反推值允许的最大相对偏差（%）。超出则标 low，只列出不自动回填。
TOLERANCE_PCT = 2.0

# 「卷标是否被人工改过长度」的日志特征
MANUAL_EDIT_ACTION = 'COIL_UPDATE'
MANUAL_EDIT_KEYWORD = '长度'


def get_connection():
    return pymysql.connect(
        host=os.environ.get('MYSQL_HOST', '10.0.6.86'),
        port=int(os.environ.get('MYSQL_PORT', 33306)),
        user=os.environ.get('MYSQL_USER', 'root'),
        password=os.environ.get('MYSQL_PASSWORD', 'root07'),
        database=os.environ.get('MYSQL_DB', 'materialpickup'),
        charset='utf8mb4',
        cursorclass=DictCursor,
    )


def factor_of(unit):
    """单位换算系数；单位未收录返回 None。"""
    u = (unit or '').strip().upper()
    return UNIT_CONVERT_FACTOR.get(u)


def nearest_standard(value):
    """返回 (最近标准值, 相对偏差%)；value 非正返回 (None, None)。

    标准值集合 = 常见整卷长度清单 + 最近的整数：
    实测库内初始长度多为「整英尺/整米」（如 819 / 905 / 648 / 999 FT），
    仅用固定清单会把大量真实初始长度误判为 low。
    """
    if not value or value <= 0:
        return None, None
    best, best_dev = None, None
    for s in list(STANDARD_LENGTHS) + [round(value)]:
        if s <= 0:
            continue
        dev = abs(value - s) / s * 100.0
        if best_dev is None or dev < best_dev:
            best, best_dev = s, dev
    return best, best_dev


def load_data(cursor):
    """一次性把回填所需的数据读进内存。"""
    cursor.execute(
        "SELECT coil_id, coil_length, unit, status, is_deleted "
        "FROM kr_wire_coil WHERE is_deleted = 0 ORDER BY coil_id"
    )
    coils = {r['coil_id']: r for r in cursor.fetchall()}

    # 退料单：卷标 → 该卷标所有退料单（按创建时间升序）
    cursor.execute(
        "SELECT coil_id, remain_length, created_at FROM kr_return_item "
        "ORDER BY created_at, id"
    )
    returns = {}
    for r in cursor.fetchall():
        returns.setdefault(r['coil_id'], []).append(r)

    # 消耗记录：卷标 → [(created_at, out_length)]
    cursor.execute(
        "SELECT coil_id, out_length, created_at FROM kr_wire_coil_consumption "
        "WHERE consume_type IN ('consumption','count_adjust') ORDER BY created_at, id"
    )
    consumptions = {}
    for r in cursor.fetchall():
        consumptions.setdefault(r['coil_id'], []).append(r)

    # 人工改过长度的卷标
    cursor.execute(
        "SELECT detail FROM kr_operation_log WHERE action = %s", (MANUAL_EDIT_ACTION,)
    )
    manual_edited = set()
    for r in cursor.fetchall():
        detail = r['detail'] or ''
        if MANUAL_EDIT_KEYWORD in detail and '修改卷标 ' in detail:
            manual_edited.add(detail.split('修改卷标 ')[1].split(':')[0].strip())

    # CSI 登记值（独立 ground truth）：首次 sl-qty-move 的 quantity = 登记时的初始长度
    cursor.execute(
        "SELECT coil_id, quantity FROM kr_csi_interface "
        "WHERE api_name = 'sl-qty-move' AND quantity IS NOT NULL "
        "ORDER BY coil_id, createdate, id"
    )
    csi_first = {}
    for r in cursor.fetchall():
        csi_first.setdefault(r['coil_id'], float(r['quantity']))

    return coils, returns, consumptions, manual_edited, csi_first


def classify(coil, returns, consumptions, manual_edited, csi_first):
    """返回 (confidence, initial_length, 说明)。

    confidence ∈ {exact, high, low, skip}

    判定顺序：
      1. 无消耗 → coil_length 即初始长度（exact），不需要换算系数；
      2. 有消耗且已回写（存在退料单 remain_length == coil_length）→ 反推初始长度（high/low）；
      3. 有消耗但被人工改过长度 → 无法反推（low）；
      4. 有消耗但未回写 → coil_length 仍是初始长度（exact）。

    high 的判定依据（按可信度从高到低）：
      1. 有 CSI 登记记录且反推值与之一致 → 直接印证，最强证据；
      2. 无 CSI 记录，但反推值接近整卷长度（整数/标准值，偏差 <= 2%）。
    """
    cl = float(coil['coil_length'])
    rows = consumptions.get(coil['coil_id'], [])
    used = sum(float(x['out_length'] or 0) for x in rows)

    # 已被回写：存在退料单，其 remain_length 等于当前 coil_length
    hit = None
    for r in returns.get(coil['coil_id'], []):
        if r['remain_length'] is None:
            continue
        if abs(float(r['remain_length']) - cl) <= 0.005:
            hit = r
            break

    if used == 0:
        # 无消耗 → 没有发生过「扣减后回写」，coil_length 就是初始长度
        return 'exact', cl, '无消耗，coil_length 即初始长度'

    factor = factor_of(coil['unit'])
    if not factor:
        return 'skip', None, '单位 %s 无换算系数，无法反推' % (coil['unit'] or '空')

    if hit is not None:
        # 反推：只算退料单创建之前的消耗
        used_before = sum(float(x['out_length'] or 0)
                          for x in rows if x['created_at'] <= hit['created_at'])
        candidate = cl + used_before / factor
        std, dev = nearest_standard(candidate)
        csi = csi_first.get(coil['coil_id'])
        if csi is not None:
            # 有独立证据（CSI 登记值）：一致则直接采信，不一致则存疑
            if abs(candidate - csi) <= max(0.02, csi * 0.002):
                return 'high', candidate, '已回写，反推初始长度 %.4f（与 CSI 登记值 %.4f 一致）' % (candidate, csi)
            return 'low', candidate, ('已回写但反推值 %.4f 与 CSI 登记值 %.4f 不一致' % (candidate, csi))
        if dev is not None and dev <= TOLERANCE_PCT:
            return 'high', candidate, '已回写，反推初始长度≈%s(偏差%.2f%%)' % (std, dev)
        return 'low', candidate, '已回写但反推值偏离标准值 %.2f%%（候选 %s）' % (dev or 0, candidate)

    if coil['coil_id'] in manual_edited:
        return 'low', None, '人工改过长度，无法反推初始长度'

    # 未回写：coil_length 仍是初始长度
    return 'exact', cl, '未回写，coil_length 即初始长度'


def main():
    parser = argparse.ArgumentParser(description='回填 kr_wire_coil.initial_length（默认 dry-run）')
    parser.add_argument('--apply', action='store_true',
                        help='真正写库（默认只打印，不写）')
    args = parser.parse_args()

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            coils, returns, consumptions, manual_edited, csi_first = load_data(cursor)

            results = []
            for coil_id, coil in coils.items():
                conf, initial, note = classify(coil, returns, consumptions, manual_edited, csi_first)
                results.append((coil_id, conf, initial, note))

            counts = {'exact': 0, 'high': 0, 'low': 0, 'skip': 0}
            for _, conf, _, _ in results:
                counts[conf] += 1

            total = len(results)
            print('=' * 78)
            print('initial_length 回填%s' % ('（--apply 写库）' if args.apply else '（dry-run，不写库）'))
            print('=' * 78)
            print('卷标总数        : %d' % total)
            print('  exact（直接取 coil_length，行为不变）: %d' % counts['exact'])
            print('  high （反推初始长度，可信）        : %d' % counts['high'])
            print('  low  （存疑，只列出不回填）        : %d' % counts['low'])
            print('  skip （单位无换算系数）            : %d' % counts['skip'])
            print()

            # 待写入：exact + high
            writable = [(cid, init, note) for cid, conf, init, note in results
                        if conf in ('exact', 'high') and init is not None]
            print('将回填条数(exact+high): %d ；low/skip 不回填' % len(writable))
            print()

            if args.apply:
                updated = 0
                for cid, init, note in writable:
                    cursor.execute(
                        "UPDATE kr_wire_coil SET initial_length = %s "
                        "WHERE coil_id = %s AND is_deleted = 0 AND initial_length IS NULL",
                        (round(float(init), 4), cid)
                    )
                    updated += cursor.rowcount
                conn.commit()
                print('[OK] 已回填 %d 条（low/skip 未回填）' % updated)
            else:
                print('[dry-run] 未写库。确认无误后加 --apply 执行。')
                print()

            print('=' * 78)
            print('前 20 条样例')
            print('=' * 78)
            print('%-12s %-6s %-14s %s' % ('coil_id', 'conf', 'initial_length', '说明'))
            for cid, conf, init, note in results[:20]:
                print('%-12s %-6s %-14s %s' % (
                    cid, conf,
                    ('%.4f' % init) if init is not None else '-', note))

            if counts['low']:
                print()
                print('=' * 78)
                print('low 清单（不自动回填，需人工确认）')
                print('=' * 78)
                print('%-12s %-14s %s' % ('coil_id', '候选值', '说明'))
                for cid, conf, init, note in results:
                    if conf == 'low':
                        print('%-12s %-14s %s' % (
                            cid, ('%.4f' % init) if init is not None else '-', note))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
