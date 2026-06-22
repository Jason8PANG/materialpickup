"""初始化V2数据库 - 重建所有表 + 种子数据"""
import pymysql
from werkzeug.security import generate_password_hash

conn = pymysql.connect(
    host='10.0.6.86', port=33306, user='powerbi',
    password='!Q1234567', database='materialpickup', charset='utf8mb4'
)
c = conn.cursor()

# Drop old tables first
for t in ['kr_request_item', 'kr_operation_log', 'kr_approval_token', 'kr_role_mapping', 'kr_material_request']:
    c.execute(f'DROP TABLE IF EXISTS {t}')

# 1. kr_material_request
c.execute("""CREATE TABLE kr_material_request (
    id INT AUTO_INCREMENT PRIMARY KEY,
    siteref VARCHAR(16) NOT NULL,
    requester VARCHAR(64) NOT NULL,
    request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'pending_approval',
    supervisor VARCHAR(64), approve_time DATETIME, approve_comment TEXT,
    warehouse_operator VARCHAR(64), short_reason TEXT, short_time DATETIME,
    signer VARCHAR(64), sign_time DATETIME, remark TEXT,
    is_deleted TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_siteref (siteref), INDEX idx_status (status), INDEX idx_requester (requester)
)""")

# 2. kr_request_item
c.execute("""CREATE TABLE kr_request_item (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL,
    job_order VARCHAR(64) NOT NULL,
    part_number VARCHAR(128) NOT NULL,
    quantity DECIMAL(12,2) NOT NULL,
    price DECIMAL(12,4), total_amount DECIMAL(14,2), stock_qty DECIMAL(12,2),
    replenish_reason VARCHAR(32), replenish_reason_other VARCHAR(256),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_request_id (request_id),
    INDEX idx_job_order (job_order)
)""")

# 3. kr_role_mapping
c.execute("""CREATE TABLE kr_role_mapping (
    id INT AUTO_INCREMENT PRIMARY KEY,
    domain_account VARCHAR(128) NOT NULL UNIQUE,
    display_name VARCHAR(128), role VARCHAR(32) NOT NULL,
    email VARCHAR(256), password_hash VARCHAR(256),
    siteref VARCHAR(16), is_active TINYINT(1) DEFAULT 1, remark VARCHAR(256),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_role (role), INDEX idx_siteref (siteref)
)""")

# 4. kr_approval_token
c.execute("""CREATE TABLE kr_approval_token (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL, token VARCHAR(64) NOT NULL UNIQUE,
    supervisor VARCHAR(64) NOT NULL,
    is_used TINYINT(1) DEFAULT 0, expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# 5. kr_operation_log
c.execute("""CREATE TABLE kr_operation_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    request_id INT, operator VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL, detail TEXT, ip_address VARCHAR(45),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# Seed users
seeds = [
    ('admin', '管理员', 'admin', 'admin@example.com', None),
    ('requester1', '领料员张三', 'requester', 'req1@example.com', '310'),
    ('requester2', '领料员李四', 'requester', 'req2@example.com', '410'),
    ('supervisor1', '主管王经理', 'supervisor', 'sup1@example.com', '310'),
    ('supervisor2', '主管陈主管', 'supervisor', 'sup2@example.com', '410'),
    ('warehouse1', '仓管王五', 'warehouse', 'ware1@example.com', '310'),
    ('warehouse2', '仓管陈七', 'warehouse', 'ware2@example.com', '410'),
]
for ac, dn, ro, em, si in seeds:
    pw = generate_password_hash(ac)
    c.execute("INSERT INTO kr_role_mapping (domain_account,display_name,role,email,password_hash,siteref,is_active) VALUES (%s,%s,%s,%s,%s,%s,1)",
              (ac, dn, ro, em, pw, si))

conn.commit()
c.close()
conn.close()
print('V2 DB initialized! Sites: 310=Suzhou, 410=Penang')
print('Users: admin(跨站) / requester1(310) / requester2(410) / supervisor1(310) / supervisor2(410) / warehouse1(310) / warehouse2(410)')
