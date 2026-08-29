# 代码审查报告：线卷全库存管理功能

- 审查人：严过关（代码审查员）
- 审查日期：2026-08-14
- 设计基线：`docs/wire_coil_inventory_spec.md`（v1.0）
- 审查范围：
  1. `app/routes/coil.py`（9 个接口 + 4 个兼容别名）
  2. `app/services/label_print_service.py`
  3. `app/services/csi_service.py`（`get_item_unit`，约 252 行）
  4. `scripts/migrate_wire_coil.py`
  5. `app/static/js/coil.js`
  6. `app/templates/request_detail.html`
  7. `app/config.py`（`LABEL_*` / `UNIT_CONVERT_FACTOR`）
  8. `app/__init__.py`（blueprint 注册）

---

## 一、结论摘要

| 级别 | 数量 | 说明 |
|------|------|------|
| P0 阻断 | 0 | 未发现可直接利用的高危漏洞（SQL 注入均参数化，XSS 输出均有转义） |
| P1 严重 | 2 | 打印范围错误（忽略入参全量打印）；CSI 批量取单位无整体超时上限 |
| P2 一般 | 5 | 并发重试在 REPEATABLE READ 下失效；siteref 用户可控；只读接口权限过宽；参数强转 500；卷号接口无角色限制 |
| P3 建议 | 16 | 见第三节 |

总体评价：**结构良好**——蓝图层、打印通道解耦、站点隔离、单位换算服务端化、操作日志、参数化 SQL 均落地且与现有代码风格一致；DDL 与文档 2.2/2.3 一致（无物理外键、逻辑外键 + 索引风格正确）。问题集中在**并发正确性**、**打印接口入参被忽略**、**CSI 故障时的可用性降级**三处，建议在联调验收前修复 P1/P2。

---

## 二、严重问题（P1）

### P1-1 打印接口忽略前端传入的卷号，实际全量打印该申请单全部卷标

- **文件/行号**：
  - `app/routes/coil.py:684-712`（`request_coils_print`）
  - `app/static/js/coil.js:351-363`（`printCoils`）、`:342-344`（保存并打印）
- **问题描述**：前端 `printCoils(coilIds)` 明确传入要打印的卷号（已录入列表中"单卷打印"、以及"保存并打印"只传本次新增卷号），但后端 `request_coils_print` 完全忽略请求体 `coil_ids`，改而执行：

  ```python
  cursor.execute("SELECT coil_id FROM kr_wire_coil WHERE request_id = %s ORDER BY id", (request_id,))
  coil_ids = [r['coil_id'] for r in cursor.fetchall()]
  ```

  随后把该申请单**全部**卷标交给 `print_coils_inner` 打印。
- **影响**：① 单卷打印会打出一整单所有标签，浪费标签纸；② "保存并打印"会把历史已打印卷重复再打一遍；③ 与文档 3.3.6"批量打印指定 coil_ids"的契约不符。这是交互层面的实际功能缺陷，直接破坏 A12 验收。
- **修复建议**：`request_coils_print` 应读取请求体 `coil_ids`，并校验每个卷号确实属于该申请单（`WHERE request_id = %s AND coil_id IN (...)`），只打印入参指定的卷；空则回退为全量打印并在响应中说明。

### P1-2 CSI 批量取单位在故障/超时场景会长时间阻塞请求并占用 DB 连接

- **文件/行号**：
  - `app/services/csi_service.py:73-108`（`_get_ido`，`timeout=60`，超时重试一次，单物料最坏 120s）
  - `app/routes/coil.py:205-219`（`_get_unit_cached`，无整体超时上限）、`:386-390`（录入逐行取单位）、`:577-578`（`request_coil_units` 逐物料查询）
- **问题描述**：`create_coils` 单批最多 500 行，`request_coil_units` 对申请单全部明细逐个查询。若 CSI 服务挂起（只重试不快速失败），每个**不同物料**等待最多 120s：50 个不同物料 ≈ 100 分钟，100 个 ≈ 200 分钟。整个期间该 HTTP 请求的 MySQL 连接与事务被长期持有，gunicorn worker 数被占满时会导致系统整体不可用。
- **影响**：CSI 故障时录入/弹窗初始化接口几乎必然超时，且可能拖垮其他模块；违背文档"CSI 查询失败时允许为空并给出警告（不阻断录入）"的降级承诺——降级逻辑存在，但等不到降级那一刻。
- **修复建议**：
  1. `get_item_unit` 使用短超时（如 `timeout=10`）且 `retry_on_timeout=False`；
  2. `_get_unit_cached` 增加进程内"故障熔断"：短时间内（如 60s）连续 N 次失败后，直接返回 None，不再打 CSI；
  3. 或给单次请求的批量单位获取设置总时间预算（如 15s），超预算余下物料直接置空。

---

## 三、一般问题（P2）

### P2-1 卷号并发重试在 InnoDB REPEATABLE READ 下可能完全失效

- **文件/行号**：`app/routes/coil.py:192-202`（`_gen_next_id`）、`:425-452`（`_insert_coil_with_retry`）
- **问题描述**：`_gen_next_id` 用 `SELECT COUNT(*) ... WHERE coil_id LIKE '前缀%'` 计算流水。在默认隔离级别 REPEATABLE READ 下，**一致性快照读**看不到并发会话已提交的新数据。并发两个请求取到相同卷号 → 一个插入成功，另一个触发 `uk_coil_id` 冲突 → `ROLLBACK TO SAVEPOINT` 后重试 `_gen_next_id`，**快照未刷新，返回同一个号** → 3 次重试全部冲突，最终报"卷号已存在"。
- **影响**：与文档 3.3.1"并发冲突捕获后重新取号重试（最多 3 次）"以及验收 B3"连续快速提交 10 个卷号不重复、不报错"相悖；并发场景会随机失败，用户需手动重试。
- **修复建议**（任选）：
  1. 重试取号改为**当前读**：`SELECT COUNT(*) ... FOR UPDATE` 或 `SELECT COALESCE(MAX(CAST(SUBSTRING(coil_id,7,3) AS UNSIGNED)),0) FROM kr_wire_coil WHERE coil_id LIKE %s FOR UPDATE`；
  2. 或最简方案：冲突后不查库，直接在失败卷号末尾流水 `+1` 推演（含进位/日界判断），配合唯一索引兜底；
  3. 若走事务级方案，重试前需先结束旧快照（`db.rollback()`），注意会连同本批已插入数据一起回滚，需用 SAVEPOINT 结构保护。

### P2-2 `batch_coil_units` 兼容别名接口 siteref 用户可控，绕过站点隔离

- **文件/行号**：`app/routes/coil.py:582-602`
- **问题描述**：

  ```python
  siteref = (data.get('siteref') or user.get('siteref') or '310')
  ```

  任意登录用户可自行传 `siteref`，驱动服务端以指定站点的 CSI 上下文（`Config.SITE_CSI_COMPANY[siteref]`）发起外部查询。该接口**未做 warehouse/admin 角色限制**。虽然返回的仅是物料单位、敏感度低，但违背"非 admin 仅能操作本站点"原则，且 `CSIClient(siteref=用户可控值)` 的 company 字符串由拼接生成，存在被乱用的空间。
- **修复建议**：不信任前端传入的 `siteref`，固定取 `user.get('siteref')`（无则 403）；若该接口确需保留，增加 `_check_warehouse_or_admin()` 校验。文档未收录此接口，建议同时补充文档或删除。

### P2-3 只读接口未限制 warehouse/admin 角色

- **文件/行号**：
  - `app/routes/coil.py:457-481`（`list_request_coils`）
  - `app/routes/coil.py:750-768`（`coil_label_preview`）
  - `app/routes/coil.py:771-803`（`request_coils_preview`）
  - `app/routes/coil.py:967-1007`（`list_consumption`）
- **问题描述**：上述接口仅校验登录 + 站点，`requester` 等角色可读取本单/本站点的卷标明细、消耗记录与标签渲染数据。文档 3.1 明确"卷标录入/打印/出库登记仅限 warehouse/admin"，读取侧未明确，但卷标/出库数据含工单、操作人、加工参数，对普通领料员开放过宽。
- **修复建议**：若业务允许领料员查看本单卷标，保持现状并在文档补充说明；否则统一加 `_check_warehouse_or_admin()`。

### P2-4 `list_coils` 的 `request_id`/日期参数未校验，非法输入返回 500

- **文件/行号**：`app/routes/coil.py:492-519`
- **问题描述**：
  - `params.append(int(request_id))`：传入非数字（`?request_id=abc`）直接抛 `ValueError` → 未捕获 → 500；
  - `date_from`/`date_to` 未做格式校验，非法值由 MySQL 做日期类型比较，可能抛 `DataError` → 500。
- **修复建议**：`int()` 包一层 try/except 返回 400；日期用 `datetime.strptime('%Y-%m-%d')` 预校验后再拼条件。

### P2-5 卷号生成接口未限制角色，泄露每日卷号用量

- **文件/行号**：`app/routes/coil.py:224-258`（`next_coil_id`）、`:261-294`（`coil_number_alias`）
- **问题描述**：仅检查登录。任意登录用户可反复调用获取当前服务器日期、当日已用计数（`daily_count`），泄露业务量；且与"卷标录入仅限 warehouse/admin"的权限基调不一致（虽然保存接口有权限兜底）。
- **修复建议**：与录入保持一致，加 `_check_warehouse_or_admin()`。

---

## 四、建议问题（P3）

| # | 文件:行号 | 问题描述 | 修复建议 |
|---|-----------|----------|----------|
| 1 | 文档 3.3.9 / A14 vs `coil.py:111` | `converted_length` 精度不一致：文档示例 0.2505（4 位小数），代码 `round(...,2)` 得到 0.25，DDL `DECIMAL(12,2)` 也只能存 2 位。代码与 DDL 一致，**文档示例错误**。 | 修订文档示例为 0.25，避免验收时误判。 |
| 2 | `coil.py:896-898` | `if not converted_length:` 在极小值（如 0.0001mm ÷ 1000）被 round 成 0.0 时会误判为"未收录换算系数"而置 NULL。 | 改为 `if converted_length is None:`。 |
| 3 | `coil.py:142-144, 205-219` | `_unit_cache` 为无界 dict，过期键不清理，长期运行内存缓慢增长。 | TTL 惰性删除时顺带清理，或加最大条目上限。 |
| 4 | `coil.py:216` | 错误处理用 `print()` 输出到 stdout，不符合现有 `logger` 风格，gunicorn 下混入访问日志。 | 改 `logger.warning`。 |
| 5 | `coil.py:607-631` | `_fetch_coils_for_print` 为死代码，未被任何调用方使用（`print_coils` 内联了相同查询）。 | 删除或复用以消除双份逻辑。 |
| 6 | `label_print_service.py:211-212, 248-256` | GDI 打印创建的 `black_brush`/`white_brush` 未销毁，每卷泄漏 2 个 GDI 对象，长跑 worker 资源累积。 | 打印结束 `dc.DeleteObject(brush.GetSafeHandle())` 或改用 with 语义。 |
| 7 | `label_print_service.py:135-155` | `build_tspl` 文本用 `"` 包裹，`_clean_fd` 未清理双引号，物料号含 `"` 时 TSPL 指令错乱。 | `_clean_fd` 同时移除 `"`、`;` 等 TSPL 控制字符。 |
| 8 | `coil.js:380-406, 515-547` | 出库弹窗默认长度总是填整卷总长，不显示已出库/剩余长度；change 校验只对卷长而非剩余长度，已部分出库的卷默认提交必被后端拒绝。 | 前端展示"已出库/剩余长度"，默认填剩余长度。 |
| 9 | `coil.js:210, 213` | 用户数据经 `escapeHtml` 后嵌入 HTML `onclick` 属性（反模式）；当前 coil_id 为 9 位数字风险极低，但属隐患。 | 改用 `data-coil-id` + 事件委托。 |
| 10 | `request_detail.html:516` | `userRole = '{{ session.user.role }}';` 会话值直接进 JS 字符串，含引号会破坏脚本。 | 改 `{{ session.user.role|tojson }}`。 |
| 11 | `migrate_wire_coil.py:199-217` | ① 对生产大表批量 `ALTER ADD COLUMN ... + DROP COLUMN request_id` 会锁表，需低峰执行；② 旧表若已有消耗数据且含 `request_id`，DROP 后该列历史无法追溯（文档已如此设计，但执行前应确认无在用数据）。 | 运维侧确认低峰执行；执行前备份/导出含 request_id 的历史。 |
| 12 | `migrate_wire_coil.py:80-135` vs `coil.py:48-100` | 宽表字段清单在两处独立维护（迁移脚本 / 后端校验），后续增删字段极易漂移。 | 抽取共享常量（如 `app/models/coil_fields.py`）单源维护。 |
| 13 | `label_print_service.py:322` | `_gateway_print` 使用 `verify=False` 禁用 TLS 校验（CSI 客户端为既有风格，网关为新增点）。 | 网关通道至少支持 `LABEL_PRINT_GATEWAY_VERIFY` 配置，内网可暂关。 |
| 14 | `coil.py:261,582,684,771` | 4 个兼容别名接口（`coil-number`/`coils/units`/`coils/print`/`coils/preview`）均未写入设计文档，增加维护面与攻击面，且行为与主接口有差异（如 P2-2、P1-1）。 | 补充文档或直接收敛到文档 9 个接口。 |
| 15 | `coil.py:353` | R6 校验 `part_number not in req_parts` 为 Python 大小写敏感比较，而 MySQL 排序规则不敏感，存在 `a123456` vs `A123456` 判定不一致。 | 比较前统一 `upper()`，或改为 SQL 侧 `IN` 判断。 |
| 16 | `coil.py:114-140` | 宽表文本字段（`wire_spec`/`manual_crimp_flow`/`preinstall_remark_*` 等）未做长度校验，超长输入触发 MySQL `Data too long` → 外层 except 返回 500。 | 按 DDL 列宽做 `[:maxlen]` 截断或长度校验返回 400。 |

---

## 五、符合项核对（通过项）

- **SQL 注入**：所有查询/插入均使用 `%s` 参数化；`IN (...)` 占位符数量由入参长度推导且长度受限（`MAX_BATCH=500`）；动态列名仅来自白名单（`CONSUMPTION_EXTRA_FIELDS` 键、`CONSUMPTION_WIDE_COLUMNS`）；`site_filter` 来自 `get_site_filter` 硬编码字符串。未见注入点。
- **XSS**：前端渲染用户/数据库数据均经 `escapeHtml`；数值字段经 `parseFloat` 或 `float()` 归一；`renderLogs` 等既有函数也保持转义。
- **站点隔离**：`_check_site` + `get_site_filter` 组合在 9 个主接口中均落地（读接口与写接口一致）。
- **单位换算服务端化**：`create_consumption` 用 `_convert_length` 覆盖前端传值；未收录单位置 NULL + 警告不阻断，与文档 R11 一致。
- **整卷置 issued**：`new_used >= total_mm - 0.0001` 判定且事务原子提交，正确。
- **R4 A/B 强制校验**：前后端双份拦截一致（`p[:1].upper() in ('A','B')` vs `/^[ab]/i`）。
- **操作日志**：`kr_operation_log.request_id` 可空（`init_db.py:131`），`print_coils` 传 `None` 合法；action 前缀 `COIL_*`/`OUTBOUND_*` 符合文档。
- **DDL 与文档一致性**：`kr_wire_coil` 与 `kr_wire_coil_consumption` 的字段、索引、`uk_coil_id`、无物理外键、逻辑外键 + 索引风格均与文档 2.2/2.3 一致；`consumption` 表已移除 `request_id`，追溯走 `JOIN kr_wire_coil`（`coil.py:985-989` 正确）。
- **依赖**：`requirements.txt` 已含 `pywin32>=306`（仅 Windows）、`python-barcode`、`Pillow`，与文档 5.2 一致。
- **打印降级**：`print_labels` 逐卷 try/except + 错误收集，环境缺依赖返回可读错误，不影响申请单主流程；GDI/RAW 用 `threading.Lock` 串行化，符合文档 5.4/5.5。
- **空数据边界**：空 `items`/空 `coil_ids`/空表 `COUNT` 均被显式处理。
- **blueprint 注册**：`coil_bp` 已在 `create_app` 注册（`app/__init__.py:21,30`），无路由冲突（固定路径优先于 `<coil_id>` 变量路由，且 method 区分）。

---

## 六、建议修复优先级

1. **上线前必须修复**：P1-1（打印范围）、P1-2（CSI 超时）。
2. **验收前建议修复**：P2-1（并发重试）、P2-4（参数 500）、P2-5（卷号接口权限）；P2-2/P2-3 与业务确认后收敛。
3. **可随迭代排期**：P3 全部。
