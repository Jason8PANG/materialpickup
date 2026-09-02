# -*- coding: utf-8 -*-
"""
裁线规格管理路由（kr_cutting_ref）。

权限：
  - 读操作（列表/搜索）：登录即可
  - 写操作（新增/编辑/删除/批量导入）：仅 me_engineer（ME工程师）与 admin
"""
import io
import json

from flask import Blueprint, jsonify, request, session

from app.models import get_db_connection
from app.utils import WhereBuilder
from app.services.cutting_import import import_cutting_ref

cutting_bp = Blueprint('cutting', __name__)

# 可编辑字段（15 个命名列；raw_data 为导入时自动生成，不手工编辑）
EDITABLE_FIELDS = [
    'finished_part', 'wire_part', 'wire_awg', 'color', 'qty_per_group',
    'cut_length_mm', 'length_tol', 'cut_device', 'device_no',
    'strip_len_a', 'strip_tol_a', 'strip_len_b', 'strip_tol_b',
    'term_a', 'term_b',
]

# 数值字段（用于创建/更新时统一转为 Decimal/None）
NUMERIC_FIELDS = {
    'qty_per_group', 'cut_length_mm', 'strip_len_a', 'strip_len_b',
}

WRITE_ROLES = ('me_engineer', 'admin')


def _check_login():
    user = session.get('user')
    if not user:
        return None, (jsonify({'success': False, 'message': '未登录'}), 401)
    return user, None


def _check_write(user):
    if user['role'] not in WRITE_ROLES:
        return jsonify({'success': False, 'message': '仅 ME 工程师/管理员可修改'}), 403
    return None


def _normalize_value(field, value):
    """把入参值转为可入库的类型；数值字段非法值转 None，字符串字段 strip 后空值转 None。"""
    if field in NUMERIC_FIELDS:
        if value is None or str(value).strip() == '':
            return None
        try:
            return float(str(value).strip().replace(',', ''))
        except (TypeError, ValueError):
            return None
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


# ================================================================== #
#  列表（搜索 + 分页，登录即可）
# ================================================================== #
@cutting_bp.route('/api/cutting-ref', methods=['GET'])
def list_cutting_ref():
    user, err = _check_login()
    if err:
        return err

    keyword = (request.args.get('keyword') or '').strip()
    page = int(request.args.get('page', 1) or 1)
    size = int(request.args.get('size', 50) or 50)
    size = max(1, min(size, 200))
    page = max(1, page)

    wb = WhereBuilder(["1=1"])
    if keyword:
        wb.add("(finished_part LIKE %s OR wire_part LIKE %s OR wire_awg LIKE %s "
               "OR color LIKE %s OR term_a LIKE %s OR term_b LIKE %s)",
               *([f"%{keyword}%"] * 6))

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS total FROM kr_cutting_ref WHERE {where_clause}", params)
        total = cur.fetchone()['total']
        offset = (page - 1) * size
        cur.execute(
            f"SELECT * FROM kr_cutting_ref WHERE {where_clause} ORDER BY id LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cur.fetchall()
        cur.close()

    for r in rows:
        for f in NUMERIC_FIELDS:
            if r.get(f) is not None:
                r[f] = float(r[f])

    return jsonify({
        'success': True,
        'data': rows,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size,
        'can_edit': user['role'] in WRITE_ROLES,
    })


# ================================================================== #
#  新增（me_engineer + admin）
# ================================================================== #
@cutting_bp.route('/api/cutting-ref', methods=['POST'])
def create_cutting_ref():
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    data = request.get_json() or {}
    values = {f: _normalize_value(f, data.get(f)) for f in EDITABLE_FIELDS}

    if not values.get('finished_part') and not values.get('wire_part'):
        return jsonify({'success': False, 'message': '成品料号与线材料号至少填写一项'}), 400

    columns = list(EDITABLE_FIELDS)
    placeholders = ', '.join(['%s'] * len(columns))
    sql = f"INSERT INTO kr_cutting_ref ({', '.join(columns)}) VALUES ({placeholders})"
    params = [values[f] for f in columns]

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(sql, params)
            db.commit()
            new_id = cur.lastrowid
            cur.close()
        return jsonify({'success': True, 'id': new_id, 'message': '创建成功'}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 400


# ================================================================== #
#  编辑（me_engineer + admin）
# ================================================================== #
@cutting_bp.route('/api/cutting-ref/<int:ref_id>', methods=['PUT'])
def update_cutting_ref(ref_id):
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    data = request.get_json() or {}
    update_fields = []
    update_params = []
    for f in EDITABLE_FIELDS:
        if f in data:
            update_fields.append(f"{f} = %s")
            update_params.append(_normalize_value(f, data.get(f)))

    if not update_fields:
        return jsonify({'success': False, 'message': '没有需要更新的字段'}), 400

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute("SELECT id FROM kr_cutting_ref WHERE id = %s", (ref_id,))
            if not cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': '记录不存在'}), 404
            update_params.append(ref_id)
            cur.execute(
                f"UPDATE kr_cutting_ref SET {', '.join(update_fields)} WHERE id = %s",
                update_params
            )
            db.commit()
            cur.close()
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 400


# ================================================================== #
#  删除（me_engineer + admin）
# ================================================================== #
@cutting_bp.route('/api/cutting-ref/<int:ref_id>', methods=['DELETE'])
def delete_cutting_ref(ref_id):
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT id FROM kr_cutting_ref WHERE id = %s", (ref_id,))
        if not cur.fetchone():
            cur.close()
            return jsonify({'success': False, 'message': '记录不存在'}), 404
        cur.execute("DELETE FROM kr_cutting_ref WHERE id = %s", (ref_id,))
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '删除成功'})


# ================================================================== #
#  批量导入（me_engineer + admin，覆盖式：清空重导）
# ================================================================== #
@cutting_bp.route('/api/cutting-ref/import', methods=['POST'])
def import_cutting_ref_api():
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'success': False, 'message': '请选择要上传的 Excel 文件'}), 400

    filename = (f.filename or '').lower()
    if not (filename.endswith('.xlsx') or filename.endswith('.xlsm')):
        return jsonify({'success': False, 'message': '仅支持 .xlsx 文件'}), 400

    buf = io.BytesIO(f.read())
    try:
        result = import_cutting_ref(buf, truncate=True)
    except Exception as e:
        return jsonify({'success': False, 'message': f'导入失败: {str(e)}'}), 400

    return jsonify({
        'success': True,
        'imported': result['imported'],
        'skipped': result['skipped'],
        'message': f'导入完成：{result["imported"]} 行（跳过空行 {result["skipped"]}）',
    })
