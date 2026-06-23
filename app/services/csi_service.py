"""
Infor CSI IDO API 轻量客户端
用于工单验证、物料验证和库存查询
"""
import time
import httpx
import logging
from urllib.parse import urlencode
from app.config import Config

logger = logging.getLogger(__name__)


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

        # Token 缓存
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  认证
    # ------------------------------------------------------------------ #
    def _fetch_token(self) -> tuple[str, int]:
        """向 OAuth2 端点请求 access_token"""
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
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
        return body["access_token"], int(body.get("expires_in", 3600))

    def _ensure_token(self) -> str:
        """获取有效 token（缓存 + 自动刷新）"""
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        token, expires_in = self._fetch_token()
        self._token = token
        self._token_expires_at = time.time() + expires_in
        return token

    # ------------------------------------------------------------------ #
    #  IDO Request Service 调用（ue_NAI_* 等自定义 IDO）
    # ------------------------------------------------------------------ #
    def _get_ido(self, ido_name: str, properties: list[str] | None = None,
                 filter_str: str | None = None) -> list:
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

        resp = httpx.get(url, headers=headers, params=params, timeout=30, verify=False)
        if resp.status_code == 401:
            # token 失效，刷新后重试一次
            token = self._fetch_token()[0]
            self._token = token
            headers["Authorization"] = f"Bearer {token}"
            resp = httpx.get(url, headers=headers, params=params, timeout=30, verify=False)
        resp.raise_for_status()
        body = resp.json()
        # 兼容不同返回格式
        records = body.get("Items") or body.get("value") or body.get("records") or []
        return records if isinstance(records, list) else [records]

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
            resp = httpx.get(url, headers=headers, params=p, timeout=30, verify=False)
            if resp.status_code == 401:
                token = self._fetch_token()[0]
                self._token = token
                headers["Authorization"] = f"Bearer {token}"
                resp = httpx.get(url, headers=headers, params=p, timeout=30, verify=False)
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
            props = ["Job", "Suffix", "Item", "Stat", "Description"]
            records = self._get_ido("ue_NAI_Jobs", properties=props, filter_str=filter_str)
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

    def get_item_lots(self, item: str, exclude_floor: bool = True) -> list[dict]:
        """
        查询物料批号库存（ue_NAI_SLLotLocs），按 FIFO 排序
        排除 floor/floor-core 库位
        返回: [{lot, loc, qty_on_hand, create_date}, ...]
        """
        try:
            filter_str = f"Item = N'{item}' AND QtyOnHand > 0"
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
    解析完整工单号 J000002124-0004
    返回 (job, suffix) 元组
    """
    import re
    pattern = r'^([A-Za-z]\d+)-(\d+)$'
    m = re.match(pattern, job_full.strip())
    if not m:
        return None, None
    job = m.group(1)
    suffix = int(m.group(2))
    return job, suffix
