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
    MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'root07')
    MYSQL_DB = os.environ.get('MYSQL_DB', 'materialpickup')

    # 外部系统集成 API Key（naiwiptrack 等调用 /api/external/* 时须带 X-API-Key 请求头）
    EXTERNAL_API_KEY = os.environ.get('EXTERNAL_API_KEY', 'NAI-WIPTRACK-2026')

    # 确认人密码回退（cutting_confirm_user 表未命中时使用）
    CUTTING_CONFIRM_PASSWORD = os.environ.get('CUTTING_CONFIRM_PASSWORD', '')
    CUTTING_CONFIRM_NAME = os.environ.get('CUTTING_CONFIRM_NAME', '')

    # 连接 URI
    SQLALCHEMY_DATABASE_URI = f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Session 安全配置（Flask 原生 signed-cookie session，无服务端文件存储）
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

    # ============================================================ #
    #  线卷标签打印配置（线卷全库存管理）
    # ============================================================ #
    # 目标打印机名称；空则取系统默认打印机
    LABEL_PRINTER_NAME = os.environ.get('LABEL_PRINTER_NAME', '')
    # 打印通道：gdi（GDI 驱动打印，通用）/ raw_zpl（ZPL 指令直发，Zebra）/
    #           raw_tspl（TSPL 指令直发，TSC）/ gateway（Windows 打印网关代理）
    LABEL_PRINT_CHANNEL = os.environ.get('LABEL_PRINT_CHANNEL', 'gdi')
    # 打印机型号（raw 通道选择指令模板用，如 'Zebra'/'TSC'），当前未强制使用
    LABEL_PRINTER_MODEL = os.environ.get('LABEL_PRINTER_MODEL', '')
    # Windows 打印网关（方案 C）：主后端跑 Linux 时，转发打印请求到装有驱动+pywin32 的 Windows 服务
    LABEL_PRINT_GATEWAY_URL = os.environ.get('LABEL_PRINT_GATEWAY_URL', '')
    LABEL_PRINT_GATEWAY_TOKEN = os.environ.get('LABEL_PRINT_GATEWAY_TOKEN', '')

    # ============================================================ #
    #  线卷单位换算系数表（线卷全库存管理，文档 2.3.1）
    # ============================================================ #
    # 换算公式：converted_length = out_length(mm) ÷ 系数
    # out_length 为出库登记的原始录入值（单位固定 mm）；
    # 系数按物料在 CSI 中的单位（kr_wire_coil.unit）确定。
    # 未收录 / CSI 单位为空时 converted_length / converted_unit 置 NULL 并给出警告（不阻断登记）。
    UNIT_CONVERT_FACTOR = {
        'M': 1000,     # 米：1 m = 1000 mm
        'FT': 304.8,   # 英尺：1 ft = 304.8 mm
        'CM': 10,      # 厘米：1 cm = 10 mm
        'IN': 25.4,    # 英寸：1 in = 25.4 mm
    }
