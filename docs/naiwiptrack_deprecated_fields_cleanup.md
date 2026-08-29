# naiwiptrack 废弃字段清理 — 修改说明

> 生成日期：2026-08-29
> 交付物：本说明文档 + 同目录补丁 `naiwiptrack_deprecated_fields_cleanup.patch`
> 状态：**交付参考**。补丁未实际应用于 naiwiptrack 源码仓库，需由 naiwiptrack 开发人员手动应用。

---

## 1. 修改背景

物料领取看板（Flask/MySQL）已从表结构与 API 中移除以下 **5 个废弃字段**：

| 字段 | 说明 | 移除位置 |
|---|---|---|
| `wire_spec` | 线材规格（废弃） | 看板消耗登记表结构 + 消耗登记 API（INSERT 不再写入） |
| `color` | 颜色（废弃） | 看板消耗登记表结构 + 消耗登记 API |
| `checker_first` | 首件确认人（废弃） | 看板消耗登记表结构 + API（SELECT/INSERT 不再涉及） |
| `checker_last` | 末件确认人（废弃） | 看板消耗登记表结构 + API |
| `actual_shear_length_last` | 末件剪切长度（废弃） | 看板消耗登记表结构 + API |

看板侧 `/api/external/*` 已清理完毕：

- `SELECT/INSERT` 均不再涉及上述字段；
- `/api/external/cutting-ref` 响应已从 **16 列减为 14 列**，不再返回 `wire_awg` / `color`（`wire_awg` 为 `wire_spec` 在裁剪参数 API 中的对应字段）。

因此，naiwiptrack（production-tracking）侧仍引用这些字段的代码会产生 `undefined`/`null` 数据或冗余列，需要同步清理。本补丁仅清理**明确确认**的引用点，遵循最小改动原则。

---

## 2. 修改清单

### 2.1 后端 `production-tracking/backend/src/controllers/cuttingController.ts`

| 编号 | 行号 | 必改/建议 | 原代码 | 修改后代码 |
|---|---|---|---|---|
| A1 | 996-997 | **必改** | `wire_spec: body.wire_spec || null,`<br>`color: body.color || null,` | 删除这两行（POST `/api/external/consumption` 请求 body 不再携带废弃字段） |
| A2-1 | 547-548 | 建议清理（已确认） | `wire_awg: r.wire_awg,`<br>`color: r.color,` | 删除这两行（从 `/api/external/cutting-ref` 响应取值映射给前端，看板已不返回 → 只会传 `undefined`） |
| A2-3 | 692-693 | 建议清理（已确认） | `wire_awg: r.wire_awg,`<br>`color: r.color,` | 删除这两行（同上，从聚合 refs 取值映射给前端） |
| A2-2 | 600-601 | 保留 | `wire_awg: null,`<br>`color: null,` | 不变（手动新增规格对象初始化，**非**从 cutting-ref API 响应取值，按规则保留） |
| A2-4 | 721-722 | 保留 | `wire_awg: null,`<br>`color: null,` | 不变（同上，手动新增行对象初始化，按规则保留） |

> **A2 清理最终范围说明**：四处中仅 **547-548、692-693** 符合「从 `/api/external/cutting-ref` 响应取 `wire_awg`/`color` 并映射传给前端」的描述，予以删除；**600-601、721-722** 是将 `wire_awg`/`color` 置 `null` 的手动规格对象初始化，不属于「从响应取值」，**保留不动**。另经检索，`wire_awg`/`color`/`wire_spec` 在 naiwiptrack 其余代码（前端页面、后端其他 controller）中**无其他使用**，故删除 547-548、692-693 后不存在任何读取方丢失数据；600-601、721-722 的 `null` 初始化实为死代码，后续如需可一并清理（本补丁不包含）。

### 2.2 前端 `production-tracking/frontend/src/pages/Report.tsx`

| 编号 | 行号 | 必改/建议 | 原代码 | 修改后代码 |
|---|---|---|---|---|
| B1 | 1477 | **必改** | `...t('expActualShearEquip'), t('expFirstChecker'), t('expLastChecker'), t('expScrapLen'),...` | `...t('expActualShearEquip'), t('expScrapLen'),...`（CSV 导出 headers 删除首/末件确认人两列，注意保留逗号） |
| B2 | 1482 | **必改** | `r.checker_first, r.checker_last, r.scrap_length_actual,` | `r.scrap_length_actual,`（CSV 导出 rows 不再输出确认人） |
| B3 | 2771-2772 | **必改** | `<TableHead>{t('checkerFirst')}</TableHead>`<br>`<TableHead>{t('checkerLast')}</TableHead>` | 删除这两行（消耗记录表格 header 移除首/末件确认人列） |
| B4 | 2802-2803 | **必改** | `<TableCell className="text-sm">{r.checker_first || '—'}</TableCell>`<br>`<TableCell className="text-sm">{r.checker_last || '—'}</TableCell>` | 删除这两行（消耗记录表格单元格不再渲染确认人） |

---

## 3. i18n key 保留说明

文件 `production-tracking/frontend/src/lib/i18n.ts` **不做修改**：

- **`checkerFirst` / `checkerLast`**：仍在活动检查表单中使用（`Report.tsx` 2616 / 2669 / 3256 / 3345 行），**必须保留**，本补丁仅删除消耗记录表格中引用它们的表头（B3），表单引用不受影响。
- **`expFirstChecker` / `expLastChecker`**：在 B1（CSV 导出 headers）删除后成为冗余 key。按最小改动原则**不删除**，冗余 key 不影响 TypeScript 编译与运行，可留待后续统一清理。

---

## 4. 应用方法

### 4.1 用补丁应用（推荐）

在 **`naiwiptrack/production-tracking`** 目录下执行：

```bash
# 1. 先检查能否干净应用（只检查不修改）
git apply --check naiwiptrack_deprecated_fields_cleanup.patch

# 2. 应用补丁
git apply naiwiptrack_deprecated_fields_cleanup.patch

# 3. 确认改动
git diff --stat
```

> 说明：补丁头部注释行（`#` 开头）仅为交付提示，`git apply` 会自动跳过，不影响应用（已在临时仓库实测通过）。

### 4.2 手动修改

按第 2 节清单逐处删除/修改即可，共 6 个 hunk：

- `cuttingController.ts`：3 处（A1、A2-1、A2-3）
- `Report.tsx`：3 处（B1、B2、B3+B4）

---

## 5. 验证建议

1. **后端类型检查**：在 `production-tracking/backend` 下执行
   ```bash
   npm run build   # 或 npx tsc --noEmit
   ```
2. **前端编译检查**：在 `production-tracking/frontend` 下执行
   ```bash
   npm run build   # 或 npx tsc --noEmit
   ```
   > 前端 tsconfig 开启了 `noUnusedLocals`/`noUnusedParameters`，本补丁只删除数组元素与表格行，不产生新的未使用变量，可放心编译。
3. **运行验证**：
   - 打开裁剪消耗登记页面，确认「消耗记录」表格不再显示「首件确认 / 末件确认」两列；
   - 导出 CSV，确认表头与数据行均不再包含「首件确认 / 末件确认」；
   - 正常登记一次消耗并查看工单全部线材列表，确认 `wire_awg`/`color` 相关数据不再出现 `undefined` 干扰展示。
4. **回归确认**：活动检查表单（首件/末件确认人输入）仍应正常显示与提交，其数据来自剪切检查 API，不受本补丁影响。

---

## 6. 交付验证记录（2026-08-29）

交付侧在**只读 naiwiptrack 源码**的前提下，于临时副本中完成了以下验证：

| 验证项 | 结果 |
|---|---|
| 临时 git 仓库 `git apply --check` | 通过（补丁头部注释行被 git 自动跳过） |
| 临时 git 仓库 `git apply` + 逐字节对比预期结果 | 一致（`cuttingController.ts`、`Report.tsx` 均 `True`） |
| 后端 `tsc --noEmit`（临时副本 + 修改后文件，junction 真实 node_modules） | **通过**（exit 0，无错误） |
| 前端 `tsc --noEmit`（临时副本 + 修改后文件，junction 真实 node_modules） | **通过**（exit 0，无错误） |
| 检查有效性对照（临时注入 `const __sanity__: number = "should_fail"` 后重跑） | 后端/前端 tsc 均按预期报错（TS2322 等），证明上述通过是真实编译而非空跑 |

> 说明：验证使用 TypeScript 5.3.x 编译真实项目 tsconfig（含 `strict`、前端 `noUnusedLocals` 等），改动后文件在完整项目上下文中类型检查零错误。
