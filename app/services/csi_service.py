"""
Infor CSI IDO API 轻量客户端
用于工单验证、物料验证和库存查询
"""
import time
import threading
import httpx
import logging
from urllib.parse import urlencode
from app.config import Config

logger = logging.getLogger(__name__)

# clear_job_matl_backflush 的返回说明（同步器据 BF_MSG_CLEARED 判定「本次确实把 1 写成了 0」）
BF_MSG_CLEARED = 'Backflush 已置为 0'
BF_MSG_ALREADY_ZERO = 'Backflush 已为 0，无需修改'

# 所有 CSI / IDO 请求共用的 httpx 参数。
# trust_env=False：忽略 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 等环境代理——
# 宝鼎/CSI 都是内网主机，被塞进本机代理（如已关闭的 127.0.0.1:58229）
# 会直接 ProxyError，整条链路不可用。verify=False 维持现状（内网自签证书）。
_HTTPX_KW = {'verify': False, 'trust_env': False}

# ---------------------------------------------------------------------- #
#  进程级 token 缓存（跨 CSIClient 实例共享）
#  背景：每次请求都新建 CSIClient，实例级缓存形同虚设，导致每个请求都走一次
#  OAuth2 password grant（实测 1-3 秒）。改为模块级缓存后只有首个请求付这笔开销。
#  key = (token_url, username, company)：company 参与 key，使不同 siteref 各持一份，
#  互不影响（token 本身是租户级，company 通过 X-Infor-MongooseConfig 头传递）。
# ---------------------------------------------------------------------- #
_TOKEN_CACHE: dict = {}
_TOKEN_LOCK = threading.Lock()
_TOKEN_REFRESH_MARGIN = 60  # 提前 60 秒视为过期，避免用到临界 token


def _clear_token_cache() -> None:
    """清空进程级 token 缓存（供测试使用）"""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def _to_bool(value) -> bool:
    """IDO 布尔字段统一判定：'1'/'0'/1/0/True/'true'/'Y' → bool"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'y', 'yes')
    return False


def is_usable_source_loc(loc, non_netable=None) -> bool:
    """判断库位能否作为卷标库位 / 移库源库位（from_location）。

    排除两类：
      1) floor* 车间在制库位（Floor-Core / FLoor-Core / FLOOR-X000 等，大小写不敏感）：
         车间的料已经从仓库领出，不能再作为仓库发料/移库的源库位；
      2) Non-Nettable 库位（不参与净值化的虚拟/过渡库位，库存不能发料）。

    CSI 侧 Non-Nettable 就是用 **MrbFlag** 表达的（业务确认：MrbFlag=1 即非净值库位）。
    ue_NAI_SLLotLocs / SLItemLocs 没有独立的 NonNetable 字段，故本函数用 non_netable
    形参接收 MrbFlag 的值；FLOOR-X000 的 MrbFlag=1，且 DerTotalNonNettableStock
    恰好等于该行 QtyOnHand。

    Args:
        loc: 库位编码，允许 None / 空串 / 带空格
        non_netable: Non-Nettable 标记，即 CSI 的 MrbFlag（1/'1'/True/'true' 视为非净值）

    Returns:
        True 才允许作为源库位；False 表示必须排除
    """
    code = str(loc or '').strip()
    if not code:
        return False
    if 'floor' in code.lower():
        return False
    if _to_bool(non_netable):
        return False
    return True


class CSIClient:
    """
    轻量版 Infor CSI / IDO Request Service 客户端（OAuth2 认证）。
    支持：
      1. 标准 CSI API（MMS005 等）-> GET {CSI_API_BASE}/<TABLE>?CONO=...&ITNO=...
      2. IDO Request Service（ue_NAI_* 等）-> GET ido/load/<IDO_NAME>?properties=...&filter=...
    """

    def __init__(self, siteref: str = '310'):
        self._siteref = siteref
        self._tenant = Config.CSI_TENANT
        self._company = Config.SITE_CSI_COMPANY.get(siteref, f'{Config.CSI_TENANT}_{siteref}')
        self._whse = Config.SITE_CSI_WHSE.get(siteref, '')
        self._token_url = Config.CSI_TOKEN_URL
        self._api_base = Config.CSI_API_BASE.rstrip('/')
        self._ido_base = Config.CSI_IDO_BASE.rstrip('/')
        self._basic_auth = f"Basic {Config.CSI_AUTH_BASIC}"

        # Token 缓存（实例字段仅作镜像，权威缓存是模块级 _TOKEN_CACHE）
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  认证
    # ------------------------------------------------------------------ #
    def _token_key(self) -> tuple:
        """进程级缓存 key：同一租户账号 + 同一 company 复用同一个 token"""
        return (self._token_url, Config.CSI_USERNAME, self._company)

    def _fetch_token(self) -> tuple[str, int]:
        """向 OAuth2 端点请求 access_token，成功后写回进程级缓存"""
        resp = httpx.post(
            self._token_url,
            headers={
                "Authorization": self._basic_auth,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "password",
                "username": Config.CSI_USERNAME,
                "password": Config.CSI_PASSWORD,
            },
            timeout=30,
            **_HTTPX_KW,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        exp = time.monotonic() + expires_in
        _TOKEN_CACHE[self._token_key()] = {'token': token, 'exp': exp}
        # 实例字段同步，兼容其他地方直接读取 self._token 的写法
        self._token = token
        self._token_expires_at = exp
        return token, expires_in

    def _ensure_token(self, force: bool = False) -> str:
        """获取有效 token：先读进程级缓存，未命中/临期才取（加锁双重检查）

        force=True 用于 401 刷新：跳过命中判断，强制重取并回写缓存。
        """
        key = self._token_key()

        if not force:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached['exp'] - time.monotonic() > _TOKEN_REFRESH_MARGIN:
                self._token = cached['token']
                return cached['token']

        with _TOKEN_LOCK:
            if not force:
                # 双重检查：等锁期间其他线程可能已经取到新 token
                cached = _TOKEN_CACHE.get(key)
                if cached and cached['exp'] - time.monotonic() > _TOKEN_REFRESH_MARGIN:
                    self._token = cached['token']
                    return cached['token']
            token, _expires_in = self._fetch_token()
            return token

    # ------------------------------------------------------------------ #
    #  IDO Request Service 调用（ue_NAI_* 等自定义 IDO）
    # ------------------------------------------------------------------ #
    def _get_ido(self, ido_name: str, properties: list[str] | None = None,
                 filter_str: str | None = None, retry_on_timeout: bool = True) -> list:
        """调用 IDO Request Service，返回记录列表"""
        url = f"{self._ido_base}/ido/load/{ido_name}"
        params = {}
        if properties:
            params["properties"] = ",".join(properties)
        if filter_str:
            params["filter"] = filter_str

        token = self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Infor-MongooseConfig": self._company,
        }

        for attempt in range(2 if retry_on_timeout else 1):
            try:
                resp = httpx.get(url, headers=headers, params=params, timeout=60, **_HTTPX_KW)
                if resp.status_code == 401:
                    logger.info(f"[IDO] 401 token expired, refreshing...")
                    token = self._ensure_token(force=True)
                    headers["Authorization"] = f"Bearer {token}"
                    resp = httpx.get(url, headers=headers, params=params, timeout=60, **_HTTPX_KW)
                resp.raise_for_status()
                body = resp.json()
                # IDO 查询失败（IDO 名不存在 / 属性名写错等）也返回 HTTP 200 + Success=false，
                # 若只看 Items 会被当成「查无数据」静默吞掉，故显式留痕。
                if body.get('Success') is False:
                    logger.warning('[IDO] %s 查询失败：%s（filter=%s）',
                                   ido_name, body.get('Message'), filter_str)
                records = body.get("Items") or body.get("value") or body.get("records") or []
                return records if isinstance(records, list) else [records]
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt == 0 and retry_on_timeout:
                    logger.warning(f"[IDO] 超时/连接失败，重试一次: {e}")
                    continue
                raise
        return []

    # ------------------------------------------------------------------ #
    #  标准 CSI API 调用（MMS005 等）
    # ------------------------------------------------------------------ #
    def _get_csi(self, table: str, params: dict | None = None,
                  site: str | None = None) -> list:
        """调用标准 CSI API，返回记录列表（支持分页）
        
        site: X-Infor-MongooseConfig header 的值，用于多站点上下文
        """
        url = f"{self._api_base}/{table}"
        params = dict(params) if params else {}
        params.setdefault("returnas", "json")
        params.setdefault("maxrecs", 1000)

        results = []
        page = 1
        while True:
            p = dict(params, page=page)
            token = self._ensure_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if site:
                headers["X-Infor-MongooseConfig"] = site
            resp = httpx.get(url, headers=headers, params=p, timeout=30, **_HTTPX_KW)
            if resp.status_code == 401:
                token = self._ensure_token(force=True)
                headers["Authorization"] = f"Bearer {token}"
                resp = httpx.get(url, headers=headers, params=p, timeout=30, **_HTTPX_KW)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("rows") or data.get("MvxRecord") or []
            results.extend(rows)
            if len(rows) < params.get("maxrecs", 1000):
                break
            page += 1
        return results

    # ------------------------------------------------------------------ #
    #  公开 API：工单验证、物料验证、库存查询
    # ------------------------------------------------------------------ #
    def validate_job(self, job: str, suffix: int) -> dict | None:
        """
        工单验证 - 查询 ue_NAI_Jobs
        返回: {Job, Suffix, Item, Stat, Description} 或 None
        """
        try:
            filter_str = f"Job = N'{job}' And Suffix = {suffix}"
            # 某些环境 Suffix 需要字符串格式，尝试 N'0' 格式
            alt_filter_str = f"Job = N'{job}' And Suffix = N'{suffix}'"
            props = ["Job", "Suffix", "Item", "Stat", "Description"]
            records = self._get_ido("ue_NAI_Jobs", properties=props, filter_str=filter_str)
            if not records:
                records = self._get_ido("ue_NAI_Jobs", properties=props, filter_str=alt_filter_str)
            if records:
                return records[0]
            return None
        except Exception as e:
            logger.error(f"[CSI] validate_job error: {e}")
            return None

    def validate_material(self, job: str, suffix: int, item: str) -> dict | None:
        """
        物料验证 - 查询 ue_NAI_SLJobmatls
        确认物料在工单BOM中且 Backflush=0
        返回: dict 或 None
        """
        try:
            filter_str = (
                f"Job = N'{job}' And Suffix = {suffix} "
                f"And Item = N'{item}' And Backflush = 0"
            )
            props = ["Job", "Suffix", "Item", "Backflush"]
            records = self._get_ido("ue_NAI_SLJobmatls", properties=props, filter_str=filter_str)
            if records:
                return records[0]
            return None
        except Exception as e:
            logger.error(f"[CSI] validate_material error: {e}")
            return None

    def get_inventory(self, item: str) -> list[dict]:
        """
        库存查询 - 调用 IDO SLItemLocs（物料/库位余额表）
        通过 IDO 方式查询，按 Item 过滤，仅返回 QtyOnHand > 0 的库位
        返回: [{Item, Loc, QtyOnHand}, ...]
        """
        try:
            filter_str = f"Item = N'{item}' And QtyOnHand > 0"
            props = ["Item", "Loc", "QtyOnHand"]
            records = self._get_ido("SLItemLocs", properties=props, filter_str=filter_str)
            result = []
            for r in records:
                qty = float(r.get("QtyOnHand", 0) or 0)
                if qty <= 0:
                    continue
                result.append({
                    "Item": r.get("Item", item),
                    "Loc": r.get("Loc", ""),
                    "QtyOnHand": qty,
                })
            return result
        except Exception as e:
            logger.error(f"[CSI] get_inventory error: {e}")
            return []

    def get_item_cost(self, item: str) -> float | None:
        """
        获取物料单价 - 调用 IDO ue_GDL_SLItems
        返回 DerUnitCost（单位成本），找不到返回 None
        """
        try:
            filter_str = f"Item = N'{item}'"
            props = ["Item", "DerUnitCost"]
            records = self._get_ido("ue_GDL_SLItems", properties=props, filter_str=filter_str)
            if records:
                cost = records[0].get("DerUnitCost")
                return float(cost) if cost else None
            return None
        except Exception as e:
            logger.error(f"[CSI] get_item_cost error: {e}")
            return None

    def get_item_backflush(self, item: str) -> bool | None:
        """
        查询物料的 Backflush 标记
        返回 True=已打勾(自动发料), False=未打勾(需手动领料), None=查不到
        """
        try:
            filter_str = f"Item = N'{item}'"
            props = ["Item", "Backflush"]
            records = self._get_ido("ue_GDL_SLItems", properties=props, filter_str=filter_str)
            if records:
                bf = records[0].get("Backflush")
                return str(bf).strip() == "1"
            return None
        except Exception as e:
            logger.error(f"[CSI] get_item_backflush error: {e}")
            return None

    def get_job_matl_backflush(self, job: str, suffix, item: str) -> dict | None:
        """
        查询工单物料（SLJobMatls）的 Backflush 标记——工单物料级，不是物料主档级。
        返回 {'backflush': bool, 'item_id': _ItemId} ；查不到/异常返回 None。
        """
        try:
            suffix_val = int(suffix) if suffix not in (None, '') else 0
        except (TypeError, ValueError):
            return None
        try:
            filter_str = (
                f"Job = N'{job}' And Suffix = {suffix_val} And Item = N'{item}'"
            )
            props = ["Job", "Suffix", "Item", "OperNum", "Sequence", "Backflush"]
            records = self._get_ido("SLJobMatls", properties=props, filter_str=filter_str)
            if not records:
                return None
            row = records[0]
            return {
                'backflush': _to_bool(row.get('Backflush')),
                'item_id': row.get('_ItemId'),
                # SLJobMatls 主键为 Job+Suffix+Item+OperNum+Sequence，update 必须全带
                'oper_num': row.get('OperNum'),
                'sequence': row.get('Sequence'),
            }
        except Exception as e:
            logger.error(f"[CSI] get_job_matl_backflush error job={job} suffix={suffix} item={item}: {e}")
            return None

    def clear_job_matl_backflush(self, job: str, suffix, item: str) -> tuple[bool, str]:
        """
        取消工单物料的 Backflush（倒冲）标记，改为手动发料。
        返回 (是否可继续发料, 说明)。已为 0 也算成功（不写库）。
        """
        try:
            suffix_val = int(suffix) if suffix not in (None, '') else 0
        except (TypeError, ValueError):
            suffix_val = 0
        tag = f"{job}-{suffix_val:04d}"

        info = self.get_job_matl_backflush(job, suffix_val, item)
        if info is None:
            msg = f'未找到工单物料 {tag} / {item}（或查询失败）'
            logger.warning(f"[CSI] 取消 Backflush 失败：{msg}")
            return False, msg

        if not info.get('backflush'):
            logger.info(f"[CSI] 工单 {tag} 物料 {item} Backflush 已为 0，无需修改")
            return True, BF_MSG_ALREADY_ZERO

        item_id = info.get('item_id')
        if not item_id:
            msg = f'工单物料 {tag} / {item} 缺少 _ItemId，无法更新'
            logger.error(f"[CSI] 取消 Backflush 失败：{msg}")
            return False, msg

        # 主键必须完整（Job+Suffix+Item+OperNum+Sequence），否则 IDO 返回 Success:false：
        # "Key property [OperNum]/[Sequence] was not included in the update request"
        oper_raw = info.get('oper_num')
        seq_raw = info.get('sequence')
        if oper_raw in (None, '') or seq_raw in (None, ''):
            msg = f'工单物料 {tag} / {item} 缺少 OperNum/Sequence，无法更新'
            logger.error(f"[CSI] 取消 Backflush 失败：{msg}")
            return False, msg

        def _as_int(v):
            try:
                return int(str(v).strip())
            except (TypeError, ValueError):
                return v

        url = f"{self._ido_base}/ido/update/SLJobMatls"
        payload = {"Changes": [{
            "Action": 2,
            "_ItemId": item_id,
            "Properties": [
                {"IsNull": False, "Modified": True, "Name": "Backflush", "Value": 0},
                {"IsNull": False, "Modified": False, "Name": "Job", "Value": job},
                {"IsNull": False, "Modified": False, "Name": "Suffix", "Value": suffix_val},
                {"IsNull": False, "Modified": False, "Name": "Item", "Value": item},
                {"IsNull": False, "Modified": False, "Name": "OperNum", "Value": _as_int(oper_raw)},
                {"IsNull": False, "Modified": False, "Name": "Sequence", "Value": _as_int(seq_raw)},
            ],
            "UpdateLocking": 1,
        }]}
        params = {"refresh": "true"}

        try:
            token = self._ensure_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Infor-MongooseConfig": self._company,
            }
            resp = httpx.post(url, headers=headers, params=params,
                              json=payload, timeout=60, **_HTTPX_KW)
            if resp.status_code == 401:
                logger.info("[CSI] 401 token expired, refreshing...")
                token = self._ensure_token(force=True)
                headers["Authorization"] = f"Bearer {token}"
                resp = httpx.post(url, headers=headers, params=params,
                                  json=payload, timeout=60, **_HTTPX_KW)
            resp.raise_for_status()
        except Exception as e:
            msg = f'更新工单物料 {tag} / {item} 失败：{e}'
            logger.error(f"[CSI] 取消 Backflush 失败：{msg}")
            return False, msg

        # IDO 常见：HTTP 200 但 body {"Success": false, "Message": "..."}，必须校验
        try:
            body = resp.json() or {}
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get('Success') is False:
            msg = f'更新工单物料 {tag} / {item} 被 IDO 拒绝：{body.get("Message") or "未知原因"}'
            logger.error(f"[CSI] 取消 Backflush 失败：{msg}")
            return False, msg

        logger.info(f"[CSI] 工单 {tag} 物料 {item} Backflush 1->0（已取消倒冲）")
        return True, BF_MSG_CLEARED

    def get_item_unit(self, item: str) -> str | None:
        """
        获取物料单位 - 查询 IDO ue_GDL_SLItems 的 UM 字段
        返回单位字符串（如 'M'/'FT'/'EA'），查不到返回 None

        注意：实测带 properties 参数查询 ue_GDL_SLItems 会返回 0 条
        （IDO properties 过滤不兼容），因此这里**不带 properties 全字段查询**，
        从返回记录中取 UM/Uom/UnitOfMeasure 字段。
        """
        try:
            filter_str = f"Item = N'{item}'"
            records = self._get_ido("ue_GDL_SLItems", filter_str=filter_str)
            if records:
                for key in ("UM", "Uom", "UnitOfMeasure"):
                    um = records[0].get(key)
                    if um:
                        return str(um).strip()
            return None
        except Exception as e:
            logger.error(f"[CSI] get_item_unit error: {e}")
            return None

    def _lot_loc_from_balances(self, item: str, lot: str) -> str | None:
        """查询 ue_NAI_SLLotLocs 中该 Lot 有余额的库位（取数量最大者）

        排除 Non-Nettable / MRB 库位（MrbFlag = 1，如 FG-RMA、MRB、FLOOR-X*、RM-X*）：
        这类库位的库存不能发料，不能作为卷标带出的库位。
        MrbFlag 可能为 NULL，故用 (MrbFlag IS NULL Or MrbFlag <> 1)。

        同时排除车间线边（Floor-Core / FLoor-Xxxx 等）库位：这类库位的货已经
        从仓库领到车间，不能作为「发料来源库位」带出（卷标录入库位不得为 floor）。
        CSI 侧字面值是 'FLoor-Core'（大小写混排），故用 'floor' 子串判断，
        不能做精确比较。全部有货库位都是 floor 时返回 None。

        过滤统一走 is_usable_source_loc：floor* 与非净值（MrbFlag=1）都排除。
        ue_NAI_SLLotLocs 没有 NonNetable 字段，用 MrbFlag 作为等价标记
        （实测 FLOOR-X000 MrbFlag=1，DerTotalNonNettableStock 等于该行数量）。
        """
        try:
            filter_str = (f"Item = N'{item}' And Lot = N'{lot}' And QtyOnHand > 0 "
                          f"And (MrbFlag IS NULL Or MrbFlag <> 1)")
            records = self._get_ido("ue_NAI_SLLotLocs", filter_str=filter_str)
            best_qty, best_loc = None, None
            for r in records:
                qty = float(r.get("QtyOnHand", 0) or 0)
                loc = str(r.get("Loc", "") or "").strip()
                if qty <= 0 or not loc:
                    continue
                if not is_usable_source_loc(loc, r.get("MrbFlag")):
                    continue
                if best_qty is None or qty > best_qty:
                    best_qty, best_loc = qty, loc
            return best_loc
        except Exception as e:
            logger.error(f"[CSI] _lot_loc_from_balances error: {e}")
            return None

    def _loc_mrb_flag(self, item: str, lot: str, loc: str) -> bool | None:
        """查 ue_NAI_SLLotLocs 中 Item/Lot/Loc 该行的 MrbFlag（Non-Nettable 标记）

        返回 True=MRB 库位、False=正常库位、None=查不到该行（无法确认，调用方不应采用）。
        MrbFlag 返回值为字符串 '0'/'1'。
        """
        try:
            filter_str = f"Item = N'{item}' And Lot = N'{lot}' And Loc = N'{loc}'"
            records = self._get_ido("ue_NAI_SLLotLocs",
                                    properties=["Loc", "MrbFlag"], filter_str=filter_str)
            if not records:
                return None
            return any(str(r.get("MrbFlag") or "").strip() == "1" for r in records)
        except Exception as e:
            logger.error(f"[CSI] _loc_mrb_flag error: {e}")
            return None

    def _lot_has_mrb_stock(self, item: str, lot: str) -> bool:
        """该 Lot 是否只在 Non-Nettable/MRB 库位（MrbFlag = 1）有余额"""
        try:
            filter_str = (f"Item = N'{item}' And Lot = N'{lot}' And QtyOnHand > 0 "
                          f"And MrbFlag = 1")
            records = self._get_ido("ue_NAI_SLLotLocs",
                                    properties=["Loc", "MrbFlag"], filter_str=filter_str)
            return bool(records)
        except Exception as e:
            logger.error(f"[CSI] _lot_has_mrb_stock error: {e}")
            return False

    def get_lot_info(self, item: str, lot: str) -> dict | None:
        """
        查询 SLLots（按 Item 过滤验证 Lot 号），返回 DerQtyOnHand（衍生可用数量）。
        用于卷标录入时验证 Lot 与长度：长度（mm）不得超过 DerQtyOnHand（×换算系数 → mm）。
        注意：SLLots 与 ue_GDL_SLItems 一样，带 properties 参数查询返回 0 条，
        因此这里**不带 properties 全字段查询**，从返回记录中取 DerQtyOnHand/QtyOnHand 等字段。
        loc 取值顺序（排除 Non-Nettable / MRB 库位、排除车间线边 Floor 库位）：
          1) ue_NAI_SLLotLocs 有余额且 MrbFlag <> 1 且非 floor 的库位（取数量最大者）
          2) SLLots.Loc —— 仅当该库位在 ue_NAI_SLLotLocs 中存在、MrbFlag <> 1 且非 floor 时才采用
          3) 空字符串（SLLots.Loc 为 MRB / floor 库位或查不到该行时不采用）
        数量字段（der_qty_on_hand / qty_on_hand）与库位过滤无关，保持原逻辑。
        返回: {item, lot, der_qty_on_hand, qty_on_hand, lot_status, whse, loc,
               loc_source('lotlocs'|'slLots'|''), loc_excluded_mrb(bool)}
               或 None（Lot 不存在/查询失败）
        """
        try:
            filter_str = f"Item = N'{item}' And Lot = N'{lot}'"
            records = self._get_ido("SLLots", filter_str=filter_str)
            if not records:
                return None
            r = records[0]
            loc = self._lot_loc_from_balances(item, lot) or ''
            loc_source = 'lotlocs' if loc else ''
            loc_excluded_mrb = False
            if not loc:
                sl_loc = str(r.get('Loc', '') or '').strip()
                # SLLots.Loc 也可能是车间线边（floor）/ 非净值库位，同样不能作为发料库位带出
                if sl_loc and not is_usable_source_loc(sl_loc):
                    sl_loc = ''
                if sl_loc:
                    mrb = self._loc_mrb_flag(item, lot, sl_loc)
                    if mrb is False:
                        loc, loc_source = sl_loc, 'slLots'
                    elif mrb:
                        loc_excluded_mrb = True
            if not loc and not loc_excluded_mrb:
                # SLLots.Loc 也为空：该 Lot 若只在 MRB 库位有货，同样视为「因 MRB 被排除」
                loc_excluded_mrb = self._lot_has_mrb_stock(item, lot)
            return {
                'item': r.get('Item', item),
                'lot': r.get('Lot', lot),
                'der_qty_on_hand': float(r.get('DerQtyOnHand', 0) or 0),
                'qty_on_hand': float(r.get('QtyOnHand', 0) or 0),
                'lot_status': str(r.get('LotStatus', '') or ''),
                'whse': str(r.get('Whse', '') or ''),
                'loc': loc,
                'loc_source': loc_source,
                'loc_excluded_mrb': loc_excluded_mrb,
            }
        except Exception as e:
            logger.error(f"[CSI] get_lot_info error: {e}")
            return None

    def get_lot_location(self, item: str, lot: str) -> str | None:
        """
        查询 Lot 当前所在库位（Loc），供 CSI 库位转移同步器补 from_location。
        优先取 ue_NAI_SLLotLocs 有余额且非 MRB 的库位；无则回退 SLLots.Loc（同样校验非 MRB）。
        库位全部为 floor*（车间在制）或 Non-Nettable 时返回 None —— 绝不把这类库位
        当作仓库源库位返回，上层据此显示「未找到可用库位」，而不是拿车间库位去移库。
        """
        info = self.get_lot_info(item, lot)
        fallback = info.get('loc') if info else None
        raw_loc = fallback or self._lot_loc_from_balances(item, lot)
        loc = str(raw_loc or '').strip()
        if is_usable_source_loc(loc):
            return loc
        if loc:
            # 原始库位存在但不可用：留痕，便于排查「为什么查不到库位」
            logger.warning(
                '[CSI] Lot 无可用源库位（floor*/Non-Nettable 已排除）item=%s lot=%s '
                'raw_loc=%s loc_source=%s mrb_excluded=%s',
                item, lot, loc, (info or {}).get('loc_source') or '',
                (info or {}).get('loc_excluded_mrb')
            )
        return None

    def get_lot_floor_core_qty(self, item: str, lot: str) -> dict | None:
        """查某物料某批号在 Floor-Core（车间线边库位）的剩余库存。

        返回 {'qty': float, 'um': str|None, 'whse': str|None}；查不到返回 None。
        数据源：IDO 表 ue_NAI_SLLotLocs，
        filter: Item = N'..' And Lot = N'..' And Loc = N'Floor-Core'
        （Loc 大小写不敏感，实测可命中 CSI 侧的字面值 'FLoor-Core'）。
        异常一律 catch 返回 None（不让 CSI 查询失败阻断同步）。
        """
        try:
            item = (item or '').strip()
            lot = (lot or '').strip()
            if not item or not lot:
                return None
            filter_str = (
                f"Item = N'{item}' And Lot = N'{lot}' And Loc = N'Floor-Core' "
                f"And (MrbFlag IS NULL Or MrbFlag <> 1)"
            )
            props = ["Item", "Lot", "Loc", "QtyOnHand", "ItemUM", "Whse"]
            records = self._get_ido("ue_NAI_SLLotLocs", properties=props, filter_str=filter_str)
            for r in records:
                # CSI 里 Floor-Core 的字面值大小写与 Config.CSI_TO_LOCATION 不一致
                # （实测 'FLoor-Core'），故用子串判断归一化，不做精确字符串比较
                loc = str(r.get("Loc") or "").strip()
                if "floor" not in loc.lower():
                    continue
                qty_raw = r.get("QtyOnHand")
                try:
                    qty = float(qty_raw)
                except (TypeError, ValueError):
                    continue
                return {
                    "qty": qty,
                    "um": str(r.get("ItemUM") or "").strip() or None,
                    "whse": str(r.get("Whse") or "").strip() or None,
                }
            return None
        except Exception as e:
            logger.error(f"[CSI] get_lot_floor_core_qty error item={item} lot={lot}: {e}")
            return None

    def get_lot_loc_qty(self, item: str, lot: str, loc: str | None = None) -> dict | None:
        """查某物料某批号在指定库位（loc 为空则汇总全部库位）的非 MRB 存货。

        数据源 ue_NAI_SLLotLocs（批次级余额表）。注意 SLLots.QtyOnHand 在该 IDO 里
        根本不返回（实测 get_lot_info 的 qty_on_hand 恒为 0），DerQtyOnHand 又是含
        MRB/非合格库位的合计，都不能用来判断「本笔提交会不会超货位/批次存货」。

        过滤：
          - MrbFlag <> 1（Non-Nettable / MRB 库位的货不能发料）
          - 指定 loc 时只取该库位（大小写不敏感等值，CSI 侧字面值有 'FLoor-Core'）
        返回 {'qty': float, 'um': str|None, 'whse': str|None, 'loc': str|None}；
        没有匹配行返回 None；异常一律 catch 返回 None（不阻断同步）。
        """
        try:
            item = (item or '').strip()
            lot = (lot or '').strip()
            if not item or not lot:
                return None
            want_loc = (loc or '').strip().lower()
            filter_str = (f"Item = N'{item}' And Lot = N'{lot}' "
                          f"And (MrbFlag IS NULL Or MrbFlag <> 1)")
            props = ["Item", "Lot", "Loc", "QtyOnHand", "ItemUM", "Whse", "MrbFlag"]
            records = self._get_ido("ue_NAI_SLLotLocs", properties=props, filter_str=filter_str)
            total = 0.0
            um, whse, hit_loc = None, None, None
            matched = False
            for r in records:
                r_loc = str(r.get("Loc") or "").strip()
                if want_loc and r_loc.lower() != want_loc:
                    continue
                try:
                    total += float(r.get("QtyOnHand") or 0)
                except (TypeError, ValueError):
                    continue
                matched = True
                um = um or (str(r.get("ItemUM") or "").strip() or None)
                whse = whse or (str(r.get("Whse") or "").strip() or None)
                hit_loc = hit_loc or (r_loc or None)
            if not matched:
                return None
            return {'qty': total, 'um': um, 'whse': whse, 'loc': hit_loc}
        except Exception as e:
            logger.error(f"[CSI] get_lot_loc_qty error item={item} lot={lot} loc={loc}: {e}")
            return None

    def get_item_lots(self, item: str, exclude_floor: bool = True) -> list[dict]:
        """
        查询物料批号库存（ue_NAI_SLLotLocs），按 FIFO 排序
        排除 floor/floor-core 库位，以及 Non-Nettable/MRB 库位（MrbFlag = 1）
        返回: [{lot, loc, qty_on_hand, create_date}, ...]
        """
        try:
            filter_str = (f"Item = N'{item}' AND QtyOnHand > 0 "
                          f"AND (MrbFlag IS NULL OR MrbFlag <> 1)")
            props = ["Item", "Loc", "Lot", "QtyOnHand", "WBLotCreateDate", "Whse"]
            records = self._get_ido("ue_NAI_SLLotLocs", properties=props, filter_str=filter_str)
            if not records:
                return []

            result = []
            for r in records:
                loc = (r.get("Loc") or "").lower()
                if exclude_floor and ("floor" in loc or "floor-core" in loc):
                    continue
                qty = float(r.get("QtyOnHand", 0))
                if qty <= 0:
                    continue
                result.append({
                    "lot": r.get("Lot", ""),
                    "loc": r.get("Loc", ""),
                    "qty_on_hand": qty,
                    "create_date": r.get("WBLotCreateDate", ""),
                    "whse": r.get("Whse", ""),
                })

            # FIFO: 按创建时间升序排列
            result.sort(key=lambda x: x["create_date"] or "")
            return result
        except Exception as e:
            logger.error(f"[CSI] get_item_lots error: {e}")
            return []


def parse_job(job_full: str) -> tuple:
    """
    解析完整工单号 J000002124-0004 或 ENG0002124-0001
    返回 (job, suffix) 元组
    """
    import re
    pattern = r'^([A-Za-z]+\d+)-(\d+)$'
    m = re.match(pattern, job_full.strip())
    if not m:
        return None, None
    job = m.group(1)
    suffix = int(m.group(2))
    return job, suffix
