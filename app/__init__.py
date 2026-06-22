from flask import Flask
from flask_session import Session
from app.config import Config


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 初始化服务端 Session
    Session(app)

    # 注册蓝图
    from app.routes.auth import auth_bp
    from app.routes.kanban import kanban_bp
    from app.routes.request_bp import request_bp
    from app.routes.approval import approval_bp
    from app.routes.warehouse import warehouse_bp
    from app.routes.admin import admin_bp
    from app.routes.validate import validate_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(request_bp)
    app.register_blueprint(approval_bp)
    app.register_blueprint(warehouse_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(validate_bp)

    # 页面路由
    from app.routes import pages
    app.register_blueprint(pages)

    return app
