from flask import Blueprint, request, jsonify, session
from datetime import datetime
from app.models import get_db_connection
from app.utils import WhereBuilder
from app.config import Config

admin_bp = Blueprint('admin', __name__)


def check_admin():
    user = session.get('user')
    if not user:
        return None, jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] != 'admin':
        return None, jsonify({'success': False, 'message': '权限不足'}), 403
    return user, None, None


@admin_bp.route('/api/role-mappings', methods=['GET'])
def list_mappings():
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    # 支持 siteref 和 role 筛选
    siteref = request.args.get('siteref')
    role = request.args.get('role')

    wb = WhereBuilder(["1=1"])
    if siteref:
        wb.add("siteref = %s", siteref)
    if role:
        wb.add("role = %s", role)

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(f"SELECT * FROM kr_role_mapping WHERE {where_clause} ORDER BY id", params)
        rows = cursor.fetchall()
        cursor.close()

    return jsonify({'success': True, 'data': rows})


@admin_bp.route('/api/role-mappings', methods=['POST'])
def create_mapping():
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json()
    required = ['domain_account', 'role']
    for field in required:
        if not data.get(field):
            return jsonify({'success': False, 'message': f'缺少必填字段: {field}'}), 400

    valid_roles = ('admin', 'requester', 'supervisor', 'warehouse')
    if data['role'] not in valid_roles:
        return jsonify({'success': False, 'message': f'无效角色，可选: {", ".join(valid_roles)}'}), 400

    # 站点验证：非 admin 角色必须填 siteref
    siteref = data.get('siteref')
    if siteref is not None:
        siteref = str(siteref).strip() or None
    if data['role'] != 'admin' and not siteref:
        return jsonify({'success': False, 'message': '非管理员角色必须指定所属站点'}), 400
    if siteref and siteref not in Config.SITE_CONFIG:
        return jsonify({'success': False, 'message': f'无效站点，可选: {", ".join(Config.SITE_CONFIG.keys())}'}), 400

    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute(
                "INSERT INTO kr_role_mapping (domain_account, display_name, role, siteref, email, is_active, remark) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    data['domain_account'].strip(),
                    data.get('display_name', '').strip(),
                    data['role'],
                    siteref,
                    data.get('email', '').strip(),
                    data.get('is_active', 1),
                    data.get('remark', '').strip()
                )
            )
            db.commit()
            new_id = cursor.lastrowid
            cursor.close()
        return jsonify({'success': True, 'id': new_id, 'message': '创建成功'}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 400


@admin_bp.route('/api/role-mappings/<int:mapping_id>', methods=['PUT'])
def update_mapping(mapping_id):
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json()
    try:
        with get_db_connection() as db:
            cursor = db.cursor()

            cursor.execute("SELECT * FROM kr_role_mapping WHERE id = %s", (mapping_id,))
            existing = cursor.fetchone()
            if not existing:
                cursor.close()
                return jsonify({'success': False, 'message': '记录不存在'}), 404

            update_fields = []
            update_params = []

            for field in ['display_name', 'role', 'email', 'is_active', 'remark', 'domain_account', 'siteref']:
                if field in data:
                    val = data[field]
                    # siteref 空字符串转为 None
                    if field == 'siteref':
                        val = val.strip() if val else None
                    update_fields.append(f"{field} = %s")
                    update_params.append(val)

            if not update_fields:
                cursor.close()
                return jsonify({'success': False, 'message': '没有需要更新的字段'}), 400

            update_params.append(mapping_id)
            cursor.execute(
                f"UPDATE kr_role_mapping SET {', '.join(update_fields)} WHERE id = %s",
                update_params
            )
            db.commit()
            cursor.close()
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 400


@admin_bp.route('/api/role-mappings/<int:mapping_id>', methods=['DELETE'])
def delete_mapping(mapping_id):
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_role_mapping WHERE id = %s", (mapping_id,))
        if not cursor.fetchone():
            cursor.close()
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        cursor.execute("DELETE FROM kr_role_mapping WHERE id = %s", (mapping_id,))
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '删除成功'})


@admin_bp.route('/api/logs', methods=['GET'])
def get_logs():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    request_id = request.args.get('request_id', type=int)
    operator = request.args.get('operator')
    action = request.args.get('action')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 50))

    wb = WhereBuilder(["1=1"])

    if request_id:
        wb.add("request_id = %s", request_id)
    if operator:
        wb.add("operator LIKE %s", f"%{operator}%")
    if action:
        wb.add("action = %s", action)

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cursor = db.cursor()

        cursor.execute(f"SELECT COUNT(*) as total FROM kr_operation_log WHERE {where_clause}", params)
        total = cursor.fetchone()['total']

        offset = (page - 1) * size
        cursor.execute(
            f"SELECT * FROM kr_operation_log WHERE {where_clause} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

    return jsonify({
        'success': True,
        'data': rows,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size
    })


@admin_bp.route('/api/logs/<int:log_id>', methods=['GET'])
def get_log_detail(log_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_operation_log WHERE id = %s", (log_id,))
        row = cursor.fetchone()
        cursor.close()

    if not row:
        return jsonify({'success': False, 'message': '日志不存在'}), 404

    return jsonify({'success': True, 'data': row})
