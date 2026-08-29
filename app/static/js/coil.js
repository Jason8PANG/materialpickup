/* ==========================================================
 * 线卷全库存管理 - 前端交互
 * 卷标信息录入弹窗 / 出库登记弹窗 / 标签预览
 * 依赖：main.js 中的 apiGet/apiPost/showToast/escapeHtml
 * ========================================================== */

let coilState = {
    requestId: null,
    request: null,
    requestItems: [],   // 本单去重后的物料列表
    unitMap: {},        // part_number -> unit（CSI 只读回填）
    rows: [],           // 录入行 {part_number, coil_id, length}
    existingCoils: [],  // 已录入卷标
    previewCoil: null,  // 当前预览的卷标
    itemId: null,       // 按行维护：当前打开的申请单行 id
    defaultPart: ''     // 按行维护：该行默认物料
};

let outboundState = {
    requestId: null,
    coils: []           // in_stock 卷标
};

// 单位换算系数（文档 2.3.1，与服务端 Config.UNIT_CONVERT_FACTOR 保持一致；
// 前端仅做实时预览，converted_length/converted_unit 最终以后端计算为准）
const UNIT_CONVERT_FACTOR = {M: 1000, FT: 304.8, CM: 10, IN: 25.4};

// 出库登记宽表字段 id（与后端 CONSUMPTION_EXTRA_FIELDS 键一致，用于汇总提交）
const OUTBOUND_EXTRA_FIELDS = [
    'job_part_number', 'shear_qty', 'shear_length', 'length_tolerance',
    'shear_equipment', 'actual_shear_equipment'
];

function convertPreview(length, unit) {
    // 前端实时预览：converted = out_length(mm) ÷ 系数；系数表外 / 空单位返回 '-'
    if (!(length > 0)) return '';
    const factor = UNIT_CONVERT_FACTOR[(unit || '').toUpperCase()];
    if (!factor) return '-';
    return (length / factor).toFixed(4);
}

function coilTotalMm(coil) {
    // 卷长换算为 mm：coil_length × 系数；单位未知返回 null（无法换算）
    const factor = UNIT_CONVERT_FACTOR[(coil.unit || '').toUpperCase()];
    if (!factor) return null;
    return Math.round(parseFloat(coil.coil_length) * factor * 100) / 100;
}

/* ================= 卷标信息弹窗 ================= */

function openCoilModal(requestId, itemId, partNumber) {
    coilState = {requestId: requestId, request: null, requestItems: [], unitMap: {},
                 rows: [], existingCoils: [], previewCoil: null, itemId: itemId || null,
                 defaultPart: partNumber || '', defaultLot: ''};

    document.getElementById('coilModalTitle').textContent = '#' + requestId;

    apiGet('/api/requests/' + requestId, function (resp) {
        coilState.request = resp.request;
        const items = resp.request.items || [];
        // 物料列表（保留所有申请单行，同一物料多行不合并；
        // item_id 为空录入时由后端自动分配到未覆盖行）
        coilState.requestItems = items.map(function (it) { return it.part_number; });
        // Lot 默认值：仅从「维护卷标图标所在行」带出（无该行则留空白，不 fallback）
        let defItem = null;
        if (coilState.itemId) { defItem = items.find(function (it) { return it.id === coilState.itemId; }); }
        coilState.defaultLot = (defItem && defItem.lot_no) || '';
        if (coilState.requestItems.length === 0) {
            showToast('error', '申请单没有物料明细，无法录入卷标');
            return;
        }
        apiGet('/api/requests/' + requestId + '/coil-units', function (resp2) {
            coilState.unitMap = resp2.data || {};
            // 备料参考：加载该申请单物料的「在库」卷标（建议优先使用）
            apiGet('/api/requests/' + requestId + '/in-stock-coils', function (ins) {
                renderInStockCoils(ins.data || {});
            }, function () { /* 提示加载失败不阻断 */ });
            // 若从某一行打开，自动添加一行且默认选中该行物料
            if (coilState.defaultPart) {
                addCoilRow(coilState.defaultPart);
            }
            apiGet('/api/requests/' + requestId + '/coils' + (coilState.itemId ? '?item_id=' + coilState.itemId : ''), function (resp3) {
                coilState.existingCoils = resp3.data || [];
                renderCoilRows();
                renderExistingCoils();
                const modal = new bootstrap.Modal(document.getElementById('coilModal'));
                modal.show();
            }, showError);
        }, showError);
    }, showError);
}

/* ---- 行操作 ---- */

function renderInStockCoils(data) {
    // 备料参考：退料回来的在库卷标优先推荐，可点击「使用」绑定到本单
    // （sign 确认取料后自动转在车间，再次发往车间）——表格方式显示
    const box = document.getElementById('inStockCoilsBox');
    if (!box) return;
    const parts = Object.keys(data).filter(function (p) { return data[p] && data[p].length > 0; });
    if (parts.length === 0) { box.classList.add('d-none'); return; }
    let html = '<div class="fw-bold mb-1"><i class="fas fa-info-circle me-1"></i>' + __('instock.title') + '</div>';
    html += '<div class="table-responsive"><table class="table table-sm table-bordered table-hover mb-0 align-middle">' +
            '<thead class="table-light"><tr>' +
            '<th>' + __('instock.part') + '</th><th>' + __('instock.coil_id') + '</th><th class="text-end">' + __('instock.remain') + '</th><th>' + __('instock.unit') + '</th><th class="text-center">' + __('instock.source') + '</th><th class="text-center" style="width:80px">' + __('instock.action') + '</th>' +
            '</tr></thead><tbody>';
    parts.forEach(function (p) {
        data[p].forEach(function (c, idx) {
            const source = c.is_return
                ? '<span class="badge bg-success">' + __('instock.returned') + '</span>'
                : '<span class="badge bg-secondary">' + __('instock.in_stock') + '</span>';
            html += '<tr>' +
                (idx === 0 ? '<td rowspan="' + data[p].length + '" class="fw-bold">' + escapeHtml(p) + '</td>' : '') +
                '<td>' + escapeHtml(c.coil_id) + '</td>' +
                '<td class="text-end">' + c.remain_length + '</td>' +
                '<td>' + escapeHtml(c.unit || '-') + '</td>' +
                '<td class="text-center">' + source + '</td>' +
                '<td class="text-center"><button class="btn btn-outline-primary btn-sm py-0 px-2" onclick="useStockCoil(\'' +
                escapeHtml(c.coil_id) + '\')"><i class="fas fa-check me-1"></i>' + __('instock.use') + '</button></td>' +
                '</tr>';
        });
    });
    html += '</tbody></table></div>';
    box.innerHTML = html;
    box.classList.remove('d-none');
}

function useStockCoil(coilId) {
    // 选用在库卷标：绑定到当前申请单 → 刷新已录入列表与在库提示
    if (!coilState.requestId) return;
    apiPost('/api/requests/' + coilState.requestId + '/coils/use-stock',
        {coil_id: coilId},
        function (resp) {
            showToast('success', resp.message || __('instock.use_ok'));
            refreshCoilLists();
        },
        function (err) { showToast('error', err.message || __('instock.use_fail')); });
}

function unuseStockCoil(coilId) {
    // 取消选用：从本单解除绑定，卷标回到可选清单
    if (!coilState.requestId) return;
    apiPost('/api/requests/' + coilState.requestId + '/coils/unuse-stock',
        {coil_id: coilId},
        function (resp) {
            showToast('success', resp.message || __('instock.unuse_ok'));
            refreshCoilLists();
        },
        function (err) { showToast('error', err.message || __('instock.unuse_fail')); });
}

function refreshCoilLists() {
    // 刷新已录入卷标 + 在库可选清单
    if (!coilState.requestId) return;
    apiGet('/api/requests/' + coilState.requestId + '/coils', function (resp3) {
        coilState.existingCoils = resp3.data || [];
        renderExistingCoils();
    }, function () {});
    apiGet('/api/requests/' + coilState.requestId + '/in-stock-coils', function (ins) {
        renderInStockCoils(ins.data || {});
    }, function () {});
}

function addCoilRow(part_number) {
    // 录入区仅保留一行（用户要求：只能添加一行，保存后转入已录入列表）
    if (coilState.rows.length >= 1) {
        return;
    }
    const firstPart = part_number || (coilState.requestItems[0] || '');
    const idx = coilState.rows.length;
    coilState.rows.push({part_number: firstPart, lot_no: coilState.defaultLot || '', coil_id: '', length: ''});
    renderCoilRows();
    updatePreview();
    // 自动生成卷标ID（全局唯一）
    genCoilIdForRow(idx);
}

function removeCoilRow(idx) {
    coilState.rows.splice(idx, 1);
    renderCoilRows();
    updatePreview();
}

function genCoilIdForRow(idx) {
    apiGet('/api/coils/next-id', function (resp) {
        coilState.rows[idx].coil_id = resp.data.coil_id;
        renderCoilRows();
        updatePreview();
    }, function (err) {
        showToast('error', err.message || '生成卷号失败');
    });
}

function genAllCoilIds() {
    const emptyIdx = [];
    coilState.rows.forEach(function (r, i) {
        if (!r.coil_id) emptyIdx.push(i);
    });
    if (emptyIdx.length === 0) {
        showToast('info', '所有录入行均已填写卷号');
        return;
    }
    genNextFor(emptyIdx, 0);
}

function genNextFor(idxs, k) {
    if (k >= idxs.length) {
        renderCoilRows();
        updatePreview();
        return;
    }
    apiGet('/api/coils/next-id', function (resp) {
        coilState.rows[idxs[k]].coil_id = resp.data.coil_id;
        genNextFor(idxs, k + 1);
    }, function (err) {
        showToast('error', err.message || '生成卷号失败');
    });
}

function isABPart(p) {
    return p && /^[ab]/i.test(p);
}

/* ---- 渲染 ---- */

function renderCoilRows() {
    const tbody = document.getElementById('coilRowsBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (coilState.rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无录入行，点击「添加一行」开始录入</td></tr>';
        return;
    }
    coilState.rows.forEach(function (row, idx) {
        const isAB = isABPart(row.part_number);
        const unit = coilState.unitMap[row.part_number] || '-';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>
                <input type="text" class="form-control form-control-sm coil-part-input" data-idx="${idx}"
                       value="${escapeHtml(row.part_number)}" readonly style="background:#f5f5f5">
            </td>
            <td>
                <input type="text" class="form-control form-control-sm coil-unit-input" data-idx="${idx}"
                       value="${escapeHtml(unit)}" readonly style="background:#f5f5f5">
            </td>
            <td>
                <input type="text" class="form-control form-control-sm coil-id-input" data-idx="${idx}"
                       value="${escapeHtml(row.coil_id)}" placeholder="${__('coils.coilid_placeholder')}" maxlength="9" readonly style="background:#f5f5f5">
            </td>
            <td style="width:20%">
                <input type="text" class="form-control form-control-sm coil-lot-input" data-idx="${idx}"
                       value="${escapeHtml(row.lot_no || '')}" placeholder="Lot" maxlength="64"
                       onblur="validateLotForRow(${idx})">
            </td>
            <td>
                <input type="number" class="form-control form-control-sm coil-length-input" data-idx="${idx}"
                       value="${row.length}" min="0" step="0.01" placeholder="${__('coils.length_placeholder')}${unit && unit !== '-' ? ' (' + escapeHtml(unit) + ')' : ''}"
                       onblur="validateLotForRow(${idx})">
            </td>
            <td class="text-center" style="width:90px">
                <button type="button" class="btn btn-sm btn-success" onclick="saveCoilRow(${idx})" title="保存该卷标"><i class="fas fa-save"></i></button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function validateLotForRow(idx) {
    // Lot + 长度验证：连接 Infor IDO SLLots（按 Item 过滤验证 Lot），
    // 长度（mm）与 DerQtyOnHand（×换算系数 → mm）对比。
    // 输入框失焦时触发；验证结果行内反馈（绿=通过/红=失败），Lot 为空跳过。
    const row = coilState.rows[idx];
    if (!row) return;
    const lotNo = (row.lot_no || '').trim();
    const partNumber = (row.part_number || '').trim();
    if (!lotNo) return;  // 未填 Lot 不验证

    const lotInput = document.querySelector('.coil-lot-input[data-idx="' + idx + '"]');
    const lenInput = document.querySelector('.coil-length-input[data-idx="' + idx + '"]');
    apiPost('/api/coils/validate-lot', {
        part_number: partNumber,
        lot_no: lotNo,
        length: row.length,
        unit: coilState.unitMap[partNumber] || ''
    }, function (resp) {
        // 通过
        if (lotInput) lotInput.classList.remove('is-invalid');
        if (lotInput) lotInput.classList.add('is-valid');
        if (lenInput) lenInput.classList.remove('is-invalid');
        if (lenInput) lenInput.classList.add('is-valid');
        showToast('success', resp.message || 'Lot 验证通过');
    }, function (err) {
        // 失败：Lot 不存在 或 长度超 DerQtyOnHand
        const msg = err.message || 'Lot 验证失败';
        if (lotInput) lotInput.classList.remove('is-valid');
        if (lotInput) lotInput.classList.add('is-invalid');
        if (lenInput) lenInput.classList.remove('is-valid');
        if (lenInput) lenInput.classList.add('is-invalid');
        showToast('error', msg);
    });
}

function saveCoilRow(idx, retryCount) {
    const row = coilState.rows[idx];
    if (!row) return;
    const partNumber = (row.part_number || '').trim();
    const coilId = (row.coil_id || '').trim();
    const length = parseFloat(row.length);
    if (!partNumber) { showToast('error', __('coils.need_part') || '请选择物料'); return; }
    if (!coilId) { showToast('error', __('coils.need_coilid')); return; }
    if (!(length > 0)) { showToast('error', __('coils.length_positive')); return; }
    retryCount = retryCount || 0;

    apiPost('/api/requests/' + coilState.requestId + '/coils', {
        items: [{
            part_number: partNumber,
            coil_id: coilId,
            length: length,
            lot_no: (row.lot_no || '').trim(),
            unit: coilState.unitMap[partNumber] || '',
            item_id: coilState.itemId  // 按申请单行绑定
        }]
    }, function (resp) {
        showToast('success', '卷标已保存');
        // 保存成功后：清空录入行并重置一行（自动生成新卷号，便于连续录入下一卷），刷新已录入列表
        coilState.rows = [];
        renderCoilRows();
        addCoilRow(coilState.defaultPart);
        apiGet('/api/requests/' + coilState.requestId + '/coils' + (coilState.itemId ? '?item_id=' + coilState.itemId : ''), function (resp2) {
            coilState.existingCoils = resp2.data || [];
            renderExistingCoils();
        }, showError);
    }, function (err) {
        // 卷号已存在 → 自动重新生成新卷号并重试（最多2次），无需用户手动操作
        const msg = err.message || '';
        if (msg.indexOf('已存在') >= 0 && retryCount < 2) {
            genCoilIdForRow(idx);
            setTimeout(function () { saveCoilRow(idx, retryCount + 1); }, 300);
            return;
        }
        showToast('error', msg || '保存失败');
    });
}

function deleteCoil(id, coilId) {
    if (!id) return;
    if (!confirm('确认删除卷标 ' + (coilId || id) + ' 吗？')) return;
    apiDelete('/api/coils/' + id, function (resp) {
        showToast('success', '卷标已删除');
        apiGet('/api/requests/' + coilState.requestId + '/coils' + (coilState.itemId ? '?item_id=' + coilState.itemId : ''), function (resp2) {
            coilState.existingCoils = resp2.data || [];
            renderExistingCoils();
        }, showError);
    }, function (err) {
        showToast('error', err.message || '删除失败');
    });
}

function renderExistingCoils() {
    const tbody = document.getElementById('existingCoilsBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!coilState.existingCoils || coilState.existingCoils.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted py-3">' + __('coils.empty') + '</td></tr>';
        return;
    }
    // 从后往前删除：仅最后一条（id 最大，列表末尾）允许删除
    const maxId = coilState.existingCoils.reduce(function (m, c) {
        return (c.id > m) ? c.id : m;
    }, 0);
    coilState.existingCoils.forEach(function (c) {
        const isLast = (c.id === maxId);
        const delBtn = isLast
            ? '<button class="btn btn-sm btn-outline-danger" onclick="deleteCoil(' + c.id + ', \'' + escapeHtml(c.coil_id) + '\')" title="删除卷标（从后往前删）"><i class="fas fa-trash"></i></button>'
            : '<button class="btn btn-sm btn-outline-secondary" disabled title="请先从最后一条开始删除"><i class="fas fa-trash"></i></button>';
        // 从在库选用的卷标：独立列显示「取消选用」按钮（还原回可选清单）
        // 注意：prev_request_id 可能为 0（可选池），须用 null 判断，不能当 falsy
        const unuseBtn = (c.prev_request_id !== null && c.prev_request_id !== undefined)
            ? '<button class="btn btn-sm btn-outline-warning" onclick="unuseStockCoil(\'' + escapeHtml(c.coil_id) + '\')" title="取消选用，卷标回到在库可选清单"><i class="fas fa-undo-alt"></i></button>'
            : '';
        const tr = document.createElement('tr');
        // 鼠标所在行背景浅色高亮（table-hover 由父表格启用，这里补充行内样式保证生效）
        tr.style.cursor = 'pointer';
        // 点击所在行 → 预览显示该行卷标（含卷标ID）
        tr.onclick = function () { previewExistingCoil(c.coil_id); };
        tr.innerHTML = `
            <td class="text-center" onclick="event.stopPropagation()">
                <input type="checkbox" class="form-check-input coil-select" value="${c.id}" data-coil="${escapeHtml(c.coil_id)}"
                       onchange="this.closest('tr').classList.toggle('coil-row-selected', this.checked); updateBatchPrintBtn();" title="选择该行">
            </td>
            <td>${escapeHtml(c.coil_id)}</td>
            <td>${escapeHtml(c.lot_no || '-')}</td>
            <td>${escapeHtml(c.part_number)}</td>
            <td>${c.coil_length}</td>
            <td>${escapeHtml(c.unit || '-')}</td>
            <td><span class="badge ${c.status === 'in_stock' ? 'bg-success' : c.status === 'issued' ? 'bg-secondary' : 'bg-danger'}">${escapeHtml(c.status_label || c.status)}</span></td>
            <td class="text-center" onclick="event.stopPropagation()">
                <button class="btn btn-sm btn-outline-success" onclick="printCoils(['${escapeHtml(c.coil_id)}'])" title="打印本行卷标"><i class="fas fa-print"></i></button>
            </td>
            <td class="text-center" onclick="event.stopPropagation()">
                <button class="btn btn-sm btn-outline-info" onclick="previewExistingCoil('${escapeHtml(c.coil_id)}')"><i class="fas fa-eye"></i></button>
            </td>
            <td class="text-center" onclick="event.stopPropagation()">${unuseBtn}</td>
            <td class="text-center" onclick="event.stopPropagation()">${delBtn}</td>
        `;
        tbody.appendChild(tr);
    });
    // 渲染后重置批量打印按钮状态（勾选已清空 → 禁用）
    updateBatchPrintBtn();
}

function printBatchSelected() {
    // 批量打印：打印所有勾选的行（仅多选时按钮可用）
    const selected = getSelectedCoils();
    if (selected.length < 2) {
        showToast('error', '请先勾选多行卷标');
        return;
    }
    printCoils(selected);
}

function getSelectedCoils() {
    // 收集已录入卷标表格中勾选行的卷标ID
    return Array.from(document.querySelectorAll('#existingCoilsBody .coil-select:checked'))
        .map(function (cb) { return cb.getAttribute('data-coil'); })
        .filter(Boolean);
}

function updateBatchPrintBtn() {
    // 批量打印按钮：仅当勾选 ≥2 行（多选）时启用
    const btn = document.getElementById('batchPrintBtn');
    if (!btn) return;
    btn.disabled = getSelectedCoils().length < 2;
}

function previewExistingCoil(coilId) {
    // 优先用后端渲染数据，保证与打印版式一致
    apiGet('/api/coils/' + encodeURIComponent(coilId) + '/label', function (resp) {
        renderLabelPreview(resp.data);
    }, function (err) {
        // 兜底：用列表数据渲染
        const coil = coilState.existingCoils.find(function (c) { return c.coil_id === coilId; });
        if (coil) renderLabelPreview(coil);
        else showToast('error', err.message || '预览失败');
    });
}

/* ---- 标签预览（300px × 100px 缩放，JsBarcode Code128） ---- */

function updatePreview() {
    const row = coilState.rows.find(function (r) { return r.coil_id; });
    if (row) {
        renderLabelPreview({
            coil_id: row.coil_id,
            part_number: row.part_number,
            length: parseFloat(row.length) || 0,
            unit: coilState.unitMap[row.part_number] || ''
        });
    } else {
        const box = document.getElementById('labelPreviewBox');
        if (box) box.classList.add('d-none');
    }
}

function renderLabelPreview(coil) {
    const box = document.getElementById('labelPreviewBox');
    if (!box || !coil) return;
    const coilId = coil.coil_id || '';
    const part = coil.part_number || '';
    const lengthText = (coil.length != null ? parseFloat(coil.length).toString() : '') +
                       (coil.unit ? ' ' + coil.unit : '');
    box.classList.remove('d-none');
    box.style.width = '300px';
    box.style.height = '100px';
    box.style.border = '1px dashed #999';
    box.style.position = 'relative';
    box.style.background = '#fff';
    // 与 ZPL/TSPL 版式一致：顶部条码 → 条码下方卷号 → Part → Lenght，全部左对齐竖排
    box.innerHTML = `
        <svg id="previewBarcodeCoil" style="position:absolute;left:6px;top:4px;height:30px;"></svg>
        <div style="position:absolute;left:6px;top:38px;font-size:13px;font-weight:bold;">${escapeHtml(coilId)}</div>
        <div style="position:absolute;left:6px;top:58px;font-size:11px;">Part : ${escapeHtml(part)}</div>
        <div style="position:absolute;left:6px;top:76px;font-size:11px;">Lenght :${escapeHtml(lengthText)}</div>
    `;
    if (window.JsBarcode) {
        try {
            JsBarcode('#previewBarcodeCoil', coilId, {format: 'CODE128', width: 1, height: 30, displayValue: false, margin: 0});
        } catch (e) {
            console.warn('JsBarcode 渲染失败', e);
        }
    }
}

/* ---- 保存 ---- */

function saveCoils(printAfter) {
    const items = [];
    const seenCoil = {};
    const submittedParts = {};
    const errors = [];

    coilState.rows.forEach(function (row, i) {
        if (!row.part_number) {
            errors.push('第' + (i + 1) + '行未选择物料');
            return;
        }
        if (!row.coil_id) {
            errors.push('第' + (i + 1) + '行（' + row.part_number + '）未填写卷号');
            return;
        }
        if (!/^\d{9}$/.test(row.coil_id)) {
            errors.push('第' + (i + 1) + '行卷号 ' + row.coil_id + ' 格式无效（应为9位数字 YYMMDD+3位）');
            return;
        }
        const len = parseFloat(row.length);
        if (!(len > 0)) {
            errors.push(__('coils.row_length_positive').replace('{row}', i + 1).replace('{part}', row.part_number));
            return;
        }
        if (seenCoil[row.coil_id]) {
            errors.push('卷号 ' + row.coil_id + ' 重复');
            return;
        }
        seenCoil[row.coil_id] = true;
        submittedParts[row.part_number] = true;
        items.push({
            part_number: row.part_number,
            coil_id: row.coil_id,
            length: len,
            lot_no: (row.lot_no || '').trim(),
            unit: coilState.unitMap[row.part_number] || ''
        });
    });

    if (errors.length) {
        showToast('error', errors[0]);
        return;
    }

    // A/B 开头物料必须录入（前端拦截）
    const seenReq = {};
    (coilState.request.items || []).forEach(function (it) { seenReq[it.part_number] = true; });
    for (var p in seenReq) {
        if (isABPart(p) && !submittedParts[p]) {
            showToast('error', '物料 ' + p + ' 以 A/B 开头，必须录入卷标信息');
            return;
        }
    }

    apiPost('/api/requests/' + coilState.requestId + '/coils', {items: items}, function (resp) {
        showToast('success', resp.message || '保存成功');
        // 刷新已录入卷标
        apiGet('/api/requests/' + coilState.requestId + '/coils', function (resp2) {
            coilState.existingCoils = resp2.data || [];
            coilState.rows = [];
            renderCoilRows();
            renderExistingCoils();
            if (printAfter && resp.inserted > 0) {
                printCoils((resp.data || []).map(function (c) { return c.coil_id; }));
            }
        }, showError);
    }, function (err) {
        showToast('error', err.message || '保存失败');
    });
}

function printCoils(coilIds) {
    if (!coilIds || coilIds.length === 0) {
        showToast('error', '没有可打印的卷标');
        return;
    }
    apiPost('/api/requests/' + coilState.requestId + '/coils/print',
            {coil_ids: coilIds}, function (resp) {
        showToast('success', resp.message || '打印成功');
    }, function (err) {
        // 打印失败不影响卷标保存等其他操作
        showToast('error', err.message || '打印失败（不影响已保存的卷标）');
    });
}

/* ================= 出库登记弹窗 ================= */

function openOutboundModal(requestId) {
    outboundState = {requestId: requestId, coils: []};
    document.getElementById('outboundModalTitle').textContent = '#' + requestId;
    loadConsumptionRecords(requestId);

    apiGet('/api/requests/' + requestId + '/coils', function (resp) {
        outboundState.coils = (resp.data || []).filter(function (c) { return c.status === 'in_stock'; });
        renderOutboundRows();
        const modal = new bootstrap.Modal(document.getElementById('outboundModal'));
        modal.show();
    }, showError);
}

function renderOutboundRows() {
    const tbody = document.getElementById('outboundRowsBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (outboundState.coils.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">该申请单没有「在库」状态的卷标，无法出库</td></tr>';
        return;
    }
    outboundState.coils.forEach(function (c, idx) {
        const unit = c.unit || '';
        const totalMm = coilTotalMm(c);
        // 出库长度默认填该卷总长（mm），整卷出库；单位未知时直接用原值
        const defLen = totalMm != null ? totalMm : c.coil_length;
        const preview = unit ? (escapeHtml(unit) + ': ' + convertPreview(defLen, unit)) : '-';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="text-center"><input type="checkbox" class="form-check-input outbound-check" data-idx="${idx}" checked></td>
            <td>${escapeHtml(c.coil_id)}</td>
            <td>${escapeHtml(c.part_number)}</td>
            <td>${c.coil_length} ${escapeHtml(unit || '-')}</td>
            <td><input type="number" class="form-control form-control-sm outbound-length" data-idx="${idx}" value="${defLen}" min="0" step="0.01"></td>
            <td class="small text-muted outbound-preview" data-idx="${idx}">${preview}</td>
            <td><input type="text" class="form-control form-control-sm outbound-job" data-idx="${idx}" placeholder="${__('coils.job_placeholder')}"></td>
        `;
        tbody.appendChild(tr);
    });
}

function loadConsumptionRecords(requestId) {
    const tbody = document.getElementById('consumptionBody');
    if (!tbody) return;
    apiGet('/api/requests/' + requestId + '/consumption', function (resp) {
        const rows = resp.data || [];
        tbody.innerHTML = '';
        if (rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-2">暂无出库记录</td></tr>';
            return;
        }
        rows.forEach(function (r) {
            let conv = '-';
            if (r.converted_length != null && r.converted_unit) {
                conv = r.converted_length + ' ' + escapeHtml(r.converted_unit);
            }
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${escapeHtml(r.coil_id)}</td>
                <td>${escapeHtml(r.part_number)}</td>
                <td>${r.out_length} mm</td>
                <td>${conv}</td>
                <td>${escapeHtml(r.job_order || '-')}</td>
                <td>${escapeHtml(r.operator)}</td>
                <td>${r.created_at || '-'}</td>
            `;
            tbody.appendChild(tr);
        });
    }, function () {
        // 查询失败不阻断弹窗
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-2">出库记录加载失败</td></tr>';
    });
}

function submitOutbound() {
    const items = [];
    const rows = document.querySelectorAll('#outboundRowsBody tr');
    let firstError = null;

    // 汇总 6 组折叠面板的宽表加工参数（全部选填，空值提交为空字符串由后端按 NULL 处理）
    const extra = {};
    OUTBOUND_EXTRA_FIELDS.forEach(function (f) {
        const el = document.getElementById('ob_' + f);
        extra[f] = el ? el.value.trim() : '';
    });

    rows.forEach(function (tr, i) {
        const check = tr.querySelector('.outbound-check');
        if (!check || !check.checked) return;
        const coil = outboundState.coils[i];
        if (!coil) return;
        const len = parseFloat(tr.querySelector('.outbound-length').value);
        if (!(len > 0)) {
            if (!firstError) firstError = '卷标 ' + coil.coil_id + ' 出库长度必须大于0';
            return;
        }
        const job = tr.querySelector('.outbound-job').value.trim();
        const item = {coil_id: coil.coil_id, job_order: job, out_length: len};
        Object.keys(extra).forEach(function (k) { item[k] = extra[k]; });
        items.push(item);
    });

    if (firstError) {
        showToast('error', firstError);
        return;
    }
    if (items.length === 0) {
        showToast('error', '请至少勾选一条出库记录');
        return;
    }

    apiPost('/api/requests/' + outboundState.requestId + '/consumption', {items: items}, function (resp) {
        showToast('success', resp.message || '出库登记成功');
        const modalEl = document.getElementById('outboundModal');
        const m = bootstrap.Modal.getInstance(modalEl);
        if (m) m.hide();
        setTimeout(function () { window.location.reload(); }, 800);
    }, function (err) {
        showToast('error', err.message || '出库登记失败');
    });
}

/* ================= 事件委托（行内输入变更同步 state） ================= */

document.addEventListener('DOMContentLoaded', function () {
    const rowsBody = document.getElementById('coilRowsBody');
    if (rowsBody) {
        rowsBody.addEventListener('input', function (e) {
            const idx = parseInt(e.target.dataset.idx, 10);
            if (isNaN(idx) || !coilState.rows[idx]) return;
            if (e.target.classList.contains('coil-id-input')) {
                coilState.rows[idx].coil_id = e.target.value.trim();
                updatePreview();
            } else if (e.target.classList.contains('coil-length-input')) {
                coilState.rows[idx].length = e.target.value;
            } else if (e.target.classList.contains('coil-lot-input')) {
                coilState.rows[idx].lot_no = e.target.value;
            }
        });
        rowsBody.addEventListener('change', function (e) {
            if (e.target.classList.contains('coil-part-select')) {
                const idx = parseInt(e.target.dataset.idx, 10);
                if (isNaN(idx) || !coilState.rows[idx]) return;
                coilState.rows[idx].part_number = e.target.value;
                renderCoilRows();
                updatePreview();
            }
        });
    }

    // 出库长度：实时刷新换算预览；失焦校验不超过卷长（mm）
    const outBody = document.getElementById('outboundRowsBody');
    if (outBody) {
        outBody.addEventListener('input', function (e) {
            if (!e.target.classList.contains('outbound-length')) return;
            const idx = parseInt(e.target.dataset.idx, 10);
            const coil = outboundState.coils[idx];
            if (!coil) return;
            const len = parseFloat(e.target.value);
            const previewCell = outBody.querySelector('.outbound-preview[data-idx="' + idx + '"]');
            if (previewCell) {
                const unit = coil.unit || '';
                previewCell.textContent = (unit ? unit + ': ' : '') + convertPreview(len, unit);
            }
        });
        outBody.addEventListener('change', function (e) {
            if (!e.target.classList.contains('outbound-length')) return;
            const idx = parseInt(e.target.dataset.idx, 10);
            const coil = outboundState.coils[idx];
            if (!coil) return;
            const len = parseFloat(e.target.value);
            const totalMm = coilTotalMm(coil);
            if (totalMm != null && len > totalMm) {
                showToast('error', '卷标 ' + coil.coil_id + ' 出库长度不能超过卷长 ' + totalMm + 'mm');
                e.target.value = totalMm;
                const previewCell = outBody.querySelector('.outbound-preview[data-idx="' + idx + '"]');
                if (previewCell) {
                    const unit = coil.unit || '';
                    previewCell.textContent = (unit ? unit + ': ' : '') + convertPreview(totalMm, unit);
                }
            }
        });
    }
});
