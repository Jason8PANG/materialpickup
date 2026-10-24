"""
线材管理路由（wire）：
  1. 卷标查询（kr_wire_coil）——筛选 + CSV 导出
  2. 线材消耗查询（kr_wire_coil_consumption）——筛选 + CSV 导出
  3. 线材库存盘点（kr_inventory_count / kr_inventory_count_item）
     - 选择卷标创建盘点单
     - 开启盘点后锁定卷标（不允许消耗）
     - 录入各卷标实际测量长度(mm)
     - 生成差异表 + PDF 报告「线边仓线材盘点报告」
"""
import csv
import io
import io as _io
import logging
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, session, render_template, Response
from pymysql.err import IntegrityError

from app.config import Config
from app.models import get_db_connection, get_site_filter
from app.services.csi_service import CSIClient
from app.utils import WhereBuilder, is_en

wire_bp = Blueprint('wire', __name__)
logger = logging.getLogger(__name__)

COIL_STATUS_LABELS = {
    'in_stock': '在库', 'in_shop': '在车间',
    'issued': '已出库', 'scrapped': '报废', 'counting': '盘点中',
}

# 盘点差异调整 → CSI 杂项交易
# diff_converted = (实际盘点 - 账面剩余) / 换算系数
#   > 0：盘盈，库存增加 → 杂项接收
#   < 0：盘亏，库存减少 → 杂项发出
COUNT_ADJUST_API_POSITIVE = 'sl-misc-receipt'  # diff_converted > 0：盘盈，库存增加
COUNT_ADJUST_API_NEGATIVE = 'sl-misc-issue'    # diff_converted < 0：盘亏，库存减少
COUNT_ADJUST_REASON_CODE = 'P1'
COUNT_ADJUST_ACCOUNT = '505100'
CSI_DOC_MAX_LEN = 12   # documentnumber 列 VARCHAR(12)，接口硬限制


def _check_login():
    user = session.get('user')
    if not user:
        return None, (jsonify({'success': False, 'message': '未登录'}), 401)
    return user, None


def _factor(unit):
    u = (unit or '').strip().upper()
    return Config.UNIT_CONVERT_FACTOR.get(u) if u else None


# ================================================================== #
#  页面
# ================================================================== #
@wire_bp.route('/wire/coils', methods=['GET'])
def coils_page():
    if not session.get('user'):
        from flask import redirect, url_for
        return redirect(url_for('pages.login_page'))
    return render_template('wire_coils.html')


@wire_bp.route('/wire/consumption', methods=['GET'])
def consumption_page():
    if not session.get('user'):
        from flask import redirect, url_for
        return redirect(url_for('pages.login_page'))
    return render_template('wire_consumption.html')


@wire_bp.route('/wire/count', methods=['GET'])
def count_page():
    if not session.get('user'):
        from flask import redirect, url_for
        return redirect(url_for('pages.login_page'))
    return render_template('wire_count.html')


# ================================================================== #
#  卷标查询
# ================================================================== #
def _build_coil_query(user=None):
    """解析卷标查询参数，返回 (where_clause, params)；传入 user 时按当前站点隔离"""
    wb = WhereBuilder(['c.is_deleted = 0'])
    args = request.args
    if args.get('coil_id'):
        wb.add('c.coil_id LIKE %s', f"%{args['coil_id'].strip()}%")
    if args.get('part_number'):
        wb.add('c.part_number LIKE %s', f"%{args['part_number'].strip()}%")
    if args.get('lot_no'):
        wb.add('c.lot_no LIKE %s', f"%{args['lot_no'].strip()}%")
    if args.get('status') and args['status'].strip() not in ('finished', 'counting'):
        wb.add('c.status = %s', args['status'].strip())
    elif args.get('status') and args['status'].strip() == 'counting':
        # 派生状态：卷标出现在活跃盘点单（status='counting'）中即视为盘点中
        wb.add("c.coil_id IN (SELECT i.coil_id FROM kr_inventory_count_item i "
               "JOIN kr_inventory_count cnt ON cnt.id = i.count_id "
               "WHERE cnt.status = 'counting')")
    if args.get('siteref'):
        wb.add('c.siteref = %s', args['siteref'].strip())
    if args.get('date_from'):
        wb.add('DATE(c.created_at) >= %s', args['date_from'].strip())
    if args.get('date_to'):
        wb.add('DATE(c.created_at) <= %s', args['date_to'].strip())
    # 站点隔离：session 当前站点（get_site_filter 对无 siteref 的 admin 不过滤）
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f'c.{site_filter}', *site_params)
    return wb.build()


@wire_bp.route('/api/wire/coils/lookup/<coil_id>', methods=['GET'])
def api_coil_lookup(coil_id):
    """扫描卷标 ID 快速查询（盘点选卷用）：返回物料/Lot/状态"""
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT coil_id, part_number, lot_no, status, unit, coil_length "
            "FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        row = cur.fetchone()
        counting = False
        if row:
            cur.execute(
                "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
                "JOIN kr_inventory_count c ON c.id = i.count_id "
                "WHERE i.coil_id = %s AND c.status = 'counting'",
                (coil_id,)
            )
            counting = cur.fetchone()['n'] > 0
    if not row:
        return jsonify({'success': False, 'message': f'卷标 {coil_id} 不存在'}), 404
    d = dict(row)
    d['status_label'] = COIL_STATUS_LABELS.get(d.get('status'), d.get('status') or '')
    if counting:
        d['status_label'] = '盘点中'
    d['can_count'] = d.get('status') == 'in_shop' and not counting
    if d.get('coil_length') is not None:
        d['coil_length'] = float(d['coil_length'])
    return jsonify({'success': True, 'data': d})


@wire_bp.route('/api/wire/coils', methods=['GET'])
def api_coils():
    user, err = _check_login()
    if err:
        return err
    status_filter = request.args.get('status') or ''
    where, params = _build_coil_query(user)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT c.*, "
            f"COALESCE((SELECT SUM(out_length) FROM kr_wire_coil_consumption k "
            f"WHERE k.coil_id = c.coil_id AND k.consume_type IN ('consumption','count_adjust')), 0) AS used_mm "
            f"FROM kr_wire_coil c WHERE {where} ORDER BY c.id DESC LIMIT 500",
            params
        )
        rows = cur.fetchall()
        # 活跃盘点中的卷标集合（派生状态：盘点中）
        counting_ids = set()
        if rows:
            cur.execute(
                "SELECT DISTINCT i.coil_id FROM kr_inventory_count_item i "
                "JOIN kr_inventory_count c ON c.id = i.count_id "
                "WHERE c.status = 'counting'"
            )
            counting_ids = {r['coil_id'] for r in cur.fetchall()}
    data = []
    for r in rows:
        d = dict(r)
        d['status_label'] = COIL_STATUS_LABELS.get(d.get('status'), d.get('status') or '')
        if d.get('coil_id') in counting_ids:
            d['status_label'] = '盘点中'
        if d.get('coil_length') is not None:
            d['coil_length'] = float(d['coil_length'])
        if d.get('used_mm') is not None:
            d['used_mm'] = float(d['used_mm'])
        # 剩余长度（mm）= 卷长×系数 - 使用；剩余长度（原始单位）= mm ÷ 系数
        factor = _factor(d.get('unit'))
        if d.get('coil_length') is not None and factor:
            remain_mm = round(d['coil_length'] * factor - (d.get('used_mm') or 0), 4)
            d['remain_mm'] = remain_mm
            d['remain_orig'] = round(remain_mm / factor, 4)
        else:
            d['remain_mm'] = None
            d['remain_orig'] = None
        data.append(d)
    # 已消完筛选（派生状态：剩余 ≤ 0）
    if status_filter == 'finished':
        data = [d for d in data if d.get('remain_mm') is not None and d['remain_mm'] <= 0]
    return jsonify({'success': True, 'data': data, 'total': len(data)})


# ------------------------------------------------------------------ #
#  卷标生命周期详情（发出 / 消耗 / 退料 / 盘点）
# ------------------------------------------------------------------ #
RETURN_REVIEW_LABELS = {None: '待审核', 'confirmed': '已审核', 'rejected': '已驳回'}


def _r4(value):
    """统一保留 4 位小数（项目全链路 4 位精度）"""
    if value is None or value == '':
        return None
    return round(float(value), 4)


def _fmt_qty(value):
    """数量展示：4 位小数，去掉无意义的尾随 0"""
    num = _r4(value)
    if num is None:
        return '-'
    return f'{num:.4f}'.rstrip('0').rstrip('.')


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None


def _request_no(cur, request_id, cache=None):
    """申请单号：取 kr_csi_interface 已生成的 CSI 单据号
    （MP{request_id}{line_seq:02d}，见 services/csi_interface._document_number），
    查不到时回落为「申请单 {id}」，不臆造单号。"""
    if not request_id:
        return None
    if cache is not None and request_id in cache:
        return cache[request_id]
    cur.execute(
        "SELECT documentnumber FROM kr_csi_interface "
        "WHERE request_id = %s AND direction = 'F' "
        "AND documentnumber IS NOT NULL AND documentnumber <> '' "
        "ORDER BY id LIMIT 1",
        (request_id,)
    )
    row = cur.fetchone()
    no = (row.get('documentnumber') if row else None) or f'申请单 {request_id}'
    if cache is not None:
        cache[request_id] = no
    return no


@wire_bp.route('/api/wire/coils/<coil_id>/history', methods=['GET'])
def api_coil_history(coil_id):
    """单个卷标的完整生命周期：发出 → 工单消耗/报废 → 退料 → 盘点盈亏"""
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        coil = cur.fetchone()
        if not coil:
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        # 站点隔离：非 admin 或 admin 已选站点时，只允许访问本站点卷标
        site_filter, site_params = get_site_filter(user)
        if site_filter and coil.get('siteref') != site_params[0]:
            return jsonify({'success': False, 'message': '无权访问该站点数据'}), 403

        req_cache = {}
        req_no = _request_no(cur, coil.get('request_id'), req_cache)
        prev_req_no = _request_no(cur, coil.get('prev_request_id'), req_cache)

        status_label = COIL_STATUS_LABELS.get(coil.get('status'), coil.get('status') or '')
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
            "JOIN kr_inventory_count c ON c.id = i.count_id "
            "WHERE i.coil_id = %s AND c.status = 'counting'",
            (coil_id,)
        )
        if cur.fetchone()['n'] > 0:
            status_label = '盘点中'   # 派生状态：盘点中

        timeline = []

        # 1) 发出：卷标创建即所属申请单发出
        if coil.get('request_id'):
            unit = coil.get('unit') or ''
            timeline.append({
                'time': _iso(coil.get('created_at')),
                'type': 'issue',
                'ref': req_no,
                'title': '发出',
                'detail': f"{req_no} 发出 {_fmt_qty(coil.get('coil_length'))} {unit}".strip(),
                'qty': _r4(coil.get('coil_length')),
                'unit': coil.get('unit'),
            })

        # 2) 工单消耗 / 报废（consume_type 不做枚举硬编码，未知类型原样显示）
        cur.execute(
            "SELECT * FROM kr_wire_coil_consumption WHERE coil_id = %s "
            "ORDER BY created_at, id",
            (coil_id,)
        )
        for r in cur.fetchall():
            ctype = (r.get('consume_type') or '').strip()
            ctype_l = ctype.lower()
            stage = (r.get('stage') or '').strip()
            stage_txt = f'（{stage}）' if stage else ''
            unit = r.get('unit') or ''
            job = (r.get('job_order') or '').strip()
            if 'scrap' in ctype_l:
                scrap_mm = r.get('scrap_length_actual')
                if scrap_mm is None:
                    scrap_mm = r.get('out_length')
                # out_length / scrap_length_actual 存的是 mm；converted_* 才是业务单位（FT 等）
                conv = r.get('converted_length')
                out_mm = r.get('out_length')
                conv_unit = (r.get('converted_unit') or '').strip() or unit
                qty, qty_unit, mm_txt = scrap_mm, 'mm', ''
                if conv is not None and out_mm and scrap_mm is not None:
                    try:
                        qty = float(scrap_mm) * float(conv) / float(out_mm)
                        qty_unit = conv_unit
                        mm_txt = f"（{_fmt_qty(scrap_mm)} mm）"
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                if job:
                    detail = f"工单 {job} 报废 {_fmt_qty(qty)} {qty_unit}{mm_txt}{stage_txt}"
                else:
                    detail = f"报废 {_fmt_qty(qty)} {qty_unit}{mm_txt}{stage_txt}"
                timeline.append({
                    'time': _iso(r.get('created_at')),
                    'type': 'scrap',
                    'ref': job or ctype,
                    'title': '报废',
                    'detail': detail,
                    'qty': _r4(qty),
                    'unit': qty_unit,
                })
                continue
            # 消耗：out_length 是 mm，converted_length 才是业务单位，别再拿 unit 套 mm 数值
            out_mm = r.get('out_length')
            conv = r.get('converted_length')
            conv_unit = (r.get('converted_unit') or '').strip() or unit
            if conv is not None:
                qty, qty_unit = conv, conv_unit
                mm_txt = f"（{_fmt_qty(out_mm)} mm）" if out_mm is not None else ''
            else:
                qty, qty_unit, mm_txt = out_mm, 'mm', ''
            title = '工单消耗' if ctype_l in ('consumption', 'issue') else f'消耗（{ctype}）'
            if job:
                detail = f"工单 {job} 消耗 {_fmt_qty(qty)} {qty_unit}{mm_txt}{stage_txt}"
            else:
                detail = f"消耗 {_fmt_qty(qty)} {qty_unit}{mm_txt}{stage_txt}"
            timeline.append({
                'time': _iso(r.get('created_at')),
                'type': 'consume',
                'ref': job or ctype,
                'title': title,
                'detail': detail,
                'qty': _r4(qty),
                'unit': qty_unit,
            })

        # 3) 退料
        cur.execute(
            "SELECT * FROM kr_return_item WHERE coil_id = %s ORDER BY created_at, id",
            (coil_id,)
        )
        for r in cur.fetchall():
            rno = _request_no(cur, r.get('request_id'), req_cache)
            review = r.get('review_status')
            review_txt = RETURN_REVIEW_LABELS.get(review, review or '待审核')
            qty = r.get('remain_length')
            unit = r.get('unit') or ''
            timeline.append({
                'time': _iso(r.get('created_at')),
                'type': 'return',
                'ref': rno,
                'title': '退回',
                'detail': f"退回{rno}，剩余 {_fmt_qty(qty)} {unit}（{review_txt}）",
                'qty': _r4(qty),
                'unit': r.get('unit'),
            })

        # 4) 盘点盈亏：diff_mm > 0 盘盈，< 0 盘亏，= 0 忽略
        cur.execute(
            "SELECT i.*, c.count_no FROM kr_inventory_count_item i "
            "LEFT JOIN kr_inventory_count c ON c.id = i.count_id "
            "WHERE i.coil_id = %s ORDER BY i.id",
            (coil_id,)
        )
        for r in cur.fetchall():
            diff = _r4(r.get('diff_mm')) or 0
            if diff == 0:
                continue
            gain = diff > 0
            ref = r.get('count_no') or f"盘点单#{r.get('count_id')}"
            sign = '+' if gain else ''
            detail = (
                f"实测 {_fmt_qty(r.get('actual_mm'))}，"
                f"差异 {sign}{_fmt_qty(diff)}（换算 {sign}{_fmt_qty(r.get('diff_converted'))}）"
            )
            timeline.append({
                'time': _iso(r.get('measured_at') or r.get('created_at')),
                'type': 'count_gain' if gain else 'count_loss',
                'ref': ref,
                'title': '盘盈' if gain else '盘亏',
                'detail': detail,
                'qty': abs(diff),
                'unit': 'mm',
            })

    timeline.sort(key=lambda e: (e['time'] is None, e['time'] or ''))

    return jsonify({
        'success': True,
        'coil': {
            'coil_id': coil.get('coil_id'),
            'part_number': coil.get('part_number'),
            'lot_no': coil.get('lot_no'),
            'coil_length': _r4(coil.get('coil_length')),
            'unit': coil.get('unit'),
            'loc': coil.get('loc'),
            'status': coil.get('status'),
            'status_label': status_label,
            'request_id': coil.get('request_id'),
            'request_no': req_no,
            'prev_request_id': coil.get('prev_request_id'),
            'prev_request_no': prev_req_no,
            'operator': coil.get('operator'),
            'remark': coil.get('remark'),
            'created_at': _iso(coil.get('created_at')),
            'updated_at': _iso(coil.get('updated_at')),
        },
        'timeline': timeline,
    })


@wire_bp.route('/api/wire/coils/export', methods=['GET'])
def export_coils():
    user, err = _check_login()
    if err:
        return err
    where, params = _build_coil_query(user)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT c.*, "
            f"COALESCE((SELECT SUM(out_length) FROM kr_wire_coil_consumption k "
            f"WHERE k.coil_id = c.coil_id AND k.consume_type IN ('consumption','count_adjust')), 0) AS used_mm "
            f"FROM kr_wire_coil c WHERE {where} ORDER BY c.id DESC",
            params
        )
        rows = cur.fetchall()
        # 活跃盘点中的卷标集合（派生状态：盘点中）
        counting_ids = set()
        if rows:
            cur.execute(
                "SELECT DISTINCT i.coil_id FROM kr_inventory_count_item i "
                "JOIN kr_inventory_count c ON c.id = i.count_id "
                "WHERE c.status = 'counting'"
            )
            counting_ids = {r['coil_id'] for r in cur.fetchall()}
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['卷标ID', '物料', 'Lot', '卷长', '单位', '状态', '站点', '申请单ID', '使用数量(mm)', '创建时间', '最后更新时间'])
    for r in rows:
        status_label = COIL_STATUS_LABELS.get(r.get('status'), r.get('status') or '')
        if r.get('coil_id') in counting_ids:
            status_label = '盘点中'
        writer.writerow([
            r.get('coil_id', ''), r.get('part_number', ''), r.get('lot_no', ''),
            r.get('coil_length', ''), r.get('unit', ''),
            status_label,
            r.get('siteref', ''), r.get('request_id', ''),
            r.get('used_mm', ''), str(r.get('created_at') or ''), str(r.get('updated_at') or ''),
        ])
    return Response(
        '\ufeff' + buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=coils.csv'}
    )


# ================================================================== #
#  消耗查询
# ================================================================== #
def _build_consumption_query(user=None):
    wb = WhereBuilder(['1=1'])
    args = request.args
    if args.get('coil_id'):
        wb.add('c.coil_id LIKE %s', f"%{args['coil_id'].strip()}%")
    if args.get('part_number'):
        wb.add('c.part_number LIKE %s', f"%{args['part_number'].strip()}%")
    if args.get('job_order'):
        wb.add('c.job_order LIKE %s', f"%{args['job_order'].strip()}%")
    if args.get('consume_type'):
        wb.add('c.consume_type = %s', args['consume_type'].strip())
    if args.get('date_from'):
        wb.add('DATE(c.created_at) >= %s', args['date_from'].strip())
    if args.get('date_to'):
        wb.add('DATE(c.created_at) <= %s', args['date_to'].strip())
    # 站点隔离：session 当前站点（get_site_filter 对无 siteref 的 admin 不过滤）
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f'c.{site_filter}', *site_params)
    return wb.build()


@wire_bp.route('/api/wire/consumption', methods=['GET'])
def api_consumption():
    user, err = _check_login()
    if err:
        return err
    where, params = _build_consumption_query(user)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT c.* FROM kr_wire_coil_consumption c WHERE {where} "
            f"ORDER BY c.id DESC LIMIT 500",
            params
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('out_length', 'converted_length', 'shear_length', 'length_tolerance'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        data.append(d)
    return jsonify({'success': True, 'data': data, 'total': len(data)})


@wire_bp.route('/api/wire/consumption/export', methods=['GET'])
def export_consumption():
    user, err = _check_login()
    if err:
        return err
    where, params = _build_consumption_query(user)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT c.* FROM kr_wire_coil_consumption c WHERE {where} ORDER BY c.id DESC",
            params
        )
        rows = cur.fetchall()
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['卷标ID', '物料', '工单', '消耗类型', '出库长度(mm)', '单位',
                     '转换长度', '转换单位', 'Lot批次', '登记人', '登记时间'])
    for r in rows:
        writer.writerow([
            r.get('coil_id', ''), r.get('part_number', ''), r.get('job_order', ''),
            r.get('consume_type', ''), r.get('out_length', ''), r.get('unit', ''),
            r.get('converted_length', ''), r.get('converted_unit', ''),
            r.get('lot_no', '') or '', r.get('operator', ''), str(r.get('created_at') or ''),
        ])
    return Response(
        '\ufeff' + buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=consumption.csv'}
    )


# ================================================================== #
#  盘点差异调整：把盘点差异写入卷标消耗表（工单号=盘点单号）
# ================================================================== #
@wire_bp.route('/wire/adjust', methods=['GET'])
def adjust_page():
    if not session.get('user'):
        from flask import redirect, url_for
        return redirect(url_for('pages.login_page'))
    # 盘点差异调整仅限仓库人员/管理员操作
    if session['user']['role'] not in ('warehouse', 'admin'):
        return '权限不足', 403
    return render_template('wire_adjust.html')


def _resolve_adjust_location(cur, siteref, coil_id, part_number, lot_no):
    """盘点差异调整的 CSI 库位：以「这一卷」的实际位置为准，不用 lot 库位。

    顺序：
      1. 卷标 status = 'in_shop'（已领用到车间）→ Config.CSI_TO_LOCATION（Floor-Core）
      2. 卷标 status = 'in_stock'（在仓）→ kr_wire_coil.loc
      3. 卷标 loc 为空 → 该卷标最近一条 SUCCESS 的 sl-qty-move 的 to_location
      4. 全部失败 → CSI get_lot_location 兜底（并记 warning，说明可能是跨库位 lot）
      5. 仍为空 → None（调用方报错回滚）

    背景：同一 lot 拆多卷后可能分别位于不同库位，SLLots.Loc 只是 lot 主记录的
    「最后位置」，按 lot 取库位会导致账实不符（如卷标 261022017 已在 Floor-Core，
    lot 库位仍返回 RM-P040-1）。
    """
    cur.execute(
        "SELECT status, loc FROM kr_wire_coil WHERE coil_id = %s ORDER BY is_deleted LIMIT 1",
        (coil_id,)
    )
    row = cur.fetchone() or {}
    if not row:
        logger.warning('[盘点差异调整] 卷标不存在，无法确定库位 coil_id=%s', coil_id)
        return None
    status = str(row.get('status') or '').strip().lower()
    loc = str(row.get('loc') or '').strip()

    # 1. 已领用到车间：实物必在车间库位，无需再查任何 CSI 接口
    if status == 'in_shop':
        shop_loc = (Config.CSI_TO_LOCATION or '').strip()
        if shop_loc:
            return shop_loc
        logger.warning('[盘点差异调整] 未配置 CSI_TO_LOCATION，车间卷标无库位 coil_id=%s', coil_id)
        return None

    # 2. 在仓（或其他状态）：以卷标自身库位为准
    if loc:
        return loc

    # 3. 卷标库位为空：取该卷标最近一次成功的库位转移目标
    cur.execute(
        "SELECT to_location FROM kr_csi_interface "
        "WHERE coil_id = %s AND api_name = 'sl-qty-move' AND status = 'SUCCESS' "
        "ORDER BY id DESC LIMIT 1",
        (coil_id,)
    )
    moved = cur.fetchone() or {}
    loc = str(moved.get('to_location') or '').strip()
    if loc:
        logger.warning('[盘点差异调整] 卷标无本地库位，采用最近一次库位转移目标 coil_id=%s loc=%s',
                       coil_id, loc)
        return loc

    # 4. 兜底：lot 库位（同一 lot 可跨多库位，仅供最后兜底）
    try:
        lot_loc = str(CSIClient(siteref).get_lot_location(part_number, lot_no) or '').strip()
    except Exception as e:
        logger.warning('[盘点差异调整] CSI 库位查询失败 item=%s lot=%s: %s', part_number, lot_no, e)
        lot_loc = ''
    if lot_loc:
        logger.warning('[盘点差异调整] 跨库位 lot，回退 lot 库位 coil_id=%s item=%s lot=%s loc=%s',
                       coil_id, part_number, lot_no, lot_loc)
        return lot_loc
    return None


@wire_bp.route('/api/wire/adjust/preview', methods=['GET'])
def api_count_adjust_preview():
    """盘点差异调整预览（只读，不写库）。

    返回汇总（总卷数、差异笔数、盘盈/盘亏金额、净影响）与明细。
    调整金额 = |diff_converted| × CSI 单位成本（DerUnitCost），同一物料只查一次；
    查不到成本的明细 unit_cost/amount 为 None，计入 unpriced_items，不阻断预览。
    """
    user, err = _check_login()
    if err:
        return err
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '仅仓库/管理员可预览盘点差异调整'}), 403
    count_no = (request.args.get('count_no') or '').strip().upper()
    if not count_no:
        return jsonify({'success': False, 'message': '请输入盘点单号'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE count_no = %s", (count_no,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': f'盘点单 {count_no} 不存在'}), 404
        if count['status'] == 'completed':
            cur.close()
            return jsonify({'success': False, 'message': f'盘点单 {count_no} 已执行过差异调整'}), 400
        if count['status'] != 'pending_adjust':
            cur.close()
            return jsonify({'success': False,
                            'message': f'盘点单 {count_no} 未完成盘点（当前状态：{count["status"]}），无法预览差异调整'}), 400
        # 防重复：已存在调整消耗记录（历史单可能状态仍为 pending_adjust）
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_wire_coil_consumption "
            "WHERE consume_type = 'count_adjust' AND job_order = %s",
            (count_no,)
        )
        if cur.fetchone()['n'] > 0:
            cur.close()
            return jsonify({'success': False, 'message': f'盘点单 {count_no} 已执行过差异调整，请勿重复执行'}), 400
        cur.execute(
            "SELECT * FROM kr_inventory_count_item WHERE count_id = %s ORDER BY id",
            (count['id'],)
        )
        rows = cur.fetchall()
        cur.close()

    siteref = str(count.get('siteref') or user.get('siteref') or '')
    items = []
    diff_items = 0
    plus_count = 0
    minus_count = 0

    for it in rows:
        raw_diff = it.get('diff_converted')
        diff_converted = float(raw_diff) if raw_diff is not None else None
        api_name = None
        if diff_converted:
            api_name = (COUNT_ADJUST_API_POSITIVE if diff_converted > 0
                        else COUNT_ADJUST_API_NEGATIVE)
            diff_items += 1
            if diff_converted > 0:
                plus_count += 1
            else:
                minus_count += 1

        items.append({
            'coil_id': it.get('coil_id'),
            'part_number': it.get('part_number'),
            'lot_no': it.get('lot_no'),
            'unit': it.get('unit'),
            'original_qty': float(it['original_qty']) if it.get('original_qty') is not None else None,
            'used_mm': float(it['used_mm']) if it.get('used_mm') is not None else None,
            'remain_mm': float(it['remain_mm']) if it.get('remain_mm') is not None else None,
            'actual_mm': float(it['actual_mm']) if it.get('actual_mm') is not None else None,
            'diff_mm': float(it['diff_mm']) if it.get('diff_mm') is not None else None,
            'diff_converted': diff_converted,
            'api_name': api_name,
        })

    return jsonify({'success': True, 'data': {
        'count_no': count_no,
        'status': count['status'],
        'siteref': siteref,
        'total_items': len(rows),
        'diff_items': diff_items,
        'plus_count': plus_count,
        'minus_count': minus_count,
        'items': items,
    }})


def _execute_count_adjust(cur, count, items, operator, siteref, now):
    """执行盘点差异调整：写卷标消耗 + CSI 杂项收发交易（不 commit，异常向上抛）。

    调用方负责：事务提交/回滚、重复执行校验、站点仓库映射的 400 前置校验。
    返回 {'consume_inserted': n, 'csi_inserted': m}。
    ValueError 表示业务性失败（单号超长 / 无法确定库位 / 缺仓库映射），由调用方转 400。
    """
    count_no = count['count_no']
    warehouse = Config.CSI_WAREHOUSE_MAP.get(siteref)
    if not warehouse:
        raise ValueError(f'站点 {siteref} 未配置 CSI 仓库映射，无法生成杂项交易')

    inserted = 0
    csi_inserted = 0
    # 行号续接该盘点单已用过的最大行号（冲销后重调时避免与已过账单号冲突）
    cur.execute(
        "SELECT documentnumber FROM kr_csi_interface WHERE documentnumber LIKE %s",
        (f'{count_no}%',)
    )
    used_seqs = []
    for r in cur.fetchall():
        suffix = (r.get('documentnumber') or '')[-3:]
        if suffix.isdigit():
            used_seqs.append(int(suffix))
    line_no = max(used_seqs) if used_seqs else 0

    for it in items:
        diff_mm = it.get('diff_mm')
        if diff_mm is None or float(diff_mm) == 0:
            continue  # 无差异不写消耗
        # 正确语义：count_adjust = 账面剩余R - 盘点实际A = -diff（取反）
        # 剩余 = 卷长 - 消耗 - (-diff) = R + diff = A（精确等于盘点实际值）
        # 若直接写 diff 会导致剩余 = 2R - A（A=0 时翻倍）
        adjust_mm = -float(diff_mm)
        adjust_converted = it.get('diff_converted')
        if adjust_converted is not None:
            adjust_converted = -float(adjust_converted)
        cur.execute(
                """INSERT INTO kr_wire_coil_consumption
                   (coil_id, job_order, part_number, consume_type, out_length, unit,
                    converted_length, converted_unit, operator, remark, created_at, is_manual, siteref)
                   VALUES (%s, %s, %s, 'count_adjust', %s, %s, %s, %s, %s, %s, %s, 1, %s)""",
                (it['coil_id'], count_no, it.get('part_number'),
                 adjust_mm, it.get('unit'),
                 adjust_converted, it.get('unit'),
                 operator, f'盘点差异调整（{count_no}）', now,
                 count.get('siteref') or siteref)
            )
        inserted += 1

        # ---- CSI 杂项收发交易（与消耗记录同事务）----
        # diff_converted 为空（计量单位无换算系数）时无法折算，跳过该卷
        diff_converted = it.get('diff_converted')
        if diff_converted is None:
            continue
        diff_converted = float(diff_converted)
        quantity = round(abs(diff_converted), 4)
        if quantity <= 0:
            continue
        api_name = (COUNT_ADJUST_API_POSITIVE if diff_converted > 0
                    else COUNT_ADJUST_API_NEGATIVE)
        line_no += 1
        documentnumber = f'{count_no}{line_no:03d}'
        if len(documentnumber) > CSI_DOC_MAX_LEN:
            raise ValueError(
                f'CSI 单据号 {documentnumber} 长度 {len(documentnumber)} 位，'
                f'超过接口上限 {CSI_DOC_MAX_LEN} 位（盘点单 {count_no}）'
            )
        to_location = _resolve_adjust_location(
            cur, siteref, it['coil_id'], it.get('part_number'), it.get('lot_no'))
        if not to_location:
            raise ValueError(
                f'无法确定 CSI 库位：卷标 {it["coil_id"]}'
                f'（物料 {it.get("part_number")} / Lot {it.get("lot_no")}）'
            )
        traceid = f'{documentnumber}-{it["coil_id"]}-{uuid.uuid4().hex[:12]}'
        cur.execute(
            """INSERT INTO kr_csi_interface
               (request_id, item_id, coil_id, lot_id, api_name, siteRef, item, lot, quantity,
                um, warehouse, from_location, to_location, reasoncode, account,
                unitcode1, unitcode2, documentnumber, line_seq, direction,
                is_sync, status, retry_count, traceid, createdate, createby)
               VALUES (NULL, %s, %s, NULL, %s, %s, %s, %s, %s,
                       %s, %s, NULL, %s, %s, %s,
                       NULL, NULL, %s, %s, 'F',
                       0, 'PENDING', 0, %s, %s, %s)""",
            (it['id'], it['coil_id'], api_name, int(siteref),
             it.get('part_number'), it.get('lot_no'), quantity,
             it.get('unit'), warehouse, to_location, COUNT_ADJUST_REASON_CODE,
             COUNT_ADJUST_ACCOUNT, documentnumber, line_no,
             traceid, now, operator)
        )
        csi_inserted += 1
    # 差异调整完成 → 盘点单置为已完成（completed_at 保留盘点完成时间）
    cur.execute(
        "UPDATE kr_inventory_count SET status = 'completed' WHERE id = %s AND status = 'pending_adjust'",
        (count['id'],)
    )
    return {'consume_inserted': inserted, 'csi_inserted': csi_inserted}


@wire_bp.route('/api/wire/adjust', methods=['POST'])
def api_count_adjust():
    """仓库人员输入盘点单号 → 将盘点差异写入卷标消耗表（consume_type='count_adjust'），
    同时为每条有差异的明细写入 CSI 杂项收发交易（kr_csi_interface）。

    消耗数量 = 差异数量(diff_mm)，可正可负；工单号 = 盘点单号。
    CSI 交易：diff_converted > 0 → sl-misc-issue；< 0 → sl-misc-receipt；
    单号 = 盘点单号 + 3 位行号（如 INV260001001，≤12 位）。
    同一盘点单号只允许执行一次（防重复）；消耗记录与接口记录同事务提交。
    """
    user, err = _check_login()
    if err:
        return err
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '仅仓库/管理员可执行盘点差异调整'}), 403
    data = request.get_json() or {}
    count_no = (data.get('count_no') or '').strip().upper()
    if not count_no:
        return jsonify({'success': False, 'message': '请输入盘点单号'}), 400
    # 防绕过：必须由前端确认弹窗显式确认本次调整影响 ERP 库存金额
    if data.get('confirm') is not True:
        return jsonify({'success': False, 'message': '请先确认本次调整将影响 ERP 库存金额'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE count_no = %s", (count_no,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': f'盘点单 {count_no} 不存在'}), 404
        # 待调整（盘点完成）才允许调整；兼容历史 completed 单（靠下方消耗表防重复）
        if count['status'] not in ('pending_adjust', 'completed'):
            cur.close()
            return jsonify({'success': False,
                            'message': f'盘点单 {count_no} 未完成盘点（当前状态：{count["status"]}），无法执行差异调整'}), 400

        # 防重复：同盘点单号已执行过
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_wire_coil_consumption "
            "WHERE consume_type = 'count_adjust' AND job_order = %s",
            (count_no,)
        )
        if cur.fetchone()['n'] > 0:
            cur.close()
            return jsonify({'success': False, 'message': f'盘点单 {count_no} 已执行过差异调整，请勿重复执行'}), 400

        cur.execute(
            "SELECT * FROM kr_inventory_count_item WHERE count_id = %s ORDER BY id",
            (count['id'],)
        )
        items = cur.fetchall()
        if not items:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单无明细'}), 400

        now = datetime.now()
        operator = user.get('username')
        siteref = str(count.get('siteref') or user.get('siteref') or '')
        warehouse = Config.CSI_WAREHOUSE_MAP.get(siteref)
        if not warehouse:
            cur.close()
            return jsonify({'success': False,
                            'message': f'站点 {siteref} 未配置 CSI 仓库映射，无法生成杂项交易'}), 400

        try:
            result = _execute_count_adjust(cur, count, items, operator, siteref, now)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.exception('[盘点差异调整] 盘点单 %s 执行失败，已回滚：%s', count_no, e)
            cur.close()
            return jsonify({'success': False, 'message': f'差异调整失败，已回滚：{e}'}), 500
        cur.close()
    inserted = result['consume_inserted']
    csi_inserted = result['csi_inserted']
    return jsonify({'success': True,
                    'message': f'盘点单 {count_no} 共 {len(items)} 卷，{inserted} 卷有差异：'
                               f'已生成 CSI 库存调整交易 {csi_inserted} 条'
                               f'（无差异的 {len(items) - inserted} 卷不产生交易）',
                    'total_items': len(items),
                    'consumption_rows': inserted, 'csi_rows': csi_inserted})


# ================================================================== #
#  线材库存盘点
# ================================================================== #
def _gen_count_no(cur):
    """生成盘点单号：INV + 年份后两位 + 4 位年度流水（如 INV260001，共 9 位）。

    流水号取该年度已有单号的最大后缀 + 1（年度内递增，不按日重置、不复用）。
    """
    prefix = datetime.now().strftime('INV%y')
    cur.execute(
        "SELECT count_no FROM kr_inventory_count WHERE count_no LIKE %s",
        (prefix + '%',)
    )
    max_seq = 0
    for r in cur.fetchall():
        suffix = (r.get('count_no') or '')[len(prefix):]
        if len(suffix) == 4 and suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f'{prefix}{max_seq + 1:04d}'


def _gen_gain_count_no(cur):
    """生成盘盈自动盘点单号：IS + 年份后两位 + 5 位年度流水（如 IS2600001，共 9 位）。

    流水号取该年度已有 IS 单号的最大后缀 + 1（年度内递增，不按日重置、不复用）。
    """
    prefix = datetime.now().strftime('IS%y')
    cur.execute(
        "SELECT count_no FROM kr_inventory_count WHERE count_no LIKE %s",
        (prefix + '%',)
    )
    max_seq = 0
    for r in cur.fetchall():
        suffix = (r.get('count_no') or '')[len(prefix):]
        if len(suffix) == 5 and suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f'{prefix}{max_seq + 1:05d}'


def _calc_item_metrics(coil, used_mm):
    """计算盘点明细指标（返回 dict）"""
    factor = _factor(coil.get('unit'))
    original = float(coil.get('coil_length') or 0)
    used = float(used_mm or 0)
    remain_mm = round(original * factor - used, 4) if factor else None
    return {
        'original_qty': original,
        'unit': coil.get('unit') or '',
        'used_mm': used,
        'remain_mm': remain_mm,
        'factor': factor,
    }


@wire_bp.route('/api/wire/counts', methods=['GET'])
def api_counts():
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_inventory_count ORDER BY id DESC LIMIT 100"
        )
        rows = cur.fetchall()
        for r in rows:
            cur.execute(
                "SELECT COUNT(*) AS n FROM kr_inventory_count_item WHERE count_id = %s",
                (r['id'],)
            )
            r['item_count'] = cur.fetchone()['n']
    return jsonify({'success': True, 'data': rows})


@wire_bp.route('/api/wire/counts', methods=['POST'])
def api_create_count():
    user, err = _check_login()
    if err:
        return err
    data = request.get_json() or {}
    coil_ids = [str(x).strip() for x in (data.get('coil_ids') or []) if str(x).strip()]
    note = (data.get('note') or '').strip()
    if not coil_ids:
        return jsonify({'success': False, 'message': '请选择要盘点的卷标'}), 400
    if len(coil_ids) > 200:
        return jsonify({'success': False, 'message': '单次最多盘点 200 卷'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        ph = ','.join(['%s'] * len(coil_ids))
        cur.execute(
            f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({ph}) AND is_deleted = 0",
            coil_ids
        )
        coils = cur.fetchall()
        if not coils:
            cur.close()
            return jsonify({'success': False, 'message': '所选卷标不存在'}), 400
        coil_map = {c['coil_id']: c for c in coils}

        # 仅线边仓（在车间 in_shop）卷标可盘点；仓库（在库 in_stock）不允许
        not_allowed = [cid for cid in coil_ids
                       if cid in coil_map and coil_map[cid].get('status') != 'in_shop']
        if not_allowed:
            cur.close()
            names = ', '.join(not_allowed[:5])
            return jsonify({'success': False,
                            'message': f'卷标 {names} 不在线边仓（状态非在车间），仅线边仓物料可盘点'}), 400

        # 禁止重复选择同一卷标
        # 单号唯一键冲突时重新取号重试（并发建单：两个请求可能算出同一年度流水）
        count_no = count_id = None
        for _attempt in range(5):
            count_no = _gen_count_no(cur)
            try:
                cur.execute(
                    "INSERT INTO kr_inventory_count (count_no, status, siteref, created_by, note) "
                    "VALUES (%s, 'pending', %s, %s, %s)",
                    (count_no, user.get('siteref') or '410', user.get('username'), note or None)
                )
            except IntegrityError:
                db.rollback()
                count_no = count_id = None
                continue
            count_id = cur.lastrowid
            break
        if not count_id:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单号生成失败，请重试'}), 500
        inserted = 0
        for cid in coil_ids:
            coil = coil_map.get(cid)
            if not coil:
                continue
            cur.execute(
                "SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption "
                "WHERE coil_id = %s AND consume_type IN ('consumption','count_adjust')",
                (cid,)
            )
            used_mm = float(cur.fetchone()['used'] or 0)
            m = _calc_item_metrics(coil, used_mm)
            cur.execute(
                "INSERT INTO kr_inventory_count_item "
                "(count_id, coil_id, part_number, lot_no, unit, original_qty, used_mm, remain_mm) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (count_id, cid, coil.get('part_number'), coil.get('lot_no'),
                 m['unit'], m['original_qty'], m['used_mm'], m['remain_mm'])
            )
            inserted += 1
        db.commit()
    return jsonify({'success': True, 'message': f'盘点单 {count_no} 已创建（{inserted} 卷）', 'id': count_id})


@wire_bp.route('/api/wire/counts/<int:count_id>', methods=['GET'])
def api_count_detail(count_id):
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        cur.execute(
            "SELECT i.*, c.status AS coil_status FROM kr_inventory_count_item i "
            "LEFT JOIN kr_wire_coil c ON c.coil_id = i.coil_id "
            "WHERE i.count_id = %s ORDER BY i.id",
            (count_id,)
        )
        items = cur.fetchall()
        for it in items:
            for f in ('original_qty', 'used_mm', 'remain_mm', 'actual_mm', 'diff_mm', 'diff_converted'):
                if it.get(f) is not None:
                    it[f] = float(it[f])
    return jsonify({'success': True, 'data': {'count': count, 'items': items}})


@wire_bp.route('/api/wire/counts/<int:count_id>', methods=['DELETE'])
def api_delete_count(count_id):
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        if count['status'] != 'pending':
            cur.close()
            return jsonify({'success': False, 'message': '仅待开始的盘点单可删除'}), 400
        cur.execute("DELETE FROM kr_inventory_count_item WHERE count_id = %s", (count_id,))
        cur.execute("DELETE FROM kr_inventory_count WHERE id = %s", (count_id,))
        db.commit()
    return jsonify({'success': True, 'message': '盘点单已删除'})


@wire_bp.route('/api/wire/counts/<int:count_id>/start', methods=['POST'])
def api_count_start(count_id):
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        if count['status'] != 'pending':
            cur.close()
            return jsonify({'success': False, 'message': '仅待开始的盘点单可开启盘点'}), 400
        cur.execute(
            "UPDATE kr_inventory_count SET status = 'counting', started_at = %s WHERE id = %s",
            (datetime.now(), count_id)
        )
        db.commit()
    return jsonify({'success': True, 'message': f'盘点单 {count["count_no"]} 已开启，卷标已锁定（不允许消耗）'})


@wire_bp.route('/api/wire/counts/<int:count_id>/items/<int:item_id>/measure', methods=['POST'])
def api_count_measure(count_id, item_id):
    user, err = _check_login()
    if err:
        return err
    data = request.get_json() or {}
    try:
        actual_mm = round(float(data.get('actual_mm')), 4)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '盘点实际数量格式无效'}), 400
    if actual_mm < 0:
        return jsonify({'success': False, 'message': '盘点实际数量不能为负'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        if count['status'] != 'counting':
            cur.close()
            return jsonify({'success': False, 'message': '盘点单未开启或已结束'}), 400
        cur.execute(
            "SELECT i.* FROM kr_inventory_count_item i WHERE i.id = %s AND i.count_id = %s",
            (item_id, count_id)
        )
        it = cur.fetchone()
        if not it:
            cur.close()
            return jsonify({'success': False, 'message': '盘点明细不存在'}), 404
        factor = _factor(it.get('unit'))
        remain_mm = it.get('remain_mm')
        diff_mm = round(actual_mm - (float(remain_mm) if remain_mm is not None else 0), 4)
        diff_converted = round(diff_mm / factor, 4) if factor else None
        cur.execute(
            "UPDATE kr_inventory_count_item SET actual_mm = %s, diff_mm = %s, diff_converted = %s, "
            "measured_by = %s, measured_at = %s WHERE id = %s",
            (actual_mm, diff_mm, diff_converted, user.get('username'), datetime.now(), item_id)
        )
        db.commit()
    return jsonify({'success': True, 'message': '已录入盘点数量', 'diff_mm': diff_mm, 'diff_converted': diff_converted})


@wire_bp.route('/api/wire/counts/<int:count_id>/complete', methods=['POST'])
def api_count_complete(count_id):
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        if count['status'] != 'counting':
            cur.close()
            return jsonify({'success': False, 'message': '盘点单未开启或已结束'}), 400
        cur.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN actual_mm IS NOT NULL THEN 1 ELSE 0 END) AS done "
            "FROM kr_inventory_count_item WHERE count_id = %s",
            (count_id,)
        )
        agg = cur.fetchone()
        if agg['total'] != agg['done']:
            cur.close()
            return jsonify({'success': False, 'message': f'还有 {agg["total"] - agg["done"]} 卷未录入盘点数量'}), 400
        # 盘点完成 → 待调整；差异调整执行成功后再置为 completed（见 api_count_adjust）
        cur.execute(
            "UPDATE kr_inventory_count SET status = 'pending_adjust', completed_at = %s WHERE id = %s",
            (datetime.now(), count_id)
        )
        db.commit()
    return jsonify({'success': True,
                    'message': f'盘点单 {count["count_no"]} 盘点完成，待差异调整'})


# ------------------------------------------------------------------ #
#  PDF 报告（全英文 + CSI 标准成本差异金额 + 单号条形码）
# ------------------------------------------------------------------ #
#  字体策略：只使用 PDF 内置 Helvetica / Helvetica-Bold。
#  生产环境是 CentOS，没有任何中文 TTF，注册 Windows 字体路径必然失败并回退，
#  中文随即变成方块/乱码。因此报告内容全部改为 ASCII 英文，不再依赖系统字体。
REPORT_FONT = 'Helvetica'
REPORT_FONT_BOLD = 'Helvetica-Bold'


def _fmt_cell(value, digits=2):
    """数值单元格格式化：None/空 → ''，其余按指定小数位输出（兼容 Decimal）"""
    if value is None or value == '':
        return ''
    try:
        return f'{float(value):.{digits}f}'
    except (TypeError, ValueError):
        return str(value)


def _fetch_unit_costs(siteref, part_numbers):
    """按物料去重查询 CSI 标准成本；同一 client 实例复用，查不到/异常记 None"""
    costs = {}
    client = None
    for pn in part_numbers:
        if not pn or pn in costs:
            continue
        try:
            if client is None:
                client = CSIClient(siteref or '310')
            costs[pn] = client.get_item_cost(pn)
        except Exception as e:      # 成本查询失败不阻断报告
            logger.warning('[REPORT] get_item_cost(%s) failed: %s', pn, e)
            costs[pn] = None
    return costs


def _build_report_pdf(count, items):
    """生成「Shopfloor Inventory Count Report」PDF。

    - 全英文、内置 Helvetica，跨平台不依赖字体文件
    - 每行差异金额 = |diff_converted| x CSI 标准成本
    - 汇总盘盈/盘亏金额与净影响，取不到成本的行计入 Unpriced Items
    - 标题区嵌入 Code128 单号条形码
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.graphics.barcode import createBarcodeDrawing

    page_w, page_h = landscape(A4)
    margin_lr = 12 * mm

    # ---- 成本查询：按物料去重，整体只用一个 CSIClient ------------------ #
    part_numbers = []
    for it in items:
        pn = it.get('part_number')
        if pn and pn not in part_numbers:
            part_numbers.append(pn)
    unit_costs = _fetch_unit_costs(count.get('siteref'), part_numbers)

    # ---- 逐行计算差异数量 / 金额 -------------------------------------- #
    rows_data = []
    gain_amount = 0.0
    loss_amount = 0.0
    diff_items = 0
    unpriced_items = 0
    sum_orig = sum_used = sum_remain = sum_actual = sum_diff_mm = sum_diff_qty = 0.0

    for it in items:
        diff_converted = it.get('diff_converted')
        if diff_converted is None and it.get('diff_mm') is not None:
            f = _factor(it.get('unit'))
            if f:
                diff_converted = float(it['diff_mm']) / f
        diff_qty = float(diff_converted) if diff_converted is not None else None

        cost = unit_costs.get(it.get('part_number'))
        amount = None
        if diff_qty is not None and cost is not None:
            amount = round(abs(diff_qty) * float(cost), 2)

        if diff_qty is not None and abs(diff_qty) > 0.000001:
            diff_items += 1
            if cost is None:
                unpriced_items += 1
            elif diff_qty > 0:
                gain_amount += amount
            else:
                loss_amount += amount

        # 合计行累加（忽略空值）
        if it.get('original_qty') is not None:
            sum_orig += float(it['original_qty'])
        if it.get('used_mm') is not None:
            sum_used += float(it['used_mm'])
        if it.get('remain_mm') is not None:
            sum_remain += float(it['remain_mm'])
        if it.get('actual_mm') is not None:
            sum_actual += float(it['actual_mm'])
        if it.get('diff_mm') is not None:
            sum_diff_mm += float(it['diff_mm'])
        if diff_qty is not None:
            sum_diff_qty += diff_qty

        rows_data.append([
            it.get('coil_id') or '',
            it.get('part_number') or '',
            it.get('lot_no') or '',
            _fmt_cell(it.get('original_qty'), 4),
            it.get('unit') or '',
            _fmt_cell(it.get('used_mm'), 4),
            _fmt_cell(it.get('remain_mm'), 4),
            _fmt_cell(it.get('actual_mm'), 4),
            _fmt_cell(it.get('diff_mm'), 4),
            _fmt_cell(diff_qty, 4),
            f'{float(cost):.4f}' if cost is not None else '-',
            f'{amount:.2f}' if amount is not None else '-',
        ])

    net_impact = gain_amount - loss_amount
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    completed_at = count.get('completed_at')
    if hasattr(completed_at, 'strftime'):
        completed_at = completed_at.strftime('%Y-%m-%d %H:%M:%S')
    elif completed_at:
        completed_at = str(completed_at)
    else:
        completed_at = '-'

    # ---- 样式 ---------------------------------------------------------- #
    cell_style = ParagraphStyle('cell', fontName=REPORT_FONT, fontSize=8, leading=10)
    head_style = ParagraphStyle('head', fontName=REPORT_FONT_BOLD, fontSize=7.5,
                                leading=9, alignment=1, textColor=colors.white)
    title_style = ParagraphStyle('title', fontName=REPORT_FONT_BOLD, fontSize=16,
                                 leading=20)
    small_style = ParagraphStyle('small', fontName=REPORT_FONT, fontSize=7, leading=9,
                                 alignment=1)
    label_style = ParagraphStyle('label', fontName=REPORT_FONT_BOLD, fontSize=8.5,
                                 leading=11)
    value_style = ParagraphStyle('value', fontName=REPORT_FONT, fontSize=8.5,
                                 leading=11)
    sign_style = ParagraphStyle('sign', fontName=REPORT_FONT, fontSize=9, leading=13)

    buf = _io.BytesIO()

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(REPORT_FONT, 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(margin_lr, 8 * mm, 'Generated by Material Pickup System')
        canvas.drawRightString(page_w - margin_lr, 8 * mm,
                               f'Generated at {generated_at}    Page {doc_.page}')
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=margin_lr, rightMargin=margin_lr,
        topMargin=12 * mm, bottomMargin=16 * mm,
        title='Shopfloor Inventory Count Report',
        author='Material Pickup System')

    story = []

    # ---- 标题 + Logo + 条形码 -------------------------------------------- #
    import os
    from reportlab.platypus import Image

    count_no = count.get('count_no') or ''
    title_block = [Paragraph('Shopfloor Inventory Count Report', title_style),
                   Spacer(1, 2 * mm),
                   Paragraph(f'Count No.: <b>{count_no}</b>', value_style)]
    # 公司 Logo（随代码分发，不依赖绝对路径）
    logo_cell = []
    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'images', 'logo.png')
    if os.path.exists(logo_path):
        try:
            logo_cell.append(Image(logo_path, width=26 * mm, height=20 * mm))
        except Exception:
            logo_cell = []
    barcode_cell = [Spacer(1, 1 * mm)]
    if count_no:
        drawing = createBarcodeDrawing(
            'Code128', value=count_no, barHeight=12 * mm,
            barWidth=0.28 * mm, humanReadable=False, quiet=True)
        drawing.hAlign = 'CENTER'
        barcode_cell.append(drawing)
        barcode_cell.append(Paragraph(count_no, small_style))
    title_row = Table([[logo_cell, title_block, barcode_cell]],
                      colWidths=[30 * mm, 160 * mm, 77 * mm])
    title_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(title_row)
    story.append(Spacer(1, 5 * mm))

    # ---- 汇总区 --------------------------------------------------------- #
    summary_rows = [
        [Paragraph('Count No.', label_style), Paragraph(str(count_no), value_style),
         Paragraph('Site', label_style), Paragraph(str(count.get('siteref') or '-'), value_style),
         Paragraph('Completed At', label_style), Paragraph(completed_at, value_style)],
        [Paragraph('Total Coils', label_style), Paragraph(str(len(items)), value_style),
         Paragraph('Diff Items', label_style), Paragraph(str(diff_items), value_style),
         Paragraph('Unpriced Items', label_style), Paragraph(str(unpriced_items), value_style)],
        [Paragraph('Gain Amount', label_style), Paragraph(f'{gain_amount:.2f}', value_style),
         Paragraph('Loss Amount', label_style), Paragraph(f'{loss_amount:.2f}', value_style),
         Paragraph('Net Impact', label_style), Paragraph(f'{net_impact:.2f}', value_style)],
    ]
    # 与明细表等宽（257mm），三张表右边缘对齐
    summary = Table(summary_rows, colWidths=[31 * mm, 56 * mm, 31 * mm, 56 * mm, 35 * mm, 58 * mm])
    summary.hAlign = 'LEFT'
    summary.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#BFBFBF')),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F2F2F2')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#F2F2F2')),
        ('BACKGROUND', (4, 0), (4, -1), colors.HexColor('#F2F2F2')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(summary)
    story.append(Spacer(1, 6 * mm))

    # ---- 明细表 --------------------------------------------------------- #
    headers = [Paragraph(h, head_style) for h in (
        'Coil ID', 'Part No.', 'Lot No.', 'Orig Qty', 'Unit', 'Used(mm)',
        'Remain(mm)', 'Counted(mm)', 'Diff(mm)', 'Diff(Qty)', 'Unit Cost',
        'Diff Amount')]
    table_rows = [headers] + rows_data
    table_rows.append([
        'TOTAL', '', '',
        _fmt_cell(sum_orig, 4), '',
        _fmt_cell(sum_used, 4), _fmt_cell(sum_remain, 4),
        _fmt_cell(sum_actual, 4), _fmt_cell(sum_diff_mm, 4),
        _fmt_cell(sum_diff_qty, 4), '', '',
    ])

    col_widths = [w * mm for w in (25, 29, 41, 17, 12, 21, 22, 23, 19, 17, 19, 22)]
    table = Table(table_rows, repeatRows=1, colWidths=col_widths)
    table.hAlign = 'LEFT'
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), REPORT_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (1, -1), 'LEFT'),
        ('ALIGN', (3, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F2F2F2')]),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#FFE699')),
        ('FONTNAME', (0, -1), (-1, -1), REPORT_FONT_BOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    story.append(Spacer(1, 4 * mm))

    note_style = ParagraphStyle('note', fontName=REPORT_FONT, fontSize=7, leading=9,
                                textColor=colors.HexColor('#595959'))
    story.append(Paragraph(
        'Diff(Qty) &gt; 0 = gain (inventory increase); Diff(Qty) &lt; 0 = loss (inventory decrease). '
        'Diff Amount = |Diff(Qty)| x Unit Cost (CSI standard cost). '
        'Unpriced Items are diff rows whose standard cost is unavailable.', note_style))
    story.append(Spacer(1, 8 * mm))

    # ---- 签字区（与汇总表/明细表等宽 257mm）------------------------------- #
    sign_w = 267 * mm
    sign_table = Table([
        [Paragraph('Applicant:', sign_style),
         Paragraph('Dept. Manager:', sign_style),
         Paragraph('Cost Accountant:', sign_style),
         Paragraph('Finance Manager:', sign_style),
         Paragraph('Operations Manager:', sign_style)],
        [Paragraph('Signature:', sign_style), Paragraph('Signature:', sign_style),
         Paragraph('Signature:', sign_style), Paragraph('Signature:', sign_style),
         Paragraph('Signature:', sign_style)],
        [Paragraph('Date:', sign_style), Paragraph('Date:', sign_style),
         Paragraph('Date:', sign_style), Paragraph('Date:', sign_style),
         Paragraph('Date:', sign_style)],
    ], colWidths=[sign_w / 5] * 5)
    sign_table.hAlign = 'LEFT'
    sign_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    story.append(sign_table)

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf


@wire_bp.route('/api/wire/counts/<int:count_id>/report', methods=['GET'])
def api_count_report(count_id):
    user, err = _check_login()
    if err:
        return err
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM kr_inventory_count WHERE id = %s", (count_id,))
        count = cur.fetchone()
        if not count:
            cur.close()
            return jsonify({'success': False, 'message': '盘点单不存在'}), 404
        cur.execute(
            "SELECT * FROM kr_inventory_count_item WHERE count_id = %s ORDER BY id",
            (count_id,)
        )
        items = cur.fetchall()
    pdf = _build_report_pdf(count, items)
    return Response(
        pdf.getvalue(),
        mimetype='application/pdf',
        # 文件名用 ASCII（HTTP 头 latin-1 限制；中文文件名会 UnicodeEncodeError 导致浏览器无响应）
        headers={'Content-Disposition': f'attachment; filename={count["count_no"]}_report.pdf'}
    )
