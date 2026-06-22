"""
数据模型 - 直接使用 pymysql 操作数据库，不使用 ORM
"""
from contextlib import contextmanager
import pymysql
from app.config import Config
from pymysql.cursors import DictCursor


def get_db():
    """获取数据库连接"""
    return pymysql.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB,
        charset='utf8mb4',
        cursorclass=DictCursor
    )


@contextmanager
def get_db_connection():
    """数据库连接上下文管理器，确保连接自动关闭"""
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def get_site_filter(session_user):
    """
    根据当前登录用户返回站点过滤条件。
    
    Returns:
        (where_clause, params) 元组，用于动态拼接 SQL
        - admin 跨站返回 (None, None)，不过滤
        - 非 admin 返回 ("siteref = %s", (site,))
    """
    if not session_user:
        return None, None
    
    role = session_user.get('role')
    siteref = session_user.get('siteref')
    
    # admin 跨站或 siteref 为空 => 不过滤
    if role == 'admin' and not siteref:
        return None, None
    
    if siteref:
        return ("siteref = %s", (siteref,))
    
    return None, None


STATUS_LABELS = {
    'pending_approval': '待审批', 'rejected': '已驳回',
    'pending_prep': '待备料', 'prepping': '备料中',
    'short': '缺料', 'ready_pickup': '待取料', 'completed': '已完成'
}


STATUS_COLORS = {
    'pending_approval': 'bg-primary', 'rejected': 'bg-secondary',
    'pending_prep': 'bg-warning text-dark', 'prepping': 'bg-success',
    'short': 'bg-danger', 'ready_pickup': 'bg-secondary',
    'completed': 'bg-success'
}
