"""CSI（宝鼎 Syteline）交易同步器

职责：扫描接口表 kr_csi_interface 的待处理行 → 按行的 api_name 提交对应接口 → 轮询终态；
平台判定失败即转人工（不再自动重试），并发邮件告警；
只有用户在看板点「重新发送」（重置为 PENDING / is_sync=0）后，同步器才会再次提交。

支持的接口（按行 api_name 分派，payload 契约不同）：
  sl-qty-move      库位转移（领用/退料），字段 fromLocation/toLocation/reasonCode
  sl-misc-receipt  杂项收入（盘盈），字段 location/reasonCode/account/lot
  sl-misc-issue    杂项发出（盘亏），字段同上
  sl-job-issue     工单发料（外部系统提交），字段 job/suffix/location，无 reasonCode/account

工单发料前置条件：工单物料若勾了 Backflush（倒冲=1），先经 IDO 改成 0 再提交，
清不掉则按失败流程重试/转人工（不清 0 手工发料会与倒冲冲突）。

运行方式：
  - 常驻：CSI_SYNC_ENABLED=true 时由 create_app() 启动后台守护线程（见 app/__init__.py）
  - 单轮调试：python -m app.csi_sync_worker
  - 常驻调试：docker exec <container> python -c "from app.csi_sync_worker import main; main()"

状态机（is_sync / status）：
  0 / PENDING       待提交（新入队或重试待重投）
  0 / SUBMITTING    本进程已抢占、正在提交/轮询
  0 / QUEUED|PENDING 平台已入队、未到终态（下一轮同 traceId 继续轮询）
  1 / SUCCESS       成功（不再处理）
  2 / FAILED_MANUAL 提交失败，已发邮件转人工（不再自动重试；仅「重新发送」可复活）
  2 / NOOP          物料已在目标库位（fromLocation == toLocation），不发请求、不告警

库位口径：库位一律取接口表行内值（生成时写入的批次/卷标库位），不做实时校正。
  同步器不再调用 CSI 实时库位查询（get_lot_location）纠正或兜底库位：行内
  from_location / to_location 为空即判失败转人工，由人工核对接口表数据后重投。
  原因：同一 Lot 可能同时在车间与仓库两个库位有量，实时查询只返回其中一处，
  覆盖行内值会把 from 改成 to 从而误判 NOOP（id=51 的失败原因），也会与生成端
  「以批次库位为准」的判定口径不一致。

并发安全：多进程/多线程可能同时扫描到同一行，靠 claim() 的条件 UPDATE
（WHERE status IN (...) AND is_sync=0）保证只有 rowcount==1 的一方处理。
"""
import html
import logging
import time
import uuid

import requests

from app.config import Config
from app.models import get_db
from app.services.csi_service import BF_MSG_CLEARED, CSIClient
from app.services.email_service import send_email

logger = logging.getLogger(__name__)

API_QTY_MOVE = 'sl-qty-move'
API_MISC_RECEIPT = 'sl-misc-receipt'   # 杂项收入（盘盈）
API_MISC_ISSUE = 'sl-misc-issue'       # 杂项发出（盘亏）
API_JOB_ISSUE = 'sl-job-issue'         # 工单发料
MISC_APIS = (API_MISC_RECEIPT, API_MISC_ISSUE)
SUPPORTED_APIS = (API_QTY_MOVE, API_MISC_RECEIPT, API_MISC_ISSUE, API_JOB_ISSUE)

# 可被抢占处理的状态（PENDING=新行/待重试，QUEUED=平台已入队，SUBMITTING=上次中断）
CLAIMABLE_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING')
MAX_ERR_LEN = 255  # errormessage 列 VARCHAR(255)

# 物料已在目标库位：不发请求、不发邮件（is_sync=2 已排除在扫描之外）
NOOP_STATUS = 'NOOP'
NOOP_MESSAGE = '物料已在目标库位，无需移库'
# 单轮最多查询的不重复 (item, lot) 数（与批量上限一致，避免一轮内大量实时查询拖慢同步）
MAX_LOCATION_LOOKUPS = 20


# ====================================================================== #
#  配置读取（运行期读取，便于测试覆盖 Config 属性）
# ====================================================================== #
def _cfg_int(name, default):
    try:
        return int(getattr(Config, name, default))
    except (TypeError, ValueError):
        return default


def _batch_limit():
    return _cfg_int('CSI_SYNC_BATCH_SIZE', 20)


def _stale_seconds():
    return _cfg_int('CSI_SYNC_STALE_SECONDS', 45)


def _poll_interval():
    return _cfg_int('CSI_SYNC_POLL_INTERVAL', 5)


def _poll_attempts():
    return max(1, _cfg_int('CSI_SYNC_POLL_ATTEMPTS', 4))


def _max_retry():
    """已废弃：失败即转人工，不再按次数重试。

    仅为兼容 .env 中已有的 CSI_SYNC_MAX_RETRY 配置而保留（不再参与任何决策）。
    """
    return max(1, _cfg_int('CSI_SYNC_MAX_RETRY', 3))


def _max_transient():
    """网络类错误（transient）在同一 traceId 下的最大重试次数"""
    return max(1, _cfg_int('CSI_SYNC_MAX_TRANSIENT', 3))


def _http_timeout():
    return _cfg_int('CSI_SYNC_HTTP_TIMEOUT', 20)


def _http_post(url, **kwargs):
    """POST 到宝鼎：每次新建独立 Session 并禁用环境代理。

    trust_env=False 让 requests 忽略 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 环境变量
    与注册表代理设置。宝鼎是内网主机，若被塞进本机代理（如已关闭的
    127.0.0.1:58229），会直接 ProxyError 导致整条过账链路失败。
    """
    with requests.Session() as sess:
        sess.trust_env = False
        return sess.post(url, **kwargs)


# ====================================================================== #
#  数据访问（独立成类，便于单测注入替身）
# ====================================================================== #
class _Repo:
    """kr_csi_interface 的最小数据访问封装（每次写操作即时 commit）"""

    def __init__(self, conn):
        self.conn = conn

    def _cursor(self):
        return self.conn.cursor()

    def fetch_candidates(self, limit=None):
        """取本轮待处理行。

        两类：
          1. is_sync=0 的新入队 / 重试待重投行
          2. 上次轮询超时未推进的行（QUEUED/PENDING/SUBMITTING）
        两者合并为 is_sync=0 + last_poll_at 超时（新行 last_poll_at 为 NULL，天然满足），
        既覆盖两类的语义，又避免刚被别的线程抢占（last_poll_at=NOW()）的行被重复取出。
        """
        limit = limit or _batch_limit()
        placeholders = ', '.join(['%s'] * len(SUPPORTED_APIS))
        sql = (
            "SELECT id, request_id, item_id, coil_id, lot_id, api_name, siteRef, item, lot, quantity, um, "
            "       job, warehouse, from_location, to_location, reasoncode, account, unitcode1, "
            "       documentnumber, status, retry_count, transient_count, traceid "
            "FROM kr_csi_interface "
            f"WHERE api_name IN ({placeholders}) AND is_sync = 0 "
            "  AND status IN ('PENDING','QUEUED','SUBMITTING') "
            "  AND (last_poll_at IS NULL OR last_poll_at < NOW() - INTERVAL %s SECOND) "
            "ORDER BY id LIMIT %s"
        )
        cur = self._cursor()
        try:
            cur.execute(sql, tuple(SUPPORTED_APIS) + (_stale_seconds(), limit))
            return list(cur.fetchall())
        finally:
            cur.close()

    def claim(self, row_id):
        """抢占一行：仅当仍可处理时 succeed（rowcount==1）。

        SUBMITTING 只有在 last_poll_at 超过 stale 阈值时才允许再抢占（崩溃恢复），
        否则同一行被两个 worker 同时取出时会重复提交。
        """
        sql = (
            "UPDATE kr_csi_interface "
            "SET status = 'SUBMITTING', submitted_at = NOW(), last_poll_at = NOW(), updatedate = NOW() "
            "WHERE id = %s AND is_sync = 0 "
            "  AND (status IN ('PENDING','QUEUED') "
            "       OR (status = 'SUBMITTING' AND (last_poll_at IS NULL "
            "           OR last_poll_at < NOW() - INTERVAL %s SECOND)))"
        )
        cur = self._cursor()
        try:
            affected = cur.execute(sql, (row_id, _stale_seconds()))
            self.conn.commit()
            return affected == 1
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def update(self, row_id, **fields):
        """更新指定列（列名来自内部常量，值走参数化）"""
        if not fields:
            return
        cols = ', '.join(f'`{k}` = %s' for k in fields)
        sql = f"UPDATE kr_csi_interface SET {cols}, updatedate = NOW() WHERE id = %s"
        cur = self._cursor()
        try:
            cur.execute(sql, tuple(fields.values()) + (row_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()


# ====================================================================== #
#  单号 / traceId 生成
# ====================================================================== #
def _retry_trace_id(row):
    """重试只换 traceId，单据号原样沿用（长度 < 128）。

    按接口类型生成不同格式，避免工单发料（无 coil_id/lot_id）拼出 LNone：
      sl-job-issue : JI{job}-{item}-{uuid12}
      其它（移库/杂项）: {documentNumber}-{coil_id 或 lot 或 L{lot_id}}-{uuid12}
    """
    uid = uuid.uuid4().hex[:12]
    if _row_api(row) == API_JOB_ISSUE:
        job = (row.get('job') or '').strip()
        item = (row.get('item') or '').strip()
        return f'JI{job}-{item}-{uid}'
    key = (row.get('coil_id')
           or (row.get('lot') or '').strip()
           or (f"L{row.get('lot_id')}" if row.get('lot_id') else 'X'))
    base = (row.get('documentnumber') or f"MP{row.get('request_id')}").strip()
    return f'{base}-{key}-{uid}'


# ====================================================================== #
#  HTTP 提交与响应判定
# ====================================================================== #
def _row_api(row):
    """取该行的接口名；历史行未带 api_name 时按 sl-qty-move 处理（向后兼容）"""
    api = str(row.get('api_name') or '').strip()
    return api or API_QTY_MOVE


def _submit_url(api_name=API_QTY_MOVE):
    return Config.CSI_SYTELINE_API_BASE.rstrip('/') + '/' + api_name


def _headers():
    return {
        'Content-Type': 'application/json',
        'X-Syteline-Token': Config.CSI_SYTELINE_TOKEN or '',
    }


def _quantity(value):
    """DECIMAL/str -> JSON 数字；整数值转 int，避免 100.0 这类多余小数位"""
    if value is None:
        return None
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return int(qty) if qty.is_integer() else qty


def _split_job(job):
    """工单号拆后缀：'J000029867-0001' → ('J000029867', '0001')。

    CSI 契约要求 job / suffix 分开传；后缀按「最后一个 - 之后的纯数字，1~4 位」识别，
    不符合该形态时原样作为 job 提交（由平台判定，失败走重试/转人工）。
    """
    job = (job or '').strip()
    if '-' in job:
        base, suffix = job.rsplit('-', 1)
        if base and suffix.isdigit() and 1 <= len(suffix) <= 4:
            return base.strip(), suffix
    return job, None


def _build_payload(row):
    """按行 api_name 组装请求体。

    sl-qty-move：库位转移契约（fromLocation/toLocation/reasonCode，无 account/location）
    sl-misc-*：杂项收发契约（location=实际库位、account、lot，无 from/to）
    sl-job-issue：工单发料契约（job/suffix + location=目标库位，无 reasonCode/account/from/to）
    """
    api = _row_api(row)
    if api == API_JOB_ISSUE:
        job, suffix = _split_job(row.get('job'))
        payload = {
            'traceId': row.get('traceid'),
            'site': str(row.get('siteRef') or '').strip(),
            'documentNumber': row.get('documentnumber'),
            'job': job,
            'item': (row.get('item') or '').strip(),
            'quantity': _quantity(row.get('quantity')),
            'lot': (row.get('lot') or '').strip(),
            'warehouse': (row.get('warehouse') or '').strip(),
            'location': (row.get('to_location') or '').strip(),
        }
        if suffix:
            payload['suffix'] = suffix
        return payload

    payload = {
        'traceId': row.get('traceid'),
        'site': str(row.get('siteRef') or '').strip(),
        'documentNumber': row.get('documentnumber'),
        'item': (row.get('item') or '').strip(),
        'quantity': _quantity(row.get('quantity')),
        'warehouse': (row.get('warehouse') or '').strip(),
    }
    if api in MISC_APIS:
        payload['location'] = (row.get('to_location') or '').strip()
        payload['reasonCode'] = (row.get('reasoncode') or '').strip()
        payload['account'] = (row.get('account') or '').strip()
        payload['lot'] = (row.get('lot') or '').strip()
        return payload

    payload['fromLocation'] = (row.get('from_location') or '').strip()
    payload['toLocation'] = (row.get('to_location') or Config.CSI_TO_LOCATION).strip()
    payload['lot'] = (row.get('lot') or '').strip()
    payload['reasonCode'] = (row.get('reasoncode') or Config.CSI_REASON_CODE).strip()
    return payload


def _resp_json(resp):
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _outer_message(resp, body):
    """入队前被拒（400）时，原因在外壳 message 里"""
    if body:
        for key in ('message', 'error', 'Message', 'Error'):
            val = body.get(key)
            if val:
                return str(val)
    text = getattr(resp, 'text', '') or ''
    return text.strip() or '无响应内容'


def _truthy(value):
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'y', 'yes')
    return False


def _ion_failures(data_obj):
    """检查 ion 明细是否全部成功。ion 可能是对象或数组，必须兼容。

    返回失败明细列表（空列表 = 全部成功 / 无明细）。
    """
    if not isinstance(data_obj, dict):
        return []
    ion = data_obj.get('ion')
    if isinstance(ion, dict):
        items = [ion]
    elif isinstance(ion, list):
        items = ion
    else:
        return []
    failures = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if not _truthy(it.get('Success')):
            failures.append(str(it.get('Message') or it.get('message') or it))
    return failures


def _classify(resp):
    """按「字段」判定响应（绝不匹配 message 文本）。

    返回 {'kind': 'success'|'pending'|'failed'|'transient', 'status': str|None, 'reason': str|None}
    """
    code = getattr(resp, 'status_code', 0)
    body = _resp_json(resp)

    if code == 409:
        return {'kind': 'failed', 'status': None,
                'reason': 'HTTP 409 CONFLICT：单号被平台锁定/冲突，需人工核对'}
    if code == 400:
        return {'kind': 'failed', 'status': None,
                'reason': f'HTTP 400 入队被拒：{_outer_message(resp, body)}'}
    if code in (401, 403):
        return {'kind': 'failed', 'status': None,
                'reason': f'HTTP {code} 认证失败：请检查 CSI_SYTELINE_TOKEN'}
    if code != 200:
        return {'kind': 'transient', 'status': None,
                'reason': f'HTTP {code}：{_outer_message(resp, body)}'}

    if body is None:
        return {'kind': 'transient', 'status': None, 'reason': '响应不是合法 JSON'}

    data_obj = body.get('data')
    if not isinstance(data_obj, dict):
        return {'kind': 'transient', 'status': None,
                'reason': f'响应缺少 data 对象：{str(body)[:150]}'}

    status = str(data_obj.get('status') or '').strip().upper()

    if status == 'SUCCESS':
        failures = _ion_failures(data_obj)
        if failures:
            return {'kind': 'failed', 'status': status,
                    'reason': 'ion 明细失败：' + '; '.join(failures)}
        return {'kind': 'success', 'status': status, 'reason': None}

    if status == 'FAILED':
        reason = data_obj.get('error') or data_obj.get('message') or '无错误详情'
        if not isinstance(reason, str):
            reason = str(reason)
        return {'kind': 'failed', 'status': status, 'reason': reason}

    if status == 'UNCONFIRMED':
        return {'kind': 'failed', 'status': status,
                'reason': 'UNCONFIRMED：平台未确认，请先核对 CSI 是否已入账'}

    if status in ('QUEUED', 'PENDING'):
        return {'kind': 'pending', 'status': status, 'reason': None}

    return {'kind': 'transient', 'status': status or None,
            'reason': f'未知 data.status={status!r}'}


def _submit_and_poll(payload, api_name=API_QTY_MOVE, sleep_func=time.sleep):
    """提交并按 5s 间隔用同一 traceId 轮询至终态。"""
    url = _submit_url(api_name)
    headers = _headers()
    attempts = _poll_attempts()

    for attempt in range(1, attempts + 1):
        try:
            resp = _http_post(url, json=payload, headers=headers, timeout=_http_timeout())
        except requests.RequestException as e:
            # 网络异常/超时：本轮不计 retry_count，保留状态待下一轮
            return {'kind': 'transient', 'status': None, 'reason': f'请求异常：{e}'}
        except Exception as e:  # 兜底：任何意外都不应中断整轮
            return {'kind': 'transient', 'status': None, 'reason': f'请求异常：{e}'}

        result = _classify(resp)
        if result['kind'] != 'pending':
            return result
        if attempt < attempts:
            sleep_func(_poll_interval())
            continue
        return result  # 仍未到终态：保留 QUEUED/PENDING，下一轮同 traceId 继续轮询

    return {'kind': 'transient', 'status': None, 'reason': '轮询次数耗尽'}


# ====================================================================== #
#  库位实时查询（已停用：同步器一律使用接口表行内库位，不再实时校正）
#  仅保留函数定义以兼容既有调用方签名与测试，生产路径不再调用。
# ====================================================================== #
def _lookup_lot_location(row):
    """查该 Lot 在 CSI 的实际库位；取不到返回 None（生产路径已不再调用）"""
    siteref = str(row.get('siteRef') or '').strip() or '410'
    item = (row.get('item') or '').strip()
    lot = (row.get('lot') or '').strip()
    if not item or not lot:
        return None
    try:
        loc = CSIClient(siteref).get_lot_location(item, lot)
        return (loc or '').strip() or None
    except Exception as e:
        logger.error('CSI 同步：查询 Lot 库位异常 item=%s lot=%s: %s', item, lot, e)
        return None


def _live_location(row, cache=None):
    """取该行的 CSI 实时库位，同一轮内同 (item, lot) 只查一次。

    已停用：同步器不再做实时库位校正，库位一律取接口表行内值。
    保留本函数仅为兼容既有调用方签名（含 loc_cache 参数）与历史测试。

    cache 为 {(item, lot): loc|None}：查询结果（含失败）都缓存，
    单轮最多查 MAX_LOCATION_LOOKUPS 个不重复 lot。
    """
    item = (row.get('item') or '').strip()
    lot = (row.get('lot') or '').strip()
    if not item or not lot:
        return None
    key = (item, lot)
    if cache is not None:
        if key in cache:
            return cache[key]
        if len(cache) >= MAX_LOCATION_LOOKUPS:
            # 超出单轮查询上限：本轮不再查，交给行内值兜底
            cache[key] = None
            return None
    loc = _lookup_lot_location(row)
    if cache is not None:
        cache[key] = loc
    return loc


def _same_loc(a, b):
    """库位比较（忽略大小写/首尾空白）"""
    return (a or '').strip().lower() == (b or '').strip().lower()


def _is_return_direction(from_location, to_location):
    """是否「车间 → 仓库」的撤回/退料方向。

    判据：源为车间库位（Config.CSI_TO_LOCATION，如 Floor-Core / Floor 开头），
    且目标不是该车间库位（RM 开头的仓库库位、C1M1 等仓库代码均属此类）。
    这类行的 from 是「物料当前所在车间库位」的确定性事实，不能被 CSI 实时
    库位覆盖——同一 Lot 可能同时在车间与仓库两个库位有量，实时查询返回哪一个
    并不确定；覆盖成仓库库位后会与 to 相同，被误判 NOOP。
    """
    frm = (from_location or '').strip()
    to = (to_location or '').strip()
    if not frm or not to:
        return False
    shop = (Config.CSI_TO_LOCATION or '').strip()
    from_shop = (bool(shop) and _same_loc(frm, shop)) or frm.upper().startswith('FLOOR')
    if not from_shop:
        return False
    return to.upper().startswith('RM') or not _same_loc(to, shop)


# ====================================================================== #
#  工单物料 Backflush（倒冲）前置处理
# ====================================================================== #
def _ensure_job_backflush_cleared(row, cache=None):
    """工单发料前把工单物料的 Backflush 清 0（倒冲物料无法手工发料）。

    返回 (ok, msg)：ok=False 时调用方不得提交发料（避免和倒冲冲突）。
    同一轮内同 (job, suffix, item) 只处理一次，结果（含失败）一并缓存；
    跨轮再查一次是可接受的——届时会返回「已为 0，无需修改」，不再写库。
    """
    job_full = (row.get('job') or '').strip()
    job, suffix = _split_job(job_full)
    item = (row.get('item') or '').strip()
    try:
        suffix_val = int(suffix) if suffix else 0
    except (TypeError, ValueError):
        suffix_val = 0

    key = (job, suffix_val, item)
    if cache is not None and key in cache:
        return cache[key]

    siteref = str(row.get('siteRef') or '').strip() or '410'
    try:
        ok, msg = CSIClient(siteref).clear_job_matl_backflush(job, suffix_val, item)
    except Exception as e:                      # 任何异常都不放行，避免倒冲冲突
        ok, msg = False, f'调用 CSI 异常：{e}'
        logger.exception('CSI 同步：取消工单物料 Backflush 异常 job=%s item=%s: %s',
                         job_full, item, e)

    if ok and msg == BF_MSG_CLEARED:
        logger.info('工单 %s 物料 %s Backflush 1->0（已取消倒冲，继续发料）', job_full, item)
    elif not ok:
        logger.warning('工单 %s 物料 %s 取消 Backflush 失败：%s', job_full, item, msg)

    if cache is not None:
        cache[key] = (ok, msg)
    return ok, msg


# ====================================================================== #
#  失败处理 / 告警邮件
# ====================================================================== #
def _fail(repo, row, reason, stats):
    """失败即转人工：不再自动重试，也不更换 traceId。

    只有用户点「重新发送」（routes/csi_interface.py 的 resend，重置为
    is_sync=0 / PENDING / retry_count=0 并生成新 traceId）后，同步器才会再次提交。
    retry_count 仅作累加统计（记录这条行一共失败/被重发过多少次），不参与决策。
    """
    retry = int(row.get('retry_count') or 0) + 1
    reason = (reason or '未知错误')[:MAX_ERR_LEN]

    repo.update(
        row['id'],
        retry_count=retry,
        is_sync=2,
        status='FAILED_MANUAL',
        errormessage=reason,
        transient_count=0,
        last_poll_at=None,
    )
    stats['manual'] += 1
    logger.error('CSI 同步失败，转人工（不自动重试）id=%s request=%s doc=%s 原因=%s',
                 row['id'], row.get('request_id'), row.get('documentnumber'), reason)
    _send_failure_alert(row, retry, reason)


_API_LABELS = {
    API_QTY_MOVE: '库位转移',
    API_MISC_RECEIPT: '盘盈杂项收入',
    API_MISC_ISSUE: '盘亏杂项发出',
    API_JOB_ISSUE: '工单发料',
}


def _send_failure_alert(row, retry, reason):
    """发人工处理邮件；发送失败只记日志，不影响主流程"""
    api = _row_api(row)
    api_label = _API_LABELS.get(api, '交易')
    request_id = row.get('request_id')
    doc_no = (row.get('documentnumber') or '').strip()
    # 盘点差异行的 request_id 为 NULL，用 CSI 单号标识
    subject_key = f'单号{request_id}' if request_id not in (None, '') else f'单号{doc_no}'
    subject = f'[物料看板] CSI同步失败需人工处理（{api}）- {subject_key}'
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    cells = [
        ('接口', api),
        ('申请单号', request_id if request_id not in (None, '') else
         ('（外部工单发料）' if api == API_JOB_ISSUE else '（盘点差异调整）')),
        ('站点', row.get('siteRef')),
        ('物料号', row.get('item')),
        ('批号', row.get('lot')),
        ('数量', row.get('quantity')),
        ('单位', row.get('um') or row.get('unitcode1')),
        ('失败单号', doc_no),
        ('traceId', row.get('traceid')),
        ('失败次数', retry),
    ]
    if api == API_JOB_ISSUE:
        cells.append(('工单号', row.get('job')))
        cells.append(('仓库 / 库位', f"{row.get('warehouse') or ''} / {row.get('to_location') or ''}"))
    elif api in MISC_APIS:
        cells.append(('仓库 / 库位', f"{row.get('warehouse') or ''} / {row.get('to_location') or ''}"))
        cells.append(('原因码 / 科目', f"{row.get('reasoncode') or ''} / {row.get('account') or ''}"))
    else:
        cells.append(('仓库 / 库位',
                      f"{row.get('warehouse') or ''} / {row.get('from_location') or ''} → {row.get('to_location') or ''}"))
    cells.append(('时间', now))

    # 紧凑版：两个「标签|值」放一行（4 列），行数减半、字号缩小，邮件预览窗格即可看全
    pairs = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    trs = []
    for pi, pair in enumerate(pairs):
        tds = []
        for idx, (k, v) in enumerate(pair):
            val = html.escape(str(v if v is not None else ''))
            # 左侧值列（表格第 2 列）固定窄宽，避免短内容把列撑得很宽
            val_w = 'width:100px;' if (pi == 0 and idx == 1) else ''
            tds.append(
                '<td style="padding:3px 8px;border:1px solid #ececec;background:#f7f7f7;'
                'color:#555;white-space:nowrap;width:72px;">' + html.escape(str(k)) + '</td>'
                '<td style="padding:3px 8px;border:1px solid #ececec;word-break:break-all;'
                + val_w + '">' + val + '</td>'
            )
        if len(pair) == 1:
            tds.append('<td style="padding:3px 8px;border:1px solid #ececec;background:#f7f7f7;"></td>'
                       '<td style="padding:3px 8px;border:1px solid #ececec;"></td>')
        trs.append('<tr>' + ''.join(tds) + '</tr>')
    rows_html = ''.join(trs)

    # 网络类错误（同 traceId 重试达上限）与业务错误的文案区分，便于运维判断处置方向
    is_network = str(reason or '').startswith('网络/服务异常重试')
    if is_network:
        intro = (f'CSI {api_label}（{api}）网络异常重试 {_max_transient()} 次仍失败，已转人工；'
                 f'请点「重新发送」或检查到宝鼎接口的网络连通性。')
        footer = (f'网络异常重试 {_max_transient()} 次仍失败，已转人工；请在「同步接口查询」页点「重新发送」，'
                  '或检查到宝鼎接口（bdsytelineapi）的网络连通性（单号与 traceId 均保持不变）。')
    else:
        intro = f'CSI {api_label}（{api}）自动同步失败，已停止自动重试，请人工处理。'
        footer = ('同步失败，已停止自动重试，请在看板「同步接口查询」页点「重新发送」'
                  '或到宝鼎平台人工处理（单号与 traceId 均保持不变）。')

    body = f"""
    <div style="max-width:700px;margin:0 auto;font-family:Arial,'Microsoft YaHei',sans-serif;
                font-size:13px;line-height:1.5;padding:6px 2px;">
      <p style="margin:6px 0;">{intro}</p>
      <table style="width:660px;max-width:100%;border-collapse:collapse;font-size:12px;line-height:1.45;margin:6px 0 10px;">{rows_html}</table>
      <p style="margin:6px 0;color:#c0392b;"><strong>错误原文：</strong>{html.escape(str(reason))}</p>
      <p style="margin:6px 0;color:#666;font-size:12px;">{footer}</p>
      <p style="margin:6px 0;color:#999;font-size:11px;">本邮件由物料领取看板系统自动发送，请勿直接回复。</p>
    </div>
    """
    try:
        send_email(Config.CSI_ALERT_EMAIL, subject, body)
    except Exception as e:
        logger.error('CSI 同步：告警邮件发送失败 request=%s: %s', request_id, e)


# ====================================================================== #
#  单行处理
# ====================================================================== #
def _process_row(repo, row, stats, sleep_func, loc_cache=None, bf_cache=None):
    row_id = row.get('id')
    api = _row_api(row)

    if not repo.claim(row_id):
        stats['skipped'] += 1
        return
    stats['claimed'] += 1

    # 1. 库位：一律取接口表行内值（生成时写入的批次/卷标库位），不做实时校正。
    #    三个分支都不再调用 _live_location / get_lot_location；行内为空即失败转人工。
    #    sl-qty-move  from_location = 申请单源库位（可追溯、不受实时查询单点返回影响）。
    #    sl-job-issue to_location   = 工单发料库位。
    #    sl-misc-*    to_location   = 杂项收发的实际库位（接口字段 location）。
    if api == API_JOB_ISSUE:
        loc = (row.get('to_location') or '').strip()
        if not loc:
            _fail(repo, row, '工单发料缺少库位（to_location 为空）', stats)
            return
    elif api in MISC_APIS:
        loc = (row.get('to_location') or '').strip()
        if not loc:
            _fail(repo, row, '杂项收发缺少库位（to_location 为空）', stats)
            return
    else:
        loc = (row.get('from_location') or '').strip()
        to_loc = (row.get('to_location') or Config.CSI_TO_LOCATION or '').strip()
        # 库位转移以「申请单源库位」为准：同一 lot 可能同时存在于仓库与 Floor-Core，
        # 实时查询只返回其中一处，一旦返回目标库位就会把 from 改成 to、误判 NOOP。
        if not loc:
            _fail(repo, row, '缺少源库位（from_location 为空）', stats)
            return
        row['from_location'] = loc

        # 源库位 = 目的库位：物料已在目标位置，提交必然失败，直接标记 NOOP（不发请求、不发邮件）
        if to_loc and _same_loc(loc, to_loc):
            repo.update(row_id, is_sync=2, status=NOOP_STATUS,
                        errormessage=NOOP_MESSAGE, last_poll_at=None)
            stats['noop'] = stats.get('noop', 0) + 1
            logger.info('CSI 同步：物料已在目标库位，跳过移库 id=%s request=%s doc=%s item=%s lot=%s loc=%s',
                        row_id, row.get('request_id'), row.get('documentnumber'),
                        row.get('item'), row.get('lot'), loc)
            return

    # 2. 基础数据校验（数量必须 > 0，否则必然 400，不必浪费一次请求）
    qty = _quantity(row.get('quantity'))
    if qty is None or qty <= 0:
        _fail(repo, row, f'数量无效：{row.get("quantity")}', stats)
        return
    if not (row.get('item') or '').strip():
        _fail(repo, row, '物料号为空', stats)
        return
    if not (row.get('traceid') or '').strip() or not (row.get('documentnumber') or '').strip():
        _fail(repo, row, '缺少 traceId 或 documentNumber', stats)
        return
    # misc 接口 account 必填，不做默认值猜测
    if api in MISC_APIS and not (row.get('account') or '').strip():
        _fail(repo, row, f'{api} 缺少 account（必填），请在接口表补齐后重投', stats)
        return
    # 工单发料 job 必填
    if api == API_JOB_ISSUE and not (row.get('job') or '').strip():
        _fail(repo, row, f'{api} 缺少 job（工单号，必填）', stats)
        return

    # 3. 工单发料：先把工单物料的 Backflush（倒冲）清 0，否则手工发料会被倒冲冲突挡住
    if api == API_JOB_ISSUE:
        ok, msg = _ensure_job_backflush_cleared(row, bf_cache)
        if not ok:
            _fail(repo, row, f'取消 Backflush 失败：{msg}', stats)
            return

    # 4. 提交并轮询
    result = _submit_and_poll(_build_payload(row), api_name=api, sleep_func=sleep_func)
    kind = result['kind']

    if kind == 'success':
        repo.update(row_id, is_sync=1, status='SUCCESS', errormessage=None, transient_count=0)
        stats['success'] += 1
        logger.info('CSI 同步成功 id=%s api=%s request=%s doc=%s', row_id, api,
                    row.get('request_id'), row.get('documentnumber'))

    elif kind == 'pending':
        # 未到终态：保留 last_poll_at=抢占时刻，下一轮（≥45s）同 traceId 继续轮询
        repo.update(row_id, status=result.get('status') or 'QUEUED', errormessage=None)
        stats['queued'] += 1
        logger.info('CSI 同步未到终态 id=%s api=%s request=%s status=%s，下一轮继续轮询',
                    row_id, api, row.get('request_id'), result.get('status'))

    elif kind == 'transient':
        # 网络/服务端异常：同一 traceId 下轮重试（换 traceId 会导致平台重复过账），
        # 达到上限才转人工。保留 SUBMITTING 与抢占时的 last_poll_at=NOW()，
        # 45s 后下一轮会重新选中（避免短时间内高频重试）。
        tcount = int(row.get('transient_count') or 0) + 1
        if tcount >= _max_transient():
            # 网络类错误达上限：转人工（traceId 保持不变，便于幂等核对）
            _fail(repo, row, f'网络/服务异常重试 {tcount} 次仍失败：{result.get("reason") or ""}', stats)
            return
        repo.update(row_id, transient_count=tcount,
                    errormessage=(result.get('reason') or '')[:MAX_ERR_LEN])
        stats['transient'] += 1
        logger.warning('CSI 同步暂时失败（第 %s/%s 次，同 traceId 下轮重试）id=%s api=%s request=%s 原因=%s',
                       tcount, _max_transient(), row_id, api, row.get('request_id'), result.get('reason'))

    else:  # failed
        _fail(repo, row, result.get('reason'), stats)


# ====================================================================== #
#  对外入口
# ====================================================================== #
def run_once(repo=None, sleep_func=None):
    """单轮处理，返回统计 dict。

    参数（仅供测试注入，生产留空）：
      repo       - 数据访问替身；为空时自建连接（每轮短连接）
      sleep_func - 轮询等待函数；默认 time.sleep
    """
    sleep_func = sleep_func or time.sleep
    stats = {'scanned': 0, 'claimed': 0, 'success': 0, 'queued': 0,
             'retried': 0, 'manual': 0, 'transient': 0, 'skipped': 0, 'noop': 0, 'errors': 0}
    # 同一轮内同 (item, lot) 只查一次 CSI 库位
    loc_cache = {}
    # 同一轮内同 (job, suffix, item) 只处理一次 Backflush 检查/清除
    bf_cache = {}

    conn = None
    own = repo is None
    if own:
        conn = get_db()
        repo = _Repo(conn)

    try:
        rows = repo.fetch_candidates(limit=_batch_limit())
        stats['scanned'] = len(rows)
        for row in rows:
            try:
                _process_row(repo, row, stats, sleep_func, loc_cache=loc_cache, bf_cache=bf_cache)
            except Exception as e:
                # 单行异常不得中断整轮
                stats['errors'] += 1
                logger.exception('CSI 同步：处理行异常 id=%s: %s', row.get('id'), e)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if stats['scanned']:
        logger.info('CSI 同步单轮完成 %s', stats)
    return stats


def _ensure_logging():
    """未配置日志时补一个 stderr handler，保证 INFO 级日志可见"""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        )


def main():
    """常驻循环：每 CSI_SYNC_INTERVAL 秒跑一轮；顶层异常不退出"""
    _ensure_logging()
    if not Config.CSI_SYNC_ENABLED:
        logger.warning('CSI_SYNC_ENABLED=false，CSI 同步器不启动')
        return

    interval = max(5, _cfg_int('CSI_SYNC_INTERVAL', 60))
    logger.info('CSI 同步器启动：interval=%ss batch=%s max_retry=%s',
                interval, _batch_limit(), _max_retry())
    while True:
        try:
            run_once()
        except Exception as e:
            logger.exception('CSI 同步单轮异常，%ss 后继续：%s', interval, e)
        try:
            time.sleep(interval)
        except Exception:
            break


if __name__ == '__main__':
    main()
