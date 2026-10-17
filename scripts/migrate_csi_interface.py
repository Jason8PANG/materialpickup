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
  3. 计量单位列
       um VARCHAR(16)（Unit of Measure）
       注意：unitcode1 / unitcode2 是 account 的 unit code，不是计量单位，
       因此单位写 um，unitcode1/2 保持 NULL
  4. 卷标 / 批次记录 Lot 所在库位
       kr_wire_coil.loc / kr_request_item_lot.loc VARCHAR(128)
       卷标维护、批次维护时自动带出并持久化，完成备料写接口表时作为 from_location
  5. 索引
       idx_sync(is_sync) / idx_status(status) / idx_request(request_id)
       idx_row(request_id, coil_id, lot_id, api_name) 普通索引（原 uk_row 唯一键已废弃）
       原因：uk_row 唯一键会阻止「完成备料 → 回到备料 → 再次完成备料」写入第二条
       同 (request_id, coil_id, lot_id) 的正向记录。去重改由应用层按 status 判断
       （仅 PENDING/QUEUED/SUBMITTING/SUCCESS 生效，REVERSED/CANCELLED/FAILED_MANUAL 不阻止）

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
  `um` VARCHAR(16) DEFAULT NULL COMMENT '计量单位(Unit of Measure)',
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
  KEY `idx_row` (`request_id`, `coil_id`, `lot_id`, `api_name`)
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

# (列名, ADD 语句) —— kr_csi_interface
NEW_COLUMNS = [
    ('request_id', "ADD COLUMN `request_id` INT NULL COMMENT '关联 kr_material_request.id' AFTER `id`"),
    ('item_id', "ADD COLUMN `item_id` INT NULL COMMENT '关联 kr_request_item.id' AFTER `request_id`"),
    ('coil_id', "ADD COLUMN `coil_id` VARCHAR(32) NULL COMMENT '卷标号（A/B 料行）；非 A/B 料为 NULL' AFTER `item_id`"),
    ('lot_id', "ADD COLUMN `lot_id` BIGINT NULL COMMENT 'kr_request_item_lot.id（非 A/B 料行）；A/B 料为 NULL' AFTER `coil_id`"),
    ('um', "ADD COLUMN `um` VARCHAR(16) NULL COMMENT '计量单位(Unit of Measure)' AFTER `quantity`"),
    ('retry_count', "ADD COLUMN `retry_count` INT NOT NULL DEFAULT 0 COMMENT '已重试次数' AFTER `status`"),
    ('submitted_at', "ADD COLUMN `submitted_at` DATETIME NULL COMMENT '最近提交时间' AFTER `retry_count`"),
    ('last_poll_at', "ADD COLUMN `last_poll_at` DATETIME NULL COMMENT '最近轮询时间' AFTER `submitted_at`"),
]

# (索引名, ADD 语句)
INDEXES = [
    ('idx_sync', "ADD INDEX `idx_sync` (`is_sync`)"),
    ('idx_status', "ADD INDEX `idx_status` (`status`)"),
    ('idx_request', "ADD INDEX `idx_request` (`request_id`)"),
    ('idx_row', "ADD INDEX `idx_row` (`request_id`, `coil_id`, `lot_id`, `api_name`)"),
]

# 历史唯一键：阻挡同一 (request_id, coil_id, lot_id, api_name) 的第二条正向记录，
# 迁移时删除并改为普通索引 idx_row（去重交给应用层按 status 判断）
LEGACY_UNIQUE_KEY = 'uk_row'

# Lot 所在库位列（卷标维护 / 批次维护时自动带出并持久化）
LOC_COLUMN = "ADD COLUMN `loc` VARCHAR(128) NULL COMMENT 'Lot所在库位(CSI)'"

# 迁移目标：(表名, CREATE 语句, 类型修正, 新增列, 索引, 核对的列)
SPECS = [
    {
        'table': TABLE,
        'create_sql': CREATE_SQL,
        'type_fixes': TYPE_FIXES,
        'new_columns': NEW_COLUMNS,
        'indexes': INDEXES,
        'verify_columns': ('item', 'traceid', 'lot', 'job', 'um'),
    },
    {
        'table': 'kr_wire_coil',
        'create_sql': None,
        'type_fixes': [],
        'new_columns': [('loc', f"{LOC_COLUMN} AFTER `unit`")],
        'indexes': [],
        'verify_columns': ('loc',),
    },
    {
        'table': 'kr_request_item_lot',
        'create_sql': None,
        'type_fixes': [],
        'new_columns': [('loc', f"{LOC_COLUMN} AFTER `qty`")],
        'indexes': [],
        'verify_columns': ('loc',),
    },
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


def get_columns(cursor, table):
    """返回 {列名: SHOW COLUMNS 行}"""
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return {row['Field']: row for row in cursor.fetchall()}


def get_index_names(cursor, table):
    cursor.execute(f"SHOW INDEX FROM `{table}`")
    return {row['Key_name'] for row in cursor.fetchall()}


def table_exists(cursor, table):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,)
    )
    return bool(cursor.fetchone()['n'])


def ensure_table(conn, cursor, spec):
    """仅 kr_csi_interface 会在缺失时建表；业务表缺失只提示不创建"""
    table = spec['table']
    if table_exists(cursor, table):
        print(f'[SKIP] {table} 表已存在')
        return True
    if not spec.get('create_sql'):
        print(f'[WARN] {table} 表不存在，跳过（请先执行该表的建表迁移）')
        return False
    cursor.execute(spec['create_sql'])
    conn.commit()
    print(f'[OK] {table} 表不存在，已按最终结构创建')
    return True


def fix_column_types(conn, cursor, spec):
    """幂等修正列类型（已是目标类型则 SKIP）"""
    table = spec['table']
    columns = get_columns(cursor, table)
    for name, expected, alter_sql in spec['type_fixes']:
        row = columns.get(name)
        if not row:
            print(f'[WARN] {table}.{name} 列不存在，跳过类型修正（请检查表结构）')
            continue
        current = (row['Type'] or '').lower()
        if current == expected:
            print(f'[SKIP] {table}.{name} 已是 {current}')
            continue
        cursor.execute(f"ALTER TABLE `{table}` {alter_sql}")
        conn.commit()
        print(f'[OK] {table}.{name}: {current} -> {expected}')


def ensure_new_columns(conn, cursor, spec):
    table = spec['table']
    columns = get_columns(cursor, table)
    for name, alter_sql in spec['new_columns']:
        if name in columns:
            print(f'[SKIP] {table}.{name} 列已存在')
            continue
        cursor.execute(f"ALTER TABLE `{table}` {alter_sql}")
        conn.commit()
        print(f'[OK] {table} 已新增列 {name}')


def ensure_indexes(conn, cursor, spec):
    table = spec['table']
    names = get_index_names(cursor, table)
    for name, alter_sql in spec['indexes']:
        if name in names:
            print(f'[SKIP] {table} 索引/唯一键 {name} 已存在')
            continue
        cursor.execute(f"ALTER TABLE `{table}` {alter_sql}")
        conn.commit()
        print(f'[OK] {table} 已新增索引/唯一键 {name}')


def drop_legacy_unique_row(conn, cursor, spec):
    """删除历史唯一键 uk_row（改为普通索引 idx_row）。

    uk_row 会阻止「完成备料 → 回到备料 → 再次完成备料」写入第二条同
    (request_id, coil_id, lot_id, api_name) 的正向记录（1062 Duplicate entry）。
    幂等：不存在 / 已非唯一键则 SKIP。
    """
    table = spec['table']
    if not spec['indexes']:
        return
    cursor.execute(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", (LEGACY_UNIQUE_KEY,))
    rows = list(cursor.fetchall())
    if not rows:
        print(f'[SKIP] {table} 无 {LEGACY_UNIQUE_KEY} 唯一键')
        return
    if all(int(r.get('Non_unique') or 0) == 1 for r in rows):
        print(f'[SKIP] {table}.{LEGACY_UNIQUE_KEY} 已非唯一键')
        return
    cursor.execute(f"ALTER TABLE `{table}` DROP INDEX `{LEGACY_UNIQUE_KEY}`")
    conn.commit()
    print(f'[OK] {table} 已删除唯一键 {LEGACY_UNIQUE_KEY}（改普通索引 idx_row，去重由应用层负责）')


def verify(cursor, spec):
    """执行后核对：关键列类型 + 新增列清单 + 行数"""
    table = spec['table']
    columns = get_columns(cursor, table)
    print(f'\n=== {table} 迁移后核对 ===')
    for name in spec['verify_columns']:
        row = columns.get(name) or {}
        print(f'  {name}: {row.get("Type")} {("NULL" if row.get("Null") == "YES" else "NOT NULL")}')
    missing = [n for n, _ in spec['new_columns'] if n not in columns]
    print(f'  新增列齐全: {"是" if not missing else "否 缺 " + ", ".join(missing)}')
    if spec['indexes']:
        indexes = sorted(get_index_names(cursor, table))
        print(f'  索引: {", ".join(indexes)}')
    cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
    print(f'  当前行数: {cursor.fetchone()["n"]}')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== CSI 接口表 / 卷标 / 批次 结构迁移开始（materialpickup） ===')
        for spec in SPECS:
            table = spec['table']
            print(f'\n--- {table} ---')
            if not ensure_table(conn, cursor, spec):
                continue
            fix_column_types(conn, cursor, spec)
            ensure_new_columns(conn, cursor, spec)
            drop_legacy_unique_row(conn, cursor, spec)
            ensure_indexes(conn, cursor, spec)
            verify(cursor, spec)
        print('\n=== 迁移完成（幂等：仅补列/改类型/建索引，未删任何列或数据） ===')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
