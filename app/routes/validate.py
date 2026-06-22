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
