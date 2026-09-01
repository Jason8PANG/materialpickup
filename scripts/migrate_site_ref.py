"""
外部集成 API 站点（siteref）维度迁移脚本（为 310 站点接入做准备）

背景：naiwiptrack 对接 API（app/routes/external.py）已增加站点维度
（X-Site-Ref 强制校验 + 按站点过滤 + 落库）。本脚本为相关表补齐 siteref 列并回填。

涉及表（只加列 + 回填，不删任何列/数据，幂等可重复执行）：
  1. kr_wire_coil_consumption（消耗/报废登记表，25 列，当前无 siteref）
       - 加列 siteref VARCHAR(16) NULL + 索引 idx_siteref
       - 回填：通过 LEFT JOIN kr_wire_coil 取关联卷标的 siteref
         （登记时服务端从关联卷标自动带出，与卷标同站点）
  2. kr_cutting_check（首末件检查登记表，30 列，当前无 siteref）
       - 加列 siteref VARCHAR(16) NULL + 索引 idx_siteref
       - 回填：当前库中为 410 站点联调测试数据（行数 < 1000），
         直接 UPDATE siteref = '410'；若行数 >= 1000 则保持 NULL，
         在报告中注明需人工确认各站点归属后再回填。
  3. kr_cutting_ref 不迁移：无 siteref 列，裁剪参数跨站点共享。

运行: python scripts/migrate_site_ref.py
说明:
  - 数据库连接参数默认值与 app/config.py 一致（10.0.6.86:33306 materialpickup root/root07），
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


def ensure_siteref_column(conn, cursor, table, comment):
    """幂等补齐 siteref 列 + 索引，返回是否本次新增列"""
    cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE 'siteref'")
    exists = cursor.fetchone() is not None
    if exists:
        print(f'[SKIP] {table}.siteref 列已存在')
        return False

    cursor.execute(f"SHOW INDEX FROM `{table}` WHERE Key_name = 'idx_siteref'")
    has_idx = cursor.fetchone() is not None
    alters = [
        f"ADD COLUMN siteref VARCHAR(16) NULL COMMENT '{comment}'",
    ]
    if not has_idx:
        alters.append("ADD INDEX idx_siteref (siteref)")
    cursor.execute(f"ALTER TABLE `{table}` " + ", ".join(alters))
    conn.commit()
    print(f'[OK] {table} 已新增 siteref 列（含索引 idx_siteref）')
    return True


def ensure_siteref_index(conn, cursor, table):
    """幂等补齐 idx_siteref 索引（列已存在但缺索引时）"""
    cursor.execute(f"SHOW INDEX FROM `{table}` WHERE Key_name = 'idx_siteref'")
    if cursor.fetchone() is None:
        cursor.execute(f"ALTER TABLE `{table}` ADD INDEX idx_siteref (siteref)")
        conn.commit()
        print(f'[OK] {table} 已新增索引 idx_siteref')


def migrate_consumption(conn, cursor):
    """kr_wire_coil_consumption：加列 + 从关联卷标回填 siteref"""
    added = ensure_siteref_column(
        conn, cursor, 'kr_wire_coil_consumption',
        '站点：310-苏州/410-槟城（冗余自关联卷标，外部 API 落库）')
    if not added:
        ensure_siteref_index(conn, cursor, 'kr_wire_coil_consumption')
    cursor.execute(
        "UPDATE kr_wire_coil_consumption c "
        "LEFT JOIN kr_wire_coil w ON c.coil_id = w.coil_id "
        "SET c.siteref = w.siteref "
        "WHERE c.siteref IS NULL"
    )
    affected = cursor.rowcount
    conn.commit()
    cursor.execute(
        "SELECT COUNT(*) AS n FROM kr_wire_coil_consumption WHERE siteref IS NULL"
    )
    remain_null = cursor.fetchone()['n']
    print(f'[OK] kr_wire_coil_consumption 回填 {affected} 行（仍为 NULL: {remain_null}）')


def migrate_cutting_check(conn, cursor):
    """kr_cutting_check：加列 + 回填。
    当前库中为 410 站点联调测试数据（行数 < 1000），直接回填 '410'；
    若行数大则保持 NULL，需人工确认站点归属。"""
    added = ensure_siteref_column(
        conn, cursor, 'kr_cutting_check',
        '站点：310-苏州/410-槟城（外部 API 按调用站点落库）')
    if not added:
        ensure_siteref_index(conn, cursor, 'kr_cutting_check')

    cursor.execute("SELECT COUNT(*) AS n FROM kr_cutting_check")
    total = cursor.fetchone()['n']
    if total < 1000:
        # 假设：当前数据为 410 站点联调测试数据（需求方确认），行数较少时直接回填 '410'
        cursor.execute(
            "UPDATE kr_cutting_check SET siteref = '410' WHERE siteref IS NULL"
        )
        affected = cursor.rowcount
        conn.commit()
        cursor.execute(
            "SELECT COUNT(*) AS n FROM kr_cutting_check WHERE siteref IS NULL"
        )
        remain_null = cursor.fetchone()['n']
        print(f'[OK] kr_cutting_check 行数 {total}（<1000，按 410 联调数据回填）'
              f'，回填 {affected} 行（仍为 NULL: {remain_null}）')
    else:
        print(f'[WARN] kr_cutting_check 行数 {total}（>=1000），siteref 保持 NULL，'
              f'需人工确认各站点归属后再回填（参见 wiptrack_api_integration.md）')


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        print('=== 站点维度迁移开始（materialpickup） ===')
        migrate_consumption(conn, cursor)
        migrate_cutting_check(conn, cursor)
        # 汇总
        cursor.execute("SELECT siteref, COUNT(*) AS n FROM kr_wire_coil_consumption GROUP BY siteref")
        print('kr_wire_coil_consumption 按站点分布:', cursor.fetchall())
        cursor.execute("SELECT siteref, COUNT(*) AS n FROM kr_cutting_check GROUP BY siteref")
        print('kr_cutting_check 按站点分布:', cursor.fetchall())
        print('=== 迁移完成（只加列+回填，未删除任何列/数据） ===')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
