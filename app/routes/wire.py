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
COUNT_ADJUST_REASON_CODE = 'CC'
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
            remain_mm = round(d['coil_length'] * factor - (d.get('used_mm') or 0), 2)
            d['remain_mm'] = remain_mm
            d['remain_orig'] = round(remain_mm / factor, 2)
        else:
            d['remain_mm'] = None
            d['remain_orig'] = None
        data.append(d)
    # 已消完筛选（派生状态：剩余 ≤ 0）
    if status_filter == 'finished':
        data = [d for d in data if d.get('remain_mm') is not None and d['remain_mm'] <= 0]
    return jsonify({'success': True, 'data': data, 'total': len(data)})


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
    """盘点差异调整的 CSI 库位：以 CSI 实时查询为准，本地卷标库位仅作兜底。

    顺序：
      1. CSI 实时查询 get_lot_location(part_number, lot_no)；
      2. 查询失败/返回空 → 回退 kr_wire_coil.loc（该列只在退料/维护时写入，
         领用出库到车间后不会更新，故不可作为首选）；
      3. 两者都空 → 返回 None（杂项收发必须带库位，由调用方报错回滚整单）。
    """
    try:
        loc = str(CSIClient(siteref).get_lot_location(part_number, lot_no) or '').strip()
    except Exception as e:
        logger.warning('[盘点差异调整] CSI 库位查询失败 item=%s lot=%s: %s', part_number, lot_no, e)
        loc = ''
    if loc:
        return loc

    cur.execute("SELECT loc FROM kr_wire_coil WHERE coil_id = %s ORDER BY is_deleted LIMIT 1",
                (coil_id,))
    row = cur.fetchone() or {}
    loc = str(row.get('loc') or '').strip()
    if loc:
        logger.warning('[盘点差异调整] CSI 无库位，回退本地卷标库位 coil_id=%s loc=%s', coil_id, loc)
    return loc or None


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
    client = CSIClient(siteref)
    cost_cache = {}          # part_number -> unit_cost|None，同一物料只查一次
    items = []
    diff_items = 0
    pl_plus_amount = 0.0
    pl_minus_amount = 0.0
    unpriced_items = 0

    for it in rows:
        raw_diff = it.get('diff_converted')
        diff_converted = float(raw_diff) if raw_diff is not None else None
        api_name = None
        unit_cost = None
        amount = None
        if diff_converted:
            api_name = (COUNT_ADJUST_API_POSITIVE if diff_converted > 0
                        else COUNT_ADJUST_API_NEGATIVE)
            diff_items += 1
            part_number = (it.get('part_number') or '').strip()
            if part_number and part_number not in cost_cache:
                try:
                    cost_cache[part_number] = client.get_item_cost(part_number)
                except Exception as e:
                    logger.warning('[盘点差异调整] 获取 CSI 成本失败 item=%s: %s', part_number, e)
                    cost_cache[part_number] = None
            unit_cost = cost_cache.get(part_number) if part_number else None
            if unit_cost is None:
                unpriced_items += 1
            else:
                amount = round(abs(diff_converted) * float(unit_cost), 2)
                if diff_converted > 0:
                    pl_plus_amount += amount
                else:
                    pl_minus_amount += amount

        items.append({
            'coil_id': it.get('coil_id'),
            'part_number': it.get('part_number'),
            'lot_no': it.get('lot_no'),
            'unit': it.get('unit'),
            'diff_mm': float(it['diff_mm']) if it.get('diff_mm') is not None else None,
            'diff_converted': diff_converted,
            'api_name': api_name,
            'unit_cost': (round(float(unit_cost), 4) if unit_cost is not None else None),
            'amount': amount,
        })

    pl_plus_amount = round(pl_plus_amount, 2)
    pl_minus_amount = round(pl_minus_amount, 2)
    return jsonify({'success': True, 'data': {
        'count_no': count_no,
        'status': count['status'],
        'siteref': siteref,
        'total_items': len(rows),
        'diff_items': diff_items,
        'pl_plus_amount': pl_plus_amount,
        'pl_minus_amount': pl_minus_amount,
        'net_amount': round(pl_plus_amount - pl_minus_amount, 2),
        'unpriced_items': unpriced_items,
        'currency_hint': 'CSI 成本货币',
        'items': items,
    }})


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

        inserted = 0
        csi_inserted = 0
        line_no = 0
        try:
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
                         count.get('siteref') or user.get('siteref'))
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
            db.commit()
        except Exception as e:
            db.rollback()
            logger.exception('[盘点差异调整] 盘点单 %s 执行失败，已回滚：%s', count_no, e)
            cur.close()
            return jsonify({'success': False, 'message': f'差异调整失败，已回滚：{e}'}), 500
        cur.close()
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


def _calc_item_metrics(coil, used_mm):
    """计算盘点明细指标（返回 dict）"""
    factor = _factor(coil.get('unit'))
    original = float(coil.get('coil_length') or 0)
    used = float(used_mm or 0)
    remain_mm = round(original * factor - used, 2) if factor else None
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
        actual_mm = float(data.get('actual_mm'))
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
        diff_mm = round(actual_mm - (float(remain_mm) if remain_mm is not None else 0), 2)
        diff_converted = round(diff_mm / factor, 2) if factor else None
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
#  PDF 报告
# ------------------------------------------------------------------ #
def _build_report_pdf(count, items):
    """生成「线边仓线材盘点报告」PDF（reportlab + SimHei 中文字体）"""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle

    # 中文字体（SimHei）
    font_paths = [
        r'C:\Windows\Fonts\simhei.ttf',
        r'C:\Windows\Fonts\simsun.ttc',
        r'C:\Windows\Fonts\msyh.ttc',
    ]
    font_name = 'SimHei'
    registered = False
    for fp in font_paths:
        try:
            pdfmetrics.registerFont(TTFont(font_name, fp))
            registered = True
            break
        except Exception:
            continue
    if not registered:
        font_name = 'Helvetica'

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,  # 纵向
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=18 * mm,
                            title='线边仓线材盘点报告')
    style = ParagraphStyle('zh', fontName=font_name, fontSize=10, leading=14)

    story = []
    # 标题行：左=标题，右=公司 Logo
    from reportlab.platypus import Image
    logo_path = None
    for lp in (r'D:\Workbuddy\多智能体\物料领取看板\app\static\images\logo.png',
               r'app/static/images/logo.png'):
        import os
        if os.path.exists(lp):
            logo_path = lp
            break
    title_para = Paragraph('<font size="16"><b>线边仓线材盘点报告</b></font>', style)
    if logo_path:
        logo_img = Image(logo_path, width=22 * mm, height=19 * mm)
        title_row = Table([[title_para, logo_img]], colWidths=[130 * mm, 40 * mm])
        title_row.hAlign = 'LEFT'
        title_row.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ]))
        story.append(title_row)
    else:
        story.append(title_para)
    story.append(Spacer(1, 4 * mm))
    # 信息区：每行一个字段
    for label, val in [
        ('盘点单号', count['count_no']),
        ('站点', count.get('siteref') or '-'),
        ('创建人', count.get('created_by') or '-'),
        ('开始', count.get('started_at') or '-'),
        ('完成', count.get('completed_at') or '-'),
    ]:
        story.append(Paragraph(f'{label}：{val}', style))
    story.append(Spacer(1, 4 * mm))

    # 表头中英双语（第一行中文、第二行英文）；最后一列单位按 CSI 单位动态显示
    head_style = ParagraphStyle('head', fontName=font_name, fontSize=8, leading=10,
                                alignment=1)  # 居中
    last_unit = next((it.get('unit') or '' for it in items if it.get('unit')), '')
    last_en = f'Diff({last_unit})' if last_unit else 'Diff'
    last_header = Paragraph(f'差异数量({last_unit})<br/>{last_en}', head_style) if last_unit \
        else Paragraph('差异数量<br/>Diff', head_style)
    headers = [
        Paragraph('物料<br/>Item', head_style),
        Paragraph('原始数量<br/>Orig', head_style),
        Paragraph('单位<br/>Unit', head_style),
        Paragraph('Lot No', head_style),
        Paragraph('盘点实际数量(mm)<br/>Actual', head_style),
        Paragraph('差异数量(mm)<br/>Diff', head_style),
        last_header,
    ]
    rows = [headers]
    total_diff = 0.0
    for it in items:
        f = _factor(it.get('unit'))
        diff_converted = it.get('diff_converted')
        if diff_converted is None and it.get('diff_mm') is not None and f:
            diff_converted = round(float(it['diff_mm']) / f, 2)
        rows.append([
            it.get('part_number') or it.get('coil_id') or '',
            it.get('original_qty') if it.get('original_qty') is not None else '',
            it.get('unit') or '',
            it.get('lot_no') or '',
            it.get('actual_mm') if it.get('actual_mm') is not None else '',
            it.get('diff_mm') if it.get('diff_mm') is not None else '',
            diff_converted if diff_converted is not None else '',
        ])
        if it.get('diff_mm') is not None:
            total_diff += float(it['diff_mm'])
    rows.append(['合计差异', '', '', '', '', round(total_diff, 2), ''])

    # 纵向 A4，7 列合计 170mm（与签核区表格同宽）；Lot No 列加宽容纳长批次号
    TABLE_WIDTH = 170 * mm
    col_widths = [35 * mm, 18 * mm, 12 * mm, 36 * mm, 28 * mm, 20 * mm, 18 * mm]
    assert sum(col_widths) <= TABLE_WIDTH, '表宽超过 170mm'
    table = Table(rows, repeatRows=1, colWidths=col_widths)
    table.hAlign = 'LEFT'   # 左边框对齐页边距（与标题/信息区/签核区一致）
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTNAME', (0, 0), (-1, 0), font_name),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),   # 全部左对齐（同 Word 习惯）
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F2F2F2')]),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#FFE699')),
    ]))
    story.append(table)
    story.append(Spacer(1, 10 * mm))

    # 签字区：申请人 / 申请人部门经理 / 仓库人员（签字 + 时间）
    # 宽度与盘点明细表一致（170mm），左右边框与明细表对齐
    sign_col = TABLE_WIDTH / 3
    sign_table = Table(
        [[Paragraph(f'申请人：', style), Paragraph(f'申请人部门经理：', style),
          Paragraph(f'仓库人员：', style)],
         [Paragraph(f'签字：', style), Paragraph(f'签字：', style), Paragraph(f'签字：', style)],
         [Paragraph(f'时间：', style), Paragraph(f'时间：', style), Paragraph(f'时间：', style)]],
        colWidths=[sign_col] * 3)
    sign_table.hAlign = 'LEFT'   # 与盘点明细表左边框对齐
    sign_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(sign_table)

    doc.build(story)
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
