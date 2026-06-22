"""
数据库初始化脚本 V2 - 多工单多物料 + 多站点隔离
运行: python -m app.init_db
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from werkzeug.security import generate_password_hash
from app.config import Config


def get_connection():
    return pymysql.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )


# ====== DROP 旧表（谨慎使用） ======
DROP_TABLES = [
    "DROP TABLE IF EXISTS kr_request_item",
    "DROP TABLE IF EXISTS kr_approval_token",
    "DROP TABLE IF EXISTS kr_operation_log",
    "DROP TABLE IF EXISTS kr_material_request",
    "DROP TABLE IF EXISTS kr_role_mapping",
]


# ====== CREATE 新表 ======
CREATE_TABLES = [
    # 1. kr_role_mapping（先创建，因无外键依赖）
    """
    CREATE TABLE kr_role_mapping (
      id              INT             NOT NULL AUTO_INCREMENT COMMENT '主键',
      domain_account  VARCHAR(128)    NOT NULL COMMENT '域账号 samAccountName',
      display_name    VARCHAR(128)    DEFAULT NULL COMMENT '显示名称',
      role            VARCHAR(32)     NOT NULL COMMENT '角色: admin/requester/supervisor/warehouse',
      siteref         VARCHAR(16)     DEFAULT NULL COMMENT '所属站点：310-苏州 / 410-槟城；NULL 表示跨站点（仅 admin 角色可用）',
      email           VARCHAR(256)    DEFAULT NULL COMMENT '邮箱地址（主管必填，用于接收审批邮件）',
      password_hash   VARCHAR(256)    DEFAULT NULL COMMENT '密码哈希',
      is_active       TINYINT(1)      NOT NULL DEFAULT 1 COMMENT '是否启用',
      remark          VARCHAR(256)    DEFAULT NULL COMMENT '备注',
      created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_domain_account (domain_account),
      INDEX idx_role (role),
      INDEX idx_siteref (siteref)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='域账号-角色映射表（含站点分配）'
    """,

    # 2. kr_material_request 主表（无物料字段）
    """
    CREATE TABLE kr_material_request (
      id                INT             NOT NULL AUTO_INCREMENT COMMENT '主键',
      siteref           VARCHAR(16)     NOT NULL COMMENT '站点编号：310-苏州工厂 / 410-槟城工厂',
      requester         VARCHAR(64)     NOT NULL COMMENT '申请人（提交人工号）',
      request_time      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '申请时间',
      status            VARCHAR(20)     NOT NULL DEFAULT 'pending_approval' COMMENT '状态',
      supervisor        VARCHAR(64)     DEFAULT NULL COMMENT '审批主管工号',
      approve_time      DATETIME        DEFAULT NULL COMMENT '审批时间',
      approve_comment   TEXT            DEFAULT NULL COMMENT '审批批注',
      warehouse_operator VARCHAR(64)    DEFAULT NULL COMMENT '备料员工号',
      short_reason      TEXT            DEFAULT NULL COMMENT '缺料原因（整体级别）',
      short_time        DATETIME        DEFAULT NULL COMMENT '缺料登记时间',
      signer            VARCHAR(64)     DEFAULT NULL COMMENT '签字确认人',
      sign_time         DATETIME        DEFAULT NULL COMMENT '签字确认时间',
      signature_data    LONGTEXT        DEFAULT NULL COMMENT '签字图片(base64)',
      remark            TEXT            DEFAULT NULL COMMENT '备注',
      is_deleted        TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除：0-正常 1-删除',
      created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      INDEX idx_siteref (siteref),
      INDEX idx_status (status),
      INDEX idx_requester (requester),
      INDEX idx_request_time (request_time)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='领料申请单主表'
    """,

    # 3. kr_request_item 明细表（新增）
    """
    CREATE TABLE kr_request_item (
      id                      INT             NOT NULL AUTO_INCREMENT COMMENT '主键',
      request_id              INT             NOT NULL COMMENT '关联主表ID（kr_material_request.id）',
      job_order               VARCHAR(64)     NOT NULL COMMENT '工单号码',
      part_number             VARCHAR(128)    NOT NULL COMMENT '物料 Part Number',
      quantity                DECIMAL(12,2)   NOT NULL COMMENT '申请数量',
      price                   DECIMAL(12,4)   DEFAULT NULL COMMENT '物料单价',
      total_amount            DECIMAL(14,2)   DEFAULT NULL COMMENT '总金额 = 数量 × 单价（自动计算）',
      stock_qty               DECIMAL(12,2)   DEFAULT NULL COMMENT '库存量（参考）',
      replenish_reason        VARCHAR(32)     DEFAULT NULL COMMENT '补料原因：报废/不良/来料不足/其他',
      replenish_reason_other  VARCHAR(256)    DEFAULT NULL COMMENT '其他原因的详细说明',
      short_reason            VARCHAR(256)    DEFAULT NULL COMMENT '按行缺料原因',
      created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      INDEX idx_request_id (request_id),
      INDEX idx_job_order (job_order),
      INDEX idx_part_number (part_number)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='申请单明细表（工单物料项）'
    """,

    # 4. kr_approval_token
    """
    CREATE TABLE kr_approval_token (
      id              INT             NOT NULL AUTO_INCREMENT COMMENT '主键',
      request_id      INT             NOT NULL COMMENT '关联申请单ID',
      token           VARCHAR(64)     NOT NULL COMMENT '一次性令牌',
      supervisor      VARCHAR(64)     NOT NULL COMMENT '主管账号',
      is_used         TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否已使用',
      expires_at      DATETIME        NOT NULL COMMENT '过期时间',
      created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_token (token),
      INDEX idx_request_id (request_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='邮件审批令牌表'
    """,

    # 5. kr_operation_log
    """
    CREATE TABLE kr_operation_log (
      id              BIGINT          NOT NULL AUTO_INCREMENT COMMENT '主键',
      request_id      INT             DEFAULT NULL COMMENT '关联申请单ID',
      operator        VARCHAR(64)     NOT NULL COMMENT '操作用户工号',
      action          VARCHAR(64)     NOT NULL COMMENT '操作类型',
      detail          TEXT            DEFAULT NULL COMMENT '操作详情（JSON格式）',
      ip_address      VARCHAR(45)     DEFAULT NULL COMMENT '操作IP',
      created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      INDEX idx_request_id (request_id),
      INDEX idx_operator (operator),
      INDEX idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='操作日志表'
    """,
]


# ====== 种子数据 ======
def get_seed_users():
    """生成种子用户数据，密码 = 账号名"""
    users = [
        # (domain_account, display_name, role, siteref, email, remark)
        ('admin', '管理员', 'admin', None, 'admin@example.com', '系统管理员'),
        ('requester1', '领料员张三', 'requester', '310', 'requester1@example.com', '苏州工厂领料员'),
        ('requester2', '领料员李四', 'requester', '410', 'requester2@example.com', '槟城工厂领料员'),
        ('supervisor1', '主管王五', 'supervisor', '310', 'supervisor1@example.com', '苏州工厂主管'),
        ('supervisor2', '主管赵六', 'supervisor', '410', 'supervisor2@example.com', '槟城工厂主管'),
        ('warehouse1', '仓管陈七', 'warehouse', '310', 'warehouse1@example.com', '苏州工厂仓库'),
        ('warehouse2', '仓管周八', 'warehouse', '410', 'warehouse2@example.com', '槟城工厂仓库'),
    ]

    sql_rows = []
    for account, name, role, site, email, remark in users:
        pw_hash = generate_password_hash(account)
        site_str = f"'{site}'" if site else 'NULL'
        sql_rows.append(
            f"('{account}', '{name}', '{role}', {site_str}, '{email}', '{pw_hash}', 1, '{remark}')"
        )
    return sql_rows


SEED_SQL = """
INSERT IGNORE INTO kr_role_mapping 
    (domain_account, display_name, role, siteref, email, password_hash, is_active, remark)
VALUES
{}
""".format(",\n".join(get_seed_users()))


def init_database():
    conn = get_connection()
    cursor = conn.cursor()
    
    print('开始删除旧表...')
    for sql in DROP_TABLES:
        cursor.execute(sql)
        print(f'  [OK] 已删除旧表')
    conn.commit()

    print('开始创建数据库表...')
    for sql in CREATE_TABLES:
        table_name = sql.split('TABLE')[1].split()[0].strip('`')
        cursor.execute(sql)
        print(f'  [OK] 表 {table_name} 已创建')
    conn.commit()

    print('开始插入种子数据...')
    try:
        cursor.execute(SEED_SQL)
        conn.commit()
        print(f'  [OK] 种子用户数据已插入 (密码 = 用户名)')
    except Exception as e:
        conn.rollback()
        print(f'  [WARN] 插入数据失败: {e}')

    cursor.close()
    conn.close()
    print('数据库初始化完成!')


if __name__ == '__main__':
    init_database()
