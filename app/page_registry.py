"""页面注册表：系统内所有「页面级」受控资源的唯一事实来源。

用途
----
角色—页面权限（kr_role_page_permission）以 page_key 为粒度存储；
菜单渲染、直输 URL 拦截、管理页面表格都从这里取页面清单与默认值，
避免三处各写一份角色判断（原 base.html 里的硬编码 if 已改为查这里）。

约定
----
1. default_roles 是「该页面默认对哪些角色可见」，用于：
   - init_default_permissions() 灌初始矩阵；
   - 库中缺行时的回落值（保证新增页面不需要立刻补数据就能用）。
2. admin 不参与矩阵：始终全开（see permission_service.can_access）。
3. path 支持 <int:xxx> / <xxx> 动态段，match_path() 用正则匹配，
   因此 /request/123 能命中 /request/<int:request_id>。
"""
import re

# 固定 5 个角色（顺序即管理页列顺序，admin 固定在首位且不可编辑）
ROLES = ['admin', 'warehouse', 'requester', 'supervisor', 'me_engineer']

ROLE_LABELS = {
    'admin': '管理员',
    'warehouse': '仓库',
    'requester': '申请人',
    'supervisor': '主管',
    'me_engineer': 'ME工程师',
}

# 可被矩阵编辑的角色（admin 始终全开，不写入矩阵）
EDITABLE_ROLES = [r for r in ROLES if r != 'admin']

_ALL_ROLES = list(ROLES)
_REQ_ADMIN = ['requester', 'admin']
_WIRE_VIEW = ['requester', 'admin', 'me_engineer', 'warehouse']
_WH_ADMIN = ['warehouse', 'admin']
_ADMIN_ONLY = ['admin']

# 页面清单：与 app/templates/base.html 的菜单结构逐项对应
PAGES = [
    {'key': 'kanban', 'path': '/kanban', 'name': '看板', 'group': '看板',
     'default_roles': _ALL_ROLES},

    {'key': 'request_new', 'path': '/request/new', 'name': '新建按需申请', 'group': '新建',
     'default_roles': _REQ_ADMIN},
    {'key': 'request_minpack', 'path': '/request/minpack/new', 'name': '新建最小包装申请', 'group': '新建',
     'default_roles': _REQ_ADMIN},
    {'key': 'return_new', 'path': '/return/new', 'name': '新建退料申请', 'group': '新建',
     'default_roles': _REQ_ADMIN},
    {'key': 'wire_count', 'path': '/wire/count', 'name': '线材库存盘点', 'group': '新建',
     'default_roles': _WIRE_VIEW},

    {'key': 'wire_coils', 'path': '/wire/coils', 'name': '卷标查询', 'group': '线材管理',
     'default_roles': _WIRE_VIEW},
    {'key': 'wire_consumption', 'path': '/wire/consumption', 'name': '线材消耗查询', 'group': '线材管理',
     'default_roles': _WIRE_VIEW},
    {'key': 'wire_scrap', 'path': '/wire/scrap', 'name': '线材报废查询', 'group': '线材管理',
     'default_roles': _WIRE_VIEW},
    {'key': 'return_review', 'path': '/return/review', 'name': '退料复核', 'group': '线材管理',
     'default_roles': _WH_ADMIN},
    {'key': 'wire_adjust', 'path': '/wire/adjust', 'name': '盘点差异调整', 'group': '线材管理',
     'default_roles': _WH_ADMIN},
    {'key': 'csi_interface', 'path': '/csi-interface', 'name': '同步接口查询', 'group': '线材管理',
     'default_roles': _WIRE_VIEW},
    {'key': 'cutting_specs', 'path': '/cutting/specs', 'name': '裁线规格管理', 'group': '线材管理',
     'default_roles': _WIRE_VIEW},

    {'key': 'pending', 'path': '/pending', 'name': '待处理', 'group': '待处理',
     'default_roles': _WH_ADMIN},

    {'key': 'history', 'path': '/history', 'name': '历史查询', 'group': '查询',
     'default_roles': _ALL_ROLES},
    {'key': 'records', 'path': '/records', 'name': '记录查询', 'group': '查询',
     'default_roles': _ALL_ROLES},

    {'key': 'admin_mappings', 'path': '/admin/mappings', 'name': '账号管理', 'group': '系统管理',
     'default_roles': _ADMIN_ONLY},
    {'key': 'admin_printers', 'path': '/admin/printers', 'name': '打印机配置', 'group': '系统管理',
     'default_roles': _ADMIN_ONLY},
    {'key': 'admin_csi_alert', 'path': '/admin/csi-alert', 'name': '失败邮件通知', 'group': '系统管理',
     'default_roles': _ADMIN_ONLY},
    {'key': 'admin_email_log', 'path': '/admin/email-log', 'name': '邮件发送日志', 'group': '系统管理',
     'default_roles': _ADMIN_ONLY},

    # 非菜单页面：申请详情，权限跟随新建申请
    {'key': 'request_detail', 'path': '/request/<int:request_id>', 'name': '申请详情（非菜单）', 'group': '其他',
     'default_roles': _REQ_ADMIN},
]

PAGE_MAP = {p['key']: p for p in PAGES}
PAGE_KEYS = [p['key'] for p in PAGES]

# 页面分组顺序（管理页表格分组表头按此排列）
GROUPS = []
for _p in PAGES:
    if _p['group'] not in GROUPS:
        GROUPS.append(_p['group'])

# 永不拦截的路径（页面级权限只管「业务页面」）
WHITELIST_PREFIXES = ('/api/', '/static/')
WHITELIST_EXACT = ('/', '/login', '/logout', '/public/kanban', '/favicon.ico')
WHITELIST_PREFIXES_EXTRA = ('/approve/',)


def _compile(path):
    """把 Flask 风格 path 转成正则：<int:name> → \\d+，<name> → [^/]+"""
    chunks = []
    for part in re.split(r'(<[^>]+>)', path):
        if part.startswith('<') and part.endswith('>'):
            inner = part[1:-1]
            chunks.append(r'\d+' if inner.startswith('int:') else r'[^/]+')
        else:
            chunks.append(re.escape(part))
    return re.compile('^' + ''.join(chunks) + '/?$')


_COMPILED = [(p, _compile(p['path'])) for p in PAGES]


def get_page(key):
    """按 page_key 取页面定义；不存在返回 None"""
    return PAGE_MAP.get(key)


def match_path(path):
    """把请求路径匹配到页面定义；未注册路径返回 None（不误伤）"""
    if not path:
        return None
    path = str(path).split('?', 1)[0]
    for page, regex in _COMPILED:
        if regex.match(path):
            return page
    return None


def is_whitelisted(path):
    """白名单：登录/登出/根路径/公开看板/邮件审批链接/API/静态资源"""
    if not path:
        return True
    if path in WHITELIST_EXACT:
        return True
    for prefix in WHITELIST_PREFIXES + WHITELIST_PREFIXES_EXTRA:
        if path.startswith(prefix):
            return True
    return False


def pages_by_group():
    """按分组返回页面列表：[{'group': '新建', 'pages': [...]}, ...]（管理页渲染用）"""
    result = []
    for group in GROUPS:
        result.append({
            'group': group,
            'pages': [p for p in PAGES if p['group'] == group],
        })
    return result


def default_matrix():
    """默认矩阵：{role: {page_key: bool}}（含 admin，全 True）"""
    matrix = {}
    for role in ROLES:
        if role == 'admin':
            matrix[role] = {p['key']: True for p in PAGES}
        else:
            matrix[role] = {p['key']: role in p['default_roles'] for p in PAGES}
    return matrix
