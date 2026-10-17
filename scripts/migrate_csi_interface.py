"""
CSI 库位转移接口表（kr_csi_interface）结构迁移

背景：物料领取看板「完成备料」后，需把库位转移数据（sl-qty-move）写入
kr_csi_interface，由常驻同步器每分钟提交到宝鼎 Syteline（Infor CSI）。
本脚本把已有接口表修正为可写入的最终结构（幂等，可重复执行）。

修正内容：
  1. 列类型修正（原表按宝鼎旧接口建表，类型不符会写入失败/被截断）
       item    INT          -> VARCHAR(128)  CSI 物料号为字符串（A080871 / 0010-16881-Celestica）
       traceid INT          -> VARCHAR(128)  API 要求字符串，≤128
       lot     VARCHAR(20)  -> VARCHAR(64)   与 kr_wire_coil.lot_no / kr_request_item_lot.lot_no 一致
       job     VARCHAR(10)  -> VARCHAR(20)   工单号可能带 -0001 后缀
  2. 新增同步器状态机所需列
       request_id / item_id / coil_id / lot_id（回溯源单，重试换号用）
       retry_count / submitted_at / last_poll_at（重试与轮询状态）
  3. 索引
       idx_sync(is_sync) / idx_status(status) / idx_request(request_id)
       uk_row(request_id, coil_id, lot_id, api_name) 防重复入队
       （MySQL 唯一键允许多个 NULL，A/B 卷标行 lot_id 为 NULL、批次行 coil_id 为 NULL，
         应用层另有「先查后插」幂等兜底）

运行: python scripts/migrate_csi_interface.py
说明:
  - 表不存在时按最终结构创建（CREATE TABLE IF NOT EXISTS），已存在则只做幂等 ALTER
  - 数据库连接参数默认值与 app/config.py 一致，可被环境变量覆盖；
    不 import app 包，避免触发 Flask 应用初始化
"""
import os

import pymysql
from pymysql.cursors import DictCursor

TABLE = 'kr_csi_interface'

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `request_id` INT NULL COMMENT '关联 kr_material_request.id',
  `item_id` INT NULL COMMENT '关联 kr_request_item.id',
  `coil_id` VARCHAR(32) NULL COMMENT '卷标号（A/B 料行）；非 A/B 料为 NULL',
  `lot_id` BIGINT NULL COMMENT 'kr_request_item_lot.id（非 A/B 料行）；A/B 料为 NULL',
  `api_name` VARCHAR(255) DEFAULT NULL,
  `siteRef` INT DEFAULT NULL,
  `item` VARCHAR(128) DEFAULT NULL COMMENT 'CSI 物料号（字符串）',
  `lot` VARCHAR(64) DEFAULT NULL,
  `quantity` DECIMAL(10,4) DEFAULT NULL,
  `job` VARCHAR(20) DEFAULT NULL,
  `warehouse` VARCHAR(10) DEFAULT NULL,
  `from_location` VARCHAR(255) DEFAULT NULL,
  `to_location` VARCHAR(255) DEFAULT NULL,
  `reasoncode` VARCHAR(255) DEFAULT NULL,
  `account` VARCHAR(255) DEFAULT NULL,
  `unitcode1` VARCHAR(10) DEFAULT NULL,
  `unitcode2` VARCHAR(10) DEFAULT NULL,
  `documentnumber` VARCHAR(12) DEFAULT NULL,
  `is_sync` INT DEFAULT NULL,
  `status` VARCHAR(255) DEFAULT NULL,
  `retry_count` INT NOT NULL DEFAULT 0,
  `submitted_at` DATETIME NULL,
  `last_poll_at` DATETIME NULL,
  `traceid` VARCHAR(128) DEFAULT NULL COMMENT '请求追踪号（字符串）',
  `errormessage` VARCHAR(255) DEFAULT NULL,
  `createdate` DATETIME DEFAULT NULL,
  `createby` VARCHAR(255) DEFAULT NULL,
  `updatedate` DATETIME DEFAULT NULL,
  `updateby` VARCHAR(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_sync` (`is_sync`),
  KEY `idx_status` (`status`),
  KEY `idx_request` (`request_id`),
  UNIQUE KEY `uk_row` (`request_id`, `coil_id`, `lot_id`, `api_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# (列名, 期望类型, MODIFY 语句)
TYPE_FIXES = [
    ('item', 'varchar(128)',
     "MODIFY COLUMN `item` VARCHAR(128) NULL COMMENT 'CSI 物料号（字符串，如 A080871）'"),
    ('traceid', 'varchar(128)',
     "MODIFY COLUMN `traceid` VARCHAR(128) NULL COMMENT '请求追踪号（字符串≤128）'"),
    ('lot', 'varchar(64)',
     "MODIFY COLUMN `lot` VARCHAR(64) NULL COMMENT '批号/卷批号'"),
    ('job', 'varchar(20)',
     "MODIFY COLUMN `job` VARCHAR(20) NULL COMMENT '工单号（可能带 -0001 后缀）'"),
]

# (列名, ADD 语句)
NEW_COLUMNS = [
    ('request_id', "ADD COLUMN `request_id` INT NULL COMMENT '关联 kr_material_request.id' AFTER `id`"),
    ('item_id', "ADD COLUMN `item_id` INT NULL COMMENT '关联 kr_request_item.id' AFTER `request_id`"),
    ('coil_id', "ADD COLUMN `coil_id` VARCHAR(32) NULL COMMENT '卷标号（A/B 料行）；非 A/B 料为 NULL' AFTER `item_id`"),
    ('lot_id', "ADD COLUMN `lot_id` BIGINT NULL COMMENT 'kr_request_item_lot.id（非 A/B 料行）；A/B 料为 NULL' AFTER `coil_id`"),
    ('retry_count', "ADD COLUMN `retry_count` INT NOT NULL DEFAULT 0 COMMENT '已重试次数' AFTER `status`"),
    ('submitted_at', "ADD COLUMN `submitted_at` DATETIME NULL COMMENT '最近提交时间' AFTER `retry_count`"),
    ('last_poll_at', "ADD COLUMN `last_poll_at` DATETIME NULL COMMENT '最近轮询时间' AFTER `submitted_at`"),
]

# (索引名, ADD 语句)
INDEXES = [
    ('idx_sync', "ADD INDEX `idx_sync` (`is_sync`)"),
    ('idx_status', "ADD INDEX `idx_status` (`status`)"),
    ('idx_request', "ADD INDEX `idx_request` (`request_id`)"),
    ('uk_row', "ADD UNIQUE KEY `uk_row` (`request_id`, `coil_id`, `lot_id`, `api_name`)"),
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


def get_columns(cursor):
    """返回 {列名: SHOW COLUMNS 行}"""
    cursor.execute(f"SHOW COLUMNS FROM `{TABLE}`")
    return {row['Field']: row for row in cursor.fetchall()}


def get_index_names(cursor):
    cursor.execute(f"SHOW INDEX FROM `{TABLE}`")
    return {row['Key_name'] for row in cursor.fetchall()}


def ensure_table(conn, cursor):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (TABLE,)
    )
    if cursor.fetchone()['n']:
        print(f'[SKIP] {TABLE} 表已存在')
        return
    cursor.execute(CREATE_SQL)
    conn.commit()
    print(f'[OK] {TABLE} 表不存在，已按最终结构创建')


def fix_column_types(conn, cursor):
    """幂等修正列类型（已是目标类型则 SKIP）"""
    columns = get_columns(cursor)
    for name, expected, alter_sql in TYPE_FIXES:
        row = columns.get(name)
        if not row:
            print(f'[WARN] {TABLE}.{name} 列不存在，跳过类型修正（请检查表结构）')
            continue
        current = (row['Type'] or '').lower()
        if current == expected:
            print(f'[SKIP] {TABLE}.{name} 已是 {current}')
            continue
        cursor.execute(f"ALTER TABLE `{TABLE}` {alter_sql}")
        conn.commit()
        print(f'[OK] {TABLE}.{name}: {current} -> {expected}')


def ensure_new_columns(conn, cursor):
    columns = get_columns(cursor)
    for name, alter_sql in NEW_COLUMNS:
        if name in columns:
            print(f'[SKIP] {TABLE}.{name} 列已存在')
            continue
        cursor.execute(f"ALTER TABLE `{TABLE}` {alter_sql}")
        conn.commit()
        print(f'[OK] {TABLE} 已新增列 {name}')


def ensure_indexes(conn, cursor):
    names = get_index_names(cursor)
    for name, alter_sql in INDEXES:
        if name in names:
            print(f'[SKIP] {TABLE} 索引/唯一键 {name} 已存在')
            continue
        cursor.execute(f"ALTER TABLE `{TABLE}` {alter_sql}")
        conn.commit()
        print(f'[OK] {TABLE} 已新增索引/唯一键 {name}')


def verify(cursor):
    """执行后核对：关键列类型 + 列/索引清单"""
    columns = get_columns(cursor)
    print('\n=== 迁移后核对 ===')
    for name in ('item', 'traceid', 'lot', 'job'):
        print(f'  {name}: {(columns.get(name) or {}).get("Type")}')
    missing = [n for n, _ in NEW_COLUMNS if n not in columns]
    print(f'  新增列齐全: {"是" if not missing else "否 缺 " + ", ".join(missing)}')
    indexes = sorted(get_index_names(cursor))
    print(f'  索引: {", ".join(indexes)}')
    cursor.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}`")
    print(f'  当前行数: {cursor.fetchone()["n"]}')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print(f'=== {TABLE} 迁移开始（materialpickup） ===')
        ensure_table(conn, cursor)
        fix_column_types(conn, cursor)
        ensure_new_columns(conn, cursor)
        ensure_indexes(conn, cursor)
        verify(cursor)
        print('=== 迁移完成（幂等：仅补列/改类型/建索引，未删任何列或数据） ===')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
