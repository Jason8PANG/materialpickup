"""
LDAP 认证服务
当前使用本地简单密码验证替代，保留 LDAP 接口方便后续对接
"""
from app.config import Config


def ldap_authenticate(username, password):
    """
    LDAP 认证 - 预留接口
    
    实际 LDAP 对接时，使用 python-ldap 库实现：
    
    import ldap
    conn = ldap.initialize(Config.LDAP_SERVER)
    conn.set_option(ldap.OPT_REFERRALS, 0)
    user_dn = f"{Config.LDAP_DOMAIN}\\{username}"
    try:
        conn.simple_bind_s(user_dn, password)
        return {'username': username, 'dn': user_dn}
    except ldap.INVALID_CREDENTIALS:
        return None
    
    当前返回 None 表示 LDAP 验证失败，由 auth.py 回退到本地密码验证
    """
    return None
