from flask import Blueprint, render_template, session, redirect, url_for

from app.config import Config

pages = Blueprint('pages', __name__)


@pages.route('/')
def login_page():
    if session.get('user'):
        return redirect(url_for('pages.kanban_page'))
    return render_template('login.html')


@pages.route('/kanban')
def kanban_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('kanban.html')


@pages.route('/request/new')
def request_new_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] not in ('requester', 'admin'):
        return '权限不足', 403
    return render_template('request_form.html')


@pages.route('/request/<int:request_id>')
def request_detail_page(request_id):
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('request_detail.html', request_id=request_id)


@pages.route('/request/minpack/new')
def minpack_request_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] not in ('requester', 'admin'):
        return '权限不足', 403
    return render_template('minpack_request_form.html')


@pages.route('/return/new')
def return_request_page():
    """新建物料退料申请（生产发起，手机/PDA 兼容）"""
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return '权限不足', 403
    return render_template('return_new.html')


@pages.route('/pending')
def pending_list_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] not in ('warehouse', 'admin'):
        return '权限不足', 403
    return render_template('pending_list.html')


@pages.route('/history')
def history_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('history.html')


@pages.route('/approve/<token>')
def approve_page(token):
    return render_template('approval_email.html', token=token)


@pages.route('/admin/mappings')
def admin_mappings_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] != 'admin':
        return '权限不足', 403
    return render_template('admin_mappings.html')


@pages.route('/admin/printers')
def admin_printers_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] != 'admin':
        return '权限不足', 403
    return render_template('admin_printers.html', sites=Config.SITE_CONFIG)


@pages.route('/admin/csi-alert')
def admin_csi_alert_page():
    """CSI 失败记录定时邮件：收件人规则维护（仅 admin）。

    页面级权限同时在 app/page_registry.py 注册（page_key=admin_csi_alert），
    非 admin 直输 URL 由 before_request 统一拦成 403 页。
    """
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] != 'admin':
        from app.page_registry import ROLE_LABELS
        return render_template(
            '403.html',
            page_key='admin_csi_alert',
            page_name='失败邮件通知',
            role=user['role'],
            role_label=ROLE_LABELS.get(user['role'], user['role']),
        ), 403
    return render_template('admin_csi_alert.html', sites=Config.SITE_CONFIG)


@pages.route('/records')
def records_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    from app.models import STATUS_LABELS
    return render_template('records.html', status_labels=STATUS_LABELS)


@pages.route('/admin/roles')
def admin_roles_page():
    """角色权限管理：仅 admin（页面级矩阵，见 app/page_registry.py）"""
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    user = session['user']
    if user['role'] != 'admin':
        from app.page_registry import ROLE_LABELS
        return render_template(
            '403.html',
            page_key='',
            page_name='角色权限',
            role=user['role'],
            role_label=ROLE_LABELS.get(user['role'], user['role']),
        ), 403

    from app.page_registry import ROLES, EDITABLE_ROLES, ROLE_LABELS, pages_by_group
    from app.services.permission_service import get_matrix
    return render_template(
        'admin_roles.html',
        groups=pages_by_group(),
        roles=ROLES,
        editable_roles=EDITABLE_ROLES,
        role_labels=ROLE_LABELS,
        matrix=get_matrix(),
    )


@pages.route('/cutting/specs')
def cutting_specs_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('cutting_specs.html')


@pages.route('/csi-interface')
def csi_interface_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('csi_interface.html')
