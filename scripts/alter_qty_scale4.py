#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把「数量 / 长度」类列的精度从 DECIMAL(x,2) 扩到 DECIMAL(x,4)。

设计要点：
  * 幂等：先查 information_schema.COLUMNS，scale 已是 4 的列直接跳过；
  * 安全：MODIFY 语句的 NOT NULL / DEFAULT / COMMENT 全部从当前定义原样回填，
           不新增、不删除任何约束（金额列不在本次范围内，一律不动）；
  * 可审计：逐表打印 SHOW CREATE TABLE 前后差异行。

用法：
    python scripts/alter_qty_scale4.py            # 执行
    python scripts/alter_qty_scale4.py --dry-run  # 只打印将要执行的 SQL
    python scripts/alter_qty_scale4.py --diff-only# 只打印当前 18 列定义（不做变更）

连接参数与 app/config.py 一致，可用环境变量 MYSQL_HOST / MYSQL_PORT /
MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB 覆盖，也可在 .env 中提供。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import pymysql

# 让脚本可以直接 `python scripts/alter_qty_scale4.py` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402

# 目标列：表名 -> [列名, ...]
TARGET_COLUMNS = {
    'kr_request_item': ['quantity', 'stock_qty'],
    'kr_request_item_lot': ['qty'],
    'kr_wire_coil': ['coil_length'],
    'kr_wire_coil_consumption': [
        'out_length', 'converted_length', 'shear_length',
        'actual_shear_length', 'scrap_length_actual', 'length_tolerance',
    ],
    'kr_inventory_count_item': [
        'original_qty', 'used_mm', 'remain_mm',
        'actual_mm', 'diff_mm', 'diff_converted',
    ],
    'kr_return_item': ['remain_length'],
}

TARGET_SCALE = 4
NUMERIC_DEFAULT_RE = re.compile(r'^-?\d+(\.\d+)?$')


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB,
        charset='utf8mb4',
        autocommit=True,
    )


def read_column(cur, table: str, column: str):
    """返回 dict：column_type / is_nullable / column_default / column_comment"""
    cur.execute(
        """
        SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT, EXTRA
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (Config.MYSQL_DB, table, column),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        'column_type': row[0],
        'is_nullable': row[1],
        'column_default': row[2],
        'column_comment': row[3],
        'extra': row[4],
    }


def parse_precision_scale(column_type: str):
    m = re.match(r'^decimal\((\d+),(\d+)\)$', column_type.lower())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def build_modify_clause(cur, table: str, column: str, info: dict) -> str:
    """按当前定义重建 MODIFY 子句，仅 scale 改成 4。"""
    ps = parse_precision_scale(info['column_type'])
    if ps is None:
        raise RuntimeError(f'{table}.{column} 不是 DECIMAL 类型：{info["column_type"]}')
    precision, _scale = ps

    parts = [f'`{column}` DECIMAL({precision},{TARGET_SCALE})']

    # 空值约束原样保留
    if info['is_nullable'] == 'NO':
        parts.append('NOT NULL')
    else:
        parts.append('NULL')

    # 默认值原样保留
    default = info['column_default']
    if default is not None:
        if NUMERIC_DEFAULT_RE.match(str(default)):
            parts.append(f'DEFAULT {default}')
        elif str(default).upper() in ('CURRENT_TIMESTAMP',) or str(default).upper().startswith('CURRENT_TIMESTAMP('):
            parts.append(f'DEFAULT {default}')
        else:
            parts.append("DEFAULT '%s'" % str(default).replace("'", "''"))
    elif info['is_nullable'] == 'YES':
        parts.append('DEFAULT NULL')

    comment = info['column_comment'] or ''
    if comment:
        comment = comment.replace('保留2位小数', '保留4位小数')
        parts.append("COMMENT '%s'" % comment.replace("'", "''"))

    return ' '.join(parts)


def show_create(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f'SHOW CREATE TABLE `{table}`')
        return cur.fetchone()[1].splitlines()


def diff_lines(before: list[str], after: list[str]) -> list[str]:
    """返回发生变化的行（before / after 各一份），保持顺序。"""
    out = []
    bset, aset = set(before), set(after)
    for ln in before:
        if ln not in aset:
            out.append('  - ' + ln.strip())
    for ln in after:
        if ln not in bset:
            out.append('  + ' + ln.strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只打印将执行的 SQL，不执行')
    ap.add_argument('--diff-only', action='store_true', help='只打印当前 18 列定义')
    args = ap.parse_args()

    conn = connect()
    print(f'数据库：{Config.MYSQL_HOST}:{Config.MYSQL_PORT}/{Config.MYSQL_DB}')
    with conn.cursor() as cur:
        cur.execute('SELECT VERSION()')
        print(f'MySQL 版本：{cur.fetchone()[0]}')

    changed, skipped, missing = [], [], []

    for table, columns in TARGET_COLUMNS.items():
        before = show_create(conn, table) if not args.diff_only else []
        modifies = []
        for column in columns:
            with conn.cursor() as cur:
                info = read_column(cur, table, column)
            if info is None:
                missing.append(f'{table}.{column}')
                continue
            ps = parse_precision_scale(info['column_type'])
            if ps is None:
                missing.append(f'{table}.{column}（非 DECIMAL：{info["column_type"]}）')
                continue
            precision, scale = ps
            if scale == TARGET_SCALE:
                skipped.append(f'{table}.{column} 已是 DECIMAL({precision},{scale})')
                continue
            with conn.cursor() as cur:
                clause = build_modify_clause(cur, table, column, info)
            modifies.append((column, clause, info['column_type']))
            changed.append(f'{table}.{column}: DECIMAL({precision},{scale}) -> DECIMAL({precision},{TARGET_SCALE})')

        if args.diff_only:
            print(f'\n== {table}')
            for column, _clause, ctype in modifies:
                print(f'   {column:<22} {ctype}')
            continue

        if not modifies:
            print(f'\n== {table}: 全部已是 4 位，跳过')
            continue

        sqls = [f'ALTER TABLE `{table}`\n  MODIFY {clause};' for _c, clause, _t in modifies]
        print(f'\n== {table}')
        for s in sqls:
            print('   ' + s.replace('\n', '\n   '))

        if args.dry_run:
            continue

        for s in sqls:
            with conn.cursor() as cur:
                cur.execute(s)

        after = show_create(conn, table)
        print(f'   -- SHOW CREATE TABLE 变化（{table}）')
        for ln in diff_lines(before, after):
            print('   ' + ln)

    print('\n================ 汇总 ================')
    print(f'变更 {len(changed)} 列：')
    for c in changed:
        print('  * ' + c)
    print(f'跳过 {len(skipped)} 列：')
    for s in skipped:
        print('  - ' + s)
    if missing:
        print(f'未找到 {len(missing)} 列：')
        for m in missing:
            print('  ! ' + m)

    # 结果复核
    if not args.dry_run:
        print('\n================ 复核（information_schema）================')
        bad = 0
        with conn.cursor() as cur:
            for table, columns in TARGET_COLUMNS.items():
                for column in columns:
                    info = read_column(cur, table, column)
                    if info is None:
                        print(f'  ! {table}.{column} 不存在')
                        bad += 1
                        continue
                    ps = parse_precision_scale(info['column_type'])
                    flag = 'OK ' if ps and ps[1] == TARGET_SCALE else 'BAD'
                    if flag == 'BAD':
                        bad += 1
                    print(f'  {flag} {table}.{column:<22} {info["column_type"]:<14} '
                          f'NULL={info["is_nullable"]:<3} DEFAULT={info["column_default"]} '
                          f'COMMENT={info["column_comment"]}')
        print(f'\n复核结果：{"全部 4 位" if bad == 0 else f"{bad} 列不符合预期"}')
        conn.close()
        return 0 if bad == 0 else 1

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
