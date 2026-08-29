"""
退料申请单路由（生产退料 → 仓库确认）。

流程：
  1. 生产（requester）扫卷标ID → POST /api/returns 创建退料单（request_type='return', status='pending_return'）
  2. 仓库在看板「待退料」泳道确认 → POST /api/returns/<id>/confirm
     卷标状态 在车间(in_shop) → 在库(in_stock)，剩余长度回写 coil_length
  3. 仓库驳回 → POST /api/returns/<id>/reject

剩余长度 = 卷标原始长度(coil_length) - 已消耗长度(consumption 表 out_length 汇总，含裁剪+报废)
"""
import re
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from app.config import Config
from app.models import get_db_connection, get_site_filter

return_bp = Blueprint('return', __name__)

COIL_ID_RE = re.compile(r'^\d{9}$')

RETURN_STATUS_LABELS = {
    'pending_return': '待退料',
    'confirmed': '已确认',
    'rejected': '已驳回',
}


def _coil_remain_length(cur, coil_id: str):
    """计算卷标剩余长度（与 coil_length 同单位，即系统单位如 M/FT）：
    coil_length - 已消耗(out_length 汇总，mm) ÷ 换算系数 → 原始单位。
    消耗表 out_length 固定为 mm（出库/裁剪/报废登记），卷长存原始单位，
    相减前必须换算，否则单位不一致导致剩余为负、误判不可退料。"""
    cur.execute(
        "SELECT coil_length, unit FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
        (coil_id,)
    )
    row = cur.fetchone()
    if not row or row['coil_length'] is None:
        return 0.0
    cur.execute(
        "SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption "
        "WHERE coil_id = %s ",
        (coil_id,)
    )
    used = float(cur.fetchone()['used'] or 0)
    unit = (row.get('unit') or '').strip().upper()
    factor = Config.UNIT_CONVERT_FACTOR.get(unit) if unit else None
    used_orig = used / factor if factor else used  # mm → 原始单位；单位未知按原值（兼容旧数据）
    return round(float(row['coil_length']) - used_orig, 2)


# ================= 1. 创建退料申请单（生产发起） =================

@return_bp.route('/api/returns', methods=['POST'])
def create_return():
    """生产退料：输入卷标ID列表，自动带出物料号/单位/剩余长度"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    coil_ids = [str(x).strip() for x in (data.get('coil_ids') or []) if str(x).strip()]
    if not coil_ids:
        return jsonify({'success': False, 'message': '请至少输入一个卷标ID'}), 400
    if len(coil_ids) > 200:
        return jsonify({'success': False, 'message': '单次最多退料 200 卷'}), 400

    siteref = user.get('siteref', '')
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    # 去重
    coil_ids = list(dict.fromkeys(coil_ids))
    errors = []
    items = []
    with get_db_connection() as db:
        cur = db.cursor()
        for cid in coil_ids:
            if not COIL_ID_RE.match(cid):
                errors.append(f'{cid}: 卷标ID格式无效')
                continue
            cur.execute(
                "SELECT coil_id, part_number, unit, status, coil_length FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
                (cid,)
            )
            c = cur.fetchone()
            if not c:
                errors.append(f'{cid}: 卷标不存在')
                continue
            if c['status'] != 'in_shop':
                errors.append(f"{cid}: 当前状态为{COIL_STATUS_LABEL(c['status'])}，仅在车间的卷标可退料")
                continue
            remain = _coil_remain_length(cur, cid)
            if remain <= 0:
                errors.append(f'{cid}: 卷标剩余长度不足（已消耗完），无需退料')
                continue
            items.append({
                'coil_id': cid,
                'part_number': c['part_number'] or '',
                'unit': c['unit'] or '',
                'remain_length': remain,
            })
        if errors:
            cur.close()
            return jsonify({'success': False, 'message': '<br>'.join(errors[:10])}), 400
        if not items:
            cur.close()
            return jsonify({'success': False, 'message': '没有可退料的卷标'}), 400

        now = datetime.now()
        # 主表：复用 kr_material_request（request_type='return', status='pending_return'）
        cur.execute(
            "INSERT INTO kr_material_request (siteref, request_type, requester, request_time, status, remark) "
            "VALUES (%s, 'return', %s, %s, 'pending_return', %s)",
            (siteref, user['username'], now, (data.get('remark') or '').strip())
        )
        return_id = cur.lastrowid

        # 明细表：卷标维度
        cur.executemany(
            "INSERT INTO kr_return_item (request_id, coil_id, part_number, unit, remain_length) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(return_id, it['coil_id'], it['part_number'], it['unit'], it['remain_length']) for it in items]
        )
        # 操作日志
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_SUBMIT', %s, %s, %s)",
            (return_id, user['username'],
             f"提交退料申请: {len(items)} 卷",
             request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'id': return_id, 'message': f'退料申请已提交（{len(items)} 卷），等待仓库确认'})


# ================= 2. 仓库确认退料 =================

@return_bp.route('/api/returns/<int:return_id>/confirm', methods=['POST'])
def confirm_return(return_id):
    """仓库确认退料：卷标 在车间→在库，剩余长度回写 coil_length"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'pending_return':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许确认退料'}), 400
        # 站点校验
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        # 明细
        cur.execute(
            "SELECT coil_id, remain_length FROM kr_return_item WHERE request_id = %s",
            (return_id,)
        )
        items = cur.fetchall()
        now = datetime.now()

        confirmed = 0
        for it in items:
            # 卷标 在车间 → 在库，回写剩余长度
            cur.execute(
                "UPDATE kr_wire_coil SET status = 'in_stock', coil_length = %s "
                "WHERE coil_id = %s AND status = 'in_shop'",
                (it['remain_length'], it['coil_id'])
            )
            confirmed += cur.rowcount

        cur.execute(
            "UPDATE kr_material_request SET status = 'confirmed', confirmed_by = %s, confirm_time = %s WHERE id = %s",
            (user['username'], now, return_id)
        )
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_CONFIRM', %s, %s, %s)",
            (return_id, user['username'], f"确认退料: {confirmed} 卷转回在库", request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': f'退料确认成功，{confirmed} 卷已转回在库'})


# ================= 3. 仓库驳回退料 =================

@return_bp.route('/api/returns/<int:return_id>/reject', methods=['POST'])
def reject_return(return_id):
    """仓库驳回退料单"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'pending_return':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许驳回'}), 400
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        now = datetime.now()
        cur.execute(
            "UPDATE kr_material_request SET status = 'rejected', reject_reason = %s WHERE id = %s",
            (reason or '仓库驳回', return_id)
        )
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_REJECT', %s, %s, %s)",
            (return_id, user['username'], f"驳回退料: {reason}", request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '退料单已驳回'})


# ================= 4. 卷标信息查询（退料页带出用） =================

@return_bp.route('/api/returns/coil-info/<coil_id>', methods=['GET'])
def coil_info(coil_id):
    """按卷标ID查询：物料号/单位/状态/剩余长度（退料页扫码带出）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT coil_id, part_number, unit, status, coil_length, request_id FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        c = cur.fetchone()
        if not c:
            cur.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        # 站点隔离
        site_filter, site_params = get_site_filter(user)
        if site_filter and site_params:
            cur.execute("SELECT siteref FROM kr_material_request WHERE id = %s", (c['request_id'],))
            rq = cur.fetchone()
            if rq and rq['siteref'] != site_params[0]:
                cur.close()
                return jsonify({'success': False, 'message': '无权查看其他站点的卷标'}), 403
        remain = _coil_remain_length(cur, coil_id)
        cur.close()

    return jsonify({
        'success': True,
        'data': {
            'coil_id': c['coil_id'],
            'part_number': c['part_number'] or '',
            'unit': c['unit'] or '',
            'status': c['status'],
            'status_label': COIL_STATUS_LABEL(c['status']),
            'remain_length': remain,
            'can_return': c['status'] == 'in_shop' and remain > 0,
        }
    })


def COIL_STATUS_LABEL(status):
    labels = {'in_stock': '在库', 'in_shop': '在车间', 'consumed': '已消耗',
              'issued': '已出库', 'scrapped': '报废'}
    return labels.get(status, status or '')
