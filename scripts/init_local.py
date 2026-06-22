"""初始化本地测试数据库"""
import pymysql
from werkzeug.security import generate_password_hash

conn = pymysql.connect(
    host='10.0.6.86', port=33306, user='powerbi',
    password='!Q1234567', database='materialpickup', charset='utf8mb4'
)
c = conn.cursor()

# 建表
c.execute("""CREATE TABLE IF NOT EXISTS kr_material_request (
    id INT AUTO_INCREMENT PRIMARY KEY, job_order VARCHAR(64) NOT NULL,
    part_number VARCHAR(128) NOT NULL, quantity DECIMAL(12,2) NOT NULL,
    price DECIMAL(12,4), total_amount DECIMAL(14,2), stock_qty DECIMAL(12,2),
    replenish_reason VARCHAR(32), replenish_reason_other VARCHAR(256),
    requester VARCHAR(64) NOT NULL, request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'pending_approval',
    supervisor VARCHAR(64), approve_time DATETIME, approve_comment TEXT,
    warehouse_operator VARCHAR(64), short_reason TEXT, short_time DATETIME,
    signer VARCHAR(64), sign_time DATETIME, remark TEXT,
    is_deleted TINYINT(1) DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status (status), INDEX idx_job_order (job_order), INDEX idx_requester (requester)
)""")

c.execute("""CREATE TABLE IF NOT EXISTS kr_role_mapping (
    id INT AUTO_INCREMENT PRIMARY KEY, domain_account VARCHAR(128) NOT NULL UNIQUE,
    display_name VARCHAR(128), role VARCHAR(32) NOT NULL, email VARCHAR(256),
    password_hash VARCHAR(256), is_active TINYINT(1) DEFAULT 1, remark VARCHAR(256),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
)""")

c.execute("""CREATE TABLE IF NOT EXISTS kr_approval_token (
    id INT AUTO_INCREMENT PRIMARY KEY, request_id INT NOT NULL,
    token VARCHAR(64) NOT NULL UNIQUE, supervisor VARCHAR(64) NOT NULL,
    is_used TINYINT(1) DEFAULT 0, expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

c.execute("""CREATE TABLE IF NOT EXISTS kr_operation_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY, request_id INT,
    operator VARCHAR(64) NOT NULL, action VARCHAR(64) NOT NULL,
    detail TEXT, ip_address VARCHAR(45), created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# 种子数据
seeds = [
    ('admin', '管理员', 'admin', 'admin@example.com'),
    ('requester1', '领料员张三', 'requester', 'req1@example.com'),
    ('requester2', '领料员李四', 'requester', 'req2@example.com'),
    ('supervisor1', '主管王经理', 'supervisor', 'sup1@example.com'),
    ('warehouse1', '仓管王五', 'warehouse', 'ware1@example.com'),
]
for sa, dn, ro, em in seeds:
    pw_hash = generate_password_hash(sa)
    c.execute("INSERT IGNORE INTO kr_role_mapping (domain_account,display_name,role,email,password_hash,is_active) VALUES (%s,%s,%s,%s,%s,1)",
              (sa, dn, ro, em, pw_hash))

conn.commit()
c.close()
conn.close()
print("Database initialized successfully!")
print("Test accounts: admin / requester1 / supervisor1 / warehouse1")
print("All passwords = username")
