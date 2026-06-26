from flask import Blueprint, jsonify, session, request, render_template
from app.models import get_db_connection, get_site_filter, STATUS_LABELS

kanban_bp = Blueprint('kanban', __name__)

STATUS_ORDER = [
    'pending_prep',
    'prepping',
    'short',
    'ready_pickup'
]

STATUS_ACTIONS = {
    'pending_approval': [],
    'pending_prep': [],
    'prepping': ['short'],
    'short': [],
    'ready_pickup': ['sign']
}

STATUS_ACTIONS_WAREHOUSE = {
    'pending_approval': [],
    'pending_prep': ['assign_worker', 'start_prep'],
    'prepping': ['complete_prep', 'short'],
    'short': [],
    'ready_pickup': []
}


@kanban_bp.route('/api/kanban/cards', methods=['GET'])
def get_kanban_cards():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    site_filter, site_params = get_site_filter(user)

    with get_db_connection() as db:
        cursor = db.cursor()

        base_sql = """SELECT r.*, 
            (SELECT COUNT(*) FROM kr_request_item WHERE request_id = r.id) as item_count,
            (SELECT ROUND(SUM(total_amount), 2) FROM kr_request_item WHERE request_id = r.id) as total_amount,
            (SELECT GROUP_CONCAT(DISTINCT job_order ORDER BY id SEPARATOR ', ') FROM kr_request_item WHERE request_id = r.id) as job_orders,
            (SELECT job_order FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_job_order,
            (SELECT part_number FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_part_number
            FROM kr_material_request r 
            WHERE r.status IN ('pending_prep','prepping','short','ready_pickup') 
            AND r.is_deleted = 0"""

        if site_filter:
            base_sql += f" AND r.{site_filter}"
            cursor.execute(base_sql + " ORDER BY r.is_urgent DESC, r.request_time ASC", site_params)
        else:
            cursor.execute(base_sql + " ORDER BY r.is_urgent DESC, r.request_time ASC")

        rows = cursor.fetchall()
        cursor.close()

    # 按状态分组
    groups = {}
    for s in STATUS_ORDER:
        column = []
        for row in rows:
            if row['status'] == s:
                card = dict(row)
                card['status_label'] = STATUS_LABELS.get(s, s)
                # 决定按钮
                if user['role'] == 'warehouse':
                    card['actions'] = STATUS_ACTIONS_WAREHOUSE.get(s, [])
                elif user['role'] == 'requester':
                    card['actions'] = STATUS_ACTIONS.get(s, [])
                elif user['role'] == 'admin':
                    card['actions'] = ['assign_worker', 'start_prep', 'complete_prep', 'short', 'sign']
                else:
                    card['actions'] = []
                column.append(card)
        groups[s] = {
            'title': STATUS_LABELS.get(s, s),
            'cards': column,
            'count': len(column)
        }

    return jsonify({
        'success': True,
        'groups': groups,
        'status_order': STATUS_ORDER,
        'status_labels': STATUS_LABELS
    })


# ====== 公共看板（无需登录） ======

@kanban_bp.route('/public/kanban')
def public_kanban_page():
    """公共看板页面，无需登录"""
    return render_template('public_kanban.html')


@kanban_bp.route('/api/public/kanban/cards')
def public_kanban_cards():
    """公共看板 API，无需登录，通过 siteref 参数过滤"""
    siteref = request.args.get('siteref', '310')

    base_sql = """SELECT r.*, 
        (SELECT COUNT(*) FROM kr_request_item WHERE request_id = r.id) as item_count,
        (SELECT ROUND(SUM(total_amount), 2) FROM kr_request_item WHERE request_id = r.id) as total_amount,
        (SELECT GROUP_CONCAT(job_order ORDER BY id SEPARATOR ', ') FROM kr_request_item WHERE request_id = r.id) as job_orders,
        (SELECT job_order FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_job_order,
        (SELECT part_number FROM kr_request_item WHERE request_id = r.id ORDER BY id LIMIT 1) as primary_part_number
        FROM kr_material_request r 
        WHERE r.status IN ('pending_prep','prepping','short','ready_pickup') 
        AND r.is_deleted = 0 AND r.siteref = %s
        ORDER BY r.is_urgent DESC, r.request_time ASC"""

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(base_sql, (siteref,))
        rows = cursor.fetchall()
        cursor.close()

    groups = {}
    for s in STATUS_ORDER:
        column = []
        for row in rows:
            if row['status'] == s:
                card = dict(row)
                card['status_label'] = STATUS_LABELS.get(s, s)
                column.append(card)
        groups[s] = {
            'title': STATUS_LABELS.get(s, s),
            'cards': column,
            'count': len(column)
        }

    return jsonify({
        'success': True,
        'groups': groups,
        'status_order': STATUS_ORDER,
        'status_labels': STATUS_LABELS
    })
