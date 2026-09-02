from flask import Flask
from app.config import Config


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

    # 页面路由
    from app.routes import pages
    app.register_blueprint(pages)

    # 全局异常处理：API 错误统一返回 JSON，避免前端拿到 500 HTML 页面
    # 导致 "Unexpected token '<'" 解析错误（如数据库不可达时）
    from flask import jsonify

    @app.errorhandler(500)
    def handle_500(e):
        return jsonify({'success': False, 'message': f'服务器内部错误: {e}'}), 500

    @app.errorhandler(404)
    def handle_404(e):
        return jsonify({'success': False, 'message': '接口不存在'}), 404

    return app
