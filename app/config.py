import os
import secrets
from dotenv import load_dotenv

# 加载 .env 文件（项目根目录）
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    
    # MySQL
    MYSQL_HOST = os.environ.get('MYSQL_HOST', '10.0.6.86')
    MYSQL_PORT = int(os.environ.get('MYSQL_PORT', 33306))
    MYSQL_USER = os.environ.get('MYSQL_USER', 'powerbi')
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '!Q1234567')
    MYSQL_DB = os.environ.get('MYSQL_DB', 'materialpickup')
    
    # 连接 URI
    SQLALCHEMY_DATABASE_URI = f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Session 安全配置
    SESSION_TYPE = 'filesystem'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_PERMANENT = True
    PERMANENT_SESSION_LIFETIME = 28800  # 8小时
    
    # LDAP / AD 认证配置
    LDAP_URL = os.environ.get('LDAP_URL', 'ldap://10.0.6.43:389')
    LDAP_BASE_DN = os.environ.get('LDAP_BASE_DN', 'DC=nai-group,DC=com')
    LDAP_BIND_DN = os.environ.get('LDAP_BIND_DN', 'jasonadmin@nai-group.com')
    LDAP_BIND_CREDENTIALS = os.environ.get('LDAP_BIND_CREDENTIALS', 'CHNX#000')
    LDAP_SEARCH_FILTER_TEMPLATE = os.environ.get('LDAP_SEARCH_FILTER', '(sAMAccountName={{username}})')
    
    # SMTP 邮件配置
    SMTP_SERVER = os.environ.get('SMTP_SERVER', 'mail.smtp2go.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 2525))
    SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
    SMTP_USER = os.environ.get('SMTP_USER', 'smtp@nai-group.com')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', 'R3UURg7LQ6A01UrZ')
    MAIL_FROM = os.environ.get('MAIL_FROM', 'materialkanban@nai-group.com')
    MAIL_FROM_NAME = os.environ.get('MAIL_FROM_NAME', '物料领取看板系统')
    
    # 系统基础 URL（用于邮件审批链接）
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')

    # Infor CSI / IDO API - 工单验证等实时查询
    CSI_TENANT = 'NAIGROUP_PRD'
    CSI_USERNAME = os.environ.get('CSI_USERNAME', '')
    CSI_PASSWORD = os.environ.get('CSI_PASSWORD', '')
    CSI_AUTH_BASIC = os.environ.get('CSI_AUTH_BASIC', '')
    CSI_TOKEN_URL = os.environ.get(
        'CSI_TOKEN_URL',
        f'https://mingle-sso.inforcloudsuite.com:443/{CSI_TENANT}/as/token.oauth2'
    )
    CSI_API_BASE = os.environ.get(
        'CSI_API_BASE',
        f'https://mingle-ionapi.inforcloudsuite.com/{CSI_TENANT}/api'
    )
    CSI_IDO_BASE = os.environ.get(
        'CSI_IDO_BASE',
        f'https://mingle-ionapi.inforcloudsuite.com/{CSI_TENANT}/CSI/IDORequestService'
    )

    # 站点配置
    SITE_CONFIG = {
        '310': '苏州工厂 (Suzhou Plant 1)',
        '410': '槟城工厂 (Penang Plant)'
    }

    # 根据站点映射 CSI Company
    SITE_CSI_COMPANY = {
        '310': 'NAIGROUP_PRD_310',
        '410': 'NAIGROUP_PRD_410',
    }

    # 站点对应的主仓库（WHLO），用于库存查询时按仓库过滤
    # 可通过环境变量 CSI_WHSE_310、CSI_WHSE_410 覆盖
    SITE_CSI_WHSE = {
        '310': os.environ.get('CSI_WHSE_310', 'S301'),
        '410': os.environ.get('CSI_WHSE_410', 'S401'),
    }
