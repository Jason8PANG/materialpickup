/* ========== 通用 API 工具 ========== */
const BASE = '';

function apiGet(url, onSuccess, onError) {
    fetch(BASE + url)
        .then(r => r.json())
        .then(resp => {
            if (resp.success) onSuccess(resp);
            else (onError || showError)(resp);
        })
        .catch(err => (onError || showError)({message: err.message}));
}

function apiPost(url, data, onSuccess, onError) {
    fetch(BASE + url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
        .then(r => r.json())
        .then(resp => {
            if (resp.success) onSuccess(resp);
            else (onError || showError)(resp);
        })
        .catch(err => (onError || showError)({message: err.message}));
}

function apiPut(url, data, onSuccess, onError) {
    fetch(BASE + url, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
        .then(r => r.json())
        .then(resp => {
            if (resp.success) onSuccess(resp);
            else (onError || showError)(resp);
        })
        .catch(err => (onError || showError)({message: err.message}));
}

function apiDelete(url, onSuccess, onError) {
    fetch(BASE + url, {method: 'DELETE'})
        .then(r => r.json())
        .then(resp => {
            if (resp.success) onSuccess(resp);
            else (onError || showError)(resp);
        })
        .catch(err => (onError || showError)({message: err.message}));
}

/* ========== Toast 提示 ========== */
function showToast(type, message) {
    const toastEl = document.getElementById('toastMsg');
    if (!toastEl) {
        alert(message);
        return;
    }
    const titleEl = document.getElementById('toastTitle');
    const bodyEl = document.getElementById('toastBody');
    if (type === 'success') {
        titleEl.innerHTML = '<i class="fas fa-check-circle text-success me-1"></i>成功';
    } else if (type === 'error') {
        titleEl.innerHTML = '<i class="fas fa-times-circle text-danger me-1"></i>错误';
    } else {
        titleEl.innerHTML = '<i class="fas fa-info-circle text-primary me-1"></i>提示';
    }
    bodyEl.textContent = message;
    const toast = new bootstrap.Toast(toastEl, {delay: 3000});
    toast.show();
}

function showError(resp) {
    showToast('error', resp.message || '操作失败');
}

/* ========== 显示加载 ========== */
function showLoading() {
    const el = document.getElementById('loadingOverlay');
    if (el) el.classList.remove('d-none');
}

function hideLoading() {
    const el = document.getElementById('loadingOverlay');
    if (el) el.classList.add('d-none');
}

/* ========== 登录 ========== */
function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value.trim();
    const btn = document.getElementById('loginBtn');
    const errorEl = document.getElementById('loginError');

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>登录中...';
    errorEl.classList.add('d-none');

    apiPost('/api/auth/login', {username, password}, function (resp) {
        window.location.href = '/kanban';
    }, function (resp) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-sign-in-alt me-2"></i>' + __('login.btn');
        errorEl.textContent = resp.message || __('login.error');
        errorEl.classList.remove('d-none');
    });

    return false;
}

/* ========== 登出 ========== */
function logout() {
    apiPost('/api/auth/logout', {}, function () {
        window.location.href = '/';
    });
}

/* ========== Admin 站点切换 ========== */
function switchSite(site) {
    apiPost('/api/auth/switch-site', {siteref: site}, function (resp) {
        showToast('success', '已切换到: ' + resp.siteref_name);
        // 刷新页面
        setTimeout(function() { location.reload(); }, 500);
    }, function (err) {
        showToast('error', err.message || '切换失败');
    });
}

/* ========== 看板 ========== */
function loadKanban() {
    showLoading();
    apiGet('/api/kanban/cards', function (resp) {
        renderKanban(resp);
        hideLoading();
    }, function (err) {
        hideLoading();
        showError(err);
        if (err.message === '未登录') window.location.href = '/';
    });
}

function refreshKanban() {
    loadKanban();
}

function renderKanban(data) {
    const board = document.getElementById('kanbanBoard');
    const filterSelect = document.getElementById('mobileStatusFilter');
    if (!board) return;

    board.innerHTML = '';
    if (filterSelect) {
        filterSelect.innerHTML = '<option value="all">全部状态</option>';
    }

    let totalCount = 0;

    data.status_order.forEach(function (statusKey) {
        const group = data.groups[statusKey];
        if (!group) return;
        totalCount += group.count;

        const col = document.createElement('div');
        col.className = 'kanban-column';
        col.dataset.status = statusKey;
        col.id = 'col-' + statusKey;

        col.innerHTML = `
            <div class="kanban-column-header">
                <span>${__('status.' + statusKey) || group.title}</span>
                <span class="count">${group.count}</span>
            </div>
            <div class="kanban-cards">
                ${group.cards.length === 0 ? '<div class="text-center text-muted py-4"><small>' + __('kanban.no_data') + '</small></div>' : ''}
            </div>
        `;

        const cardsContainer = col.querySelector('.kanban-cards');

        group.cards.forEach(function (card) {
            const cardEl = document.createElement('div');
            cardEl.className = 'kanban-card';
            cardEl.onclick = function () {
                window.location.href = '/request/' + card.id;
            };

            let actionHtml = '';
            card.actions.forEach(function (action) {
                if (action === 'approve') {
                    actionHtml += `<button class="btn btn-success btn-sm" onclick="event.stopPropagation();doAction(${card.id},'approve')">审批通过</button>`;
                } else if (action === 'reject') {
                    actionHtml += `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation();doAction(${card.id},'reject')">驳回</button>`;
                } else if (action === 'start_prep') {
                    actionHtml += `<button class="btn btn-warning btn-sm" onclick="event.stopPropagation();doAction(${card.id},'start_prep')">开始备料</button>`;
                } else if (action === 'complete_prep') {
                    actionHtml += `<button class="btn btn-success btn-sm" onclick="event.stopPropagation();doAction(${card.id},'complete_prep')">完成备料</button>`;
                } else if (action === 'short') {
                    actionHtml += `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation();doAction(${card.id},'short')">缺料</button>`;
                } else if (action === 'sign') {
                    actionHtml += `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();doAction(${card.id},'sign')">签字取料</button>`;
                } else if (action === 'assign_worker') {
                    actionHtml += `<button class="btn btn-outline-secondary btn-sm" onclick="event.stopPropagation();showAssignWorker(${card.id})">指定备料员</button>`;
                }
            });

            // 卡片显示: 所有工单号
            const itemCount = card.item_count || 0;
            const jobOrders = card.job_orders || card.primary_job_order || '-';
            const primaryPartNumber = card.primary_part_number || card.part_number || '';

            // 如果有多个工单号，分行显示
            var jobDisplay = '';
            if (card.job_orders) {
                var orders = card.job_orders.split(', ');
                orders.forEach(function (jo, i) {
                    jobDisplay += '<div class="card-job-order">' + escapeHtml(jo) + '</div>';
                });
            } else {
                jobDisplay = '<div class="card-job-order">' + escapeHtml(jobOrders) + '</div>';
            }

            cardEl.innerHTML = `
                <div class="card-header-row">
                    <div class="card-job-list">${card.is_urgent ? '<span class="badge bg-danger me-1"><i class="fas fa-exclamation-triangle"></i> 急料</span>' : ''}${jobDisplay}</div>
                    <span class="card-item-badge badge bg-secondary">${itemCount} ${__('kanban.item_count')}</span>
                </div>
                <div class="card-part-number">${escapeHtml(primaryPartNumber)}${itemCount > 1 ? ' <span class="text-muted">' + __('kanban.etc') + itemCount + __('kanban.items') + '</span>' : ''}</div>
                <div class="card-qty">${__('kanban.total_amt')}: ${card.total_amount ? parseFloat(card.total_amount).toFixed(2) : '0.00'} | ${__('kanban.total_qty')}: ${card.total_quantity || card.quantity || '-'}</div>
                <div class="card-time text-muted small">${card.request_time || ''}</div>
                <div class="card-footer">${actionHtml}</div>
            `;

            cardsContainer.appendChild(cardEl);
        });

        board.appendChild(col);

        if (filterSelect) {
            const opt = document.createElement('option');
            opt.value = statusKey;
            opt.textContent = group.title + ' (' + group.count + ')';
            filterSelect.appendChild(opt);
        }
    });

    const totalEl = document.getElementById('totalCount');
    if (totalEl) totalEl.textContent = __('kanban.total') + ' ' + totalCount + ' ' + __('kanban.items');

    if (window.innerWidth < 768) {
        const firstCol = board.querySelector('.kanban-column');
        if (firstCol) firstCol.classList.add('active');
    }
}

function filterMobileKanban(status) {
    const cols = document.querySelectorAll('.kanban-column');
    cols.forEach(function (col) {
        if (status === 'all' || col.dataset.status === status) {
            col.classList.add('active');
        } else {
            col.classList.remove('active');
        }
    });
}

/* ========== 操作处理 ========== */
function doAction(requestId, action) {
    currentRequestId = requestId;
    currentAction = action;

    const actionNames = {
        'approve': '审批通过',
        'reject': '驳回',
        'start_prep': '开始备料',
        'complete_prep': '完成备料',
        'short': '缺料登记',
        'sign': '签字取料'
    };

    document.getElementById('actionModalTitle').textContent = actionNames[action] || action;
    document.getElementById('actionModalText').textContent = '确认' + (actionNames[action] || action) + '此单据吗？';
    document.getElementById('actionModalExtra').classList.add('d-none');

    if (action === 'reject') {
        document.getElementById('actionModalExtra').classList.remove('d-none');
        document.getElementById('extraLabel').textContent = '驳回意见（必填）';
        document.getElementById('extraInput').value = '';
        document.getElementById('extraInput').placeholder = '请填写驳回原因';
    } else if (action === 'short') {
        document.getElementById('actionModalExtra').classList.remove('d-none');
        document.getElementById('extraLabel').textContent = '缺料原因（必填）';
        document.getElementById('extraInput').value = '';
        document.getElementById('extraInput').placeholder = '请填写缺料原因';
    }

    document.getElementById('actionConfirmBtn').onclick = confirmAction;
    actionModal.show();
}

function confirmAction() {
    const extraInput = document.getElementById('extraInput');
    const comment = extraInput ? extraInput.value.trim() : '';

    if ((currentAction === 'reject' || currentAction === 'short') && !comment) {
        showToast('error', '请填写必要信息');
        return;
    }

    actionModal.hide();

    const urlMap = {
        'approve': '/api/requests/' + currentRequestId + '/approve',
        'reject': '/api/requests/' + currentRequestId + '/reject',
        'cancel': '/api/requests/' + currentRequestId + '/cancel',
        'start_prep': '/api/requests/' + currentRequestId + '/start-prep',
        'complete_prep': '/api/requests/' + currentRequestId + '/complete-prep',
        'short': '/api/requests/' + currentRequestId + '/short',
        'sign': '/api/requests/' + currentRequestId + '/sign'
    };

    const dataMap = {
        'approve': {comment: comment},
        'reject': {comment: comment},
        'short': {short_reason: comment}
    };

    apiPost(urlMap[currentAction], dataMap[currentAction] || {}, function (resp) {
        showToast('success', resp.message || '操作成功');
        loadKanban();
    }, function (err) {
        showToast('error', err.message || '操作失败');
    });
}

/* ========== 指定备料员 ========== */
function showAssignWorker(requestId) {
    currentRequestId = requestId;
    document.getElementById('workerInput').value = '';
    const modal = new bootstrap.Modal(document.getElementById('assignWorkerModal'));
    document.getElementById('assignWorkerConfirmBtn').onclick = function () {
        const worker = document.getElementById('workerInput').value.trim();
        if (!worker) {
            showToast('error', '请输入备料员');
            return;
        }
        modal.hide();
        apiPut('/api/requests/' + requestId + '/assign-worker',
            {warehouse_operator: worker},
            function (resp) {
                showToast('success', resp.message || '已指定');
                loadKanban();
            },
            function (err) {
                showToast('error', err.message || '操作失败');
            }
        );
    };
    modal.show();
}

/* ========== 申请单详情 ========== */
function renderDetail(req, logs) {
    const setText = function (id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val || '-';
    };

    setText('detailId', req.id);
    setText('detailTotalAmount', req.total_amount != null ? parseFloat(req.total_amount).toFixed(2) : '-');
    setText('detailRequester', req.requester);
    setText('detailRequestTime', req.request_time || '-');
    setText('detailSupervisor', req.supervisor || '-');
    setText('detailApproveTime', req.approve_time || '-');
    setText('detailApproveComment', req.approve_comment || '-');
    setText('detailWarehouseOperator', req.warehouse_operator || '-');
    setText('detailShortReason', req.short_reason || '-');
    setText('detailShortTime', req.short_time || '-');
    setText('detailRemark', req.remark || '-');

    const signInfo = req.signer ? req.signer + ' / ' + (req.sign_time || '-') : '-';
    setText('detailSignInfo', signInfo);

    // 显示签字图片
    var sigArea = document.getElementById('signatureImageArea');
    var sigImg = document.getElementById('detailSignatureImage');
    if (sigArea && sigImg && req.signature_data) {
        sigImg.src = req.signature_data;
        sigArea.style.display = '';
    } else if (sigArea) {
        sigArea.style.display = 'none';
    }

    // 站点标签
    const siteEl = document.getElementById('detailSite');
    if (siteEl) {
        siteEl.textContent = req.siteref || '-';
    }

    // 状态标签
    const statusEl = document.getElementById('detailStatus');
    const statusLabel = req.status_label || req.status;
    const statusColors = {
        'pending_approval': 'bg-primary', 'rejected': 'bg-secondary',
        'pending_prep': 'bg-warning text-dark', 'prepping': 'bg-success',
        'short': 'bg-danger', 'ready_pickup': 'bg-secondary',
        'completed': 'bg-success'
    };
    statusEl.className = 'badge fs-6 px-3 py-2 ' + (statusColors[req.status] || 'bg-secondary');
    statusEl.textContent = statusLabel;

    // 如果状态为缺料，启用缺料原因编辑
    if (req.status === 'short' && typeof enableEditShortReason === 'function') {
        enableEditShortReason();
    }
}

function renderActions(req) {
    const area = document.getElementById('actionArea');
    const buttons = document.getElementById('actionButtons');
    if (!area || !buttons) return;

    const status = req.status;
    const role = userRole || '';

    let actions = [];

    // 取消申请：发起人可取消待审批的申请
    if ((role === 'requester' || role === 'admin') && status === 'pending_approval') {
        actions.push({label: __('action.cancel'), class: 'btn-outline-danger', action: 'cancel'});
    }

    if (role === 'supervisor' || role === 'admin') {
        if (status === 'pending_approval') {
            actions.push({label: __('action.approve'), class: 'btn-success', action: 'approve'});
            actions.push({label: __('action.reject'), class: 'btn-danger', action: 'reject'});
        }
    }

    if (role === 'warehouse' || role === 'admin') {
        if (status === 'pending_prep') {
            actions.push({label: __('action.assign_worker'), class: 'btn-outline-secondary', action: 'assign_worker'});
            actions.push({label: __('action.start_prep'), class: 'btn-warning', action: 'start_prep'});
        }
        if (status === 'prepping') {
            actions.push({label: __('action.complete_prep'), class: 'btn-success', action: 'complete_prep'});
            actions.push({label: __('action.short'), class: 'btn-danger', action: 'short'});
        }
        if (status === 'short') {
            actions.push({label: __('action.restore_from_short'), class: 'btn-warning', action: 'restore_from_short'});
        }
    }

    if ((role === 'requester' || role === 'warehouse' || role === 'admin') &&
        (status === 'ready_pickup' || status === 'short')) {
        actions.push({label: __('action.sign'), class: 'btn-primary', action: 'sign'});
    }

    if (actions.length === 0) {
        area.classList.add('d-none');
        return;
    }

    area.classList.remove('d-none');
    buttons.innerHTML = '';
    actions.forEach(function (act) {
        const btn = document.createElement('button');
        btn.className = 'btn ' + act.class + ' me-2';
        btn.textContent = act.label;
        if (act.action === 'assign_worker') {
            btn.onclick = function () {
                if (typeof showAssignWorker === 'function') {
                    showAssignWorker(req.id);
                } else {
                    doAction(req.id, 'assign_worker');
                }
            };
        } else {
            btn.onclick = function () {
                doAction(req.id, act.action);
            };
        }
        buttons.appendChild(btn);
    });
}

function renderLogs(logs) {
    const container = document.getElementById('logTimeline');
    if (!container) return;

    if (!logs || logs.length === 0) {
        container.innerHTML = '<div class="text-muted text-center py-3">暂无日志</div>';
        return;
    }

    const actionLabels = {
        'SUBMIT': '提交申请',
        'APPROVE': '审批通过',
        'REJECT': '驳回',
        'START_PREP': '开始备料',
        'COMPLETE_PREP': '完成备料',
        'SHORT': '缺料登记',
        'SIGN': '签字确认',
        'ASSIGN_WORKER': '指定备料员'
    };

    container.innerHTML = '';
    logs.forEach(function (log) {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.innerHTML = `
            <div class="timeline-time">
                <i class="far fa-clock me-1"></i>${log.created_at || ''}
            </div>
            <div class="timeline-content">
                <strong>${escapeHtml(log.operator)}</strong>
                ${actionLabels[log.action] || log.action}
                ${log.detail ? ' - ' + escapeHtml(log.detail) : ''}
            </div>
        `;
        container.appendChild(item);
    });
}

/* ========== 未完成单据列表 ========== */
let pendingPage = 1;

function loadPending() {
    showLoading();
    const jobOrder = document.getElementById('searchJobOrder').value.trim();
    const status = document.getElementById('statusFilter').value;

    let url = '/api/requests/pending?page=' + pendingPage + '&size=20';
    if (jobOrder) url += '&job_order=' + encodeURIComponent(jobOrder);
    if (status) url += '&status=' + status;

    apiGet(url, function (resp) {
        renderPendingTable(resp);
        hideLoading();
    }, function (err) {
        hideLoading();
        showError(err);
    });
}

function renderPendingTable(resp) {
    const tbody = document.getElementById('pendingTableBody');
    const pagination = document.getElementById('pendingPagination');
    userRole = userRole || '';

    if (resp.data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">暂无数据</td></tr>';
        pagination.classList.add('d-none');
        return;
    }

    tbody.innerHTML = '';
    resp.data.forEach(function (row) {
        const tr = document.createElement('tr');
        const statusColors = {
            'pending_approval': 'bg-primary',
            'pending_prep': 'bg-warning text-dark',
            'prepping': 'bg-success',
            'short': 'bg-danger',
            'ready_pickup': 'bg-secondary'
        };
        const itemCount = row.item_count || 0;
        const primaryJobOrder = row.primary_job_order || '-';

        let html = `<td>${row.id}</td>`;
        if (userRole === 'admin') {
            html += `<td><span class="badge bg-info">${escapeHtml(row.siteref || '-')}</span></td>`;
        }
        html += `
            <td><strong>${escapeHtml(primaryJobOrder)}</strong></td>
            <td><span class="badge bg-secondary">${itemCount} 项</span></td>
            <td>${escapeHtml(row.requester)}</td>
            <td><span class="badge ${statusColors[row.status] || 'bg-secondary'}">${row.status_label}</span></td>
            <td>${row.request_time || '-'}</td>
            <td><a href="/request/${row.id}" class="btn btn-sm btn-outline-primary">详情</a></td>
        `;
        tr.innerHTML = html;
        tbody.appendChild(tr);
    });

    renderPagination(pagination, resp.page, resp.total_pages, function (page) {
        pendingPage = page;
        loadPending();
    });
}

/* ========== 历史查询 ========== */
let historyPage = 1;

function loadHistory() {
    showLoading();
    const jobOrder = document.getElementById('searchJobOrder').value.trim();
    const status = document.getElementById('statusFilter').value;
    const dateFrom = document.getElementById('dateFrom').value;
    const dateTo = document.getElementById('dateTo').value;

    let url = '/api/requests/history?page=' + historyPage + '&size=20';
    if (jobOrder) url += '&job_order=' + encodeURIComponent(jobOrder);
    if (status) url += '&status=' + status;
    if (dateFrom) url += '&date_from=' + dateFrom;
    if (dateTo) url += '&date_to=' + dateTo;

    apiGet(url, function (resp) {
        renderHistoryTable(resp);
        hideLoading();
    }, function (err) {
        hideLoading();
        showError(err);
    });
}

function renderHistoryTable(resp) {
    const tbody = document.getElementById('historyTableBody');
    const pagination = document.getElementById('historyPagination');
    userRole = userRole || '';

    if (resp.data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无数据</td></tr>';
        pagination.classList.add('d-none');
        return;
    }

    tbody.innerHTML = '';
    resp.data.forEach(function (row) {
        const tr = document.createElement('tr');
        const statusColors = {
            'completed': 'bg-success',
            'rejected': 'bg-secondary'
        };
        const itemCount = row.item_count || 0;
        const primaryJobOrder = row.primary_job_order || '-';

        let html = `<td>${row.id}</td>`;
        if (userRole === 'admin') {
            html += `<td><span class="badge bg-info">${escapeHtml(row.siteref || '-')}</span></td>`;
        }
        html += `
            <td><strong>${escapeHtml(primaryJobOrder)}</strong></td>
            <td><span class="badge bg-secondary">${itemCount} 项</span></td>
            <td>${escapeHtml(row.requester)}</td>
            <td><span class="badge ${statusColors[row.status] || 'bg-secondary'}">${row.status_label}</span></td>
            <td>${escapeHtml(row.supervisor || '-')}</td>
            <td>${row.request_time || '-'}</td>
            <td><a href="/request/${row.id}" class="btn btn-sm btn-outline-primary">详情</a></td>
        `;
        tr.innerHTML = html;
        tbody.appendChild(tr);
    });

    renderPagination(pagination, resp.page, resp.total_pages, function (page) {
        historyPage = page;
        loadHistory();
    });
}

/* ========== 分页通用 ========== */
function renderPagination(container, currentPage, totalPages, onPageClick) {
    container.classList.remove('d-none');
    const ul = container.querySelector('ul');
    ul.innerHTML = '';

    const prevLi = document.createElement('li');
    prevLi.className = 'page-item' + (currentPage <= 1 ? ' disabled' : '');
    prevLi.innerHTML = '<a class="page-link" href="#">上一页</a>';
    prevLi.onclick = function (e) {
        e.preventDefault();
        if (currentPage > 1) onPageClick(currentPage - 1);
    };
    ul.appendChild(prevLi);

    const start = Math.max(1, currentPage - 2);
    const end = Math.min(totalPages, currentPage + 2);

    for (let i = start; i <= end; i++) {
        const li = document.createElement('li');
        li.className = 'page-item' + (i === currentPage ? ' active' : '');
        li.innerHTML = '<a class="page-link" href="#">' + i + '</a>';
        li.onclick = (function (page) {
            return function (e) {
                e.preventDefault();
                onPageClick(page);
            };
        })(i);
        ul.appendChild(li);
    }

    const nextLi = document.createElement('li');
    nextLi.className = 'page-item' + (currentPage >= totalPages ? ' disabled' : '');
    nextLi.innerHTML = '<a class="page-link" href="#">下一页</a>';
    nextLi.onclick = function (e) {
        e.preventDefault();
        if (currentPage < totalPages) onPageClick(currentPage + 1);
    };
    ul.appendChild(nextLi);
}

/* ========== 账号管理 ========== */
let mappingModal = null;

function loadMappings() {
    mappingModal = new bootstrap.Modal(document.getElementById('mappingModal'));
    showLoading();

    let url = '/api/role-mappings';
    const filterSite = document.getElementById('filterSite');
    const filterRole = document.getElementById('filterRole');
    const params = [];
    if (filterSite && filterSite.value) params.push('siteref=' + filterSite.value);
    if (filterRole && filterRole.value) params.push('role=' + filterRole.value);
    if (params.length) url += '?' + params.join('&');

    apiGet(url, function (resp) {
        renderMappingTable(resp.data);
        hideLoading();
    }, function (err) {
        hideLoading();
        showError(err);
    });
}

function renderMappingTable(data) {
    const tbody = document.getElementById('mappingTableBody');
    const roleLabels = {
        'admin': '管理员',
        'requester': '领料员',
        'supervisor': '主管',
        'warehouse': '仓库'
    };
    const siteLabels = {
        '310': '苏州工厂',
        '410': '槟城工厂'
    };

    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    data.forEach(function (row) {
        const tr = document.createElement('tr');
        const siterefDisplay = row.siteref ? (siteLabels[row.siteref] || row.siteref) : '<span class="text-muted">跨站</span>';
        tr.innerHTML = `
            <td>${row.id}</td>
            <td>${escapeHtml(row.domain_account)}</td>
            <td>${escapeHtml(row.display_name || '-')}</td>
            <td><span class="badge bg-primary">${roleLabels[row.role] || row.role}</span></td>
            <td>${siterefDisplay}</td>
            <td>${escapeHtml(row.email || '-')}</td>
            <td>${row.is_active ? '<span class="badge bg-success">启用</span>' : '<span class="badge bg-secondary">禁用</span>'}</td>
            <td>${escapeHtml(row.remark || '-')}</td>
            <td>
                <button class="btn btn-sm btn-outline-primary me-1" onclick="editMapping(${row.id})">编辑</button>
                <button class="btn btn-sm btn-outline-danger" onclick="deleteMapping(${row.id})">删除</button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function toggleSiteField() {
    const role = document.getElementById('fRole').value;
    const siteSelect = document.getElementById('fSiteref');
    const siteRequired = document.getElementById('siteRequired');
    const siteHint = document.getElementById('siteAdminHint');

    if (role === 'admin') {
        siteRequired.classList.add('d-none');
        if (siteHint) siteHint.classList.remove('d-none');
    } else {
        siteRequired.classList.remove('d-none');
        if (siteHint) siteHint.classList.add('d-none');
    }
}

function showAddModal() {
    document.getElementById('mappingModalTitle').textContent = '新增账号';
    document.getElementById('editId').value = '';
    document.getElementById('fDomainAccount').value = '';
    document.getElementById('fDomainAccount').readOnly = false;
    document.getElementById('fDisplayName').value = '';
    document.getElementById('fRole').value = 'requester';
    document.getElementById('fSiteref').value = '';
    document.getElementById('fEmail').value = '';
    document.getElementById('fIsActive').value = '1';
    document.getElementById('fRemark').value = '';
    document.getElementById('saveMappingBtn').onclick = createMapping;
    toggleSiteField();
    mappingModal.show();
}

function createMapping() {
    const data = {
        domain_account: document.getElementById('fDomainAccount').value.trim(),
        display_name: document.getElementById('fDisplayName').value.trim(),
        role: document.getElementById('fRole').value,
        siteref: document.getElementById('fSiteref').value || null,
        email: document.getElementById('fEmail').value.trim(),
        is_active: parseInt(document.getElementById('fIsActive').value),
        remark: document.getElementById('fRemark').value.trim()
    };

    if (!data.domain_account) {
        showToast('error', '域账号不能为空');
        return;
    }

    if (data.role !== 'admin' && !data.siteref) {
        showToast('error', '非管理员角色必须选择所属站点');
        return;
    }

    mappingModal.hide();
    apiPost('/api/role-mappings', data, function (resp) {
        showToast('success', '创建成功');
        loadMappings();
    }, function (err) {
        showToast('error', err.message || '创建失败');
    });
}

function editMapping(id) {
    document.getElementById('mappingModalTitle').textContent = '编辑账号';
    document.getElementById('editId').value = id;

    const rows = document.querySelectorAll('#mappingTableBody tr');
    for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (cells.length >= 9 && cells[0].textContent == id) {
            document.getElementById('fDomainAccount').value = cells[1].textContent.trim();
            document.getElementById('fDomainAccount').readOnly = true;
            document.getElementById('fDisplayName').value = cells[2].textContent.trim() === '-' ? '' : cells[2].textContent.trim();

            const roleText = cells[3].textContent.trim();
            const roleMap = {'管理员': 'admin', '领料员': 'requester', '主管': 'supervisor', '仓库': 'warehouse'};
            document.getElementById('fRole').value = roleMap[roleText] || 'requester';

            // 站点字段
            const siteText = cells[4].textContent.trim();
            const siteMap = {'苏州工厂': '310', '槟城工厂': '410'};
            document.getElementById('fSiteref').value = siteMap[siteText] || '';
            toggleSiteField();

            document.getElementById('fEmail').value = cells[5].textContent.trim() === '-' ? '' : cells[5].textContent.trim();
            document.getElementById('fIsActive').value = cells[6].textContent.includes('启用') ? '1' : '0';
            document.getElementById('fRemark').value = cells[7].textContent.trim() === '-' ? '' : cells[7].textContent.trim();
            break;
        }
    }

    document.getElementById('saveMappingBtn').onclick = function () {
        updateMapping(id);
    };
    mappingModal.show();
}

function updateMapping(id) {
    const data = {
        display_name: document.getElementById('fDisplayName').value.trim(),
        role: document.getElementById('fRole').value,
        siteref: document.getElementById('fSiteref').value || null,
        email: document.getElementById('fEmail').value.trim(),
        is_active: parseInt(document.getElementById('fIsActive').value),
        remark: document.getElementById('fRemark').value.trim()
    };

    mappingModal.hide();
    apiPut('/api/role-mappings/' + id, data, function (resp) {
        showToast('success', '更新成功');
        loadMappings();
    }, function (err) {
        showToast('error', err.message || '更新失败');
    });
}

function deleteMapping(id) {
    if (!confirm('确认删除此账号映射吗？')) return;
    apiDelete('/api/role-mappings/' + id, function (resp) {
        showToast('success', '删除成功');
        loadMappings();
    }, function (err) {
        showToast('error', err.message || '删除失败');
    });
}

/* ========== 工具 ========== */
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
