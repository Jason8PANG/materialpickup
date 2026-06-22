from flask import Blueprint, request, jsonify, session
from datetime import datetime
from app.models import get_db_connection, get_site_filter
from app.utils import WhereBuilder

warehouse_bp = Blueprint('warehouse', __name__)


def add_log(cursor, request_id, operator, action, detail, ip):
    cursor.execute(
        "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (request_id, operator, action, detail, ip, datetime.now())
    )


def check_warehouse_or_admin():
    """检查仓库或管理员权限"""
    user = session.get('user')
    if not user:
        return None, jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return None, jsonify({'success': False, 'message': '权限不足'}), 403
    return user, None, None


def validate_site_match(request_row, user):
    """
    校验站点匹配。
    admin 可跨站，非 admin 必须本站点。
    """
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        if request_row['siteref'] != site_params[0]:
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403
    return None


@warehouse_bp.route('/api/requests/<int:request_id>/start-prep', methods=['POST'])
def start_prep(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'pending_prep':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许开始备料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'prepping', warehouse_operator = %s WHERE id = %s",
            (user['username'], request_id)
        )
        add_log(cursor, request_id, user['username'], 'START_PREP', '开始备料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '开始备料'})


@warehouse_bp.route('/api/requests/<int:request_id>/complete-prep', methods=['POST'])
def complete_prep(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'prepping':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许完成备料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'ready_pickup' WHERE id = %s",
            (request_id,)
        )
        add_log(cursor, request_id, user['username'], 'COMPLETE_PREP', '完成备料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '备料完成'})


@warehouse_bp.route('/api/requests/<int:request_id>/restore-from-short', methods=['POST'])
def restore_from_short(request_id):
    """缺料恢复：缺料 → 待取料"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'short':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许转为待取料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'ready_pickup', short_reason = NULL, short_time = NULL WHERE id = %s",
            (request_id,)
        )
        add_log(cursor, request_id, user['username'], 'RESTORE_FROM_SHORT', '缺料恢复→待取料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '已转为待取料'})


@warehouse_bp.route('/api/requests/<int:request_id>/short', methods=['POST'])
def short_material(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    short_reason = data.get('short_reason', '').strip()
    if not short_reason:
        return jsonify({'success': False, 'message': '请填写缺料原因'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] not in ('prepping', 'pending_prep'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许登记缺料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'short', short_reason = %s, short_time = %s WHERE id = %s",
            (short_reason, now, request_id)
        )
        add_log(cursor, request_id, user['username'], 'SHORT', f"缺料: {short_reason}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '缺料登记成功'})


@warehouse_bp.route('/api/requests/<int:request_id>/sign', methods=['POST'])
def sign_request(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点校验
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cursor.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        if req['status'] not in ('ready_pickup', 'short'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许签字确认'}), 400

        now = datetime.now()
        data = request.get_json() or {}
        signature_data = data.get('signature_data', '')

        cursor.execute(
            "UPDATE kr_material_request SET status = 'completed', signer = %s, sign_time = %s, signature_data = %s WHERE id = %s",
            (user['username'], now, signature_data, request_id)
        )
        add_log(cursor, request_id, user['username'], 'SIGN', '签字确认完成', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '签字确认成功'})


@warehouse_bp.route('/api/requests/<int:request_id>/assign-worker', methods=['PUT'])
def assign_worker(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    worker = data.get('warehouse_operator', '').strip()
    if not worker:
        return jsonify({'success': False, 'message': '请指定备料员'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] not in ('pending_prep', 'prepping'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不能指定备料员'}), 400

        cursor.execute(
            "UPDATE kr_material_request SET warehouse_operator = %s WHERE id = %s",
            (worker, request_id)
        )
        add_log(cursor, request_id, user['username'], 'ASSIGN_WORKER',
                f"指定备料员: {worker}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '已指定备料员'})


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/short', methods=['PUT'])
def update_item_short_reason(request_id, item_id):
    """按行更新缺料原因"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    short_reason = (data.get('short_reason') or '').strip()
    if not short_reason:
        return jsonify({'success': False, 'message': '请输入缺料原因'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        # 校验申请单
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 校验申请单状态
        if req['status'] not in ('prepping', 'short'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不能维护缺料原因'}), 400

        # 更新明细行缺料原因
        cursor.execute(
            "UPDATE kr_request_item SET short_reason = %s WHERE id = %s AND request_id = %s",
            (short_reason, item_id, request_id)
        )
        if cursor.rowcount == 0:
            cursor.close()
            return jsonify({'success': False, 'message': '明细行不存在'}), 404

        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '缺料原因已保存'})


@warehouse_bp.route('/api/requests/pending', methods=['GET'])
def pending_requests():
    """查询未完成单据（仓库用）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    status = request.args.get('status')
    job_order = request.args.get('job_order')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 20))

    wb = WhereBuilder(["r.is_deleted = 0", "r.status NOT IN ('completed', 'rejected')"])

    # 站点过滤
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f"r.{site_filter}", *site_params)

    if status:
        wb.add("r.status = %s", status)
    if job_order:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND job_order LIKE %s)", f"%{job_order}%")

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cursor = db.cursor()

        cursor.execute(f"SELECT COUNT(*) as total FROM kr_material_request r WHERE {where_clause}", params)
        total = cursor.fetchone()['total']

        offset = (page - 1) * size
        cursor.execute(
            f"""SELECT r.*,
                (SELECT COUNT(*) FROM kr_request_item WHERE request_id = r.id) as item_count,
                (SELECT job_order FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_job_order
                FROM kr_material_request r 
                WHERE {where_clause} 
                ORDER BY r.request_time DESC LIMIT %s OFFSET %s""",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

    STATUS_LABELS = {
        'pending_approval': '待审批', 'rejected': '已驳回',
        'pending_prep': '待备料', 'prepping': '备料中',
        'short': '缺料', 'ready_pickup': '待取料', 'completed': '已完成'
    }
    result = []
    for row in rows:
        r = dict(row)
        r['status_label'] = STATUS_LABELS.get(row['status'], row['status'])
        result.append(r)

    return jsonify({
        'success': True,
        'data': result,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size
    })
