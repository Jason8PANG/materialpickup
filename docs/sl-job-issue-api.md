# sl-job-issue（工单发料）接口对接文档

> 面向对象：需要把「物料从仓库发到工单」写进 ERP 的开发者，以及学习后要**独立完成集成**的 AI Agent。
> 事实来源：本仓库 `app/services/csi_interface.py`、`app/csi_sync_worker.py`、`app/config.py`、`app/routes/csi_interface.py`、`app/routes/external.py`。
> 本文所有字段名、URL、错误文案均可在代码或实测响应中找到出处；代码里没有依据的写「待确认」，不编造。

---

## 1. 概览

### 1.1 这个接口做什么

`sl-job-issue` 是宝鼎 Syteline（Infor CSI）网关上的**工单发料**接口：把指定物料、指定批次、指定数量，从指定仓库库位**扣减**并记入某张工单的物料需求（`SLJobMatls` 的已发数量 `QtyIssued`）。

一次调用 = 一条 ERP 发料明细。ERP 侧结果：库存减少、工单物料已发数增加。

### 1.2 和 `sl-qty-move` 的区别

| 维度 | `sl-job-issue`（工单发料） | `sl-qty-move`（库位转移） |
|---|---|---|
| 业务语义 | 物料**发给工单**，扣工单物料需求 | 只在库位之间**挪库存**，不动工单 |
| 是否要工单号 | 必须（`job` + `suffix`） | 不需要 |
| 库位字段 | 只有 `location`（发料库位） | `fromLocation` → `toLocation` |
| 原因码/科目 | 无 `reasonCode` / `account` | 有 `reasonCode`（默认 `C1N`） |
| ERP 副作用 | 库存 −、工单已发数 + | 库存从 A 库位搬到 B 库位，总量不变 |
| 看板触发条件 | 申请单明细 `job_order` **非空** | 明细 `job_order` **为空** |

出处：`app/services/csi_interface.py:11-12`、`app/csi_sync_worker.py:7-11`、`app/csi_sync_worker.py:283-319`。

> 同一张申请单允许**混合**：部分明细带工单走 job-issue，部分不带工单走 qty-move，两者在同一事务内一起入队（`app/services/csi_interface.py:13`）。

### 1.3 典型使用场景

| 场景 | 谁发起 | 说明 |
|---|---|---|
| 车间按工单领料，仓库完成备料 | 看板「完成备料」 | 明细带工单号 → 自动入队 `sl-job-issue` |
| 卷标耗用登记（裁线/上机消耗） | 外部系统 `POST /api/external/consumption` | 消耗记录带 `job_order` 时，同事务入队一条 job-issue（`app/routes/external.py:1065-1109`） |
| 报废登记（需冲减工单物料） | 外部系统 `POST /api/external/consumption/scrap` | 同上（`app/routes/external.py:405-407`） |
| 外部系统直接提交工单发料 | 外部系统 `POST /api/external/job-issue` | 直接写接口表，由同步器提交（`app/routes/external.py:1112-1212`） |

---

## 2. 调用链路

```
业务方（看板 / 外部系统）
        │  ① 写接口表（同事务，PENDING / is_sync=0）
        ▼
  kr_csi_interface         ← 中间层：待同步队列 + 状态机 + 幂等记录
        │  ② 同步器每 CSI_SYNC_INTERVAL（默认 60s）扫描 is_sync=0
        ▼
app/csi_sync_worker.py
        │  ③ 条件 UPDATE 抢占 → 清 Backflush → POST → 轮询终态
        ▼
宝鼎 Syteline 网关  http://bdsytelineapi/api/cmd/sl-job-issue
```

- ① 写入位置：`enqueue_qty_move()`（`app/services/csi_interface.py:196-300`），由「完成备料」事务调用；外部入口见 `POST /api/external/job-issue`。
- ② 扫描条件：`api_name IN (4 个接口) AND is_sync=0 AND status IN ('PENDING','QUEUED','SUBMITTING') AND (last_poll_at IS NULL OR last_poll_at < NOW()-45s)`，按 id 升序，单轮上限 `CSI_SYNC_BATCH_SIZE=20`（`app/csi_sync_worker.py:138-164`）。
- ③ 提交地址 = `Config.CSI_SYTELINE_API_BASE` + `api_name`，默认 `http://bdsytelineapi/api/cmd/`（`app/config.py:79`、`app/csi_sync_worker.py:239-240`）。

### 2.1 为什么业务方不该直接打宝鼎

| 直接打宝鼎会丢掉什么 | 看板接口表替你做了什么 |
|---|---|
| Token 管理 | 统一用 `CSI_SYTELINE_TOKEN`，业务方不接触（`app/csi_sync_worker.py:243-247`） |
| 失败重试与分类 | `transient`（网络）自动同 traceId 重试 3 次；业务错误立即转人工（见第 5 章） |
| 失败告警 | 转人工时发邮件给 `CSI_ALERT_EMAIL`，附单号/traceId/错误原文（`app/csi_sync_worker.py:602-684`） |
| 并发重复过账 | `claim()` 条件 UPDATE 抢占，多进程不会重复提交（`app/csi_sync_worker.py:166-189`） |
| 工单物料倒冲（Backflush）冲突 | 提交前自动把 `Backflush` 清 0，清不掉就不提交（`app/csi_sync_worker.py:529-563`） |
| 可追溯/可重发 | 单号、traceId、错误原文、重发次数全部落库，界面可点「重新发送」 |

---

## 3. 请求

### 3.1 方法与地址

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| URL | `http://bdsytelineapi/api/cmd/sl-job-issue`（`CSI_SYTELINE_API_BASE` + `/sl-job-issue`） |
| Content-Type | `application/json` |
| 认证头 | `X-Syteline-Token: <CSI_SYTELINE_TOKEN>`（形如 `slt_xxx`） |
| 超时 | `CSI_SYNC_HTTP_TIMEOUT = 20` 秒（`app/config.py:94`） |

> `CSI_SYTELINE_API_BASE`（宝鼎网关）与 `CSI_API_BASE` / `CSI_IDO_BASE`（Infor CSI REST，用于工单/库存实时查询）是**两套不同的地址**，不要混用（`app/config.py:73-79`）。

### 3.2 Body 字段表

payload 由 `_build_payload()` 组装（`app/csi_sync_worker.py:283-298`），字段值全部来自接口表行。

| 字段 | 类型 | 必填 | 示例 | 来源列 | 说明与约束 |
|---|---|---|---|---|---|
| `traceId` | string | 是 | `JIENG0001317-0000-D041636-0f1e2d3c4b5a` | `traceid` | **幂等键**。生成规则 `JI{job}-{item}-{uuid12}`，总长 ≤128（`app/services/csi_interface.py:80-84`） |
| `site` | string | 是 | `"410"` | `siteRef` | 站点编码，`int` 转 `str`。只支持 `310`(苏州) / `410`(槟城)（`app/config.py:98-101`） |
| `documentNumber` | string | 是 | `MP301501` | `documentnumber` | 单据号，**上限 12 位**（`MAX_DOC_LEN`，列 `VARCHAR(12)`） |
| `job` | string | 是 | `ENG0001317` | `job`（拆分后主号） | 工单**主号**，不含 `-0000` 后缀 |
| `suffix` | string | 否 | `"0000"` | `job`（拆分后后缀） | 仅当工单号带「`-` + 1~4 位纯数字」时才出现该键 |
| `item` | string | 是 | `D041636` | `item` | CSI 物料号，`strip()` 后提交 |
| `quantity` | number | 是 | `4` / `6.5` | `quantity` | 必须 > 0；**最多 4 位小数**（列 `DECIMAL(10,4)`）；整数值提交为 int（不带 `.0`） |
| `lot` | string | 是 | `202609010000076` | `lot` | 批次号，`strip()` 后提交 |
| `warehouse` | string | 是 | `M1M1` | `warehouse` | 仓库，按站点映射（`CSI_WAREHOUSE_MAP`） |
| `location` | string | 是 | `RM-B009-4` | `to_location` | **发料库位 = 批次/卷标所在库位**，见 3.3 |

**job-issue 契约里没有**：`fromLocation`、`toLocation`、`reasonCode`、`account`、`unitcode1/2`。这些列在接口表里为 `NULL`，也不会出现在 payload（`app/services/csi_interface.py:262-264`）。

#### 逐字段来源与约束

**`traceId` —— 幂等键**

- 规则：`JI{job}-{item}-{uuid12}`，`uuid12` 为 12 位十六进制；`head` 超过 `128 - 13` 位时截断前半段，保留唯一后缀（`app/services/csi_interface.py:80-84`）。
- `job` 用的是**完整工单号**（含 `-0000`），例如 `JIENG0001317-0000-D041636-...`。
- 平台按 `traceId + site + documentNumber` 幂等（`app/services/csi_interface.py:20`）。同一 traceId 重复提交 → 平台返回同一条结果，不会重复过账。

> ⚠️ `traceId` 只在两种情况下变化：**新入队**（新 uuid）和**人工「重新发送」**（`_retry_trace_id()` 生成新 uuid）。自动重试**绝不换** traceId —— 换了就会重复过账。`documentNumber` 则从头到尾不变。

**`site` —— 站点编码**

- 取自 `kr_material_request.siteref`（正向入队时写入 `int(siteref)`），提交时转 `str`。
- 站点必须在 `Config.CSI_WAREHOUSE_MAP` 里，否则完成备料直接跳过、不写接口表（`app/services/csi_interface.py:210-213`）。

**`documentNumber` —— 单据号**

- 正向规则：`MP{request_id}{line_seq:02d}`，如 `MP301501`（`app/services/csi_interface.py:101-105`）。
- `line_seq` 是该申请单内**永久递增**的行序号（含 `REVERSED`/`CANCELLED`/`FAILED_MANUAL` 占用过的号），任状态都不复用；因为平台可能锁死失败单号（`app/services/csi_interface.py:87-98`、`236-238`）。
- 超 12 位时**抛错并回滚整个完成备料**，绝不静默跳过（否则会出现「实物已领用、ERP 未记账」）。

> ⚠️ 单号一旦被平台受理过（哪怕最终失败），**不要拿它改数量重投**。实测会返回 `HTTP 409 CONFLICT`（平台锁单）。正确做法是点「重新发送」，沿用原单号 + 新 traceId；或作废该行重新入队拿新单号。

**`job` + `suffix` —— 工单号拆分**

`_split_job()` 的实际行为（`app/csi_sync_worker.py:261-272`）：

| 输入 | 输出 `job` | 输出 `suffix` | 备注 |
|---|---|---|---|
| `J000029867-0001` | `J000029867` | `0001` | 常规 |
| `ENG0001317-0000` | `ENG0001317` | `0000` | `0000` 也会作为 suffix 提交 |
| `J000029867` | `J000029867` | `None` | 无 `-` → 不传 `suffix` 键 |
| `J000029867-00001` | 原样 `J000029867-00001` | `None` | 后缀 5 位，不符合 1~4 位 → 不拆，交给平台判定 |
| `J000029867-ABC` | 原样 | `None` | 后缀非纯数字 → 不拆 |

- 拆分规则：取**最后一个** `-`；其后必须是 1~4 位纯数字，且 `-` 前非空。
- `suffix` 在 payload 里是**字符串**（`"0000"`），不是数字。
- 接口表 `job` 列保留**完整工单号**（含后缀），便于追溯（`tests/test_csi_sync_worker.py:870-881`）。

**`quantity` —— 数量**

- `_quantity()`：`float()` 后整数转 `int`，否则保留 float（`app/csi_sync_worker.py:250-258`）。所以 `100.0000` 提交为 `100`，`6.5000` 提交为 `6.5`。
- 列宽 `DECIMAL(10,4)`（`scripts/migrate_csi_interface.py:62`）→ 小数最多 4 位。
- 提交前校验：`None` 或 `<= 0` 直接转人工，不发请求（`app/csi_sync_worker.py:733-737`）。

**`warehouse` —— 仓库**

- `CSI_WAREHOUSE_MAP = {'310': 'C1M1', '410': 'M1M1'}`，可被 `CSI_WAREHOUSE_310/410` 覆盖（`app/config.py:83-84`）。

**`location` —— 发料库位（最容易搞错的一个，且**按入口不同规则不同**）**

| 入口 | `location` 取值 | 代码位置 | 适用场景 |
|---|---|---|---|
| **外部系统 `POST /api/external/job-issue`** | **固定 `Floor-Core`**（`Config.CSI_TO_LOCATION`），**不允许传其他库位** | `app/routes/external.py:1120`、`:1198-1199` | 线边仓（Floor-Core）已有实物，按工单发料扣账 |
| 看板内部「完成备料」 | **批次/卷标当前所在库位**（如 `RM-B009-4`） | `app/services/csi_interface.py:258-263` | 从仓库库位直接发料到工单，跳过线边仓 |

> ⚠️ **外部调用者只允许 `Floor-Core`。** 传别的一律不符合契约——外部接口不接受库位入参，`to_location` 由服务端写死 `Config.CSI_TO_LOCATION`。
>
> ⚠️ 内部申请单链路相反：`location` 必须是批次/卷标所在库位（`kr_wire_coil.loc` / `kr_request_item_lot.loc`），写死 `Floor-Core` 会导致「批次存货 0」。
>
> payload 里的 `location` 取自接口表 `to_location` 列（`app/csi_sync_worker.py:294`）；job-issue 行入队时 `from_location` 恒为 `None`。看表时不要按列名字面理解。

历史事故：内部链路早期把 `location` 固定写成 `Floor-Core`，批次实际在 `RM-B009-4`，平台按 `Floor-Core` 扣库存时该批次可用量为 0，返回 `转移数量 X 大于批次存货 0`。内部已改为取批次库位（见第 10 章变更记录）；**外部入口保持 `Floor-Core` 是设计约束，不是遗漏**。

### 3.3 接口表 → payload 字段映射（排错用）

| payload | ← `kr_csi_interface` 列 | job-issue 行的实际内容 |
|---|---|---|
| `traceId` | `traceid` | `JI{job}-{item}-{uuid12}` |
| `site` | `siteRef` | `410` |
| `documentNumber` | `documentnumber` | `MP301501` |
| `job` | `job` | 完整工单号，提交前拆 `suffix` |
| `item` | `item` | `D041636` |
| `quantity` | `quantity` | `4.0000`（提交时 `4`） |
| `lot` | `lot` | `202609010000076` |
| `warehouse` | `warehouse` | `M1M1` |
| `location` | **`to_location`** | `RM-B009-4`（批次库位） |
| —（不提交） | `from_location` | `NULL` |
| —（不提交） | `reasoncode` / `account` | `NULL` |

---

## 4. 响应

### 4.1 平台返回结构

```json
{
  "success": true,
  "data": {
    "status": "QUEUED",
    "ion": { "Success": true, "Message": "ok" }
  }
}
```

- 判定**只读 `data`**，外层 `success` 不参与决策（`_classify()` 只取 `body['data']`，`app/csi_sync_worker.py:398-401`）。
- `data.status` 可能值：`QUEUED` / `PENDING`（异步未终态）、`SUCCESS`、`FAILED`、`UNCONFIRMED`。

### 4.2 成功判定条件（代码实际怎么判）

判定顺序（`app/csi_sync_worker.py:374-426`），**按字段判，绝不匹配 message 文本**：

| 步骤 | 条件 | 判定 |
|---|---|---|
| 1 | HTTP 409 | `failed`（单号被平台锁定） |
| 2 | HTTP 400 | `failed`（入队被拒，原因取外层 `message`/`error`） |
| 3 | HTTP 401 / 403 | `failed`（token 失效/无权限） |
| 4 | HTTP 其他非 200 | `transient`（按网络类处理） |
| 5 | 响应不是合法 JSON | `transient` |
| 6 | 缺 `data` 对象 | `transient` |
| 7 | `data.status == 'SUCCESS'` 且 `ion` 全部 `Success` 为真 | **`success`** |
| 8 | `data.status == 'SUCCESS'` 但 `ion` 有失败明细 | `failed`（原因 `ion 明细失败：...`） |
| 9 | `data.status == 'FAILED'` | `failed`（原因取 `data.error` → `data.message` → `无错误详情`） |
| 10 | `data.status == 'UNCONFIRMED'` | `failed`（平台未确认，需人工核对 CSI 是否已入账） |
| 11 | `data.status ∈ {QUEUED, PENDING}` | `pending`（同 traceId 继续轮询） |
| 12 | 未知 status | `transient` |

`ion` 兼容对象或数组（`_ion_failures()`，`app/csi_sync_worker.py:351-371`）；`Success` 的真值判定：`true` / `1` / `"true"/"1"/"y"/"yes"`（`_truthy()`）。

> 幂等回放：同一 traceId 重复提交，平台返回的是**同一条结果**。本地代码不区分「首次」与「回放」，只要 `data.status=SUCCESS` 且 `ion` 全通过就记成功。所以**重发/重试必须沿用原 traceId**，否则等于新建一笔发料。

### 4.3 成功示例

```json
{
  "success": true,
  "data": {
    "status": "SUCCESS",
    "ion": { "Success": true, "Message": "发料成功" }
  }
}
```

落库结果：`is_sync=1`、`status='SUCCESS'`、`errormessage=NULL`、`transient_count=0`（`app/csi_sync_worker.py:764-768`）。

### 4.4 失败示例

业务失败（`200` + `FAILED`，`success` 外层为 false，仍走 `data.status`）：

```json
{
  "success": false,
  "data": {
    "status": "FAILED",
    "error": "本次要发的数量比工单还能发的数量多 12"
  }
}
```

落库结果：`is_sync=2`、`status='FAILED_MANUAL'`、`errormessage` = 平台原文（截断到 255 字符）、`retry_count+1`，并发告警邮件（`_fail()`，`app/csi_sync_worker.py:569-591`）。

入队被拒（`400`，原因在外层）：

```json
{ "success": false, "message": "缺少必填参数: site" }
```

落库：`errormessage` = `HTTP 400 入队被拒：缺少必填参数: site`。

---

## 5. 幂等与重试

### 5.1 三层幂等

| 层 | 键 | 作用 | 出处 |
|---|---|---|---|
| 入队去重 | `(coil_id, lot_id)` + 生效状态 | 同一卷标/批次只发一次（完成备料重复点击） | `app/services/csi_interface.py:220-233` |
| 外部接口去重 | `(site, documentNumber, job, item, lot)` | 重复请求返回 `200 duplicated`，不重复入队 | `app/routes/external.py:1166-1187` |
| 平台幂等 | `traceId + site + documentNumber` | 重复提交同 traceId 不重复过账 | `app/services/csi_interface.py:20` |

### 5.2 提交 + 轮询

`_submit_and_poll()`（`app/csi_sync_worker.py:429-452`）：

- 单轮最多轮询 `CSI_SYNC_POLL_ATTEMPTS = 4` 次，间隔 `CSI_SYNC_POLL_INTERVAL = 5` 秒，单次超时 20 秒 → 最坏约 20~30 秒。
- **每次轮询都用同一份 payload（同一 traceId）重新 POST**，不是「查询接口」。
- 拿到非 `pending` 结果立即返回；4 次仍是 `QUEUED/PENDING` → 保留状态，下一轮（≥45s 后）继续同 traceId 轮询。

### 5.3 三类结果的处置

| kind | 触发 | 是否重试 | 落库 |
|---|---|---|---|
| `success` | `data.status=SUCCESS` + ion 全通过 | — | `is_sync=1 / SUCCESS` |
| `pending` | `QUEUED` / `PENDING` | 下一轮同 traceId 继续轮询（不计次数） | `status` 保留 `QUEUED/PENDING` |
| `transient` | 网络异常、超时、非 200、非 JSON、缺 `data`、未知 status | **同 traceId** 重试，最多 `CSI_SYNC_MAX_TRANSIENT = 3` 次 | 未达上限：`transient_count+1`，保留行；达上限：转人工 |
| `failed` | 409 / 400 / 401 / 403 / `FAILED` / `UNCONFIRMED` / ion 明细失败 | **不重试，立即转人工** | `is_sync=2 / FAILED_MANUAL` |

关键实现细节：

- `transient_count` 与 `retry_count` 是两套计数：前者只在网络类失败时 +1，达 3 次后转人工；后者只统计「失败/被重发」次数，**不参与任何决策**（`app/csi_sync_worker.py:97-107`、`569-591`）。
- 网络类失败转人工时，`errormessage` 前缀固定为 `网络/服务异常重试 N 次仍失败：`，告警邮件据此区分处置方向（`app/csi_sync_worker.py:658-668`、`782-785`）。
- `CSI_SYNC_MAX_RETRY` 已废弃，读它只为兼容旧 `.env`（`app/csi_sync_worker.py:97-102`）。
- 为什么业务错误不重试：超发、批次无库存、单号被锁这类错误**重试结果不变**，只会刷日志、重复发告警邮件，还会把「单号已被平台记录」的情况搅乱。必须人工改数据后点「重新发送」。

### 5.4 重发（`POST /api/csi-interface/<id>/resend`）

`app/routes/csi_interface.py:268-373`。

- 权限：`warehouse` / `admin`，且非 admin 只能操作本站记录。
- 准入：`is_sync=1` → `400 该记录已成功过账，不允许重发`；状态不在 `FAILED_MANUAL/NOOP/CANCELLED/FAILED` 且 `is_sync≠2` → `400 该记录正在同步中，请稍后再试`。
- 动作：`_retry_trace_id()` 生成**新 traceId** → 重置 `is_sync=0 / status=PENDING / retry_count=0 / transient_count=0 / errormessage=NULL / submitted_at=NULL / last_poll_at=NULL` → 同步执行一次 `_process_row()` 并返回结果。
- **`documentNumber` 不变**，job-issue 的新 traceId 仍是 `JI{job}-{item}-{uuid12}`（`app/csi_sync_worker.py:211-227`）。

> ⚠️ **已知坑：重发不会重置 `from_location` / `to_location`。**
> 重发只是换 traceId + 重置状态，库位沿用行内原值。如果原行 `to_location` 是错的（例如历史数据写成了 `Floor-Core`），点多少次「重新发送」都会以同样理由失败。
> 处置：先在接口表里把 `to_location` 改成批次实际库位（或作废该行重新入队生成新行），再重发。

### 5.5 状态机速查

| `is_sync` / `status` | 含义 | 同步器会再处理吗 |
|---|---|---|
| `0 / PENDING` | 待提交（新入队或重发） | 会 |
| `0 / SUBMITTING` | 已抢占、正在提交/轮询 | 45s 后未推进才允许再抢占 |
| `0 / QUEUED` | 平台已入队、未终态 | 会（同 traceId 继续轮询） |
| `1 / SUCCESS` | 成功过账 | 不会 |
| `2 / FAILED_MANUAL` | 失败转人工 | 不会（仅「重新发送」可复活） |
| `2 / CANCELLED` | 人工/业务取消 | 不会 |
| `2 / NOOP` | 物料已在目标库位（仅 qty-move） | 不会 |
| `1 / REVERSED` | 已被反向冲销（仅 qty-move） | 不会 |

---

## 6. 业务前置条件（提交前逐项确认）

- [ ] **1. 工单上该物料的剩余可发量足够**
      剩余可发 = `QtyReleased - QtyIssued`。本次 `quantity` 超过剩余量 → 平台拒绝：`本次要发的数量比工单还能发的数量多 N`。
      *本地代码不做预检*（`_process_row` 只校验数量 > 0），这条由平台把关。

- [ ] **2. `location` 上该批次确有库存**
      发料是按「物料 + 批次 + 库位」扣库存，库位写错就会扣不到。实测案例：批次全在 `Floor-Core`、而提交的 `location` 是 `RM-B009-4`（该库位该批次为 0）→ 平台返回 `转移数量 X 大于批次存货 0`。

- [ ] **3. 单号未被平台锁定**
      同一 `documentNumber` 被平台受理过之后再提交（尤其改了数量）→ `HTTP 409 CONFLICT`。

- [ ] **4. 物料在该工单 BOM 中，且 `Backflush`（倒冲）= 0**
      同步器会自动处理：`_ensure_job_backflush_cleared()` 先经 **Infor CSI IDO**（`CSI_IDO_BASE` + `/ido/update/SLJobMatls`，不走宝鼎网关）把 `SLJobMatls.Backflush` 改成 0；改不动就不提交、直接转人工（`app/csi_sync_worker.py:753-758`、`app/services/csi_service.py:341-432`）。
      真实失败文案（前缀统一为 `取消 Backflush 失败：`）：
      - `未找到工单物料 J000029867-0001 / B052552（或查询失败）` —— 物料不在该工单 BOM，或工单/后缀写错
      - `工单物料 ... 缺少 _ItemId，无法更新`
      - `工单物料 ... 缺少 OperNum/Sequence，无法更新`
      - `更新工单物料 ... 被 IDO 拒绝：<Message>`

- [ ] **5. `site` 已配置且与仓库匹配**
      `310 → C1M1`、`410 → M1M1`（`app/config.py:83-84`）。站点无映射 → 完成备料静默跳过，不产生接口行（`app/services/csi_interface.py:210-213`）。

- [ ] **6. `documentNumber` ≤ 12 位**
      单号列 `VARCHAR(12)`，超长入队阶段直接抛错回滚（`app/services/csi_interface.py:247-253`）。

---

## 7. 错误码 / 错误消息对照

### 7.1 HTTP 层

| HTTP | 本地判定 | `errormessage` 实际文案 | 触发条件 | 处置建议 |
|---|---|---|---|---|
| `409` | `failed` | `HTTP 409 CONFLICT：单号被平台锁定/冲突，需人工核对` | 单号已被平台受理/锁定后再次提交（改数量重投最易触发） | 不改单号重投；确认平台是否已记账。未记账则作废该行重新入队拿新单号 |
| `400` | `failed` | `HTTP 400 入队被拒：<平台原文>` | 报文不合契约 | 按平台原文改字段（见 7.2） |
| `401` / `403` | `failed` | `HTTP 401 认证失败：请检查 CSI_SYTELINE_TOKEN` | token 失效/过期/未配置 | 更新 `CSI_SYTELINE_TOKEN` 后「重新发送」 |
| 其他非 200 / 超时 | `transient` | `请求异常：...` / `HTTP 502：...` | 网关不可达、内网不通、代理干扰 | 检查 `bdsytelineapi` 连通性与系统代理（见 8 章），同 traceId 自动重试 3 次 |
| `200` 但非 JSON | `transient` | `响应不是合法 JSON` | 网关返回 HTML 错误页 | 看网关日志；同 traceId 重试 |

### 7.2 业务层（`200` + `data.status=FAILED`，`errormessage` 即平台原文）

| 平台原文 | 触发条件 | 处置建议 |
|---|---|---|
| `本次要发的数量比工单还能发的数量多 N` | 超发：`quantity > QtyReleased - QtyIssued` | 减少本次数量（拆行/改批次），或先在 ERP 调整工单发放量；**不要原样重发** |
| `转移数量 X 大于批次存货 0` | `location` 上该批次无库存 | 把 `to_location` 改成批次实际库位后重发；或换有库存的批次 |
| `缺少必填参数: site` | 报文 `site` 为空/未传 | 检查接口行 `siteRef` 是否为空。本地不会发空 `site`——出现此文案说明是**外部直接调网关**或历史脏数据 |
| `ion 明细失败：<明细 Message>` | 事务头成功但明细失败 | 按明细 Message 处理，通常同样是超发/批次/单号问题 |
| `UNCONFIRMED：平台未确认，请先核对 CSI 是否已入账` | 平台未给出终态确认 | **先到 CSI 查该单号是否已过账**，已过账则人工核销，不得直接重发 |
| `取消 Backflush 失败：<原因>` | 倒冲清 0 失败（非平台返回，本地生成） | 按原因修工单物料（BOM/`_ItemId`/`OperNum`）后重发 |

> 判定规则很关键：本地把 409/400/401/403/FAILED/UNCONFIRMED **一律视为业务失败**，不重试。`errormessage` 前缀不是 `网络/服务异常重试` 的，全部按业务问题处理。

---

## 8. 调用示例

### 8.1 curl 直连宝鼎（排障用）

```bash
curl --noproxy '*' -X POST "http://bdsytelineapi/api/cmd/sl-job-issue" \
  -H "Content-Type: application/json" \
  -H "X-Syteline-Token: slt_xxxxxxxxxxxxxxxx" \
  -d '{
    "traceId": "JIENG0001317-0000-D041636-0f1e2d3c4b5a",
    "site": "410",
    "documentNumber": "MP301501",
    "job": "ENG0001317",
    "suffix": "0000",
    "item": "D041636",
    "quantity": 4,
    "lot": "202609010000076",
    "warehouse": "M1M1",
    "location": "RM-B009-4"
  }'
```

- `--noproxy '*'` **必须加**：宝鼎是内网主机，被塞进本机代理会直接失败。
- 重复执行同一条命令（同 traceId）是安全的：平台幂等，返回同一条结果。
- 改数量再执行会得到 `409 CONFLICT`。

### 8.2 Python requests

```python
import requests

payload = {
    "traceId": "JIENG0001317-0000-D041636-0f1e2d3c4b5a",
    "site": "410",
    "documentNumber": "MP301501",
    "job": "ENG0001317",
    "suffix": "0000",
    "item": "D041636",
    "quantity": 4,
    "lot": "202609010000076",
    "warehouse": "M1M1",
    "location": "RM-B009-4",
}

sess = requests.Session()
sess.trust_env = False          # ⚠️ 关键：忽略 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 与注册表代理
try:
    resp = sess.post(
        "http://bdsytelineapi/api/cmd/sl-job-issue",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-Syteline-Token": "slt_xxxxxxxxxxxxxxxx",
        },
        timeout=20,
    )
finally:
    sess.close()

print(resp.status_code, resp.text)
```

> ⚠️ **代理坑（踩过）**：本机/服务器上若存在系统代理（哪怕已关闭但仍在注册表里，如 `127.0.0.1:58229`），`requests` 会把内网请求塞进代理，直接 `ProxyError`，整条过账链路全挂。`trust_env=False` 是必写项，看板同步器就是这么做的（`app/csi_sync_worker.py:114-123`）。

### 8.3 通过看板入队（推荐路径）

业务方**不要**直接打宝鼎，二选一：

**方式 A：外部系统调看板 API**

```bash
curl -X POST "http://10.0.6.86:5001/api/external/job-issue" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <EXTERNAL_API_KEY>" \
  -H "X-Site-Ref: 410" \
  -d '{
    "job": "ENG0001317-0000",
    "item": "D041636",
    "lot": "202609010000076",
    "quantity": 4,
    "unit": "EA",
    "submitter": "jason.pang",
    "documentNumber": "MP301501"
  }'
```

返回：

```json
{ "success": true, "id": 1234, "documentnumber": "MP301501", "message": "已入队" }
```

重复提交同 `(site, documentNumber, job, item, lot)` → `200 {"success": true, "duplicated": true, "id": 1234, "message": "已存在相同发料记录，未重复入队"}`（`app/routes/external.py:1182-1187`）。

约束：`job/item/lot/unit/submitter` 必填，`quantity` 为数字且 > 0（四舍五入 4 位），`documentNumber` ≤12 位，为空时取 `job[:12]`（`app/routes/external.py:1141-1162`）。

**方式 B：走「完成备料」自动入队**

申请单明细填了 `job_order`，仓库完成备料时自动写接口表，无需额外调用：

```
kr_request_item.job_order = 'ENG0001317-0000'
        ↓ 完成备料
kr_csi_interface: api_name='sl-job-issue', job='ENG0001317-0000',
                  to_location=<批次库位>, documentnumber='MP301501',
                  traceid='JIENG0001317-0000-D041636-xxxxxxxxxxxx',
                  status='PENDING', is_sync=0
        ↓ 同步器（≤60s）
宝鼎 sl-job-issue
```

> ✅ **外部入口的库位规则（设计约束，不是缺陷）**：`POST /api/external/job-issue` 的 `to_location` **固定写为 `Config.CSI_TO_LOCATION`（`Floor-Core`）**（`app/routes/external.py:1120`、`1198-1199`），服务端不接受库位入参。外部系统只负责「线边仓 → 工单」的扣账，实物必须已经在 Floor-Core。
>
> 只有看板内部「完成备料」链路才使用批次实际库位（`app/services/csi_interface.py:258-263`）。两条路径不要混用。

---

## 9. 字段速查表

| 字段 | 类型 | 必填 | 约束 | 常见错误 |
|---|---|---|---|---|
| `traceId` | string | 是 | ≤128；`JI{job}-{item}-{uuid12}`；同一次发料生命周期内不变 | 重试时换新值 → 重复过账；长度溢出 → 平台拒绝 |
| `site` | string | 是 | `"310"` / `"410"` | 空值 → `缺少必填参数: site`；站点与仓库不匹配 → 找不到库存 |
| `documentNumber` | string | 是 | ≤12 位；同站点内唯一 | 超长 → 入队抛错回滚；重复用 → `409 CONFLICT` |
| `job` | string | 是 | 工单主号，不含后缀 | 传了完整 `-0000` 但没拆 → 平台找不到工单 |
| `suffix` | string | 否 | 1~4 位纯数字字符串 | 传 `0` 而非 `"0000"` → 工单定位失败（待确认平台对位数是否敏感） |
| `item` | string | 是 | 必须在工单 BOM 中 | BOM 无此料 → 取消 Backflush 失败 / 发料被拒 |
| `quantity` | number | 是 | > 0；≤4 位小数 | 超发 → `本次要发的数量比工单还能发的数量多 N`；0 → 本地直接转人工 |
| `lot` | string | 是 | 与库存批次一致 | 批号错 → `转移数量 X 大于批次存货 0` |
| `warehouse` | string | 是 | `C1M1`(310) / `M1M1`(410) | 与 `site` 不匹配 → 库存查不到 |
| `location` | string | 是 | **外部入口固定 `Floor-Core`**（服务端写死，不收此参数）；内部链路才是批次库位 | 外部传其他库位 → 不符合契约；内部写成 `Floor-Core` → `转移数量 X 大于批次存货 0` |

---

## 10. AI Agent 集成提示（照做清单）

1. **只走看板接口表或 `/api/external/job-issue`，不要直连宝鼎**。直连会失去失败分类、重试、告警、Backflush 前置与并发保护。
2. **幂等键这样生成**：`traceId = f"JI{job}-{item}-{uuid4().hex[:12]}"`，`job` 用完整工单号（含 `-0000`）。生成一次就**存下来**，重试/重发沿用；只有「换一笔业务」时才生成新的。
3. **`documentNumber` 一旦提交就不要改**。长度 ≤12；失败后改数量重投必然 409。失败要处置时改的是数据（批次/库位/数量），走「重新发送」。
4. **数量精度 4 位**：`round(qty, 4)`；整数提交为 int（`4` 而不是 `4.0`）；必须 > 0。
5. **库位规则按入口区分（重要）**：
   - 走 **`/api/external/job-issue`（外部同事常用）** → **不要传 location，也不要想当然填仓库库位**。服务端固定用 `Floor-Core`，前提是实物已在线边仓。
   - 走 **看板内部「完成备料」** → `location` 取批次实际库位（`kr_wire_coil.loc` / `kr_request_item_lot.loc`），**绝不写死 `Floor-Core`**，且写之前确认该库位该批次有库存。
   - 混用后果：外部把仓库库位当 Floor-Core 传 → 平台扣不到库存，报 `转移数量 X 大于批次存货 0`。
6. **工单号拆分**：`job` 取最后一个 `-` 前的主号，`suffix` 取其后的 1~4 位纯数字；不符合形态就整串当 `job` 传、不传 `suffix`。
7. **失败先读 `errormessage`**：
   - 前缀是 `网络/服务异常重试` → 网络问题，可继续同 traceId 重试或查连通性；
   - 含 `409 CONFLICT` → 单号锁，人工核对；
   - 含 `还能发的数量` → 超发，改数量；
   - 含 `大于批次存货` → 库位/批次错，改 `location` 或换批次。
8. **业务错误不要盲目重试**。重试不改变结果，只会重复告警；必须改数据后走「重新发送」（会换 traceId，但**不会**重置库位）。
9. **`UNCONFIRMED` 先查账再动**：到 CSI 查该单号是否已入账，已入账就人工核销，不要重发。
10. **HTTP 客户端必须 `trust_env=False`**，并设 20 秒超时；单次提交可能耗时 20~30 秒（4 次轮询 × 5 秒），调用方超时要放宽。
11. **成功判定看字段不看文案**：`data.status == "SUCCESS"` 且 `ion` 全部 `Success` 为真才算成功；不要用 `"成功" in text` 之类判断。

---

## 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-10-21 | 新增本文档 | 依据 `csi_sync_worker.py` / `csi_interface.py` 现行实现整理 |
| 2026-10-21 | **`location` 改为批次/卷标库位** | 由固定 `Floor-Core` 改为取批次实际库位（`app/services/csi_interface.py:258-263`）。原因：批次不在 `Floor-Core` 时扣不到库存，报 `转移数量 X 大于批次存货 0` |
| 2026-10-21 | **停止实时库位纠正** | 同步器不再调用 `get_lot_location` 校正/兜底库位，一律用接口表行内值；行内为空即转人工（`app/csi_sync_worker.py:29-34`、`698-707`）。原因：同一批次可能同时在车间与仓库有量，实时查询只返回一处，覆盖后会误判 NOOP（id=51 案例） |
| 2026-10-21 | **明确外部入口库位固定 `Floor-Core`** | 业务确认：`POST /api/external/job-issue` 只允许线边仓发料，服务端写死 `Config.CSI_TO_LOCATION`（`app/routes/external.py:1120`、`1198-1199`），**不接受库位入参**。与内部「完成备料」用批次库位是两条独立路径，勿混用 |
