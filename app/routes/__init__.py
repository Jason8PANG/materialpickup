from flask import Blueprint, render_template, session, redirect, url_for

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


@pages.route('/records')
def records_page():
    if not session.get('user'):
        return redirect(url_for('pages.login_page'))
    return render_template('records.html')
