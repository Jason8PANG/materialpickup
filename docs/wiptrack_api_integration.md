# naiwiptrack 对接 API 文档（物料领取看板外部集成）

> 目的：把 naiwiptrack 中**直接读写 materialpickup 数据库**的动作，全部改为通过本 API 调用。
> 看板服务 Base URL：`http://<看板服务器>:5001`（本地调试 http://localhost:5001）

---

## 1. 认证

所有请求须带请求头：

```
X-API-Key: NAI-WIPTRACK-2026
```

未带/错误 → `401 {"success": false, "error": "API Key 无效"}`

---

## 2. 接口清单

| # | 方法 | 路径 | 用途 |
|:--|:--|:--|:--|
| 1 | GET | `/api/external/coils/<coil_id>` | 查卷标（含剩余长度） |
| 2 | POST | `/api/external/consumption` | **消耗登记**（裁剪，stage=complete） |
| 3 | POST | `/api/external/consumption/scrap` | **报废登记** |
| 4 | POST | `/api/external/cutting-check` | **首件/末件检查登记** |
| 5 | GET | `/api/external/consumption?coil_id=&job_order=` | 消耗查询 |
| 6 | GET | `/api/external/cutting-check?job_order=&part_number=&check_type=` | 首末件检查查询 |
| 7 | GET | `/api/external/cutting-ref?finished_part=` | 裁剪参数查询 |
| 8 | POST | `/api/external/confirm-user` | **确认人密码校验**（cutting_confirm_user） |
| 9 | DELETE | `/api/external/consumption/<id>` | **删除消耗记录**（需确认人密码） |
| 10 | GET | `/api/external/consumption/list` | **消耗记录分页列表** |
| 11 | GET | `/api/external/coils/list` | **卷标信息分页列表** |

---

## 3. 接口详情

### 3.1 查卷标
```
GET /api/external/coils/260826002
```
响应：
```json
{
  "success": true,
  "data": {
    "coil_id": "260826002", "part_number": "A080507", "lot_no": "202607070000035",
    "unit": "FT", "coil_length": 100.0,
    "status": "in_shop", "status_label": "在车间",
    "request_id": 1675, "used_mm": 0.0, "remain_mm": 30480.0, "remain_orig": 100.0
  }
}
```
> 替代 wiptrack 中 `SELECT * FROM materialpickup.kr_wire_coil WHERE coil_id=?` + 剩余计算

### 3.2 消耗登记（裁剪）
```
POST /api/external/consumption
Content-Type: application/json

{
  "coil_id": "260826002",
  "part_number": "A080507",
  "job_order": "J000004113-0000",
  "job_part_number": "可选",
  "shear_qty": 24,               // 消耗数量（必须 > 0）
  "actual_shear_length": 152.4,  // 实际剪切长度 mm（必须 > 0）
  "scrap_length_actual": 0,      // 报废长度 mm（可选，默认 0）
  "wire_spec": "可选", "color": "可选",
  "cut_length_mm": "可选", "length_tolerance": "可选（如 ±0.5）",
  "shear_equipment": "可选", "shear_device_no": "可选", "actual_shear_equipment": "可选",
  "operator": "tester", "checker": "可选", "is_manual": false, "remark": "可选",
  "force": false                  // 超限时是否强制（确认人授权后 true）
}
```
**业务校验（服务端执行，wiptrack 不再直查库）**：
- 卷标存在 + 状态 `in_shop`，否则 400 `卷标ID的状态不正确`
- 出库长度 = `shear_qty × actual_shear_length + scrap_length_actual`
- 超限（`已用 + 本次 > 卷长×系数`）且 `force=false` → `400 {"needForce": true, "error": "长度超限，需确认人授权"}`
  - wiptrack 收到 needForce 后弹出确认人授权，再带 `force:true` 重发
- 单位换算：`converted_length = out_length ÷ 系数`（FT=304.8/M=1000）

成功响应：
```json
{
  "success": true, "message": "消耗记录已保存", "id": 123,
  "consume_type": "consumption", "out_length": 3657.6,
  "converted_length": 12.0, "converted_unit": "FT",
  "remaining_mm": 26822.4
}
```

### 3.3 报废登记
```
POST /api/external/consumption/scrap
{
  "coil_id": "260826002", "part_number": "A080507",
  "job_order": "J000004113-0000",
  "out_length": 500,          // 报废长度 mm（必须 > 0）
  "operator": "tester", "remark": "可选"
}
```
- 校验：卷标存在 + `in_shop`
- 成功：`{"success": true, "message": "报废记录已保存", "id": 124, "consume_type": "scrap", "out_length": 500}`

### 3.4 首件/末件检查登记
```
POST /api/external/cutting-check
{
  "job_order": "J000004113-0000",
  "job_part_number": "可选（缺省取 part_number）",
  "part_number": "A080507",
  "check_type": "first | last",
  "cut_length_mm": "可选",
  "is_manual": false,
  "shear_std_length": 152.4, "shear_std_tol": "±0.5",
  "shear_std_device": "可选", "shear_actual_device": "可选",
  "shear_actual_length": 152.2,     // 必须 > 0
  "shear_operator": "可选", "shear_checker": "确认人（必填）",
  "strip_a_std_length": "可选", "strip_a_std_tol": "可选", "strip_a_std_device": "可选",
  "strip_a_actual_device": "可选", "strip_a_actual_length": "可选",
  "strip_a_operator": "可选", "strip_a_checker": "可选",
  "strip_b_std_length": "可选", "strip_b_std_tol": "可选", "strip_b_std_device": "可选",
  "strip_b_actual_device": "可选", "strip_b_actual_length": "可选",
  "strip_b_operator": "可选", "strip_b_checker": "可选",
  "scrap_length": "可选",
  "force": false
}
```
**公差校验**：剪线/去皮 A/B 实际长度超出标准±公差 且 `force=false` → `400 {"needForce": true, "error": "剪线长度超出公差，需确认人授权"}`（多个错误以 `；` 连接），确认人授权后带 `force:true` 重发。

成功：`{"success": true, "message": "首件检查已保存/末件检查已保存", "id": 125, "check_type": "first"}`

### 3.5 消耗查询
```
GET /api/external/consumption?coil_id=260826002&job_order=J000004113
```
响应结构与 wiptrack 原 `SELECT` 一致（含全部宽表字段、consume_type_label、stage）。

### 3.6 首末件检查查询
```
GET /api/external/cutting-check?job_order=J000004113&part_number=&check_type=
```
返回 kr_cutting_check 全字段。

### 3.7 裁剪参数查询
```
GET /api/external/cutting-ref?finished_part=A080507&wire_part=可选
```
响应（对齐 wiptrack 原结构）：
```json
{ "success": true, "data": [{
  "id": 1, "finished_part": "A080507", "wire_part": "B010101", "wire_awg": "24",
  "color": null, "qty_per_group": 24, "cut_length_mm": 152.4, "length_tol": "±0.5",
  "cut_device": "TSPL", "device_no": "SZP104",
  "strip_len_a": 5, "strip_tol_a": "±0.3", "strip_len_b": 5, "strip_tol_b": "±0.3",
  "term_a": null, "term_b": null
}]}
```

### 3.8 确认人密码校验
```
POST /api/external/confirm-user
{ "password": "确认密码" }
```
- 优先匹配 `cutting_confirm_user` 表（password→name）；未命中回退 env `CUTTING_CONFIRM_PASSWORD`/`CUTTING_CONFIRM_NAME`（未配置则仅表校验）
- 成功：`{"success": true, "name": "线长-王"}`（name 用于记录确认人）
- 失败：`400 {"success": false, "error": "确认密码错误"}`

### 3.9 删除消耗记录
```
DELETE /api/external/consumption/<id>
{ "password": "确认密码" }
```
- 需先通过确认人密码校验，否则 `400 确认密码错误`
- 记录不存在 → `404 记录不存在`
- 成功：`{"success": true, "message": "记录已删除", "id": <id>}`

### 3.10 消耗记录分页列表
```
GET /api/external/consumption/list?page=1&pageSize=20&job=&part=&coilId=&startDate=&endDate=
```
- 参数：`page`（默认1）、`pageSize`（1-200，默认20）、`job`（工单模糊）、`part`（物料模糊）、`coilId`（精确）、`startDate`/`endDate`（YYYY-MM-DD）
- 响应：`{"success": true, "data": [...], "total": N, "page": 1, "pageSize": 20}`
- data 字段与 wiptrack 原 `listConsumptionRecords` 返回一致（含 consume_type_label、stage、宽表字段、operator、remark、created_at）

### 3.11 卷标信息分页列表
```
GET /api/external/coils/list?page=1&pageSize=20&coilId=&part=&status=
```
- 参数：`coilId`（精确）、`part`（物料模糊）、`status`（in_stock/in_shop/scrapped...）
- 响应：`{"success": true, "data": [...], "total": N, "page": 1, "pageSize": 20}`
- data 字段：`coil_id, part_number, status, status_label, coil_length, unit, siteref, used_mm, scrapped_mm, total_used_mm, remain_mm, remain_orig, created_at`（used_mm=consumption 汇总、scrapped_mm=scrap 汇总、total_used_mm=全部汇总）

---

## 4. 需要改动的 wiptrack 位置（替换直连 DB）

| wiptrack 文件 | 动作 | 改为调用 |
|:--|:--|:--|
| cuttingController.ts 202/240 | 查卷标+剩余 | 3.1 查卷标 |
| cuttingController.ts 807-838 | 报废登记 | 3.3 报废登记 |
| cuttingController.ts 844-899 | 消耗登记（超限校验） | 3.2 消耗登记 |
| cuttingController.ts 957-990 | 首/末件检查登记 | 3.4 检查登记 |
| cuttingController.ts 1005-1038 | 确认人密码校验（cutting_confirm_user） | 3.8 确认人校验 |
| cuttingController.ts 1042-1095 | 删除裁线记录（DELETE /api/cutting/consumption/:id） | 3.9 删除消耗记录 |
| cuttingController.ts 1100-1180 | 消耗记录分页列表（GET /api/cutting/records/list） | 3.10 消耗分页列表 |
| cuttingController.ts 1186-1270 | 卷标信息分页列表（GET /api/cutting/coils/list） | 3.11 卷标分页列表 |
| cuttingController.ts 1014 | 消耗查询 | 3.5 消耗查询 |
| cuttingController.ts 439/497 | 裁剪参数查询 | 3.7 裁剪参数 |
| cuttingController.ts 523-642 | 检查查询/状态 | 3.6 检查查询 |
| recordController.ts 304 | 消耗记录 | 3.5 消耗查询 |

> recordController.ts 中 `production_records` 表操作（415/423/464 行）**不属于 materialpickup 库**，保持不变。

## 5. 配置
- API Key 环境变量：`EXTERNAL_API_KEY`（默认开发值 `NAI-WIPTRACK-2026`，生产请改）
- 确认人回退密码（cutting_confirm_user 表未命中时）：`CUTTING_CONFIRM_PASSWORD` / `CUTTING_CONFIRM_NAME`（可选，不配置则仅表校验）
- 数据库读写权限建议：后续从 wiptrack 移除 materialpickup 库连接配置
