# naiwiptrack 调用看板 API 携带站点标识（X-Site-Ref）补丁说明

> 生成日期：2026-08-29
> 交付物：`naiwiptrack_site_ref.patch`（统一 diff 格式，可直接 `git apply`）

## 一、背景

物料领取看板的外部 API（`/api/external/*`）即将强制要求请求头 `X-Site-Ref`
（站点号 `310` / `410` 或公司码），缺失时返回 HTTP 400。

naiwiptrack 后端调用看板 API 的唯一入口是
`backend/src/controllers/cuttingController.ts` 中的 `externalApi` 函数（约 165-191 行），
文件内全部 13 处看板 API 调用都经由该函数发出请求。因此**只修改 `externalApi` 一处**，
即可让所有调用自动携带 `X-Site-Ref` 请求头，无需逐一改动 13 个调用点。

## 二、改动清单

补丁包含 2 个文件、共 **3 个 hunk**：

### 1. `backend/src/controllers/cuttingController.ts`（2 个 hunk）

**hunk 1** —— 常量区（`EXTERNAL_API_KEY` 定义之后，原约 120 行处）新增：

```ts
// 调用看板外部 API 的站点标识（看板侧强制校验 X-Site-Ref），生产环境用环境变量
// EXTERNAL_SITE_REF 配置（如 '410'/'310'），默认 '410'（当前联调站点）。
const EXTERNAL_SITE_REF = process.env.EXTERNAL_SITE_REF || '410';
```

**hunk 2** —— `externalApi` 函数 `headers`（原约 169-172 行，现有 `'X-API-Key'` 与 `'Content-Type'`）新增一行：

```ts
'X-Site-Ref': EXTERNAL_SITE_REF,
```

### 2. `docker-compose.yml`（1 个 hunk）

`backend` 服务 `environment`（YAML 列表结构）中，在 `CSI_USERNAME` 行之后追加：

```yaml
# 看板外部 API 站点标识（310/410），与看板 SITE_CSI_COMPANY 对应
- EXTERNAL_SITE_REF=410
```

## 三、EXTERNAL_SITE_REF 环境变量配置说明

| 场景 | 配置方式 | 说明 |
|------|----------|------|
| Docker Compose | `docker-compose.yml` 已追加 `EXTERNAL_SITE_REF=410` | 生产/联调部署随容器注入 |
| 本地直接运行 | 进程环境变量 `EXTERNAL_SITE_REF`，或在 `.env` 中设置并加载 | 未设置时代码回退默认值 `'410'` |
| 默认值 | `'410'`（当前联调站点） | 与看板 `SITE_CSI_COMPANY` 对应 |

注意：默认值仅用于本地开发兜底；正式环境应显式配置 `410` 或 `310`，避免误用默认站点。

## 四、应用方法

补丁路径前缀为 `backend/...`，对应独立 git 仓库 **`production-tracking`**
（`naiwiptrack` 根目录不是 git 仓库）。在仓库根目录执行：

```bash
cd D:/Workbuddy/naiwiptrack/production-tracking
git apply --check naiwiptrack_site_ref.patch   # 先校验，输出为空即通过
git apply naiwiptrack_site_ref.patch           # 应用
```

说明：patch 头部以 `#` 开头的注释行会被 `git apply` 自动跳过，不影响应用。
如需回退，使用 `git apply -R naiwiptrack_site_ref.patch`。

## 五、验证建议

1. **类型检查**：在 `backend` 目录执行
   ```bash
   cd backend
   npm run build        # 或 npx tsc --noEmit
   ```
   已实际验证：应用补丁后的完整源码执行 `tsc --noEmit` 通过，无错误（exit 0）。

2. **行为验证**：启动后端后调用任一看板接口（如 coils 查询），确认请求头携带 `X-Site-Ref`。
   可用 `curl` 抽查：
   ```bash
   curl -H "X-API-Key: NAI-WIPTRACK-2026" -H "X-Site-Ref: 410" \
     "http://localhost:5000/api/external/coils/..."
   ```

## 六、为什么只改 `externalApi` 一处即可覆盖全部 13 个调用点

以下 13 处看板调用全部经由 `externalApi` 发送请求（行号基于当前文件，`cuttingController.ts`）：

| # | 行号 | 调用 | 用途 |
|---|------|------|------|
| 1 | 321 | `/api/external/coils/...` | 按卷号查询 coil |
| 2 | 536 | `/api/external/cutting-ref?finished_part=...` | 裁线参照查询 |
| 3 | 577 | `/api/external/cutting-ref?finished_part=...` | 裁线参照查询（批量校验场景） |
| 4 | 618 | `/api/external/consumption?job_order=...` | 消耗量查询 |
| 5 | 622 | `/api/external/cutting-check?job_order=...` | 裁线完成校验 |
| 6 | 950 | `/api/external/consumption/scrap` | 报废消耗上报 |
| 7 | 986 | `/api/external/consumption` | 消耗上报 |
| 8 | 1068 | `/api/external/cutting-check` | 裁线完成确认（Complete 场景） |
| 9 | 1132 | `/api/external/consumption?...` | 消耗分页查询 |
| 10 | 1160 | `/api/external/confirm-user` | 用户校验 |
| 11 | 1192 | `/api/external/consumption/{id}` | 删除消耗记录 |
| 12 | 1219 | `/api/external/consumption/list?...` | 消耗列表分页 |
| 13 | 1249 | `/api/external/coils/list?...` | coil 列表分页 |

请求头在 `externalApi` 的 `httpRequest` 调用处统一注入（`headers` 对象），
因此上述 13 处调用无需任何改动即可携带 `X-Site-Ref`。后续新增看板调用若同样经由
`externalApi`，也会自动携带。
