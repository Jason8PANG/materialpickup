#!/bin/bash
set -e

# ============================================
# 物料领取看板系统 - CentOS 部署脚本
# ============================================
APP_DIR=/home/app/material-kanban
SECRET_KEY="85c0cda9e2ec51ceffe7f26cec8ff2bc8bb62a5edcd82e466584621452f9545a"

echo "=== 开始部署物料领取看板系统 ==="

# 1. 检查必要命令
echo "[1/7] 检查依赖..."
command -v python3 >/dev/null 2>&1 || { echo "错误: 需要 python3"; exit 1; }
command -v rsync    >/dev/null 2>&1 || { echo "安装 rsync..."; yum install -y rsync; }
command -v pip3     >/dev/null 2>&1 || { echo "安装 python3-pip..."; yum install -y python3-pip; }

# 2. 创建部署目录
echo "[2/7] 创建部署目录 $APP_DIR ..."
mkdir -p "$APP_DIR"

# 3. 复制应用文件 (排除 pycache、测试、需求文档、flask_session)
echo "[3/7] 复制应用文件..."
rsync -av --delete \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.git' \
  --exclude='需求文档' \
  --exclude='tests' \
  --exclude='flask_session' \
  --exclude='*.pyc' \
  --exclude='deploy' \
  ./ "$APP_DIR/"

# 4. 创建虚拟环境并安装依赖
echo "[4/7] 创建 Python 虚拟环境..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate

echo "安装 Python 依赖..."
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
pip install gunicorn -i https://mirrors.aliyun.com/pypi/simple/

# 5. 初始化数据库
echo "[5/7] 初始化数据库..."
export MYSQL_PASSWORD='!Q1234567'
export SECRET_KEY="$SECRET_KEY"
python init_db.py

# 6. 创建 systemd 服务
echo "[6/7] 创建 systemd 服务..."
cat > /etc/systemd/system/material-kanban.service << 'SERVICEOF'
[Unit]
Description=Material Requisition Kanban System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/app/material-kanban
Environment="MYSQL_PASSWORD=!Q1234567"
Environment="SECRET_KEY=85c0cda9e2ec51ceffe7f26cec8ff2bc8bb62a5edcd82e466584621452f9545a"
ExecStart=/home/app/material-kanban/venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 'app:create_app()'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEOF

# 7. 启用并启动服务
echo "[7/7] 启动服务..."
systemctl daemon-reload
systemctl enable material-kanban
systemctl start material-kanban
sleep 2
systemctl status material-kanban --no-pager

echo ""
echo "=== 部署完成! ==="
echo "访问地址: http://$(curl -s ifconfig.me || hostname -I | awk '{print $1}'):5000"
echo ""
