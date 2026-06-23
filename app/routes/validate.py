"""
工单验证、物料验证、库存查询 API（通过 Infor CSI IDO API 实时查询）
"""
from flask import Blueprint, request, jsonify, session
from app.services.csi_service import CSIClient, parse_job

validate_bp = Blueprint('validate', __name__)


@validate_bp.route('/api/validate/job', methods=['POST'])
def api_validate_job():
    """工单验证 - 输入完整工单号，返回工单信息"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    data = request.get_json()
    job_full = (data.get('job_full') or '').strip()
    siteref = data.get('siteref') or user.get('siteref', '310')

    if not job_full:
        return jsonify({'success': False, 'message': '请输入工单号'}), 400

    # 解析工单号
    job, suffix = parse_job(job_full)
    if not job or suffix is None:
        return jsonify({
            'success': False,
            'message': '工单号格式无效，应为 J000002124-0004 格式'
        }), 400

    # 通过 CSI API 查询工单
    client = CSIClient(siteref=siteref)
    result = client.validate_job(job, suffix)
    if not result:
        return jsonify({'success': False, 'message': '工单不存在'}), 404

    return jsonify({
        'success': True,
        'data': {
            'Job': result.get('Job', job),
            'Suffix': result.get('Suffix', suffix),
            'Item': result.get('Item', ''),
            'Stat': result.get('Stat', ''),
            'Description': result.get('Description', ''),
        }
    })


@validate_bp.route('/api/validate/material', methods=['POST'])
def api_validate_material():
    """物料验证 + 库存查询"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    data = request.get_json()
    job = (data.get('job') or '').strip()
    suffix = data.get('suffix')
    item = (data.get('item') or '').strip()
    siteref = data.get('siteref') or user.get('siteref', '310')

    if not job or suffix is None or not item:
        return jsonify({'success': False, 'message': '参数不完整'}), 400

    client = CSIClient(siteref=siteref)

    # 物料验证
    matl = client.validate_material(job, suffix, item)
    if not matl:
        return jsonify({
            'success': False,
            'message': '该物料不在工单BOM中或不需要手动领料'
        })

    # 库存查询
    inventory = client.get_inventory(item)
    stock_list = []
    stock_summary = ''
    if inventory:
        stock_list = [
            {'loc': inv['Loc'], 'qty': float(inv['QtyOnHand'])}
            for inv in inventory
        ]
        stock_summary = ', '.join(
            [f"{inv['Loc']}({float(inv['QtyOnHand'])})" for inv in inventory]
        )

    # 获取物料单价
    unit_cost = client.get_item_cost(item)

    return jsonify({
        'success': True,
        'data': {
            'material': matl,
            'inventory': stock_list,
            'stock_summary': stock_summary,
            'unit_cost': unit_cost
        }
    })


@validate_bp.route('/api/validate/part-stock', methods=['GET'])
def api_part_stock():
    """最小包装用：按 Part Number 查库存（排除floor库位），返回库存量、位置、单价"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    part_number = request.args.get('part_number', '').strip()
    if not part_number:
        return jsonify({'success': False, 'message': '请输入Part Number'}), 400

    siteref = request.args.get('siteref') or user.get('siteref', '310')

    client = CSIClient(siteref=siteref)

    # Backflush 验证暂时取消
    # backflush = client.get_item_backflush(part_number)
    # if backflush is None:
    #     return jsonify({'success': False, 'message': f'物料 {part_number} 在系统中不存在'}), 404
    # if backflush is False:
    #     return jsonify({'success': False, 'message': f'物料 {part_number} 未标记Backflush，不支持最小包装发放'}), 400

    inventory = client.get_inventory(part_number)
    unit_cost = client.get_item_cost(part_number)

    # 过滤掉名称包含 "floor" 的库位（不区分大小写）
    valid_locs = []
    stock_qty = 0
    if inventory:
        for inv in inventory:
            loc_name = (inv.get('Loc') or '').lower()
            if 'floor' in loc_name:
                continue
            qty = float(inv.get('QtyOnHand', 0))
            valid_locs.append({'loc': inv['Loc'], 'qty': qty})
            stock_qty += qty

    # 组装位置字符串
    loc_str = ', '.join([f"{l['loc']}({l['qty']})" for l in valid_locs]) if valid_locs else ''
    price = unit_cost if unit_cost else 0

    return jsonify({
        'success': True,
        'data': {
            'part_number': part_number,
            'stock_qty': stock_qty,
            'price': price,
            'locations': loc_str,
            'location_list': valid_locs
        }
    })


@validate_bp.route('/api/validate/part-lots', methods=['GET'])
def api_part_lots():
    """查询物料批号库存，按 FIFO 排序，排除 floor 库位"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    part_number = request.args.get('part_number', '').strip()
    qty_needed = request.args.get('qty', type=float)
    if not part_number:
        return jsonify({'success': False, 'message': '请输入Part Number'}), 400

    siteref = request.args.get('siteref') or user.get('siteref', '310')
    client = CSIClient(siteref=siteref)
    lots = client.get_item_lots(part_number)

    # FIFO 分配：按创建时间排序已由 API 完成
    fifo_allocation = []
    remaining = qty_needed
    for lot in lots:
        if remaining is None or remaining <= 0:
            break
        take = min(lot['qty_on_hand'], remaining)
        fifo_allocation.append({
            'lot': lot['lot'],
            'loc': lot['loc'],
            'qty_take': take,
            'qty_on_hand': lot['qty_on_hand'],
        })
        if remaining is not None:
            remaining -= take

    return jsonify({
        'success': True,
        'data': {
            'part_number': part_number,
            'qty_needed': qty_needed,
            'lots': lots,
            'fifo_allocation': fifo_allocation,
            'total_available': sum(l['qty_on_hand'] for l in lots),
        }
    })
