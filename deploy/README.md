# 物料领取看板系统 - 部署操作说明

## 部署前准备

### 服务器要求

| 项目 | 要求 |
|------|------|
| 操作系统 | CentOS 7+ |
| Python | 3.8+ |
| MySQL | 5.7+ (已提供: 10.0.6.86:33306) |
| Nginx | 可选，用于反向代理 |

### 数据库准备

数据库 `materialpickup` 已经在 MySQL 服务器 `10.0.6.86:33306` 上就绪，用户 `powerbi` 可用。

### 应用文件清单

将以下文件上传到 CentOS 服务器任一目录（如 `/root/`）：

| 文件 | 说明 |
|------|------|
| `deploy.sh` | 一键部署脚本 |
| `material-kanban.zip` | 应用代码压缩包 |

---

## 部署方式一：一键部署（推荐）

### 步骤 1：上传文件到服务器

```bash
# 在本地执行，将文件上传到服务器
scp deploy.sh material-kanban.zip root@10.0.6.134:/root/
```

### 步骤 2：SSH 登录服务器并执行部署

```bash
ssh root@10.0.6.134
cd /root/

# 解压代码
unzip -o material-kanban.zip -d /tmp/material-kanban-src

# 执行部署脚本
cd /tmp/material-kanban-src
chmod +x deploy.sh
./deploy.sh
```

### 步骤 3：验证部署

```bash
# 检查服务状态
systemctl status material-kanban

# 查看日志
journalctl -u material-kanban -n 50 --no-pager

# 测试 HTTP 访问
curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/
# 应返回 200（登录页）或 302（重定向）
```

---

## 部署方式二：分步手动部署

如果一键部署遇到问题，可按照以下步骤手动部署：

### 1. 安装系统依赖

```bash
yum install -y python3 python3-pip rsync nginx unzip
```

### 2. 创建部署目录并复制文件

```bash
APP_DIR=/home/app/material-kanban
mkdir -p $APP_DIR

# 解压代码包到临时目录后 rsync 过去
unzip -o /root/material-kanban.zip -d /tmp/material-kanban-src
rsync -av --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' /tmp/material-kanban-src/ $APP_DIR/
```

### 3. 创建虚拟环境并安装依赖

```bash
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
pip install gunicorn -i https://mirrors.aliyun.com/pypi/simple/
```

### 4. 初始化数据库

```bash
cd $APP_DIR
export MYSQL_PASSWORD='!Q1234567'
export SECRET_KEY='85c0cda9e2ec51ceffe7f26cec8ff2bc8bb62a5edcd82e466584621452f9545a'
source venv/bin/activate
python init_db.py
```

### 5. 创建 systemd 服务

```bash
cat > /etc/systemd/system/material-kanban.service << 'EOF'
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
EOF
```

### 6. 启动服务

```bash
systemctl daemon-reload
systemctl enable material-kanban
systemctl start material-kanban
systemctl status material-kanban
```

---

## 配置 Nginx 反向代理（可选）

如果希望通过 80 端口访问（而非直接 5000 端口），配置 Nginx：

```bash
# 复制 Nginx 配置
cp /home/app/material-kanban/deploy/nginx.conf /etc/nginx/conf.d/material-kanban.conf

# 检查配置并重载
nginx -t
systemctl reload nginx

# 关闭 SELinux（如开启）以允许 Nginx 代理
setsebool -P httpd_can_network_connect 1
```

配置后即可通过 `http://10.0.6.134/` 访问系统（无需端口号）。

---

## 常用运维命令

```bash
# 查看服务状态
systemctl status material-kanban

# 查看实时日志
journalctl -u material-kanban -f

# 查看最近 100 行日志
journalctl -u material-kanban -n 100 --no-pager

# 重启服务
systemctl restart material-kanban

# 停止服务
systemctl stop material-kanban

# 查看 Gunicorn 进程
ps aux | grep gunicorn

# 手动启动（调试模式，前台运行）
cd /home/app/material-kanban
source venv/bin/activate
export MYSQL_PASSWORD='!Q1234567'
export SECRET_KEY='85c0cda9e2ec51ceffe7f26cec8ff2bc8bb62a5edcd82e466584621452f9545a'
gunicorn -w 4 -b 0.0.0.0:5000 'app:create_app()'
```

---

## 防火墙配置

如果无法访问，检查服务器防火墙：

```bash
# 开放 5000 端口
firewall-cmd --add-port=5000/tcp --permanent
firewall-cmd --reload

# 或直接关闭防火墙（测试环境）
systemctl stop firewalld
```

---

## 常见问题

### Q: 数据库连接失败

确认 MySQL 可达且密码正确：

```bash
yum install -y mysql
mysql -h 10.0.6.86 -P 33306 -u powerbi -p materialpickup -e "SELECT 1"
```

### Q: Gunicorn 启动报错 ModuleNotFoundError

确认在虚拟环境内且依赖安装完整：

```bash
cd /home/app/material-kanban
source venv/bin/activate
pip list | grep -i flask
python -c "from app import create_app; print('OK')"
```

### Q: 静态文件 404

确认静态文件目录存在：

```bash
ls -la /home/app/material-kanban/app/static/
```

### Q: Session 无法保存

`flask_session/` 目录会被自动创建在 `WorkingDirectory` 下，确认目录可写。
