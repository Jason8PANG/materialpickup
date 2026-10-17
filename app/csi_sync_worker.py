"""CSI（宝鼎 Syteline）库位转移同步器

职责：扫描接口表 kr_csi_interface 的待处理行 → 提交 sl-qty-move → 轮询终态；
失败自动换单号重试（最多 3 次），超限发邮件告警并停止自动重试。

运行方式：
  - 常驻：CSI_SYNC_ENABLED=true 时由 create_app() 启动后台守护线程（见 app/__init__.py）
  - 单轮调试：python -m app.csi_sync_worker
  - 常驻调试：docker exec <container> python -c "from app.csi_sync_worker import main; main()"

状态机（is_sync / status）：
  0 / PENDING       待提交（新入队或换号后待重试）
  0 / SUBMITTING    本进程已抢占、正在提交/轮询
  0 / QUEUED|PENDING 平台已入队、未到终态（下一轮同 traceId 继续轮询）
  1 / SUCCESS       成功（不再处理）
  2 / FAILED_MANUAL 连续 3 次失败，已发邮件（不再处理）

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
from app.services.csi_service import CSIClient
from app.services.email_service import send_email

logger = logging.getLogger(__name__)

API_QTY_MOVE = 'sl-qty-move'

# 可被抢占处理的状态（PENDING=新行/待重试，QUEUED=平台已入队，SUBMITTING=上次中断）
CLAIMABLE_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING')
MAX_ERR_LEN = 255  # errormessage 列 VARCHAR(255)


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
    return max(1, _cfg_int('CSI_SYNC_MAX_RETRY', 3))


def _http_timeout():
    return _cfg_int('CSI_SYNC_HTTP_TIMEOUT', 20)


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
          1. is_sync=0 的新入队 / 换号待重试行
          2. 上次轮询超时未推进的行（QUEUED/PENDING/SUBMITTING）
        两者合并为 is_sync=0 + last_poll_at 超时（新行 last_poll_at 为 NULL，天然满足），
        既覆盖两类的语义，又避免刚被别的线程抢占（last_poll_at=NOW()）的行被重复取出。
        """
        limit = limit or _batch_limit()
        sql = (
            "SELECT id, request_id, item_id, coil_id, lot_id, siteRef, item, lot, quantity, um, "
            "       warehouse, from_location, to_location, reasoncode, unitcode1, "
            "       documentnumber, status, retry_count, traceid "
            "FROM kr_csi_interface "
            "WHERE api_name = %s AND is_sync = 0 AND status IN ('PENDING','QUEUED','SUBMITTING') "
            "  AND (last_poll_at IS NULL OR last_poll_at < NOW() - INTERVAL %s SECOND) "
            "ORDER BY id LIMIT %s"
        )
        cur = self._cursor()
        try:
            cur.execute(sql, (API_QTY_MOVE, _stale_seconds(), limit))
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
def _retry_document_number(request_id, retry):
    """换号规则：MP{request_id}R{n}，长度 ≤12（接口硬限制）"""
    return f'MP{request_id}R{retry}'[:12]


def _retry_trace_id(row, retry):
    """换号规则：MP{rid}-R{n}-{coil_id|L{lot_id}}-{uuid8}，长度 < 128"""
    key = row.get('coil_id') or f"L{row.get('lot_id')}"
    return f"MP{row.get('request_id')}-R{retry}-{key}-{uuid.uuid4().hex[:8]}"


# ====================================================================== #
#  HTTP 提交与响应判定
# ====================================================================== #
def _submit_url():
    return Config.CSI_SYTELINE_API_BASE.rstrip('/') + '/' + API_QTY_MOVE


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


def _build_payload(row):
    return {
        'traceId': row.get('traceid'),
        'site': str(row.get('siteRef') or '').strip(),
        'documentNumber': row.get('documentnumber'),
        'item': (row.get('item') or '').strip(),
        'quantity': _quantity(row.get('quantity')),
        'warehouse': (row.get('warehouse') or '').strip(),
        'fromLocation': (row.get('from_location') or '').strip(),
        'toLocation': (row.get('to_location') or Config.CSI_TO_LOCATION).strip(),
        'lot': (row.get('lot') or '').strip(),
        'reasonCode': (row.get('reasoncode') or Config.CSI_REASON_CODE).strip(),
    }


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
                'reason': 'HTTP 409 CONFLICT：该单号曾失败被锁定，需更换新单号'}
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


def _submit_and_poll(payload, sleep_func=time.sleep):
    """提交并按 5s 间隔用同一 traceId 轮询至终态。"""
    url = _submit_url()
    headers = _headers()
    attempts = _poll_attempts()

    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_http_timeout())
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
#  from_location 补值
# ====================================================================== #
def _lookup_from_location(row):
    """查该 Lot 在 CSI 的实际库位；取不到返回 None"""
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


# ====================================================================== #
#  失败处理 / 告警邮件
# ====================================================================== #
def _fail(repo, row, reason, stats):
    """失败重试流程：<3 次换新单号重投；>=3 次转人工并发邮件"""
    retry = int(row.get('retry_count') or 0) + 1
    reason = (reason or '未知错误')[:MAX_ERR_LEN]

    if retry < _max_retry():
        doc = _retry_document_number(row.get('request_id'), retry)
        trace = _retry_trace_id(row, retry)
        repo.update(
            row['id'],
            retry_count=retry,
            documentnumber=doc,
            traceid=trace,
            is_sync=0,
            status='PENDING',
            errormessage=reason,
            last_poll_at=None,
            submitted_at=None,
        )
        stats['retried'] += 1
        logger.warning('CSI 同步失败（第 %s/%s 次），已换号重试 id=%s request=%s doc=%s trace=%s 原因=%s',
                       retry, _max_retry(), row['id'], row.get('request_id'), doc, trace, reason)
        return

    repo.update(
        row['id'],
        retry_count=retry,
        is_sync=2,
        status='FAILED_MANUAL',
        errormessage=reason,
        last_poll_at=None,
    )
    stats['manual'] += 1
    logger.error('CSI 同步连续失败 %s 次，转人工 id=%s request=%s doc=%s 原因=%s',
                 retry, row['id'], row.get('request_id'), row.get('documentnumber'), reason)
    _send_failure_alert(row, retry, reason)


def _send_failure_alert(row, retry, reason):
    """发人工处理邮件；发送失败只记日志，不影响主流程"""
    request_id = row.get('request_id')
    subject = f'[物料看板] CSI同步失败需人工处理 - 单号{request_id}'
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    cells = [
        ('申请单号', request_id),
        ('站点', row.get('siteRef')),
        ('物料号', row.get('item')),
        ('批号', row.get('lot')),
        ('数量', row.get('quantity')),
        ('单位', row.get('um') or row.get('unitcode1')),
        ('失败单号', row.get('documentnumber')),
        ('traceId', row.get('traceid')),
        ('失败次数', retry),
        ('仓库 / 库位', f"{row.get('warehouse') or ''} / {row.get('from_location') or ''} → {row.get('to_location') or ''}"),
        ('时间', now),
    ]
    rows_html = ''.join(
        f'<tr><td style="padding:6px 10px;border:1px solid #e8e8e8;background:#fafafa;'
        f'white-space:nowrap;">{html.escape(str(k))}</td>'
        f'<td style="padding:6px 10px;border:1px solid #e8e8e8;">{html.escape(str(v if v is not None else ""))}</td></tr>'
        for k, v in cells
    )

    body = f"""
    <div style="max-width:640px;margin:0 auto;font-family:Arial,sans-serif;padding:16px;">
      <p>CSI 库位转移（sl-qty-move）自动同步失败，已停止自动重试，请人工处理。</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin:12px 0;">{rows_html}</table>
      <p style="color:#c0392b;"><strong>错误原文：</strong>{html.escape(str(reason))}</p>
      <p>已自动换号重试 3 次仍失败，已停止自动重试，请人工在宝鼎平台重新发送或核对数据。</p>
      <p style="color:#999;font-size:12px;">本邮件由物料领取看板系统自动发送，请勿直接回复。</p>
    </div>
    """
    try:
        send_email(Config.CSI_ALERT_EMAIL, subject, body)
    except Exception as e:
        logger.error('CSI 同步：告警邮件发送失败 request=%s: %s', request_id, e)


# ====================================================================== #
#  单行处理
# ====================================================================== #
def _process_row(repo, row, stats, sleep_func):
    row_id = row.get('id')

    if not repo.claim(row_id):
        stats['skipped'] += 1
        return
    stats['claimed'] += 1

    # 1. 补 from_location：A/B 料行入队时该列为空，需查 Lot 在 CSI 的实际库位
    if not (row.get('from_location') or '').strip():
        loc = _lookup_from_location(row)
        if not loc:
            _fail(repo, row, '无法获取 Lot 库位', stats)
            return
        row['from_location'] = loc
        repo.update(row_id, from_location=loc)

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

    # 3. 提交并轮询
    result = _submit_and_poll(_build_payload(row), sleep_func=sleep_func)
    kind = result['kind']

    if kind == 'success':
        repo.update(row_id, is_sync=1, status='SUCCESS', errormessage=None)
        stats['success'] += 1
        logger.info('CSI 同步成功 id=%s request=%s doc=%s', row_id, row.get('request_id'),
                    row.get('documentnumber'))

    elif kind == 'pending':
        # 未到终态：保留 last_poll_at=抢占时刻，下一轮（≥45s）同 traceId 继续轮询
        repo.update(row_id, status=result.get('status') or 'QUEUED', errormessage=None)
        stats['queued'] += 1
        logger.info('CSI 同步未到终态 id=%s request=%s status=%s，下一轮继续轮询',
                    row_id, row.get('request_id'), result.get('status'))

    elif kind == 'transient':
        # 网络/服务端异常：不计 retry，保留 SUBMITTING 与抢占时的 last_poll_at=NOW()，
        # 45s 后下一轮会重新选中（避免短时间内高频重试）
        repo.update(row_id, errormessage=(result.get('reason') or '')[:MAX_ERR_LEN])
        stats['transient'] += 1
        logger.warning('CSI 同步暂时失败（不计重试）id=%s request=%s 原因=%s',
                       row_id, row.get('request_id'), result.get('reason'))

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
             'retried': 0, 'manual': 0, 'transient': 0, 'skipped': 0, 'errors': 0}

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
                _process_row(repo, row, stats, sleep_func)
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
