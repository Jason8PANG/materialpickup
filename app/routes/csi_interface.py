"""同步接口查询（kr_csi_interface）：条件筛选列表 + Excel 导出。

数据来源：备料完成写入 CSI 接口表，由 app/csi_sync_worker.py 轮询同步。
权限：登录即可查看；站点范围受会话 available_sites 限制（admin 可跨站）。
"""
import io
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, session, send_file
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import get_db_connection
from app.utils import WhereBuilder

csi_interface_bp = Blueprint('csi_interface', __name__)

# 导出上限：超出截断（文件名标注）
EXPORT_MAX_ROWS = 50000

SYNC_LABELS = {0: '待同步', 1: '成功', 2: '人工失败'}
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
    'ci.unitcode1, ci.unitcode2, ci.documentnumber, ci.is_sync, ci.status, ci.traceid, '
    'ci.errormessage, ci.createdate, ci.createby, ci.updatedate, ci.updateby, '
    'ci.request_id, ci.item_id, ci.coil_id, ci.lot_id, ci.retry_count, ci.submitted_at, '
    'ci.last_poll_at'
)


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


EXPORT_HEADERS = [
    '申请单号', '站点', '接口', '物料号', '批次', '数量', '单位', '仓库',
    '源货位', '目的货位', '原因码', '单据号', 'traceId', '同步状态', '状态',
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
            row.get('reasoncode'), row.get('documentnumber'), row.get('traceid'),
            SYNC_LABELS.get(row.get('is_sync'), row.get('is_sync')),
            row.get('status'), row.get('retry_count'), row.get('errormessage'),
            row.get('createdate'), row.get('createby'), row.get('updatedate'),
        ]
        ws.append([_cell_text(v) for v in values])
        is_sync = row.get('is_sync')
        if is_sync in SYNC_FILLS:
            ws.cell(row=r_idx, column=14).fill = SYNC_FILLS[is_sync]
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
