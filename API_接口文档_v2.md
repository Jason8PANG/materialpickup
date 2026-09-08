# 物料领取看板 — API 接口文档 v2

> **生成日期：2026-09-08**
> **版本说明：** 本文档基于本地最新代码（`app/routes/` 下 12 个蓝图共 **109** 条路由）逐路由提取生成，与旧版 `API 文档.md`（805 行，已过时）相互独立。凡与旧文档不一致处，**以本文档及代码为准**。涵盖近几日的多站点隔离、Bartender 触发文件打印、卷标编辑、回到备料、卷号生成含软删取号等变更。
> **覆盖范围**：admin 12 / approval 4 / auth 4 / coil 19 / cutting 5 / external 12 / kanban 3 / request_bp 8 / return_bp 10 / validate 4 / warehouse 10 / wire 18。
> **用途**：作为后续需求修改的接口契约依据（供开发/测试 agent 引用）。

---

## 1. 通用约定

### 1.1 服务与数据模型前缀
- 业务表统一前缀 `kr_`（`kr_material_request` 申请/退料主表、`kr_request_item` 明细、`kr_wire_coil` 卷标、`kr_wire_coil_consumption` 消耗、`kr_role_mapping` 角色站点映射、`kr_site_printer` 站点打印机、`kr_operation_log` 操作日志、`kr_cutting_ref` 裁线规格、`kr_cutting_check` 首末件检查、`kr_return_item` 退料明细、`kr_inventory_count(_item)` 盘点、`kr_approval_token` 邮件审批令牌）。仅 `cutting_confirm_user`（确认人密码）为无前缀表。
- 申请单主表 `kr_material_request` 同时承载三类单据，由 `request_type` 区分：`NULL/空`=普通领料、`minpack`=最小包装、`return`=退料。

### 1.2 鉴权与会话
- **站内接口（除公开/邮件审批外全部）**：基于 Flask `session`。先 `POST /api/auth/login` 获得会话，后续请求携带会话 Cookie。
  - 未登录 → HTTP 401 `{"success": false, "message": "未登录"}`
  - 已登录但角色不足 → HTTP 403 `{"success": false, "message": "权限不足"}`（个别接口文案略异，以具体接口为准）
- **角色**：`admin`（管理员）/ `requester`（申请人/生产）/ `supervisor`（主管/审批人）/ `warehouse`（仓库）/ `me_engineer`（ME 工程师，仅裁线规格写权限）。
- **公开接口（无需登录）**：`GET /public/kanban`、`GET /api/public/kanban/cards`、`GET /api/approve/token/<token>`、`POST /api/approve/token/<token>/action`。
- **外部集成接口**：请求头 `X-API-Key`（见 1.5、第 12 章）。

### 1.3 多站点（siteref）与数据隔离规则 —— 重要
站点号当前支持：`310`=苏州工厂、`410`=槟城工厂（`Config.SITE_CONFIG`，可扩展）。CSI 公司码映射 `Config.SITE_CSI_COMPANY`：`310→NAIGROUP_PRD_310`、`410→NAIGROUP_PRD_410`。

**账号-站点绑定（`kr_role_mapping`）**：
- 字段：`domain_account`（域账号）、`role`、`siteref`（默认站，非 admin/me_engineer 必填）、`site_access`（可访问多站，逗号分隔字符串，为空=仅默认站）。
- `admin`：`siteref`/`site_access` 均可空；登录时自动获得**全站** `available_sites`。
- `me_engineer`：可跨站维护裁线规格，`siteref` 允许空。
- 站点合法性校验以 `Config.SITE_CONFIG` 为准；非空 `site_access` 会被规范化（过滤非法站、去重、**自动补入默认站兜底**、排序）。`admin` 规范化后为 `None`（登录时全站）。

**登录/切站（session）**：
- 登录后 `session['user']` 含：`username`、`display_name`、`role`、`email`、`siteref`（**当前站**）、`original_siteref`（映射默认站）、`available_sites`（可访问站点列表）。
- `admin`：`available_sites`=全部站点，`siteref`=映射站或兜底 `310`。
- 非 `admin`：`available_sites`=`site_access` 拆分；为空则回退 `[siteref]`。
- **切站** `POST /api/auth/switch-site`：任何角色只要目标站在 `available_sites` 内即可切换（含非 admin），`admin` 全站可切；切换后仅更新 `session['user']['siteref']`。

**SQL 过滤（`app/models/__init__.py: get_site_filter(user)`）**：
- 返回 `(None, None)`：未登录，或 `role==admin` 且当前站 `siteref` 为空 → 不过滤（全站）。
- 返回 `("siteref = %s", (siteref,))`：session 有当前站（非 admin 通常必有；admin 登录时也会兜底到 310/映射站，因此**也按当前站过滤**）→ 只可见当前站数据。
- 因此"跨站查看"的正确方式是 `switch-site` 后刷新列表；不传站参数的后台接口一律按当前站过滤，无法一次跨站拉全量（`admin` 例外情形为上述 siteref 为空）。
- 各写接口内嵌 `validate_site_match`/`_check_site`：单据/卷标的 `siteref != 当前站` → HTTP 403「无权操作其他站点的单据」。

**站点隔离落点（本次变更核心）**：
- 裁线规格 `kr_cutting_ref`：**按站独立**——list/create/update/delete/import 全部按当前站读写；import 按目标站删旧插新，仅覆盖该站。
- 卷标 `kr_wire_coil`：新增 `siteref` 列，录入/出库/打印/查询均带站（经申请单站或直接站过滤）。
- 消耗 `kr_wire_coil_consumption`：冗余 `siteref` 列。
- 盘点 `kr_inventory_count`：创建时记录 `siteref`（=创建人当前站或 410）。
- 退料选卷：仅返回当前站 `in_shop` 卷标。
- 看板/申请单/备料等：`r.siteref` 按当前站过滤。

### 1.4 请求 / 响应格式
- 请求体一律 `Content-Type: application/json`（上传接口除外）；创建成功多数返回 201。
- 统一响应骨架：`{"success": true/false, ...}`；失败带 `message`（外部集成接口带 `error`），由 HTTP 状态码区分语义。
- 分页约定：query `page`（默认 1）、`size`（各接口默认 20~100 不等，见各接口）；响应含 `total`、`page`、`size`、`total_pages`。
- 明细金额/长度类数值返回为数字；时间字段通常为 `"YYYY-MM-DD HH:MM:SS"` 字符串（按接口）。

### 1.5 外部集成鉴权与站点解析（X-API-Key / X-Site-Ref）
仅第 12 章 `/api/external/*` 使用，供 naiwiptrack / production-tracking 等外部系统调用（替代其直连数据库）。

1. **API Key**：请求头 `X-API-Key: <key>`，与 `Config.EXTERNAL_API_KEY` 比对（env `EXTERNAL_API_KEY`，代码默认 `NAI-WIPTRACK-2026`）。缺失/不符 → 401 `{"success":false,"error":"API Key 无效"}`。
2. **站点解析 `_resolve_site()`（除 confirm-user 外所有接口强制）**，取值优先级：
   1. 请求头 `X-Site-Ref`
   2. query 参数 `site`
   3. JSON body `site`（仅 POST/DELETE）
   解析规则：strip 空白、转大写、去空格。接受：
   - 站点号：`310` / `410`（须在 `SITE_CONFIG`）；
   - 公司码：`NAIGROUP_PRD_310` / `NAIGROUP_PROD_310` 等（容错 `PRD/PROD`、单/双下划线），按 `SITE_CSI_COMPANY` value 反查站点号；也兼容去掉 `NAIGROUP_PRD_` / `NAIGROUP_PROD_` 前缀后的纯站点号。
   解析失败 → 400 `{"success":false,"error":"缺少或无效的站点标识（X-Site-Ref），可选站点: 310, 410"}`。
   所有 external 查询/写入都以解析后的站点过滤/落库（SQL 均带 `siteref = %s`），**杜绝跨站读写**。
3. **confirm-user 例外**：`cutting_confirm_user` 无站点概念（确认人跨站共用），仅校验 API Key、不解析站点。

### 1.6 申请单状态机与状态标签（`STATUS_LABELS`）
状态流转主链：`pending_prep`（待备料）→ `prepping`（备料中）→ `ready_pickup`（待取料）→ `completed`（已完成）；旁支 `short`（缺料，可恢复/签字）、`pending_approval`（待审批，兼容遗留）、`rejected`（已驳回）、`pending_return`/`confirmed`（退料单）。
```
中文标签：pending_approval=待审批  rejected=已驳回  pending_prep=待备料  prepping=备料中
          short=缺料  ready_pickup=待取料  completed=已完成  pending_return=待退料  confirmed=已确认
```
- 普通领料与最小包装申请创建后**直接进入 `pending_prep`**（跳过审批环节）。`approve/reject/token` 仍保留，用于处理历史/外部创建的 `pending_approval` 单据。
- 卷标状态：`in_stock`=在库、`in_shop`=在车间（签字取料后转入）、`issued`=已出库、`scrapped`=报废；派生状态「盘点中」= 卷标存在于状态 `counting` 的活跃盘点单中。

### 1.7 卷标与单位换算
- 卷号规则：9 位数字 = `YYMMDD`(6 位) + 3 位流水，每日上限 999；**生成取数含软删**：`SELECT MAX(coil_id) WHERE coil_id LIKE '<yyyymmdd>%'`（含 `is_deleted=1` 行，`FOR UPDATE` 当前读防并发）→ MAX 序号 +1。卷号一经分配即占用唯一索引，软删行也**不可复用**。
- 长度口径（重要）：`kr_wire_coil.coil_length` 存**原始单位**（M/FT/CM/IN，由 CSI 取物料单位）；消耗表 `out_length` 固定存 **mm**。换算系数 `Config.UNIT_CONVERT_FACTOR`：M=1000、FT=304.8、CM=10、IN=25.4。公式：`converted_length(mm) = 录入长度 ÷ 系数`；剩余长度回写时 `mm ÷ 系数 → 原始单位`。单位未收录时按接口降级（警告或阻断见具体规则）。
- **剩余扣减口径**：`consume_type IN ('consumption','count_adjust')`（剪线消耗+盘点调整）参与剩余计算；`issue`（出库登记）、`scrap`（报废）**不扣减剩余**，但 `issue` 有自己独立的「累计出库 ≤ 卷长」防超发校验口径。
- 盘点锁定：卷标在状态 `counting` 的盘点单中时，禁止消耗登记/报废/退料/出库登记（各接口 400 拒绝）。

---

## 2. 认证与账号（auth.py · 4 接口）

### 2.1 登录
`POST /api/auth/login`
- 权限：公开。
- 请求体：`username`（域账号，自动剥离 `域\` 前缀与 `@域名` 后缀）、`password`。
- 流程：查 `kr_role_mapping`（`domain_account` + `is_active=1`）→ LDAP/AD 校验密码 → 组装站点信息 → 写 session。
- 失败：无映射 403「该用户没有系统访问权限」；LDAP 密码错 401；LDAP 不可用 401「认证服务暂时不可用」。
- 响应：`user`（同 session 结构）、`siteref`（当前站）、`siteref_name`、`available_sites`。站点组装规则见 1.3。

### 2.2 登出
`POST /api/auth/logout`
- 权限：登录即可。清空 session。响应 `{"success": true}`。

### 2.3 当前用户
`GET /api/auth/me`
- 权限：登录（未登录 401）。
- 响应：`user`、`siteref`、`siteref_name`、`available_sites`（session 中的站点信息；admin 无 `available_sites` 时兜底为全站列表）。

### 2.4 切换站点
`POST /api/auth/switch-site`
- 权限：登录即可；**任意角色**均可切换，条件 = 目标站在该用户 `available_sites` 内（admin 登录时即为全站）。
- 请求体：`siteref`（目标站点号）。
- 校验：站点无效 400「无效的站点」；不在可访问列表 403「无权访问该站点」。成功后仅更新 session 当前站。
- 响应：`siteref`、`siteref_name`。后续所有接口以新站过滤。

---

## 3. 申请单（request_bp.py · 8 接口）

申请单（普通领料 / 最小包装）创建后直接进入 `pending_prep`，走仓库备料流程，无审批步骤。

### 3.1 创建普通领料申请
`POST /api/requests`
- 权限：`requester`、`admin`。
- 请求体：
  ```
  {
    "items": [{ "job_order": "J000002124-0004", "part_number": "A...", "quantity": 10,
                "price": 1.2, "stock_qty": 50, "replenish_reason": "...", "replenish_reason_other": "...",
                "stock_loc": "S301A01" }],   // ≥1 行；job_order/part_number/quantity 必填
    "remark": "", "is_urgent": 0
  }
  ```
- 校验（逐行）：工单号格式（`parse_job`）；调 Infor CSI 校验工单存在且状态必须为 `R`（Released），否则 400；`quantity > stock_qty`（调用方传入值）→ 400。
- 落库：主表 `siteref = 用户当前站`（缺失 400「站点信息缺失」），`status='pending_prep'`；明细含金额 `total = qty*price`、`unit`（创建后异步经 CSI 回填 `kr_request_item.unit`，失败不阻断）。
- 通知：同工单+同物料已在其他未完成申请，或同工单所有申请累计金额 ≥ $30 时，向**当前站 supervisor** 邮箱发提醒（不影响创建成功）。
- 响应 201/200：`{"success": true, "id": request_id}`。

### 3.2 创建最小包装申请
`POST /api/requests/minpack`
- 权限：`requester`、`admin`。
- 请求体：`items[]`（`part_number`、`quantity`、`price`、`stock_qty`、`stock_loc`；**无工单**）、`remark`、`is_urgent`。
- 主表 `request_type='minpack'`、`status='pending_prep'`；明细 `job_order=NULL`。同样回填 `unit`。**不做** CSI 工单/库存校验。
- 响应 200：`{"success": true, "id": request_id}`。

### 3.3 申请单列表
`GET /api/requests`
- 权限：登录即可。仅返回未删除单。
- query：`status`（精确）、`job_order`、`part_number`（明细 LIKE）、`page`、`size`。
- 站点：按当前站过滤（`r.siteref`）。
- 响应：`data[]`（含 `item_count`、`primary_job_order`、`status_label`）、`total`、`page`、`size`、`total_pages`。

### 3.4 申请单详情
`GET /api/requests/<int:request_id>`
- 权限：登录即可。站点校验：非当前站单据 403。
- 响应：`request`（主表字段 + `status_label` + `items[]` + `item_count` + `total_amount` 汇总 + `return_items[]`（退料单才返回卷标明细））、`logs[]`（操作日志按时间升序）。

### 3.5 取消申请单
`POST /api/requests/<int:request_id>/cancel`
- 权限：**发起人本人**或 `admin`；其余 403。
- 状态要求：仅 `pending_prep` / `pending_approval` 可取消，否则 400。
- 实现：逻辑删除 `is_deleted=1`，写 `CANCEL` 日志。站点校验同 1.3。

### 3.6 历史单据
`GET /api/requests/history`
- 权限：登录即可。
- query：`status`、`job_order`、`part_number`、`date_from`、`date_to`（`request_time` 区间，date_to 补到当日 23:59:59）、`page`、`size`。
- 限定：仅 `status IN ('completed','rejected')` 且未删除；按当前站过滤。

### 3.7 综合记录（明细行展开）
`GET /api/records`
- 权限：登录即可。
- query：`date_from`、`date_to`、`page`、`size`（默认 100）。
- 响应：`kr_material_request` LEFT JOIN `kr_request_item` 展开为行级，字段含主表 `requester/request_time/supervisor/approve_time/warehouse_operator/signer/sign_time/siteref/status/short_reason/remark` + 明细 `job_order/part_number/quantity/price/total_amount/stock_qty/item_short_reason`。按当前站过滤。

### 3.8 最小包装重复物料检查
`GET /api/requests/check-duplicate-part`
- 权限：登录即可。
- query：`part_number`（必填）。
- 逻辑：查同站（admin 不限站）`status IN ('pending_prep','prepping')` 未删除申请中是否已有该物料，最多 10 条。
- 响应：`{"success":true,"duplicate":true/false,"requests":[{id,status,status_label,requester,request_time}]}`。

---

## 4. 审批（approval.py · 4 接口）

> 当前创建流程不产生 `pending_approval` 单据，本组接口用于兼容处理遗留/外部创建的待审批单，及邮件审批通道。

### 4.1 审批通过
`POST /api/requests/<int:request_id>/approve`
- 权限：`supervisor`、`admin`。
- 请求体：`comment`（批注，可空）。
- 状态要求：仅 `pending_approval`，否则 400。站点校验：非当前站 403。
- 效果：`status→pending_prep`，写入 `supervisor`（操作人）、`approve_time`、`approve_comment`；`APPROVE` 日志。
- 响应 200：`{"success": true, "message": "审批通过"}`。

### 4.2 审批驳回
`POST /api/requests/<int:request_id>/reject`
- 权限：`supervisor`、`admin`。
- 请求体：`comment` **必填**（驳回意见），缺省 400。
- 状态要求：仅 `pending_approval`。效果：`status→rejected`，写 supervisor/时间/意见；`REJECT` 日志。

### 4.3 邮件审批令牌详情（公开）
`GET /api/approve/token/<token>`
- 权限：**无需登录**（令牌即凭证）。
- 校验：`kr_approval_token` 中 token 未使用且未过期（72h 有效），否则 404「令牌无效或已过期」。
- 响应：`request`（主表 JOIN 明细后字段 + `status_label` + `items[]` + `item_count`）。

### 4.4 邮件审批动作（公开）
`POST /api/approve/token/<token>/action`
- 权限：**无需登录**。
- 请求体：`action` ∈ `approve`/`reject`（其他 400）；`comment`（reject 必填）。
- 校验：令牌有效、单据仍为 `pending_approval`（否则 400「单据状态已变更」）。令牌标记 `is_used=1`（一次性）。
- 效果：approve→`pending_prep` / reject→`rejected`；操作人记令牌绑定的 `supervisor`；日志分别 `APPROVE`/`REJECT`（"邮件审批通过/驳回"，IP `0.0.0.0`）。

---

## 5. 备料流程（warehouse.py · 10 接口）

「单据操作」型接口均校验站点匹配（非当前站 403）。角色约定：`warehouse`、`admin`。

### 5.1 开始备料
`POST /api/requests/<int:request_id>/start-prep`
- 权限：`warehouse`、`admin`。
- 状态要求：仅 `pending_prep`，否则 400。
- 效果：`status→prepping`、`warehouse_operator=操作人`；`START_PREP` 日志。

### 5.2 完成备料
`POST /api/requests/<int:request_id>/complete-prep`
- 权限：`warehouse`、`admin`。
- 状态要求：仅 `prepping`。
- 特殊校验（**minpack** 申请单）：物料号以 `A`/`B` 开头且未维护行级缺料原因（`short_reason` 为空）的每一明细行，**必须至少录入 1 个在库卷标**（`kr_wire_coil` 该 `item_id` 有效行），否则 400（中英文按 `?lang=` 环境，文案如「A/B 开头物料必须录入卷标：...」）。已维护缺料原因的行豁免。
- 效果：`status→ready_pickup`；`COMPLETE_PREP` 日志。

### 5.3 缺料恢复 → 待取料
`POST /api/requests/<int:request_id>/restore-from-short`
- 权限：`warehouse`、`admin`。状态要求：仅 `short`。
- 效果：`status→ready_pickup`，并清空 `short_reason`、`short_time`；`RESTORE_FROM_SHORT` 日志。

### 5.4 回到备料中（新增）
`POST /api/requests/<int:request_id>/back-to-prepping`
- 权限：`warehouse`、`admin`。
- 状态要求：仅 `ready_pickup`，否则 400「仅待取料（ready_pickup）状态的申请单可回到备料中」。
- 用途：待取料单需**补录/修正卷标、出库登记**等时退回 `prepping`；仅改 status，无副作用字段。
- 效果：`status→prepping`；`BACK_TO_PREPPING` 日志。

### 5.5 登记缺料
`POST /api/requests/<int:request_id>/short`
- 权限：`warehouse`、`admin`。
- 请求体：`short_reason` 必填。
- 状态要求：`prepping` 或 `pending_prep`。
- 效果：`status→short`，写 `short_reason`、`short_time`；`SHORT` 日志。

### 5.6 签字确认取料
`POST /api/requests/<int:request_id>/sign`
- 权限：`requester`、`warehouse`、`admin`。
- 状态要求：`ready_pickup` 或 `short`。
- 请求体：`signature_data`（签名图片数据，可为空）。
- 效果：`status→completed`、写 `signer`/`sign_time`/`signature_data`；**该单全部 `in_stock` 卷标 → `in_shop`（转车间，离开仓库库存）**；`SIGN` 日志（含转车间卷数）。

### 5.7 指定备料员
`PUT /api/requests/<int:request_id>/assign-worker`
- 权限：`warehouse`、`admin`。
- 请求体：`warehouse_operator`（备料员账号）必填。
- 状态要求：`pending_prep` 或 `prepping`。写字段 + `ASSIGN_WORKER` 日志。

### 5.8 按行维护缺料原因
`PUT /api/requests/<int:request_id>/items/<int:item_id>/short`
- 权限：`warehouse`、`admin`。
- 请求体：`short_reason` 必填（"请输入缺料原因"）。
- 状态要求：申请单须为 `prepping` 或 `short`；更新 `kr_request_item.short_reason`；行不存在 404。
- 作用：行级缺料豁免完成备料的 A/B 卷标校验。

### 5.9 保存物料行批次号
`PUT /api/requests/<int:request_id>/items/<int:item_id>/batch`
- 权限：`warehouse`、`admin`。
- 请求体：`batch_no`（可空 → 清空为 NULL）。
- 状态要求：无（仅校验行存在）。更新 `kr_request_item.batch_no`；行不存在 404。

### 5.10 未完成单据列表（仓库用）
`GET /api/requests/pending`
- 权限：登录即可。
- query：`status`、`job_order`（明细 LIKE）、`page`、`size`（默认 20）。
- 范围：未删除且 `status NOT IN ('completed','rejected')`（含退料待确认单）；按当前站过滤。
- 响应：`data[]`（含 `item_count`、`primary_job_order`、`status_label`）+ 分页四件套。

---

## 6. 卷标与出库登记（coil.py · 19 接口）

> 卷标仅服务 `minpack`（最小包装）申请单。录入/修改/删除/打印/出库均要求 `warehouse`、`admin`。所有按申请单操作均校验单据站点（见 1.3）。
> 卷号：9 位 `YYMMDD+3位`，**含软删 MAX+1**（见 1.7），日上限 999；录入唯一键冲突自动重取号重试（最多 3 次）。
> 批量上限 `MAX_BATCH=500`。

### 6.1 卷号生成（GET）
`GET /api/coils/next-id`
- 权限：`warehouse`、`admin`（防泄露日用量）。
- query：`date`（可选，`YYYY-MM-DD`，默认今天）。
- 响应 `data`：`coil_id`、`date`、`date_prefix`(YYMMDD)、`seq`、`daily_count`（近似 = seq-1）、`daily_limit`(999)。
- 规则：`_gen_next_id` 取当日前缀含软删 `MAX(coil_id)+1`（`SELECT ... FOR UPDATE` 当前读，防并发同号）；当日达 999 → 400「当日卷号已用完（每天最多999卷）」。

### 6.2 卷标 Lot 验证（新增）
`POST /api/coils/validate-lot`
- 权限：`warehouse`、`admin`。
- 请求体：`part_number`（必填）、`lot_no`（必填）、`length`（可选，原始单位数值）、`unit`（可选）。
- 逻辑：以当前站 `CSIClient` 连 Infor IDO `SLLots` 按 Item 过滤查 Lot；`length > DerQtyOnHand` → 400「长度超过 Lot 可用数量」；Lot 不存在 400。
- 响应成功：`lot_exists/lot/item/lot_status/whse/der_qty_on_hand/unit/message`。

### 6.3 卷号生成（POST 别名）
`POST /api/requests/<int:request_id>/coil-number`
- 权限：`warehouse`、`admin`。请求体/响应同 6.1（`date` 可空）。`request_id` 实际不使用（历史别名）。

### 6.4 卷标信息批量录入
`POST /api/requests/<int:request_id>/coils`
- 权限：`warehouse`、`admin`。
- 请求体：
  ```
  { "items": [{ "part_number":"A...", "coil_id":"260908001", "length":50.0,
                "unit":"M", "lot_no":"LOT001", "item_id":12, "is_initial_half":false }] }
  ```
  字段：`part_number`、`coil_id`(9位数字) 、`length`(>0)、`item_id`(可选，按申请单行绑定)、`lot_no`、`unit`、`is_initial_half`（`1/true/yes` 视为期初半卷）、`remark`。
- 前置校验：单据存在且 `request_type='minpack'`、`status='prepping'`（均不符 → 400）；物料必须在单据明细内；卷号格式 9 位数字；长度>0；`item_id` 若传须属于该单且物料一致。
- R4 校验：**批量模式**（均未带 item_id）下，A/B 开头的物料必须出现在本次提交中。
- R4.5 自动分配行：未带 item_id 的行自动绑到该物料第一个尚无卷标的明细行（全覆盖则绑最后一行）；`lot_no` 为空自动带出明细行 `batch_no`。
- 写入：`siteref=申请单站`、`status='in_stock'`；`unit` 优先取发起时保存的 `kr_request_item.unit`，兜底 CSI 缓存查询；单位获取失败仅警告不阻断。
- 响应：`{"success":true,"message","inserted":n,"data":[{id,coil_id,part_number,length,unit,status,item_id,is_initial_half}],"warnings":[]}`。

### 6.5 申请单卷标列表
`GET /api/requests/<int:request_id>/coils`
- 权限：`warehouse`、`admin`（卷标数据含工单/操作人）。
- query：`item_id`（可选，按申请单行过滤）。
- 响应 `data[]`：卷标全字段 + `status_label`（在库/在车间/已出库/报废）+ `coil_length`(float) + `created_at`(字符串)。

### 6.6 删除卷标（软删除）
`DELETE /api/coils/<int:coil_id>`
- 权限：`warehouse`、`admin`。按卷标来源申请单校验站点。
- 删除前提（**全部满足**，否则 400）：
  1. 卷标状态为 `in_stock`；
  2. `kr_wire_coil_consumption` 无该卷记录（未出库/报废）；
  3. `kr_return_item` 无该卷记录（未参与退料）；
  4. 来源申请单**从未打印过标签**（无 `COIL_PRINT` 日志）；
  5. **只能从后往前删**：该卷必须是所在范围（有 item_id 按行、否则按单）`in_stock` 未删卷中 `id` 最大的那条。
- 实现：软删除 `is_deleted=1`（保留可恢复），`COIL_DELETE` 日志。
- 响应 200：`{"success":true,"message":"卷标已删除（软删除，可恢复）"}`。

### 6.7 修改卷标（Lot / 长度，新增）
`PUT /api/coils/<int:coil_id>`
- 权限：`warehouse`、`admin`。
- 请求体：`lot_no`（可传空串=清空）、`coil_length`（须>0，`float`）；二者**至少传一个**。
- 前置校验：卷存在未删；来源单为 `minpack` 且 `prepping`（已出库/已完成不可改）；卷状态 `in_stock`（已离仓拒绝）。
- 改长度且**已有消耗**（`SUM(out_length)>0`，mm）时：须 `新长度(原始单位) × 换算系数 ≥ 已消耗(mm)`，否则 400「新长度小于已消耗长度」；单位无换算系数 → 400 提示联系管理员。
- 写 `updated_at=NOW()`；`COIL_UPDATE` 日志记录新旧值（如 `Lot A→B`、`长度 50.0→48.0`）。
- 响应 200：`{"success":true,"message":"卷标信息已更新"}`。

### 6.8 线卷库存列表查询
`GET /api/coils`
- 权限：登录即可。
- query：`part_number`(LIKE)、`status`(精确)、`request_id`、`date_from`、`date_to`(创建时间区间，`YYYY-MM-DD`)、`page`(默认1)、`size`(默认20，≤200)。
- 站点：按当前站过滤；排除软删。排序 `id DESC`。
- 响应：`data[]`（每项含 `status_label` 等，见 6.5）+ 分页四件套。

### 6.9 备料参考：在库可选用卷标
`GET /api/requests/<int:request_id>/in-stock-coils`
- 权限：登录即可；单据站点校验。
- 返回该申请单各物料当前**可复用**的在库卷标：排除仍挂在未完成申请（`pending_prep/prepping/ready_pickup/short`）下的卷。
- 剩余长度：退料回来的卷（`is_return`）直接取回写后 `coil_length`；普通卷 = `coil_length − 已消耗(mm)÷系数`；≤0 过滤。返回按 `is_return DESC, id` 排序。
- 响应 `data`：`{ part_number: [{coil_id, remain_length, unit, is_return}] }`。

### 6.10 选用在库卷标（绑定到本单）
`POST /api/requests/<int:request_id>/coils/use-stock`
- 权限：`warehouse`、`admin`。请求体：`coil_id`（必填）。
- 前置：申请单状态在 `pending_prep/prepping/short/ready_pickup`；卷存在、`in_stock`、物料属于本单。
- 效果：卷标 `request_id`/`item_id` 改绑本单；若曾属其他申请单，备份到 `prev_request_id`/`prev_item_id`（原归属可为 0=可选池，判空用 `is not None`）；`COIL_USE_STOCK` 日志。
- 说明：绑定后随本单 sign（签字取料）自动转 `in_shop`。

### 6.11 取消选用在库卷标
`POST /api/requests/<int:request_id>/coils/unuse-stock`
- 权限：`warehouse`、`admin`。请求体：`coil_id`（必填）。
- 前置：申请单状态同 6.10；卷属于本单、`in_stock`、且确为选用而来（`prev_request_id` 非空，否则 400「无需取消（请用删除）」）。
- 效果：`request_id`/`item_id` 还原到原归属（0=可选池），清空 prev 字段；`COIL_UNUSE_STOCK` 日志。

### 6.12 申请单物料单位
`GET /api/requests/<int:request_id>/coil-units`
- 权限：登录即可；站点校验。
- 逻辑：直接读 `kr_request_item.unit`（发起时已存），历史空值兜底查 CSI 缓存。
- 响应：`{"success":true,"data":{part_number: unit}}`。

### 6.13 批量物料单位（POST 别名）
`POST /api/coils/units`
- 权限：`warehouse`、`admin`。
- 请求体：`part_numbers[]`（≤200）。**站点固定取登录用户站**，不信任前端传入的 `siteref`（防篡改）；并发 8 线程带缓存查 CSI。
- 响应：`{"success":true,"data":{part_number: unit}}`。

### 6.14 标签打印（Bartender 触发文件）
`POST /api/coils/print`
- 权限：`warehouse`、`admin`。
- 请求体：`coil_ids[]`（1~500）、`printer`（可选，显式打印机名）。
- 站点：非 admin 仅可打印本站卷（卷行带 `siteref` 过滤）。
- **打印方式（本次变更）**：不再向打印机发 ZPL/TSPL/GDI 指令；改为调用 `label_print_service.write_bartender_file(coils, printer)` 生成 **`.dd` 触发文件**写入 `Config.LABEL_PRINT_BTW_DIR`（env `LABEL_PRINT_BTW_DIR`，默认 `\\172.26.1.7\Coil_Label_Scanned`）共享目录，由 Bartender 监视目录自动打印。
- **打印机解析优先级**：① 请求体显式 `printer` → ② 站点表 `kr_site_printer`（取**第一批卷标**的 `siteref` 查 `printer_name`；`channel` 不再参与下发，仅历史兼容）→ ③ 未配置返回可读错误。
- 数据行顺序按 `coil_ids` 请求顺序重排（`_order_coils_by_ids`）。
- 成功响应：`printed`(n)、`errors`:[]、`file_path`；失败 404（无匹配卷）/错误响应含 `errors`。

### 6.15 申请单卷标打印（POST 别名/兼容）
`POST /api/requests/<int:request_id>/coils/print`
- 权限：`warehouse`、`admin`；单据站点校验。
- 请求体：`coil_ids[]`（可选；不传=打印该申请单**全部**卷标，兼容旧调用；传了只打本单内指定卷）、`printer`。
- 无卷可打 → 400「没有可打印的卷标」。实现复用 6.14 的打印逻辑。

### 6.16 单卷标签渲染数据（预览）
`GET /api/coils/<coil_id>/label`
- 权限：`warehouse`、`admin`。卷不存在 404；按卷的 `siteref` 校验站点。
- 响应：`data` = `label_print_service.LabelRenderer.render(coil)` 渲染字段（供标签排版/预览）。

### 6.17 申请单卷标预览（GET 别名）
`GET /api/requests/<int:request_id>/coils/preview`
- 权限：`warehouse`、`admin`。
- query：`coil_id`（可选，单卷）。
- 响应：`data[]` = 每卷 `render` 结果，顺序按录入。

### 6.18 出库消耗登记
`POST /api/requests/<int:request_id>/consumption`
- 权限：`warehouse`、`admin`。状态要求：申请单须为 `prepping`。
- 请求体：`items[]`，每行：
  ```
  { "coil_id":"260908001", "job_order":"J...", "out_length":1200.0,   // out_length 固定为 mm
    "remark":"...",
    "job_part_number":"...", "shear_qty":10, "shear_length":...,
    "length_tolerance":..., "shear_equipment":"...", "actual_shear_equipment":"..." }
  ```
  后 6 个为宽表字段（`CONSUMPTION_EXTRA_FIELDS`：job_part_number 文本 / shear_qty 整数 / shear_length、length_tolerance 小数 / 设备文本×2），可留空=存 NULL，类型错误返回第 n 行可读 400。
- 逐行校验：
  - 卷标须属本单且 `in_stock`（R9）；
  - 盘点锁定：卷在 `counting` 盘点单中 → 400 禁止消耗（R9.5）；
  - `job_order` 可空，传则须通过 `parse_job` 格式校验；
  - **单位换算（R11）**：服务端按 `kr_wire_coil.unit` 查 `UNIT_CONVERT_FACTOR` 换算 `converted_length`，**不信任前端传值**；单位未知 → 仅警告；
  - **防超发（R8）**：`本次 + 该卷 consume_type='issue' 累计 ≤ coil_length(原始)×系数(mm)`，超 → 400「出库长度超过剩余长度」；
  - 整卷出完（累计≈卷长 mm）→ 卷状态置 `issued`。
- 写入 `consume_type='issue'`、冗余 `siteref=申请单站`（不再写 request_id，经 coil_id 追溯）。
- 响应：`inserted`、`issued[]`（整卷出库的卷号）、`warnings[]`。日志 `OUTBOUND_REGISTER`。

### 6.19 申请单消耗记录查询
`GET /api/requests/<int:request_id>/consumption`
- 权限：`warehouse`、`admin`。
- 逻辑：`kr_wire_coil_consumption c JOIN kr_wire_coil w ON coil_id` 按 `w.request_id` 追溯本单（消耗表无 request_id 列）。
- 响应 `data[]`：消耗全字段（数值列 `out_length/converted_length/shear_length/length_tolerance` 转 float、`created_at` 转字符串）。

---

## 7. 退料（return_bp.py · 10 接口）

流程：生产（requester/warehouse/admin）扫码创建退料单（`request_type='return'`、`status='pending_return'`）→ 仓库确认（逐卷 `in_shop→in_stock` 并回写剩余长度）→ 申请人签字（`confirmed→completed`）；另有手机端复核通道与明细维护。
**剩余长度** = `coil_length(原始单位) − 消耗(mm)÷系数`；扣减口径 = `consumption + count_adjust`（剪线消耗+盘点调整），`issue/scrap` 不计入。
**退料选卷按当前站过滤**：创建退料单只接受本站 `in_shop` 卷标。

### 7.1 创建退料单
`POST /api/returns`
- 权限：`requester`、`warehouse`、`admin`。
- 请求体：`coil_ids[]`（1~200，自动去重）、`remark`。
- 逐卷校验：格式 9 位；须存在且 `siteref=当前站`；状态须 `in_shop`（400「仅在车间的卷标可退料」）；剩余长度须 >0（已消耗完不可退）。任一错误 → 400（HTML `<br>` 拼接，最多 10 条）。
- 落库：主表 `request_type='return'`、`status='pending_return'`；`kr_return_item` 明细（coil_id/part_number/unit/remain_length）；`RETURN_SUBMIT` 日志。
- 响应：`{"success":true,"id":return_id}`。

### 7.2 仓库确认退料
`POST /api/returns/<int:return_id>/confirm`
- 权限：`warehouse`、`admin`。校验 `request_type='return'`、`status='pending_return'`、站点匹配。
- 效果：清单每卷 `status='in_shop'` → `in_stock`，且 `coil_length` 回写为 `remain_length`（原始单位）；主表 `status→confirmed`、写 `confirmed_by`/`confirm_time`；`RETURN_CONFIRM` 日志。

### 7.3 仓库驳回退料
`POST /api/returns/<int:return_id>/reject`
- 权限：`warehouse`、`admin`。
- 请求体：`reason`（可空，默认「仓库驳回」）。
- 前置同 7.2。效果：主表 `status→rejected`、写 `reject_reason`；卷标**状态不变**；`RETURN_REJECT` 日志。

### 7.4 卷标信息查询（退料页带出）
`GET /api/returns/coil-info/<coil_id>`
- 权限：登录即可。卷不存在 404；非当前站卷标 403。
- 响应 `data`：`coil_id/part_number/unit/status/status_label/remain_length/can_return/reason`（`can_return = in_shop 且剩余>0`；否则给出中文 `reason`）。

### 7.5 退料复核页面（HTML）
`GET /return/review`
- 权限：`warehouse`、`admin`。渲染 `return_review.html`（手机端扫码复核）。

### 7.6 复核扫码查询
`GET /api/returns/review/coil/<coil_id>`
- 权限：`warehouse`、`admin`；卷须当前站（403）。
- 响应：卷标信息 + `remain_length` + `matched`（是否命中 `pending_return` 待处理退料单，多单取最新 `return_id`）+ `available_locations`（有匹配且物料存在时，实时查 CSI 该站非 Floor 库位去重列表；失败/空则前端默认 Stock）。
- 卷不存在也返回 `success:true`（coil_id 带出，status=null，matched=false）。

### 7.7 复核退回单卷
`POST /api/returns/review/<coil_id>/confirm`
- 权限：`warehouse`、`admin`。
- 请求体：`return_id`（必填）、`location`（可选，默认 `Stock`）。
- 校验：退料单存在且 `pending_return`、站点匹配；卷存在且 `in_shop`；**盘点锁定**（counting 中 → 400 禁止退料）；卷须在退料清单内。
- 效果：卷 `in_shop→in_stock`、回写 `coil_length=剩余`；`kr_return_item` 写 `review_status='confirmed'`、`location`、`reviewed_by/at`；`RETURN_REVIEW_CONFIRM` 日志。
- **闭环**：退料单无未复核明细（`review_status IS NULL` 为 0）→ 主表 `status→confirmed`。

### 7.8 复核不予退料
`POST /api/returns/review/<coil_id>/reject`
- 权限：`warehouse`、`admin`。请求体：`reason` 必填。
- 卷须当前站；卷标**状态保持不变**。若命中待退料单则写 `review_status='rejected'`、`review_note`，并做同样闭环检查；无匹配也照常记录。日志 `RETURN_REVIEW_REJECT`。

### 7.9 维护退料明细异常原因
`PUT /api/returns/<int:return_id>/note`
- 权限：`requester`、`warehouse`、`admin`。
- 请求体：`coil_id`（必填）、`note`（可空串=清空）。
- 限制：退料单存在；已复核(`confirmed`)行不可改（400）；行不存在 400。
- 效果：写 `review_note`；`RETURN_NOTE_UPDATE` 日志。

### 7.10 申请人签字确认退料交接
`POST /api/returns/<int:return_id>/sign`
- 权限：`requester`、`admin`。请求体：`signature_data`（可空）。
- 前置：单据 `request_type='return'` 且 `status='confirmed'`、站点匹配；**不允许存在**未复核行（`review_status IS NULL`）；`rejected` 行必须已填 `review_note`，否则 400。
- 效果：主表 `status→completed`、写 `signer/sign_time/signature_data`；`RETURN_SIGN` 日志。

---

## 8. 看板（kanban.py · 3 接口）

### 8.1 内部看板卡片
`GET /api/kanban/cards`
- 权限：登录即可。
- 站点：按当前站过滤（`r.siteref`）。
- 范围：未删除且状态 ∈ `pending_prep/prepping/short/ready_pickup/pending_return`（按 `STATUS_ORDER` 分组输出）。
- 卡片字段：主表全字段 + `item_count`、`total_amount`、`job_orders`(逗号连接)、`primary_job_order`、`primary_part_number`、`return_coil_count`、`status_label`、`request_type_label`（`minpack`→「最小包装」、`return`→「退料」，否则「领料」）、`actions[]`（按角色）：
  - `warehouse`：pending_prep→`assign_worker,start_prep`；prepping→`complete_prep,short`；ready_pickup→无；pending_return→`confirm_return,reject_return`
  - `requester`：prepping→`short`；ready_pickup→`sign`
  - `admin`：pending_return→`confirm_return,reject_return`；其余泳道→全按钮
  - 其他角色：`[]`
  - 退料单卡片附 `return_coils[]`（kr_return_item 卷标维度：request_id/coil_id/part_number/unit/remain_length）。
- 响应：`groups{状态:{title,cards[],count}}` + `status_order` + `status_labels`。

### 8.2 公共看板页面
`GET /public/kanban`
- 权限：**无需登录**。渲染 `public_kanban.html`（大屏展示页）。

### 8.3 公共看板数据
`GET /api/public/kanban/cards`
- 权限：**无需登录**。
- query：`siteref`（默认 `310`）。
- 范围：状态 ∈ `pending_prep/prepping/short/ready_pickup`（不含退料泳道）、未删除且 `siteref=参数`。
- 响应结构与 8.1 一致（不含 `actions`/退料明细）。

---

## 9. 工单 / 物料 / 库存验证（validate.py · 4 接口）

> 全部实时调用 Infor CSI IDO API（`CSIClient`）。站点取 `siteref` 参数（body/query），缺省用当前站 `310`；未对目标站点做授权校验（读取性验证，站点即 CSI 公司上下文）。

### 9.1 工单验证
`POST /api/validate/job`
- 权限：登录即可。请求体：`job_full`（如 `J000002124-0004`，必填）、`siteref`（可选）。
- `parse_job` 拆分工单号/后缀；格式无效 400。CSI 查无 → 404「工单不存在」。
- 响应 `data`：`Job/Suffix/Item/Stat/Description`。

### 9.2 物料验证 + 库存
`POST /api/validate/material`
- 权限：登录即可。请求体：`job`、`suffix`、`item`、`siteref`。
- CSI 校验物料在工单 BOM 且需手动领料，否则 400 文案「该物料不在工单BOM中或不需要手动领料」。
- 响应 `data`：`material`、`inventory[]`（loc/qty）、`stock_summary`（`库位(数量)` 串）、`unit_cost`。

### 9.3 按料号查库存（最小包装用）
`GET /api/validate/part-stock`
- 权限：登录即可。query：`part_number`（必填）、`siteref`（可选）。
- 逻辑：CSI `get_inventory`，**排除库位名含 `floor`** 的库位；汇总可发放量并取单价。
- 响应 `data`：`part_number/stock_qty/price/locations(文本)/location_list[]`。
- 注：Backflush 校验逻辑已被注释停用。

### 9.4 物料批号库存（FIFO 分配）
`GET /api/validate/part-lots`
- 权限：登录即可。query：`part_number`（必填）、`qty`（可选 float）、`siteref`。
- CSI `get_item_lots`（FIFO 排序，排除 floor 由 CSI 端处理与否以实际为准——本接口未再本地过滤）；按 `qty_needed` 做 FIFO 逐批分配。
- 响应 `data`：`lots[]`（lot/loc/qty_on_hand…）、`fifo_allocation[]`（lot/loc/qty_take/qty_on_hand）、`total_available`、`qty_needed`。

---

## 10. 裁线规格（cutting.py · 5 接口）

数据表 `kr_cutting_ref` **已站点化**（新增 `siteref` 列）：各站点独立规格，list 只返回当前站；写操作落库当前站；更新/删除带站约束；import 按目标站"删旧插新"覆盖导入。
可编辑 15 个字段：`finished_part, wire_part, wire_awg, color, qty_per_group, cut_length_mm, length_tol, cut_device, device_no, strip_len_a, strip_tol_a, strip_len_b, strip_tol_b, term_a, term_b`（`raw_data` 为导入自动生成，不手工编辑）。数值字段：`qty_per_group/cut_length_mm/strip_len_a/strip_len_b`。
读：登录即可；写：`me_engineer`、`admin`（`WRITE_ROLES`），其他 403「仅 ME 工程师/管理员可修改」。

### 10.1 列表/搜索
`GET /api/cutting-ref`
- 权限：登录即可。
- query：`keyword`（对 finished_part/wire_part/wire_awg/color/term_a/term_b LIKE）、`page`(≥1)、`size`(默认50，1~200)。
- 站点：按当前站过滤。
- 响应：`data[]`（数值列转 float）+ `total/page/size/total_pages` + **`can_edit`**（当前用户是否 me_engineer/admin，供前端显隐按钮）。

### 10.2 新增
`POST /api/cutting-ref`
- 权限：`me_engineer`、`admin`。
- 请求体：15 字段任意子集，但 **`finished_part` 与 `wire_part` 至少填一项**（否则 400）。数值非法转 NULL。
- `siteref = 当前用户站点`（缺失 400「站点信息缺失」）——**各站独立，不可指定他站**。
- 响应 201：`{"success":true,"id":new_id}`。

### 10.3 编辑
`PUT /api/cutting-ref/<int:ref_id>`
- 权限：`me_engineer`、`admin`。请求体：需更新的字段（至少一个）。
- 站点隔离：`SELECT` 存在性校验 + `UPDATE` 均带 `AND siteref = 当前站`（双重约束）；跨站/不存在 404。
- 响应 200：`{"success":true,"message":"更新成功"}`。

### 10.4 删除
`DELETE /api/cutting-ref/<int:ref_id>`
- 权限：`me_engineer`、`admin`。同样 `SELECT`+`DELETE` 双重带站约束；404 语义同 10.3。

### 10.5 批量导入（Excel，站点覆盖）
`POST /api/cutting-ref/import`
- 权限：`me_engineer`、`admin`。`multipart/form-data`：文件字段 `file`，仅 `.xlsx/.xlsm`。
- 目标站点：form/query 参数 `site`（可选）→ 缺省 `当前用户站点`；若显式传 site，须在用户 `available_sites` 内（否则 403「无权向站点 X 导入裁线规格」）。
- 行为（`import_cutting_ref`）：解析 Excel 行 → **先删除该站点全部旧规格 → 再全量插入**（站内覆盖，不影响他站）；空行跳过。
- 响应：`siteref`、`imported`、`skipped`、`message`（含覆盖删除的旧行数）。

---

## 11. 线材管理 · 盘点 · 报表（wire.py · 18 接口）

分三块：卷标查询/导出、消耗查询/导出、线边仓盘点（创建→开启锁定→实测→完成→PDF 报告→差异调整）。
页面路由 4 个 + API 14 个。站点隔离以 `get_site_filter` 为准（卷标/消耗查询按当前站过滤）；**盘点单系列当前未做站点隔离校验**（登录即可，站信息记录于盘点单 `siteref`）。

### 11.1 卷标管理页
`GET /wire/coils`
- 权限：登录（未登录 302 → 登录页）。渲染 `wire_coils.html`。

### 11.2 消耗管理页
`GET /wire/consumption`
- 权限：登录。渲染 `wire_consumption.html`。

### 11.3 盘点管理页
`GET /wire/count`
- 权限：登录。渲染 `wire_count.html`。

### 11.4 差异调整页
`GET /wire/adjust`
- 权限：登录且 `warehouse`/`admin`（否则 403 纯文本）。渲染 `wire_adjust.html`。

### 11.5 卷标扫码查询（盘点选卷用）
`GET /api/wire/coils/lookup/<coil_id>`
- 权限：登录即可。**未做站点隔离**（可查任意卷）。
- 响应 `data`：`coil_id/part_number/lot_no/status/unit/coil_length` + `status_label`（在活跃盘点中显示「盘点中」）+ `can_count`（= `in_shop` 且非盘点中，**仅线边仓可盘点**）。

### 11.6 卷标列表
`GET /api/wire/coils`
- 权限：登录即可。
- query：`coil_id`/`part_number`/`lot_no`(LIKE)、`status`（物理状态精确；特殊值 `finished`=剩余≤0 派生筛选；`counting`=在活跃盘点中）、`siteref`、`date_from`/`date_to`（创建日期）。
- 站点：`get_site_filter` 过滤；上限 500 条，按 `id DESC`。
- 响应 `data[]`：卷标全字段 + `used_mm`(consumption+count_adjust 口径) + `status_label` + `remain_mm`/`remain_orig`（系数换算；单位未知为 null）。

### 11.7 卷标 CSV 导出
`GET /api/wire/coils/export`
- 权限：登录即可。过滤同 11.6（全量无 500 限制）。
- 返回 CSV（UTF-8 BOM，`Content-Disposition: coils.csv`），列：卷标ID/物料/Lot/卷长/单位/状态/站点/申请单ID/使用数量(mm)/创建时间/最后更新时间。

### 11.8 消耗列表
`GET /api/wire/consumption`
- 权限：登录即可。
- query：`coil_id`/`part_number`/`job_order`(LIKE)、`consume_type`(精确)、`date_from`/`date_to`。
- 站点：`get_site_filter`（消耗表 `siteref`）；上限 500。
- 响应 `data[]`：数值列 `out_length/converted_length/shear_length/length_tolerance` 转 float。

### 11.9 消耗 CSV 导出
`GET /api/wire/consumption/export`
- 权限：登录即可。过滤同 11.8（全量）。CSV 列：卷标ID/物料/工单/消耗类型/出库长度(mm)/单位/转换长度/转换单位/Lot批次/登记人/登记时间。

### 11.10 盘点差异调整
`POST /api/wire/adjust`
- 权限：登录 + `warehouse`/`admin`。
- 请求体：`count_no`（盘点单号，大写化，必填）。
- 前置：盘点单存在；`status='completed'`（否则 400「未完成，无法执行差异调整」）；**防重复**：同号已存在 `count_adjust` 记录 → 400。
- 效果：对每条有差异（`diff_mm != 0`）的盘点明细，写一条 `consume_type='count_adjust'` 消耗记录，`job_order=盘点单号`、`operator=操作人`、`is_manual=1`、`siteref=盘点单站`。
- **取号语义**：`adjust_mm = -diff_mm`（账面剩余 R 与盘点实际 A 的关系：写入 −diff 后 剩余=卷长−消耗−(−diff)=R+diff=A）。
- 响应：`message`（含写入卷数）。

### 11.11 盘点单列表
`GET /api/wire/counts`
- 权限：登录即可。**未做站点隔离**。
- 逻辑：`kr_inventory_count ORDER BY id DESC LIMIT 100`，每单附 `item_count`。
- 响应：`data[]`。

### 11.12 创建盘点单
`POST /api/wire/counts`
- 权限：登录即可。
- 请求体：`coil_ids[]`（1~200）、`note`。
- 前置：卷标存在未删；**仅 `in_shop`（线边仓）卷可盘点**，含非 `in_shop` → 400 列出卷号。
- 逻辑：盘点单号 `INV YYYYMMDD + 3位`（当天计数+1）；主表 `siteref = 创建人当前站 or '410'`、`status='pending'`、`created_by`；明细快照 `original_qty/used_mm/remain_mm`（remain_mm = 卷长×系数 − consumption+count_adjust 消耗，单位 mm）。
- 响应：`{"success":true,"message","id"}`。

### 11.13 盘点单详情
`GET /api/wire/counts/<int:count_id>`
- 权限：登录即可。响应 `data`：`{count, items[]}`（明细数值列 `original_qty/used_mm/remain_mm/actual_mm/diff_mm/diff_converted` 转 float，附 `coil_status`）。

### 11.14 删除盘点单
`DELETE /api/wire/counts/<int:count_id>`
- 权限：登录即可。仅 `status='pending'`（待开始）可删（级联删明细），否则 400。

### 11.15 开启盘点（锁定卷标）
`POST /api/wire/counts/<int:count_id>/start`
- 权限：登录即可。仅 `pending` 可开。
- 效果：`status='counting'`、写 `started_at`。**开启后清单内卷标被锁定**（消耗/报废/出库/退料等接口均会拦截，见 1.7）。

### 11.16 录入盘点实测
`POST /api/wire/counts/<int:count_id>/items/<int:item_id>/measure`
- 权限：登录即可。请求体：`actual_mm`（≥0，必填，mm）。
- 前置：盘点单 `status='counting'`。
- 计算：`diff_mm = actual_mm − remain_mm`；`diff_converted = diff_mm ÷ 系数`（单位已知时）。写 `measured_by/measured_at`。
- 响应：`diff_mm`、`diff_converted`。

### 11.17 完成盘点
`POST /api/wire/counts/<int:count_id>/complete`
- 权限：登录即可。仅 `counting` 可完成。
- 前置：所有明细均已录入 `actual_mm`，否则 400「还有 n 卷未录入盘点数量」。
- 效果：`status='completed'`、写 `completed_at`。

### 11.18 盘点 PDF 报告
`GET /api/wire/counts/<int:count_id>/report`
- 权限：登录即可。
- 返回 `application/pdf`（A4 纵向「线边仓线材盘点报告」：信息区 + 中英文双语差异表 + 合计 + 三方签字区）；文件名 `{count_no}_report.pdf`（ASCII，防 HTTP 头乱码）。

---

## 12. 外部集成接口（external.py · 12 接口）

> **服务对象**：naiwiptrack / production-tracking 等外部系统（替代其直连物料领取库）。
> **认证**：请求头 `X-API-Key`（默认 `NAI-WIPTRACK-2026`，env `EXTERNAL_API_KEY`）。见 1.5。
> **站点**：除 confirm-user 外，全部接口强制 `X-Site-Ref`（亦可 `?site=` 或 body `site`，POST/DELETE），解析/规范化/失败语义见 **1.5**；查询/写入一律按解析站点过滤或落库。请求/响应统一骨架：`{"success":bool, "error":...}`（非 message）。
> 站点上下文同时决定 CSI 公司（`CSIClient(siteref=site)`），保证实时库存口径与站点一致。
> 消耗表 `out_length` 单位固定 **mm**；卷长按原始单位换算（系数见 1.7）。

### 12.1 卷标查询（含剩余）
`GET /api/external/coils/<coil_id>`
- X-Site-Ref 必需。卷不存在/跨站 404「卷标不存在」。
- 响应 `data`：`coil_id/part_number/lot_no/unit/coil_length/status/status_label/siteref/request_id` + `counting`（活跃盘点中）+ `used_mm`（consumption+count_adjust 口径）+ `remain_mm`、`remain_orig`（换算；单位未知为 null）。`status_label` 盘点中时显示「盘点中」。

### 12.2 消耗登记（剪线消耗，stage=complete）
`POST /api/external/consumption`
- X-Site-Ref 必需（可从 body `site` 取）。必填：`coil_id`、`part_number`。
- 数量：`shear_qty`(>0)、`actual_shear_length`(>0,mm)、`scrap_length_actual`(≥0,mm 可省)。`out_length = qty×实际长度 + 报废`。
- 其余字段：`job_order`、`job_part_number`、`cut_length_mm`、`length_tolerance`、`shear_equipment`、`shear_device_no`、`actual_shear_equipment`、`operator`(默认 unknown)、`checker`、`is_manual`、`remark`。
- 前置校验：卷存在且站点匹配；**卷状态必须 `in_shop`**（否则 400「卷标ID的状态不正确」）；**盘点锁定** 400；`out_length+累计(consumption+count_adjust) ≤ 卷长mm`（超 → 400「消耗超限…」）。
- 落库：`consume_type='consumption'`、`stage='complete'`、`converted_length/unit` 服务端换算、冗余 `siteref`。
- 响应：`id`、`consume_type`、`out_length`、`converted_length`、`converted_unit`、`remaining_mm`。

### 12.3 报废登记
`POST /api/external/consumption/scrap`
- X-Site-Ref 必需。必填：`coil_id`、`part_number`；`out_length`(报废长度 mm，>0)。
- 前置：卷 `in_shop` + 盘点锁定 + `scrap_len+累计 ≤ 卷长mm`（超 → 400「报废超限…」）。
- 落库：`consume_type='scrap'`。`operator`/`remark` 可选。
- 响应：`id`、`consume_type`、`out_length`。

### 12.4 首末件检查登记
`POST /api/external/cutting-check`
- X-Site-Ref 必需。必填：`job_order`、`part_number`；`shear_actual_length`(>0,mm)、`shear_checker`(确认人)。`check_type`=`last` 否则 `first`。
- 公差校验：`shear_std_length/actual + shear_std_tol`（±0.5 与 0.5 均接受）、`strip_a_*`、`strip_b_*`（去皮段 optional）；超差时若 **`force=true`** 则放行，否则 400 `{"needForce": true, "error":"剪线长度超出公差，需确认人授权"}`。
- 其他可传：`cut_length_mm`、`is_manual`、各 std/actual/device/operator/checker 字段、`scrap_length`。
- 落库 `kr_cutting_check`（含 `siteref`）。响应：`id`、`check_type`。

### 12.5 确认人密码校验（无站点）
`POST /api/external/confirm-user`
- **不要求 X-Site-Ref**（确认人跨站共用）。请求体：`password` 必填。
- 匹配 `cutting_confirm_user` 表；无表记录则回退 env `CUTTING_CONFIRM_PASSWORD`（命中返回 `CUTTING_CONFIRM_NAME`，默认「线长」）。错误 400「确认密码错误」。
- 成功：`{"success":true,"name":...}`。

### 12.6 删除消耗记录（需确认密码）
`DELETE /api/external/consumption/<int:record_id>`
- X-Site-Ref 必需（body 可传）。请求体：`password` 必填（校验同 12.5）。
- 纵深防御：SELECT + DELETE 均带 `AND siteref = 站点`。不存在 404。
- 成功：`{"success":true,"message","id"}`。

### 12.7 消耗记录分页列表
`GET /api/external/consumption/list`
- X-Site-Ref 必需。query：`page`(默认1)、`pageSize`(默认20，≤200)、`job`/`part`(LIKE)、`coilId`(精确)、`startDate`/`endDate`（created_at 区间，补时分秒）。
- 强制 `siteref = 站点`。响应行含 `consume_type_label`（Scrap/consumption）、`stage`（空显示 first）、数值列 float；`total/page/pageSize`。

### 12.8 卷标信息分页列表
`GET /api/external/coils/list`
- X-Site-Ref 必需。query：`page`/`pageSize`、`coilId`(精确)、`part`(LIKE)、`status`(精确)。
- LEFT JOIN 消耗聚合：`used_mm`(consumption)、`scrapped_mm`(scrap)、`total_used_mm`、`remain_mm`/`remain_orig`。`status_label` 中文。
- 响应：`data[]` + `total/page/pageSize`。

### 12.9 消耗查询（无分页）
`GET /api/external/consumption`
- X-Site-Ref 必需。query：`coil_id`、`job_order`（精确，可只传一或都传）。强制站点过滤。
- 返回最近 **200** 条，字段同 12.7。

### 12.10 首末件检查查询
`GET /api/external/cutting-check`
- X-Site-Ref 必需。query：`job_order`、`part_number`、`check_type`（精确过滤，均可选）。强制站点过滤，最近 200 条。数值列 float。

### 12.11 裁剪参数查询
`GET /api/external/cutting-ref`
- X-Site-Ref 必需。**`finished_part` 必传**（否则 400「请提供 finished_part 过滤」），可选 `wire_part`。
- `kr_cutting_ref` 站点化：强制 `siteref = 解析站点`。
- 响应 `data[]`：`id/finished_part/wire_part/qty_per_group/cut_length_mm/length_tol/cut_device/device_no/strip_len_a/strip_tol_a/strip_len_b/strip_tol_b/term_a/term_b`（4 个数值列转 float），按 `wire_part, cut_length_mm` 排序。

### 12.12 库位库存查询（实时 CSI，非下载表）
`GET /api/external/part-stock/<part>`
- X-Site-Ref 必需。
- 数据源：**实时** Infor CSI IDO `SLItemLocs`（`CSIClient.get_inventory`，QtyOnHand>0，站点公司上下文），非 csi_datawarehouse 下载表。
- 归类：库位名含 `floor`（不区分大小写）→ Floor；其余 → Other。数量按 `UNIT_CONVERT_FACTOR` 换算 `*_mm`。
- CSI 异常 → 502「CSI 实时库存查询失败」。
- 响应 `data`（与旧 CSI 直连结构兼容）：`item/unit/floor_qty/other_qty/total_on_hand` + `*_mm` + `floor_locations/other_locations[]`（location/qty/unit/qty_mm）。

---

## 13. 系统管理（admin.py · 12 接口）

权限分两级：角色映射与站点打印机**仅 `admin`**（`check_admin`，其他角色 403「权限不足」）；日志查询**登录即可**。

### 13.1 角色映射列表
`GET /api/role-mappings`
- 权限：`admin`。query：`siteref`、`role`（可选过滤）。返回 `kr_role_mapping` 全部行按 id 排序。

### 13.2 创建角色映射
`POST /api/role-mappings`
- 权限：`admin`。
- 请求体：`domain_account`、`role`（必填，合法值 `admin/requester/supervisor/warehouse/me_engineer`）；`siteref`（**非 admin/me_engineer 必填**且须在 `SITE_CONFIG`）；`site_access`（可选，逗号分隔多站；为空=仅默认站；自动补默认站、过滤非法站、排序、去重，`admin` 强制为 NULL）；`display_name/email/is_active(默认1)/remark`。
- 落库（站内角色映射**无 id 存在性去重校验**，重复 domain_account 会并存）。成功 201 带 `id`；IntegrityError/其他 400。

### 13.3 更新角色映射
`PUT /api/role-mappings/<int:mapping_id>`
- 权限：`admin`。不存在 404。
- 可更新字段（传哪个更新哪个）：`display_name/role/email/is_active/remark/domain_account/siteref/site_access`；空请求体 400。
- `site_access` 按**更新后**的 role/siteref 重新规范化；`siteref` 空串→NULL。

### 13.4 删除角色映射
`DELETE /api/role-mappings/<int:mapping_id>`
- 权限：`admin`。不存在 404。物理删除。

### 13.5 操作日志列表
`GET /api/logs`
- 权限：登录即可。query：`request_id`(精确)、`operator`(LIKE)、`action`(精确)、`page`(默认1)、`size`(默认50)。
- 响应：`data[]`(按 id DESC) + `total/page/size/total_pages`。

### 13.6 操作日志详情
`GET /api/logs/<int:log_id>`
- 权限：登录即可。不存在 404。返回单行。

### 13.7 站点打印机列表
`GET /api/site-printers`
- 权限：`admin`。返回 `kr_site_printer` 全表按 `siteref` 排序。

### 13.8 新增站点打印机
`POST /api/site-printers`
- 权限：`admin`。
- 请求体：`siteref`（必填且在 `SITE_CONFIG`）、`printer_name`（必填）、`channel`（默认 `gdi`，合法 `gdi/raw_zpl/raw_tspl/gateway`）、`remark`。
- **每站唯一**：重复 400「该站点已配置」（IntegrityError）。成功 201 带 `id`。

### 13.9 更新站点打印机
`PUT /api/site-printers/<int:printer_id>`
- 权限：`admin`。不存在 404。可更新 `printer_name/channel/remark`（channel 校验同 13.8）；空请求 400。

### 13.10 删除站点打印机
`DELETE /api/site-printers/<int:printer_id>`
- 权限：`admin`。不存在 404。物理删除。

### 13.11 打印机连接测试（新增）
`POST /api/site-printers/<int:printer_id>/test-connection`
- 权限：`admin`。记录不存在 404。
- 按该配置通道探测打印机可达性（**不实际打印**），经 `label_print_service.test_printer_connection(printer_name, channel)`。
- 响应：`success`、`message`、`detail`、`printer_name`、`channel`。

### 13.12 打印机测试页打印（新增）
`POST /api/site-printers/<int:printer_id>/test-print`
- 权限：`admin`。记录不存在 404。
- 按配置通道实际打印一张固定测试标签（`print_test_page`）。
- 响应：`success/message/detail/printed/errors/printer_name/channel`。

---

## 附录 A：接口总索引与自检对照

以下按蓝图文件统计路由条数（含页面路由，均以代码 `@xxx_bp.route` 装饰器为准），本文档已逐一覆盖：

| 蓝图文件 | 路由数 | 文档章节 | 覆盖 |
|---|---|---|---|
| auth.py | 4 | §2.1~2.4 | ✅ |
| request_bp.py | 8 | §3.1~3.8 | ✅ |
| approval.py | 4 | §4.1~4.4 | ✅ |
| warehouse.py | 10 | §5.1~5.10 | ✅ |
| coil.py | 19 | §6.1~6.19 | ✅ |
| return_bp.py | 10 | §7.1~7.10 | ✅ |
| kanban.py | 3 | §8.1~8.3 | ✅ |
| validate.py | 4 | §9.1~9.4 | ✅ |
| cutting.py | 5 | §10.1~10.5 | ✅ |
| wire.py | 18 | §11.1~11.18 | ✅ |
| external.py | 12 | §12.1~12.12 | ✅ |
| admin.py | 12 | §13.1~13.12 | ✅ |
| **合计** | **109** | — | ✅ |

## 附录 B：相对旧版 `API 文档.md` 的主要差异（新增/变更）

1. **多站点 siteref 体系**：`kr_role_mapping` 新增 `site_access`（多站授权）与规范化逻辑；登录返回 `available_sites`；`switch-site` 放开至任意角色（限各自可访问站）；`get_site_filter` 当前站过滤语义统一（见 §1.3）。旧文档为单站假设，本次全文改写。
2. **裁线规格 `kr_cutting_ref` 站点化**：list/create/update/delete/import 全部按站隔离，import 从"全表覆盖"改为"**按目标站删旧插新**"（§10）。
3. **卷标/消耗带站**：`kr_wire_coil` 新增 `siteref` 列；出库登记（§6.18）与盘点调整（§11.10）落 `siteref`；wire 报表按用户站过滤（§11.6/11.8）；退料选卷按站过滤（§7.1）。
4. **打印改 Bartender 触发文件**：print 接口不再直接向打印机发指令，改生成 `.dd` 文件写入 `LABEL_PRINT_BTW_DIR`（默认 `\\172.26.1.7\Coil_Label_Scanned`）；打印机改按站点从 `kr_site_printer` 解析（§6.14/6.15）。
5. **卷标编辑接口（新）**：`PUT /api/coils/<id>` 改 Lot/长度（prepping + in_stock + 改长须 ≥ 已消耗），并配套 `Lot 验证 POST /api/coils/validate-lot`（§6.7/6.2）。
6. **打印机配置页（新）**：`/api/site-printers` CRUD + `test-connection` / `test-print`（§13.7~13.12）。
7. **回到备料（新）**：`POST /api/requests/<id>/back-to-prepping`（ready_pickup→prepping，§5.4）。
8. **卷号生成修复**：`_gen_next_id` 改为取**含软删** `MAX+1`（`FOR UPDATE` 当前读），保证软删占号不复用、并发不撞唯一键（§6.1/6.3、1.7）。
9. **盘点/消耗联动**：消耗、报废、退料、出库登记均增加「活跃盘点锁定」校验；wire 盘点单闭环流程（§11）。
10. **物料明细行绿底**：前端展示变更，无接口差异，不在本文档契约范围内。

