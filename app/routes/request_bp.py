from flask import Blueprint, request, jsonify, session
from datetime import datetime
from app.models import get_db_connection, get_site_filter, STATUS_LABELS
from app.utils import WhereBuilder
from app.config import Config
from app.routes.approval import generate_approval_token
from app.services.email_service import send_approval_email

request_bp = Blueprint('request_bp', __name__)


@request_bp.route('/api/requests', methods=['POST'])
def create_request():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json()
    items = data.get('items', [])

    if not items:
        return jsonify({'success': False, 'message': '请至少添加一行物料明细'}), 400

    # 校验每行必填
    for i, item in enumerate(items):
        if not item.get('job_order') or not item.get('part_number') or not item.get('quantity'):
            return jsonify({'success': False, 'message': f'第{i+1}行缺少必填字段（工单号、物料号、数量）'}), 400

    # 校验工单状态（必须为 R）
    from app.services.csi_service import CSIClient, parse_job
    client = CSIClient(siteref=user.get('siteref', '310'))
    bad_jobs = []
    for i, item in enumerate(items):
        job_order = item['job_order'].strip()
        job, suffix = parse_job(job_order)
        if not job:
            bad_jobs.append(f"第{i+1}行工单号格式无效: {job_order}")
            continue
        job_info = client.validate_job(job, suffix)
        if not job_info:
            bad_jobs.append(f"第{i+1}行工单不存在: {job_order}")
            continue
        stat = (job_info.get('Stat') or '').strip()
        if stat != 'R':
            bad_jobs.append(f"第{i+1}行工单 {job_order} 状态为 {stat}，仅允许状态 R 的工单提交")

    if bad_jobs:
        return jsonify({'success': False, 'message': '<br>'.join(bad_jobs[:5])}), 400

    # 校验申请数量不能超过库存量
    for i, item in enumerate(items):
        qty = float(item['quantity'])
        stock_qty = float(item['stock_qty']) if item.get('stock_qty') else None
        if stock_qty is not None and qty > stock_qty:
            return jsonify({'success': False, 'message': f'第{i+1}行申请数量({qty})超过库存量({stock_qty})，请修改'}), 400

    siteref = user.get('siteref', '')
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        now = datetime.now()

        # 1. 插入主表（直接 pending_prep，跳过审批）
        cursor.execute(
            """INSERT INTO kr_material_request 
            (siteref, requester, request_time, status, remark, is_urgent)
            VALUES (%s, %s, %s, 'pending_prep', %s, %s)""",
            (siteref, user['username'], now, data.get('remark', ''), data.get('is_urgent', 0))
        )
        request_id = cursor.lastrowid

        # 2. 批量插入明细表
        item_data = []
        for item in items:
            qty = float(item['quantity'])
            price = float(item['price']) if item.get('price') else None
            total = qty * price if price else None
            item_data.append((
                request_id,
                item['job_order'].strip(),
                item['part_number'].strip(),
                qty,
                price,
                total,
                float(item['stock_qty']) if item.get('stock_qty') else None,
                item.get('replenish_reason'),
                item.get('replenish_reason_other'),
                item.get('stock_loc')
            ))

        cursor.executemany(
            """INSERT INTO kr_request_item 
            (request_id, job_order, part_number, quantity, price, total_amount, 
             stock_qty, replenish_reason, replenish_reason_other, stock_loc)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            item_data
        )

        # 3. 记录日志
        first_item = items[0]
        item_summary = f"{first_item['part_number']} x {first_item['quantity']}"
        if len(items) > 1:
            item_summary += f" 等{len(items)}项"
        cursor.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'SUBMIT', %s, %s, %s)",
            (request_id, user['username'], f"提交领料申请: {item_summary}",
             request.remote_addr, now)
        )
        db.commit()
        cursor.close()

    # 提交后发通知邮件（重复申请 或 累计金额超$30，发给当前站点主管）
    try:
        with get_db_connection() as db:
            # 查询当前站点主管邮箱
            cursor = db.cursor()
            cursor.execute(
                "SELECT email FROM kr_role_mapping "
                "WHERE role = 'supervisor' AND is_active = 1 AND siteref = %s "
                "AND email IS NOT NULL AND email != ''",
                (siteref,)
            )
            sup_emails = [r['email'] for r in cursor.fetchall()]
            cursor.close()

            alert_msgs = []
            # 同工单+同物料是否已在其他未完成申请中
            for item in items:
                cursor = db.cursor()
                cursor.execute(
                    """SELECT r.id, r.requester FROM kr_request_item ri
                    JOIN kr_material_request r ON r.id = ri.request_id
                    WHERE ri.job_order = %s AND ri.part_number = %s
                    AND r.id != %s AND r.status IN ('pending_prep','prepping','short','ready_pickup')
                    AND r.is_deleted = 0 LIMIT 5""",
                    (item['job_order'].strip(), item['part_number'].strip(), request_id)
                )
                for d in cursor.fetchall():
                    alert_msgs.append(
                        f"工单 {item['job_order']} 物料 {item['part_number']} 已在申请单 #{d['id']}({d['requester']}) 中"
                    )
                cursor.close()

            # 同一工单在所有申请单（含已完成）中的累计金额 >= $30
            current_job_orders = set()
            for it in items:
                jo = it['job_order'].strip()
                current_job_orders.add(jo)
            if current_job_orders:
                placeholders = ','.join(['%s'] * len(current_job_orders))
                cursor = db.cursor()
                cursor.execute(
                    f"""SELECT ri.job_order, SUM(ri.total_amount) as total
                    FROM kr_request_item ri
                    JOIN kr_material_request r ON r.id = ri.request_id
                    WHERE ri.job_order IN ({placeholders})
                    AND r.is_deleted = 0
                    GROUP BY ri.job_order""",
                    list(current_job_orders)
                )
                for row in cursor.fetchall():
                    jo = row['job_order']
                    total = float(row['total'] or 0)
                    if total >= 30:
                        alert_msgs.append(f"工单 {jo} 所有申请单累计金额 ${total:.2f} ≥ $30")
                cursor.close()

        # 在 with 块外发邮件（连接已关闭，但数据已在 alert_msgs 中）
        if alert_msgs:
            from app.services.email_service import send_email
            subject = f"[物料领取提醒] {user['username']} #{request_id}"
            body = "<h3>物料申请提醒</h3>"
            body += f"<p>申请人: {user['username']} | 站点: {siteref} | "
            body += f"<a href='{Config.BASE_URL}/request/{request_id}'>申请单 #{request_id}</a></p><hr><ul>"
            for m in alert_msgs:
                body += f"<li>{m}</li>"
            body += "</ul><p style='color:#999;font-size:12px;'>物料领取看板系统自动通知</p>"
            for email in sup_emails:
                print(f"[NOTIFICATION] 发送提醒邮件至 {email}")
                send_email(email, subject, body)
        else:
            print(f"[NOTIFICATION] 无触发条件，不发送邮件（共检查 {len(sup_emails)} 个主管邮箱）")
    except Exception as e:
        print(f"[NOTIFICATION ERROR] {e}")

    return jsonify({'success': True, 'id': request_id, 'message': '申请提交成功'})


@request_bp.route('/api/requests/<int:request_id>/cancel', methods=['POST'])
def cancel_request(request_id):
    """发起人取消申请单（仅 pending_prep 状态可取消，兼容旧 pending_approval）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

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

        # 仅申请人和管理员可取消
        if req['requester'] != user['username'] and user['role'] != 'admin':
            cursor.close()
            return jsonify({'success': False, 'message': '只有发起人和管理员可取消此申请单'}), 403

        # 站点校验
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cursor.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        if req['status'] not in ('pending_prep', 'pending_approval'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许取消'}), 400

        from datetime import datetime
        now = datetime.now()
        # 逻辑删除
        cursor.execute(
            "UPDATE kr_material_request SET is_deleted = 1 WHERE id = %s",
            (request_id,)
        )
        # 记录操作日志
        cursor.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'CANCEL', %s, %s, %s)",
            (request_id, user['username'], f"取消申请单", request.remote_addr, now)
        )
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '申请单已取消'})


@request_bp.route('/api/requests/<int:request_id>', methods=['GET'])
def get_request(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 站点隔离检查
        site_filter, site_params = get_site_filter(user)
        if site_filter:
            if row['siteref'] != site_params[0]:
                cursor.close()
                return jsonify({'success': False, 'message': '无权查看此站点单据'}), 403

        # 获取明细
        cursor.execute(
            "SELECT * FROM kr_request_item WHERE request_id = %s ORDER BY id",
            (request_id,)
        )
        items = cursor.fetchall()

        # 获取操作日志
        cursor.execute(
            "SELECT * FROM kr_operation_log WHERE request_id = %s ORDER BY created_at",
            (request_id,)
        )
        logs = cursor.fetchall()
        cursor.close()

    result = dict(row)
    result['status_label'] = STATUS_LABELS.get(row['status'], row['status'])
    result['items'] = items
    result['item_count'] = len(items)
    # 计算总金额
    total_amount = 0
    for item in items:
        if item.get('total_amount'):
            total_amount += float(item['total_amount'])
    result['total_amount'] = round(total_amount, 2)

    return jsonify({'success': True, 'request': result, 'logs': logs})


@request_bp.route('/api/requests', methods=['GET'])
def list_requests():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    status = request.args.get('status')
    job_order = request.args.get('job_order')
    part_number = request.args.get('part_number')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 20))

    wb = WhereBuilder(["r.is_deleted = 0"])

    # 站点过滤
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f"r.{site_filter}", *site_params)

    if status:
        wb.add("r.status = %s", status)
    if job_order:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND job_order LIKE %s)", f"%{job_order}%")
    if part_number:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND part_number LIKE %s)", f"%{part_number}%")

    where_clause, params = wb.build()

    with get_db_connection() as db:
        cursor = db.cursor()

        # 总数
        cursor.execute(f"SELECT COUNT(*) as total FROM kr_material_request r WHERE {where_clause}", params)
        total = cursor.fetchone()['total']

        # 分页 - 使用子查询获取 item_count
        offset = (page - 1) * size
        cursor.execute(
            f"""SELECT r.*, 
                (SELECT COUNT(*) FROM kr_request_item WHERE request_id = r.id) as item_count,
                (SELECT job_order FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_job_order
                FROM kr_material_request r 
                WHERE {where_clause} 
                ORDER BY r.id DESC LIMIT %s OFFSET %s""",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

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


@request_bp.route('/api/requests/history', methods=['GET'])
def history():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    status = request.args.get('status')
    job_order = request.args.get('job_order')
    part_number = request.args.get('part_number')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 20))

    wb = WhereBuilder(["r.is_deleted = 0"])

    # 站点过滤
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f"r.{site_filter}", *site_params)

    if status:
        wb.add("r.status = %s", status)
    if job_order:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND job_order LIKE %s)", f"%{job_order}%")
    if part_number:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND part_number LIKE %s)", f"%{part_number}%")
    if date_from:
        wb.add("r.request_time >= %s", date_from)
    if date_to:
        wb.add("r.request_time <= %s", f"{date_to} 23:59:59")

    # 只查已完成和已驳回
    wb.add("(r.status = 'completed' OR r.status = 'rejected')")

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
                ORDER BY r.id DESC LIMIT %s OFFSET %s""",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

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


@request_bp.route('/api/records', methods=['GET'])
def records():
    """综合记录查询 - 展开明细行，按时间筛选"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 100))

    conditions = ["r.is_deleted = 0"]
    params = []

    # 站点隔离
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        conditions.append(f"r.{site_filter}")
        params.extend(site_params)

    if date_from:
        conditions.append("r.request_time >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("r.request_time <= %s")
        params.append(f"{date_to} 23:59:59")

    where = " AND ".join(conditions)

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(f"SELECT COUNT(DISTINCT r.id) as total FROM kr_material_request r WHERE {where}", params)
        total = cursor.fetchone()['total']

        offset = (page - 1) * size
        sql = f"""SELECT r.id, r.requester, r.request_time, r.supervisor, r.approve_time,
            r.warehouse_operator, r.signer, r.sign_time, r.siteref, r.status,
            r.short_reason, r.remark,
            i.job_order, i.part_number, i.quantity, i.price, i.total_amount,
            i.stock_qty, i.short_reason as item_short_reason
            FROM kr_material_request r 
            LEFT JOIN kr_request_item i ON i.request_id = r.id
            WHERE {where}
            ORDER BY r.id DESC, i.id ASC
            LIMIT %s OFFSET %s"""
        cursor.execute(sql, params + [size, offset])
        rows = cursor.fetchall()
        cursor.close()

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


@request_bp.route('/api/requests/minpack', methods=['POST'])
def create_minpack_request():
    """创建最小包装物料申请（无需审批，直接到待备料）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json()
    items = data.get('items', [])
    if not items:
        return jsonify({'success': False, 'message': '请至少添加一行物料'}), 400

    siteref = user.get('siteref', '')
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    now = datetime.now()

    with get_db_connection() as db:
        cursor = db.cursor()

        # 插入主表 - 状态直接到 pending_prep（待备料），不需要审批
        cursor.execute(
            """INSERT INTO kr_material_request 
            (siteref, request_type, requester, request_time, status, remark, is_urgent)
            VALUES (%s, 'minpack', %s, %s, 'pending_prep', %s, %s)""",
            (siteref, user['username'], now, data.get('remark', ''), data.get('is_urgent', 0))
        )
        request_id = cursor.lastrowid

        # 插入明细
        item_data = []
        for item in items:
            qty = float(item['quantity'])
            price = float(item['price']) if item.get('price') else 0
            total = qty * price
            stock_qty = float(item['stock_qty']) if item.get('stock_qty') else None
            stock_loc = item.get('stock_loc')
            item_data.append((
                request_id, item['part_number'], qty, price, total,
                stock_qty, stock_loc
            ))

        cursor.executemany(
            """INSERT INTO kr_request_item 
            (request_id, job_order, part_number, quantity, price, total_amount, stock_qty, stock_loc)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)""",
            item_data
        )

        # 操作日志
        cursor.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'SUBMIT_MINPACK', %s, %s, %s)",
            (request_id, user['username'],
             f"提交最小包装申请: {len(items)}项物料",
             request.remote_addr, now)
        )
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'id': request_id, 'message': '最小包装申请已提交，仓库将开始备料'})


@request_bp.route('/api/requests/check-duplicate-part', methods=['GET'])
def check_duplicate_part():
    """检查物料是否已在未完成的申请单中（用于最小包装重复提醒）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    part_number = request.args.get('part_number', '').strip()
    if not part_number:
        return jsonify({'success': False, 'message': '请输入Part Number'}), 400

    siteref = user.get('siteref')
    conditions = ["i.part_number = %s", "r.status IN ('pending_prep', 'prepping')", "r.is_deleted = 0"]
    params = [part_number]

    if siteref and user.get('role') != 'admin':
        conditions.append("r.siteref = %s")
        params.append(siteref)

    where = " AND ".join(conditions)

    with get_db_connection() as db:
        cursor = db.cursor()
        sql = f"""SELECT DISTINCT r.id, r.status, r.requester, r.request_time
            FROM kr_material_request r
            JOIN kr_request_item i ON i.request_id = r.id
            WHERE {where}
            ORDER BY r.id DESC LIMIT 10"""
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()

    if not rows:
        return jsonify({'success': True, 'duplicate': False, 'requests': []})

    from app.models import STATUS_LABELS
    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'status': row['status'],
            'status_label': STATUS_LABELS.get(row['status'], row['status']),
            'requester': row['requester'],
            'request_time': str(row['request_time']) if row['request_time'] else ''
        })

    return jsonify({'success': True, 'duplicate': True, 'requests': result})
