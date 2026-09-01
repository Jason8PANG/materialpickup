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

## 1.1 站点标识（X-Site-Ref）

除 **3.8 确认人密码校验** 外，其余所有接口都必须携带站点标识，看板侧**强制校验 + 按站点过滤 + 落库**。

```
X-Site-Ref: 310            // 站点号
X-Site-Ref: NAIGROUP_PRD_410   // 公司码
X-Site-Ref: NAIGROUP_PROD_410  // 公司码（容错 PRD/PROD、单/双下划线）
```

- 取值优先级：请求头 `X-Site-Ref` > query 参数 `site` > JSON body 的 `site`（仅 POST/DELETE）
- 接受两种格式：
  1. 站点号 `310` / `410`（须为看板已配置站点）；
  2. 公司码 `NAIGROUP_PRD_XXX` / `NAIGROUP_PROD_XXX`（容错 PRD/PROD、单/双下划线），或去掉前缀后的站点号（如 `410`）
- 站点号 `310`=苏州工厂、`410`=槟城工厂
- 缺失/无效 → `400 {"success": false, "error": "缺少或无效的站点标识（X-Site-Ref），可选站点: 310, 410"}`

各接口站点行为见下表：数据过滤/落库以校验通过的站点为准；消耗/报废登记时 `siteref` 从关联卷标自动带出（不依赖调用方传入），首件/末件检查登记写入调用站点号。

---

## 2. 接口清单

| # | 方法 | 路径 | 用途 | 站点 |
|:--|:--|:--|:--|:--|
| 1 | GET | `/api/external/coils/<coil_id>` | 查卷标（含剩余长度） | 必需 |
| 2 | POST | `/api/external/consumption` | **消耗登记**（裁剪，stage=complete） | 必需 |
| 3 | POST | `/api/external/consumption/scrap` | **报废登记** | 必需 |
| 4 | POST | `/api/external/cutting-check` | **首件/末件检查登记** | 必需 |
| 5 | GET | `/api/external/consumption?coil_id=&job_order=` | 消耗查询 | 必需 |
| 6 | GET | `/api/external/cutting-check?job_order=&part_number=&check_type=` | 首末件检查查询 | 必需 |
| 7 | GET | `/api/external/cutting-ref?finished_part=` | 裁剪参数查询 | 必需（仅校验，不过滤） |
| 8 | POST | `/api/external/confirm-user` | **确认人密码校验**（cutting_confirm_user） | 无需 |
| 9 | DELETE | `/api/external/consumption/<id>` | **删除消耗记录**（需确认人密码） | 必需 |
| 10 | GET | `/api/external/consumption/list` | **消耗记录分页列表** | 必需 |
| 11 | GET | `/api/external/coils/list` | **卷标信息分页列表** | 必需 |
| 12 | GET | `/api/external/part-stock/<part>` | **库位库存查询**（Floor/Other/Total，待实现） | 必需 |

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
    "siteref": "410",
    "request_id": 1675, "used_mm": 0.0, "remain_mm": 30480.0, "remain_orig": 100.0
  }
}
```
> 需带 `X-Site-Ref`；站点与卷标不一致时返回 `404 卷标不存在`（跨站隔离）。
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
  "cut_length_mm": "可选", "length_tolerance": "可选（如 ±0.5）",
  "shear_equipment": "可选", "shear_device_no": "可选", "actual_shear_equipment": "可选",
  "operator": "tester", "checker": "可选", "is_manual": false, "remark": "可选",
  "force": false                  // 超限时是否强制（确认人授权后 true）
}
```
**业务校验（服务端执行，wiptrack 不再直查库）**：
- 需带 `X-Site-Ref`；卷标不存在或站点不一致 → 404（跨站隔离）
- 卷标存在 + 状态 `in_shop`，否则 400 `卷标ID的状态不正确`
- 出库长度 = `shear_qty × actual_shear_length + scrap_length_actual`
- 超限（`已用 + 本次 > 卷长×系数`）且 `force=false` → `400 {"needForce": true, "error": "长度超限，需确认人授权"}`
  - wiptrack 收到 needForce 后弹出确认人授权，再带 `force:true` 重发
- 单位换算：`converted_length = out_length ÷ 系数`（FT=304.8/M=1000）
- **siteref 落库值取自关联卷标（coil.siteref），自动带出，不依赖调用方传参**

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
- 需带 `X-Site-Ref`；校验：卷标存在（且站点一致）+ `in_shop`
- **siteref 落库值取自关联卷标（coil.siteref），自动带出**
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

- 需带 `X-Site-Ref`；**siteref 落库值 = 调用站点号**（检查记录归属调用站点）
- 成功：`{"success": true, "message": "首件检查已保存/末件检查已保存", "id": 125, "check_type": "first"}`

### 3.5 消耗查询
```
GET /api/external/consumption?coil_id=260826002&job_order=J000004113
```
- 需带 `X-Site-Ref`；数据按站点过滤
- 响应结构与 wiptrack 原 `SELECT` 一致（含宽表字段、`siteref`、consume_type_label、stage）。

> 废弃字段说明（2026-08-29 已从表结构/API 移除，不再返回）：
> `wire_spec`、`color`、`checker_first`、`checker_last`、`actual_shear_length_last`

### 3.6 首末件检查查询
```
GET /api/external/cutting-check?job_order=J000004113&part_number=&check_type=
```
- 需带 `X-Site-Ref`；数据按站点过滤
- 返回 kr_cutting_check 全字段（含 `siteref`）。

### 3.7 裁剪参数查询
```
GET /api/external/cutting-ref?finished_part=A080507&wire_part=可选
```
- 需带 `X-Site-Ref`（**仅校验站点有效性，不过滤数据**——kr_cutting_ref 无 siteref 列，裁剪参数跨站点共享）
响应（对齐 wiptrack 原结构）：
```json
{ "success": true, "data": [{
  "id": 1, "finished_part": "A080507", "wire_part": "B010101",
  "qty_per_group": 24, "cut_length_mm": 152.4, "length_tol": "±0.5",
  "cut_device": "TSPL", "device_no": "SZP104",
  "strip_len_a": 5, "strip_tol_a": "±0.3", "strip_len_b": 5, "strip_tol_b": "±0.3",
  "term_a": null, "term_b": null
}]}
```
> 变更说明（2026-08-29）：`wire_awg`、`color` 已从 API 响应中移除（看板 SELECT 不再返回）。`kr_cutting_ref` 表中仍保留这两列，如需恢复展示请告知看板侧。

### 3.8 确认人密码校验
```
POST /api/external/confirm-user
{ "password": "确认密码" }
```
- **无需站点标识**（cutting_confirm_user 表无站点概念，确认人跨站点共用）
- 优先匹配 `cutting_confirm_user` 表（password→name）；未命中回退 env `CUTTING_CONFIRM_PASSWORD`/`CUTTING_CONFIRM_NAME`（未配置则仅表校验）
- 成功：`{"success": true, "name": "线长-王"}`（name 用于记录确认人）
- 失败：`400 {"success": false, "error": "确认密码错误"}`

### 3.9 删除消耗记录
```
DELETE /api/external/consumption/<id>
{ "password": "确认密码" }
```
- 需带 `X-Site-Ref`（缺失/无效 → 400）
- 需先通过确认人密码校验，否则 `400 确认密码错误`
- 记录不存在或**站点不一致** → `404 记录不存在`（跨站不可删）
- 成功：`{"success": true, "message": "记录已删除", "id": <id>}`

### 3.10 消耗记录分页列表
```
GET /api/external/consumption/list?page=1&pageSize=20&job=&part=&coilId=&startDate=&endDate=
```
- 需带 `X-Site-Ref`；数据按站点过滤
- 参数：`page`（默认1）、`pageSize`（1-200，默认20）、`job`（工单模糊）、`part`（物料模糊）、`coilId`（精确）、`startDate`/`endDate`（YYYY-MM-DD）
- 响应：`{"success": true, "data": [...], "total": N, "page": 1, "pageSize": 20}`
- data 字段与 wiptrack 原 `listConsumptionRecords` 返回一致（含 `siteref`、consume_type_label、stage、宽表字段、operator、remark、created_at）
- 废弃字段说明（2026-08-29 已从表结构/API 移除，不再返回）：`wire_spec`、`color`、`checker_first`、`checker_last`、`actual_shear_length_last`

### 3.11 卷标信息分页列表
```
GET /api/external/coils/list?page=1&pageSize=20&coilId=&part=&status=
```
- 需带 `X-Site-Ref`；数据按站点过滤
- 参数：`coilId`（精确）、`part`（物料模糊）、`status`（in_stock/in_shop/scrapped...）
- 响应：`{"success": true, "data": [...], "total": N, "page": 1, "pageSize": 20}`
- data 字段：`coil_id, part_number, status, status_label, coil_length, unit, siteref, used_mm, scrapped_mm, total_used_mm, remain_mm, remain_orig, created_at`（used_mm=consumption 汇总、scrapped_mm=scrap 汇总、total_used_mm=全部汇总）

### 3.12 库位库存查询（part-stock，看板已实现 2026-08-31）
```
GET /api/external/part-stock/<part>
```
- 需带 `X-API-Key` + `X-Site-Ref`（强制站点校验，按站点查 SLItemLocs）
- **用途**：生产跟踪后端 `GET /api/cutting/part-stock/:part?siteRef=` 已改为转发本接口（不再直连 CSI）。当前后端请求 `X-Site-Ref` 传的是调用方站点（如 `NAIGROUP_PROD_410`），看板 `_resolve_site` 可直接解析。
- **数据源**：实时调用 Infor CSI IDO `SLItemLocs`（`CSIClient(siteref=site).get_inventory(part)`，QtyOnHand>0，按站点公司上下文），**非** csi_datawarehouse 下载表 SLITEMLOC；单位经 `_factor` 换算（FT=304.8/M=1000）；库位名含 `floor`（不区分大小写）归 Floor，其余归 Other。
- 响应（data 结构与旧 CSI 直连返回保持一致，前端无需改动）：
```json
{
  "success": true,
  "data": {
    "item": "A080507", "unit": "FT",
    "floor_qty": 5722.489, "floor_qty_mm": 1744214.6472,
    "other_qty": 23855, "other_qty_mm": 7271004,
    "total_on_hand": 29577.489, "total_on_hand_mm": 9015218.647200001,
    "floor_locations": [{"location": "FLoor-Core", "qty": 5722.489, "unit": "FT", "qty_mm": 1744214.6472}],
    "other_locations": [{"location": "RM-P040-1", "qty": 23855, "unit": "FT", "qty_mm": 7271004}]
  }
}
```
- 失败：`{"success": false, "error": "..."}` + 4xx/5xx（后端透传状态码；`unit` 查不到时 `*_mm` 可为 null）

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
