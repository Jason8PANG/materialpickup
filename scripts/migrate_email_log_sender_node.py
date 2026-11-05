"""kr_email_log 增加 sender_node 列迁移（发信节点标识）

背景：
  生产环境曾出现「告警邮件发出去了，但在任何实例的 kr_email_log 里都查不到」
  的情况（邮件来自旧版本代码 / 被邮件服务器重投）。为便于事后按节点定位
  「这封邮件到底是谁发的」，在邮件服务里给每封外发邮件打上发信节点标识
  （hostname/pid），同时给邮件日志表补一列，便于直接按节点筛选。

迁移内容（幂等，可重复执行）：
  ALTER TABLE kr_email_log ADD COLUMN sender_node VARCHAR(128) NULL
      COMMENT '发信节点: hostname/pid' AFTER `errormessage`

运行: python scripts/migrate_email_log_sender_node.py
说明:
  - 先查 information_schema.COLUMNS 判断是否已存在，存在则 SKIP
  - 不 import app 包，避免触发 Flask 应用初始化
"""
import os

import pymysql
from pymysql.cursors import DictCursor

TABLE = 'kr_email_log'

NEW_COLUMNS = [
    ('sender_node',
     "ADD COLUMN `sender_node` VARCHAR(128) NULL "
     "COMMENT '发信节点: hostname/pid' AFTER `errormessage`"),
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

    cursor.execute(f"SELECT COUNT(*) AS total, "
                   f"SUM(`sender_node` IS NOT NULL) AS with_node FROM `{TABLE}`")
    print(f'  当前行数: {cursor.fetchone()}')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== kr_email_log sender_node 列迁移开始（materialpickup） ===')
        if not table_exists(cursor, TABLE):
            print(f'[ERROR] {TABLE} 表不存在，请先建表')
            return 1
        ensure_new_columns(conn, cursor)
        verify(cursor)
        print('\n=== 迁移完成（幂等：仅补列，未改任何已有数据） ===')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
