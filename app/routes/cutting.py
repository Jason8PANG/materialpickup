# -*- coding: utf-8 -*-
"""
裁线规格管理路由（kr_cutting_ref）。

权限：
  - 读操作（列表/搜索）：登录即可
  - 写操作（新增/编辑/删除/批量导入）：仅 me_engineer（ME工程师）与 admin
"""
import csv
import io
import json
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request, session

from app.models import get_db_connection, get_site_filter
from app.utils import WhereBuilder
from app.services.cutting_import import import_cutting_ref
from app.services.operation_log import log_from_request

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

    # 站点隔离：各站点独立规格，仅展示当前用户站点数据
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(site_filter, *site_params)

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

    # 落库站点 = 当前用户站点（各站点独立规格）
    siteref = (user.get('siteref') or '').strip() or None
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    columns = list(EDITABLE_FIELDS) + ['siteref']
    placeholders = ', '.join(['%s'] * len(columns))
    sql = f"INSERT INTO kr_cutting_ref ({', '.join(columns)}) VALUES ({placeholders})"
    params = [values[f] for f in EDITABLE_FIELDS] + [siteref]

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

    # 站点隔离：仅允许更新本站点记录（SELECT 存在性校验 + UPDATE 双重带站约束）
    site_filter, site_params = get_site_filter(user)
    site_guard_sql, site_guard_params = "", ()
    if site_filter:
        site_guard_sql = " AND siteref = %s"
        site_guard_params = (site_params[0],)

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"SELECT id FROM kr_cutting_ref WHERE id = %s{site_guard_sql}",
                (ref_id,) + site_guard_params
            )
            if not cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': '记录不存在'}), 404
            update_params.append(ref_id)
            update_params.extend(site_guard_params)
            cur.execute(
                f"UPDATE kr_cutting_ref SET {', '.join(update_fields)} "
                f"WHERE id = %s{site_guard_sql}",
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

    # 站点隔离：仅允许删除本站点记录（SELECT 存在性校验 + DELETE 双重带站约束）
    site_filter, site_params = get_site_filter(user)
    site_guard_sql, site_guard_params = "", ()
    if site_filter:
        site_guard_sql = " AND siteref = %s"
        site_guard_params = (site_params[0],)

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT id FROM kr_cutting_ref WHERE id = %s{site_guard_sql}",
            (ref_id,) + site_guard_params
        )
        if not cur.fetchone():
            cur.close()
            return jsonify({'success': False, 'message': '记录不存在'}), 404
        cur.execute(
            f"DELETE FROM kr_cutting_ref WHERE id = %s{site_guard_sql}",
            (ref_id,) + site_guard_params
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '删除成功'})


# ================================================================== #
#  批量导入（me_engineer + admin，站点化覆盖：仅清空当前站点旧数据后重导）
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
    # 站点化覆盖导入：默认导入当前用户站点；支持显式 site 参数（须在用户可访问站点内）
    site = (request.form.get('site') or '').strip() or (request.args.get('site') or '').strip()
    if not site:
        site = (user.get('siteref') or '').strip()
    available = user.get('available_sites') or []
    if available and site not in available:
        return jsonify({'success': False, 'message': f'无权向站点 {site} 导入裁线规格'}), 403
    if not site:
        return jsonify({'success': False, 'message': '站点信息缺失，无法导入'}), 400
    try:
        result = import_cutting_ref(buf, siteref=site)
    except Exception as e:
        return jsonify({'success': False, 'message': f'导入失败: {str(e)}'}), 400

    return jsonify({
        'success': True,
        'siteref': site,
        'imported': result['imported'],
        'skipped': result['skipped'],
        'message': f'站点 {site} 导入完成：{result["imported"]} 行（跳过空行 {result["skipped"]}，'
                   f'已覆盖该站点原 {result["deleted"]} 行旧数据）',
    })


# ================================================================== #
#  成品豁免名单（cutting_no_cut_exempt）
#    可维护字段：finished_part + note；siteref 自动取登录站点，不可手工指定
#    同一站点内成品料号唯一（接口层拦截，不建库唯一索引）
# ================================================================== #
def _exempt_site_guard(user):
    """返回 (and_sql, params) 站点守卫片段，用于 WHERE 追加 siteref 约束。"""
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        return " AND siteref = %s", (site_params[0],)
    return "", ()


@cutting_bp.route('/api/cutting-exempt', methods=['GET'])
def list_cutting_exempt():
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
        wb.add("(finished_part LIKE %s OR note LIKE %s)", *([f"%{keyword}%"] * 2))

    # 站点隔离：仅展示当前站点豁免名单
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(site_filter, *site_params)

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS total FROM cutting_no_cut_exempt WHERE {where_clause}", params)
        total = cur.fetchone()['total']
        offset = (page - 1) * size
        cur.execute(
            "SELECT id, siteref, finished_part, note, "
            "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created_at "
            f"FROM cutting_no_cut_exempt WHERE {where_clause} ORDER BY id LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cur.fetchall()
        cur.close()

    return jsonify({
        'success': True,
        'data': rows,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size,
        'can_edit': user['role'] in WRITE_ROLES,
    })


@cutting_bp.route('/api/cutting-exempt', methods=['POST'])
def create_cutting_exempt():
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    data = request.get_json() or {}
    finished_part = str(data.get('finished_part') or '').strip()
    if not finished_part:
        return jsonify({'success': False, 'message': '成品料号不能为空'}), 400

    note_raw = data.get('note')
    note = str(note_raw).strip() if note_raw is not None and str(note_raw).strip() else None

    # 落库站点 = 当前用户站点（各站点独立豁免名单）
    siteref = (user.get('siteref') or '').strip() or None
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            # 同站同料号查重
            cur.execute(
                "SELECT id FROM cutting_no_cut_exempt WHERE siteref = %s AND finished_part = %s",
                (siteref, finished_part)
            )
            if cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': '该成品料号已在本站豁免名单中'}), 400

            cur.execute(
                "INSERT INTO cutting_no_cut_exempt (siteref, finished_part, note) VALUES (%s, %s, %s)",
                (siteref, finished_part, note)
            )
            db.commit()
            new_id = cur.lastrowid
            cur.close()
        return jsonify({'success': True, 'id': new_id, 'message': '创建成功'}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 400


@cutting_bp.route('/api/cutting-exempt/<int:exempt_id>', methods=['PUT'])
def update_cutting_exempt(exempt_id):
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    data = request.get_json() or {}
    update_fields = []
    update_params = []
    new_finished_part = None

    if 'finished_part' in data:
        new_finished_part = str(data.get('finished_part') or '').strip()
        if not new_finished_part:
            return jsonify({'success': False, 'message': '成品料号不能为空'}), 400
        update_fields.append("finished_part = %s")
        update_params.append(new_finished_part)
    if 'note' in data:
        note_raw = data.get('note')
        note = str(note_raw).strip() if note_raw is not None and str(note_raw).strip() else None
        update_fields.append("note = %s")
        update_params.append(note)

    if not update_fields:
        return jsonify({'success': False, 'message': '没有需要更新的字段'}), 400

    # 站点隔离：仅允许修改本站记录（SELECT 存在性校验 + UPDATE 双重带站约束）
    site_guard_sql, site_guard_params = _exempt_site_guard(user)

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"SELECT id, siteref FROM cutting_no_cut_exempt WHERE id = %s{site_guard_sql}",
                (exempt_id,) + site_guard_params
            )
            row = cur.fetchone()
            if not row:
                cur.close()
                return jsonify({'success': False, 'message': '记录不存在'}), 404

            # 改成品料号时同站查重（排除自身）
            if new_finished_part is not None:
                cur.execute(
                    "SELECT id FROM cutting_no_cut_exempt "
                    "WHERE siteref = %s AND finished_part = %s AND id <> %s",
                    (row['siteref'], new_finished_part, exempt_id)
                )
                if cur.fetchone():
                    cur.close()
                    return jsonify({'success': False, 'message': '该成品料号已在本站豁免名单中'}), 400

            update_params.append(exempt_id)
            update_params.extend(site_guard_params)
            cur.execute(
                f"UPDATE cutting_no_cut_exempt SET {', '.join(update_fields)} "
                f"WHERE id = %s{site_guard_sql}",
                update_params
            )
            db.commit()
            cur.close()
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 400


@cutting_bp.route('/api/cutting-exempt/<int:exempt_id>', methods=['DELETE'])
def delete_cutting_exempt(exempt_id):
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    # 站点隔离：仅允许删除本站记录（SELECT 存在性校验 + DELETE 双重带站约束）
    site_guard_sql, site_guard_params = _exempt_site_guard(user)

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT id FROM cutting_no_cut_exempt WHERE id = %s{site_guard_sql}",
            (exempt_id,) + site_guard_params
        )
        if not cur.fetchone():
            cur.close()
            return jsonify({'success': False, 'message': '记录不存在'}), 404
        cur.execute(
            f"DELETE FROM cutting_no_cut_exempt WHERE id = %s{site_guard_sql}",
            (exempt_id,) + site_guard_params
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '删除成功'})


# ================================================================== #
#  裁线首末检记录查询（kr_cutting_check）
#    数据由外部接口 POST /api/external/cutting-check 写入，本页不提供「新增」
#    读：登录即可（按站点隔离）
#    编辑 / 软删除：仅 me_engineer（ME工程师）与 admin
#    删除为软删除（is_deleted=1），外部查询接口同步过滤
# ================================================================== #
CHECK_EXPORT_MAX_ROWS = 50000

# 业务字段（按表字段顺序，排除 id / created_at / siteref / is_deleted）
CHECK_FIELDS = (
    'job_order', 'job_part_number', 'part_number', 'cut_length_mm',
    'check_type', 'is_manual',
    'shear_std_length', 'shear_std_tol', 'shear_std_device', 'shear_actual_device',
    'shear_actual_length', 'shear_operator', 'shear_checker',
    'strip_a_std_length', 'strip_a_std_tol', 'strip_a_std_device', 'strip_a_actual_device',
    'strip_a_actual_length', 'strip_a_operator', 'strip_a_checker',
    'strip_b_std_length', 'strip_b_std_tol', 'strip_b_std_device', 'strip_b_actual_device',
    'strip_b_actual_length', 'strip_b_operator', 'strip_b_checker',
    'scrap_length',
)

CHECK_DECIMAL_FIELDS = {
    'cut_length_mm', 'shear_std_length', 'shear_actual_length',
    'strip_a_std_length', 'strip_a_actual_length',
    'strip_b_std_length', 'strip_b_actual_length', 'scrap_length',
}

# NOT NULL 且无默认值的业务字段（编辑后不得为空）
CHECK_REQUIRED_FIELDS = {'job_order', 'job_part_number', 'part_number'}

# 字符串字段最大长度（与表定义一致，超长直接 400 拒绝，不做静默截断）
CHECK_MAXLEN = {
    'job_order': 50, 'job_part_number': 50, 'part_number': 50,
    'shear_std_tol': 20, 'strip_a_std_tol': 20, 'strip_b_std_tol': 20,
    'shear_std_device': 50, 'shear_actual_device': 50,
    'strip_a_std_device': 50, 'strip_a_actual_device': 50,
    'strip_b_std_device': 50, 'strip_b_actual_device': 50,
    'shear_operator': 50, 'shear_checker': 50,
    'strip_a_operator': 50, 'strip_a_checker': 50,
    'strip_b_operator': 50, 'strip_b_checker': 50,
}

# 中文列名：CSV 表头 + 校验报错文案
CHECK_FIELD_LABELS = {
    'job_order': '工单号',
    'job_part_number': '成品物料号',
    'part_number': '线材物料号',
    'cut_length_mm': '下料长度(mm)',
    'check_type': '检查类型',
    'is_manual': '是否手工检查',
    'shear_std_length': '剪切标准长度(mm)',
    'shear_std_tol': '剪切标准公差',
    'shear_std_device': '剪切标准设备',
    'shear_actual_device': '剪切实际设备',
    'shear_actual_length': '剪切实际长度(mm)',
    'shear_operator': '剪切操作员',
    'shear_checker': '剪切检查员',
    'strip_a_std_length': '剥皮A标准长度(mm)',
    'strip_a_std_tol': '剥皮A标准公差',
    'strip_a_std_device': '剥皮A标准设备',
    'strip_a_actual_device': '剥皮A实际设备',
    'strip_a_actual_length': '剥皮A实际长度(mm)',
    'strip_a_operator': '剥皮A操作员',
    'strip_a_checker': '剥皮A检查员',
    'strip_b_std_length': '剥皮B标准长度(mm)',
    'strip_b_std_tol': '剥皮B标准公差',
    'strip_b_std_device': '剥皮B标准设备',
    'strip_b_actual_device': '剥皮B实际设备',
    'strip_b_actual_length': '剥皮B实际长度(mm)',
    'strip_b_operator': '剥皮B操作员',
    'strip_b_checker': '剥皮B检查员',
    'scrap_length': '废料长度(mm)',
}

CHECK_TYPE_LABELS = {'first': '首检', 'last': '末检'}


def _check_site_guard(user):
    """站点守卫片段：(and_sql, params)，非 admin 仅能操作本站记录。"""
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        return " AND siteref = %s", (site_params[0],)
    return "", ()


def _check_query_filters(user):
    """解析首末检查询条件（列表与导出共用）。

    Returns:
        (where_clause, params, error_message)
    """
    job_order = (request.args.get('job_order') or '').strip()
    job_part_number = (request.args.get('job_part_number') or '').strip()
    part_number = (request.args.get('part_number') or '').strip()
    check_type = (request.args.get('check_type') or '').strip().lower()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()

    for label, value in (('date_from', date_from), ('date_to', date_to)):
        if value:
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                return None, None, f'{label} 格式应为 YYYY-MM-DD'

    wb = WhereBuilder(['is_deleted = 0'])
    if job_order:
        wb.add("job_order LIKE %s", f"%{job_order}%")
    if job_part_number:
        wb.add("job_part_number LIKE %s", f"%{job_part_number}%")
    if part_number:
        wb.add("part_number LIKE %s", f"%{part_number}%")
    if check_type in CHECK_TYPE_LABELS:
        wb.add("check_type = %s", check_type)
    if date_from:
        wb.add("created_at >= %s", date_from)
    if date_to:
        # 含当天：用 < date_to + 1 天，避免 <= date_to 丢掉当天 00:00 之后的记录
        wb.add("created_at < DATE_ADD(%s, INTERVAL 1 DAY)", date_to)

    # 站点隔离：仅展示当前用户站点数据
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(site_filter, *site_params)

    where_clause, params = wb.build()
    return where_clause, params, None


def _validate_check_payload(data):
    """校验并归一化编辑入参。

    Returns:
        (values, error_message)：values 只含请求中出现的字段
    """
    values = {}
    for f in CHECK_FIELDS:
        if f not in data:
            continue
        raw = data.get(f)
        label = CHECK_FIELD_LABELS.get(f, f)

        if f == 'check_type':
            v = str(raw or '').strip().lower()
            if v not in CHECK_TYPE_LABELS:
                return None, '检查类型只能是 first（首检）或 last（末检）'
            values[f] = v
            continue

        if f == 'is_manual':
            if raw is None or str(raw).strip() == '':
                values[f] = None
                continue
            s = str(raw).strip().lower()
            if s in ('1', 'true', 'yes', '是'):
                values[f] = 1
            elif s in ('0', 'false', 'no', '否'):
                values[f] = 0
            else:
                return None, '是否手工检查只能是 0 / 1'
            continue

        if f in CHECK_DECIMAL_FIELDS:
            if raw is None or str(raw).strip() == '':
                values[f] = None
                continue
            try:
                num = float(str(raw).strip().replace(',', ''))
            except (TypeError, ValueError):
                return None, f'{label}必须为数字'
            if num < 0:
                return None, f'{label}不能为负数'
            values[f] = round(num, 2)
            continue

        s = '' if raw is None else str(raw).strip()
        if not s:
            if f in CHECK_REQUIRED_FIELDS:
                return None, f'{label}不能为空'
            values[f] = None
            continue
        limit = CHECK_MAXLEN.get(f)
        if limit and len(s) > limit:
            return None, f'{label}长度不能超过 {limit} 个字符'
        values[f] = s

    return values, None


def _same_value(field, old, new):
    """比较新旧值是否等价（DECIMAL 与 float 按数值比较）。"""
    if old is None or new is None:
        return old is None and new is None
    if field in CHECK_DECIMAL_FIELDS:
        try:
            return abs(float(old) - float(new)) < 1e-9
        except (TypeError, ValueError):
            return False
    return str(old).strip() == str(new).strip()


def _json_safe(v):
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def _write_check_log(action, check_id, job_order, detail):
    """首末检记录操作审计（kr_operation_log，独立短连接，失败不影响主业务）。"""
    log_from_request(
        'cutting_check', action,
        siteref=(session.get('user') or {}).get('siteref') or '',
        target_type='cutting_check', target_id=check_id,
        target_key=job_order or '', result='ok', detail=detail,
    )


@cutting_bp.route('/api/cutting-check', methods=['GET'])
def list_cutting_check():
    """首末检记录列表（登录即可，站点隔离，分页）。"""
    user, err = _check_login()
    if err:
        return err

    page = max(1, request.args.get('page', type=int) or 1)
    size = min(max(request.args.get('size', type=int) or 50, 1), 200)

    where_clause, params, ferr = _check_query_filters(user)
    if ferr:
        return jsonify({'success': False, 'message': ferr}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS total FROM kr_cutting_check WHERE {where_clause}", params)
        total = int(cur.fetchone()['total'] or 0)
        offset = (page - 1) * size
        cur.execute(
            f"SELECT id, {', '.join(CHECK_FIELDS)}, siteref, "
            "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created_at "
            f"FROM kr_cutting_check WHERE {where_clause} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cur.fetchall()
        cur.close()

    for r in rows:
        for f in CHECK_DECIMAL_FIELDS:
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


@cutting_bp.route('/api/cutting-check/export', methods=['GET'])
def export_cutting_check():
    """首末检记录导出 CSV（不受分页限制，最多 CHECK_EXPORT_MAX_ROWS 行）。"""
    user, err = _check_login()
    if err:
        return err

    where_clause, params, ferr = _check_query_filters(user)
    if ferr:
        return jsonify({'success': False, 'message': ferr}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"SELECT id, {', '.join(CHECK_FIELDS)}, siteref, "
            "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created_at "
            f"FROM kr_cutting_check WHERE {where_clause} ORDER BY id DESC LIMIT %s",
            params + [CHECK_EXPORT_MAX_ROWS]
        )
        rows = cur.fetchall()
        cur.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['ID'] + [CHECK_FIELD_LABELS[f] for f in CHECK_FIELDS] + ['检查时间', '站点'])
    for r in rows:
        line = [r.get('id')]
        for f in CHECK_FIELDS:
            v = r.get(f)
            if v is None:
                line.append('')
            elif f in CHECK_DECIMAL_FIELDS:
                line.append(f'{float(v):.2f}')
            else:
                line.append(str(v))
        if r.get('check_type') in CHECK_TYPE_LABELS:
            line[CHECK_FIELDS.index('check_type') + 1] = CHECK_TYPE_LABELS[r['check_type']]
        line.append(r.get('created_at') or '')
        line.append(r.get('siteref') or '')
        writer.writerow(line)

    payload = buf.getvalue().encode('utf-8-sig')  # BOM：Excel 直接打开不乱码
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    filename = f'裁线首末检记录_{ts}.csv'
    resp = Response(payload, mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = (
        f"attachment; filename=cutting_checks_{ts}.csv; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return resp


@cutting_bp.route('/api/cutting-check/<int:check_id>', methods=['PUT'])
def update_cutting_check(check_id):
    """编辑首末检记录（仅 me_engineer / admin，单条、按站点约束）。"""
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    data = request.get_json() or {}
    values, verr = _validate_check_payload(data)
    if verr:
        return jsonify({'success': False, 'message': verr}), 400
    if not values:
        return jsonify({'success': False, 'message': '没有需要更新的字段'}), 400

    guard_sql, guard_params = _check_site_guard(user)

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"SELECT id, {', '.join(CHECK_FIELDS)} FROM kr_cutting_check "
                f"WHERE id = %s{guard_sql} AND is_deleted = 0",
                (check_id,) + guard_params
            )
            old = cur.fetchone()
            if not old:
                cur.close()
                return jsonify({'success': False, 'message': '记录不存在或已删除'}), 404

            changes = {}
            for f, new in values.items():
                if not _same_value(f, old.get(f), new):
                    changes[f] = {'old': _json_safe(old.get(f)), 'new': _json_safe(new)}

            if not changes:
                cur.close()
                return jsonify({'success': True, 'message': '无变更', 'changed': 0})

            set_sql = ', '.join(f"{f} = %s" for f in values)
            cur.execute(
                f"UPDATE kr_cutting_check SET {set_sql} "
                f"WHERE id = %s{guard_sql} AND is_deleted = 0",
                list(values.values()) + [check_id] + list(guard_params)
            )
            db.commit()
            cur.close()

        _write_check_log('CUTTING_CHECK_UPDATE', check_id, old.get('job_order'), {
            'id': check_id,
            'job_order': old.get('job_order'),
            'changes': changes,
        })
        return jsonify({'success': True, 'message': '更新成功', 'changed': len(changes)})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 400


@cutting_bp.route('/api/cutting-check/<int:check_id>', methods=['DELETE'])
def delete_cutting_check(check_id):
    """软删除首末检记录（仅 me_engineer / admin）：is_deleted=1。"""
    user, err = _check_login()
    if err:
        return err
    perm_err = _check_write(user)
    if perm_err:
        return perm_err

    guard_sql, guard_params = _check_site_guard(user)

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT id, job_order, job_part_number, part_number, check_type "
                f"FROM kr_cutting_check WHERE id = %s{guard_sql} AND is_deleted = 0",
                (check_id,) + guard_params
            )
            row = cur.fetchone()
            if not row:
                cur.close()
                return jsonify({'success': False, 'message': '记录不存在或已删除'}), 404

            cur.execute(
                f"UPDATE kr_cutting_check SET is_deleted = 1 "
                f"WHERE id = %s{guard_sql} AND is_deleted = 0",
                (check_id,) + guard_params
            )
            db.commit()
            cur.close()

        _write_check_log('CUTTING_CHECK_DELETE', check_id, row.get('job_order'), {
            'id': check_id,
            'job_order': row.get('job_order'),
            'job_part_number': row.get('job_part_number'),
            'part_number': row.get('part_number'),
            'check_type': row.get('check_type'),
        })
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'}), 400
