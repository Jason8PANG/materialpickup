from flask import Blueprint, request, jsonify, session
from datetime import datetime
from pymysql.err import IntegrityError
from app.models import get_db_connection, get_site_filter
from app.utils import WhereBuilder
from app.config import Config
from app.services.label_print_service import test_printer_connection, print_test_page

VALID_PRINT_CHANNELS = ('gdi', 'raw_zpl', 'raw_tspl', 'gateway')

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


def _normalize_site_access(raw, role, default_site):
    """规范化 site_access：
    - 接受逗号分隔字符串或列表；去空格、过滤非法站点、去重、排序后 join；
    - 空结果返回 None（= 仅默认站，由登录侧兜底）；
    - 非空列表且未含默认站 siteref 时自动补入（默认站必然可访问）；
    - admin 返回 None（登录时自动全站）。
    """
    if role == 'admin':
        return None
    parts = []
    if isinstance(raw, str):
        parts = raw.split(',')
    elif isinstance(raw, (list, tuple)):
        parts = raw
    sites = []
    for p in parts:
        p = str(p).strip()
        if p and p in Config.SITE_CONFIG and p not in sites:
            sites.append(p)
    if not sites:
        return None
    if default_site and default_site in Config.SITE_CONFIG and default_site not in sites:
        sites.append(default_site)
    sites.sort()
    return ','.join(sites)


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

    valid_roles = ('admin', 'requester', 'supervisor', 'warehouse', 'me_engineer')
    if data['role'] not in valid_roles:
        return jsonify({'success': False, 'message': f'无效角色，可选: {", ".join(valid_roles)}'}), 400

    # 站点验证：非 admin / me_engineer 角色必须填 siteref（ME工程师可跨站，siteref 允许空）
    siteref = data.get('siteref')
    if siteref is not None:
        siteref = str(siteref).strip() or None
    if data['role'] not in ('admin', 'me_engineer') and not siteref:
        return jsonify({'success': False, 'message': '非管理员/ME工程师角色必须指定所属站点'}), 400
    if siteref and siteref not in Config.SITE_CONFIG:
        return jsonify({'success': False, 'message': f'无效站点，可选: {", ".join(Config.SITE_CONFIG.keys())}'}), 400

    # site_access：逗号分隔可访问站点，可为空（= 仅默认站）；非空时自动补入默认站兜底
    site_access = _normalize_site_access(data.get('site_access'), data['role'], siteref)

    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute(
                "INSERT INTO kr_role_mapping "
                "(domain_account, display_name, role, siteref, site_access, email, is_active, remark) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    data['domain_account'].strip(),
                    data.get('display_name', '').strip(),
                    data['role'],
                    siteref,
                    site_access,
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

            # 更新后的角色与默认站（用于 site_access 规范化：角色为 admin 时置空、默认站兜底补入）
            role_after = data.get('role') or existing.get('role')
            if 'siteref' in data:
                siteref_after = str(data['siteref']).strip() if data['siteref'] else None
            else:
                siteref_after = existing.get('siteref')

            update_fields = []
            update_params = []

            for field in ['display_name', 'role', 'email', 'is_active', 'remark', 'domain_account', 'siteref', 'site_access']:
                if field in data:
                    val = data[field]
                    # siteref 空字符串转为 None
                    if field == 'siteref':
                        val = val.strip() if val else None
                    # site_access 规范化：过滤/去重/补默认站，admin 置空
                    elif field == 'site_access':
                        val = _normalize_site_access(val, role_after, siteref_after)
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


# ================= 站点打印机配置 =================

@admin_bp.route('/api/site-printers', methods=['GET'])
def list_site_printers():
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_site_printer ORDER BY siteref")
        rows = cursor.fetchall()
        cursor.close()

    return jsonify({'success': True, 'data': rows})


@admin_bp.route('/api/site-printers', methods=['POST'])
def create_site_printer():
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    siteref = str(data.get('siteref') or '').strip()
    printer_name = str(data.get('printer_name') or '').strip()
    channel = str(data.get('channel') or 'gdi').strip().lower()
    remark = str(data.get('remark') or '').strip() or None

    if siteref not in Config.SITE_CONFIG:
        return jsonify({'success': False, 'message': f'无效站点，可选: {", ".join(Config.SITE_CONFIG.keys())}'}), 400
    if not printer_name:
        return jsonify({'success': False, 'message': '缺少必填字段: printer_name'}), 400
    if channel not in VALID_PRINT_CHANNELS:
        return jsonify({'success': False, 'message': f'无效通道，可选: {", ".join(VALID_PRINT_CHANNELS)}'}), 400

    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute(
                "INSERT INTO kr_site_printer (siteref, printer_name, channel, remark) "
                "VALUES (%s, %s, %s, %s)",
                (siteref, printer_name, channel, remark)
            )
            db.commit()
            new_id = cursor.lastrowid
            cursor.close()
        return jsonify({'success': True, 'id': new_id, 'message': '创建成功'}), 201
    except IntegrityError:
        return jsonify({'success': False, 'message': '该站点已配置'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 400


@admin_bp.route('/api/site-printers/<int:printer_id>', methods=['PUT'])
def update_site_printer(printer_id):
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM kr_site_printer WHERE id = %s", (printer_id,))
            if not cursor.fetchone():
                cursor.close()
                return jsonify({'success': False, 'message': '记录不存在'}), 404

            update_fields = []
            update_params = []

            if 'printer_name' in data:
                val = str(data.get('printer_name') or '').strip()
                if not val:
                    cursor.close()
                    return jsonify({'success': False, 'message': 'printer_name 不能为空'}), 400
                update_fields.append("printer_name = %s")
                update_params.append(val)

            if 'channel' in data:
                val = str(data.get('channel') or '').strip().lower()
                if val not in VALID_PRINT_CHANNELS:
                    cursor.close()
                    return jsonify({'success': False, 'message': f'无效通道，可选: {", ".join(VALID_PRINT_CHANNELS)}'}), 400
                update_fields.append("channel = %s")
                update_params.append(val)

            if 'remark' in data:
                update_fields.append("remark = %s")
                update_params.append(str(data.get('remark') or '').strip() or None)

            if not update_fields:
                cursor.close()
                return jsonify({'success': False, 'message': '没有需要更新的字段'}), 400

            update_params.append(printer_id)
            cursor.execute(
                f"UPDATE kr_site_printer SET {', '.join(update_fields)} WHERE id = %s",
                update_params
            )
            db.commit()
            cursor.close()
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 400


@admin_bp.route('/api/site-printers/<int:printer_id>', methods=['DELETE'])
def delete_site_printer(printer_id):
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_site_printer WHERE id = %s", (printer_id,))
        if not cursor.fetchone():
            cursor.close()
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        cursor.execute("DELETE FROM kr_site_printer WHERE id = %s", (printer_id,))
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '删除成功'})


def _get_site_printer_row(printer_id):
    """按 id 查站点打印机记录，返回 row 或 None"""
    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_site_printer WHERE id = %s", (printer_id,))
        row = cursor.fetchone()
        cursor.close()
    return row


@admin_bp.route('/api/site-printers/<int:printer_id>/test-connection', methods=['POST'])
def test_site_printer_connection(printer_id):
    """连接测试：按配置通道探测打印机可达性（不实际打印）"""
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    row = _get_site_printer_row(printer_id)
    if not row:
        return jsonify({'success': False, 'message': '记录不存在'}), 404

    result = test_printer_connection(row['printer_name'], row['channel'])
    return jsonify({
        'success': bool(result.get('success')),
        'message': result.get('message', ''),
        'detail': result.get('detail', ''),
        'printer_name': row['printer_name'],
        'channel': row['channel'],
    })


@admin_bp.route('/api/site-printers/<int:printer_id>/test-print', methods=['POST'])
def test_site_printer_print(printer_id):
    """测试页打印：按配置通道实际打印一张固定测试标签"""
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    row = _get_site_printer_row(printer_id)
    if not row:
        return jsonify({'success': False, 'message': '记录不存在'}), 404

    result = print_test_page(row['printer_name'], row['channel'])
    return jsonify({
        'success': bool(result.get('success')),
        'message': result.get('message', ''),
        'detail': result.get('detail', ''),
        'printed': result.get('printed', 0),
        'errors': result.get('errors', []),
        'printer_name': row['printer_name'],
        'channel': row['channel'],
    })


# ============================================================
#  角色—页面权限（5 角色 × 18 页面矩阵，admin 恒全开）
# ============================================================
@admin_bp.route('/api/admin/roles/permissions', methods=['GET'])
def get_role_permissions_api():
    """读取当前矩阵（管理页渲染兜底用）"""
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    from app.page_registry import ROLES, ROLE_LABELS, PAGES
    from app.services.permission_service import get_matrix
    return jsonify({
        'success': True,
        'roles': ROLES,
        'role_labels': ROLE_LABELS,
        'pages': [{'key': p['key'], 'name': p['name'], 'group': p['group']} for p in PAGES],
        'matrix': get_matrix(),
    })


@admin_bp.route('/api/admin/roles/permissions', methods=['POST'])
def save_role_permissions():
    """保存矩阵：body = {"warehouse": ["kanban", ...], "requester": [...]}（不含 admin）

    每个角色全量覆盖；保存后清缓存立即生效，并写 kr_operation_log（含前后差异）。
    """
    user, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({'success': False, 'message': '请求体应为 {角色: [页面key,...]}'}), 400

    from app.page_registry import EDITABLE_ROLES
    from app.services.permission_service import set_role_permissions

    unknown = [r for r in data if r not in EDITABLE_ROLES]
    if unknown:
        return jsonify({'success': False,
                        'message': f'不可编辑的角色: {", ".join(unknown)}（admin 始终全开）'}), 400
    for role, keys in data.items():
        if not isinstance(keys, (list, tuple)):
            return jsonify({'success': False, 'message': f'角色 {role} 的权限应为数组'}), 400

    try:
        results = [
            set_role_permissions(role, keys, operator=user.get('username') or 'SYSTEM')
            for role, keys in data.items()
        ]
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'保存失败: {e}'}), 500

    return jsonify({
        'success': True,
        'message': '权限已保存',
        'changed': [{'role': r['role'], 'added': r['added'], 'removed': r['removed']}
                    for r in results],
    })


# ============================================================
#  CSI 失败记录定时邮件（收件人规则按「站点 + 接口名」配置）
#  数据/发送逻辑见 app/services/csi_alert_service.py，
#  时间判定与常驻循环见 app/csi_alert_scheduler.py。
# ============================================================
@admin_bp.route('/api/admin/csi-alert/rules', methods=['GET'])
def list_csi_alert_rules():
    """规则列表 + 前端下拉用的站点/接口候选"""
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    from app.services.csi_alert_service import ensure_schema, list_rules, api_names
    try:
        ensure_schema()
        rules = list_rules()
        apis = api_names()
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取规则失败: {e}'}), 500

    return jsonify({
        'success': True,
        'data': rules,
        'apis': apis,
        'sites': {k: v for k, v in Config.SITE_CONFIG.items()},
    })


@admin_bp.route('/api/admin/csi-alert/rules', methods=['POST'])
def save_csi_alert_rule():
    """新增/更新规则（body 含 id 则按 id 更新）"""
    user, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': '请求体应为 JSON 对象'}), 400

    from app.services.csi_alert_service import ensure_schema, save_rule, log_audit
    try:
        ensure_schema(with_default_rules=False)
        ok, message, rule_id = save_rule(data, operator=user.get('username') or 'SYSTEM')
    except Exception as e:
        log_audit('save_rule', {'body': data, 'error': str(e)}, result='fail',
                  operator=user.get('username') or 'SYSTEM', siteref=str(data.get('siteref') or ''))
        return jsonify({'success': False, 'message': f'保存失败: {e}'}), 500

    log_audit('save_rule', {'body': data, 'rule_id': rule_id, 'ok': ok, 'message': message},
              result='ok' if ok else 'fail',
              operator=user.get('username') or 'SYSTEM', siteref=str(data.get('siteref') or ''))
    if not ok:
        return jsonify({'success': False, 'message': message}), 400
    return jsonify({'success': True, 'message': message, 'id': rule_id})


@admin_bp.route('/api/admin/csi-alert/rules/<int:rule_id>', methods=['DELETE'])
def delete_csi_alert_rule(rule_id):
    user, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    from app.services.csi_alert_service import delete_rule, log_audit
    try:
        ok = delete_rule(rule_id)
    except Exception as e:
        log_audit('delete_rule', {'rule_id': rule_id, 'error': str(e)}, result='fail',
                  operator=user.get('username') or 'SYSTEM')
        return jsonify({'success': False, 'message': f'删除失败: {e}'}), 500

    log_audit('delete_rule', {'rule_id': rule_id, 'deleted': ok},
              result='ok' if ok else 'fail', operator=user.get('username') or 'SYSTEM')
    if not ok:
        return jsonify({'success': False, 'message': '规则不存在（可能已被删除）'}), 404
    return jsonify({'success': True, 'message': '已删除'})


@admin_bp.route('/api/admin/csi-alert/test', methods=['POST'])
def test_csi_alert_send():
    """立即测试发送：用当前规则实发一封（slot='manual'，force 语义绕过幂等）"""
    user, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    from app.csi_alert_scheduler import run_slot
    from app.services.csi_alert_service import log_audit
    try:
        summary = run_slot('manual', force=True)
    except Exception as e:
        log_audit('test_send', {'error': str(e)}, result='fail',
                  operator=user.get('username') or 'SYSTEM')
        return jsonify({'success': False, 'message': f'测试发送失败: {e}'}), 500

    log_audit('test_send', summary, result='ok', operator=user.get('username') or 'SYSTEM')
    sent = [g for g in summary['groups'] if g.get('status') == 'sent']
    return jsonify({
        'success': bool(sent),
        'message': (f"已发送 {len(sent)} 封（共 {summary['total_rows']} 条失败记录）"
                    if sent else ('当前没有待处理的失败记录' if summary['total_rows'] == 0
                                  else '未发送，详见明细')),
        'data': summary,
    })


@admin_bp.route('/api/admin/csi-alert/logs', methods=['GET'])
def list_csi_alert_logs():
    """最近发送记录（读 kr_csi_alert_log）"""
    _, err_resp, err_code = check_admin()
    if err_resp:
        return err_resp, err_code

    from app.services.csi_alert_service import ensure_schema, list_logs
    try:
        ensure_schema(with_default_rules=False)
        rows = list_logs(request.args.get('limit', 20))
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取发送记录失败: {e}'}), 500
    return jsonify({'success': True, 'data': rows})


# ============================================================
#  邮件发送日志（kr_email_log）
#  写入见 app/services/email_log.py（send_email 内部调用）；
#  页面 /admin/email-log 仅 admin，接口按登录用户的站点隔离。
# ============================================================
@admin_bp.route('/api/admin/email-log', methods=['GET'])
def list_email_logs():
    """邮件发送日志查询。

    参数：module / result（精确）、to_emails / subject / related_key（LIKE）、
          date_from / date_to（DATE(created_at)）、page / size（默认 1/100，≤500）
    """
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    from app.services.email_log import ensure_schema
    ensure_schema()

    wb = WhereBuilder(["1=1"])

    module = (request.args.get('module') or '').strip()
    if module:
        wb.add("module = %s", module)
    result = (request.args.get('result') or '').strip()
    if result:
        wb.add("result = %s", result)
    to_emails = (request.args.get('to_emails') or '').strip()
    if to_emails:
        wb.add("to_emails LIKE %s", f"%{to_emails}%")
    subject = (request.args.get('subject') or '').strip()
    if subject:
        wb.add("subject LIKE %s", f"%{subject}%")
    related_key = (request.args.get('related_key') or '').strip()
    if related_key:
        wb.add("related_key LIKE %s", f"%{related_key}%")
    date_from = (request.args.get('date_from') or '').strip()
    if date_from:
        wb.add("DATE(created_at) >= %s", date_from)
    date_to = (request.args.get('date_to') or '').strip()
    if date_to:
        wb.add("DATE(created_at) <= %s", date_to)

    # 站点隔离：admin 不过滤，其余角色只看本站
    site_clause, site_params = get_site_filter(user)
    if site_clause:
        wb.add(site_clause, *site_params)

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.args.get('size', 100))
    except (TypeError, ValueError):
        size = 100
    size = max(1, min(size, 500))

    where_clause, params = wb.build()
    offset = (page - 1) * size

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(f"SELECT COUNT(*) AS c FROM kr_email_log WHERE {where_clause}", params)
        total = int(cursor.fetchone()['c'])

        summary = {'ok': 0, 'fail': 0, 'skip': 0}
        cursor.execute(
            f"SELECT result, COUNT(*) AS c FROM kr_email_log WHERE {where_clause} GROUP BY result",
            params,
        )
        for row in cursor.fetchall():
            if row['result'] in summary:
                summary[row['result']] = int(row['c'])

        cursor.execute(
            "SELECT id, module, subject, to_emails, siteref, related_type, related_id, "
            "related_key, result, errormessage, body_preview, created_at "
            f"FROM kr_email_log WHERE {where_clause} "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            params + [size, offset],
        )
        rows = cursor.fetchall()
        cursor.close()

    for r in rows:
        if isinstance(r.get('created_at'), datetime):
            r['created_at'] = r['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        if r.get('related_id') is not None:
            r['related_id'] = int(r['related_id'])

    total_pages = (total + size - 1) // size if total else 0
    return jsonify({
        'success': True,
        'data': rows,
        'total': total,
        'page': page,
        'size': size,
        'total_pages': total_pages,
        'summary': summary,
    })
