# 线卷全库存管理功能开发文档

- 版本：v1.0
- 日期：2026-08-14
- 作者：查明科（需求分析师）
- 关联系统：物料领取看板（Flask + MySQL，Infor CSI 集成）
- 适用对象：开发人员、测试人员、仓库业务用户

---

## 1. 需求概述与功能模块划分

### 1.1 业务目标

对每一卷线材（Wire Coil）进行全生命周期库存管理：

1. **实时跟踪库存**：每一卷线在系统中有一条库存记录（线卷库存表），可查询在库/已出库/报废状态；
2. **跟踪消耗去向**：记录每一卷线被哪个工单领取了多少长度（线卷消耗记录表）；
3. **报废管理**：报废长度的登记在后续迭代实现（本期仅建表 + 预留字段）。

### 1.2 范围界定（本期范围）

| 编号 | 功能 | 本期是否实现 |
|------|------|-------------|
| F1 | 卷标信息录入（仅最小包装 minpack 申请单、备料中 prepping 状态、仓库/管理员） | ✅ |
| F2 | 卷标信息独立表维护（线卷库存表 kr_wire_coil） | ✅ |
| F3 | 消耗记录表建表（kr_wire_coil_consumption） | ✅ 仅建表 |
| F4 | 出库消耗登记（写入消耗记录表） | ✅ |
| F5 | 报废长度登记 | ❌ 后续迭代 |
| F6 | 条形码标签打印（3 inch × 1 inch） | ✅ |
| F7 | 单位自动获取（CSI 物料单位字段，只读展示） | ✅ |
| F8 | 校验规则（A/B 开头强制、卷号唯一、每日 999 上限） | ✅ |

### 1.3 功能模块划分

```
┌─────────────────────────────────────────────────────────────┐
│                    线卷全库存管理                            │
├───────────────┬─────────────────────────────────────────────┤
│  模块          │  说明                                       │
├───────────────┼─────────────────────────────────────────────┤
│ 1. 卷标录入    │ 备料中 minpack 申请单 → "卷标信息"弹窗       │
│                │ 多行录入：卷标ID/长度/单位（CSI自动回填）    │
├───────────────┼─────────────────────────────────────────────┤
│ 2. 库存维护    │ kr_wire_coil 独立表，每卷一行，状态流转      │
│                │ in_stock → issued（本期）→ scrapped（下期） │
├───────────────┼─────────────────────────────────────────────┤
│ 3. 出库登记    │ 出库消耗登记写入 kr_wire_coil_consumption   │
│                │ 记录：哪卷被哪个工单领取多少长度             │
├───────────────┼─────────────────────────────────────────────┤
│ 4. 标签打印    │ 3"×1" 标签：卷号/条码 + 物料/条码 + 长度/单位│
│                │ Windows 打印驱动输出（详见第 5 节）          │
├───────────────┼─────────────────────────────────────────────┤
│ 5. 库存查询    │ 卷标列表查询（按物料/状态/日期筛选）          │
│                │ 后续迭代可扩展看板、报表                     │
└───────────────┴─────────────────────────────────────────────┘
```

### 1.4 业务规则要点

- 卷标录入入口：**仅** `request_type = 'minpack'` 且状态为 `prepping` 的申请单，操作角色为仓库（warehouse）或管理员（admin）；
- 校验规则：物料号以 **A 或 B 字母开头**（不区分大小写）的物料，卷标信息**必须填写**；其他开头物料**可选填**；
- 卷标ID：`年份2位 + 月份2位 + 日期2位 + 流水号3位`，共 9 位数字，如 `260814001`，全局唯一；每天最多 999 卷，超出提示；
- 单位：由系统通过 CSI API 查询物料单位字段，**只读展示**，禁止手工修改；
- 卷标保存成功后，状态为 `in_stock`（在库）；出库登记后若整卷出完则更新为 `issued`（已出库）。

---

## 2. 数据库表设计（DDL）

### 2.1 设计约定

- 数据库：MySQL 5.7+，字符集 utf8mb4，引擎 InnoDB；
- **不使用物理外键约束**（沿用现有 `kr_request_item` 等表的设计：只建普通索引，外键关系由应用层保证），理由：现有系统全部为逻辑外键 + 应用层校验，保持一致并避免级联操作风险；
- 字段命名、注释风格与现有表（`kr_material_request`、`kr_request_item`）保持一致；
- 站点隔离：线卷属于特定站点，通过冗余 `siteref` 字段（可从来源申请单继承）支持按站点查询；
- 新增表通过**增量迁移脚本**（`CREATE TABLE IF NOT EXISTS`）添加到现有数据库，**不修改** `app/init_db.py` 中已有表的 DROP/CREATE 逻辑（注意：现有 `init_db.py` 的 DDL 与运行时代码已存在差异——运行时代码中 `kr_material_request.request_type / is_urgent`、`kr_request_item.stock_loc / batch_no / short_reason` 等字段在 `init_db.py` 中未体现，本次新增表同样独立迁移，不合并进重建脚本）。

### 2.2 线卷库存表 kr_wire_coil（每卷线一行）

```sql
CREATE TABLE IF NOT EXISTS kr_wire_coil (
  id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  coil_id         VARCHAR(16)   NOT NULL COMMENT '卷标ID：YYMMDD+3位流水号，共9位数字，如260814001，全局唯一',
  part_number     VARCHAR(128)  NOT NULL COMMENT '物料 Part Number',
  coil_length     DECIMAL(12,2) NOT NULL COMMENT '长度（数值，保留2位小数）',
  unit            VARCHAR(16)   DEFAULT NULL COMMENT '单位，自动取自CSI物料单位字段（只读回填，如M/FT/EA）',
  status          VARCHAR(20)   NOT NULL DEFAULT 'in_stock' COMMENT '状态：in_stock在库 / issued已出库 / scrapped报废',
  request_id      INT           NOT NULL COMMENT '来源申请单ID（逻辑关联 kr_material_request.id）',
  siteref         VARCHAR(16)   NOT NULL COMMENT '站点：310-苏州 / 410-槟城（冗余自申请单）',
  operator        VARCHAR(64)   NOT NULL COMMENT '录入操作人工号',
  remark          VARCHAR(256)  DEFAULT NULL COMMENT '备注',
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_coil_id (coil_id),
  INDEX idx_part_number (part_number),
  INDEX idx_status (status),
  INDEX idx_request_id (request_id),
  INDEX idx_siteref (siteref),
  INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线卷库存表（每卷线一行）';
```

字段说明：

| 字段 | 说明 | 关键点 |
|------|------|--------|
| coil_id | 卷标ID | 9位数字（YYMMDD+3位流水），UNIQUE 约束保证全局唯一，同时支撑"每日999上限"统计 |
| status | 状态 | `in_stock` 录入即默认在库；出库整卷完成后更新 `issued`；`scrapped` 本期预留，报废登记下期启用 |
| unit | 单位 | 由后端调用 CSI 回填，前端只读；CSI 查询失败时允许为空并给出警告（不阻断录入） |

### 2.3 线卷消耗记录表 kr_wire_coil_consumption

```sql
CREATE TABLE IF NOT EXISTS kr_wire_coil_consumption (
  id                     BIGINT          NOT NULL AUTO_INCREMENT COMMENT '主键',
  coil_id                VARCHAR(16)     NOT NULL COMMENT '卷标ID（逻辑关联 kr_wire_coil.coil_id）',
  job_order              VARCHAR(64)     DEFAULT NULL COMMENT '消耗/领取的工单号（minpack申请单可空）',
  part_number            VARCHAR(128)    NOT NULL COMMENT '物料 Part Number（冗余自线卷表，便于独立查询）',
  consume_type           VARCHAR(20)     NOT NULL DEFAULT 'issue' COMMENT '消耗类型：issue出库 / scrap报废（报废登记后续迭代）',
  out_length             DECIMAL(12,2)   NOT NULL COMMENT '消耗/出库长度（原始录入值，单位固定 mm）',
  unit                   VARCHAR(16)     DEFAULT NULL COMMENT 'CSI 物料单位（冗余自线卷表，作为单位换算的源单位，如 M/FT）',
  converted_length       DECIMAL(12,2)   DEFAULT NULL COMMENT '转换后长度（mm 按系数换算为 CSI 单位，服务端计算）',
  converted_unit         VARCHAR(16)     DEFAULT NULL COMMENT '转换后单位（即 CSI 单位）',
  -- 基础/线材组
  job_part_number        VARCHAR(64)     DEFAULT NULL COMMENT '工单物料号',
  wire_spec              VARCHAR(128)    DEFAULT NULL COMMENT '线材规格型号（如 0.5mm²/22AWG/UL1007）',
  color                  VARCHAR(32)     DEFAULT NULL COMMENT '颜色',
  shear_qty              INT             DEFAULT NULL COMMENT '剪切数量',
  shear_length           DECIMAL(12,2)   DEFAULT NULL COMMENT '剪切长度',
  length_tolerance       DECIMAL(10,2)   DEFAULT NULL COMMENT '长度公差',
  shear_equipment        VARCHAR(64)     DEFAULT NULL COMMENT '剪切设备',
  actual_shear_equipment VARCHAR(64)     DEFAULT NULL COMMENT '实际剪切设备',
  checker_first          VARCHAR(64)     DEFAULT NULL COMMENT '首件确认人',
  checker_last           VARCHAR(64)     DEFAULT NULL COMMENT '末件确认人',
  -- A端去皮组
  strip_len_a            DECIMAL(12,2)   DEFAULT NULL COMMENT '去皮尺寸A端',
  strip_tol_a            DECIMAL(10,2)   DEFAULT NULL COMMENT '去皮尺寸公差A端',
  strip_equip_a          VARCHAR(64)     DEFAULT NULL COMMENT '去皮A端设备',
  strip_actual_equip_a   VARCHAR(64)     DEFAULT NULL COMMENT 'A端实际设备',
  checker_first_a        VARCHAR(64)     DEFAULT NULL COMMENT 'A端首件确认人',
  checker_last_a         VARCHAR(64)     DEFAULT NULL COMMENT 'A端末件确认人',
  -- B端去皮组
  strip_len_b            DECIMAL(12,2)   DEFAULT NULL COMMENT '去皮尺寸B端',
  strip_tol_b            DECIMAL(10,2)   DEFAULT NULL COMMENT '去皮尺寸公差B端',
  strip_equip_b          VARCHAR(64)     DEFAULT NULL COMMENT '去皮B端设备',
  strip_actual_equip_b   VARCHAR(64)     DEFAULT NULL COMMENT 'B端实际设备',
  checker_first_b        VARCHAR(64)     DEFAULT NULL COMMENT 'B端首件确认人',
  checker_last_b         VARCHAR(64)     DEFAULT NULL COMMENT 'B端末件确认人',
  -- 打端组
  crimp_machine_a        ENUM('Yes','No') DEFAULT NULL COMMENT 'A端打端一体机作业',
  crimp_machine_b        ENUM('Yes','No') DEFAULT NULL COMMENT 'B端打端一体机作业',
  manual_crimp_flow      VARCHAR(256)    DEFAULT NULL COMMENT '备料手工打端区作业流程',
  -- A端端子预加工组
  prep_time_a            DECIMAL(10,2)   DEFAULT NULL COMMENT 'A端单根预加工备料工时（s）',
  terminal_part_a        VARCHAR(128)    DEFAULT NULL COMMENT 'A端端子料号',
  equip_no_a             VARCHAR(64)     DEFAULT NULL COMMENT '设备编号',
  die_no_a               VARCHAR(64)     DEFAULT NULL COMMENT '刀模编号',
  height_mm_a            DECIMAL(10,2)   DEFAULT NULL COMMENT '高度（mm）',
  height_tol_mm_a        DECIMAL(10,2)   DEFAULT NULL COMMENT '高度公差（mm）',
  pull_force_a           DECIMAL(10,2)   DEFAULT NULL COMMENT '拉力',
  loose_chain_a          ENUM('散','链') DEFAULT NULL COMMENT '散端/链端（散/链）',
  terminal_qty_a         INT             DEFAULT NULL COMMENT 'A端端子用量',
  preinstall_remark_a    VARCHAR(256)    DEFAULT NULL COMMENT '是否提前预装件备注',
  -- B端端子预加工组
  prep_time_b            DECIMAL(10,2)   DEFAULT NULL COMMENT 'B端单根预加工备料工时（s）',
  terminal_part_b        VARCHAR(128)    DEFAULT NULL COMMENT 'B端端子料号',
  equip_no_b             VARCHAR(64)     DEFAULT NULL COMMENT '设备编号',
  die_no_b               VARCHAR(64)     DEFAULT NULL COMMENT '刀模编号',
  height_mm_b            DECIMAL(10,2)   DEFAULT NULL COMMENT '高度（mm）',
  height_tol_mm_b        DECIMAL(10,2)   DEFAULT NULL COMMENT '高度公差（mm）',
  pull_force_b           DECIMAL(10,2)   DEFAULT NULL COMMENT '拉力',
  loose_chain_b          ENUM('散','链') DEFAULT NULL COMMENT '散端/链端（散/链）',
  terminal_qty_b         INT             DEFAULT NULL COMMENT 'B端端子用量',
  preinstall_remark_b    VARCHAR(256)    DEFAULT NULL COMMENT '是否提前预装件备注',
  operator               VARCHAR(64)     NOT NULL COMMENT '登记操作人工号',
  remark                 VARCHAR(256)    DEFAULT NULL COMMENT '备注',
  created_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '登记时间',
  updated_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  INDEX idx_coil_id (coil_id),
  INDEX idx_job_order (job_order),
  INDEX idx_consume_type (consume_type),
  INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线卷消耗记录表（每卷每次出库/报废一行，宽表存储线材加工过程参数）';
```

字段说明：

| 字段 | 说明 | 关键点 |
|------|------|--------|
| consume_type | 消耗类型 | 本期仅写入 `issue`；`scrap` 类型为下期报废登记预留 |
| job_order | 工单号 | minpack 申请单明细无工单（`kr_request_item.job_order` 为 NULL），此处可为空；normal 类后续若接入可为工单号 |
| coil_id + out_length | 哪卷消耗多少（原始值，mm） | 支持一卷多次出库（每次一条记录）；已移除 `request_id`，追溯申请单通过 `coil_id → kr_wire_coil.request_id` 上查 |
| out_length / unit / converted_length / converted_unit | 单位换算三元组 | `out_length` 为录入原始值（mm）；服务端按 `unit`（CSI 单位）查换算系数计算 `converted_length`，`converted_unit` 冗余换算目标单位 |
| 宽表加工字段（基础/去皮/打端/端子预加工组） | 线材加工过程参数 | 全部可空，出库登记时按弹窗分组填写；未填写保存为 NULL 不报错 |

#### 2.3.1 单位换算规则（换算系数表）

换算公式：`converted_length = out_length(mm) ÷ 系数`，系数按该物料在 CSI 中的单位（`unit` 字段）确定：

| CSI 单位 | 换算系数（mm ÷ 系数 = 目标单位值） | 示例（out_length = 250.50 mm） | 说明 |
|----------|----------------------------------|-------------------------------|------|
| M | 1000 | 250.50 ÷ 1000 = 0.2505 | 米 |
| FT | 304.8 | 250.50 ÷ 304.8 = 0.8219 | 英尺，1 ft = 304.8 mm |
| CM | 10 | 250.50 ÷ 10 = 25.050 | 厘米 |
| IN | 25.4 | 250.50 ÷ 25.4 = 9.8622 | 英寸，1 in = 25.4 mm |
| 其他 / 未收录单位 | 由实施人员按 CSI 实际单位定义补充系数 | — | 未匹配时 `converted_length` / `converted_unit` 置 NULL，页面给出警告但不阻断 |

实施说明：
- 换算系数统一维护在服务端配置（建议 `app/config.py`：`UNIT_CONVERT_FACTOR = {'M': 1000, 'FT': 304.8, 'CM': 10, 'IN': 25.4}`）；
- `converted_length` / `converted_unit` **仅由服务端计算并覆盖**，不信任前端传值；
- CSI 单位查询失败或未收录时，转换结果允许为空并给出警告，不影响出库登记（与第 6 节单位回填降级策略一致）。

### 2.4 外键关系图（逻辑关系）

```
kr_material_request (1) ──< (N) kr_request_item
        │
        │ request_id
        ▼
kr_wire_coil (1) ──< (N) kr_wire_coil_consumption
     │ coil_id                  ▲ job_order
     │                          │
     └── 冗余 siteref           └── 可追溯回工单
```

> 消耗记录表已移除 `request_id` 列；需要追溯来源申请单时，通过 `kr_wire_coil_consumption.coil_id → kr_wire_coil.coil_id` 关联，再取 `kr_wire_coil.request_id`。

### 2.5 索引设计说明

- `kr_wire_coil.uk_coil_id`：全局唯一，防重复录入，同时作为每日上限校验的统计依据；
- `kr_wire_coil.idx_request_id`：按申请单查卷标（详情页展示）；
- `kr_wire_coil_consumption.idx_job_order`：后续"按工单追溯消耗"查询；
- 均单列索引即可满足本期查询场景，不引入复合索引（避免过度设计）。

---

## 3. API 接口设计（RESTful）

### 3.1 通用约定

- 基础路径：`/api`；
- 认证：沿用现有 `session` 登录态校验（`session['user']`），未登录返回 401；
- 权限：卷标录入/打印/出库登记仅限 `warehouse`、`admin` 角色，返回 403；
- 站点隔离：沿用 `get_site_filter(user)` 工具，非 admin 仅能操作本站点数据；
- 响应格式：统一 `{ "success": bool, "message": str, ... }`；错误返回 4xx/5xx 及 `message`；
- 操作日志：所有写操作同步写入 `kr_operation_log`（action 前缀建议 `COIL_` / `OUTBOUND_`）。

### 3.2 接口清单

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 1 | GET | `/api/coils/next-id` | 生成卷号（按当天日期返回下一可用流水） |
| 2 | POST | `/api/requests/<request_id>/coils` | 卷标信息批量录入（写 kr_wire_coil） |
| 3 | GET | `/api/requests/<request_id>/coils` | 查询某申请单已录入的卷标列表 |
| 4 | GET | `/api/coils` | 线卷库存列表查询（多条件筛选 + 分页） |
| 5 | GET | `/api/requests/<request_id>/coil-units` | 批量获取申请单各物料的 CSI 单位（弹窗初始化用） |
| 6 | POST | `/api/coils/print` | 标签打印（批量） |
| 7 | GET | `/api/coils/<coil_id>/label` | 获取单卷标签渲染数据（前端预览用） |
| 8 | POST | `/api/requests/<request_id>/consumption` | 出库消耗登记（写 kr_wire_coil_consumption） |
| 9 | GET | `/api/requests/<request_id>/consumption` | 查询申请单的消耗登记记录 |

### 3.3 接口详情

#### 3.3.1 卷号生成

```
GET /api/coils/next-id
```
- 请求参数：`date`（可选，格式 YYYY-MM-DD，默认取服务器当天日期，用于联调/测试）
- 响应：

```json
{
  "success": true,
  "data": {
    "coil_id": "260814001",
    "date": "2026-08-14",
    "date_prefix": "260814",
    "seq": 1,
    "daily_count": 0,
    "daily_limit": 999
  }
}
```

- 逻辑：
  1. 计算前缀 `date_prefix = YYMMDD`；
  2. `SELECT COUNT(*) FROM kr_wire_coil WHERE coil_id LIKE '260814%'` 得到当日已用数量 `daily_count`；
  3. 若 `daily_count >= 999` 返回 400 `"当日卷号已用完（每天最多999卷）"`；
  4. `seq = daily_count + 1`，`coil_id = date_prefix + f"{seq:03d}"`。
- 并发说明：正式保存时依赖 `uk_coil_id` 唯一索引，若并发冲突捕获 `pymysql.err.IntegrityError`（Duplicate entry）后重新取号重试（最多重试 3 次）。

#### 3.3.2 卷标信息批量录入

```
POST /api/requests/<request_id>/coils
```

请求体：

```json
{
  "items": [
    {
      "part_number": "A123456",
      "coil_id": "260814001",
      "length": 250.50,
      "unit": "M"
    },
    {
      "part_number": "W0303408",
      "coil_id": "260814002",
      "length": 100.00,
      "unit": "M"
    }
  ]
}
```

响应（成功）：

```json
{
  "success": true,
  "message": "已录入 2 卷卷标",
  "inserted": 2,
  "data": [
    { "id": 1, "coil_id": "260814001", "part_number": "A123456", "length": 250.50, "unit": "M", "status": "in_stock" }
  ]
}
```

校验逻辑（详见第 7 节）：
1. 申请单存在且 `request_type='minpack'`、`status='prepping'`（后端强制，不信任前端）；
2. 角色为 warehouse/admin，站点匹配；
3. `items` 非空，最多单次 500 行；
4. 每行：`part_number` 必须属于该申请单明细；`coil_id` 满足 9 位数字正则；`length > 0`；A/B 开头物料必填（前端已拦，后端兜底）；
5. `unit` 由**后端重新调 CSI 获取并覆盖**（防止前端篡改），CSI 失败置空并警告；
6. 逐行 INSERT，捕获唯一键冲突回滚并提示具体卷号。

#### 3.3.3 申请单卷标列表

```
GET /api/requests/<request_id>/coils
```

响应：

```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "coil_id": "260814001",
      "part_number": "A123456",
      "length": 250.50,
      "unit": "M",
      "status": "in_stock",
      "status_label": "在库",
      "operator": "warehouse1",
      "created_at": "2026-08-14 09:30:00"
    }
  ]
}
```

#### 3.3.4 线卷库存列表查询

```
GET /api/coils?part_number=A123456&status=in_stock&date_from=2026-08-01&date_to=2026-08-14&page=1&size=20
```

- 参数：`part_number`、`status`、`request_id`、`date_from`、`date_to`（按 created_at）、`page`、`size`
- 站点隔离：非 admin 自动加 `siteref` 过滤
- 响应（沿用现有分页风格）：

```json
{
  "success": true,
  "data": [ { "id": 1, "coil_id": "260814001", "part_number": "A123456", "length": 250.50, "unit": "M", "status": "in_stock", "status_label": "在库", "request_id": 100, "siteref": "310", "created_at": "2026-08-14 09:30:00" } ],
  "total": 1,
  "page": 1,
  "size": 20,
  "total_pages": 1
}
```

#### 3.3.5 批量获取物料单位（CSI）

```
GET /api/requests/<request_id>/coil-units
```

响应：

```json
{
  "success": true,
  "data": { "A123456": "M", "W0303408": "M" }
}
```

- 服务端：取申请单全部明细物料号，逐个调 `CSIClient.get_item_unit()`（会话级 dict 缓存，避免重复请求）；CSI 查不到的物料返回空字符串并前端显示"-"。

#### 3.3.6 标签打印

```
POST /api/coils/print
```

请求体：

```json
{
  "coil_ids": ["260814001", "260814002"],
  "printer": "ZT411"     // 可选，默认取 Config.LABEL_PRINTER_NAME
}
```

响应（成功）：

```json
{
  "success": true,
  "message": "已提交 2 张标签打印",
  "printed": 2,
  "errors": []
}
```

响应（部分失败）：

```json
{
  "success": false,
  "message": "部分标签打印失败",
  "printed": 1,
  "errors": [ "260814002: 打印机 ZT411 未找到或未连接" ]
}
```

- 打印实现细节见第 5 节。

#### 3.3.7 标签渲染数据（预览）

```
GET /api/coils/<coil_id>/label
```

响应：

```json
{
  "success": true,
  "data": {
    "coil_id": "260814001",
    "part_number": "A123456",
    "length": 250.50,
    "unit": "M",
    "barcode_coil": "260814001",
    "barcode_part": "A123456"
  }
}
```

- 前端用 JsBarcode（Code128）渲染预览。

#### 3.3.8 出库消耗登记

```
POST /api/requests/<request_id>/consumption
```

请求体（含全部新增宽表字段，按弹窗分组组织；以下为完整示例）：
- 基础/线材组：`job_part_number`、`wire_spec`、`color`、`shear_qty`、`shear_length`、`length_tolerance`、`shear_equipment`、`actual_shear_equipment`、`checker_first`、`checker_last`
- A端去皮组：`strip_len_a`、`strip_tol_a`、`strip_equip_a`、`strip_actual_equip_a`、`checker_first_a`、`checker_last_a`
- B端去皮组：`strip_len_b`、`strip_tol_b`、`strip_equip_b`、`strip_actual_equip_b`、`checker_first_b`、`checker_last_b`
- 打端组：`crimp_machine_a`、`crimp_machine_b`、`manual_crimp_flow`
- A端端子预加工组：`prep_time_a`、`terminal_part_a`、`equip_no_a`、`die_no_a`、`height_mm_a`、`height_tol_mm_a`、`pull_force_a`、`loose_chain_a`、`terminal_qty_a`、`preinstall_remark_a`
- B端端子预加工组：`prep_time_b`、`terminal_part_b`、`equip_no_b`、`die_no_b`、`height_mm_b`、`height_tol_mm_b`、`pull_force_b`、`loose_chain_b`、`terminal_qty_b`、`preinstall_remark_b`

```json
{
  "items": [
    {
      "coil_id": "260814001",
      "job_order": "J000002124-0004",
      "out_length": 250.50,
      "remark": "整卷出库",
      "job_part_number": "J000002124-0004",
      "wire_spec": "0.5mm²/22AWG/UL1007",
      "color": "红",
      "shear_qty": 100,
      "shear_length": 250.50,
      "length_tolerance": 1.00,
      "shear_equipment": "SL-01",
      "actual_shear_equipment": "SL-01",
      "checker_first": "王五",
      "checker_last": "李四",
      "strip_len_a": 5.00,
      "strip_tol_a": 0.50,
      "strip_equip_a": "PW-01",
      "strip_actual_equip_a": "PW-01",
      "checker_first_a": "王五",
      "checker_last_a": "李四",
      "strip_len_b": 5.00,
      "strip_tol_b": 0.50,
      "strip_equip_b": "PW-01",
      "strip_actual_equip_b": "PW-01",
      "checker_first_b": "王五",
      "checker_last_b": "李四",
      "crimp_machine_a": "Yes",
      "crimp_machine_b": "No",
      "manual_crimp_flow": "备料手工打端区流程：按 SOP-XX 手工打端并全检",
      "prep_time_a": 12.50,
      "terminal_part_a": "TERM-A-001",
      "equip_no_a": "EQ-01",
      "die_no_a": "DIE-01",
      "height_mm_a": 8.00,
      "height_tol_mm_a": 0.10,
      "pull_force_a": 25.00,
      "loose_chain_a": "散",
      "terminal_qty_a": 100,
      "preinstall_remark_a": "是，提前预装",
      "prep_time_b": 12.50,
      "terminal_part_b": "TERM-B-002",
      "equip_no_b": "EQ-02",
      "die_no_b": "DIE-02",
      "height_mm_b": 8.00,
      "height_tol_mm_b": 0.10,
      "pull_force_b": 25.00,
      "loose_chain_b": "链",
      "terminal_qty_b": 100,
      "preinstall_remark_b": ""
    }
  ]
}
```

响应（成功）：

```json
{
  "success": true,
  "message": "已登记 2 条出库记录",
  "inserted": 2
}
```

服务端逻辑：
1. 申请单存在且状态为 `prepping`（或 `ready_pickup`，二者取其一，建议 `prepping` 内完成），角色/站点校验同前；
2. 每个 `coil_id` 必须属于该申请单已录入的卷标（通过 `kr_wire_coil.request_id` 关联判断），且该卷状态为 `in_stock`；
3. `out_length > 0`，且换算后累计出库长度 ≤ 该卷卷标长度（比较前统一换算为 mm：卷长(mm) = `coil_length × 换算系数`，系数见 2.3.1 换算系数表；允许一卷多次部分出库，累计不可超总长）；
4. `job_order` 可选（minpack 无工单）；若填写则校验工单格式（`parse_job`）不强制存在；
5. 单位换算（服务端计算并覆盖，不信任前端）：按 `unit`（CSI 单位）查换算系数，`converted_length = round(out_length / factor, 2)`、`converted_unit = unit`；CSI 单位为空或未收录时两者置 NULL 并给出警告（不阻断）；
6. 写 `kr_wire_coil_consumption`（`consume_type='issue'`，冗余 part_number/unit，并按分组写入全部宽表字段，未传字段存 NULL）；
7. 若该卷累计出库长度（mm）≥ 卷标长度（mm），则更新 `kr_wire_coil.status = 'issued'`；
8. 写入 `kr_operation_log`（action=`OUTBOUND_REGISTER`）。

#### 3.3.9 申请单消耗记录查询

```
GET /api/requests/<request_id>/consumption
```

响应（其余宽表字段省略展示，按需返回）：

```json
{
  "success": true,
  "data": [
    { "id": 1, "coil_id": "260814001", "job_order": "J000002124-0004", "part_number": "A123456", "out_length": 250.50, "unit": "M", "converted_length": 0.2505, "converted_unit": "M", "wire_spec": "0.5mm²/22AWG/UL1007", "color": "红", "crimp_machine_a": "Yes", "loose_chain_a": "散", "consume_type": "issue", "operator": "warehouse1", "created_at": "2026-08-14 10:00:00" }
  ]
}
```

---

## 4. 页面交互设计

### 4.1 涉及页面

- `app/templates/request_detail.html`（备料详情页）——卷标信息按钮、卷标录入弹窗、出库登记弹窗、标签预览；
- `app/static/js/main.js`——`renderActions()` 中新增按钮渲染逻辑；
- 新增 JS 片段（建议独立 `app/static/js/coil.js`，在 request_detail.html 中按需引入）。

### 4.2 按钮位置（备料详情页操作区）

`renderActions(req)`（main.js:424）中，`prepping` 状态下当前已有"完成备料、缺料登记"按钮。新增规则：

```js
// 仅最小包装申请单在备料中显示
if ((role === 'warehouse' || role === 'admin') && status === 'prepping' && req.request_type === 'minpack') {
    actions.push({label: '卷标信息', class: 'btn-outline-info', action: 'coil_info'});
    actions.push({label: '出库登记', class: 'btn-outline-primary', action: 'outbound_register'});
}
```

按钮布局示意：

```
[操作区]
[指定备料员][开始备料]                       （pending_prep 时）
[卷标信息] [出库登记] [完成备料] [缺料登记]   （prepping 且 minpack 时）
```

> 注意：`/api/requests/<id>` 返回的 `request` 对象需确认含 `request_type` 字段（现已在 `SELECT *` 范围内，前端可直接读取）。

### 4.3 卷标信息录入弹窗（coilModal）

弹窗结构（Bootstrap modal）：

```
┌────────────────────────────────────────────────────────┐
│  卷标信息录入  #100 (minpack)            [必填项规则提示] │
├────────────────────────────────────────────────────────┤
│  物料明细（只读汇总，来自申请单）                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │ Part Number    | 单位(CSI只读) | 卷标ID      | 长度│  │
│  ├──────────────────────────────────────────────────┤  │
│  │ A123456 (必填) | M            | 260814001  | 250.5│  │
│  │ A123456 (必填) | M            | 260814002  | 100  │  │
│  │ W0303408 (选填)| M            | 260814003  | 80   │  │
│  │ [添加一行] [自动生成卷号]                          │  │
│  └──────────────────────────────────────────────────┘  │
│  已录入卷标（本单）                                    │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 卷标ID | 物料 | 长度 | 单位 | 状态 | 打印          │  │
│  └──────────────────────────────────────────────────┘  │
│  标签预览（3:1 缩放预览，见 4.5）                        │
├────────────────────────────────────────────────────────┤
│  [取消] [仅保存] [保存并打印]                           │
└────────────────────────────────────────────────────────┘
```

交互要点：

1. 打开弹窗：调用 `GET /api/requests/<id>/coil-units` 拉取本单全部物料的单位并缓存到前端 Map；
2. 行字段：
   - **Part Number**：下拉选择（取本单明细物料，去重），选择后自动回填"单位"（只读 input，灰色背景 + `readonly`）；
   - **卷标ID**：默认空，点击行内"生成"图标（或顶部"自动生成卷号"）调 `GET /api/coils/next-id` 填充；允许手工修改后失焦校验格式；
   - **长度**：数字输入，> 0；
3. 必填标识：选中 A/B 开头的物料后，该行标注红色"必填"；提交时 A/B 物料若没有该行则前端拦截并提示"物料 A123456 为 A/B 开头，必须录入卷标信息"；
4. 提交：调 `POST /api/requests/<id>/coils`；成功 toast 提示并刷新"已录入卷标"列表；
5. "保存并打印"：先保存，成功后调用 `POST /api/coils/print` 打印本次新增卷标（自动跳过打印失败的卷，错误 toast 提示）；
6. 已录入卷标列表：只读展示（本期不做删除；下期可加"删除未出库卷标"能力）。

### 4.4 出库登记弹窗（outboundModal，分组展示）

弹窗按 **6 组折叠面板 / Tab** 组织：线材基础组、A端去皮组、B端去皮组、打端组、A端端子预加工组、B端端子预加工组。

```
┌────────────────────────────────────────────────────────┐
│  出库消耗登记  #100                                    │
├────────────────────────────────────────────────────────┤
│  ① 线材基础组                                          │
│  卷标ID | 物料 | 原长度 | 出库长度(mm) | 工单号          │
│  260814001 | A123456 | 250.5 | [250.5]  | J00...       │
│  换算预览(只读): CSI单位 M | 转换后长度 0.2505           │
│  工单物料号   [J00...]   线材规格 [0.5mm²/22AWG/UL1007] │
│  颜色[红]  剪切数量[100]  剪切长度[250.5]  长度公差[1.0] │
│  剪切设备[SL-01] 实际设备[SL-01]                        │
│  首件确认[王五]  末件确认[李四]                          │
│  ─────────────────────────────────────────────────────│
│  ② A端去皮组                                           │
│  去皮尺寸[5.0] 公差[0.5] 设备[PW-01] 实际设备[PW-01]    │
│  首件确认[王五] 末件确认[李四]                          │
│  ③ B端去皮组（同A端布局）                              │
│  ─────────────────────────────────────────────────────│
│  ④ 打端组                                              │
│  A端一体机 [Yes▼/No]  B端一体机 [Yes▼/No]              │
│  手工打端作业流程 [多行文本框：备料手工打端区流程说明]    │
│  ─────────────────────────────────────────────────────│
│  ⑤ A端端子预加工组                                     │
│  工时(s)[12.5] 端子料号[TERM-A-001] 设备编号[EQ-01]     │
│  刀模编号[DIE-01] 高度(mm)[8.0] 公差(mm)[0.1] 拉力[25]  │
│  散/链[散▼] 端子用量[100] 提前预装备注[是，提前预装]     │
│  ⑥ B端端子预加工组（同A端布局）                        │
├────────────────────────────────────────────────────────┤
│  [取消] [确认出库]                                     │
└────────────────────────────────────────────────────────┘
```

交互要点：

1. 数据来源：`GET /api/requests/<id>/coils` 过滤 `status == 'in_stock'`；
2. 出库长度默认填该卷总长（整卷出库），可改小（部分出库）；**单位固定 mm**，旁边只读展示 CSI 单位与转换后长度（`converted_length`，由前端按换算系数表实时预览，最终以后端计算为准）；
3. ②~⑥ 组默认折叠，展开后填写；**全部为选填**，未填写项提交为空（后端按 NULL 处理）；
4. `loose_chain_a/b` 为下拉（散/链）、`crimp_machine_a/b` 为下拉（Yes/No），其余为文本/数字输入框；
5. 提交：`POST /api/requests/<id>/consumption`。

### 4.5 标签预览

- 预览在录入弹窗底部，按 3 inch × 1 inch 比例缩放（如 300px × 100px）；
- 使用前端库 **JsBarcode**（Code128）生成卷号条码与物料条码，其他文本用 CSS 排版；
- 预览版式与最终打印版式保持一致（见 5.4 版式定义）。

---

## 5. 打印模块技术方案

### 5.1 现状与约束

- 现有后端部署：CentOS + gunicorn（`deploy/README.md`）及 Dockerfile（python:3.11-slim）；
- 业务要求：**通过 Windows 打印驱动**，调用系统已安装的标签打印机驱动打印 3"×1" 标签；
- 本机开发环境（Windows，Python 3.13.14）已具备：`pywin32 312`、`Pillow 12.2.0`、`reportlab 4.5.1`（`pip list` 实测确认）；
- 结论：打印能力必须以 **Windows 宿主机**为承载。若生产后端仍部署在 Linux，需要引入"Windows 打印网关"（见 5.6 方案 C）。

### 5.2 依赖新增（requirements.txt）

```txt
pywin32>=306; platform_system == "Windows"   # 仅 Windows 需要
python-barcode>=0.15                          # Code128 条码生成（前后端通用数据）
Pillow>=10.0.0                                # 条码位图渲染（打印/预览）
```

> 若采用前端 JsBarcode 生成条码位图 + 后端仅透传位图，`python-barcode` 可省略；建议后端也具备生成能力以支持无前端场景（如批量补打）。

### 5.3 方案对比与选型

| 方案 | 技术路线 | 优点 | 缺点 | 适用场景 |
|------|----------|------|------|----------|
| **A. win32print + GDI 绘制** | pywin32 `win32ui.CreateDC` + 打印机 DC，直接绘制文本/条码位图 | 通用：任何已装驱动的标签打印机（Zebra/TSC/Honeywell/兄弟）均可；所见即所得；中文/字体无兼容问题 | 后端必须运行在 Windows；GDI 坐标需按 DPI 换算；多 worker 并发需串行化 | 后端迁移/部署在 Windows 上 |
| **B. win32print + RAW 指令直发** | `StartDocPrinter(type='RAW')` + `WritePrinter` 发送 ZPL/TSPL/EZPL | 条码由打印机固件生成，质量最佳、速度最快；可顺带支持 socket:9100 直连打印机 IP（绕过驱动） | 需按打印机品牌维护指令模板；换品牌需改模板；中文需转图形/内码 | 确认打印机品牌型号支持 ZPL/TSPL（Zebra、TSC、Godex 等） |
| **C. Windows 打印网关代理** | Linux 后端不变；另起一个部署在 Windows 的小型 HTTP 服务（Flask/FastAPI 或 Windows 服务），内网调用其打印接口 | 主系统继续跑 Linux；打印能力独立、可复用；打印失败不影响主流程 | 多维护一个服务；需内网鉴权（token）；增加一跳网络调用 | **生产后端保持 CentOS/Docker 部署时的推荐过渡方案** |
| D. 共享打印机 + smbclient | Linux 上用 `smbclient` 把 ZPL/文件发到 Windows 共享打印机 | 无需新增服务 | 仅适合指令型打印机；依赖 SMB 共享与账号；调试困难 | 不推荐 |

### 5.4 推荐方案（按优先级）

**首选：方案 B（RAW 指令直发，走 Windows 驱动 pass-through）——前提是确认标签打印机品牌型号。**

```python
# app/services/label_print_service.py（示意，ZPL 版）
import win32print

def print_zpl(printer_name: str, zpl: bytes) -> None:
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, ("WireCoilLabel", None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, zpl)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)
```

ZPL 示例（Zebra 203dpi，3" 宽 ≈ 609 dot，1" 高 ≈ 203 dot）：

```zpl
^XA
^PW609^LL203^LH0,0^FO20,15^A0N,28,28^FD260814001^FS        ; 卷号文本
^FO20,45^BCN,60,Y,N,N^FD260814001^FS                       ; 卷号 Code128 条码
^FO330,15^A0N,28,28^FDA123456^FS                           ; 物料文本
^FO330,45^BCN,60,Y,N,N^FDA123456^FS                        ; 物料 Code128 条码
^FO20,120^A0N,24,24^FD250.50 M^FS                          ; 长度/单位
^XZ
```

> TSC（TSPL）/ Godex（EZPL）同理，模板集中在 `LABEL_TEMPLATES = {'zpl': ..., 'tspl': ...}`，按打印机型号映射。

**次选：方案 A（GDI 驱动打印）——无法确认打印机指令集时使用，通用性最强。**

```python
# app/services/label_print_service.py（示意，GDI 版）
import win32ui, win32con
from PIL import Image, ImageWin

def print_gdi(printer_name: str, elements) -> None:
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer_name)
    # 按标签物理尺寸设置页面（75mm×25mm，用 GetDeviceCaps 换算像素）
    dc.StartDoc("WireCoilLabel")
    dc.StartPage()
    for el in elements:            # 文本用 dc.TextOut；条码位图用 dc.StretchBlt(ImageWin.DIB)
        ...
    dc.EndPage()
    dc.EndDoc()
    dc.DeleteDC()
```

要点：
- 尺寸换算：`mm_to_px = mm * dpi / 25.4`；`dpi = dc.GetDeviceCaps(88/90)`；
- 条码位图：用 `python-barcode` + `Pillow` 生成 Code128 PNG，`ImageWin.DIB` 绘制进 DC；
- 打印机纸型：Windows 打印驱动中把标签纸类型/尺寸配好（如 Zebra "3 x 1 in"），或代码中通过 DEVMODE（`dmPaperWidth/dmPaperLength`，单位 0.1mm）设置自定义纸型；
- 多 worker 并发：打印函数用 `threading.Lock` 串行化，避免 GDI/RAW 并发错乱。

### 5.5 服务抽象（推荐封装）

新建 `app/services/label_print_service.py`，对外暴露统一接口，内部屏蔽通道差异：

```
LabelRenderer.render(coil) -> LabelData      # 纯函数：卷号/物料/长度/单位 + Code128 条码内容
LabelPrinter.print_labels(coil_ids) -> result # 组合：查库存 → 渲染 → 选通道打印
PrintChannel  = GdiChannel | RawZplChannel | RawTsplChannel   # 通道可配置
Config.LABEL_PRINTER_NAME / LABEL_PRINT_CHANNEL / LABEL_PRINTER_MODEL
```

配置项（config.py 新增）：

```python
LABEL_PRINTER_NAME = os.environ.get('LABEL_PRINTER_NAME', '')      # 空则取系统默认打印机
LABEL_PRINT_CHANNEL = os.environ.get('LABEL_PRINT_CHANNEL', 'gdi') # gdi | raw_zpl | raw_tspl
LABEL_PRINT_GATEWAY_URL = os.environ.get('LABEL_PRINT_GATEWAY_URL', '')  # 方案C时填写
LABEL_PRINT_GATEWAY_TOKEN = os.environ.get('LABEL_PRINT_GATEWAY_TOKEN', '')
```

### 5.6 部署方案建议

1. **生产迁移到 Windows**（若可行）：直接部署 Flask 到 Windows 服务器（或 Windows Docker 容器挂载打印机），方案 A/B 直连驱动，最简洁；
2. **保持 Linux 后端 + Windows 打印网关（方案 C，过渡推荐）**：
   - 在装有标签打印机驱动的 Windows 电脑上部署小型打印代理（可用 pywin32，接口 `/print` 接收 `{printer, labels:[...]}`，用固定 token 鉴权）；
   - 主系统 `label_print_service` 检测到 `LABEL_PRINT_GATEWAY_URL` 非空即走 HTTP 转发；
   - 打印失败仅影响该请求，返回部分失败信息，不影响备料主流程。

### 5.7 标签版式定义（3"×1" = 75mm × 25mm）

```
┌────────────────────────────────────────────┐
│ 卷号: 260814001           物料: A123456    │
│ ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆          ▆▆▆▆▆▆▆▆▆▆▆    │
│ ▆(Code128)▆▆▆▆▆▆▆▆        ▆(Code128)▆▆▆▆  │
│ 长度: 250.50 M                             │
└────────────────────────────────────────────┘
```

- 内容：卷号 + 卷号 Code128 条码 + 物料号 + 物料 Code128 条码 + 长度 + 单位；
- 尺寸：75mm × 25mm；条码高度约 12mm（60 dot @203dpi），保证可扫；
- 字体：等宽/黑体（GDI 方案用系统字体；ZPL 用 `^A0N`）。

---

## 6. CSI 单位获取

### 6.1 现状模式分析

`app/services/csi_service.py` 中现有查询模式（以 `get_item_cost` 为例）：

```python
def get_item_cost(self, item: str) -> float | None:
    filter_str = f"Item = N'{item}'"
    props = ["Item", "DerUnitCost"]
    records = self._get_ido("ue_GDL_SLItems", properties=props, filter_str=filter_str)
    ...
```

- 模式：`_get_ido(ido_name, properties, filter_str)`，OAuth2 认证 + token 缓存 + 401 自动刷新；
- 物料主数据 IDO 为 `ue_GDL_SLItems`（单价 `DerUnitCost`、Backflush 均来自此表）。

### 6.2 新增方法 get_item_unit

```python
def get_item_unit(self, item: str) -> str | None:
    """
    获取物料单位 - 查询 IDO ue_GDL_SLItems 的 UM 字段
    返回单位字符串（如 'M'/'FT'/'EA'），查不到返回 None
    """
    try:
        filter_str = f"Item = N'{item}'"
        props = ["Item", "UM"]   # M3 标准单位字段；若 IDO 未暴露，见 6.3 备选
        records = self._get_ido("ue_GDL_SLItems", properties=props, filter_str=filter_str)
        if records:
            um = records[0].get("UM") or records[0].get("Uom") or records[0].get("UnitOfMeasure")
            return str(um).strip() if um else None
        return None
    except Exception as e:
        logger.error(f"[CSI] get_item_unit error: {e}")
        return None
```

### 6.3 字段名确认与备选方案

- **字段名风险**：`ue_GDL_SLItems` 是否暴露单位字段需在**生产环境实测确认**（M3 标准字段为 `UM`，部分 IDO 会命名为 `Uom`/`UnitOfMeasure`）。建议联调时先用手工 curl 验证，再定 props；
- **备选 1（标准 CSI API）**：沿用 `_get_csi` 模式查询 MMS005（项目注释中已有的标准表查询通道）：

```python
rows = self._get_csi("MMS005", {"CONO": Config.SITE_CSI_COMPANY[siteref].split('_')[-1], "ITNO": item})
unit = rows[0].get("UM") if rows else None
```

- **备选 2（本地兜底）**：CSI 不通时允许 unit 为空，前端显示 "-"，录入不阻断（业务可接受），并在页面给出警告条。

### 6.4 批量与缓存

- 弹窗初始化一次取全部明细物料单位：`GET /api/requests/<id>/coil-units`，服务端逐物料调 `get_item_unit` 并做**进程内 dict 缓存**（`{part_number: unit}`，TTL 建议 30 分钟），避免用户反复开关弹窗造成 CSI 高频调用。

---

## 7. 校验逻辑

### 7.1 校验规则总表

| 编号 | 规则 | 校验位置 | 实现 |
|------|------|----------|------|
| R1 | 卷标ID格式：`^\d{9}$`（YYMMDD+3位） | 前端失焦 + 后端 | 正则校验 |
| R2 | 卷标ID全局唯一 | 后端（硬性） | `kr_wire_coil.uk_coil_id` 唯一索引 + Duplicate entry 捕获回滚，提示"卷号已存在" |
| R3 | 每日最多 999 卷 | 后端（硬性） | `COUNT(*) WHERE coil_id LIKE 'YYMMDD%' >= 999` 则拒绝 |
| R4 | A/B 开头物料必须录入卷标 | 前端（提示）+ 后端（兜底） | `part_number[0].upper() in ('A','B')` 时，该物料在 items 中必须至少出现一次 |
| R5 | 长度 > 0 | 前端 + 后端 | `length > 0`，数值类型 |
| R6 | 物料必须属于该申请单明细 | 后端 | `part_number IN (SELECT part_number FROM kr_request_item WHERE request_id=?)` |
| R7 | 单位只读，由 CSI 回填 | 后端 | 后端重新调 `get_item_unit` 覆盖前端传值 |
| R8 | 出库长度 ≤ 卷标长度（统一换算 mm 比较） | 后端 | 卷长(mm) = `coil_length × 换算系数`；单次及累计 `SUM(out_length)`（mm）均不可超卷长(mm) |
| R9 | 出库登记卷标必须属于本单且 in_stock | 后端 | 通过 `kr_wire_coil.request_id` 关联判断所属申请单 |
| R10 | 仅 minpack + prepping 可录入/出库 | 后端 | `request_type='minpack' AND status='prepping'` |
| R11 | 单位换算由服务端计算 | 后端 | 按 `unit` 查 2.3.1 系数表计算 `converted_length`；CSI 单位为空/未收录时置 NULL 并警告，不阻断 |
| R12 | 宽表字段取值校验 | 前端 + 后端 | `crimp_machine_a/b` ∈ ('Yes','No')、`loose_chain_a/b` ∈ ('散','链')；数字类字段为数值类型；其余为文本 |

### 7.2 关键规则说明

**R4（A/B 强制）**：物料号首字母（大小写不敏感）为 A 或 B 的物料，提交时该物料的卷标行数 ≥ 1；否则返回 400：
```json
{ "success": false, "message": "物料 A123456 以 A/B 开头，必须录入卷标信息" }
```
前端在弹窗中同步用红色"必填"角标提示，减少后端报错。

**R2/R3 与并发**：生成卷号接口先按 `MAX(SUBSTRING(coil_id,7,3))` 或 COUNT 计算下一流水；正式保存依赖唯一索引兜底——插入冲突时捕获 `pymysql.err.IntegrityError`，自动重取新卷号重试（最多 3 次），仍失败则整批回滚并提示。

**R8（累计出库不超总长，统一 mm 比较）**：
```sql
SELECT COALESCE(SUM(out_length),0) FROM kr_wire_coil_consumption
WHERE coil_id = %s AND consume_type = 'issue';
```
`out_length` 原始单位为 mm，卷标长度 `coil_length` 为 CSI 单位（如 M/FT），比较前统一换算为 mm：`卷长(mm) = coil_length × 换算系数`（系数见 2.3.1）；本次出库长度 + 累计 ≤ 卷长(mm)，达标后该卷状态置 `issued`。

---

## 8. 验收标准

### 8.1 功能验收

| 编号 | 验收项 | 验收标准 |
|------|--------|----------|
| A1 | 按钮可见性 | 登录 warehouse/admin，打开 minpack 申请单（prepping 状态），操作区显示"卷标信息""出库登记"按钮；normal 申请单或非 prepping 状态不显示 |
| A2 | 卷标多行录入 | 弹窗支持添加多行，每行可保存；A/B 开头物料显示必填标识 |
| A3 | 单位自动获取 | 打开弹窗，物料行"单位"自动显示 CSI 返回的单位且不可编辑；CSI 不可用时显示"-"并给出警告 |
| A4 | 卷号生成 | 点击生成返回格式 `YYMMDD+3位`；手工输入 `123` 被格式校验拦截 |
| A5 | 强制校验 | 仅录入非 A/B 物料卷标，提交被拒并提示 A/B 物料必须录入 |
| A6 | 唯一性 | 重复录入同一卷号返回"卷号已存在"，数据不写入 |
| A7 | 999 上限 | 用 SQL 预造 999 条当日记录后，生成卷号接口返回 400"当日卷号已用完" |
| A8 | 库存表维护 | 录入成功后 `kr_wire_coil` 出现对应行，状态 `in_stock`，含来源申请单ID、操作人 |
| A9 | 消耗表建表 | `kr_wire_coil_consumption` 表结构存在（本期验收 DDL 与字段注释） |
| A10 | 出库登记 | 出库登记后 `kr_wire_coil_consumption` 写入 `issue` 记录；整卷出库后该卷状态变为 `issued` |
| A11 | 出库校验 | 出库长度大于卷长被拒；非本单卷被拒 |
| A12 | 标签打印 | 选择已录入卷标调用打印，Windows 打印机驱动打印出 3"×1" 标签，含卷号/卷号条码/物料/物料条码/长度/单位，条码可扫 |
| A13 | 站点隔离 | 410 站点 warehouse 无法查看/操作 310 站点的卷标数据 |
| A14 | 单位转换正确性 | CSI 单位为 M 时录入 250.50 mm → 保存后 `converted_length=0.2505`、`converted_unit='M'`；FT 单位录入 250.50 mm → `250.50÷304.8=0.8219`；系数表外单位不报错，转换字段为 NULL 且页面有警告 |
| A15 | 宽表字段录入保存 | 出库登记填写全部新增字段（线材基础/去皮A/B/打端/端子预加工A/B），保存后逐字段与数据库一致；未填写的宽表字段保存为 NULL 且不报错 |

### 8.2 非功能验收

| 编号 | 验收项 | 标准 |
|------|--------|------|
| B1 | 权限 | 非 warehouse/admin 调用卷标/出库/打印接口返回 403 |
| B2 | 操作日志 | 录入、出库、打印均有 `kr_operation_log` 记录（action 带 COIL_/OUTBOUND_ 前缀） |
| B3 | 并发 | 连续快速提交 10 个卷号不重复、不报错 |
| B4 | 打印容错 | 打印机离线时返回部分失败信息，主流程（备料状态流转）不受影响 |
| B5 | 兼容性 | 现有 normal 流程、minpack 流程原有功能回归无回归（不显示新按钮即可） |

### 8.3 回归范围

- 申请提交（normal/minpack）、开始备料、完成备料、缺料登记、签字取料全流程回归；
- 批次号 FIFO 填充、库存查询、单位查询等现有功能无回归。

---

## 9. 开发任务拆解建议（供排期参考）

| # | 任务 | 涉及文件 |
|---|------|----------|
| 1 | 数据库迁移脚本（新增 2 表） | `scripts/migrate_wire_coil.py`（新） |
| 2 | CSI 单位查询方法 | `app/services/csi_service.py` |
| 3 | 线卷 Blueprint（9 个接口） | `app/routes/coil.py`（新），注册进 `app/__init__.py` |
| 4 | 打印服务（渲染 + GDI/RAW/网关通道） | `app/services/label_print_service.py`（新） |
| 5 | 前端弹窗与按钮 | `app/templates/request_detail.html`、`app/static/js/coil.js`（新）、`app/static/js/main.js` |
| 6 | 配置项 | `app/config.py`（LABEL_* 系列） |
| 7 | 测试与验收 | 测试用例 + 联调（CSI 字段、打印机实机） |

---

## 附录 A：关键设计决策摘要

1. **独立表 + 消耗宽表**：`kr_wire_coil`（每卷一行）+ `kr_wire_coil_consumption`（每卷每次出库/报废一行），均独立于申请单主/明细表；消耗表采用**宽表设计**，一次性冗余线材基础、去皮、打端、端子预加工等加工过程参数，出库登记留存全量数据，避免后续多表关联；已移除 `request_id`，申请单追溯通过 `coil_id → kr_wire_coil.request_id` 上查；不设物理外键，沿用现有逻辑外键风格。
2. **录入入口收窄**：仅 minpack + prepping + 仓库/管理员，避免正常领料流程受扰。
3. **单位只读 + 换算服务端化**：CSI 为主数据源，后端回填防篡改；CSI 故障降级为可空。出库登记的 `out_length` 以 mm 为原始录入单位，换算为 CSI 单位的系数表与计算逻辑统一由服务端维护，前端仅只读展示 `converted_length`/`converted_unit`。
4. **卷号方案**：YYMMDD+3位 自编码 + 唯一索引兜底 + 每日 COUNT 上限，无需额外序列表，简单可靠。
5. **打印通道**：服务抽象隔离"渲染"与"通道"，Windows 驱动优先（GDI 通用 / RAW 高效），生产仍跑 Linux 时走 Windows 打印网关代理（方案 C）。
6. **本期边界**：只登记出库记录；报废登记、库存看板/报表、卷标删除、标签重印管理留待后续迭代。

## 附录 B：风险与待确认项

| 风险/待确认 | 影响 | 应对 |
|-------------|------|------|
| `ue_GDL_SLItems` 单位字段名（UM vs Uom）未实测 | 单位回填失败 | 联调先手工 curl 验证，另备 MMS005 标准 API 兜底 |
| 生产后端为 Linux，Windows 打印驱动不可直连 | 打印方案不可落地 | 采用方案 C（Windows 打印网关），config 增加 gateway 配置 |
| 标签打印机品牌型号未确认 | 无法确定 ZPL/TSPL | 默认 GDI 通用方案；确认型号后加对应 RAW 模板 |
| 现有 `init_db.py` DDL 与运行时代码不一致（缺 request_type 等字段） | 新表迁移脚本误用重建脚本 | 新表一律 `CREATE TABLE IF NOT EXISTS` 独立迁移，不并入重建流程 |
