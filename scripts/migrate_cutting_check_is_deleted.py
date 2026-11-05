"""裁线首末检记录表（kr_cutting_check）软删除列 + 查询索引迁移

背景：
  「裁线首末检记录查询」页面需要支持删除，但记录由外部接口（生产端）写入，
  物理删除会丢失检验追溯数据，因此改为软删除：
      is_deleted TINYINT NOT NULL DEFAULT 0   0=正常 1=已删除
  外部查询接口（GET /api/external/cutting-check）同步过滤 is_deleted = 0，
  避免生产端看到已删除记录。

  页面主查询字段为 job_order（工单号模糊匹配），当前 1100+ 行虽小，
  但数据会持续增长，故一并补 idx_job_order 索引。

迁移内容（幂等，可重复执行）：
  1. ALTER TABLE kr_cutting_check ADD COLUMN is_deleted ...
  2. ALTER TABLE kr_cutting_check ADD INDEX idx_job_order (job_order)

运行: python scripts/migrate_cutting_check_is_deleted.py
说明:
  - 先查 information_schema.COLUMNS / SHOW INDEX 判断是否已存在，存在则 SKIP
  - 不 import app 包，避免触发 Flask 应用初始化
"""
import os

import pymysql
from pymysql.cursors import DictCursor

TABLE = 'kr_cutting_check'

NEW_COLUMNS = [
    ('is_deleted',
     "ADD COLUMN `is_deleted` TINYINT NOT NULL DEFAULT 0 "
     "COMMENT '软删除:0正常 1已删除' AFTER `siteref`"),
]

INDEXES = [
    ('idx_job_order', "ADD INDEX `idx_job_order` (`job_order`)"),
]


def db_config():
    """与 app/config.py 保持一致的连接参数（环境变量可覆盖）"""
    return {
        'host': os.environ.get('MYSQL_HOST', '10.0.6.86'),
        'port': int(os.environ.get('MYSQL_PORT', 33306)),
        'user': os.environ.get('MYSQL_USER', 'root'),
        'password': os.environ.get('MYSQL_PASSWORD', 'root07'),
        'database': os.environ.get('MYSQL_DB', 'materialpickup'),
    }


def get_connection():
    return pymysql.connect(
        charset='utf8mb4',
        cursorclass=DictCursor,
        **db_config(),
    )


def table_exists(cursor, table):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,)
    )
    return bool(cursor.fetchone()['n'])


def column_exists(cursor, table, column):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column)
    )
    return bool(cursor.fetchone()['n'])


def index_exists(cursor, table, index_name):
    cursor.execute(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", (index_name,))
    return bool(cursor.fetchall())


def get_columns(cursor, table):
    cursor.execute(f"SHOW FULL COLUMNS FROM `{table}`")
    return {row['Field']: row for row in cursor.fetchall()}


def ensure_new_columns(conn, cursor):
    for name, alter_sql in NEW_COLUMNS:
        if column_exists(cursor, TABLE, name):
            print(f'[SKIP] {TABLE}.{name} 列已存在')
            continue
        cursor.execute(f"ALTER TABLE `{TABLE}` {alter_sql}")
        conn.commit()
        print(f'[OK] {TABLE} 已新增列 {name}')


def ensure_indexes(conn, cursor):
    for name, alter_sql in INDEXES:
        if index_exists(cursor, TABLE, name):
            print(f'[SKIP] {TABLE} 索引 {name} 已存在')
            continue
        cursor.execute(f"ALTER TABLE `{TABLE}` {alter_sql}")
        conn.commit()
        print(f'[OK] {TABLE} 已新增索引 {name}')


def verify(cursor):
    print(f'\n=== {TABLE} 迁移后核对 ===')
    columns = get_columns(cursor, TABLE)
    for name, _ in NEW_COLUMNS:
        row = columns.get(name) or {}
        print(f'  {name}: {row.get("Type")} '
              f'{"NULL" if row.get("Null") == "YES" else "NOT NULL"} '
              f'default={row.get("Default")} comment={row.get("Comment")}')
    missing = [n for n, _ in NEW_COLUMNS if n not in columns]
    print(f'  新增列齐全: {"是" if not missing else "否 缺 " + ", ".join(missing)}')

    for name, _ in INDEXES:
        print(f'  索引 {name}: {"存在" if index_exists(cursor, TABLE, name) else "缺失"}')

    cursor.execute(f"SELECT COUNT(*) AS total, "
                   f"SUM(`is_deleted` = 0) AS alive, "
                   f"SUM(`is_deleted` = 1) AS deleted FROM `{TABLE}`")
    print(f'  当前行数: {cursor.fetchone()}')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== kr_cutting_check 软删除列迁移开始（materialpickup） ===')
        if not table_exists(cursor, TABLE):
            print(f'[ERROR] {TABLE} 表不存在，请先建表')
            return 1
        ensure_new_columns(conn, cursor)
        ensure_indexes(conn, cursor)
        verify(cursor)
        print('\n=== 迁移完成（幂等：仅补列/补索引，未改任何已有数据） ===')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
