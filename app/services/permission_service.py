"""角色—页面权限服务（kr_role_page_permission）。

设计要点
--------
1. **只做页面级**：菜单是否显示 + 直输 URL 拦截；页面内按钮的操作权限
   仍由各路由/前端的既有硬编码判断，不在这里管。
2. **admin 永远全开**：不读库、不写库、不受矩阵影响。
3. **读库失败不误伤**：异常时回落到 page_registry 的 default_roles（fail-open
   到默认矩阵），保证数据库抖动时用户不会被莫名挡在门外。
4. **进程内缓存 60s**：菜单渲染一次要看十几个页面的可见性，缓存避免每格一次
   SELECT；写入后立即 clear_cache()，保存后马上生效。
5. 所有写操作都在独立事务里完成，写完清缓存并落审计日志
   （module=permission, action=update_permissions, detail 含前后差异）。
"""
import logging
import threading
import time

import pymysql
from pymysql.cursors import DictCursor

from app.config import Config
from app.page_registry import (
    PAGES, PAGE_KEYS, PAGE_MAP, ROLES, EDITABLE_ROLES, ROLE_LABELS,
    default_matrix, match_path, is_whitelisted,
)
from app.services.operation_log import log_operation

logger = logging.getLogger(__name__)

CACHE_TTL = 60          # 秒
_TABLE = 'kr_role_page_permission'

# role -> (expire_at, {page_key: bool})
_cache = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  数据库
# --------------------------------------------------------------------------- #
def _connect():
    return pymysql.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB,
        charset='utf8mb4',
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
    )


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  role VARCHAR(32) NOT NULL,
  page_key VARCHAR(64) NOT NULL,
  allowed TINYINT(1) NOT NULL DEFAULT 0,
  updated_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_role_page (role, page_key),
  KEY idx_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色页面权限'
"""


def ensure_table():
    """建表（幂等）"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
    finally:
        conn.close()


def _read_role(role):
    """读某角色的库内权限 {page_key: bool}；无行/异常返回 None（调用方回落默认值）"""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT page_key, allowed FROM {_TABLE} WHERE role = %s',
                    (role,),
                )
                rows = cur.fetchall() or []
        finally:
            conn.close()
        return {str(r['page_key']): bool(r['allowed']) for r in rows}
    except Exception as e:                      # noqa: BLE001 - 权限读失败按默认矩阵放行
        logger.warning('读取角色页面权限失败（回落默认值）role=%s: %s', role, e)
        return None


# --------------------------------------------------------------------------- #
#  缓存
# --------------------------------------------------------------------------- #
def clear_cache(role=None):
    with _cache_lock:
        if role is None:
            _cache.clear()
        else:
            _cache.pop(role, None)


def _cache_get(role):
    with _cache_lock:
        item = _cache.get(role)
    if not item:
        return None
    expire_at, value = item
    if time.time() >= expire_at:
        return None
    return dict(value)


def _cache_put(role, value):
    with _cache_lock:
        _cache[role] = (time.time() + CACHE_TTL, dict(value))


# --------------------------------------------------------------------------- #
#  查询
# --------------------------------------------------------------------------- #
def get_role_permissions(role):
    """返回 {page_key: allowed}。库中缺行的页面按 default_roles 回落。"""
    role = (role or '').strip()
    if role not in ROLES:
        # 未知角色：按默认矩阵计算（通常全部 False），不抛异常避免页面 500
        return {p['key']: role in p['default_roles'] for p in PAGES}
    if role == 'admin':
        return {key: True for key in PAGE_KEYS}

    cached = _cache_get(role)
    if cached is not None:
        return cached

    db_map = _read_role(role) or {}
    result = {}
    for page in PAGES:
        key = page['key']
        if key in db_map:
            result[key] = db_map[key]
        else:
            result[key] = role in page['default_roles']
    _cache_put(role, result)
    return result


def can_access_page(user, page_key):
    """当前用户能否看到/进入某个 page_key（user 为 session['user'] dict）"""
    if not page_key:
        return True
    if not user:
        return False
    if (user.get('role') or '') == 'admin':
        return True
    if page_key not in PAGE_MAP:
        return True                              # 未注册的 key 不拦
    return bool(get_role_permissions(user.get('role')).get(page_key, False))


def can_access_page_for_session(page_key):
    """模板全局 can_access_page(page_key)：自动取当前登录用户"""
    try:
        from flask import session
        return can_access_page(session.get('user'), page_key)
    except Exception:                            # noqa: BLE001 - 渲染期异常不拦页面
        return True


def can_access(user, path):
    """请求级判定：当前用户能否访问该路径。

    - admin：全开
    - 白名单（/、/login、/logout、/public/kanban、/approve/*、/api/*、/static/*、/favicon.ico）：放行
    - 未注册路径：放行（不误伤新页面）
    """
    if not user:
        return True                              # 未登录交给现有登录跳转
    if (user.get('role') or '') == 'admin':
        return True
    if is_whitelisted(path):
        return True
    page = match_path(path)
    if not page:
        return True
    return can_access_page(user, page['key'])


# --------------------------------------------------------------------------- #
#  写入
# --------------------------------------------------------------------------- #
def set_role_permissions(role, page_keys, operator='SYSTEM'):
    """全量覆盖某角色的页面权限（admin 不可改）。

    Args:
        role: 目标角色（不能是 admin）
        page_keys: 允许的 page_key 列表（未列出的页面即为不可见）
        operator: 操作人（写审计日志用）

    Returns:
        {'role':..., 'added': [...], 'removed': [...], 'before': {...}, 'after': {...}}

    Raises:
        ValueError: 角色非法 / 页面 key 非法
    """
    role = (role or '').strip()
    if role not in EDITABLE_ROLES:
        raise ValueError(f'角色不可编辑: {role}')

    keys = set()
    for key in (page_keys or []):
        key = str(key).strip()
        if key not in PAGE_MAP:
            raise ValueError(f'未知页面: {key}')
        keys.add(key)

    before = dict(get_role_permissions(role))
    after = {p['key']: (p['key'] in keys) for p in PAGES}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {_TABLE} WHERE role = %s', (role,))
            rows = [
                (role, p['key'], 1 if after[p['key']] else 0, (operator or 'SYSTEM')[:64])
                for p in PAGES
            ]
            cur.executemany(
                f'INSERT INTO {_TABLE} (role, page_key, allowed, updated_by) '
                f'VALUES (%s, %s, %s, %s)',
                rows,
            )
    finally:
        conn.close()

    clear_cache(role)

    added = [k for k in PAGE_KEYS if after[k] and not before.get(k)]
    removed = [k for k in PAGE_KEYS if not after[k] and before.get(k)]

    # 审计：权限变更必须可追溯（日志失败不影响保存结果）
    try:
        log_operation(
            'permission', 'update_permissions',
            target_type='role', target_key=role,
            operator=operator or 'SYSTEM', result='ok',
            detail={
                'role': role,
                'role_label': ROLE_LABELS.get(role, role),
                'added': added,
                'removed': removed,
                'before': sorted([k for k, v in before.items() if v]),
                'after': sorted([k for k, v in after.items() if v]),
            },
        )
    except Exception as e:                      # noqa: BLE001
        logger.warning('权限变更审计日志写入失败: %s', e)

    return {'role': role, 'added': added, 'removed': removed,
            'before': before, 'after': after}


def init_default_permissions():
    """幂等灌默认矩阵（INSERT IGNORE）：5 角色 × 18 页面 = 90 行。

    已有行不会被覆盖（不破坏管理员后续的调整）。
    """
    ensure_table()
    matrix = default_matrix()
    rows = []
    for role in ROLES:
        for page in PAGES:
            rows.append((role, page['key'], 1 if matrix[role][page['key']] else 0, 'init'))

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                f'INSERT IGNORE INTO {_TABLE} (role, page_key, allowed, updated_by) '
                f'VALUES (%s, %s, %s, %s)',
                rows,
            )
            cur.execute(f'SELECT COUNT(*) AS c FROM {_TABLE}')
            total = int(cur.fetchone()['c'])
    finally:
        conn.close()

    clear_cache()
    return {'attempted': len(rows), 'total': total}


def get_matrix():
    """管理页用：{role: {page_key: bool}}（admin 恒为 True）"""
    return {role: get_role_permissions(role) for role in ROLES}


# --------------------------------------------------------------------------- #
#  CLI：python -m app.services.permission_service --init
# --------------------------------------------------------------------------- #
def _main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--init' in argv:
        stats = init_default_permissions()
        print(f"默认权限已灌入：尝试 {stats['attempted']} 行，表内现有 {stats['total']} 行")
        return 0
    if '--dump' in argv:
        for role in ROLES:
            perms = get_role_permissions(role)
            allowed = [k for k in PAGE_KEYS if perms.get(k)]
            print(f'{role:12s} ({ROLE_LABELS.get(role, role)}): {len(allowed)} 项')
            print('   ', ', '.join(allowed))
        return 0
    print(__doc__)
    print('用法: python -m app.services.permission_service --init | --dump')
    return 1


if __name__ == '__main__':
    raise SystemExit(_main())
