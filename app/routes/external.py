"""
外部系统集成 API（naiwiptrack 等调用）：
  替代其他系统直接读写 materialpickup 库，统一走本服务 API。

认证：请求头 X-API-Key: <EXTERNAL_API_KEY>（Config.EXTERNAL_API_KEY）
站点：除 confirm-user 外，所有接口须携带 X-Site-Ref（310/410 或公司码 NAIGROUP_PRD_XXX，兼容 NAIGROUP_PROD_XXX），
      看板强制校验并按站点过滤/落库（见 _resolve_site）。
Base：/api/external/

接口：
  GET  /api/external/coils/<coil_id>            卷标查询（含剩余）
  GET  /api/external/consumption?coil_id=&job_order=   消耗查询
  POST /api/external/consumption                消耗登记（stage=complete，含超限校验+force）
  POST /api/external/consumption/scrap          报废登记
  GET  /api/external/cutting-check?job_order=   首末件检查查询
  POST /api/external/cutting-check              首末件检查登记（公差校验+force）
  GET  /api/external/cutting-ref?job_order=&part_number=  裁剪参数查询
"""
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.config import Config
from app.models import get_db_connection

external_bp = Blueprint('external', __name__)

EXTERNAL_KEY = getattr(Config, 'EXTERNAL_API_KEY', '') or ''

COIL_STATUS_LABELS = {
    'in_stock': '在库', 'in_shop': '在车间', 'consumed': '已消耗',
    'issued': '已出库', 'scrapped': '报废',
}


def _check_api_key():
    key = (request.headers.get('X-API-Key') or '').strip()
    if not EXTERNAL_KEY or key != EXTERNAL_KEY:
        return jsonify({'success': False, 'error': 'API Key 无效'}), 401
    return None


def _resolve_site():
    """解析站点标识，返回 (site_or_None, error_or_None)。

    取值优先级：请求头 X-Site-Ref > query 参数 site > JSON body 的 site（仅 POST/DELETE）。
    规范化：strip 空白；接受两种格式：
      ① 站点号 '310'/'410'（必须是 Config.SITE_CONFIG 的 key）；
      ② 公司码 'NAIGROUP_PRD_XXX' / 'NAIGROUP_PROD_XXX'（容错 PRD/PROD、单/双下划线），
         通过 Config.SITE_CSI_COMPANY 的 value 匹配反查站点号，也兼容去掉
         'NAIGROUP_PRD_' / 'NAIGROUP_PROD_' 前缀后的值（如 '410'）。
    """
    site_val = (request.headers.get('X-Site-Ref') or '').strip()
    if not site_val:
        site_val = (request.args.get('site') or '').strip()
    if not site_val and request.method in ('POST', 'DELETE'):
        b = request.get_json(silent=True) or {}
        site_val = str(b.get('site') or '').strip()
    raw = (site_val or '').upper().replace(' ', '')
    if raw in Config.SITE_CONFIG:
        return raw, None
    # 公司码匹配：NAIGROUP_PRD_XXX / NAIGROUP_PROD_XXX（容错 PRD/PROD、单/双下划线）
    compact = raw.replace('_', '')
    if compact:
        for code, snum in Config.SITE_CSI_COMPANY.items():
            if compact.replace('PROD', 'PRD') == code.upper().replace('_', ''):
                return snum, None
        # 去掉 'NAIGROUP_PRD_'/'NAIGROUP_PROD_' 前缀后的纯站点号（如 '410'）
        if compact.startswith('NAIGROUP'):
            rest = compact[len('NAIGROUP'):]
            for pre in ('PRD', 'PROD'):
                if rest.startswith(pre):
                    rest = rest[len(pre):].lstrip('_')
                    break
            if rest in Config.SITE_CONFIG:
                return rest, None
    return None, '缺少或无效的站点标识（X-Site-Ref），可选站点: 310, 410'


def _require_site():
    """强制要求站点标识：调用 _resolve_site，失败返回 (jsonify 响应, 400)；成功返回站点号字符串"""
    site, err = _resolve_site()
    if err:
        return jsonify({'success': False, 'error': err}), 400
    return site


def _factor(unit):
    u = (unit or '').strip().upper()
    return Config.UNIT_CONVERT_FACTOR.get(u) if u else None


def _parse_tol(val):
    """公差解析：±0.5 / 0.5 → 0.5"""
    if val is None or val == '':
        return None
    try:
        return float(str(val).replace('±', '').replace('±', '').strip())
    except (TypeError, ValueError):
        return None


def _check_tol(target, actual, tol):
    """公差校验，返回 (ok, diff, tol)"""
    try:
        t = float(target)
        a = float(actual)
    except (TypeError, ValueError):
        return True, 0.0, None
    tol_n = _parse_tol(tol)
    if tol_n is None:
        return True, abs(a - t), None
    return abs(a - t) <= tol_n, abs(a - t), tol_n


def _num(v, default=None):
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ================================================================== #
#  卷标查询
# ================================================================== #
@external_bp.route('/api/external/coils/<coil_id>', methods=['GET'])
def ext_coil_lookup(coil_id):
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0 AND siteref = %s",
            (coil_id, site)
        )
        coil = cur.fetchone()
        if not coil:
            cur.close()
            return jsonify({'success': False, 'error': '卷标不存在'}), 404
        cur.execute(
            "SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption "
            "WHERE coil_id = %s",
            (coil_id,)
        )
        used = float(cur.fetchone()['used'] or 0)
    factor = _factor(coil.get('unit'))
    total_mm = float(coil['coil_length'] or 0) * factor if factor and coil.get('coil_length') is not None else None
    remain_mm = round(total_mm - used, 2) if total_mm is not None else None
    return jsonify({
        'success': True,
        'data': {
            'coil_id': coil['coil_id'],
            'part_number': coil.get('part_number') or '',
            'lot_no': coil.get('lot_no') or '',
            'unit': coil.get('unit') or '',
            'coil_length': float(coil['coil_length']) if coil.get('coil_length') is not None else None,
            'status': coil.get('status'),
            'status_label': COIL_STATUS_LABELS.get(coil.get('status'), coil.get('status') or ''),
            'siteref': coil.get('siteref'),
            'request_id': coil.get('request_id'),
            'used_mm': used,
            'remain_mm': remain_mm,
            'remain_orig': round(remain_mm / factor, 2) if remain_mm is not None and factor else None,
        }
    })


# ================================================================== #
#  消耗登记（裁剪消耗，stage=complete）
# ================================================================== #
@external_bp.route('/api/external/consumption', methods=['POST'])
def ext_create_consumption():
    err = _check_api_key()
    if err:
        return err
    b = request.get_json() or {}
    site = _require_site()
    if isinstance(site, tuple):
        return site
    coil_id = str(b.get('coil_id') or '').strip()
    part_number = (b.get('part_number') or '').strip()
    job_order = (b.get('job_order') or '').strip() or None
    job_part_number = (b.get('job_part_number') or '').strip() or None

    if not coil_id or not part_number:
        return jsonify({'success': False, 'error': 'coil_id、part_number 为必填项'}), 400
    qty = _num(b.get('shear_qty'))
    act_len = _num(b.get('actual_shear_length'))
    if qty is None or qty <= 0:
        return jsonify({'success': False, 'error': '消耗数量必须大于0'}), 400
    if act_len is None or act_len <= 0:
        return jsonify({'success': False, 'error': '实际剪切长度必须大于0'}), 400
    scrap = _num(b.get('scrap_length_actual'), 0) or 0
    if scrap < 0:
        return jsonify({'success': False, 'error': '报废长度格式不正确'}), 400
    out_length = qty * act_len + scrap
    force = bool(b.get('force'))

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0 AND siteref = %s",
            (coil_id, site)
        )
        coil = cur.fetchone()
        if not coil:
            cur.close()
            return jsonify({'success': False, 'error': '卷标不存在'}), 404
        if coil.get('status') != 'in_shop':
            cur.close()
            return jsonify({'success': False, 'error': '卷标ID的状态不正确'}), 400
        cur.execute(
            "SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption "
            "WHERE coil_id = %s",
            (coil_id,)
        )
        used = float(cur.fetchone()['used'] or 0)
        factor = _factor(coil.get('unit'))
        if factor:
            total_mm = float(coil['coil_length'] or 0) * factor
            if out_length + used > total_mm + 0.0001 and not force:
                cur.close()
                return jsonify({
                    'success': False, 'error': '长度超限，需确认人授权',
                    'needForce': True,
                }), 400

        cur.execute(
            """INSERT INTO kr_wire_coil_consumption
               (coil_id, job_order, job_part_number, part_number, consume_type, out_length, unit,
                converted_length, converted_unit, shear_qty, shear_length,
                actual_shear_length, length_tolerance, shear_equipment, shear_device_no,
                actual_shear_equipment, scrap_length_actual, operator, checker, is_manual, remark, stage,
                siteref)
               VALUES (%s, %s, %s, %s, 'consumption', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', %s)""",
            (coil_id, job_order, job_part_number, part_number, out_length,
             coil.get('unit') or None,
             factor and round(out_length / factor, 4) or None,
             factor and coil.get('unit') or None,
             int(qty),
             _num(b.get('cut_length_mm')),
             act_len,
             _parse_tol(b.get('length_tolerance')),
             (b.get('shear_equipment') or '').strip() or None,
             (b.get('shear_device_no') or '').strip() or None,
             (b.get('actual_shear_equipment') or '').strip() or None,
             scrap or None,
             (b.get('operator') or '').strip() or 'unknown',
             (b.get('checker') or '').strip() or None,
             1 if b.get('is_manual') else 0,
             (b.get('remark') or '').strip() or None,
             coil.get('siteref'))
        )
        new_id = cur.lastrowid
        db.commit()
    return jsonify({
        'success': True,
        'message': '消耗记录已保存',
        'id': new_id,
        'consume_type': 'consumption',
        'out_length': out_length,
        'converted_length': factor and round(out_length / factor, 4) or None,
        'converted_unit': factor and coil.get('unit') or None,
        'remaining_mm': factor and round(max(total_mm - used - out_length, 0), 2) or None,
    })


# ================================================================== #
#  报废登记
# ================================================================== #
@external_bp.route('/api/external/consumption/scrap', methods=['POST'])
def ext_create_scrap():
    err = _check_api_key()
    if err:
        return err
    b = request.get_json() or {}
    site = _require_site()
    if isinstance(site, tuple):
        return site
    coil_id = str(b.get('coil_id') or '').strip()
    part_number = (b.get('part_number') or '').strip()
    job_order = (b.get('job_order') or '').strip() or None
    if not coil_id or not part_number:
        return jsonify({'success': False, 'error': 'coil_id、part_number 为必填项'}), 400
    scrap_len = _num(b.get('out_length'))
    if scrap_len is None or scrap_len <= 0:
        return jsonify({'success': False, 'error': '报废长度必须大于0'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0 AND siteref = %s",
            (coil_id, site)
        )
        coil = cur.fetchone()
        if not coil:
            cur.close()
            return jsonify({'success': False, 'error': '卷标不存在'}), 404
        if coil.get('status') != 'in_shop':
            cur.close()
            return jsonify({'success': False, 'error': '卷标ID的状态不正确'}), 400
        factor = _factor(coil.get('unit'))
        cur.execute(
            """INSERT INTO kr_wire_coil_consumption
               (coil_id, job_order, part_number, consume_type, out_length, unit,
                converted_length, converted_unit, operator, remark, siteref)
               VALUES (%s, %s, %s, 'scrap', %s, %s, %s, %s, %s, %s, %s)""",
            (coil_id, job_order, part_number, scrap_len, coil.get('unit') or None,
             factor and round(scrap_len / factor, 4) or None,
             factor and coil.get('unit') or None,
             (b.get('operator') or '').strip() or 'unknown',
             (b.get('remark') or '').strip() or None,
             coil.get('siteref'))
        )
        new_id = cur.lastrowid
        db.commit()
    return jsonify({
        'success': True, 'message': '报废记录已保存', 'id': new_id,
        'consume_type': 'scrap', 'out_length': scrap_len,
    })


# ================================================================== #
#  首件/末件检查登记
# ================================================================== #
@external_bp.route('/api/external/cutting-check', methods=['POST'])
def ext_create_check():
    err = _check_api_key()
    if err:
        return err
    b = request.get_json() or {}
    site = _require_site()
    if isinstance(site, tuple):
        return site
    job_order = (b.get('job_order') or '').strip()
    part_number = (b.get('part_number') or '').strip()
    if not job_order or not part_number:
        return jsonify({'success': False, 'error': 'job_order、part_number 为必填项'}), 400
    ctype = 'last' if b.get('check_type') == 'last' else 'first'
    if _num(b.get('shear_actual_length')) is None or _num(b.get('shear_actual_length')) <= 0:
        return jsonify({'success': False, 'error': '剪线实际长度必须大于0'}), 400
    if not (b.get('shear_checker') or '').strip():
        return jsonify({'success': False, 'error': '剪线确认人必填'}), 400
    force = bool(b.get('force'))

    tol_errors = []
    ok, _d, _t = _check_tol(b.get('shear_std_length'), b.get('shear_actual_length'), b.get('shear_std_tol'))
    if not ok and not force:
        tol_errors.append('剪线长度超出公差，需确认人授权')
    for key, label in [('strip_a_', '去皮A'), ('strip_b_', '去皮B')]:
        actual = b.get(f'{key}actual_length')
        if actual is not None and actual != '':
            ok, _d, _t = _check_tol(b.get(f'{key}std_length'), actual, b.get(f'{key}std_tol'))
            if not ok and not force:
                tol_errors.append(f'{label}长度超出公差，需确认人授权')
    if tol_errors:
        return jsonify({'success': False, 'error': '；'.join(tol_errors), 'needForce': True}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO kr_cutting_check
               (job_order, job_part_number, part_number, cut_length_mm, check_type, is_manual,
                shear_std_length, shear_std_tol, shear_std_device, shear_actual_device, shear_actual_length,
                shear_operator, shear_checker,
                strip_a_std_length, strip_a_std_tol, strip_a_std_device, strip_a_actual_device, strip_a_actual_length,
                strip_a_operator, strip_a_checker,
                strip_b_std_length, strip_b_std_tol, strip_b_std_device, strip_b_actual_device, strip_b_actual_length,
                strip_b_operator, strip_b_checker, scrap_length, siteref)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (job_order, (b.get('job_part_number') or '').strip() or part_number, part_number,
             _num(b.get('cut_length_mm')), ctype, 1 if b.get('is_manual') else 0,
             _num(b.get('shear_std_length')), str(b.get('shear_std_tol') or '') or None,
             (b.get('shear_std_device') or '').strip() or None,
             (b.get('shear_actual_device') or '').strip() or None,
             _num(b.get('shear_actual_length')),
             (b.get('shear_operator') or '').strip() or None,
             (b.get('shear_checker') or '').strip() or None,
             _num(b.get('strip_a_std_length')), str(b.get('strip_a_std_tol') or '') or None,
             (b.get('strip_a_std_device') or '').strip() or None,
             (b.get('strip_a_actual_device') or '').strip() or None,
             _num(b.get('strip_a_actual_length')),
             (b.get('strip_a_operator') or '').strip() or None,
             (b.get('strip_a_checker') or '').strip() or None,
             _num(b.get('strip_b_std_length')), str(b.get('strip_b_std_tol') or '') or None,
             (b.get('strip_b_std_device') or '').strip() or None,
             (b.get('strip_b_actual_device') or '').strip() or None,
             _num(b.get('strip_b_actual_length')),
             (b.get('strip_b_operator') or '').strip() or None,
             (b.get('strip_b_checker') or '').strip() or None,
             _num(b.get('scrap_length')), site)
        )
        new_id = cur.lastrowid
        db.commit()
    return jsonify({
        'success': True,
        'message': '首件检查已保存' if ctype == 'first' else '末件检查已保存',
        'id': new_id,
        'check_type': ctype,
    })


# ================================================================== #
#  确认人密码校验（cutting_confirm_user，回退 env）
# ================================================================== #
@external_bp.route('/api/external/confirm-user', methods=['POST'])
def ext_confirm_user():
    err = _check_api_key()
    if err:
        return err
    # 不要求站点标识：cutting_confirm_user 表无站点概念（确认人跨站点共用），仅校验 API Key。
    b = request.get_json() or {}
    pwd = str(b.get('password') or '').strip()
    if not pwd:
        return jsonify({'success': False, 'error': '确认密码必填'}), 400
    name = None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT name FROM cutting_confirm_user WHERE password = %s LIMIT 1",
            (pwd,)
        )
        row = cur.fetchone()
    if row:
        name = row['name']
    else:
        expected = getattr(Config, 'CUTTING_CONFIRM_PASSWORD', '') or ''
        if expected and pwd == expected:
            name = getattr(Config, 'CUTTING_CONFIRM_NAME', '') or '线长'
    if not name:
        return jsonify({'success': False, 'error': '确认密码错误'}), 400
    return jsonify({'success': True, 'name': name})


# ================================================================== #
#  删除消耗记录（需确认人密码）
# ================================================================== #
@external_bp.route('/api/external/consumption/<int:record_id>', methods=['DELETE'])
def ext_delete_consumption(record_id):
    err = _check_api_key()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    site = _require_site()
    if isinstance(site, tuple):
        return site
    pwd = str(b.get('password') or '').strip()
    if not pwd:
        return jsonify({'success': False, 'error': '确认密码必填'}), 400
    name = None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT name FROM cutting_confirm_user WHERE password = %s LIMIT 1",
            (pwd,)
        )
        row = cur.fetchone()
        if row:
            name = row['name']
        else:
            expected = getattr(Config, 'CUTTING_CONFIRM_PASSWORD', '') or ''
            if expected and pwd == expected:
                name = getattr(Config, 'CUTTING_CONFIRM_NAME', '') or '线长'
        if not name:
            cur.close()
            return jsonify({'success': False, 'error': '确认密码错误'}), 400
        cur.execute(
            "SELECT id FROM kr_wire_coil_consumption WHERE id = %s AND siteref = %s",
            (record_id, site)
        )
        if not cur.fetchone():
            cur.close()
            return jsonify({'success': False, 'error': '记录不存在'}), 404
        cur.execute("DELETE FROM kr_wire_coil_consumption WHERE id = %s", (record_id,))
        db.commit()
    return jsonify({'success': True, 'message': '记录已删除', 'id': record_id})


# ================================================================== #
#  消耗记录分页列表
# ================================================================== #
@external_bp.route('/api/external/consumption/list', methods=['GET'])
def ext_consumption_list():
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    try:
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('pageSize', 20)), 1), 200)
    except (TypeError, ValueError):
        page, page_size = 1, 20
    wb_where, wb_params = [], []
    wb_where.append('siteref = %s')
    wb_params.append(site)
    job = (request.args.get('job') or '').strip()
    part = (request.args.get('part') or '').strip()
    coil_id = (request.args.get('coilId') or '').strip()
    start_date = (request.args.get('startDate') or '').strip()
    end_date = (request.args.get('endDate') or '').strip()
    if job:
        wb_where.append('job_order LIKE %s')
        wb_params.append(f'%{job}%')
    if part:
        wb_where.append('part_number LIKE %s')
        wb_params.append(f'%{part}%')
    if coil_id:
        wb_where.append('coil_id = %s')
        wb_params.append(coil_id)
    if start_date:
        wb_where.append('created_at >= %s')
        wb_params.append(f'{start_date} 00:00:00')
    if end_date:
        wb_where.append('created_at <= %s')
        wb_params.append(f'{end_date} 23:59:59')
    where = (' WHERE ' + ' AND '.join(wb_where)) if wb_where else ''
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS c FROM kr_wire_coil_consumption{where}",
            wb_params
        )
        total = cur.fetchone()['c']
        cur.execute(
            f"SELECT id, coil_id, siteref, job_order, job_part_number, part_number, consume_type, stage, "
            f"out_length, unit, converted_length, converted_unit, "
            f"shear_qty, shear_length, actual_shear_length, actual_shear_equipment, "
            f"scrap_length_actual, "
            f"operator, remark, created_at "
            f"FROM kr_wire_coil_consumption{where} ORDER BY id DESC LIMIT %s OFFSET %s",
            wb_params + [page_size, (page - 1) * page_size]
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('out_length', 'converted_length', 'shear_qty', 'shear_length', 'actual_shear_length',
                  'scrap_length_actual'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        d['consume_type_label'] = 'Scrap' if d.get('consume_type') == 'scrap' else 'consumption'
        d['stage'] = d.get('stage') or 'first'
        data.append(d)
    return jsonify({'success': True, 'data': data, 'total': total, 'page': page, 'pageSize': page_size})


# ================================================================== #
#  卷标信息分页列表
# ================================================================== #
@external_bp.route('/api/external/coils/list', methods=['GET'])
def ext_coils_list():
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    try:
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('pageSize', 20)), 1), 200)
    except (TypeError, ValueError):
        page, page_size = 1, 20
    wb_where, wb_params = [], []
    wb_where.append('c.siteref = %s')
    wb_params.append(site)
    coil_id = (request.args.get('coilId') or '').strip()
    part = (request.args.get('part') or '').strip()
    status = (request.args.get('status') or '').strip()
    if coil_id:
        wb_where.append('c.coil_id = %s')
        wb_params.append(coil_id)
    if part:
        wb_where.append('c.part_number LIKE %s')
        wb_params.append(f'%{part}%')
    if status:
        wb_where.append('c.status = %s')
        wb_params.append(status)
    where = (' WHERE ' + ' AND '.join(wb_where)) if wb_where else ''
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS c FROM kr_wire_coil c{where}",
            wb_params
        )
        total = cur.fetchone()['c']
        cur.execute(
            f"SELECT c.coil_id, c.part_number, c.status, c.coil_length, c.unit, c.siteref, "
            f"COALESCE(SUM(CASE WHEN x.consume_type = 'consumption' THEN x.out_length ELSE 0 END), 0) AS used_mm, "
            f"COALESCE(SUM(CASE WHEN x.consume_type = 'scrap' THEN x.out_length ELSE 0 END), 0) AS scrapped_mm, "
            f"COALESCE(SUM(x.out_length), 0) AS total_used_mm, "
            f"MAX(c.created_at) AS created_at "
            f"FROM kr_wire_coil c "
            f"LEFT JOIN kr_wire_coil_consumption x ON x.coil_id = c.coil_id "
            f"{where} GROUP BY c.coil_id, c.part_number, c.status, c.coil_length, c.unit, c.siteref "
            f"ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
            wb_params + [page_size, (page - 1) * page_size]
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('coil_length', 'used_mm', 'scrapped_mm', 'total_used_mm'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        d['status_label'] = COIL_STATUS_LABELS.get(d.get('status'), d.get('status') or '')
        factor = _factor(d.get('unit'))
        if d.get('coil_length') is not None and factor:
            remain_mm = round(d['coil_length'] * factor - d.get('total_used_mm', 0), 2)
            d['remain_mm'] = remain_mm
            d['remain_orig'] = round(remain_mm / factor, 2)
        else:
            d['remain_mm'] = None
            d['remain_orig'] = None
        data.append(d)
    return jsonify({'success': True, 'data': data, 'total': total, 'page': page, 'pageSize': page_size})


# ================================================================== #
#  消耗查询
# ================================================================== #
@external_bp.route('/api/external/consumption', methods=['GET'])
def ext_consumption_query():
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    wb_where, wb_params = [], []
    wb_where.append('siteref = %s')
    wb_params.append(site)
    coil_id = (request.args.get('coil_id') or '').strip()
    job_order = (request.args.get('job_order') or '').strip()
    if coil_id:
        wb_where.append('coil_id = %s')
        wb_params.append(coil_id)
    if job_order:
        wb_where.append('job_order = %s')
        wb_params.append(job_order)
    where = (' WHERE ' + ' AND '.join(wb_where)) if wb_where else ''
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT id, coil_id, siteref, job_order, job_part_number, part_number, consume_type, stage, "
            f"out_length, unit, converted_length, converted_unit, "
            f"shear_qty, shear_length, actual_shear_length, "
            f"shear_equipment, shear_device_no, actual_shear_equipment, "
            f"scrap_length_actual, "
            f"operator, remark, created_at "
            f"FROM kr_wire_coil_consumption{where} ORDER BY id DESC LIMIT 200",
            wb_params
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('out_length', 'converted_length', 'shear_qty', 'shear_length', 'actual_shear_length',
                  'scrap_length_actual'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        d['consume_type_label'] = 'Scrap' if d.get('consume_type') == 'scrap' else 'consumption'
        d['stage'] = d.get('stage') or 'first'
        data.append(d)
    return jsonify({'success': True, 'data': data})


# ================================================================== #
#  首末件检查查询
# ================================================================== #
@external_bp.route('/api/external/cutting-check', methods=['GET'])
def ext_check_query():
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    wb_where, wb_params = [], []
    wb_where.append('siteref = %s')
    wb_params.append(site)
    job_order = (request.args.get('job_order') or '').strip()
    part_number = (request.args.get('part_number') or '').strip()
    check_type = (request.args.get('check_type') or '').strip()
    if job_order:
        wb_where.append('job_order = %s')
        wb_params.append(job_order)
    if part_number:
        wb_where.append('part_number = %s')
        wb_params.append(part_number)
    if check_type:
        wb_where.append('check_type = %s')
        wb_params.append(check_type)
    where = (' WHERE ' + ' AND '.join(wb_where)) if wb_where else ''
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT * FROM kr_cutting_check{where} ORDER BY id DESC LIMIT 200",
            wb_params
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('cut_length_mm', 'shear_std_length', 'shear_actual_length',
                  'strip_a_std_length', 'strip_a_actual_length',
                  'strip_b_std_length', 'strip_b_actual_length', 'scrap_length'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        data.append(d)
    return jsonify({'success': True, 'data': data})


# ================================================================== #
#  裁剪参数查询
# ================================================================== #
@external_bp.route('/api/external/cutting-ref', methods=['GET'])
def ext_cutting_ref():
    err = _check_api_key()
    if err:
        return err
    site = _require_site()
    if isinstance(site, tuple):
        return site
    # kr_cutting_ref 无 siteref 列，参数为跨站点共享；仅校验站点标识有效性，不做数据过滤
    wb_where, wb_params = [], []
    finished_part = (request.args.get('finished_part') or '').strip()
    wire_part = (request.args.get('wire_part') or '').strip()
    if finished_part:
        wb_where.append('finished_part = %s')
        wb_params.append(finished_part)
    if wire_part:
        wb_where.append('wire_part = %s')
        wb_params.append(wire_part)
    if not wb_where:
        return jsonify({'success': False, 'error': '请提供 finished_part 过滤'}), 400
    where = ' WHERE ' + ' AND '.join(wb_where)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT id, finished_part, wire_part, qty_per_group, cut_length_mm, "
            f"length_tol, cut_device, device_no, strip_len_a, strip_tol_a, strip_len_b, "
            f"strip_tol_b, term_a, term_b "
            f"FROM kr_cutting_ref{where} ORDER BY wire_part, cut_length_mm",
            wb_params
        )
        rows = cur.fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for f in ('qty_per_group', 'cut_length_mm', 'strip_len_a', 'strip_len_b'):
            if d.get(f) is not None:
                d[f] = float(d[f])
        data.append(d)
    return jsonify({'success': True, 'data': data})
