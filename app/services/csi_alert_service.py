"""CSI 失败记录定时邮件通知 —— 数据层 / 邮件构建 / 幂等发送。

职责
----
1. 建表（幂等）：kr_csi_alert_rule（收件人规则）、kr_csi_alert_log（发送日志，唯一键即幂等锁）
2. 读 kr_csi_interface 中 status='FAILED_MANUAL' 的记录，按 (站点, 接口名) 分组
3. 按规则解析收件人（精确 (site, api) > (site, '*') > Config.CSI_ALERT_EMAIL）
4. 生成 HTML 邮件并发送，发送前用 kr_csi_alert_log 的唯一键抢占（多 worker 只发一次）

幂等
----
gunicorn -w 4 时 create_app() 会执行 4 次（每个 worker 一次），定时线程也就有 4 份。
内存去重只在单进程内有效，真正的互斥靠 log 表的
UNIQUE KEY uk_slot(send_date, slot, siteref, api_name)：
只有 INSERT IGNORE 返回 1 行的那份线程拿到发送权，其余一律跳过。

时区：容器为 Asia/Shanghai，统一用 datetime.now() / date.today()，不引入 pytz。
"""
import html
import logging
import re
from datetime import date, datetime

import pymysql
from pymysql.cursors import DictCursor

from app.config import Config
from app.services.operation_log import log_operation

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  表结构
# --------------------------------------------------------------------------- #
CREATE_RULE_SQL = """
CREATE TABLE IF NOT EXISTS kr_csi_alert_rule (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  siteref VARCHAR(16) NOT NULL COMMENT '站点 310/410',
  api_name VARCHAR(64) NOT NULL DEFAULT '*' COMMENT '接口名，*=该站点全部接口',
  emails TEXT NOT NULL COMMENT '收件人，逗号分隔',
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  remark VARCHAR(255) DEFAULT '',
  updated_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_site_api (siteref, api_name),
  KEY idx_site (siteref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='CSI失败邮件收件人规则'
"""

CREATE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS kr_csi_alert_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  send_date DATE NOT NULL,
  slot VARCHAR(8) NOT NULL COMMENT '09:00/12:00/15:00/17:00',
  siteref VARCHAR(16) NOT NULL,
  api_name VARCHAR(64) NOT NULL,
  record_count INT NOT NULL DEFAULT 0,
  emails TEXT,
  result VARCHAR(16) NOT NULL DEFAULT 'ok',
  errormessage VARCHAR(500) DEFAULT '',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_slot (send_date, slot, siteref, api_name),
  KEY idx_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='CSI失败邮件发送日志(唯一键即幂等锁)'
"""

DEFAULT_RULE_SITES = ('310', '410')
DEFAULT_RULE_REMARK = '默认兜底'

# 邮件表格列（顺序即渲染顺序）
MAIL_COLUMNS = ('ID', '接口', '物料', '批次', '数量', '仓库', '状态', '错误信息', '最后轮询时间')
ERR_MAX_LEN = 100          # 错误信息截断长度
API_ALL = '*'             # 「该站点全部接口」的通配规则

_EMAIL_RE = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$')


def _connect():
    return pymysql.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB,
        charset='utf8mb4',
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=8,
        read_timeout=10,
        write_timeout=10,
    )


# --------------------------------------------------------------------------- #
#  建表 / 默认规则
# --------------------------------------------------------------------------- #
def ensure_schema(with_default_rules=True):
    """幂等建表 + 灌默认兜底规则（310/410 各一条，INSERT IGNORE 不覆盖人工改过的配置）"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_RULE_SQL)
            cur.execute(CREATE_LOG_SQL)
            added = 0
            default_email = (getattr(Config, 'CSI_ALERT_EMAIL', '') or '').strip()
            if with_default_rules and default_email:
                for site in DEFAULT_RULE_SITES:
                    added += cur.execute(
                        "INSERT IGNORE INTO kr_csi_alert_rule "
                        "(siteref, api_name, emails, enabled, remark, updated_by) "
                        "VALUES (%s, %s, %s, 1, %s, 'init')",
                        (site, API_ALL, default_email, DEFAULT_RULE_REMARK),
                    )
            cur.execute("SELECT COUNT(*) AS c FROM kr_csi_alert_rule")
            total = int(cur.fetchone()['c'])
        return {'added': added, 'rules_total': total}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  规则 CRUD（前端维护）
# --------------------------------------------------------------------------- #
def list_rules():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, siteref, api_name, emails, enabled, remark, updated_by, updated_at "
                "FROM kr_csi_alert_rule ORDER BY siteref, api_name"
            )
            rows = list(cur.fetchall() or [])
    finally:
        conn.close()
    for r in rows:
        r['enabled'] = bool(r.get('enabled'))
        if isinstance(r.get('updated_at'), datetime):
            r['updated_at'] = r['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
    return rows


def parse_emails(raw):
    """逗号/分号/空白分隔 → 去重邮箱列表（保持录入顺序）"""
    parts = re.split(r'[,;\s，；、]+', str(raw or ''))
    seen, result = set(), []
    for p in parts:
        addr = p.strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            result.append(addr)
    return result


def is_valid_email(addr):
    return bool(_EMAIL_RE.match(str(addr or '').strip()))


def save_rule(data, operator='SYSTEM'):
    """新增或更新规则。

    - 带 id：按主键更新（改 (site, api) 撞唯一键时明确报错，避免静默覆盖别人的规则）
    - 不带 id：(site, api) 已存在则更新（重复新增即编辑，前端更省事）
    返回 (ok, message, rule_id)
    """
    siteref = str(data.get('siteref') or '').strip()
    api_name = str(data.get('api_name') or '').strip() or API_ALL
    emails = parse_emails(data.get('emails'))
    enabled = 1 if data.get('enabled', True) in (True, 1, '1', 'true', 'True', 'on') else 0
    remark = str(data.get('remark') or '')[:255]
    rule_id = data.get('id')

    if siteref not in Config.SITE_CONFIG:
        return False, f'站点不合法：{siteref}', None
    if len(api_name) > 64:
        return False, '接口名过长（最多 64 字符）', None
    if not emails:
        return False, '收件人不能为空', None
    bad = [e for e in emails if not is_valid_email(e)]
    if bad:
        return False, '邮箱格式不正确：' + ', '.join(bad), None

    emails_str = ','.join(emails)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if rule_id:
                cur.execute(
                    "UPDATE kr_csi_alert_rule SET siteref=%s, api_name=%s, emails=%s, "
                    "enabled=%s, remark=%s, updated_by=%s WHERE id=%s",
                    (siteref, api_name, emails_str, enabled, remark, operator, rule_id),
                )
                if cur.rowcount == 0:
                    cur.execute("SELECT id FROM kr_csi_alert_rule WHERE id=%s", (rule_id,))
                    if not cur.fetchone():
                        return False, '规则不存在（可能已被删除）', None
                new_id = int(rule_id)
            else:
                # 唯一键冲突即改为更新：(site, api) 是业务主键
                cur.execute(
                    "INSERT INTO kr_csi_alert_rule "
                    "(siteref, api_name, emails, enabled, remark, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE emails=VALUES(emails), enabled=VALUES(enabled), "
                    "remark=VALUES(remark), updated_by=VALUES(updated_by)",
                    (siteref, api_name, emails_str, enabled, remark, operator),
                )
                cur.execute(
                    "SELECT id FROM kr_csi_alert_rule WHERE siteref=%s AND api_name=%s",
                    (siteref, api_name),
                )
                row = cur.fetchone()
                new_id = int(row['id']) if row else None
    except pymysql.err.IntegrityError as e:
        return False, f'站点+接口名已存在另一条规则（唯一键冲突）：{e}', None
    finally:
        conn.close()
    return True, '保存成功', new_id


def delete_rule(rule_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            affected = cur.execute("DELETE FROM kr_csi_alert_rule WHERE id=%s", (rule_id,))
    finally:
        conn.close()
    return affected > 0


def resolve_rule(siteref, api_name):
    """取该组适用的规则：精确 (site, api) 优先，其次 (site, '*')，都没有返回 None。

    返回的是原始行（含 enabled），由调用方判定 enabled=0 时跳过——
    精确规则被停用即视为「这组不发」，不再回落到 * 规则（配置要可预期）。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, siteref, api_name, emails, enabled, remark "
                "FROM kr_csi_alert_rule WHERE siteref=%s AND api_name IN (%s, %s)",
                (str(siteref), str(api_name), API_ALL),
            )
            rows = {str(r['api_name']): r for r in (cur.fetchall() or [])}
    finally:
        conn.close()
    return rows.get(str(api_name)) or rows.get(API_ALL)


def list_logs(limit=20):
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, send_date, slot, siteref, api_name, record_count, emails, "
                "result, errormessage, created_at FROM kr_csi_alert_log "
                f"ORDER BY id DESC LIMIT {limit}"
            )
            rows = list(cur.fetchall() or [])
    finally:
        conn.close()
    for r in rows:
        if isinstance(r.get('send_date'), date):
            r['send_date'] = r['send_date'].strftime('%Y-%m-%d')
        if isinstance(r.get('created_at'), datetime):
            r['created_at'] = r['created_at'].strftime('%Y-%m-%d %H:%M:%S')
    return rows


def api_names():
    """接口名候选（供前端下拉）：取自接口表实际出现过的 api_name"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT api_name FROM kr_csi_interface "
                "WHERE api_name IS NOT NULL AND api_name <> '' ORDER BY api_name"
            )
            rows = [str(r['api_name']) for r in (cur.fetchall() or [])]
    finally:
        conn.close()
    return rows


# --------------------------------------------------------------------------- #
#  失败记录查询 / 分组
# --------------------------------------------------------------------------- #
def fetch_failed_rows():
    """status='FAILED_MANUAL' 且站点非空的记录，按 last_poll_at 升序（最久未推进的在前）

    last_poll_at 为 NULL（从未轮询成功过）视为最久，排最前。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, `siteRef` AS siteref, api_name, item, lot, quantity, warehouse, "
                "       status, errormessage, last_poll_at "
                "FROM kr_csi_interface "
                "WHERE status = 'FAILED_MANUAL' "
                "  AND `siteRef` IS NOT NULL AND `siteRef` <> '' "
                "ORDER BY (last_poll_at IS NULL) DESC, last_poll_at ASC, id ASC"
            )
            return list(cur.fetchall() or [])
    finally:
        conn.close()


def group_rows(rows):
    """按 (站点, 接口名) 分组，保持查询顺序；返回 [(siteref, api_name, [rows]), ...]"""
    groups, index = [], {}
    for row in rows:
        site = str(row.get('siteref') or '').strip()
        api = str(row.get('api_name') or '').strip() or '-'
        key = (site, api)
        if key not in index:
            index[key] = len(groups)
            groups.append((site, api, []))
        groups[index[key]][2].append(row)
    return groups


# --------------------------------------------------------------------------- #
#  邮件正文
# --------------------------------------------------------------------------- #
def format_qty(value):
    """数量：保留 4 位小数后去尾零（149.4488 → 149.4488，23.6220 → 23.622）"""
    if value is None or value == '':
        return '-'
    try:
        text = f'{float(value):.4f}'
    except (TypeError, ValueError):
        return str(value)
    return text.rstrip('0').rstrip('.') or '0'


def truncate(text, limit=ERR_MAX_LEN):
    """错误信息截断：超长保留 limit 字并补 …（按字符而非字节）"""
    text = '' if text is None else str(text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + '…'


def _fmt_time(value):
    if not value:
        return '-'
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def build_email(siteref, api_name, rows, now=None, max_rows=None):
    """生成 (subject, html)。表格用 <table> + 内联样式，兼容各邮件客户端（不用 flex/grid）。"""
    now = now or datetime.now()
    max_rows = int(max_rows or getattr(Config, 'CSI_ALERT_MAX_ROWS', 100) or 100)
    total = len(rows)
    shown = rows[:max_rows]

    site_label = Config.SITE_CONFIG.get(str(siteref), str(siteref))
    subject = f'[物料看板] CSI接口失败待处理 - 站点 {siteref} / {api_name}（{total} 条）'

    th = ('padding:6px 8px;border:1px solid #d0d0d0;background:#f2f2f2;'
          'font-weight:bold;text-align:left;white-space:nowrap;')
    td = 'padding:5px 8px;border:1px solid #e0e0e0;vertical-align:top;'

    head_cells = ''.join(f'<th style="{th}">{html.escape(c)}</th>' for c in MAIL_COLUMNS)
    body_rows = []
    for r in shown:
        cells = (
            r.get('id'),
            r.get('api_name') or api_name,
            r.get('item') or '-',
            r.get('lot') or '-',
            format_qty(r.get('quantity')),
            r.get('warehouse') or '-',
            r.get('status') or '-',
            truncate(r.get('errormessage')) or '-',
            _fmt_time(r.get('last_poll_at')),
        )
        tds = []
        for idx, val in enumerate(cells):
            style = td + ('white-space:nowrap;' if idx in (0, 4, 8) else '')
            style += 'text-align:right;' if idx == 4 else ''
            tds.append(f'<td style="{style}">{html.escape(str(val if val is not None else "-"))}</td>')
        body_rows.append('<tr>' + ''.join(tds) + '</tr>')

    more_note = ''
    if total > len(shown):
        more_note = (f'<p style="margin:8px 0 0;color:#e6a23c;font-size:12px;">'
                     f'仅显示前 {len(shown)} 条，共 {total} 条，请到系统内查看全部。</p>')

    base_url = (getattr(Config, 'BASE_URL', '') or '').rstrip('/')

    html_body = f"""<div style="max-width:1000px;margin:0 auto;font-family:Arial,'Microsoft YaHei',sans-serif;
     font-size:13px;line-height:1.5;color:#333;padding:8px 4px;">
  <h3 style="margin:6px 0 10px;color:#c0392b;font-size:16px;">CSI 接口同步失败，需人工处理</h3>
  <p style="margin:0 0 10px;">
    站点 {html.escape(str(siteref))}（{html.escape(site_label)}） ｜
    接口 {html.escape(str(api_name))} ｜
    共 {total} 条 ｜
    生成时间 {now.strftime('%Y-%m-%d %H:%M')}
  </p>
  <table border="1" cellspacing="0" cellpadding="4"
         style="border-collapse:collapse;width:100%;font-size:12px;border:1px solid #d0d0d0;">
    <thead><tr>{head_cells}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
  {more_note}
  <p style="color:#c00;margin:14px 0 6px;font-weight:bold;">请尽快处理，避免账务不正确。</p>
  <p style="margin:0 0 6px;">处理入口：<a href="{base_url}/csi-interface">{base_url}/csi-interface</a></p>
  <p style="margin:10px 0 0;color:#999;font-size:11px;">本邮件由物料领取看板系统定时发送，请勿直接回复。</p>
</div>"""
    return subject, html_body


# --------------------------------------------------------------------------- #
#  幂等锁（kr_csi_alert_log 唯一键）
# --------------------------------------------------------------------------- #
def claim_log(send_date, slot, siteref, api_name, record_count, emails, force=False):
    """抢占本组本时段的发送权。

    force=True（手动测试/--force）：已存在行时直接覆盖并继续发，不算冲突。
    返回 True 表示可以发信。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if force:
                cur.execute(
                    "INSERT INTO kr_csi_alert_log "
                    "(send_date, slot, siteref, api_name, record_count, emails, result) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'ok') "
                    "ON DUPLICATE KEY UPDATE record_count=VALUES(record_count), "
                    "emails=VALUES(emails), result='ok', errormessage=''",
                    (send_date, slot, str(siteref), str(api_name), record_count, emails),
                )
                return True
            affected = cur.execute(
                "INSERT IGNORE INTO kr_csi_alert_log "
                "(send_date, slot, siteref, api_name, record_count, emails) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (send_date, slot, str(siteref), str(api_name), record_count, emails),
            )
            return affected == 1
    finally:
        conn.close()


def finish_log(send_date, slot, siteref, api_name, result, errormessage='', record_count=None):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if record_count is None:
                cur.execute(
                    "UPDATE kr_csi_alert_log SET result=%s, errormessage=%s "
                    "WHERE send_date=%s AND slot=%s AND siteref=%s AND api_name=%s",
                    (result, str(errormessage or '')[:500], send_date, slot, str(siteref), str(api_name)),
                )
            else:
                cur.execute(
                    "UPDATE kr_csi_alert_log SET result=%s, errormessage=%s, record_count=%s "
                    "WHERE send_date=%s AND slot=%s AND siteref=%s AND api_name=%s",
                    (result, str(errormessage or '')[:500], record_count,
                     send_date, slot, str(siteref), str(api_name)),
                )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  发送
# --------------------------------------------------------------------------- #
def _send_to_each(emails, subject, html_body, **log_kw):
    """逐个收件人发信（send_email 只接受单个地址，多地址逗号串会被 SMTP 当成一个非法 RCPT）。

    log_kw 原样透传给 send_email，用于写 kr_email_log（module/siteref/related_*）。

    返回 (成功数, 失败明细)
    """
    from app.services.email_service import send_email

    ok_count, errors = 0, []
    for addr in emails:
        try:
            if send_email(addr, subject, html_body, **log_kw):
                ok_count += 1
            else:
                errors.append(f'{addr}: 发送失败')
        except Exception as e:                     # noqa: BLE001 - 单个地址失败不影响其它地址
            errors.append(f'{addr}: {e}')
    return ok_count, errors


def send_group(slot, siteref, api_name, rows, send_date=None, force=False, now=None):
    """发送一组（一个 站点+接口 的所有失败记录），带幂等抢占。"""
    send_date = send_date or date.today()
    now = now or datetime.now()

    rule = resolve_rule(siteref, api_name)
    if rule is not None and not rule.get('enabled'):
        logger.info('CSI 告警：规则已停用，跳过 %s/%s', siteref, api_name)
        return {'siteref': siteref, 'api_name': api_name, 'status': 'disabled',
                'count': len(rows), 'emails': ''}

    emails = parse_emails(rule.get('emails')) if rule else []
    source = 'rule' if emails else 'default'
    if not emails:
        emails = parse_emails(getattr(Config, 'CSI_ALERT_EMAIL', ''))
    if not emails:
        logger.warning('CSI 告警：%s/%s 无收件人（规则与默认邮箱均为空）', siteref, api_name)
        return {'siteref': siteref, 'api_name': api_name, 'status': 'no_recipient',
                'count': len(rows), 'emails': ''}

    emails_str = ','.join(emails)
    if not claim_log(send_date, slot, siteref, api_name, len(rows), emails_str, force=force):
        # 已被其它 gunicorn worker 抢到（或本次时段已发过）→ 静默跳过，绝不重复发信
        logger.info('CSI 告警：%s %s/%s 已有发送记录，跳过（幂等）', slot, siteref, api_name)
        return {'siteref': siteref, 'api_name': api_name, 'status': 'duplicate',
                'count': len(rows), 'emails': emails_str}

    subject, html_body = build_email(siteref, api_name, rows, now=now)
    ok_count, errors = _send_to_each(
        emails, subject, html_body,
        module='csi_alert_scheduler',
        siteref=str(siteref),
        related_type='csi_interface',
        related_key=f'{siteref}/{api_name}',
    )
    result = 'ok' if not errors else 'fail'
    finish_log(send_date, slot, siteref, api_name, result, '; '.join(errors), record_count=len(rows))

    logger.info('CSI 告警：%s %s/%s 共 %s 条 → %s（%s/%s 成功）%s',
                slot, siteref, api_name, len(rows), emails_str, ok_count, len(emails),
                '' if not errors else ' 失败: ' + '; '.join(errors))
    return {'siteref': siteref, 'api_name': api_name,
            'status': 'sent' if ok_count else 'fail',
            'count': len(rows), 'emails': emails_str, 'subject': subject,
            'recipient_source': source, 'ok_count': ok_count,
            'error': '; '.join(errors)}


def send_alert(slot, send_date=None, force=False, now=None):
    """扫描所有 FAILED_MANUAL 记录并按组发送；返回汇总 dict。

    单组异常只影响该组（记 fail 日志），不中断其余组。
    """
    send_date = send_date or date.today()
    now = now or datetime.now()
    rows = fetch_failed_rows()
    groups = group_rows(rows)
    results = []
    for siteref, api_name, group_rows_ in groups:
        try:
            results.append(send_group(slot, siteref, api_name, group_rows_,
                                      send_date=send_date, force=force, now=now))
        except Exception as e:                     # noqa: BLE001 - 单组失败不能拖垮整轮
            logger.exception('CSI 告警：组 %s/%s 发送异常: %s', siteref, api_name, e)
            results.append({'siteref': siteref, 'api_name': api_name,
                            'status': 'error', 'count': len(group_rows_), 'error': str(e)})
    return {'slot': slot, 'send_date': send_date.strftime('%Y-%m-%d'),
            'total_rows': len(rows), 'groups': results}


def log_audit(action, detail, result='ok', operator='SYSTEM', siteref=''):
    """审计（module=csi_alert）：规则维护 / 手动测试发送都留痕"""
    return log_operation('csi_alert', action, siteref=siteref,
                         target_type='csi_alert_rule', result=result,
                         operator=operator, detail=detail)
