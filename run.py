#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
物料领取看板系统 - 应用入口
"""
import sys
import os

# 解决 Windows 控制台编码问题
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

app = create_app()

if __name__ == '__main__':
    print('=' * 50)
    print('Material Requisition Kanban System starting...')
    print('Access URL: http://0.0.0.0:5000')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)
