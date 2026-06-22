from flask import Blueprint, request, jsonify, session
from datetime import datetime, timedelta
import secrets
from app.models import get_db_connection, get_site_filter, STATUS_LABELS

approval_bp = Blueprint('approval', __name__)


def add_log(cursor, request_id, operator, action, detail, ip):
    cursor.execute(
        "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (request_id, operator, action, detail, ip, datetime.now())
    )


def validate_site_match(request_row, user):
    """校验站点匹配"""
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        if request_row['siteref'] != site_params[0]:
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403
    return None


@approval_bp.route('/api/requests/<int:request_id>/approve', methods=['POST'])
def approve_request(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('supervisor', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
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

        if req['status'] != 'pending_approval':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许审批'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'pending_prep', supervisor = %s, "
            "approve_time = %s, approve_comment = %s WHERE id = %s",
            (user['username'], now, data.get('comment', ''), request_id)
        )
        add_log(cursor, request_id, user['username'], 'APPROVE',
                f"审批通过: {data.get('comment', '')}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '审批通过'})


@approval_bp.route('/api/requests/<int:request_id>/reject', methods=['POST'])
def reject_request(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('supervisor', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
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

        if req['status'] != 'pending_approval':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许驳回'}), 400

        if not data.get('comment'):
            cursor.close()
            return jsonify({'success': False, 'message': '驳回时必须填写批注意见'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'rejected', supervisor = %s, "
            "approve_time = %s, approve_comment = %s WHERE id = %s",
            (user['username'], now, data['comment'], request_id)
        )
        add_log(cursor, request_id, user['username'], 'REJECT',
                f"驳回: {data['comment']}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '已驳回'})


@approval_bp.route('/api/approve/token/<token>', methods=['GET'])
def get_approval_by_token(token):
    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT t.*, r.* FROM kr_approval_token t "
            "JOIN kr_material_request r ON t.request_id = r.id "
            "WHERE t.token = %s AND t.is_used = 0 AND t.expires_at > NOW()",
            (token,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return jsonify({'success': False, 'message': '令牌无效或已过期'}), 404

        # 获取明细
        cursor.execute(
            "SELECT * FROM kr_request_item WHERE request_id = %s ORDER BY id",
            (row['request_id'],)
        )
        items = cursor.fetchall()
        cursor.close()

    result = dict(row)
    result['status_label'] = STATUS_LABELS.get(row['status'], row['status'])
    result['items'] = items
    result['item_count'] = len(items)
    return jsonify({'success': True, 'request': result})


@approval_bp.route('/api/approve/token/<token>/action', methods=['POST'])
def token_approve_action(token):
    data = request.get_json() or {}
    action = data.get('action')
    comment = data.get('comment', '')

    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'message': '无效操作'}), 400

    if action == 'reject' and not comment:
        return jsonify({'success': False, 'message': '驳回时必须填写批注意见'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT t.*, r.status as req_status, r.siteref FROM kr_approval_token t "
            "JOIN kr_material_request r ON t.request_id = r.id "
            "WHERE t.token = %s AND t.is_used = 0 AND t.expires_at > NOW()",
            (token,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return jsonify({'success': False, 'message': '令牌无效或已过期'}), 404

        if row['req_status'] != 'pending_approval':
            cursor.close()
            return jsonify({'success': False, 'message': '单据状态已变更，不能操作'}), 400

        now = datetime.now()
        request_id = row['request_id']
        supervisor = row['supervisor']

        # 标记令牌已使用
        cursor.execute("UPDATE kr_approval_token SET is_used = 1 WHERE token = %s", (token,))

        if action == 'approve':
            cursor.execute(
                "UPDATE kr_material_request SET status = 'pending_prep', supervisor = %s, "
                "approve_time = %s, approve_comment = %s WHERE id = %s",
                (supervisor, now, comment, request_id)
            )
            add_log(cursor, request_id, supervisor, 'APPROVE', f"邮件审批通过: {comment}", '0.0.0.0')
        else:
            cursor.execute(
                "UPDATE kr_material_request SET status = 'rejected', supervisor = %s, "
                "approve_time = %s, approve_comment = %s WHERE id = %s",
                (supervisor, now, comment, request_id)
            )
            add_log(cursor, request_id, supervisor, 'REJECT', f"邮件审批驳回: {comment}", '0.0.0.0')

        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '操作成功'})


def generate_approval_token(request_id, supervisor):
    """生成邮件审批令牌（供 email_service 调用）"""
    token = secrets.token_urlsafe(48)
    now = datetime.now()
    expires = now + timedelta(hours=72)  # 72小时有效
    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO kr_approval_token (request_id, token, supervisor, expires_at) "
            "VALUES (%s, %s, %s, %s)",
            (request_id, token, supervisor, expires)
        )
        db.commit()
        cursor.close()
    return token
