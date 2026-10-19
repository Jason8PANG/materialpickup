"""
kr_role_mapping.site_access 增量迁移脚本（非 admin 账号支持多站点访问）

背景：物料领取看板原先一个账号绑定单站点（siteref=默认站），admin 才能跨站切换。
本次改造：kr_role_mapping 新增 site_access 列，记录某账号可访问的站点列表
（逗号分隔，如 '310,410'），登录/切换站点据此放开到普通角色。

改动（只加列 + 回填，不删任何列/数据，幂等可重复执行）：
  1. kr_role_mapping 加列 site_access VARCHAR(64) NULL（若已存在则跳过）
       COMMENT '可访问站点列表(逗号分隔)；NULL=仅默认站'，位置在 siteref 之后
  2. 回填：site_access IS NULL 且 siteref 非空的行，site_access = siteref
       （老数据默认只访问自己的 siteref 站；admin/无默认站账号保持 NULL，
        登录时 admin 自动全站、无默认站账号可访问列表为空）

运行: python scripts/migrate_role_site_access.py
说明:
  - 采用 migrate_site_ref.py 同款幂等模式（SHOW COLUMNS 检测缺失列再 ALTER）；
  - 数据库连接参数默认值与 app/config.py 一致（10.0.6.86:33306 materialpickup），
    可被环境变量覆盖；不 import app 包，避免触发 Flask 应用初始化。
"""
import os

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


COLUMN_DDL = (
    "site_access VARCHAR(64) DEFAULT NULL "
    "COMMENT '可访问站点列表(逗号分隔)；NULL=仅默认站'"
)


def migrate_role_mapping(conn, cursor):
    """kr_role_mapping：加 site_access 列 + 回填 siteref"""
    # 1) 检测并补列（幂等）
    cursor.execute("SHOW COLUMNS FROM kr_role_mapping LIKE 'site_access'")
    added = False
    if cursor.fetchone() is None:
        cursor.execute(f"ALTER TABLE kr_role_mapping ADD COLUMN {COLUMN_DDL} AFTER siteref")
        conn.commit()
        print('[OK] kr_role_mapping 已新增 site_access 列（AFTER siteref）')
        added = True
    else:
        print('[SKIP] kr_role_mapping.site_access 列已存在')

    # 2) 回填：老数据 = 仅可访问自己的默认站 siteref
    cursor.execute(
        "UPDATE kr_role_mapping SET site_access = siteref "
        "WHERE site_access IS NULL AND siteref IS NOT NULL"
    )
    affected = cursor.rowcount
    conn.commit()

    cursor.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(site_access IS NULL) AS null_cnt, "
        "SUM(site_access IS NOT NULL) AS filled_cnt "
        "FROM kr_role_mapping"
    )
    stat = cursor.fetchone()
    print(f'[OK] 回填 {affected} 行（本次新增列: {added}）')
    print(f'[OK] 现状: 共 {stat["total"]} 行，site_access 非空 {stat["filled_cnt"]} 行，'
          f'NULL {stat["null_cnt"]} 行')
    cursor.execute(
        "SELECT role, siteref, site_access FROM kr_role_mapping "
        "WHERE site_access IS NOT NULL AND site_access <> siteref "
        "ORDER BY id LIMIT 20"
    )
    rows = cursor.fetchall()
    if rows:
        print('[INFO] site_access 与 siteref 不一致的行（人工配置过多站点）:')
        for r in rows:
            print('  ', r)
    else:
        print('[INFO] 无 site_access 与 siteref 不一致的行（均为单一默认站）')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== kr_role_mapping.site_access 迁移开始 ===')
        migrate_role_mapping(conn, cursor)
        print('=== 迁移完成（只加列+回填，未删除任何列/数据） ===')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
