"""同步接口查询（kr_csi_interface）：条件筛选列表 + Excel 导出 + 失败重发 + 人工取消。

数据来源：备料完成写入 CSI 接口表，由 app/csi_sync_worker.py 轮询同步。
权限：登录即可查看；站点范围受会话 available_sites 限制（admin 可跨站）。
重发（POST /api/csi-interface/<id>/resend）：仅 warehouse/admin，且只对失败/终止态开放。
取消（POST /api/csi-interface/<id>/cancel）：仅 warehouse/admin，且只对未成功过账的记录开放。
"""
import io
import logging
import time
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, session, send_file
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app import csi_sync_worker as sync_worker
from app.models import get_db, get_db_connection, get_site_filter
from app.services.operation_log import log_from_request, log_operation
from app.utils import WhereBuilder

csi_interface_bp = Blueprint('csi_interface', __name__)
logger = logging.getLogger(__name__)

# 导出上限：超出截断（文件名标注）
EXPORT_MAX_ROWS = 50000

# 同步状态标签：0 待同步 / 1 成功 / 2 人工失败。
# 例外：is_sync=2 但业务状态为 CANCELLED（人工取消）时显示「已取消」，
# 避免用户把「已取消」误读成「同步失败、不可重发」。
SYNC_LABELS = {0: '待同步', 1: '成功', 2: '人工失败'}
CANCELLED_SYNC_LABEL = '已取消'
ALLOWED_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING', 'SUCCESS', 'FAILED_MANUAL',
                  'REVERSED', 'CANCELLED')


class _SiteForbidden(Exception):
    """请求的站点不在当前用户可访问范围内"""


def _check_login():
    user = session.get('user')
    if not user:
        return None, (jsonify({'success': False, 'message': '未登录'}), 401)
    return user, None


def _resolve_site(user):
    """解析站点范围：返回具体站点（None 表示 admin 跨站不过滤）。

    规则：
      - 非 admin：只能用当前站（请求他站 -> 403），默认当前站；
      - admin：可传 siteref 指定站点；不传则查全部站点。
    """
    role = (user or {}).get('role')
    current = str((user or {}).get('siteref') or '')
    available = [str(s) for s in ((user or {}).get('available_sites') or [])]
    if not available and current:
        available = [current]
    requested = (request.args.get('siteref') or '').strip()

    if requested and available and requested not in available:
        raise _SiteForbidden(requested)
    if requested:
        return requested
    if role == 'admin':
        return None
    return current or None


def _parse_date(value):
    """校验 YYYY-MM-DD，返回原串；非法返回 None"""
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return value
    except (TypeError, ValueError):
        return None


def _build_query(user):
    """按筛选条件构建 WHERE；返回 (where, params)。

    站点越权抛 _SiteForbidden，参数非法抛 ValueError。
    """
    wb = WhereBuilder(['1=1'])
    args = request.args

    site = _resolve_site(user)
    if site:
        wb.add('ci.siteref = %s', site)

    is_sync = (args.get('is_sync') or '').strip()
    if is_sync:
        if is_sync not in ('0', '1', '2'):
            raise ValueError('is_sync 参数非法')
        wb.add('ci.is_sync = %s', int(is_sync))

    status = (args.get('status') or '').strip().upper()
    if status:
        if status not in ALLOWED_STATUS:
            raise ValueError('status 参数非法')
        wb.add('ci.status = %s', status)

    for key, col in (('item', 'ci.item'), ('lot', 'ci.lot'),
                     ('documentnumber', 'ci.documentnumber')):
        val = (args.get(key) or '').strip()
        if val:
            wb.add(f'{col} LIKE %s', f'%{val}%')

    traceid = (args.get('traceid') or '').strip()
    if traceid:
        wb.add('ci.traceid LIKE %s', f'%{traceid}%')

    request_id = (args.get('request_id') or '').strip()
    if request_id:
        if not request_id.isdigit():
            raise ValueError('申请单号必须为数字')
        wb.add('ci.request_id = %s', int(request_id))

    date_from = (args.get('date_from') or '').strip()
    if date_from:
        if not _parse_date(date_from):
            raise ValueError('date_from 日期格式应为 YYYY-MM-DD')
        wb.add('DATE(ci.createdate) >= %s', date_from)

    date_to = (args.get('date_to') or '').strip()
    if date_to:
        if not _parse_date(date_to):
            raise ValueError('date_to 日期格式应为 YYYY-MM-DD')
        wb.add('DATE(ci.createdate) <= %s', date_to)

    keyword = (args.get('keyword') or '').strip()
    if keyword:
        kw = f'%{keyword}%'
        wb.add('(ci.traceid LIKE %s OR ci.errormessage LIKE %s OR ci.createby LIKE %s)',
               kw, kw, kw)

    return wb.build()


def _paging():
    try:
        page = int(request.args.get('page') or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.args.get('size') or 50)
    except (TypeError, ValueError):
        size = 50
    page = max(page, 1)
    size = min(max(size, 1), 200)
    return page, size


SELECT_COLUMNS = (
    'ci.id, ci.api_name, ci.siteref, ci.item, ci.lot, ci.quantity, ci.um, ci.job, '
    'ci.warehouse, ci.from_location, ci.to_location, ci.reasoncode, ci.account, '
    'ci.unitcode1, ci.unitcode2, ci.documentnumber, ci.line_seq, ci.direction, '
    'ci.is_sync, ci.status, ci.traceid, '
    'ci.errormessage, ci.createdate, ci.createby, ci.updatedate, ci.updateby, '
    'ci.request_id, ci.item_id, ci.coil_id, ci.lot_id, ci.retry_count, ci.submitted_at, '
    'ci.last_poll_at'
)

DIRECTION_LABELS = {'F': '正向', 'B': '反向'}


def _to_dict(row):
    """行转 JSON 友好结构（Decimal/datetime 序列化）"""
    d = dict(row)
    qty = d.get('quantity')
    if isinstance(qty, Decimal):
        d['quantity'] = float(qty)
    for key in ('createdate', 'updatedate', 'submitted_at', 'last_poll_at'):
        val = d.get(key)
        if isinstance(val, datetime):
            d[key] = val.strftime('%Y-%m-%d %H:%M:%S')
        elif val is not None:
            d[key] = str(val)
    d['is_sync_label'] = SYNC_LABELS.get(d.get('is_sync'), str(d.get('is_sync') or ''))
    if str(d.get('status') or '').upper() == 'CANCELLED':
        d['is_sync_label'] = CANCELLED_SYNC_LABEL
    return d


@csi_interface_bp.route('/api/csi-interface', methods=['GET'])
def api_list():
    user, err = _check_login()
    if err:
        return err
    try:
        where, params = _build_query(user)
    except _SiteForbidden:
        return jsonify({'success': False, 'message': '无权访问该站点数据'}), 403
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    page, size = _paging()
    offset = (page - 1) * size
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT {SELECT_COLUMNS} FROM kr_csi_interface ci "
            f"WHERE {where} ORDER BY ci.createdate DESC, ci.id DESC "
            f"LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = [_to_dict(r) for r in cur.fetchall()]
        cur.execute(
            f"SELECT COUNT(*) AS total, "
            f"COALESCE(SUM(CASE WHEN ci.is_sync = 0 THEN 1 ELSE 0 END), 0) AS pending, "
            f"COALESCE(SUM(CASE WHEN ci.is_sync = 1 THEN 1 ELSE 0 END), 0) AS success, "
            f"COALESCE(SUM(CASE WHEN ci.is_sync = 2 THEN 1 ELSE 0 END), 0) AS failed "
            f"FROM kr_csi_interface ci WHERE {where}",
            params
        )
        agg = cur.fetchone()
    summary = {
        'total': int(agg['total'] or 0),
        'pending': int(agg['pending'] or 0),
        'success': int(agg['success'] or 0),
        'failed': int(agg['failed'] or 0),
    }
    return jsonify({'success': True, 'data': rows, 'total': summary['total'],
                    'page': page, 'size': size, 'summary': summary})


# ====================================================================== #
#  重新发送（失败/终止态 → 重生成 traceId → 立即提交一次）
# ====================================================================== #
# 可重发的「终止态」：与管理端筛选项保持一致（FAILED 为平台显式返回的失败状态）
RESEND_FAILED_STATUS = ('FAILED_MANUAL', 'NOOP', 'CANCELLED', 'FAILED')
# 注意：CANCELLED 虽在此元组内，但 api_resend 会在前面显式拦截（已取消不允许重发），
# 保留它只为兼容历史调用；真正的放行集合 = 元组 - {CANCELLED}


def _empty_stats():
    """与 csi_sync_worker.run_once 同构的统计字典（_process_row/_fail 会累加）"""
    return {'scanned': 0, 'claimed': 0, 'success': 0, 'queued': 0, 'retried': 0,
            'manual': 0, 'transient': 0, 'skipped': 0, 'noop': 0, 'errors': 0}


def _fetch_row(conn, row_id):
    """按 id 读整行（含 api_name/job/coil_id 等 _retry_trace_id 需要的字段）"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM kr_csi_interface WHERE id = %s", (row_id,))
        return cur.fetchone()
    finally:
        cur.close()


def _check_site_scope(user, row):
    """非 admin 只能重发本站记录（与列表的站点隔离口径一致）"""
    if (user or {}).get('role') == 'admin':
        return None
    site = str((user or {}).get('siteref') or '')
    row_site = str(row.get('siteref') or row.get('siteRef') or '')
    if site and row_site and site != row_site:
        return jsonify({'success': False, 'message': '无权操作该站点的记录'}), 403
    return None


def _resend_data(row_id, trace, api_name, status, message, error):
    return {'row_id': row_id, 'traceid': trace, 'status': status,
            'api_name': api_name, 'message': message, 'error': error}


@csi_interface_bp.route('/api/csi-interface/<int:row_id>/resend', methods=['POST'])
def api_resend(row_id):
    """失败记录重发：重生成 traceId → 重置为待同步 → 立即同步提交一次并返回结果。

    只对失败/终止态开放（is_sync=2 或 status ∈ RESEND_FAILED_STATUS）：
      - is_sync=1（SUCCESS/REVERSED）已成功过账，禁止重发（400）；
      - 其余（PENDING/QUEUED/SUBMITTING）正在同步中（400）。

    提交复用 csi_sync_worker 的 _Repo / _process_row，不复制一套提交逻辑。
    单次耗时可达 20~30 秒（内部 5s×4 轮询），前端必须 loading。
    """
    user, err = _check_login()
    if err:
        return err
    if (user or {}).get('role') != 'admin':
        return jsonify({'success': False, 'message': '仅管理员可重新发送'}), 403

    operator = user.get('username') or user.get('display_name') or 'system'
    conn = get_db()
    try:
        repo = sync_worker._Repo(conn)
        row = _fetch_row(conn, row_id)
        if not row:
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        scope_err = _check_site_scope(user, row)
        if scope_err:
            return scope_err

        is_sync = row.get('is_sync')
        status = str(row.get('status') or '').upper()
        api_name = sync_worker._row_api(row)
        if is_sync == 1:
            return jsonify({'success': False, 'message': '该记录已成功过账，不允许重发'}), 400
        # 已取消是业务终态，不允许重发（要与前端 csiCanResend 保持一致）
        if status == 'CANCELLED':
            return jsonify({'success': False, 'message': '该记录已取消，不允许重新发送'}), 400
        if not (is_sync == 2 or status in RESEND_FAILED_STATUS):
            return jsonify({'success': False, 'message': '该记录正在同步中，请稍后再试'}), 400

        # 审计：记录操作前的原值与操作人，供事后调查（日志写入失败不影响重发）
        old_status = status
        old_traceid = row.get('traceid') or ''
        audit_site = str(row.get('siteRef') or row.get('siteref') or '')
        audit_key = str(row.get('documentnumber') or '')

        def _audit_resend(audit_result, new_trace='', extra=None):
            detail = {'old_status': old_status, 'old_traceid': old_traceid,
                      'new_traceid': new_trace}
            if extra:
                detail.update(extra)
            log_from_request('csi_interface', 'resend', siteref=audit_site,
                             target_type='csi_interface', target_id=row_id,
                             target_key=audit_key, operator=operator,
                             result=audit_result, detail=detail)

        trace = sync_worker._retry_trace_id(row)
        repo.update(row_id, is_sync=0, status='PENDING', retry_count=0, transient_count=0,
                    errormessage=None, submitted_at=None, last_poll_at=None,
                    traceid=trace, updateby=operator)
        logger.info('CSI 重发：重置为待同步 id=%s api=%s trace=%s 操作人=%s',
                    row_id, api_name, trace, operator)

        # 重新读取，确保 _process_row 用的是最新 traceId / 库位快照
        row = _fetch_row(conn, row_id)
        stats = _empty_stats()
        try:
            # claim 在 _process_row 内部完成（条件 UPDATE），此处不再预抢占，
            # 否则第二次 claim 必然失败、行会被跳过。
            sync_worker._process_row(repo, row, stats, time.sleep, {})
        except Exception as e:  # 兜底：任何异常都要给前端可读提示，且不留下 SUBMITTING
            logger.exception('CSI 重发：处理行异常 id=%s: %s', row_id, e)
            message = f'提交异常：{e}'[:sync_worker.MAX_ERR_LEN]
            try:
                repo.update(row_id, is_sync=2, status='FAILED_MANUAL',
                            errormessage=message, last_poll_at=None)
            except Exception:
                logger.exception('CSI 重发：异常回写失败 id=%s', row_id)
            _audit_resend('fail', trace, {'message': message, 'stage': 'exception'})
            return jsonify({
                'success': False, 'message': message,
                'data': _resend_data(row_id, trace, api_name, 'FAILED_MANUAL', message, message),
            })

        if stats.get('skipped'):
            message = '该记录正在同步中，请稍后再试'
            _audit_resend('fail', trace, {'message': message, 'stage': 'claim_skipped'})
            return jsonify({
                'success': False, 'message': message,
                'data': _resend_data(row_id, trace, api_name, 'SUBMITTING', message, message),
            })

        # 平台明确失败但未达同步器重试上限时（_fail 走「换 traceId 待重投」分支），
        # 手工重发是一次用户显式操作：直接置为终止态，保证记录保持可重发，
        # 避免用户看到失败却无法再次点击（自动重投仍由后台同步器负责其它行）。
        if stats.get('retried'):
            repo.update(row_id, is_sync=2, status='FAILED_MANUAL', last_poll_at=None)

        latest = _fetch_row(conn, row_id) or row
        final_status = str(latest.get('status') or '')
        final_is_sync = latest.get('is_sync')
        final_trace = latest.get('traceid') or trace
        db_error = (latest.get('errormessage') or '').strip()

        if final_status.upper() == 'SUCCESS' and final_is_sync == 1:
            message = '重新发送成功'
            _audit_resend('ok', final_trace, {'new_status': final_status})
            return jsonify({
                'success': True, 'message': message,
                'data': _resend_data(row_id, final_trace, api_name, final_status, message, None),
            })

        if final_is_sync == 2:
            error = db_error or '平台返回失败'
            message = f'重新发送失败：{error}'
        else:
            error = db_error or '平台未返回终态'
            message = (f'提交已发出但未拿到终态（{final_status}）：{error}，'
                       f'记录保持待同步，后台同步器会继续轮询')
        _audit_resend('fail', final_trace, {'new_status': final_status, 'error': error})
        return jsonify({
            'success': False, 'message': message,
            'data': _resend_data(row_id, final_trace, api_name, final_status, message, error),
        })
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ====================================================================== #
#  人工取消（未成功过账的记录 → CANCELLED，不再提交到 ERP）
# ====================================================================== #
# 仅这些状态可取消；SUCCESS/REVERSED 已过账、CANCELLED 已取消，均不允许
CANCEL_ALLOWED_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING', 'FAILED_MANUAL', 'NOOP', 'FAILED')
CANCEL_SUCCESS_STATUS = ('SUCCESS', 'REVERSED')


@csi_interface_bp.route('/api/csi-interface/<int:row_id>/cancel', methods=['POST'])
def api_cancel(row_id):
    """人工取消：置 CANCELLED / is_sync=2，保留 traceId 以便追溯。

    取消后同步器不会再捞取该行（fetch_candidates 仅取 is_sync=0），
    需要恢复时可对失败行使用「重新发送」重置为待同步。
    """
    user, err = _check_login()
    if err:
        return err
    if (user or {}).get('role') not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    operator = user.get('username') or user.get('display_name') or 'system'
    conn = get_db()
    try:
        row = _fetch_row(conn, row_id)
        if not row:
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        scope_err = _check_site_scope(user, row)
        if scope_err:
            return scope_err

        is_sync = row.get('is_sync')
        status = str(row.get('status') or '').upper()
        if status == 'CANCELLED':
            return jsonify({'success': False, 'message': '已是取消状态'}), 400
        if is_sync == 1 or status in CANCEL_SUCCESS_STATUS:
            return jsonify({'success': False,
                            'message': '已成功过账，不能取消（如需冲销请走盘点差异调整或反向交易）'}), 400
        if status not in CANCEL_ALLOWED_STATUS:
            return jsonify({'success': False,
                            'message': f'当前状态（{status or "空"}）不允许取消'}), 400

        # 条件更新兜底并发：期间被同步器抢占/提交成功则 rowcount=0
        placeholders = ', '.join(['%s'] * len(CANCEL_ALLOWED_STATUS))
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE kr_csi_interface SET status = 'CANCELLED', is_sync = 2, "
                "errormessage = %s, transient_count = 0, last_poll_at = NULL, "
                "updateby = %s, updatedate = NOW() "
                f"WHERE id = %s AND is_sync <> 1 AND status IN ({placeholders})",
                ('人工取消', operator, row_id) + CANCEL_ALLOWED_STATUS
            )
            affected = cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

        if not affected:
            return jsonify({'success': False, 'message': '记录状态已变化，请刷新后重试'}), 400

        logger.info('CSI 取消：id=%s 原状态=%s trace=%s 操作人=%s',
                    row_id, status or '空', row.get('traceid'), operator)
        log_from_request(
            'csi_interface', 'cancel',
            siteref=str(row.get('siteRef') or row.get('siteref') or ''),
            target_type='csi_interface', target_id=row_id,
            target_key=str(row.get('documentnumber') or ''),
            operator=operator, result='ok',
            detail={'old_status': status or '', 'old_traceid': row.get('traceid') or '',
                    'new_status': 'CANCELLED', 'reason': '人工取消'},
        )
        return jsonify({'success': True, 'message': '已取消',
                        'data': {'row_id': row_id, 'status': 'CANCELLED', 'message': '已取消'}})
    finally:
        try:
            conn.close()
        except Exception:
            pass


EXPORT_HEADERS = [
    '申请单号', '站点', '接口', '物料号', '批次', '数量', '单位', '仓库',
    '源货位', '目的货位', '原因码', '单据号', '行序号', '方向', 'traceId', '同步状态', '状态',
    '重试次数', '错误信息', '创建时间', '创建人', '更新时间',
]

SYNC_FILLS = {
    0: PatternFill('solid', fgColor='FFF2CC'),  # 待同步 - 黄
    1: PatternFill('solid', fgColor='C6EFCE'),  # 成功 - 绿
    2: PatternFill('solid', fgColor='FFC7CE'),  # 人工失败 - 红
}


def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return value


def _export_filename(truncated):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    if truncated:
        return f'同步接口查询_{ts}_前{EXPORT_MAX_ROWS}行.xlsx'
    return f'同步接口查询_{ts}.xlsx'


@csi_interface_bp.route('/api/csi-interface/export', methods=['GET'])
def api_export():
    user, err = _check_login()
    if err:
        return err
    try:
        where, params = _build_query(user)
    except _SiteForbidden:
        return jsonify({'success': False, 'message': '无权访问该站点数据'}), 403
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT {SELECT_COLUMNS} FROM kr_csi_interface ci "
            f"WHERE {where} ORDER BY ci.createdate DESC, ci.id DESC "
            f"LIMIT %s",
            params + [EXPORT_MAX_ROWS + 1]
        )
        rows = cur.fetchall()

    truncated = len(rows) > EXPORT_MAX_ROWS
    rows = rows[:EXPORT_MAX_ROWS]

    wb = Workbook()
    ws = wb.active
    ws.title = '同步接口'
    ws.append(EXPORT_HEADERS)

    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='4472C4')
    for idx in range(1, len(EXPORT_HEADERS) + 1):
        cell = ws.cell(row=1, column=idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')

    widths = [len(h) * 2 for h in EXPORT_HEADERS]
    for r_idx, row in enumerate(rows, start=2):
        values = [
            row.get('request_id'), row.get('siteref'), row.get('api_name'),
            row.get('item'), row.get('lot'), row.get('quantity'), row.get('um'),
            row.get('warehouse'), row.get('from_location'), row.get('to_location'),
            row.get('reasoncode'), row.get('documentnumber'), row.get('line_seq'),
            DIRECTION_LABELS.get(row.get('direction'), row.get('direction')),
            row.get('traceid'),
            SYNC_LABELS.get(row.get('is_sync'), row.get('is_sync')),
            row.get('status'), row.get('retry_count'), row.get('errormessage'),
            row.get('createdate'), row.get('createby'), row.get('updatedate'),
        ]
        ws.append([_cell_text(v) for v in values])
        is_sync = row.get('is_sync')
        if is_sync in SYNC_FILLS:
            ws.cell(row=r_idx, column=16).fill = SYNC_FILLS[is_sync]
        for c_idx, val in enumerate(values, start=1):
            if val is not None:
                widths[c_idx - 1] = max(widths[c_idx - 1], len(str(val)) + 2)

    for c_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c_idx)].width = min(max(width, 8), 60)
    ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=_export_filename(truncated),
    )


# ====================================================================== #
#  操作审计日志查询（kr_operation_log）
# ====================================================================== #
# 页面「日志」按钮：按 target_id + target_type 取某条记录的操作时间线。
# 参数全走 WhereBuilder 参数化，站点隔离与列表口径一致（非 admin 只看本站）。
OPERATION_LOG_FIELDS = ('id, siteref, module, action, target_type, target_id, target_key, '
                        'operator, operator_ip, result, detail, created_at')


@csi_interface_bp.route('/api/operation-log', methods=['GET'])
def api_operation_log():
    """操作审计日志查询。

    参数：target_key(LIKE) / target_id(精确) / target_type(精确) / module / action /
          operator(LIKE) / date_from / date_to(YYYY-MM-DD) / page / size(≤500)
    """
    user, err = _check_login()
    if err:
        return err

    target_key = (request.args.get('target_key') or '').strip()
    target_type = (request.args.get('target_type') or '').strip()
    module = (request.args.get('module') or '').strip()
    action = (request.args.get('action') or '').strip()
    operator = (request.args.get('operator') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    target_id = request.args.get('target_id', type=int)
    page = max(1, request.args.get('page', type=int) or 1)
    size = min(max(request.args.get('size', type=int) or 100, 1), 500)

    for label, value in (('date_from', date_from), ('date_to', date_to)):
        if value:
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                return jsonify({'success': False, 'message': f'{label} 格式应为 YYYY-MM-DD'}), 400

    wb = WhereBuilder(['1=1'])
    if target_key:
        wb.add('target_key LIKE %s', f'%{target_key}%')
    if target_id is not None:
        wb.add('target_id = %s', target_id)
    if target_type:
        wb.add('target_type = %s', target_type)
    if module:
        wb.add('module = %s', module)
    if action:
        wb.add('action = %s', action)
    if operator:
        wb.add('operator LIKE %s', f'%{operator}%')
    if date_from:
        wb.add('DATE(created_at) >= %s', date_from)
    if date_to:
        wb.add('DATE(created_at) <= %s', date_to)

    # 站点隔离：admin（无 siteref）不过滤，可见 siteref='' 的系统日志；非 admin 只看本站
    site_clause, site_params = get_site_filter(user)
    if site_clause:
        wb.add(site_clause, *site_params)

    where_clause, params = wb.build()
    offset = (page - 1) * size

    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) AS total FROM kr_operation_log WHERE {where_clause}', params)
            row = cur.fetchone() or {}
            total = int(row.get('total') or 0)
            cur.execute(
                f'SELECT {OPERATION_LOG_FIELDS} FROM kr_operation_log '
                f'WHERE {where_clause} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s',
                list(params) + [size, offset]
            )
            rows = cur.fetchall()
        finally:
            cur.close()

    data = []
    for r in rows:
        item = dict(r)
        created = item.get('created_at')
        item['created_at'] = created.isoformat() if hasattr(created, 'isoformat') else created
        data.append(item)

    return jsonify({
        'success': True,
        'data': data,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size,
    })
