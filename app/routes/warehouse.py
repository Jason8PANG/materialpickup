import logging

from flask import Blueprint, request, jsonify, session
from datetime import datetime
from app.models import get_db_connection, get_site_filter
from app.services.csi_interface import enqueue_qty_move, enqueue_reverse_qty_move
from app.services.csi_service import CSIClient
from app.utils import WhereBuilder, is_en

warehouse_bp = Blueprint('warehouse', __name__)
logger = logging.getLogger(__name__)


def add_log(cursor, request_id, operator, action, detail, ip):
    cursor.execute(
        "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (request_id, operator, action, detail, ip, datetime.now())
    )


def check_warehouse_or_admin():
    """检查仓库或管理员权限"""
    user = session.get('user')
    if not user:
        return None, jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return None, jsonify({'success': False, 'message': '权限不足'}), 403
    return user, None, None


def validate_site_match(request_row, user):
    """
    校验站点匹配。
    admin 可跨站，非 admin 必须本站点。
    """
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        if request_row['siteref'] != site_params[0]:
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403
    return None


# 批次累计数量比较的浮点容差
LOT_QTY_TOLERANCE = 0.0001


def _fmt_qty(value):
    """数量展示：最多 4 位小数，去掉尾随零（1.2300 -> 1.23，2.0000 -> 2）"""
    try:
        s = f'{float(value):.4f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(value)
    return s or '0'


def validate_item_lots(cursor, request_id):
    """完成备料前校验非 A/B 明细的批次维护。

    规则（A/B 物料按卷标粒度维护，不要求批次表；short_reason 非空的缺料行豁免）：
      1. 每行至少 1 条 qty > 0 的 kr_request_item_lot 记录，否则视为未维护批次；
      2. 每行批次数量合计不得超过明细申请数量（浮点容差 0.0001）。
    返回错误提示文案；校验通过返回 None。
    """
    cursor.execute(
        "SELECT id, part_number, quantity, short_reason FROM kr_request_item "
        "WHERE request_id = %s ORDER BY id",
        (request_id,)
    )
    items = [it for it in cursor.fetchall()
             if (it.get('part_number') or '')[:1].upper() not in ('A', 'B')
             and (it.get('short_reason') or '').strip() == '']
    if not items:
        return None

    placeholders = ','.join(['%s'] * len(items))
    cursor.execute(
        f"SELECT item_id, COALESCE(SUM(qty), 0) AS total_qty, "
        f"SUM(CASE WHEN qty > 0 THEN 1 ELSE 0 END) AS positive_cnt "
        f"FROM kr_request_item_lot "
        f"WHERE request_id = %s AND item_id IN ({placeholders}) GROUP BY item_id",
        [request_id] + [it['id'] for it in items]
    )
    stats = {r['item_id']: (float(r.get('total_qty') or 0), int(r.get('positive_cnt') or 0))
             for r in cursor.fetchall()}

    en = is_en()
    row_label = 'row' if en else '行'
    missing, over = [], []
    for it in items:
        total, positive = stats.get(it['id'], (0.0, 0))
        if positive == 0:
            missing.append(f"{it['part_number']}（{row_label} {it['id']}）" if not en
                           else f"{it['part_number']}(row{it['id']})")
            continue
        item_qty = float(it.get('quantity') or 0)
        if total > item_qty + LOT_QTY_TOLERANCE:
            over.append((it['part_number'], it['id'], total, item_qty))

    if not missing and not over:
        return None

    if en:
        parts = []
        if missing:
            parts.append('Materials without lot allocation: ' + ', '.join(missing)
                         + '; please maintain lots on the prep detail page')
        if over:
            parts.append('Lot total exceeds requested quantity: ' + '; '.join(
                f'{p}(row{r}) {_fmt_qty(t)} > {_fmt_qty(q)}' for p, r, t, q in over))
        return ' | '.join(parts)

    parts = []
    if missing:
        parts.append('物料 ' + '、'.join(missing) + '未维护批次，请先在备料详情页维护批次')
    if over:
        parts.append('批次累计超过申请数量：' + '；'.join(
            f'物料 {p}（行 {r}）批次累计 {_fmt_qty(t)} 超过申请数量 {_fmt_qty(q)}' for p, r, t, q in over))
    return '；'.join(parts)


@warehouse_bp.route('/api/requests/<int:request_id>/start-prep', methods=['POST'])
def start_prep(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

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

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'pending_prep':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许开始备料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'prepping', warehouse_operator = %s WHERE id = %s",
            (user['username'], request_id)
        )
        add_log(cursor, request_id, user['username'], 'START_PREP', '开始备料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '开始备料'})


@warehouse_bp.route('/api/requests/<int:request_id>/complete-prep', methods=['POST'])
def complete_prep(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

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

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'prepping':
            cursor.close()
            return jsonify({'success': False,
                            'message': 'Current status does not allow completing prep' if is_en() else '当前状态不允许完成备料'}), 400

        # 卷标按行维护校验：仅最小包装（minpack）申请单 —— A/B 开头物料的每一行
        # 都必须至少录入 1 个卷标，否则阻止完成备料（卷标录入仅对 minpack 开放）
        # 例外：已按行维护缺料原因（short_reason 非空）的 A/B 行不发卷，豁免该校验
        if (req.get('request_type') or '') == 'minpack':
            cursor.execute(
                "SELECT id, part_number, short_reason FROM kr_request_item WHERE request_id = %s",
                (request_id,)
            )
            # 需录卷标的 A/B 行：仅限未缺料（short_reason 为空）的行；已按行维护缺料原因的行不发卷，豁免卷标校验
            ab_items = [it for it in cursor.fetchall()
                        if (it['part_number'] or '')[:1].upper() in ('A', 'B')
                        and (it.get('short_reason') or '').strip() == '']
            if ab_items:
                placeholders = ','.join(['%s'] * len(ab_items))
                cursor.execute(
                    f"SELECT item_id FROM kr_wire_coil WHERE request_id = %s AND item_id IN ({placeholders}) "
                    f"AND is_deleted = 0 GROUP BY item_id",
                    [request_id] + [it['id'] for it in ab_items]
                )
                covered_item_ids = {r['item_id'] for r in cursor.fetchall()}
                missing = [it for it in ab_items if it['id'] not in covered_item_ids]
                if missing:
                    en = is_en()
                    row_label = 'row' if en else '行'
                    parts = ', '.join(f"{it['part_number']}({row_label}{it['id']})" for it in missing)
                    cursor.close()
                    if en:
                        return jsonify({
                            'success': False,
                            'message': f'A/B materials require coil entries: {parts}, please maintain coils on the prep detail page'
                        }), 400
                    return jsonify({
                        'success': False,
                        'message': f'A/B 开头物料必须录入卷标：{parts}，请先在备料详情页维护卷标'
                    }), 400

        # 批次维护校验：非 A/B 物料必须维护批次，且批次累计不得超过申请数量。
        # A/B 物料走卷标粒度（卷标录入不写批次表），不要求批次。
        lot_error = validate_item_lots(cursor, request_id)
        if lot_error:
            cursor.close()
            return jsonify({'success': False, 'message': lot_error}), 400

        now = datetime.now()
        # 写入 CSI 库位转移接口表：与状态更新同事务，失败整体回滚
        try:
            enqueue_qty_move(cursor, req, user['username'], now)
        except Exception:
            db.rollback()
            cursor.close()
            logger.exception('完成备料：写入 CSI 接口表失败 request_id=%s', request_id)
            return jsonify({'success': False,
                            'message': 'Failed to queue CSI location transfer, prep not completed'
                                       if is_en() else '写入 CSI 库位转移接口失败，备料未完成'}), 500

        cursor.execute(
            "UPDATE kr_material_request SET status = 'ready_pickup' WHERE id = %s",
            (request_id,)
        )
        add_log(cursor, request_id, user['username'], 'COMPLETE_PREP', '完成备料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '备料完成'})


@warehouse_bp.route('/api/requests/<int:request_id>/restore-from-short', methods=['POST'])
def restore_from_short(request_id):
    """缺料恢复：缺料 → 待取料"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

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

        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'short':
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许转为待取料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'ready_pickup', short_reason = NULL, short_time = NULL WHERE id = %s",
            (request_id,)
        )
        add_log(cursor, request_id, user['username'], 'RESTORE_FROM_SHORT', '缺料恢复→待取料', request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '已转为待取料'})


@warehouse_bp.route('/api/requests/<int:request_id>/back-to-prepping', methods=['POST'])
def back_to_prepping(request_id):
    """待取料退回备料中：用于补录/修正卷标、出库登记等。

    已成功过账的库位转移需生成反向交易（from/to 对调）写回接口表，
    由同步器提交，否则物料在 CSI 里会停留在车间库位。
    """
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

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

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] != 'ready_pickup':
            cursor.close()
            return jsonify({'success': False,
                            'message': '仅待取料（ready_pickup）状态的申请单可回到备料中'}), 400

        now = datetime.now()
        # 反向库位转移与状态更新同事务，失败整体回滚（状态不流转）
        try:
            rev = enqueue_reverse_qty_move(cursor, req, user['username'], now)
        except Exception:
            db.rollback()
            cursor.close()
            logger.exception('回到备料：生成反向 CSI 库位转移失败 request_id=%s', request_id)
            return jsonify({'success': False,
                            'message': 'Failed to queue reverse CSI location transfer, status unchanged'
                                       if is_en() else '回到备料失败，请稍后重试'}), 500

        cursor.execute(
            "UPDATE kr_material_request SET status = 'prepping' WHERE id = %s",
            (request_id,)
        )
        # 日志明细：反向笔数 + 作废的未过账记录数
        notes = []
        if rev['inserted']:
            notes.append(f"生成反向库位转移 {rev['inserted']} 笔")
        else:
            notes.append('无已过账记录，未生成反向交易')
        if rev['cancelled']:
            notes.append(f"作废未过账记录 {rev['cancelled']} 笔")
        add_log(cursor, request_id, user['username'], 'BACK_TO_PREPPING',
                f"待取料退回备料中（{'，'.join(notes)}）", request.remote_addr)
        db.commit()
        cursor.close()

    message = '已回到备料中，可继续维护卷标/出库登记'
    if rev['inserted']:
        message = f"已回到备料中（已生成 {rev['inserted']} 笔反向库位转移），可继续维护卷标/出库登记"
    elif rev['forward_count'] == 0:
        message = '已回到备料中（无已过账记录，未生成反向交易），可继续维护卷标/出库登记'
    return jsonify({'success': True, 'message': message})


@warehouse_bp.route('/api/requests/<int:request_id>/short', methods=['POST'])
def short_material(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    short_reason = data.get('short_reason', '').strip()
    if not short_reason:
        return jsonify({'success': False, 'message': '请填写缺料原因'}), 400

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

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] not in ('prepping', 'pending_prep'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许登记缺料'}), 400

        now = datetime.now()
        cursor.execute(
            "UPDATE kr_material_request SET status = 'short', short_reason = %s, short_time = %s WHERE id = %s",
            (short_reason, now, request_id)
        )
        add_log(cursor, request_id, user['username'], 'SHORT', f"缺料: {short_reason}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '缺料登记成功'})


@warehouse_bp.route('/api/requests/<int:request_id>/sign', methods=['POST'])
def sign_request(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

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

        # 站点校验
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cursor.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        if req['status'] not in ('ready_pickup', 'short'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许签字确认'}), 400

        now = datetime.now()
        data = request.get_json() or {}
        signature_data = data.get('signature_data', '')

        cursor.execute(
            "UPDATE kr_material_request SET status = 'completed', signer = %s, sign_time = %s, signature_data = %s WHERE id = %s",
            (user['username'], now, signature_data, request_id)
        )
        # 取料完成（sign）：该申请单关联的在库卷标 → 在车间（已转移至车间库位，脱离仓库库存）
        cursor.execute(
            "UPDATE kr_wire_coil SET status = 'in_shop' "
            "WHERE request_id = %s AND status = 'in_stock'",
            (request_id,)
        )
        moved = cursor.rowcount
        add_log(cursor, request_id, user['username'], 'SIGN',
                f'签字确认完成，{moved} 卷标转入车间' if moved else '签字确认完成',
                request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '签字确认成功'})


@warehouse_bp.route('/api/requests/<int:request_id>/assign-worker', methods=['PUT'])
def assign_worker(request_id):
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    worker = data.get('warehouse_operator', '').strip()
    if not worker:
        return jsonify({'success': False, 'message': '请指定备料员'}), 400

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

        # 站点校验
        err = validate_site_match(req, user)
        if err:
            cursor.close()
            return err

        if req['status'] not in ('pending_prep', 'prepping'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不能指定备料员'}), 400

        cursor.execute(
            "UPDATE kr_material_request SET warehouse_operator = %s WHERE id = %s",
            (worker, request_id)
        )
        add_log(cursor, request_id, user['username'], 'ASSIGN_WORKER',
                f"指定备料员: {worker}", request.remote_addr)
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '已指定备料员'})


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/short', methods=['PUT'])
def update_item_short_reason(request_id, item_id):
    """按行更新缺料原因"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    short_reason = (data.get('short_reason') or '').strip()
    if not short_reason:
        return jsonify({'success': False, 'message': '请输入缺料原因'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        # 校验申请单
        cursor.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (request_id,)
        )
        req = cursor.fetchone()
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404

        # 校验申请单状态
        if req['status'] not in ('prepping', 'short'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不能维护缺料原因'}), 400

        # 更新明细行缺料原因
        cursor.execute(
            "UPDATE kr_request_item SET short_reason = %s WHERE id = %s AND request_id = %s",
            (short_reason, item_id, request_id)
        )
        if cursor.rowcount == 0:
            cursor.close()
            return jsonify({'success': False, 'message': '明细行不存在'}), 404

        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '缺料原因已保存'})


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/batch', methods=['PUT'])
def update_item_batch_no(request_id, item_id):
    """保存物料行批次号"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    batch_no = (data.get('batch_no') or '').strip()

    with get_db_connection() as db:
        cursor = db.cursor()
        # 先校验明细行存在性：MySQL 的 rowcount 是「实际变更行数」，
        # 写入值与现有值相同时为 0，不能据此判断行是否存在。
        _req, _item, err = _check_request_item(cursor, request_id, item_id, user)
        if err:
            cursor.close()
            return err[0], err[1]

        cursor.execute(
            "UPDATE kr_request_item SET batch_no = %s WHERE id = %s AND request_id = %s",
            (batch_no if batch_no else None, item_id, request_id)
        )
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': '批次号已保存'})


# ========== 明细行批次分配（非 A/B 料：多条「批号 + 数量」） ==========

def _check_request_item(cursor, request_id, item_id, user):
    """校验单据存在 + 站点匹配 + 明细行归属；返回 (req, item, err)。

    err 为 (Response, status) 元组，调用方直接 `return err[0], err[1]`。
    """
    cursor.execute(
        "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
        (request_id,)
    )
    req = cursor.fetchone()
    if not req:
        return None, None, (jsonify({'success': False, 'message': '单据不存在'}), 404)

    site_filter, site_params = get_site_filter(user)
    if site_filter and req['siteref'] != site_params[0]:
        return None, None, (jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403)

    cursor.execute(
        "SELECT id, request_id, part_number, quantity FROM kr_request_item "
        "WHERE id = %s AND request_id = %s",
        (item_id, request_id)
    )
    item = cursor.fetchone()
    if not item:
        return None, None, (jsonify({'success': False, 'message': '明细行不存在'}), 404)
    return req, item, None


def _parse_lot_payload(data):
    """校验批号/数量，返回 (lot_no, qty, err_response)"""
    lot_no = (data.get('lot_no') or '').strip()
    if not lot_no:
        return None, None, jsonify({'success': False, 'message': '批号不能为空'})
    if len(lot_no) > 64:
        return None, None, jsonify({'success': False, 'message': '批号长度不能超过 64 个字符'})

    raw_qty = data.get('qty')
    if raw_qty is None or str(raw_qty).strip() == '':
        return None, None, jsonify({'success': False, 'message': '数量不能为空'})
    try:
        qty = float(raw_qty)
    except (TypeError, ValueError):
        return None, None, jsonify({'success': False, 'message': '数量必须为数字'})
    if qty <= 0:
        return None, None, jsonify({'success': False, 'message': '数量必须大于 0'})
    if qty > 9999999999.99:
        return None, None, jsonify({'success': False, 'message': '数量超出允许范围'})
    return lot_no, round(qty, 4), None


def _resolve_lot_loc(siteref, part_number, lot_no):
    """查询 Lot 所在库位（CSI）；查询失败返回 None，不阻断批次维护"""
    if not part_number or not lot_no:
        return None
    try:
        loc = CSIClient(siteref or '410').get_lot_location(part_number, lot_no)
        return (loc or '').strip() or None
    except Exception as e:
        logger.warning('批次维护：查询 Lot 库位失败 item=%s lot=%s: %s', part_number, lot_no, e)
        return None


def _lots_payload(cursor, request_id, item_id, item):
    """读取某明细行的批次列表并计算合计/缺量提示"""
    cursor.execute(
        "SELECT id, lot_no, qty, loc FROM kr_request_item_lot "
        "WHERE request_id = %s AND item_id = %s ORDER BY id",
        (request_id, item_id)
    )
    rows = cursor.fetchall()
    data = [{'id': r['id'], 'lot_no': r['lot_no'], 'qty': float(r['qty']),
             'loc': r.get('loc')} for r in rows]
    total_qty = round(sum(r['qty'] for r in data), 4)
    item_quantity = float(item['quantity'] or 0)
    return {
        'success': True,
        'data': data,
        'total_qty': total_qty,
        'item_quantity': item_quantity,
        # 数量合计不足仅提示，不阻断保存
        'short': total_qty < item_quantity,
    }


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/lots', methods=['GET'])
def list_item_lots(request_id, item_id):
    """查询明细行批次分配列表"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        req, item, err = _check_request_item(cursor, request_id, item_id, user)
        if err:
            cursor.close()
            return err[0], err[1]
        payload = _lots_payload(cursor, request_id, item_id, item)
        cursor.close()
    return jsonify(payload)


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/lots', methods=['POST'])
def create_item_lot(request_id, item_id):
    """新增一条批次分配"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    lot_no, qty, bad = _parse_lot_payload(data)
    if bad:
        return bad, 400
    # Lot 所在库位：请求可显式传；未传则由 CSI 兜底
    loc = str(data.get('loc') or '').strip() or None

    with get_db_connection() as db:
        cursor = db.cursor()
        req, item, err = _check_request_item(cursor, request_id, item_id, user)
        if err:
            cursor.close()
            return err[0], err[1]

        if not loc:
            loc = _resolve_lot_loc(req['siteref'], item['part_number'], lot_no)

        cursor.execute(
            "INSERT INTO kr_request_item_lot (request_id, item_id, lot_no, qty, siteref, loc) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (request_id, item_id, lot_no, qty, req['siteref'], loc)
        )
        new_id = cursor.lastrowid
        add_log(cursor, request_id, user['username'], 'ITEM_LOT_ADD',
                f"明细行 {item_id} 批次 {lot_no} 数量 {qty}", request.remote_addr)
        db.commit()

        payload = _lots_payload(cursor, request_id, item_id, item)
        cursor.close()
    payload['message'] = '批次已添加'
    payload['id'] = new_id
    return jsonify(payload)


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/lots/<int:lot_id>',
                    methods=['PUT'])
def update_item_lot(request_id, item_id, lot_id):
    """修改一条批次分配（批号/数量均可选）"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    lot_no, qty, bad = _parse_lot_payload(data)
    if bad:
        return bad, 400
    # loc：可显式传（空串=清空）；未传且批号非空时自动补查
    has_loc = data.get('loc') is not None
    loc_new = (str(data.get('loc')).strip() or None) if has_loc else None

    with get_db_connection() as db:
        cursor = db.cursor()
        req, item, err = _check_request_item(cursor, request_id, item_id, user)
        if err:
            cursor.close()
            return err[0], err[1]

        cursor.execute(
            "SELECT id, lot_no, loc FROM kr_request_item_lot "
            "WHERE id = %s AND request_id = %s AND item_id = %s",
            (lot_id, request_id, item_id)
        )
        lot_row = cursor.fetchone()
        if not lot_row:
            cursor.close()
            return jsonify({'success': False, 'message': '批次记录不存在'}), 404

        if not has_loc:
            old_lot = (lot_row.get('lot_no') or '').strip() or None
            lot_changed = lot_no != old_lot
            if lot_no and (lot_changed or not (lot_row.get('loc') or '').strip()):
                loc_new = _resolve_lot_loc(req['siteref'], item['part_number'], lot_no)

        cursor.execute(
            "UPDATE kr_request_item_lot SET lot_no = %s, qty = %s, loc = %s "
            "WHERE id = %s AND request_id = %s AND item_id = %s",
            (lot_no, qty, loc_new, lot_id, request_id, item_id)
        )

        add_log(cursor, request_id, user['username'], 'ITEM_LOT_EDIT',
                f"明细行 {item_id} 批次 #{lot_id} 改为 {lot_no} 数量 {qty}",
                request.remote_addr)
        db.commit()

        payload = _lots_payload(cursor, request_id, item_id, item)
        cursor.close()
    payload['message'] = '批次已更新'
    return jsonify(payload)


@warehouse_bp.route('/api/requests/<int:request_id>/items/<int:item_id>/lots/<int:lot_id>',
                    methods=['DELETE'])
def delete_item_lot(request_id, item_id, lot_id):
    """删除一条批次分配"""
    user, err_resp, err_code = check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        req, item, err = _check_request_item(cursor, request_id, item_id, user)
        if err:
            cursor.close()
            return err[0], err[1]

        cursor.execute(
            "SELECT lot_no, qty FROM kr_request_item_lot "
            "WHERE id = %s AND request_id = %s AND item_id = %s",
            (lot_id, request_id, item_id)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return jsonify({'success': False, 'message': '批次记录不存在'}), 404

        cursor.execute(
            "DELETE FROM kr_request_item_lot "
            "WHERE id = %s AND request_id = %s AND item_id = %s",
            (lot_id, request_id, item_id)
        )
        add_log(cursor, request_id, user['username'], 'ITEM_LOT_DEL',
                f"明细行 {item_id} 删除批次 {row['lot_no']} 数量 {row['qty']}",
                request.remote_addr)
        db.commit()

        payload = _lots_payload(cursor, request_id, item_id, item)
        cursor.close()
    payload['message'] = '批次已删除'
    return jsonify(payload)


@warehouse_bp.route('/api/requests/pending', methods=['GET'])
def pending_requests():
    """查询未完成单据（仓库用）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    status = request.args.get('status')
    job_order = request.args.get('job_order')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 20))

    wb = WhereBuilder(["r.is_deleted = 0", "r.status NOT IN ('completed', 'rejected')"])

    # 站点过滤
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        wb.add(f"r.{site_filter}", *site_params)

    if status:
        wb.add("r.status = %s", status)
    if job_order:
        wb.add("EXISTS (SELECT 1 FROM kr_request_item WHERE request_id = r.id AND job_order LIKE %s)", f"%{job_order}%")

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
                ORDER BY r.request_time DESC LIMIT %s OFFSET %s""",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

    STATUS_LABELS = {
        'rejected': '已驳回',
        'pending_prep': '待备料', 'prepping': '备料中',
        'short': '缺料', 'ready_pickup': '待取料', 'completed': '已完成'
    }
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
