"""
申请单明细行批次分配子表迁移脚本（非 A/B 料按行维护多条「批号 + 数量」）

背景：申请单详情页明细行按物料类型分流维护——
  - A/B 开头物料：只维护卷标（FIFO 仅提示应发批号，仍落库 kr_request_item.batch_no）
  - 其他物料：禁止录卷标，改为按行维护多条「批号 + 数量」

本脚本新建 kr_request_item_lot（幂等，可重复执行；只建表，不改动/删除任何既有列与数据）:
  id           BIGINT 自增主键
  request_id   申请单主表 id
  item_id      申请单明细行 id（kr_request_item.id）
  lot_no       批号
  qty          数量
  siteref      站点（310-苏州 / 410-槟城），从单据带出
  created_at / updated_at 时间戳

运行: python scripts/migrate_request_item_lot.py
说明:
  - 数据库连接参数默认值与 app/config.py 一致（10.0.6.86:33306 materialpickup root/root07），
    可被环境变量覆盖；不 import app 包，避免触发 Flask 应用初始化。
"""
import os

import pymysql
from pymysql.cursors import DictCursor

TABLE = 'kr_request_item_lot'

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  request_id INT NOT NULL,
  item_id INT NOT NULL,
  lot_no VARCHAR(64) NOT NULL,
  qty DECIMAL(12,4) NOT NULL,
  siteref VARCHAR(16) DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_item (item_id),
  INDEX idx_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='申请单行批次分配（非A/B料）'
"""


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


def table_exists(cursor):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (TABLE,)
    )
    return cursor.fetchone()['n'] > 0


def ensure_columns(conn, cursor):
    """幂等补齐关键列/索引（表已存在但结构落后时），仅补充缺失项"""
    cursor.execute(f"SHOW COLUMNS FROM `{TABLE}`")
    cols = {row['Field'] for row in cursor.fetchall()}
    alters = []
    if 'siteref' not in cols:
        alters.append("ADD COLUMN siteref VARCHAR(16) DEFAULT NULL")
    if 'updated_at' not in cols:
        alters.append(
            "ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP")
    if alters:
        cursor.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(alters))
        conn.commit()
        print(f'[OK] {TABLE} 已补齐列: {", ".join(alters)}')

    cursor.execute(f"SHOW INDEX FROM `{TABLE}`")
    idx = {row['Key_name'] for row in cursor.fetchall()}
    idx_alters = []
    if 'idx_item' not in idx:
        idx_alters.append("ADD INDEX idx_item (item_id)")
    if 'idx_request' not in idx:
        idx_alters.append("ADD INDEX idx_request (request_id)")
    if idx_alters:
        cursor.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(idx_alters))
        conn.commit()
        print(f'[OK] {TABLE} 已补齐索引: {", ".join(idx_alters)}')


def migrate(conn, cursor):
    existed = table_exists(cursor)
    cursor.execute(CREATE_SQL)
    conn.commit()
    if existed:
        print(f'[SKIP] {TABLE} 表已存在（结构校验中）')
        ensure_columns(conn, cursor)
    else:
        print(f'[OK] {TABLE} 表已创建（含 idx_item / idx_request）')

    cursor.execute(f"SHOW CREATE TABLE `{TABLE}`")
    print(cursor.fetchone()['Create Table'])
    cursor.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}`")
    print(f'{TABLE} 当前行数: {cursor.fetchone()["n"]}')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== 申请单行批次子表迁移开始（materialpickup） ===')
        migrate(conn, cursor)
        print('=== 迁移完成（仅建表/补列，未删除任何列与数据） ===')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
