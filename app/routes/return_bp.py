"""
退料申请单路由（生产退料 → 仓库确认）。

流程：
  1. 生产（requester）扫卷标ID → POST /api/returns 创建退料单（request_type='return', status='pending_return'）
  2. 仓库在看板「待退料」泳道确认 → POST /api/returns/<id>/confirm
     卷标状态 在车间(in_shop) → 在库(in_stock)，剩余长度回写 coil_length
  3. 仓库驳回 → POST /api/returns/<id>/reject

剩余长度 = 卷标原始长度(coil_length) - 已消耗长度(consumption 表 out_length 汇总，
口径 = consumption 剪线消耗 + count_adjust 盘点调整；issue 出库 / scrap 报废不计入)
"""
import logging
import re
from datetime import datetime

from flask import Blueprint, request, jsonify, session, render_template

from app.config import Config
from app.models import get_db_connection, get_site_filter
from app.services.csi_interface import enqueue_return_qty_move
from app.services.unit_convert import convert_from_mm

logger = logging.getLogger(__name__)

return_bp = Blueprint('return', __name__)

COIL_ID_RE = re.compile(r'^\d{9}$')

RETURN_STATUS_LABELS = {
    'pending_return': '待退料',
    'confirmed': '已确认',
    'rejected': '已驳回',
}

# 退料明细复核状态 'removed'：车间继续使用该卷标，已移出退料清单（不退仓库）。
# 用状态标记而非物理删除 kr_return_item 行：删除会改变单内行号，
# 导致 enqueue_return_qty_move 计算出的 CSI 单号/traceid 漂移（见 services/csi_interface.py）。
RETURN_ITEM_REMOVED = 'removed'
REMOVED_REVIEW_NOTE = '车间继续使用'


def _coil_counting_locked(cur, coil_id):
    """盘点锁定检查：活跃盘点单（counting）中的卷标不允许退料/移出退料清单，
    避免破坏盘点一致性。"""
    cur.execute(
        "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
        "JOIN kr_inventory_count c ON c.id = i.count_id "
        "WHERE i.coil_id = %s AND c.status = 'counting'",
        (coil_id,)
    )
    return int((cur.fetchone() or {}).get('n') or 0) > 0


def _do_remove_return_item(cur, user, return_id, req_status, coil_id, now):
    """核心移除逻辑：把退料清单中的某个卷标标记为「车间继续使用」（review_status='removed'）。

    校验链：
      1. 卷标必须在该退料单的 kr_return_item 内
      2. review_status 必须是 NULL（已复核 confirmed/rejected 的不允许再移除）；
         已是 'removed' → 幂等返回成功（先判幂等，再判单据状态，
         因为移除最后一个未复核卷标后主表会闭环为 confirmed，重复移除仍应成功）
      3. 退料单状态必须是 pending_return（confirmed 时卷标已退回仓库，移除无意义）
      4. 卷标状态必须是 in_shop
      5. 卷标不在盘点锁定中

    副作用：只标记 kr_return_item；**不改** kr_wire_coil.status（保持 in_shop = 车间继续使用），
    也**不改** coil_length（剩余长度不变）。

    返回 (ok, message, http_status)。
    """
    cur.execute(
        "SELECT id, review_status FROM kr_return_item WHERE request_id = %s AND coil_id = %s",
        (return_id, coil_id)
    )
    item = cur.fetchone()
    if not item:
        return False, '该卷标不在退料清单中', 400
    if item['review_status'] == RETURN_ITEM_REMOVED:
        # 幂等：已移除直接返回成功，不报错
        return True, f'卷标 {coil_id} 已移出退料清单（车间继续使用）', 200
    if item['review_status'] is not None:
        return False, '该卷标已复核，无法移出退料清单', 400
    if req_status != 'pending_return':
        return False, '当前状态不允许移出退料清单', 400

    # 卷标必须在车间
    cur.execute(
        "SELECT coil_id, status FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
        (coil_id,)
    )
    c = cur.fetchone()
    if not c:
        return False, '卷标不存在', 404
    if c['status'] != 'in_shop':
        return False, f'卷标状态异常（{COIL_STATUS_LABEL(c["status"])}），无法移出退料清单', 400
    # 盘点锁定检查
    if _coil_counting_locked(cur, coil_id):
        return False, f'卷标 {coil_id} 正在盘点中（已锁定），不允许移出退料清单', 400

    # 标记移除：卷标保持 in_shop（车间继续使用），不回写剩余长度
    cur.execute(
        "UPDATE kr_return_item SET review_status = %s, review_note = %s, "
        "reviewed_by = %s, reviewed_at = %s WHERE request_id = %s AND coil_id = %s",
        (RETURN_ITEM_REMOVED, REMOVED_REVIEW_NOTE, user['username'], now, return_id, coil_id)
    )
    cur.execute(
        "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
        "VALUES (%s, %s, 'RETURN_ITEM_REMOVE', %s, %s, %s)",
        (return_id, user['username'],
         f"移出退料清单（车间继续使用）: 单 {return_id} 卷标 {coil_id}",
         request.remote_addr, now)
    )
    # 闭环：移除后若本单已无未复核明细（removed 不是 NULL，不计入）→ 主表确认。
    # 这样「移除」不会把退料单卡在 pending_return，申请人仍可签字完成整单。
    cur.execute(
        "SELECT COUNT(*) AS n FROM kr_return_item WHERE request_id = %s AND review_status IS NULL",
        (return_id,)
    )
    if int((cur.fetchone() or {}).get('n') or 0) == 0:
        cur.execute(
            "UPDATE kr_material_request SET status = 'confirmed', confirmed_by = %s, confirm_time = %s WHERE id = %s",
            (user['username'], now, return_id)
        )
    return True, f'卷标 {coil_id} 已移出退料清单（车间继续使用）', 200


def _coil_remain_length(cur, coil_id: str):
    """计算卷标剩余长度（与 coil_length 同单位，即系统单位如 M/FT）：
    初始长度(initial_length，优先；历史行为 coil_length) × 换算系数 - 已消耗(mm) → 原始单位。
    消耗表 out_length 固定为 mm（剪线/盘点/出库/报废登记），卷长存原始单位，
    相减前必须换算，否则单位不一致导致剩余为负、误判不可退料。
    剩余扣减口径统一为 consumption+count_adjust（剪线消耗+盘点库存调整）；
    issue（出库登记）、scrap（报废）不计入剩余扣减。

    基准取 initial_length：coil_length 在退料确认时会被回写为剩余长度，
    再用它减一次已消耗会把剩余量重复扣减。"""
    cur.execute(
        "SELECT coil_length, initial_length, unit FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
        (coil_id,)
    )
    row = cur.fetchone()
    if not row or row['coil_length'] is None:
        return 0.0
    cur.execute(
        "SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption "
        "WHERE coil_id = %s AND consume_type IN ('consumption','count_adjust')",
        (coil_id,)
    )
    used = float(cur.fetchone()['used'] or 0)
    unit = (row.get('unit') or '').strip().upper()
    factor = Config.UNIT_CONVERT_FACTOR.get(unit) if unit else None
    base = row['initial_length'] if row.get('initial_length') is not None else row['coil_length']
    base = float(base)
    used_orig = convert_from_mm(used, unit) if factor else used  # mm → 原始单位；单位未知按原值（兼容旧数据）
    return round(base - used_orig, 4)


# ================= 1. 创建退料申请单（生产发起） =================

@return_bp.route('/api/returns', methods=['POST'])
def create_return():
    """生产退料：输入卷标ID列表，自动带出物料号/单位/剩余长度"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    coil_ids = [str(x).strip() for x in (data.get('coil_ids') or []) if str(x).strip()]
    if not coil_ids:
        return jsonify({'success': False, 'message': '请至少输入一个卷标ID'}), 400
    if len(coil_ids) > 200:
        return jsonify({'success': False, 'message': '单次最多退料 200 卷'}), 400

    siteref = user.get('siteref', '')
    if not siteref:
        return jsonify({'success': False, 'message': '站点信息缺失'}), 400

    # 去重
    coil_ids = list(dict.fromkeys(coil_ids))
    errors = []
    items = []
    with get_db_connection() as db:
        cur = db.cursor()
        for cid in coil_ids:
            if not COIL_ID_RE.match(cid):
                errors.append(f'{cid}: 卷标ID格式无效')
                continue
            cur.execute(
                "SELECT coil_id, part_number, unit, status, coil_length FROM kr_wire_coil "
                "WHERE coil_id = %s AND is_deleted = 0 AND siteref = %s",
                (cid, siteref)
            )
            c = cur.fetchone()
            if not c:
                errors.append(f'{cid}: 卷标不存在')
                continue
            if c['status'] != 'in_shop':
                errors.append(f"{cid}: 当前状态为{COIL_STATUS_LABEL(c['status'])}，仅在车间的卷标可退料")
                continue
            remain = _coil_remain_length(cur, cid)
            if remain <= 0:
                errors.append(f'{cid}: 卷标剩余长度不足（已消耗完），无需退料')
                continue
            items.append({
                'coil_id': cid,
                'part_number': c['part_number'] or '',
                'unit': c['unit'] or '',
                'remain_length': remain,
            })
        if errors:
            cur.close()
            return jsonify({'success': False, 'message': '<br>'.join(errors[:10])}), 400
        if not items:
            cur.close()
            return jsonify({'success': False, 'message': '没有可退料的卷标'}), 400

        now = datetime.now()
        # 主表：复用 kr_material_request（request_type='return', status='pending_return'）
        cur.execute(
            "INSERT INTO kr_material_request (siteref, request_type, requester, request_time, status, remark) "
            "VALUES (%s, 'return', %s, %s, 'pending_return', %s)",
            (siteref, user['username'], now, (data.get('remark') or '').strip())
        )
        return_id = cur.lastrowid

        # 明细表：卷标维度
        cur.executemany(
            "INSERT INTO kr_return_item (request_id, coil_id, part_number, unit, remain_length) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(return_id, it['coil_id'], it['part_number'], it['unit'], it['remain_length']) for it in items]
        )
        # 操作日志
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_SUBMIT', %s, %s, %s)",
            (return_id, user['username'],
             f"提交退料申请: {len(items)} 卷",
             request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'id': return_id, 'message': f'退料申请已提交（{len(items)} 卷），等待仓库确认'})


# ================= 2. 仓库确认退料 =================

@return_bp.route('/api/returns/<int:return_id>/confirm', methods=['POST'])
def confirm_return(return_id):
    """仓库确认退料：卷标 在车间→在库，剩余长度回写 coil_length"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'pending_return':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许确认退料'}), 400
        # 站点校验
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        # 明细：排除 review_status='removed'（车间继续使用，不退仓库）
        # 说明：removed 行必须跳过，否则整单确认会把「车间继续使用」的卷标错误退回仓库。
        # 用 COALESCE 兜住 NULL（未复核）行，保证 NULL 行仍参与退回。
        cur.execute(
            "SELECT coil_id, remain_length, review_status FROM kr_return_item "
            "WHERE request_id = %s AND (review_status IS NULL OR review_status <> 'removed')",
            (return_id,)
        )
        items = cur.fetchall()
        now = datetime.now()

        confirmed = 0
        skipped = 0
        for it in items:
            # 卷标 在车间 → 在库，回写剩余长度
            # 条件 AND status='in_shop' 是有意保留的护栏：卷标已被其他流程改状态时不覆盖。
            # 但命中 0 行时绝不能静默跳过 —— coil_length 会停在登记值，外部系统按它取移库量
            # 就会取到脏值（261102001 事故：应为 217.7822，实取 1000）。故留 warning + skipped 计数。
            cur.execute(
                "UPDATE kr_wire_coil SET status = 'in_stock', coil_length = %s "
                "WHERE coil_id = %s AND status = 'in_shop'",
                (it['remain_length'], it['coil_id'])
            )
            if cur.rowcount:
                confirmed += cur.rowcount
            else:
                # FOR UPDATE = 当前读，能拿到并发事务最新提交的状态（普通 SELECT 在
                # REPEATABLE READ 下可能仍读到本事务快照里的旧值，日志就没意义了）
                cur.execute(
                    "SELECT status FROM kr_wire_coil WHERE coil_id = %s FOR UPDATE",
                    (it['coil_id'],)
                )
                row = cur.fetchone()
                actual_status = row['status'] if row else '不存在/已删除'
                logger.warning(
                    '退料整单确认回写跳过(未命中 in_shop): request_id=%s coil_id=%s '
                    'remain_length=%s 期望状态=in_shop 实际状态=%s',
                    return_id, it['coil_id'], it['remain_length'], actual_status
                )
                skipped += 1
            # 同步明细复核状态：整单确认的卷标标记为 confirmed（removed 行不在此处）
            if it['review_status'] is None:
                cur.execute(
                    "UPDATE kr_return_item SET review_status = 'confirmed', "
                    "reviewed_by = %s, reviewed_at = %s WHERE request_id = %s AND coil_id = %s",
                    (user['username'], now, return_id, it['coil_id'])
                )

        cur.execute(
            "UPDATE kr_material_request SET status = 'confirmed', confirmed_by = %s, confirm_time = %s WHERE id = %s",
            (user['username'], now, return_id)
        )
        detail = f"确认退料: {confirmed} 卷转回在库"
        if skipped:
            detail += f"，{skipped} 卷回写失败(卷标状态非 in_shop，coil_length 未更新)"
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_CONFIRM', %s, %s, %s)",
            (return_id, user['username'], detail, request.remote_addr, now)
        )
        db.commit()
        cur.close()

    message = f'退料确认成功，{confirmed} 卷已转回在库'
    if skipped:
        message += f'；{skipped} 卷因状态异常未回写剩余长度，请检查卷标状态'
    return jsonify({'success': True, 'message': message, 'confirmed': confirmed, 'skipped': skipped})


# ================= 2.1 移出退料清单（车间继续使用） =================

@return_bp.route('/api/returns/<int:return_id>/items/<coil_id>/remove', methods=['POST'])
def remove_return_item(return_id, coil_id):
    """退料单详情页：把某个卷标移出退料清单（车间继续使用，不退仓库）。

    允许 requester / warehouse / admin；站点隔离：非 admin 只能操作本单据站点。
    幂等：已是 removed 的卷标重复移除直接返回成功。
    """
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    coil_id = str(coil_id).strip()
    now = datetime.now()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        # 站点隔离：admin 跨站，其余必须与单据站点一致
        site_filter, site_params = get_site_filter(user)
        if site_filter and site_params and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        ok, message, code = _do_remove_return_item(
            cur, user, return_id, req['status'], coil_id, now
        )
        if not ok:
            cur.close()
            return jsonify({'success': False, 'message': message}), code
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': message})


# ================= 3. 仓库驳回退料 =================

@return_bp.route('/api/returns/<int:return_id>/reject', methods=['POST'])
def reject_return(return_id):
    """仓库驳回退料单"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'pending_return':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许驳回'}), 400
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        now = datetime.now()
        cur.execute(
            "UPDATE kr_material_request SET status = 'rejected', reject_reason = %s WHERE id = %s",
            (reason or '仓库驳回', return_id)
        )
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_REJECT', %s, %s, %s)",
            (return_id, user['username'], f"驳回退料: {reason}", request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '退料单已驳回'})


# ================= 4. 卷标信息查询（退料页带出用） =================

@return_bp.route('/api/returns/coil-info/<coil_id>', methods=['GET'])
def coil_info(coil_id):
    """按卷标ID查询：物料号/单位/状态/剩余长度（退料页扫码带出）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT coil_id, part_number, unit, status, coil_length, request_id FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        c = cur.fetchone()
        if not c:
            cur.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        # 站点隔离
        site_filter, site_params = get_site_filter(user)
        if site_filter and site_params:
            cur.execute("SELECT siteref FROM kr_material_request WHERE id = %s", (c['request_id'],))
            rq = cur.fetchone()
            if rq and rq['siteref'] != site_params[0]:
                cur.close()
                return jsonify({'success': False, 'message': '无权查看其他站点的卷标'}), 403
        remain = _coil_remain_length(cur, coil_id)
        cur.close()

    can_return = c['status'] == 'in_shop' and remain > 0
    if c['status'] != 'in_shop':
        reason = f'仅状态为「在车间」的卷标可退料（当前：{COIL_STATUS_LABEL(c["status"])}）'
    elif remain <= 0:
        reason = f'卷标剩余长度不足（当前剩余 {remain} {(c["unit"] or "").strip()}），无法退料'
    else:
        reason = ''

    return jsonify({
        'success': True,
        'data': {
            'coil_id': c['coil_id'],
            'part_number': c['part_number'] or '',
            'unit': c['unit'] or '',
            'status': c['status'],
            'status_label': COIL_STATUS_LABEL(c['status']),
            'remain_length': remain,
            'can_return': can_return,
            'reason': reason,
        }
    })


def COIL_STATUS_LABEL(status):
    labels = {'in_stock': '在库', 'in_shop': '在车间',
              'issued': '已出库', 'scrapped': '报废'}
    return labels.get(status, status or '')


# ================= 5. 退料复核（手机端，仅仓库人员） =================

@return_bp.route('/return/review', methods=['GET'])
def return_review_page():
    """退料复核页面：扫码卷标ID → 匹配待退料单 → 逐卷选库位退回 / 不匹配维护原因"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403
    return render_template('return_review.html')


@return_bp.route('/api/returns/review/coil/<coil_id>', methods=['GET'])
def review_coil_query(coil_id):
    """扫码查询：卷标信息 + 是否匹配待退料单 + 可用库位（CSI 非 Floor 库位）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    coil_id = str(coil_id).strip()
    result = {
        'coil_id': coil_id,
        'part_number': '',
        'lot': '',
        'unit': '',
        'status': None,
        'status_label': '',
        'remain_length': 0.0,
        'matched': False,
        'return_id': None,
        'review_status': None,
        'available_locations': [],
        'lot_history_locations': [],
        'location_hints': {},
    }

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT coil_id, part_number, lot_no, unit, status, coil_length, siteref "
            "FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        c = cur.fetchone()
        if not c:
            cur.close()
            result['success'] = True
            return jsonify(result)

        # 站点隔离
        site_filter, site_params = get_site_filter(user)
        if site_filter and site_params and c['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权查看其他站点的卷标'}), 403

        result['part_number'] = c['part_number'] or ''
        result['lot'] = c['lot_no'] or ''
        result['unit'] = c['unit'] or ''
        result['status'] = c['status']
        result['status_label'] = COIL_STATUS_LABEL(c['status'])
        result['remain_length'] = _coil_remain_length(cur, coil_id)

        # 匹配 pending_return 退料单（多个取最新）
        cur.execute(
            "SELECT ri.request_id, ri.review_status FROM kr_return_item ri "
            "JOIN kr_material_request r ON r.id = ri.request_id "
            "WHERE ri.coil_id = %s AND r.request_type = 'return' AND r.status = 'pending_return' "
            "ORDER BY r.id DESC LIMIT 1",
            (coil_id,)
        )
        match = cur.fetchone()
        if match:
            result['matched'] = True
            result['return_id'] = match['request_id']
            result['review_status'] = match['review_status']
        else:
            # 已被标记为「车间继续使用」(removed) 的卷标：即使所在单已闭环为 confirmed，
            # 也要让前端知道它不在退料清单里（不可再复核退回）。
            cur.execute(
                "SELECT ri.request_id FROM kr_return_item ri "
                "JOIN kr_material_request r ON r.id = ri.request_id "
                "WHERE ri.coil_id = %s AND r.request_type = 'return' AND ri.review_status = %s "
                "ORDER BY r.id DESC LIMIT 1",
                (coil_id, RETURN_ITEM_REMOVED)
            )
            done = cur.fetchone()
            if done:
                result['review_status'] = RETURN_ITEM_REMOVED
                result['return_id'] = done['request_id']
        coil_siteref = c['siteref']

        # 该 Lot 的历史退入库位：同物料同批号、从车间线边（Floor*）移出到仓库库位的
        # 已过账记录（status=SUCCESS），按最近使用时间倒序。
        # 注意：不按 direction 过滤——实测库里既有 F 也有 B，方向字段不能区分
        # 「备料出库」与「退料入库」，用 from_location 是否含 floor 判断才可靠。
        # 查询失败不影响主流程（历史库位为空，其余照常）。
        lot_history_locations = []
        if result['part_number'] and result['lot']:
            try:
                cur.execute(
                    "SELECT to_location, MAX(createdate) AS last_at "
                    "FROM kr_csi_interface "
                    "WHERE item = %s AND lot = %s AND api_name = 'sl-qty-move' "
                    "  AND status = 'SUCCESS' "
                    "  AND from_location IS NOT NULL AND LOWER(from_location) LIKE %s "
                    "  AND to_location IS NOT NULL AND TRIM(to_location) <> '' "
                    "  AND LOWER(to_location) NOT LIKE %s "
                    "GROUP BY to_location "
                    "ORDER BY last_at DESC",
                    (result['part_number'], result['lot'], '%floor%', '%floor%')
                )
                for row in cur.fetchall():
                    loc = str(row['to_location'] or '').strip()
                    if loc and loc not in lot_history_locations:
                        lot_history_locations.append(loc)
            except Exception:
                lot_history_locations = []

        cur.close()

    # 库位下拉：lot 历史退入库位（「上次退此处」）在前，同物料现有非 floor 库位在后。
    # 仅匹配到待退料单时才有意义；失败/为空则前端降级为 Stock（保持原逻辑）。
    if result['matched'] and result['part_number']:
        available = list(lot_history_locations)
        hints = {loc: 'last_return' for loc in lot_history_locations}
        try:
            from app.services.csi_service import CSIClient
            client = CSIClient(siteref=coil_siteref)
            rows = client.get_inventory(result['part_number'])
            for r in rows:
                loc = (r.get('Loc') or '').strip()
                if not loc or 'floor' in loc.lower():
                    continue
                if loc not in available:
                    available.append(loc)
                hints.setdefault(loc, 'item_stock')
        except Exception:
            pass
        result['lot_history_locations'] = lot_history_locations
        result['available_locations'] = available
        result['location_hints'] = hints

    result['success'] = True
    return jsonify(result)


@return_bp.route('/api/returns/review/<coil_id>/confirm', methods=['POST'])
def review_coil_confirm(coil_id):
    """复核通过：卷标退回（in_shop→in_stock，回写剩余长度）+ 逐卷库位"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    return_id = data.get('return_id')
    location = (data.get('location') or '').strip() or 'Stock'
    coil_id = str(coil_id).strip()

    if not return_id:
        return jsonify({'success': False, 'message': '缺少退料单号'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'pending_return':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许确认退料'}), 400
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        # 卷标存在且状态 in_shop
        cur.execute(
            "SELECT coil_id, status FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        c = cur.fetchone()
        if not c:
            cur.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        if c['status'] != 'in_shop':
            cur.close()
            return jsonify({'success': False, 'message': '卷标状态异常，无法退回'}), 400
        # 盘点锁定检查：正在盘点中（活跃盘点单 counting）的卷标不允许退料，避免破坏盘点一致性
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
            "JOIN kr_inventory_count c ON c.id = i.count_id "
            "WHERE i.coil_id = %s AND c.status = 'counting'",
            (coil_id,)
        )
        if cur.fetchone()['n'] > 0:
            cur.close()
            return jsonify({'success': False, 'message': f'卷标 {coil_id} 正在盘点中（已锁定），不允许退料'}), 400
        # 卷标属于该退料单
        cur.execute(
            "SELECT id, review_status FROM kr_return_item WHERE request_id = %s AND coil_id = %s",
            (return_id, coil_id)
        )
        item = cur.fetchone()
        if not item:
            cur.close()
            return jsonify({'success': False, 'message': '该卷标不在退料清单中'}), 400
        # 已被移出退料清单（车间继续使用）的卷标不允许复核退回
        if item['review_status'] == RETURN_ITEM_REMOVED:
            cur.close()
            return jsonify({'success': False, 'message': '该卷标已被移出退料清单（车间继续使用），无法复核'}), 400

        remain = _coil_remain_length(cur, coil_id)
        now = datetime.now()
        # 卷标退回 + 逐卷库位 + CSI 移库交易必须同一事务：
        # 任一步失败整体回滚，避免「退了但没记账」的半状态。
        try:
            cur.execute(
                "UPDATE kr_wire_coil SET status = 'in_stock', coil_length = %s, loc = %s "
                "WHERE coil_id = %s AND status = 'in_shop'",
                (remain, location, coil_id)
            )
            # 同整单确认：条件未命中说明卷标状态在「校验→回写」之间被并发改走，
            # 此时 coil_length / loc 都不会更新（上面校验过的 in_shop 已过期），必须留痕。
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT status FROM kr_wire_coil WHERE coil_id = %s FOR UPDATE",
                    (coil_id,)
                )
                row = cur.fetchone()
                actual_status = row['status'] if row else '不存在/已删除'
                logger.warning(
                    '退料复核回写跳过(未命中 in_shop): return_id=%s coil_id=%s remain_length=%s '
                    '期望状态=in_shop 实际状态=%s',
                    return_id, coil_id, remain, actual_status
                )
            cur.execute(
                "UPDATE kr_return_item SET review_status = 'confirmed', location = %s, "
                "reviewed_by = %s, reviewed_at = %s WHERE request_id = %s AND coil_id = %s",
                (location, user['username'], now, return_id, coil_id)
            )
            # 移库交易：Floor-Core → 所选退回库位，单号 = 退料单号 + 单内行号
            enqueue_return_qty_move(
                cur, req, coil_id, remain, location, user['username'], now
            )
            cur.execute(
                "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
                "VALUES (%s, %s, 'RETURN_REVIEW_CONFIRM', %s, %s, %s)",
                (return_id, user['username'], f"复核退回卷标 {coil_id} → 库位 {location}",
                 request.remote_addr, now)
            )

            # 闭环：该退料单已无未复核明细 → 主表确认
            cur.execute(
                "SELECT COUNT(*) AS n FROM kr_return_item WHERE request_id = %s AND review_status IS NULL",
                (return_id,)
            )
            if cur.fetchone()['n'] == 0:
                cur.execute(
                    "UPDATE kr_material_request SET status = 'confirmed', confirmed_by = %s, confirm_time = %s WHERE id = %s",
                    (user['username'], now, return_id)
                )
            db.commit()
        except Exception as e:
            db.rollback()
            cur.close()
            logger.exception('退料复核确认失败（已回滚）: return_id=%s coil_id=%s', return_id, coil_id)
            return jsonify({
                'success': False,
                'message': f'退料成功但移库交易写入失败，请重试/联系管理员: {e}'
            }), 500
        cur.close()

    return jsonify({'success': True, 'message': f'卷标 {coil_id} 已退回至 {location}'})


@return_bp.route('/api/returns/review/<coil_id>/reject', methods=['POST'])
def review_coil_reject(coil_id):
    """不匹配：维护原因不予退料，卷标状态保持不变"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    coil_id = str(coil_id).strip()
    if not reason:
        return jsonify({'success': False, 'message': '请填写不予退料原因'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT coil_id, siteref FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        c = cur.fetchone()
        if not c:
            cur.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404

        now = datetime.now()
        return_id = None
        # 该卷标最近一条退料明细若已是 removed（车间继续使用）→ 不允许再登记复核。
        # 取 id 最大的一条：若卷标被移除后又被重新提交到新的退料单，新行 id 更大且未复核，不受影响。
        cur.execute(
            "SELECT ri.request_id, ri.review_status FROM kr_return_item ri "
            "JOIN kr_material_request r ON r.id = ri.request_id "
            "WHERE ri.coil_id = %s AND r.request_type = 'return' "
            "ORDER BY ri.id DESC LIMIT 1",
            (coil_id,)
        )
        latest = cur.fetchone()
        if latest and latest['review_status'] == RETURN_ITEM_REMOVED:
            cur.close()
            return jsonify({'success': False, 'message': '该卷标已被移出退料清单（车间继续使用），无法复核'}), 400
        # 匹配 pending_return 单（若有则写明细复核状态 + 闭环检查）
        cur.execute(
            "SELECT ri.request_id, ri.review_status FROM kr_return_item ri "
            "JOIN kr_material_request r ON r.id = ri.request_id "
            "WHERE ri.coil_id = %s AND r.request_type = 'return' AND r.status = 'pending_return' "
            "ORDER BY r.id DESC LIMIT 1",
            (coil_id,)
        )
        m = cur.fetchone()
        if m:
            # 该 pending_return 单内的明细若已被移出清单 → 不允许再登记不予退料
            if m['review_status'] == RETURN_ITEM_REMOVED:
                cur.close()
                return jsonify({'success': False, 'message': '该卷标已被移出退料清单（车间继续使用），无法复核'}), 400
            return_id = m['request_id']
            site_filter, site_params = get_site_filter(user)
            if site_filter and c['siteref'] != site_params[0]:
                cur.close()
                return jsonify({'success': False, 'message': '无权操作其他站点的卷标'}), 403
            cur.execute(
                "UPDATE kr_return_item SET review_status = 'rejected', review_note = %s, "
                "reviewed_by = %s, reviewed_at = %s WHERE request_id = %s AND coil_id = %s",
                (reason, user['username'], now, return_id, coil_id)
            )
            cur.execute(
                "SELECT COUNT(*) AS n FROM kr_return_item WHERE request_id = %s AND review_status IS NULL",
                (return_id,)
            )
            if cur.fetchone()['n'] == 0:
                cur.execute(
                    "UPDATE kr_material_request SET status = 'confirmed', confirmed_by = %s, confirm_time = %s WHERE id = %s",
                    (user['username'], now, return_id)
                )

        # 操作日志（匹配或未匹配均记）
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_REVIEW_REJECT', %s, %s, %s)",
            (return_id, user['username'], f"不予退料卷标 {coil_id}: {reason}",
             request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '已记录不予退料，卷标状态保持不变'})


@return_bp.route('/api/returns/review/<coil_id>/remove', methods=['POST'])
def review_coil_remove(coil_id):
    """仓库复核页：扫描卷标ID后把该卷标移出退料清单（车间继续使用，不退仓库）。

    退料单从「卷标是否命中 pending_return 退料单」逻辑取（同 review_coil_query）。
    权限：warehouse / admin（与同页 confirm/reject 一致）。
    幂等：已是 removed 的卷标重复移除直接返回成功。
    """
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    coil_id = str(coil_id).strip()
    now = datetime.now()
    with get_db_connection() as db:
        cur = db.cursor()
        # 匹配 pending_return 退料单（多个取最新）
        cur.execute(
            "SELECT ri.request_id, ri.review_status FROM kr_return_item ri "
            "JOIN kr_material_request r ON r.id = ri.request_id "
            "WHERE ri.coil_id = %s AND r.request_type = 'return' AND r.status = 'pending_return' "
            "ORDER BY r.id DESC LIMIT 1",
            (coil_id,)
        )
        match = cur.fetchone()
        if not match:
            # 幂等兜底：卷标已在某退料单中被标记为 removed（该单可能已闭环为 confirmed）→ 视为成功
            cur.execute(
                "SELECT ri.request_id, ri.review_status FROM kr_return_item ri "
                "JOIN kr_material_request r ON r.id = ri.request_id "
                "WHERE ri.coil_id = %s AND r.request_type = 'return' AND ri.review_status = %s "
                "ORDER BY r.id DESC LIMIT 1",
                (coil_id, RETURN_ITEM_REMOVED)
            )
            done = cur.fetchone()
            if done:
                cur.close()
                return jsonify({'success': True,
                                'message': f'卷标 {coil_id} 已移出退料清单（车间继续使用）'})
            cur.close()
            return jsonify({'success': False, 'message': '未匹配到待退料单，或卷标不存在'}), 400

        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (match['request_id'],)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        site_filter, site_params = get_site_filter(user)
        if site_filter and site_params and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        ok, message, code = _do_remove_return_item(
            cur, user, req['id'], req['status'], coil_id, now
        )
        if not ok:
            cur.close()
            return jsonify({'success': False, 'message': message}), code
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': message})


# ================= 6. 退料明细维护原因 + 申请人签字确认（详情页） =================

@return_bp.route('/api/returns/<int:return_id>/note', methods=['PUT'])
def update_return_note(return_id):
    """维护退料明细异常原因（仅非已复核行可维护，note 允许空串清空）"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    coil_id = str(data.get('coil_id') or '').strip()
    note = data.get('note')
    note = str(note).strip() if note is not None else ''

    if not coil_id:
        return jsonify({'success': False, 'message': '缺少卷标ID'}), 400

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        cur.execute(
            "SELECT review_status FROM kr_return_item WHERE request_id = %s AND coil_id = %s",
            (return_id, coil_id)
        )
        item = cur.fetchone()
        if not item:
            cur.close()
            return jsonify({'success': False, 'message': '该卷标不在退料清单中'}), 400
        if item['review_status'] == 'confirmed':
            cur.close()
            return jsonify({'success': False, 'message': '已复核的卷标不可维护异常原因'}), 400

        now = datetime.now()
        cur.execute(
            "UPDATE kr_return_item SET review_note = %s WHERE request_id = %s AND coil_id = %s",
            (note, return_id, coil_id)
        )
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_NOTE_UPDATE', %s, %s, %s)",
            (return_id, user['username'], f"维护退料明细 {coil_id} 异常原因: {note or '(清空)'}",
             request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '维护原因已保存'})


@return_bp.route('/api/returns/<int:return_id>/sign', methods=['POST'])
def sign_return(return_id):
    """申请人签字确认退料交接：confirmed → completed"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('requester', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    signature_data = data.get('signature_data') or ''

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
            (return_id,)
        )
        req = cur.fetchone()
        if not req:
            cur.close()
            return jsonify({'success': False, 'message': '退料单不存在'}), 404
        if req['request_type'] != 'return':
            cur.close()
            return jsonify({'success': False, 'message': '该单据不是退料单'}), 400
        if req['status'] != 'confirmed':
            cur.close()
            return jsonify({'success': False, 'message': '当前状态不允许签字确认'}), 400
        site_filter, site_params = get_site_filter(user)
        if site_filter and req['siteref'] != site_params[0]:
            cur.close()
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403

        # 前置：每行卷标须为 confirmed，或 rejected 且已填写异常原因，否则不允许签字
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_return_item WHERE request_id = %s AND review_status IS NULL",
            (return_id,)
        )
        if cur.fetchone()['n'] > 0:
            cur.close()
            return jsonify({'success': False, 'message': '退料清单尚有未复核卷标，暂不能签字确认'}), 400
        cur.execute(
            "SELECT COUNT(*) AS n FROM kr_return_item WHERE request_id = %s "
            "AND review_status = 'rejected' AND (review_note IS NULL OR TRIM(review_note) = '')",
            (return_id,)
        )
        if cur.fetchone()['n'] > 0:
            cur.close()
            return jsonify({'success': False, 'message': '存在已登记不予退料但未填写异常原因的卷标，请先维护原因'}), 400

        now = datetime.now()
        cur.execute(
            "UPDATE kr_material_request SET status = 'completed', signer = %s, sign_time = %s, signature_data = %s WHERE id = %s",
            (user['username'], now, signature_data, return_id)
        )
        cur.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'RETURN_SIGN', %s, %s, %s)",
            (return_id, user['username'], '申请人签字确认退料交接', request.remote_addr, now)
        )
        db.commit()
        cur.close()

    return jsonify({'success': True, 'message': '退料交接已签字确认'})
