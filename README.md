# 物料领取看板系统 (Material Requisition Kanban)

覆盖领料申请、主管审批、仓库备料、缺料处理、签字确认全业务流程的物料领取看板系统，实现生产领料员、生产主管、仓库人员之间的高效协作与信息透明。

---

## 业务背景

生产车间日常需要从仓库领取物料（补料），涉及多个角色的协作：

1. **领料员** 发现物料不足时，提交领料申请
2. **生产主管** 审批该申请是否合理
3. **仓库人员** 根据审批通过的申请进行备料
4. **领料员** 到仓库取料并签字确认

传统方式依赖纸质单据、口头沟通、电话确认，效率低下且信息不透明。本系统通过看板形式将整个流程可视化，并支持邮件审批，极大提升协作效率。

### 核心流程

```
领料员提交申请
     ↓
主管审批 ──→ 驳回 → 结束
     ↓ 通过
仓库备料 ──→ 缺料登记 → 等待处理
     ↓ 完成
领料员取料签字
     ↓
完成归档
```

### 看板状态流转

```
待审批 ─→ 待备料 ─→ 备料中 ─→ 待取料 ─→ 已完成
                  ↘ 缺料 ↗             (看板隐藏)
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | Python Flask 3.1 |
| 数据库 | MySQL 5.7+ / 8.0 |
| ORM / 数据库驱动 | PyMySQL (原生 SQL) |
| Session 存储 | Flask-Session (filesystem) |
| 前端 UI | Bootstrap 5 + 响应式布局 |
| 认证方式 | LDAP 域账号 (已预留) / 本地密码验证 |
| 邮件服务 | SMTP |
| 部署 | Gunicorn + Nginx 反向代理 |

---

## 角色说明

| 角色 | 职责 | 核心操作 |
|------|------|----------|
| **admin** | 系统管理员 | 账号映射管理、全系统查看、所有操作权限 |
| **requester** | 领料员 | 提交领料申请、查看看板、取料签字确认 |
| **supervisor** | 生产主管 | 审批/驳回领料申请 (系统内 + 邮件链接) |
| **warehouse** | 仓库人员 | 查询未完成单据、备料操作、缺料登记、签字确认 |

---

## 功能清单一览

| 编号 | 功能 | 优先级 | 角色 |
|------|------|--------|------|
| F-001 | LDAP 域账号登录 | P0 | 全部 |
| F-002 | 看板主页 (Kanban Board) | P0 | 全部 |
| F-003 | 提交领料申请 | P0 | requester |
| F-004 | 主管审批 (系统内) | P0 | supervisor |
| F-005 | 主管审批 (邮件超链接) | P0 | supervisor |
| F-006 | 仓库查询未完成单据 | P0 | warehouse |
| F-007 | 仓库备料状态更新 | P0 | warehouse |
| F-008 | 缺料登记反馈 | P0 | warehouse |
| F-009 | 取料签字确认 | P0 | warehouse |
| F-010 | 账号-角色映射管理 | P1 | admin |
| F-011 | 历史单据归档查询 | P1 | 全部 |
| F-012 | 审批邮件发送 | P1 | 自动 |
| F-013 | 看板响应式布局 | P1 | 全部 |
| F-014 | 申请单编辑/撤回 | P2 | requester |
| F-015 | 操作日志审计 | P2 | 全部 |

---

## 项目目录结构

```
material-kanban/
├── run.py                    # 应用入口
├── app/
│   ├── __init__.py           # Flask 应用工厂
│   ├── config.py             # 全局配置
│   ├── init_db.py            # 数据库初始化脚本
│   ├── models/               # 数据模型 & 数据库连接
│   ├── routes/               # 路由模块
│   │   ├── auth.py           # 认证 (登录/登出)
│   │   ├── kanban.py         # 看板数据
│   │   ├── request_bp.py     # 申请单 CRUD
│   │   ├── approval.py       # 审批 & 邮件令牌
│   │   ├── warehouse.py      # 仓库操作
│   │   └── admin.py          # 管理员 & 日志
│   ├── services/             # 业务服务
│   │   ├── ldap_service.py   # LDAP 认证 (预留)
│   │   └── email_service.py  # 邮件发送
│   ├── utils/                # 工具函数
│   ├── templates/            # Jinja2 模板
│   └── static/               # CSS / JS / 静态资源
├── tests/                    # 单元测试
├── requirements.txt          # Python 依赖
├── README.md                 # 本文档
├── API 文档.md               # API 接口参考
└── 部署说明.md               # 部署运维手册
```

---

## 数据库表

| 表名 | 说明 |
|------|------|
| `kr_material_request` | 领料申请单主表 |
| `kr_role_mapping` | 域账号-角色映射表 |
| `kr_approval_token` | 邮件审批一次性令牌表 |
| `kr_operation_log` | 操作日志表 |

---

## 快速启动

### 前置条件

- Python 3.9+
- MySQL 5.7+ / 8.0
- pip / venv

### 步骤

```bash
# 1. 克隆项目
cd /path/to/project

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate    # Linux / macOS
# venv\Scripts\activate     # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量 (或直接编辑 app/config.py)
export MYSQL_HOST=10.0.6.86
export MYSQL_PORT=33306
export MYSQL_USER=powerbi
export MYSQL_PASSWORD=your_password
export MYSQL_DB=materialpickup
export SECRET_KEY=your-secret-key-here

# 5. 初始化数据库
python -m app.init_db

# 6. 启动开发服务器
python run.py

# 浏览器访问 http://localhost:5000
```

### 初始账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin | 系统管理员 |
| supervisor1 | supervisor1 | 生产主管 |
| worker1 | worker1 | 领料员 |
| warehouse1 | warehouse1 | 仓库人员 |
| worker2 | worker2 | 领料员 |
| warehouse2 | warehouse2 | 仓库人员 |

> 首次登录密码与用户名相同，请在系统中修改密码。

---

## 许可证

本项目为内部生产管理系统。
