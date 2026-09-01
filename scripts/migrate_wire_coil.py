"""
线卷全库存管理 - 数据库增量迁移脚本

新增两张表（幂等，CREATE TABLE IF NOT EXISTS，不破坏现有数据/现有表结构）：
  1. kr_wire_coil              线卷库存表（每卷线一行）
  2. kr_wire_coil_consumption  线卷消耗记录表（每卷每次出库/报废一行，宽表存储线材加工过程参数）

kr_wire_coil_consumption 结构变更（文档 2.3，v1.0 宽表升级）：
  - 删除 request_id 列（追溯通过 coil_id → kr_wire_coil.request_id 上查）
  - 新增 converted_length / converted_unit（单位换算，服务端计算）
  - 新增线材基础 / A端去皮 / B端去皮 / 打端 / A端端子预加工 / B端端子预加工 六组宽表字段

kr_wire_coil 结构变更（迭代需求：卷标按申请单行维护）：
  - 新增 item_id 列（关联 kr_request_item.id），卷标录入时记录申请单行，支持按行查询

迁移策略：
  - 表不存在时直接 CREATE TABLE IF NOT EXISTS（新宽表结构）；
  - 表已存在旧结构时，通过 information_schema 检测缺失列，ALTER TABLE ADD COLUMN 补齐，
    并删除残留的 request_id 列及其索引（幂等，可重复执行）。
  - 迭代中已废弃的加工参数列（wire_spec/color/首末件确认人/去皮A/B端组，共17列）
    已于 2026-08-29 从数据库物理删除，本脚本不再维护其清理逻辑。

运行: python scripts/migrate_wire_coil.py
说明:
  - 独立于 app/init_db.py，不修改其现有 DROP/CREATE 逻辑。
  - 数据库连接参数默认值与 app/config.py 一致，可被环境变量覆盖；
    不 import app 包，避免触发 Flask 应用初始化。
"""
import os
import sys

import pymysql
from pymysql.cursors import DictCursor


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


# ====== 线卷库存表（每卷线一行） ======
CREATE_WIRE_COIL = """
CREATE TABLE IF NOT EXISTS kr_wire_coil (
  id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  coil_id         VARCHAR(16)   NOT NULL COMMENT '卷标ID：YYMMDD+3位流水号，共9位数字，如260814001，全局唯一',
  part_number     VARCHAR(128)  NOT NULL COMMENT '物料 Part Number',
  coil_length     DECIMAL(12,2) NOT NULL COMMENT '长度（数值，保留2位小数）',
  unit            VARCHAR(16)   DEFAULT NULL COMMENT '单位，自动取自CSI物料单位字段（只读回填，如M/FT/EA）',
  status          VARCHAR(20)   NOT NULL DEFAULT 'in_stock' COMMENT '状态：in_stock在库 / issued已出库 / scrapped报废',
  request_id      INT           NOT NULL COMMENT '来源申请单ID（逻辑关联 kr_material_request.id）',
  item_id         BIGINT        DEFAULT NULL COMMENT '申请单行ID（关联 kr_request_item.id，按行维护卷标）',
  siteref         VARCHAR(16)   NOT NULL COMMENT '站点：310-苏州 / 410-槟城（冗余自申请单）',
  operator        VARCHAR(64)   NOT NULL COMMENT '录入操作人工号',
  remark          VARCHAR(256)  DEFAULT NULL COMMENT '备注',
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_coil_id (coil_id),
  INDEX idx_part_number (part_number),
  INDEX idx_status (status),
  INDEX idx_request_id (request_id),
  INDEX idx_item_id (item_id),
  INDEX idx_siteref (siteref),
  INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线卷库存表（每卷线一行）'
"""

# ====== 线卷消耗记录表宽表列（文档 2.3） ======
# 单位换算组 + 线材基础组（去皮A/B端、打端、端子预加工等加工参数组已于迭代中移除）。
# 该列表同时用于：
#   1) 新表 CREATE 时的建表列（由 _build_consumption_ddl 拼接）；
#   2) 旧表升级时的 ALTER TABLE ADD COLUMN（缺失列补建）。
CONSUMPTION_WIDE_COLUMNS = [
    # ---- 单位换算三元组（服务端计算，前端不传） ----
    ("converted_length", "DECIMAL(12,2)   DEFAULT NULL COMMENT '转换后长度（mm 按系数换算为 CSI 单位，服务端计算）'"),
    ("converted_unit",   "VARCHAR(16)     DEFAULT NULL COMMENT '转换后单位（即 CSI 单位）'"),
    # ---- 基础/线材组 ----
    ("job_part_number",        "VARCHAR(64)     DEFAULT NULL COMMENT '工单物料号'"),
    ("shear_qty",              "INT             DEFAULT NULL COMMENT '剪切数量'"),
    ("shear_length",           "DECIMAL(12,2)   DEFAULT NULL COMMENT '剪切长度'"),
    ("actual_shear_length",    "DECIMAL(12,2)   DEFAULT NULL COMMENT '实际剪切长度'"),
    ("length_tolerance",       "DECIMAL(10,2)   DEFAULT NULL COMMENT '长度公差'"),
    ("shear_equipment",        "VARCHAR(64)     DEFAULT NULL COMMENT '剪切设备'"),
    ("shear_device_no",        "VARCHAR(64)     DEFAULT NULL COMMENT '剪切设备编号'"),
    ("actual_shear_equipment", "VARCHAR(64)     DEFAULT NULL COMMENT '实际剪切设备'"),
    # ---- 外部集成 / 状态组（naiwiptrack 消耗登记） ----
    ("scrap_length_actual",    "DECIMAL(12,2)   DEFAULT NULL COMMENT '实际报废长度'"),
    ("stage",                  "VARCHAR(16)     DEFAULT 'first' COMMENT '阶段：first/last/complete'"),
    ("is_manual",              "TINYINT(1)      DEFAULT 0 COMMENT '是否手动录入'"),
    ("checker",                "VARCHAR(64)     DEFAULT NULL COMMENT '确认人'"),
]

# 历史废弃列（wire_spec/color/首末件确认人 5 列 + 去皮A/B端组 12 列，共 17 列）
# 已于 2026-08-29 从数据库物理删除，清理清单已随之移除（见 git 提交记录），
# 不再需要 DROP COLUMN 清理逻辑。

# 消耗记录表固定基础列（含索引/主键），宽表列由 CONSUMPTION_WIDE_COLUMNS 动态拼接
CONSUMPTION_BASE_COLUMNS = """
  id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  coil_id         VARCHAR(16)   NOT NULL COMMENT '卷标ID（逻辑关联 kr_wire_coil.coil_id）',
  job_order       VARCHAR(64)   DEFAULT NULL COMMENT '消耗/领取的工单号（minpack申请单可空）',
  part_number     VARCHAR(128)  NOT NULL COMMENT '物料 Part Number（冗余自线卷表，便于独立查询）',
  consume_type    VARCHAR(20)   NOT NULL DEFAULT 'issue' COMMENT '消耗类型：issue出库 / scrap报废（报废登记后续迭代）',
  out_length      DECIMAL(12,2) NOT NULL COMMENT '消耗/出库长度（原始录入值，单位固定 mm）',
  unit            VARCHAR(16)   DEFAULT NULL COMMENT 'CSI 物料单位（冗余自线卷表，作为单位换算的源单位，如 M/FT）',
"""

CONSUMPTION_TAIL_COLUMNS = """
  operator        VARCHAR(64)   NOT NULL COMMENT '登记操作人工号',
  remark          VARCHAR(256)  DEFAULT NULL COMMENT '备注',
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '登记时间',
  updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  INDEX idx_coil_id (coil_id),
  INDEX idx_job_order (job_order),
  INDEX idx_consume_type (consume_type),
  INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线卷消耗记录表（每卷每次出库/报废一行，宽表存储线材加工过程参数）'
"""


def _build_consumption_ddl() -> str:
    """由基础列 + 宽表列拼接生成完整 CREATE TABLE DDL（文档 2.3 新宽表结构）"""
    wide = ",\n".join(f"  {name:<22} {ddl}" for name, ddl in CONSUMPTION_WIDE_COLUMNS)
    base = CONSUMPTION_BASE_COLUMNS.rstrip("\n").rstrip(",")
    tail = CONSUMPTION_TAIL_COLUMNS.strip("\n")
    # 注意：tail 已包含闭合的 ")" 和 ENGINE 子句，结尾不再追加 ")"
    return "CREATE TABLE IF NOT EXISTS kr_wire_coil_consumption (\n" + \
        base + ",\n" + wide + ",\n" + tail + "\n"


CREATE_WIRE_COIL_CONSUMPTION = _build_consumption_ddl()


def _ensure_consumption_structure(conn, cursor):
    """
    对齐 kr_wire_coil_consumption 表结构与文档 2.3 宽表设计（幂等，可重复执行）：
      1. 表不存在 → CREATE 已建新结构，无需处理；
      2. 表存在但为旧结构（缺宽表列 / 含 request_id）→ ALTER TABLE 补齐/删除。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil_consumption'"
    )
    if not cursor.fetchone()['n']:
        return

    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil_consumption'"
    )
    existing_cols = {r['COLUMN_NAME'] for r in cursor.fetchall()}
    cursor.execute(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil_consumption' "
        "GROUP BY INDEX_NAME"
    )
    existing_idxs = {r['INDEX_NAME'] for r in cursor.fetchall()}

    alters = []
    # 1. 补齐缺失的宽表列
    for name, ddl in CONSUMPTION_WIDE_COLUMNS:
        if name not in existing_cols:
            alters.append(f"ADD COLUMN {name} {ddl}")
    # 2. 删除旧结构残留的 request_id 列（及其单列索引）
    if 'request_id' in existing_cols:
        if 'idx_request_id' in existing_idxs:
            alters.append("DROP INDEX idx_request_id")
        alters.append("DROP COLUMN request_id")
    # 注：迭代中已废弃的加工参数列（wire_spec/color/首末件确认人/去皮A/B端组，共17列）
    #     已于 2026-08-29 从数据库物理删除，清理清单及删除逻辑已移除。
    # 3. 补齐索引
    for idx in ('idx_coil_id', 'idx_job_order', 'idx_consume_type', 'idx_created_at'):
        if idx not in existing_idxs:
            alters.append(f"ADD INDEX {idx} ({idx[4:]})")

    if alters:
        cursor.execute("ALTER TABLE kr_wire_coil_consumption " + ", ".join(alters))
        conn.commit()
        print('[OK] kr_wire_coil_consumption 已升级为宽表结构（补齐宽表列 / 删除 request_id）')


def _ensure_wire_coil_item_id(conn, cursor):
    """
    kr_wire_coil 按行维护（迭代需求）：补齐 item_id 列（幂等，可重复执行）。
    item_id 关联 kr_request_item.id，用于按申请单行绑定卷标。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil' "
        "AND COLUMN_NAME = 'item_id'"
    )
    if cursor.fetchone()['n']:
        return

    alters = [
        "ADD COLUMN item_id BIGINT DEFAULT NULL COMMENT '申请单行ID（关联 kr_request_item.id，按行维护卷标）' AFTER request_id",
        "ADD INDEX idx_item_id (item_id)",
    ]
    cursor.execute("ALTER TABLE kr_wire_coil " + ", ".join(alters))
    conn.commit()
    print('[OK] kr_wire_coil 已补充 item_id 列（按申请单行维护卷标）')


def _ensure_wire_coil_is_deleted(conn, cursor):
    """
    kr_wire_coil 补齐 is_deleted 列（幂等）：删除改为软删除（标记），
    已参与消耗/退料/打印的卷标不可物理删除，可恢复。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil' "
        "AND COLUMN_NAME = 'is_deleted'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_wire_coil ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 "
        "COMMENT '软删除标记：1=已删除（可恢复）' AFTER item_id"
    )
    conn.commit()
    print('[OK] kr_wire_coil 已补充 is_deleted 列（软删除）')


def _ensure_wire_coil_prev_bind(conn, cursor):
    """
    kr_wire_coil 补齐 prev_request_id/prev_item_id 列（幂等）：
    在库卷标被选用（use-stock）绑定到其他申请单时，备份原归属；
    取消选用（unuse-stock）时还原，卷标回到原申请单/可选清单。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil' "
        "AND COLUMN_NAME = 'prev_request_id'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_wire_coil "
        "ADD COLUMN prev_request_id BIGINT DEFAULT NULL COMMENT '绑定前原申请单ID（取消选用时还原）' AFTER item_id, "
        "ADD COLUMN prev_item_id BIGINT DEFAULT NULL COMMENT '绑定前原申请单行ID' AFTER prev_request_id"
    )
    conn.commit()
    print('[OK] kr_wire_coil 已补充 prev_request_id/prev_item_id 列（可取消选用）')


def _ensure_wire_coil_lot_no(conn, cursor):
    """
    kr_wire_coil 补齐 lot_no 列（幂等）：卷标 Lot（物料批次跟踪）。
    录入时默认带出申请单行的批次号，允许修改。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_wire_coil' "
        "AND COLUMN_NAME = 'lot_no'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_wire_coil ADD COLUMN lot_no VARCHAR(64) DEFAULT NULL "
        "COMMENT 'Lot 批次号（物料批次跟踪，默认带出申请单行批次号可修改）' AFTER part_number"
    )
    conn.commit()
    print('[OK] kr_wire_coil 已补充 lot_no 列（Lot 批次号）')


def _ensure_request_item_unit(conn, cursor):
    """
    kr_request_item 补齐 unit 列（幂等，可重复执行）。
    单位在最小包装申请单发起时从 CSI 获取并保存，后续卷标录入/出库直接读库，不再连 CSI。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_request_item' "
        "AND COLUMN_NAME = 'unit'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_request_item ADD COLUMN unit varchar(16) DEFAULT NULL "
        "COMMENT '物料单位（申请单发起时从CSI获取）' AFTER stock_loc"
    )
    conn.commit()
    print('[OK] kr_request_item 已补充 unit 列（申请单发起时获取单位）')


CREATE_RETURN_ITEM = """
CREATE TABLE IF NOT EXISTS kr_return_item (
  id            BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  request_id    BIGINT        NOT NULL COMMENT '退料申请单ID（关联 kr_material_request.id）',
  coil_id       VARCHAR(16)   NOT NULL COMMENT '卷标ID',
  part_number   VARCHAR(128)  DEFAULT NULL COMMENT '物料号（退料时带出）',
  unit          VARCHAR(16)   DEFAULT NULL COMMENT '单位（带出）',
  remain_length DECIMAL(12,2) DEFAULT NULL COMMENT '退料时剩余长度（原始长度-已消耗）',
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_return_request (request_id),
  INDEX idx_coil_id (coil_id),
  UNIQUE KEY uq_return_coil (request_id, coil_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='退料申请单明细（卷标维度，生产退料时扫卷标录入）'
"""


def _ensure_request_reject_reason(conn, cursor):
    """
    kr_material_request 补齐 reject_reason 列（幂等）：退料驳回原因。
    """
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_material_request' "
        "AND COLUMN_NAME = 'reject_reason'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_material_request ADD COLUMN reject_reason VARCHAR(256) DEFAULT NULL "
        "COMMENT '驳回原因（退料驳回时记录）' AFTER short_reason"
    )
    conn.commit()
    print('[OK] kr_material_request 已补充 reject_reason 列（退料驳回原因）')


def _ensure_inventory_count_tables(conn, cursor):
    """
    线材库存盘点：kr_inventory_count（盘点单）+ kr_inventory_count_item（盘点明细）。
    盘点开启后锁定卷标（不允许消耗），完成录入实际长度生成差异。
    """
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS kr_inventory_count (\n"
        "  id            BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',\n"
        "  count_no      VARCHAR(32)   NOT NULL COMMENT '盘点单号（如 INV260826001）',\n"
        "  status        VARCHAR(20)   NOT NULL DEFAULT 'pending' COMMENT 'pending待开始/counting盘点中/completed已完成',\n"
        "  siteref       VARCHAR(16)   NOT NULL COMMENT '站点',\n"
        "  created_by    VARCHAR(64)   DEFAULT NULL COMMENT '创建人',\n"
        "  started_at    DATETIME      DEFAULT NULL COMMENT '开始盘点时间',\n"
        "  completed_at  DATETIME      DEFAULT NULL COMMENT '完成时间',\n"
        "  note          VARCHAR(256)  DEFAULT NULL COMMENT '备注',\n"
        "  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
        "  PRIMARY KEY (id),\n"
        "  UNIQUE KEY uq_count_no (count_no),\n"
        "  INDEX idx_count_status (status)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线材库存盘点单'"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS kr_inventory_count_item (\n"
        "  id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',\n"
        "  count_id        BIGINT        NOT NULL COMMENT '盘点单ID',\n"
        "  coil_id         VARCHAR(16)   NOT NULL COMMENT '卷标ID',\n"
        "  part_number     VARCHAR(128)  DEFAULT NULL COMMENT '物料号',\n"
        "  lot_no          VARCHAR(64)   DEFAULT NULL COMMENT 'Lot批次号',\n"
        "  unit            VARCHAR(16)   DEFAULT NULL COMMENT '单位',\n"
        "  original_qty    DECIMAL(12,2) DEFAULT NULL COMMENT '原始数量（coil_length，系统单位）',\n"
        "  used_mm         DECIMAL(12,2) DEFAULT NULL COMMENT '使用数量(mm，消耗汇总）',\n"
        "  remain_mm       DECIMAL(12,2) DEFAULT NULL COMMENT '剩余数量(mm）',\n"
        "  actual_mm       DECIMAL(12,2) DEFAULT NULL COMMENT '盘点实际数量(mm）',\n"
        "  diff_mm         DECIMAL(12,2) DEFAULT NULL COMMENT '差异数量(mm）=实际-剩余',\n"
        "  diff_converted  DECIMAL(12,2) DEFAULT NULL COMMENT '转换差异数量(CSI单位）',\n"
        "  measured_by     VARCHAR(64)   DEFAULT NULL COMMENT '测量人',\n"
        "  measured_at     DATETIME      DEFAULT NULL COMMENT '测量时间',\n"
        "  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
        "  PRIMARY KEY (id),\n"
        "  UNIQUE KEY uq_count_coil (count_id, coil_id),\n"
        "  INDEX idx_count_id (count_id),\n"
        "  INDEX idx_coil_id (coil_id)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线材库存盘点明细'"
    )
    conn.commit()
    print('[OK] 线材盘点表 kr_inventory_count / kr_inventory_count_item 已确保')
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_material_request' "
        "AND COLUMN_NAME = 'reject_reason'"
    )
    if cursor.fetchone()['n']:
        return

    cursor.execute(
        "ALTER TABLE kr_material_request ADD COLUMN reject_reason VARCHAR(256) DEFAULT NULL "
        "COMMENT '驳回原因（退料驳回时记录）' AFTER short_reason"
    )
    conn.commit()
    print('[OK] kr_material_request 已补充 reject_reason 列（退料驳回原因）')


def _ensure_return_item_table(conn, cursor):
    """退料申请单明细表（幂等）。主表复用 kr_material_request（request_type='return'）"""
    cursor.execute(CREATE_RETURN_ITEM)
    # 退料确认字段（幂等）
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_material_request' AND COLUMN_NAME = 'confirmed_by'"
    )
    if not cursor.fetchone()['n']:
        cursor.execute(
            "ALTER TABLE kr_material_request "
            "ADD COLUMN confirmed_by varchar(64) DEFAULT NULL COMMENT '退料确认操作人' AFTER signature_data, "
            "ADD COLUMN confirm_time datetime DEFAULT NULL COMMENT '退料确认时间' AFTER confirmed_by"
        )
    conn.commit()
    print('[OK] kr_return_item 表已创建/已存在，退料确认字段已确保')


def _ensure_return_item_review(conn, cursor):
    """
    kr_return_item 补齐退料复核列（幂等，可重复执行）：
      - location      退回库位
      - review_status 复核状态：NULL未复核 / confirmed已退回 / rejected不予退料
      - review_note   不匹配原因
      - reviewed_by   复核人
      - reviewed_at   复核时间
    """
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kr_return_item'"
    )
    existing_cols = {r['COLUMN_NAME'] for r in cursor.fetchall()}

    review_columns = [
        ("location",      "VARCHAR(32)  DEFAULT NULL COMMENT '退回库位'"),
        ("review_status", "VARCHAR(20)  DEFAULT NULL COMMENT '复核状态：NULL未复核/confirmed已退回/rejected不予退料'"),
        ("review_note",   "VARCHAR(256) DEFAULT NULL COMMENT '不匹配原因'"),
        ("reviewed_by",   "VARCHAR(64)  DEFAULT NULL COMMENT '复核人'"),
        ("reviewed_at",   "DATETIME     DEFAULT NULL COMMENT '复核时间'"),
    ]
    alters = []
    for name, ddl in review_columns:
        if name not in existing_cols:
            alters.append(f"ADD COLUMN {name} {ddl}")

    if alters:
        cursor.execute("ALTER TABLE kr_return_item " + ", ".join(alters))
        conn.commit()
        print('[OK] kr_return_item 已补充退料复核列（location/review_status/review_note/reviewed_by/reviewed_at）')


def migrate():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for name, sql in (('kr_wire_coil', CREATE_WIRE_COIL),
                          ('kr_wire_coil_consumption', CREATE_WIRE_COIL_CONSUMPTION)):
            cursor.execute(sql)
            conn.commit()
            print(f'[OK] 表 {name} 已创建/已存在')

        # 旧表升级：对齐 kr_wire_coil_consumption 宽表结构（新增列/删除 request_id）
        _ensure_consumption_structure(conn, cursor)

        # 旧表升级：kr_wire_coil 补齐 item_id 列（按申请单行维护卷标）
        _ensure_wire_coil_item_id(conn, cursor)

        # 旧表升级：kr_wire_coil 补齐 is_deleted 列（软删除，可恢复）
        _ensure_wire_coil_is_deleted(conn, cursor)

        # 旧表升级：kr_wire_coil 补齐 prev 绑定列（可取消选用）
        _ensure_wire_coil_prev_bind(conn, cursor)

        # 旧表升级：kr_wire_coil 补齐 lot_no 列（Lot 批次号）
        _ensure_wire_coil_lot_no(conn, cursor)

        # 旧表升级：kr_request_item 补齐 unit 列（申请单发起时从 CSI 获取单位，后续读库）
        _ensure_request_item_unit(conn, cursor)

        # 旧表升级：kr_material_request 补齐 reject_reason 列（退料驳回原因）
        _ensure_request_reject_reason(conn, cursor)

        # 线材库存盘点表
        _ensure_inventory_count_tables(conn, cursor)

        # 退料申请单明细表（生产退料，卷标维度）
        _ensure_return_item_table(conn, cursor)

        # 退料复核列（逐卷库位/复核状态/不匹配原因等）
        _ensure_return_item_review(conn, cursor)

        # 校验表结构是否存在
        cursor.execute("SHOW TABLES LIKE 'kr_wire_coil'")
        t1 = cursor.fetchone()
        cursor.execute("SHOW TABLES LIKE 'kr_wire_coil_consumption'")
        t2 = cursor.fetchone()
        if not t1 or not t2:
            print('[ERROR] 表创建失败，请检查数据库账号权限')
            sys.exit(1)
        print('[OK] 迁移完成，两张新表均存在。')
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f'[ERROR] 迁移失败: {e}')
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    migrate()
