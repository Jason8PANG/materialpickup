from flask import Flask
import sys
from app.config import Config

# 模块级标志：防止 Flask debug reload / 多次 create_app 重复起线程
_CSI_SYNC_THREAD_STARTED = False
_CSI_ALERT_THREAD_STARTED = False


def _start_csi_sync_thread():
    """CSI 库位转移同步器：CSI_SYNC_ENABLED=true 时以守护线程常驻。

    注意：gunicorn -w 4 时每个 worker 都会起一条线程，重复提交由
    kr_csi_interface 的条件 UPDATE 抢占（status→SUBMITTING）保证不会发生。
    """
    global _CSI_SYNC_THREAD_STARTED
    if _CSI_SYNC_THREAD_STARTED or not Config.CSI_SYNC_ENABLED:
        return
    _CSI_SYNC_THREAD_STARTED = True
    try:
        import threading
        from app.csi_sync_worker import main as csi_sync_main
        threading.Thread(target=csi_sync_main, name='csi-sync', daemon=True).start()
    except Exception as e:  # 同步器起不来不影响 Web 服务
        print(f'[CSI 同步] 启动后台线程失败: {e}')


def _start_csi_alert_thread():
    """CSI 失败记录定时邮件调度器：CSI_ALERT_ENABLED=true 时以守护线程常驻。

    注意：gunicorn -w 4 时 create_app() 会执行 4 次，本线程也就有 4 份；
    重复发信由 kr_csi_alert_log 的唯一键 (send_date, slot, siteref, api_name)
    在 INSERT IGNORE 时抢占（rowcount=1 的那份才发），不依赖内存标志。
    这里的内存标志只防同一进程内 Flask reload 重复起线程。
    测试进程（pytest）不起线程：避免后台线程用被 mock 的 pymysql 干扰用例。
    """
    global _CSI_ALERT_THREAD_STARTED
    if _CSI_ALERT_THREAD_STARTED or not Config.CSI_ALERT_ENABLED:
        return
    if 'pytest' in sys.modules:
        return
    _CSI_ALERT_THREAD_STARTED = True
    try:
        import threading
        from app.csi_alert_scheduler import main as csi_alert_main
        threading.Thread(target=csi_alert_main, name='csi-alert', daemon=True).start()
    except Exception as e:  # 邮件调度器起不来不影响 Web 服务
        print(f'[CSI 告警] 启动后台线程失败: {e}')


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 会话：使用 Flask 原生 signed-cookie session（无服务端文件存储，
    # 避免 flask_session 文件缓存在 Windows 沙箱下清理阻塞导致请求卡死）
    # 注：移除 flask_session 的 Session(app)；登录态存于签名的客户端 cookie。

    # 注册蓝图
    from app.routes.auth import auth_bp
    from app.routes.kanban import kanban_bp
    from app.routes.request_bp import request_bp
    from app.routes.approval import approval_bp
    from app.routes.warehouse import warehouse_bp
    from app.routes.admin import admin_bp
    from app.routes.validate import validate_bp
    from app.routes.coil import coil_bp
    from app.routes.return_bp import return_bp
    from app.routes.wire import wire_bp
    from app.routes.external import external_bp
    from app.routes.cutting import cutting_bp
    from app.routes.csi_interface import csi_interface_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(request_bp)
    app.register_blueprint(approval_bp)
    app.register_blueprint(warehouse_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(validate_bp)
    app.register_blueprint(coil_bp)
    app.register_blueprint(return_bp)
    app.register_blueprint(wire_bp)
    app.register_blueprint(external_bp)
    app.register_blueprint(cutting_bp)
    app.register_blueprint(csi_interface_bp)

    # 全局模板上下文：站点编号 → 双语站点名（供导航切换站点菜单等渲染）
    @app.context_processor
    def _inject_site_config():
        return {'site_config': Config.SITE_CONFIG}

    # 页面路由
    from app.routes import pages
    app.register_blueprint(pages)

    # ===== 角色—页面权限（页面级：菜单显示 + 直输 URL 拦截） =====
    # 矩阵存 kr_role_page_permission，读服务带 60s 进程内缓存（app/services/permission_service.py）。
    # 只拦 GET 页面请求：/api/* 仍由各接口原有的角色校验负责，页面内按钮权限保持原硬编码不变。
    from flask import render_template, request, session
    from app.page_registry import ROLE_LABELS, match_path
    from app.services.permission_service import (
        can_access, can_access_page_for_session,
    )
    from app.services.operation_log import log_operation

    # 模板里用 can_access_page('kanban') 判定菜单可见性（自动取当前登录用户）
    app.jinja_env.globals['can_access_page'] = can_access_page_for_session
    app.jinja_env.globals['role_label'] = lambda role: ROLE_LABELS.get(role, role)

    @app.before_request
    def _enforce_page_permission():
        if request.method != 'GET':
            return None
        path = request.path
        if path.startswith('/api/') or path.startswith('/static/'):
            return None
        user = session.get('user')
        if not user:
            return None                      # 未登录交给各页面路由跳登录页
        try:
            if can_access(user, path):
                return None
        except Exception as e:               # 判定异常时放行，绝不让权限服务拖垮页面
            print(f'[权限] 页面判定异常（放行）{path}: {e}')
            return None

        page = match_path(path)
        page_key = page['key'] if page else ''
        role = user.get('role') or ''
        # 审计：被拦截的直输 URL 访问（日志失败不影响响应）
        try:
            log_operation(
                'permission', 'deny',
                target_type='page', target_key=path,
                operator=user.get('username') or 'SYSTEM',
                result='fail',
                detail={'role': role, 'page_key': page_key, 'path': path},
            )
        except Exception:
            pass
        return render_template(
            '403.html',
            page_key=page_key,
            page_name=page['name'] if page else path,
            role=role,
            role_label=ROLE_LABELS.get(role, role or '未知'),
        ), 403

    # 全局异常处理：API 错误统一返回 JSON，避免前端拿到 500 HTML 页面
    # 导致 "Unexpected token '<'" 解析错误（如数据库不可达时）
    from flask import jsonify

    @app.errorhandler(500)
    def handle_500(e):
        return jsonify({'success': False, 'message': f'服务器内部错误: {e}'}), 500

    @app.errorhandler(404)
    def handle_404(e):
        return jsonify({'success': False, 'message': '接口不存在'}), 404

    # CSI 库位转移同步器（常驻守护线程，CSI_SYNC_ENABLED=false 时不启动）
    _start_csi_sync_thread()

    # CSI 失败记录定时邮件（09:00/12:00/15:00/17:00，常驻守护线程）
    _start_csi_alert_thread()

    return app
