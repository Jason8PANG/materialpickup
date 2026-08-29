# -*- coding: utf-8 -*-
"""本地调试启动脚本：端口 5001，debug 模式（不自动重载）。
用法: python run_debug_5001.py
"""
from app import create_app

app = create_app()
app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)
