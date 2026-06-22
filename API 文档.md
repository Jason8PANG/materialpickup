# API 文档

## 通用约定

### 基础路径

所有 API 以 `/api/` 为前缀。当前无版本号前缀（预留：`/api/v1/`）。

### 鉴权方式

- 使用 **服务端 Session** 鉴权（Flask-Session，filesystem 存储）
- 登录成功后，服务端写入 Session，客户端自动携带 Cookie
- 所有受保护接口返回 `401` 表示未登录，`403` 表示权限不足

### 请求头

| 头 | 值 |
|---|-----|
| Content-Type | `application/json` |
| Cookie | `session=<session_id>` (浏览器自动携带) |

### 响应格式

所有接口返回 JSON 格式：

```json
{
  "success": true,
  "message": "success",
  ...业务数据...
}
```

### HTTP 状态码

| 状态码 | 说明 |
|--------|------|
| 200 | 请求成功 |
| 201 | 创建成功 |
| 400 | 请求参数错误 / 业务规则不满足 |
| 401 | 未登录 (Session 无效或已过期) |
| 403 | 权限不足 (当前角色不允许操作) |
| 404 | 资源不存在 |

---

## 1. 认证接口

### 1.1 用户登录

```
POST /api/auth/login
```

**请求体：**

```json
{
  "username": "worker1",
  "password": "worker1"
}
```

**鉴权：** 无需登录

**响应 (200)：**

```json
{
  "success": true,
  "user": {
    "username": "worker1",
    "display_name": "领料员李四",
    "role": "requester",
    "email": "worker1@example.com"
  }
}
```

**错误响应：**

```json
// 400 - 缺少参数
{ "success": false, "message": "用户名和密码不能为空" }

// 401 - 密码错误
{ "success": false, "message": "用户名或密码错误" }

// 403 - 无访问权限
{ "success": false, "message": "该用户没有系统访问权限，请联系管理员" }
```

---

### 1.2 退出登录

```
POST /api/auth/logout
```

**鉴权：** 需要登录

**响应 (200)：**

```json
{ "success": true }
```

---

### 1.3 获取当前用户信息

```
GET /api/auth/me
```

**鉴权：** 需要登录

**响应 (200)：**

```json
{
  "success": true,
  "user": {
    "username": "worker1",
    "display_name": "领料员李四",
    "role": "requester",
    "email": "worker1@example.com"
  }
}
```

**响应 (401)：**

```json
{ "success": false, "message": "未登录" }
```

---

## 2. 看板接口

### 2.1 获取看板卡片 (按状态分组)

```
GET /api/kanban/cards
```

**鉴权：** 需要登录

**说明：** 返回所有进行中（未完成/未驳回）的单据，按 `pending_approval`(待审批)、`pending_prep`(待备料)、`prepping`(备料中)、`short`(缺料)、`ready_pickup`(待取料) 五种状态分组。

每个卡片会根据当前用户角色自动计算可操作按钮 (`actions` 字段)：

| 角色 | 待审批 | 待备料 | 备料中 | 缺料 | 待取料 |
|------|--------|--------|--------|------|--------|
| admin | approve, reject | assign_worker, start_prep | complete_prep, short | — | sign |
| supervisor | approve, reject | — | — | — | — |
| warehouse | — | assign_worker, start_prep | complete_prep, short | — | — |
| requester | — | — | short | — | sign |

**响应 (200)：**

```json
{
  "success": true,
  "groups": {
    "pending_approval": {
      "title": "待审批",
      "count": 2,
      "cards": [
        {
          "id": 1,
          "job_order": "WO-2024-001",
          "part_number": "PN-10086",
          "quantity": 100.00,
          "price": 12.50,
          "total_amount": 1250.00,
          "stock_qty": 500.00,
          "replenish_reason": "报废",
          "requester": "worker1",
          "request_time": "2024-01-15 09:00:00",
          "status": "pending_approval",
          "status_label": "待审批",
          "actions": ["approve", "reject"]
        }
      ]
    },
    "pending_prep": { "title": "待备料", "count": 0, "cards": [] },
    "prepping": { "title": "备料中", "count": 0, "cards": [] },
    "short": { "title": "缺料", "count": 0, "cards": [] },
    "ready_pickup": { "title": "待取料", "count": 0, "cards": [] }
  },
  "status_order": [
    "pending_approval",
    "pending_prep",
    "prepping",
    "short",
    "ready_pickup"
  ],
  "status_labels": {
    "pending_approval": "待审批",
    "pending_prep": "待备料",
    "prepping": "备料中",
    "short": "缺料",
    "ready_pickup": "待取料",
    "rejected": "已驳回",
    "completed": "已完成"
  }
}
```

---

## 3. 申请单接口

### 3.1 提交领料申请

```
POST /api/requests
```

**鉴权：** `requester` 或 `admin`

**请求体：**

```json
{
  "job_order": "WO-2024-001",
  "part_number": "PN-10086",
  "quantity": 100,
  "price": 12.50,
  "total_amount": 1250.00,
  "stock_qty": 500,
  "replenish_reason": "报废",
  "replenish_reason_other": ""
}
```

**必填字段：** `job_order`, `part_number`, `quantity`

**补料原因选项：** `报废` / `不良` / `来料不足` / `其他`

**响应 (200)：**

```json
{
  "success": true,
  "id": 1,
  "message": "申请提交成功"
}
```

> 申请提交后，系统自动向所有已激活且有邮箱的主管发送审批通知邮件。

---

### 3.2 获取申请单详情

```
GET /api/requests/{id}
```

**鉴权：** 需要登录

**响应 (200)：**

```json
{
  "success": true,
  "request": {
    "id": 1,
    "job_order": "WO-2024-001",
    "part_number": "PN-10086",
    "quantity": 100.00,
    "price": 12.50,
    "total_amount": 1250.00,
    "status": "pending_approval",
    "status_label": "待审批",
    "requester": "worker1",
    "request_time": "2024-01-15 09:00:00",
    "supervisor": null,
    "approve_time": null,
    "approve_comment": null,
    "warehouse_operator": null,
    "short_reason": null,
    "short_time": null,
    "signer": null,
    "sign_time": null,
    "remark": null,
    "created_at": "2024-01-15 09:00:00",
    "updated_at": "2024-01-15 09:00:00"
  },
  "logs": [
    {
      "id": 1,
      "request_id": 1,
      "operator": "worker1",
      "action": "SUBMIT",
      "detail": "提交领料申请: PN-10086 x 100.0",
      "ip_address": "10.0.0.1",
      "created_at": "2024-01-15 09:00:00"
    }
  ]
}
```

---

### 3.3 查询申请单列表

```
GET /api/requests?status=&job_order=&part_number=&page=1&size=20
```

**鉴权：** 需要登录

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 状态筛选 |
| job_order | string | 否 | 工单号模糊搜索 |
| part_number | string | 否 | 物料号模糊搜索 |
| page | int | 否 | 页码，默认 1 |
| size | int | 否 | 每页条数，默认 20 |

**响应 (200)：**

```json
{
  "success": true,
  "data": [ ...申请单数组... ],
  "total": 50,
  "page": 1,
  "size": 20,
  "total_pages": 3
}
```

---

### 3.4 查询历史单据

```
GET /api/requests/history?status=&job_order=&part_number=&date_from=&date_to=&page=1&size=20
```

**鉴权：** 需要登录

**说明：** 仅查询已完成 (`completed`) 和已驳回 (`rejected`) 的单据。

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | `completed` / `rejected` |
| job_order | string | 否 | 工单号模糊搜索 |
| part_number | string | 否 | 物料号模糊搜索 |
| date_from | string | 否 | 开始日期 (含)，格式 `YYYY-MM-DD` |
| date_to | string | 否 | 结束日期 (含)，格式 `YYYY-MM-DD` |
| page | int | 否 | 页码，默认 1 |
| size | int | 否 | 每页条数，默认 20 |

**响应 (200)：**

```json
{
  "success": true,
  "data": [ ...申请单数组... ],
  "total": 100,
  "page": 1,
  "size": 20,
  "total_pages": 5
}
```

---

### 3.5 查询未完成单据 (仓库用)

```
GET /api/requests/pending?status=&job_order=&page=1&size=20
```

**鉴权：** 需要登录

**说明：** 查询所有未完成单据（排除 `completed` 和 `rejected`）。

**响应格式：** 同 3.3 / 3.4

---

## 4. 审批接口

### 4.1 审批通过 (系统内)

```
POST /api/requests/{id}/approve
```

**鉴权：** `supervisor` 或 `admin`

**前置条件：** 单据状态为 `pending_approval`

**请求体：**

```json
{
  "comment": "同意，请尽快备料"
}
```

**响应 (200)：**

```json
{ "success": true, "message": "审批通过" }
```

---

### 4.2 驳回申请 (系统内)

```
POST /api/requests/{id}/reject
```

**鉴权：** `supervisor` 或 `admin`

**前置条件：** 单据状态为 `pending_approval`

**请求体：**

```json
{
  "comment": "库存充足，无需补料"
}
```

> **注意：** 驳回时 `comment` 字段为必填。

**响应 (200)：**

```json
{ "success": true, "message": "已驳回" }
```

---

### 4.3 邮件令牌验证

```
GET /api/approve/token/{token}
```

**鉴权：** 无需登录 (但操作时通过令牌验证)

**说明：** 验证邮件审批链接中的一次性令牌，返回对应的申请单详情。

**响应 (200)：**

```json
{
  "success": true,
  "request": { ...申请单详情... }
}
```

**响应 (404)：**

```json
{ "success": false, "message": "令牌无效或已过期" }
```

> 令牌有效期 72 小时，使用后失效。

---

### 4.4 通过邮件令牌执行审批

```
POST /api/approve/token/{token}/action
```

**鉴权：** 无需登录 (但通过令牌验证身份)

**请求体：**

```json
// 审批通过
{ "action": "approve", "comment": "同意" }

// 驳回
{ "action": "reject", "comment": "理由不充分，驳回" }
```

> 驳回时 `comment` 为必填。

**响应 (200)：**

```json
{ "success": true, "message": "操作成功" }
```

---

## 5. 仓库操作接口

### 5.1 开始备料

```
POST /api/requests/{id}/start-prep
```

**鉴权：** `warehouse` 或 `admin`

**前置条件：** 单据状态为 `pending_prep`

**请求体：**

```json
{
  "operator": "warehouse1"
}
```

> `operator` 可选。若提供则设置备料员；否则使用当前登录用户作为备料员。

**响应 (200)：**

```json
{ "success": true, "message": "开始备料" }
```

---

### 5.2 完成备料

```
POST /api/requests/{id}/complete-prep
```

**鉴权：** `warehouse` 或 `admin`

**前置条件：** 单据状态为 `prepping`

**响应 (200)：**

```json
{ "success": true, "message": "备料完成" }
```

---

### 5.3 登记缺料

```
POST /api/requests/{id}/short
```

**鉴权：** `warehouse` 或 `admin`

**前置条件：** 单据状态为 `prepping` 或 `pending_prep`

**请求体：**

```json
{
  "short_reason": "该批次来料不足，缺 50 个"
}
```

**响应 (200)：**

```json
{ "success": true, "message": "缺料登记成功" }
```

---

### 5.4 指定/切换备料员

```
PUT /api/requests/{id}/assign-worker
```

**鉴权：** `warehouse` 或 `admin`

**前置条件：** 单据状态为 `pending_prep` 或 `prepping`

**请求体：**

```json
{
  "warehouse_operator": "warehouse2"
}
```

**响应 (200)：**

```json
{ "success": true, "message": "已指定备料员" }
```

---

### 5.5 签字确认完成

```
POST /api/requests/{id}/sign
```

**鉴权：** `requester`、`warehouse` 或 `admin`

**前置条件：** 单据状态为 `ready_pickup` 或 `short`

**响应 (200)：**

```json
{ "success": true, "message": "签字确认成功" }
```

> 签字确认后，单据状态变更为 `completed`，看板中不再显示该卡片。

---

## 6. 账号管理接口

### 6.1 获取所有角色映射

```
GET /api/role-mappings
```

**鉴权：** `admin`

**响应 (200)：**

```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "domain_account": "worker1",
      "display_name": "领料员李四",
      "role": "requester",
      "email": "worker1@example.com",
      "password_hash": null,
      "is_active": 1,
      "remark": "领料员",
      "created_at": "2024-01-01 00:00:00",
      "updated_at": "2024-01-01 00:00:00"
    }
  ]
}
```

---

### 6.2 新增角色映射

```
POST /api/role-mappings
```

**鉴权：** `admin`

**请求体：**

```json
{
  "domain_account": "newuser",
  "display_name": "新用户",
  "role": "requester",
  "email": "newuser@example.com",
  "is_active": 1,
  "remark": "新增领料员"
}
```

**必填字段：** `domain_account`, `role`

**有效角色：** `admin` / `requester` / `supervisor` / `warehouse`

**响应 (201)：**

```json
{ "success": true, "id": 10, "message": "创建成功" }
```

---

### 6.3 编辑角色映射

```
PUT /api/role-mappings/{id}
```

**鉴权：** `admin`

**请求体：** (支持部分更新)

```json
{
  "display_name": "新显示名",
  "role": "supervisor",
  "email": "new@example.com",
  "is_active": 0,
  "remark": "已离职"
}
```

**响应 (200)：**

```json
{ "success": true, "message": "更新成功" }
```

---

### 6.4 删除角色映射

```
DELETE /api/role-mappings/{id}
```

**鉴权：** `admin`

**响应 (200)：**

```json
{ "success": true, "message": "删除成功" }
```

---

## 7. 日志接口

### 7.1 查询操作日志

```
GET /api/logs?request_id=&operator=&action=&page=1&size=50
```

**鉴权：** 需要登录

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| request_id | int | 否 | 按申请单 ID 筛选 |
| operator | string | 否 | 操作用户模糊搜索 |
| action | string | 否 | 操作类型精确匹配 |
| page | int | 否 | 页码，默认 1 |
| size | int | 否 | 每页条数，默认 50 |

**操作类型枚举：** `SUBMIT` / `APPROVE` / `REJECT` / `START_PREP` / `COMPLETE_PREP` / `SHORT` / `SIGN` / `ASSIGN_WORKER`

**响应 (200)：**

```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "request_id": 1,
      "operator": "worker1",
      "action": "SUBMIT",
      "detail": "提交领料申请: PN-10086 x 100.0",
      "ip_address": "10.0.0.1",
      "created_at": "2024-01-15 09:00:00"
    }
  ],
  "total": 100,
  "page": 1,
  "size": 50,
  "total_pages": 2
}
```

---

## 附：状态枚举

| 状态值 | 标签 | 说明 |
|--------|------|------|
| `pending_approval` | 待审批 | 申请已提交，等待主管审批 |
| `pending_prep` | 待备料 | 审批通过，等待仓库备料 |
| `prepping` | 备料中 | 仓库正在备料 |
| `short` | 缺料 | 备料时发现库存不足 |
| `ready_pickup` | 待取料 | 备料完成，等待领料员取料签字 |
| `completed` | 已完成 | 签字确认，流程结束 |
| `rejected` | 已驳回 | 主管驳回申请 |

### 状态流转图

```
                         +---> pending_prep ---> prepping ---> ready_pickup ---> completed
                         |        ↑                  |              ↑
pending_approval --------+        |                  +---> short ---+
    |                            assign_worker    缺料登记      签字确认
    +---> rejected
```
