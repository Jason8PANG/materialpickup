from flask import Blueprint, request, jsonify, session
from app.models import get_db_connection
from app.config import Config
from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPBindError, LDAPException
import logging

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


def authenticate_ldap(username, password):
    """通过 LDAP/AD 验证用户。返回 (success, display_name)"""
    if not username or not password:
        return False, '用户名和密码不能为空'

    # 清除域前缀/后缀
    username = username.strip()
    if '\\' in username:
        username = username.split('\\', 1)[1]
    if '@' in username:
        username = username.split('@')[0]

    try:
        server = Server(Config.LDAP_URL, get_info=ALL, connect_timeout=5)
        conn = Connection(server, user=Config.LDAP_BIND_DN,
                          password=Config.LDAP_BIND_CREDENTIALS, auto_bind=True)

        search_filter = Config.LDAP_SEARCH_FILTER_TEMPLATE.replace('{{username}}', username)
        conn.search(
            search_base=Config.LDAP_BASE_DN,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=['distinguishedName', 'displayName', 'mail', 'sAMAccountName']
        )

        if len(conn.entries) == 0:
            logger.warning(f'LDAP user not found: {username}')
            return False, '用户名或密码错误'

        entry = conn.entries[0]
        sAMAccountName = str(entry.sAMAccountName.value) if entry.sAMAccountName else username
        displayName = str(entry.displayName.value) if entry.displayName else sAMAccountName

        # 用用户凭据验证密码
        domain_parts = [
            dc.replace('DC=', '') for dc in Config.LDAP_BASE_DN.split(',')
            if dc.strip().upper().startswith('DC=')
        ]
        domain = '.'.join(domain_parts)
        user_upn = f'{sAMAccountName}@{domain}'

        user_conn = Connection(server, user=user_upn, password=password, auto_bind=True)
        user_conn.unbind()
        conn.unbind()

        logger.info(f'LDAP auth success: {username} ({displayName})')
        return True, displayName

    except LDAPBindError:
        return False, '用户名或密码错误'
    except LDAPException as e:
        logger.error(f'LDAP error: {e}')
        return False, '认证服务暂时不可用，请稍后重试'
    except Exception as e:
        logger.error(f'Auth unexpected error: {e}')
        return False, '登录失败，请联系管理员'


@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供登录信息'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'}), 400

    # 先查询角色映射
    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            'SELECT * FROM kr_role_mapping WHERE domain_account = %s AND is_active = 1',
            (username,)
        )
        mapping = cursor.fetchone()
        cursor.close()

    if not mapping:
        return jsonify({'success': False, 'message': '该用户没有系统访问权限，请联系管理员'}), 403

    # LDAP 验证密码
    ldap_ok, ldap_result = authenticate_ldap(username, password)
    if not ldap_ok:
        return jsonify({'success': False, 'message': ldap_result}), 401

    # 获取站点信息
    siteref = mapping.get('siteref')
    siteref_name = Config.SITE_CONFIG.get(siteref, '') if siteref else ''

    # admin 跨站
    if mapping['role'] == 'admin':
        available_sites = list(Config.SITE_CONFIG.keys())
        effective_siteref = siteref or '310'
    else:
        available_sites = [siteref] if siteref else []
        effective_siteref = siteref

    # 使用 LDAP 返回的 displayName
    display_name = ldap_result if isinstance(ldap_result, str) else (mapping.get('display_name') or mapping['domain_account'])

    session['user'] = {
        'username': mapping['domain_account'],
        'display_name': display_name,
        'role': mapping['role'],
        'email': mapping['email'],
        'siteref': effective_siteref,
        'original_siteref': siteref,
        'available_sites': available_sites
    }
    session.permanent = True

    return jsonify({
        'success': True,
        'user': session['user'],
        'siteref': effective_siteref,
        'siteref_name': siteref_name,
        'available_sites': available_sites
    })


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@auth_bp.route('/api/auth/me', methods=['GET'])
def me():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    siteref = user.get('siteref')
    siteref_name = Config.SITE_CONFIG.get(siteref, '') if siteref else ''
    available_sites = user.get('available_sites', list(Config.SITE_CONFIG.keys()) if user.get('role') == 'admin' else [siteref] if siteref else [])

    return jsonify({
        'success': True,
        'user': user,
        'siteref': siteref,
        'siteref_name': siteref_name,
        'available_sites': available_sites
    })


@auth_bp.route('/api/auth/switch-site', methods=['POST'])
def switch_site():
    """admin 切换站点"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] != 'admin':
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    target_site = data.get('siteref', '').strip()

    if target_site not in Config.SITE_CONFIG:
        return jsonify({'success': False, 'message': '无效的站点'}), 400

    # 更新 session 中的当前站点
    user['siteref'] = target_site
    session['user'] = user
    session.permanent = True

    return jsonify({
        'success': True,
        'siteref': target_site,
        'siteref_name': Config.SITE_CONFIG[target_site]
    })
