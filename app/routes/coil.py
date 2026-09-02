"""
线卷全库存管理 API（Wire Coil Inventory）

覆盖接口：
  1. GET  /api/coils/next-id                         卷号生成（YYMMDD+3位流水）
  2. POST /api/requests/<request_id>/coils           卷标信息批量录入（支持 item_id 按申请单行绑定）
  3. GET  /api/requests/<request_id>/coils           申请单卷标列表（支持 ?item_id= 按行过滤）
  4. GET  /api/coils                                 线卷库存列表查询（多条件+分页）
  5. GET  /api/requests/<request_id>/coil-units      批量获取申请单物料单位（CSI，只读）
  6. POST /api/coils/print                           标签打印（批量）
  7. GET  /api/coils/<coil_id>/label                 单卷标签渲染数据（预览）
  8. POST /api/requests/<request_id>/consumption     出库消耗登记（issue）
  9. GET  /api/requests/<request_id>/consumption     申请单消耗记录查询
 10. DELETE /api/coils/<id>                          删除卷标（仅本申请单 + warehouse/admin）

兼容别名（开发任务清单约定）：
  - POST /api/requests/<request_id>/coil-number      卷号生成（POST 形式）
  - POST /api/coils/units                            批量获取物料单位（POST 形式）
  - POST /api/requests/<request_id>/coils/print      打印（申请单维度）
  - GET  /api/requests/<request_id>/coils/preview    申请单卷标预览数据
"""
import re
import time
from datetime import datetime

from flask import Blueprint, request, jsonify, session
from pymysql.err import IntegrityError

from app.config import Config
from app.models import get_db_connection, get_site_filter
from app.services.csi_service import CSIClient
from app.services import label_print_service

coil_bp = Blueprint('coil', __name__)

COIL_ID_RE = re.compile(r'^\d{9}$')
DAILY_LIMIT = 999
MAX_BATCH = 500

COIL_STATUS_LABELS = {
    'in_stock': '在库',
    'in_shop': '在车间',
    'issued': '已出库',
    'scrapped': '报废',
}

# ================= 出库登记宽表字段定义（文档 2.3，宽表存储线材加工过程参数） =================
# field -> (type, allowed_values or None)
#   type: 'text' 文本 / 'num' 小数 / 'int' 整数 / 'enum' 枚举（R12 取值校验）
CONSUMPTION_EXTRA_FIELDS = {
    # ---- 基础/线材组 ----
    'job_part_number': ('text', None),
    'shear_qty': ('int', None),
    'shear_length': ('num', None),
    'length_tolerance': ('num', None),
    'shear_equipment': ('text', None),
    'actual_shear_equipment': ('text', None),
}


def _convert_length(out_length: float, unit):
    """单位换算（服务端计算并覆盖，不信任前端传值，文档 R11 / 2.3.1）：
    converted_length = out_length(mm) ÷ 系数；系数按 CSI 单位查 Config.UNIT_CONVERT_FACTOR。
    单位为空或未收录时返回 (None, None)。"""
    unit = (unit or '').strip()
    factor = Config.UNIT_CONVERT_FACTOR.get(unit.upper()) if unit else None
    if not factor:
        return None, None
    return round(out_length / factor, 2), unit


def _parse_extra_fields(it: dict, row_no: int):
    """解析出库登记宽表字段，返回 (values_dict, error_message_or_None)。
    空值统一转 None；类型不符 / 枚举越界返回可读错误（R12）。"""
    values = {}
    for field, (ftype, allowed) in CONSUMPTION_EXTRA_FIELDS.items():
        raw = it.get(field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            values[field] = None
            continue
        if ftype == 'text':
            values[field] = str(raw).strip()
        elif ftype == 'num':
            try:
                values[field] = round(float(raw), 2)
            except (TypeError, ValueError):
                return None, f'第{row_no}行 {field} 必须为数值'
        elif ftype == 'int':
            try:
                values[field] = int(float(raw))
            except (TypeError, ValueError):
                return None, f'第{row_no}行 {field} 必须为整数'
        elif ftype == 'enum':
            val = str(raw).strip()
            if val not in allowed:
                return None, f'第{row_no}行 {field} 取值必须为 {" / ".join(allowed)}'
            values[field] = val
    return values, None

# 进程内单位缓存 / 共享 CSI 客户端见 _get_unit_cached 处定义


# ================= 公共辅助 =================

def _check_warehouse_or_admin():
    """仓库/管理员权限校验，返回 (user, error_response)"""
    user = session.get('user')
    if not user:
        return None, jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return None, jsonify({'success': False, 'message': '权限不足'}), 403
    return user, None, None


def _get_request(cursor, request_id):
    cursor.execute(
        "SELECT * FROM kr_material_request WHERE id = %s AND is_deleted = 0",
        (request_id,)
    )
    return cursor.fetchone()


def _check_site(req, user):
    """站点隔离：非 admin 仅能操作本站点。返回 None 或 error_response"""
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        if req['siteref'] != site_params[0]:
            return jsonify({'success': False, 'message': '无权操作其他站点的单据'}), 403
    return None


def _add_log(cursor, request_id, operator, action, detail, ip):
    cursor.execute(
        "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (request_id, operator, action, detail, ip, datetime.now())
    )


def _coil_to_dict(row):
    d = dict(row)
    d['status_label'] = COIL_STATUS_LABELS.get(d.get('status'), d.get('status'))
    d['coil_length'] = float(d['coil_length']) if d.get('coil_length') is not None else None
    d['created_at'] = str(d['created_at']) if d.get('created_at') else None
    return d


def _gen_next_id(cursor, d: datetime) -> str:
    """按当天计数生成下一可用卷号；当日 >=999 抛 ValueError。

    使用当前读（SELECT ... FOR UPDATE）：REPEATABLE READ 下一致性快照读看不到
    并发会话已提交的新数据，冲突后重试会取回同一卷号；当前读锁定当日前缀记录，
    保证并发取号串行且能看到最新已提交数据（P2-1）。
    """
    prefix = d.strftime('%y%m%d')
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM kr_wire_coil WHERE coil_id LIKE %s AND is_deleted = 0 FOR UPDATE",
        (prefix + '%',)
    )
    cnt = cursor.fetchone()['cnt']
    if cnt >= DAILY_LIMIT:
        raise ValueError(f'当日卷号已用完（每天最多{DAILY_LIMIT}卷）')
    return f"{prefix}{cnt + 1:03d}"


# 进程内单位缓存：{(siteref, part_number): (unit, fetched_at)}，TTL 30 分钟
_unit_cache = {}
_UNIT_TTL = 30 * 60
# 进程内共享 CSIClient 实例（按 siteref）：复用 OAuth2 token，避免每次 new 重复获取
_csi_clients = {}


def _get_unit_cached(siteref: str, part_number: str):
    """进程内缓存 + CSI 查询物料单位（共享 CSIClient 实例）；失败返回 None（不阻断录入）"""
    key = (siteref, part_number)
    now = time.time()
    cached = _unit_cache.get(key)
    if cached and now - cached[1] < _UNIT_TTL:
        return cached[0]
    try:
        client = _csi_clients.get(siteref)
        if client is None:
            client = CSIClient(siteref=siteref)
            _csi_clients[siteref] = client
        unit = client.get_item_unit(part_number)
    except Exception as e:
        print(f"[COIL] get_item_unit error for {part_number}: {e}")
        unit = None
    _unit_cache[key] = (unit, now)
    return unit


# ================= 1. 卷号生成 =================

@coil_bp.route('/api/coils/next-id', methods=['GET'])
def next_coil_id():
    # 卷号生成泄露每日卷号用量，仅限 warehouse/admin（与录入权限一致，P2-5）
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    date_str = (request.args.get('date') or '').strip()
    if date_str:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'message': '日期格式应为 YYYY-MM-DD'}), 400
    else:
        d = datetime.now()

    with get_db_connection() as db:
        cursor = db.cursor()
        try:
            coil_id = _gen_next_id(cursor, d)
        except ValueError as e:
            cursor.close()
            return jsonify({'success': False, 'message': str(e)}), 400
        cursor.close()

    return jsonify({
        'success': True,
        'data': {
            'coil_id': coil_id,
            'date': d.strftime('%Y-%m-%d'),
            'date_prefix': d.strftime('%y%m%d'),
            'seq': int(coil_id[-3:]),
            'daily_count': int(coil_id[-3:]) - 1,
            'daily_limit': DAILY_LIMIT,
        }
    })


@coil_bp.route('/api/coils/validate-lot', methods=['POST'])
def validate_lot():
    """卷标 Lot 验证：连接 Infor IDO SLLots，按 Item 过滤验证 Lot 号；
    输入长度（mm）与 DerQtyOnHand（×换算系数 → mm）对比校验。"""
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    part_number = str(data.get('part_number') or '').strip()
    lot_no = str(data.get('lot_no') or '').strip()
    length_raw = data.get('length')

    if not part_number:
        return jsonify({'success': False, 'message': '缺少物料号'}), 400
    if not lot_no:
        return jsonify({'success': False, 'message': '缺少 Lot 号'}), 400

    length = None
    if length_raw not in (None, ''):
        try:
            length = float(length_raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': '长度格式无效'}), 400

    siteref = (user.get('siteref') or '310').strip()
    client = CSIClient(siteref)

    # 1. SLLots 验证 Lot（按 Item 过滤）
    lot_info = client.get_lot_info(part_number, lot_no)
    if lot_info is None:
        return jsonify({
            'success': False,
            'message': f'Lot {lot_no} 不存在或查询失败（SLLots，Item={part_number}）',
            'lot_exists': False,
        }), 400

    der_qty = lot_info['der_qty_on_hand']
    # 2. 长度对比：录入长度为「原始单位」（与系统单位一致，如 M/FT），
    #    直接与 DerQtyOnHand（同单位）比较，不做 mm 换算
    unit = (data.get('unit') or '').strip().upper()

    result = {
        'success': True,
        'lot_exists': True,
        'lot': lot_info['lot'],
        'item': lot_info['item'],
        'lot_status': lot_info['lot_status'],
        'whse': lot_info['whse'],
        'der_qty_on_hand': der_qty,
        'unit': unit,
    }

    # 3. 长度校验（输入为系统单位原始值，直接与 DerQtyOnHand 比较）
    if length is not None:
        if length > der_qty:
            result['success'] = False
            result['message'] = (f'长度 {length:g}{unit or ""} 超过 Lot {lot_no} 可用数量 '
                                 f'{der_qty:g}{unit or ""}（DerQtyOnHand）')
            return jsonify(result), 400

    result['message'] = f'Lot {lot_no} 验证通过，可用数量 {der_qty:g}{unit or ""}' if unit else f'Lot {lot_no} 验证通过'
    return jsonify(result)


@coil_bp.route('/api/requests/<int:request_id>/coil-number', methods=['POST'])
def coil_number_alias(request_id):
    """卷号生成（POST 形式，开发任务清单约定）；仅限 warehouse/admin（P2-5）"""
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code
    data = request.get_json() or {}
    date_str = (data.get('date') or request.args.get('date') or '').strip()
    if date_str:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'message': '日期格式应为 YYYY-MM-DD'}), 400
    else:
        d = datetime.now()
    with get_db_connection() as db:
        cursor = db.cursor()
        try:
            coil_id = _gen_next_id(cursor, d)
        except ValueError as e:
            cursor.close()
            return jsonify({'success': False, 'message': str(e)}), 400
        cursor.close()
    return jsonify({
        'success': True,
        'data': {
            'coil_id': coil_id,
            'date': d.strftime('%Y-%m-%d'),
            'date_prefix': d.strftime('%y%m%d'),
            'seq': int(coil_id[-3:]),
            'daily_count': int(coil_id[-3:]) - 1,
            'daily_limit': DAILY_LIMIT,
        }
    })


# ================= 2. 卷标信息批量录入 =================

@coil_bp.route('/api/requests/<int:request_id>/coils', methods=['POST'])
def create_coils(request_id):
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'success': False, 'message': '请至少录入一行卷标'}), 400
    if len(items) > MAX_BATCH:
        return jsonify({'success': False, 'message': f'单次最多录入 {MAX_BATCH} 行'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()

        # 1. 申请单校验：minpack + prepping + 站点
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if (req.get('request_type') or '') != 'minpack':
            cursor.close()
            return jsonify({'success': False, 'message': '仅最小包装（minpack）申请单支持卷标录入'}), 400
        if req['status'] != 'prepping':
            cursor.close()
            return jsonify({'success': False, 'message': '仅备料中（prepping）状态的申请单可录入卷标'}), 400

        # 2. 该申请单的物料集合（用于 R6 校验）
        cursor.execute(
            "SELECT part_number FROM kr_request_item WHERE request_id = %s",
            (request_id,)
        )
        req_part_rows = cursor.fetchall()
        req_parts = set(r['part_number'] for r in req_part_rows)
        if not req_parts:
            cursor.close()
            return jsonify({'success': False, 'message': '申请单没有物料明细，无法录入卷标'}), 400

        # 3. 逐行基础校验（R1/R5/R6，item_id 按申请单行绑定）
        parsed = []
        submitted_parts = set()
        for i, it in enumerate(items):
            part_number = str(it.get('part_number') or '').strip()
            coil_id = str(it.get('coil_id') or '').strip()
            item_id_raw = it.get('item_id')
            item_id = None
            if item_id_raw not in (None, ''):
                try:
                    item_id = int(item_id_raw)
                except (TypeError, ValueError):
                    cursor.close()
                    return jsonify({'success': False, 'message': f'第{i + 1}行申请单行参数无效'}), 400
            try:
                length = float(it.get('length'))
            except (TypeError, ValueError):
                length = None

            if part_number not in req_parts:
                cursor.close()
                return jsonify({'success': False, 'message': f'第{i + 1}行物料 {part_number} 不属于该申请单明细'}), 400
            if not COIL_ID_RE.match(coil_id):
                cursor.close()
                return jsonify({'success': False, 'message': f'第{i + 1}行卷号 {coil_id} 格式无效，应为9位数字（YYMMDD+3位）'}), 400
            if length is None or length <= 0:
                cursor.close()
                return jsonify({'success': False, 'message': f'第{i + 1}行长度必须大于0'}), 400

            # 按行维护：item_id 必须属于该申请单，且物料与申请单行一致
            if item_id is not None:
                cursor.execute(
                    "SELECT id, part_number FROM kr_request_item WHERE id = %s AND request_id = %s",
                    (item_id, request_id)
                )
                item_row = cursor.fetchone()
                if not item_row:
                    cursor.close()
                    return jsonify({'success': False, 'message': f'第{i + 1}行申请单行不存在或不属于该申请单'}), 400
                if item_row['part_number'] != part_number:
                    cursor.close()
                    return jsonify({'success': False, 'message': f'第{i + 1}行物料 {part_number} 与申请单行 {item_row["part_number"]} 不一致'}), 400

            submitted_parts.add(part_number)
            parsed.append({
                'part_number': part_number,
                'coil_id': coil_id,
                'length': round(length, 2),
                'unit': str(it.get('unit') or '').strip(),
                'lot_no': str(it.get('lot_no') or '').strip() or None,
                'item_id': item_id,
            })

        # R4：A/B 开头物料必须至少录入一行卷标（前端已拦，后端兜底）
        #   - 批量模式（未带 item_id）：按物料校验 A/B 必须出现在本次提交中
        #   - 按行模式（带 item_id）：A/B 行完整性由完成备料接口统一校验，本接口只写本行卷标
        if not any(p.get('item_id') is not None for p in parsed):
            ab_parts = [p for p in req_parts if p[:1].upper() in ('A', 'B')]
            for p in ab_parts:
                if p not in submitted_parts:
                    cursor.close()
                    return jsonify({'success': False, 'message': f'物料 {p} 以 A/B 开头，必须录入卷标信息'}), 400

        # R4.5：自动分配申请单行（修复同物料多行合并问题）
        #   item_id 为空的行：自动绑定到该物料第一个「还没有卷标」的申请单行；
        #   全部已覆盖时归到该物料最后一个行（允许继续追加卷标）。
        #   lot_no 为空时：默认带出该申请单行的批次号（物料批次跟踪，允许用户修改后传入）。
        for p in parsed:
            if p.get('item_id') is not None:
                continue
            cursor.execute(
                "SELECT id, batch_no AS lot_no FROM kr_request_item WHERE request_id = %s AND part_number = %s ORDER BY id",
                (request_id, p['part_number'])
            )
            item_rows = cursor.fetchall()
            item_ids = [r['id'] for r in item_rows]
            if not item_ids:
                cursor.close()
                return jsonify({'success': False, 'message': f"物料 {p['part_number']} 无对应申请单行"}), 400
            target = None
            for iid in item_ids:
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM kr_wire_coil "
                    "WHERE request_id = %s AND item_id = %s AND is_deleted = 0",
                    (request_id, iid)
                )
                if cursor.fetchone()['cnt'] == 0:
                    target = iid
                    break
            target = target if target is not None else item_ids[-1]
            p['item_id'] = target
            # Lot 默认带出该行的批次号（用户未传或为空时）
            if not (p.get('lot_no') or '').strip():
                row_lot = next((r.get('lot_no') or '' for r in item_rows if r['id'] == target), '')
                p['lot_no'] = row_lot or None

        # 4. 逐行写入（单位由 CSI 重新获取覆盖，防止篡改；唯一冲突重取号重试）
        siteref = req['siteref']
        operator = user['username']
        inserted = []
        warnings = []
        now = datetime.now()
        # 单位优先读申请单发起时保存的 kr_request_item.unit（不连 CSI），空则兜底查 CSI
        item_unit_map = {}
        try:
            cursor.execute(
                "SELECT part_number, unit FROM kr_request_item WHERE request_id = %s",
                (request_id,)
            )
            for r in cursor.fetchall():
                u = (r.get('unit') or '').strip()
                if u:
                    item_unit_map[r['part_number']] = u
        except Exception:
            pass
        try:
            for it in parsed:
                # R7：单位优先用发起时保存的单位，兜底再查 CSI
                unit = item_unit_map.get(it['part_number']) or _get_unit_cached(siteref, it['part_number'])
                if not unit:
                    warnings.append(f"物料 {it['part_number']} 单位获取失败，已留空（可稍后打印时确认）")
                it['unit'] = unit or None

                ok, new_id, err_msg = _insert_coil_with_retry(
                    cursor, it, request_id, siteref, operator, now
                )
                if not ok:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': err_msg}), 400
                inserted.append({
                    'id': new_id,
                    'coil_id': it['coil_id'],
                    'part_number': it['part_number'],
                    'length': it['length'],
                    'unit': it['unit'],
                    'status': 'in_stock',
                    'item_id': it.get('item_id'),
                })

            # 5. 操作日志
            _add_log(cursor, request_id, operator, 'COIL_REGISTER',
                     f"卷标录入: {len(inserted)} 卷", request.remote_addr)
            db.commit()
            cursor.close()
        except Exception as e:
            db.rollback()
            cursor.close()
            return jsonify({'success': False, 'message': f'保存失败: {e}'}), 500

    message = f'已录入 {len(inserted)} 卷卷标'
    if warnings:
        message += '；' + '；'.join(warnings[:3])
    return jsonify({'success': True, 'message': message, 'inserted': len(inserted),
                    'data': inserted, 'warnings': warnings})


def _insert_coil_with_retry(cursor, it, request_id, siteref, operator, now, max_retry=3):
    """
    按卷插入 kr_wire_coil，uk_coil_id 唯一冲突时重新取号重试（最多 max_retry 次）。
    返回 (ok, lastrowid, err_msg)
    """
    for attempt in range(max_retry):
        cursor.execute("SAVEPOINT sp_coil")
        try:
            cursor.execute(
                """INSERT INTO kr_wire_coil
                   (coil_id, part_number, lot_no, coil_length, unit, status, request_id, siteref, operator, item_id, remark, created_at)
                   VALUES (%s, %s, %s, %s, %s, 'in_stock', %s, %s, %s, %s, %s, %s)""",
                (it['coil_id'], it['part_number'], it.get('lot_no'), it['length'], it['unit'],
                 request_id, siteref, operator, it.get('item_id'), it.get('remark'), now)
            )
            return True, cursor.lastrowid, None
        except IntegrityError as e:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_coil")
            if e.args[0] == 1062:  # Duplicate entry
                if attempt == max_retry - 1:
                    return False, None, f"卷号 {it['coil_id']} 已存在，请重新生成"
                try:
                    # 重试沿用原卷号日期前缀（YYYYMMDD），避免跨日生成不同日期卷号
                    orig_date = datetime.strptime(it['coil_id'][:6], '%y%m%d')
                    it['coil_id'] = _gen_next_id(cursor, orig_date)
                except ValueError as ve:
                    return False, None, str(ve)
            else:
                raise
    return False, None, '未知错误'


# ================= 3. 申请单卷标列表 =================

@coil_bp.route('/api/requests/<int:request_id>/coils', methods=['GET'])
def list_request_coils(request_id):
    # 卷标数据含工单/操作人/加工参数，仅限 warehouse/admin 查看（P2-3）
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err

        # 按申请单行（item_id）过滤：卷标按行维护时仅查该行已录入的卷标
        item_id = (request.args.get('item_id') or '').strip()
        if item_id:
            try:
                item_id_int = int(item_id)
            except ValueError:
                cursor.close()
                return jsonify({'success': False, 'message': 'item_id 必须为整数'}), 400
            cursor.execute(
                "SELECT * FROM kr_wire_coil WHERE request_id = %s AND item_id = %s AND is_deleted = 0 ORDER BY id",
                (request_id, item_id_int)
            )
        else:
            cursor.execute(
                "SELECT * FROM kr_wire_coil WHERE request_id = %s AND is_deleted = 0 ORDER BY id",
                (request_id,)
            )
        rows = cursor.fetchall()
        cursor.close()

    return jsonify({'success': True, 'data': [_coil_to_dict(r) for r in rows]})


# ================= 3.1 删除卷标（按行维护） =================

@coil_bp.route('/api/coils/<int:coil_id>', methods=['DELETE'])
def delete_coil(coil_id):
    """删除卷标（软删除，仅本申请单 + warehouse/admin）。

    删除前提（全部满足才允许）：
      1. 状态为「在库」(in_stock)；
      2. 未参与任何物料消耗（kr_wire_coil_consumption 无该卷标记录）；
      3. 未参与退料交易（kr_return_item 无该卷标记录）；
      4. 该申请单未打印过标签（无 COIL_PRINT 日志，避免删除已打印标签的卷标）；
      5. 按顺序从后往前删除（只能删最后一条）。
    满足条件后执行**软删除**（is_deleted=1），数据保留可恢复。
    """
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM kr_wire_coil WHERE id = %s AND is_deleted = 0", (coil_id,))
            coil = cursor.fetchone()
            if not coil:
                cursor.close()
                return jsonify({'success': False, 'message': '卷标不存在或已删除'}), 404

            # 站点隔离：按来源申请单校验
            req = _get_request(cursor, coil['request_id'])
            if not req:
                cursor.close()
                return jsonify({'success': False, 'message': '卷标来源申请单不存在'}), 404
            err = _check_site(req, user)
            if err:
                cursor.close()
                return err

            if coil['status'] != 'in_stock':
                cursor.close()
                return jsonify({
                    'success': False,
                    'message': f"卷标 {coil['coil_id']} 当前状态为 {COIL_STATUS_LABELS.get(coil['status'], coil['status'])}，不能删除"
                }), 400

            # 已消耗过（出库/报废记录）→ 禁止删除
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM kr_wire_coil_consumption WHERE coil_id = %s",
                (coil['coil_id'],)
            )
            if cursor.fetchone()['cnt'] > 0:
                cursor.close()
                return jsonify({
                    'success': False,
                    'message': f"卷标 {coil['coil_id']} 已有物料消耗记录（出库/报废），禁止删除"
                }), 400

            # 参与过退料交易 → 禁止删除
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM kr_return_item WHERE coil_id = %s",
                (coil['coil_id'],)
            )
            if cursor.fetchone()['cnt'] > 0:
                cursor.close()
                return jsonify({
                    'success': False,
                    'message': f"卷标 {coil['coil_id']} 已参与退料交易，禁止删除"
                }), 400

            # 该申请单打印过标签 → 禁止删除（已打印的卷标标签可能已贴出）
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM kr_operation_log WHERE request_id = %s AND action = 'COIL_PRINT'",
                (coil['request_id'],)
            )
            if cursor.fetchone()['cnt'] > 0:
                cursor.close()
                return jsonify({
                    'success': False,
                    'message': f"该申请单已打印过标签，卷标 {coil['coil_id']} 禁止删除（标签可能已使用）"
                }), 400

            # 从后往前删除：仅允许删除最后一条（id 最大）的 in_stock 卷标
            # 范围：有 item_id 按申请单行级，否则按申请单级
            if coil.get('item_id'):
                cursor.execute(
                    "SELECT MAX(id) AS max_id FROM kr_wire_coil WHERE request_id = %s AND item_id = %s AND status = 'in_stock' AND is_deleted = 0",
                    (coil['request_id'], coil['item_id'])
                )
            else:
                cursor.execute(
                    "SELECT MAX(id) AS max_id FROM kr_wire_coil WHERE request_id = %s AND status = 'in_stock' AND is_deleted = 0",
                    (coil['request_id'],)
                )
            max_row = cursor.fetchone()
            max_id = (max_row or {}).get('max_id')
            if max_id is None or int(max_id) != int(coil_id):
                cursor.close()
                return jsonify({
                    'success': False,
                    'message': '请按顺序从后往前删除（只能先删除最后录入的一条卷标）'
                }), 400

            # 软删除（数据保留，可恢复）
            cursor.execute(
                "UPDATE kr_wire_coil SET is_deleted = 1 WHERE id = %s AND is_deleted = 0",
                (coil_id,)
            )
            if cursor.rowcount == 0:
                cursor.close()
                return jsonify({'success': False, 'message': '卷标已删除或状态变化，请刷新'}), 400
            _add_log(cursor, coil['request_id'], user['username'], 'COIL_DELETE',
                     f"软删除卷标: {coil['coil_id']}", request.remote_addr)
            db.commit()
            cursor.close()
    except Exception as e:
        logger.error(f"[COIL] delete_coil error: {e}")
        return jsonify({'success': False, 'message': f'删除失败: {e}'}), 500

    return jsonify({'success': True, 'message': '卷标已删除（软删除，可恢复）'})


# ================= 4. 线卷库存列表查询 =================

@coil_bp.route('/api/coils', methods=['GET'])
def list_coils():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    part_number = (request.args.get('part_number') or '').strip()
    status = (request.args.get('status') or '').strip()
    request_id = (request.args.get('request_id') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        size = min(200, max(1, int(request.args.get('size', 20))))
    except ValueError:
        return jsonify({'success': False, 'message': '分页参数无效'}), 400

    conditions = []
    params = []
    if part_number:
        conditions.append("part_number LIKE %s")
        params.append(f"%{part_number}%")
    if status:
        conditions.append("status = %s")
        params.append(status)
    if request_id:
        try:
            request_id_int = int(request_id)
        except ValueError:
            return jsonify({'success': False, 'message': 'request_id 必须为整数'}), 400
        conditions.append("request_id = %s")
        params.append(request_id_int)
    if date_from:
        try:
            datetime.strptime(date_from, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'message': 'date_from 格式应为 YYYY-MM-DD'}), 400
        conditions.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        try:
            datetime.strptime(date_to, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'message': 'date_to 格式应为 YYYY-MM-DD'}), 400
        conditions.append("created_at <= %s")
        params.append(f"{date_to} 23:59:59")

    # 站点隔离
    site_filter, site_params = get_site_filter(user)
    if site_filter:
        conditions.append(f"s.{site_filter}")
        params.extend(site_params)

    # 排除软删除
    conditions.append("s.is_deleted = 0")

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * size

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute(f"SELECT COUNT(*) AS total FROM kr_wire_coil s{where}", params)
        total = cursor.fetchone()['total']
        cursor.execute(
            f"SELECT * FROM kr_wire_coil s{where} ORDER BY s.id DESC LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cursor.fetchall()
        cursor.close()

    return jsonify({
        'success': True,
        'data': [_coil_to_dict(r) for r in rows],
        'total': total,
        'page': page,
        'size': size,
        'total_pages': (total + size - 1) // size,
    })


# ================= 5. 批量获取物料单位（CSI） =================

@coil_bp.route('/api/requests/<int:request_id>/in-stock-coils', methods=['GET'])
def request_in_stock_coils(request_id):
    """备料参考：该申请单各物料现有「在库」卷标列表（建议优先使用，不够再生成新卷标）。

    返回 { part_number: [{coil_id, remain_length, unit}, ...] }
    """
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        cursor.execute(
            "SELECT DISTINCT part_number FROM kr_request_item WHERE request_id = %s AND part_number != ''",
            (request_id,)
        )
        parts = [r['part_number'] for r in cursor.fetchall()]
        result = {}
        if parts:
            ph = ','.join(['%s'] * len(parts))
            # 在库卷标（含剩余长度 = coil_length - 已消耗；is_return 标记退料回来的卷标，优先推荐）
            cursor.execute(
                f"SELECT c.coil_id, c.part_number, c.unit, c.coil_length, "
                f"COALESCE((SELECT SUM(out_length) FROM kr_wire_coil_consumption k WHERE k.coil_id = c.coil_id), 0) AS used, "
                f"EXISTS (SELECT 1 FROM kr_return_item ri WHERE ri.coil_id = c.coil_id) AS is_return "
                f"FROM kr_wire_coil c "
                f"WHERE c.status = 'in_stock' AND c.is_deleted = 0 AND c.part_number IN ({ph}) "
                f"AND NOT EXISTS ( "
                f"  SELECT 1 FROM kr_material_request r WHERE r.id = c.request_id "
                f"  AND r.is_deleted = 0 AND r.status IN ('pending_prep','prepping','ready_pickup','short') "
                f") "
                f"ORDER BY is_return DESC, c.id",
                parts
            )
            for r in cursor.fetchall():
                unit = (r['unit'] or '').strip().upper()
                factor = Config.UNIT_CONVERT_FACTOR.get(unit) if unit else None
                coil_length = float(r['coil_length'] or 0)
                used = float(r['used'] or 0)
                is_return = bool(r['is_return'])
                if is_return:
                    # 退回的卷标：确认退料时 coil_length 已回写为「剩余长度」（原始单位），
                    # 不能再减历史消耗（历史消耗在回写时已扣，且 used 单位为 mm，直接相减会
                    # 单位不一致+重复扣减 → remain 恒为负 → 卷标被 continue 过滤掉）。
                    remain = coil_length
                else:
                    # 未退回的在库卷标：coil_length 为原始长度，used(mm) 须换算原始单位后相减
                    used_orig = used / factor if factor else used
                    remain = round(coil_length - used_orig, 2)
                if remain <= 0:
                    continue
                result.setdefault(r['part_number'], []).append({
                    'coil_id': r['coil_id'],
                    'remain_length': remain,
                    'unit': r['unit'] or '',
                    'is_return': is_return,
                })
        cursor.close()

    return jsonify({'success': True, 'data': result})


@coil_bp.route('/api/requests/<int:request_id>/coils/use-stock', methods=['POST'])
def use_stock_coil(request_id):
    """备料时选用「在库」卷标：将该在库卷标绑定到当前申请单。

    绑定后该卷标随申请单 sign（确认取料）自动转「在车间」，再次发往车间。
    """
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    coil_id = str(data.get('coil_id') or '').strip()
    if not coil_id:
        return jsonify({'success': False, 'message': '缺少卷标ID'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if req['status'] not in ('pending_prep', 'prepping', 'short', 'ready_pickup'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许选用卷标'}), 400

        # 卷标校验：存在 + 在库
        cursor.execute(
            "SELECT coil_id, part_number, status, request_id, item_id FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        coil = cursor.fetchone()
        if not coil:
            cursor.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        if coil['status'] != 'in_stock':
            cursor.close()
            return jsonify({'success': False, 'message': '该卷标不是「在库」状态，无法选用'}), 400

        # 物料必须属于该申请单
        cursor.execute(
            "SELECT id FROM kr_request_item WHERE request_id = %s AND part_number = %s LIMIT 1",
            (request_id, coil['part_number'])
        )
        item = cursor.fetchone()
        if not item:
            cursor.close()
            return jsonify({'success': False, 'message': f"卷标 {coil_id} 的物料不属于本申请单"}), 400

        # 绑定到当前申请单（卷标所有权转移；备份原归属，便于取消选用时还原）
        # 注意：原归属可能为 0（可选池），不能用 bool 判断，须用 is not None
        prev_request_id = coil['request_id'] if coil['request_id'] != request_id else None
        cursor.execute(
            "UPDATE kr_wire_coil SET request_id = %s, item_id = %s, "
            "prev_request_id = %s, prev_item_id = %s "
            "WHERE coil_id = %s AND status = 'in_stock'",
            (request_id, item['id'], prev_request_id,
             coil['item_id'] if prev_request_id is not None else None, coil_id)
        )
        if cursor.rowcount == 0:
            cursor.close()
            return jsonify({'success': False, 'message': '卷标状态已变化，请刷新后重试'}), 400
        # 操作日志
        cursor.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'COIL_USE_STOCK', %s, %s, %s)",
            (request_id, user['username'], f"选用在库卷标 {coil_id}（绑定至本单）", request.remote_addr, datetime.now())
        )
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': f'已选用在库卷标 {coil_id}，签字取料后自动转在车间'})


@coil_bp.route('/api/requests/<int:request_id>/coils/unuse-stock', methods=['POST'])
def unuse_stock_coil(request_id):
    """取消选用在库卷标：从当前申请单解除绑定，还原到原归属申请单（回到可选清单）。"""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if user['role'] not in ('warehouse', 'admin'):
        return jsonify({'success': False, 'message': '权限不足'}), 403

    data = request.get_json() or {}
    coil_id = str(data.get('coil_id') or '').strip()
    if not coil_id:
        return jsonify({'success': False, 'message': '缺少卷标ID'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if req['status'] not in ('pending_prep', 'prepping', 'short', 'ready_pickup'):
            cursor.close()
            return jsonify({'success': False, 'message': '当前状态不允许取消选用'}), 400

        cursor.execute(
            "SELECT coil_id, status, request_id, item_id, prev_request_id, prev_item_id, is_deleted "
            "FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0",
            (coil_id,)
        )
        coil = cursor.fetchone()
        if not coil:
            cursor.close()
            return jsonify({'success': False, 'message': '卷标不存在'}), 404
        if coil['request_id'] != request_id:
            cursor.close()
            return jsonify({'success': False, 'message': '该卷标不属于本申请单'}), 400
        if coil['status'] != 'in_stock':
            cursor.close()
            return jsonify({'success': False, 'message': '该卷标状态已变化（非在库），不能取消选用'}), 400
        if coil['prev_request_id'] is None:
            cursor.close()
            return jsonify({'success': False, 'message': '该卷标不是从在库选用的，无需取消（请用删除）'}), 400

        # 还原到原归属申请单（可为 0 = 可选池）
        cursor.execute(
            "UPDATE kr_wire_coil SET request_id = %s, item_id = %s, "
            "prev_request_id = NULL, prev_item_id = NULL "
            "WHERE coil_id = %s AND status = 'in_stock'",
            (coil['prev_request_id'],
             coil['prev_item_id'] if coil['prev_item_id'] is not None else 0, coil_id)
        )
        if cursor.rowcount == 0:
            cursor.close()
            return jsonify({'success': False, 'message': '取消选用失败，请刷新后重试'}), 400
        cursor.execute(
            "INSERT INTO kr_operation_log (request_id, operator, action, detail, ip_address, created_at) "
            "VALUES (%s, %s, 'COIL_UNUSE_STOCK', %s, %s, %s)",
            (request_id, user['username'], f"取消选用卷标 {coil_id}（还原至原申请单{coil['prev_request_id']}）",
             request.remote_addr, datetime.now())
        )
        db.commit()
        cursor.close()

    return jsonify({'success': True, 'message': f'已取消选用卷标 {coil_id}，回到可选清单'})


@coil_bp.route('/api/requests/<int:request_id>/coil-units', methods=['GET'])
def request_coil_units(request_id):
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': '未登录'}), 401

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        cursor.execute(
            "SELECT part_number, unit FROM kr_request_item WHERE request_id = %s",
            (request_id,)
        )
        item_rows = cursor.fetchall()
        cursor.close()

    # 单位在申请单发起时已从 CSI 获取并存入 kr_request_item.unit，直接读库（不再连 CSI）
    result = {}
    for r in item_rows:
        p = r['part_number']
        u = (r.get('unit') or '').strip()
        # 历史数据 unit 为空时兜底查 CSI（带缓存）
        if not u:
            u = _get_unit_cached(req['siteref'], p) or ''
        result[p] = u
    return jsonify({'success': True, 'data': result})


@coil_bp.route('/api/coils/units', methods=['POST'])
def batch_coil_units():
    """批量获取物料单位（POST 形式，开发任务清单约定）。

    站点固定取登录用户站点，不信任前端传入的 siteref（P2-2）；
    仅限 warehouse/admin 调用。
    """
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    part_numbers = data.get('part_numbers') or []
    siteref = user.get('siteref')
    if not siteref:
        return jsonify({'success': False, 'message': '当前用户未绑定站点，无法查询物料单位'}), 403
    if not part_numbers:
        return jsonify({'success': False, 'message': '请提供 part_numbers 列表'}), 400
    if len(part_numbers) > 200:
        return jsonify({'success': False, 'message': '单次最多查询 200 个物料'}), 400

    result = {}
    parts_clean = [str(p).strip() for p in part_numbers if str(p).strip()]
    if parts_clean:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_get_unit_cached, siteref, p): p for p in parts_clean}
            for fut in futures:
                p = futures[fut]
                try:
                    result[p] = fut.result() or ''
                except Exception:
                    result[p] = ''
    return jsonify({'success': True, 'data': result})


# ================= 6. 标签打印 =================

def _fetch_coils_for_print(coil_ids):
    """按 coil_ids 查询线卷（含站点隔离），返回 (coils, user) 或抛错"""
    user = session.get('user')
    if not user:
        raise PermissionError('未登录')
    if user['role'] not in ('warehouse', 'admin'):
        raise PermissionError('权限不足')

    placeholders = ','.join(['%s'] * len(coil_ids))
    site_filter, site_params = get_site_filter(user)
    with get_db_connection() as db:
        cursor = db.cursor()
        if site_filter:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0 AND {site_filter}",
                coil_ids + list(site_params)
            )
        else:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0",
                coil_ids
            )
        rows = cursor.fetchall()
        cursor.close()
    return rows


def _resolve_site_printer(cursor, coils, explicit_printer):
    """按站点解析打印目标：(printer_name, channel)。

    优先级：
      1. 请求体显式指定的 printer（最高优先级）
      2. 站点表 kr_site_printer（按第一条卷标的 siteref 查询）
      3. 站点表未配 → printer 留空、channel 留空（print_labels 内回退 Config 默认）

    一批卷标应为同站点，取第一条卷标的 siteref。
    """
    printer = (explicit_printer or '').strip()
    channel = None
    if not printer and coils:
        siteref = (coils[0].get('siteref') or '').strip()
        if siteref:
            cursor.execute(
                "SELECT printer_name, channel FROM kr_site_printer WHERE siteref = %s",
                (siteref,)
            )
            row = cursor.fetchone()
            if row:
                printer = (row.get('printer_name') or '').strip()
                channel = (row.get('channel') or '').strip()
    return printer, channel


@coil_bp.route('/api/coils/print', methods=['POST'])
def print_coils():
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    coil_ids = data.get('coil_ids') or []
    explicit_printer = (data.get('printer') or '').strip()
    if not coil_ids:
        return jsonify({'success': False, 'message': '请选择需要打印的卷标'}), 400
    if len(coil_ids) > MAX_BATCH:
        return jsonify({'success': False, 'message': f'单次最多打印 {MAX_BATCH} 卷'}), 400
    coil_ids = [str(c).strip() for c in coil_ids if str(c).strip()]

    with get_db_connection() as db:
        cursor = db.cursor()
        # 打印属于本站点（非 admin）可访问的卷
        placeholders = ','.join(['%s'] * len(coil_ids))
        site_filter, site_params = get_site_filter(user)
        if site_filter:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0 AND {site_filter}",
                coil_ids + list(site_params)
            )
        else:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0",
                coil_ids
            )
        coils = cursor.fetchall()
        # 按站点解析打印机与通道（显式 printer 优先）
        printer, channel = _resolve_site_printer(cursor, coils, explicit_printer)
        # 操作日志
        _add_log(cursor, None, user['username'], 'COIL_PRINT',
                 f"标签打印: {len(coils)} 卷（{','.join(coil_ids[:10])}{'...' if len(coil_ids) > 10 else ''}）",
                 request.remote_addr)
        db.commit()
        cursor.close()

    if not coils:
        return jsonify({'success': False, 'message': '未找到可打印的卷标（可能不属于当前站点）'}), 404

    # 打印失败不影响其他操作，返回可读错误
    result = label_print_service.print_labels([dict(c) for c in coils], printer, channel)
    if result['success']:
        return jsonify({'success': True, 'message': f"已提交 {result['printed']} 张标签打印",
                        'printed': result['printed'], 'errors': []})
    return jsonify({'success': False, 'message': '部分标签打印失败',
                    'printed': result['printed'], 'errors': result['errors']})


@coil_bp.route('/api/requests/<int:request_id>/coils/print', methods=['POST'])
def request_coils_print(request_id):
    """打印申请单卷标（开发任务清单约定）。

    - 请求体带 coil_ids：只打印指定卷标（按行打印/批量打印所选）
    - 请求体不带 coil_ids：兼容旧调用，打印该申请单全部卷标
    """
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    coil_ids_param = [str(x).strip() for x in (data.get('coil_ids') or []) if str(x).strip()]

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if coil_ids_param:
            # 仅打印指定卷标（须属于该申请单）
            placeholders = ','.join(['%s'] * len(coil_ids_param))
            cursor.execute(
                f"SELECT coil_id FROM kr_wire_coil WHERE request_id = %s AND coil_id IN ({placeholders}) ORDER BY id",
                [request_id] + coil_ids_param
            )
        else:
            # 兼容旧调用：无 coil_ids 时打印该申请单全部卷标
            cursor.execute(
                "SELECT coil_id FROM kr_wire_coil WHERE request_id = %s ORDER BY id",
                (request_id,)
            )
        coil_ids = [r['coil_id'] for r in cursor.fetchall()]
        cursor.close()

    if not coil_ids:
        return jsonify({'success': False, 'message': '没有可打印的卷标'}), 400

    return print_coils_inner(coil_ids, (data.get('printer') or '').strip(), user, request_id)


def print_coils_inner(coil_ids, explicit_printer, user, request_id=None):
    """打印逻辑（内部复用），供 print_coils / request_coils_print 调用"""
    with get_db_connection() as db:
        cursor = db.cursor()
        placeholders = ','.join(['%s'] * len(coil_ids))
        site_filter, site_params = get_site_filter(user)
        if site_filter:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0 AND {site_filter}",
                coil_ids + list(site_params)
            )
        else:
            cursor.execute(
                f"SELECT * FROM kr_wire_coil WHERE coil_id IN ({placeholders}) AND is_deleted = 0",
                coil_ids
            )
        coils = cursor.fetchall()
        # 按站点解析打印机与通道（显式 printer 优先）
        printer, channel = _resolve_site_printer(cursor, coils, explicit_printer)
        _add_log(cursor, request_id, user['username'], 'COIL_PRINT',
                 f"标签打印: {len(coils)} 卷", request.remote_addr)
        db.commit()
        cursor.close()

    if not coils:
        return jsonify({'success': False, 'message': '未找到可打印的卷标'}), 404

    result = label_print_service.print_labels([dict(c) for c in coils], printer, channel)
    if result['success']:
        return jsonify({'success': True, 'message': f"已提交 {result['printed']} 张标签打印",
                        'printed': result['printed'], 'errors': []})
    return jsonify({'success': False, 'message': '部分标签打印失败',
                    'printed': result['printed'], 'errors': result['errors']})


# ================= 7. 标签渲染数据（预览） =================

@coil_bp.route('/api/coils/<coil_id>/label', methods=['GET'])
def coil_label_preview(coil_id):
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM kr_wire_coil WHERE coil_id = %s AND is_deleted = 0", (coil_id,))
        coil = cursor.fetchone()
        cursor.close()

    if not coil:
        return jsonify({'success': False, 'message': '卷标不存在'}), 404
    err = _check_site(coil, user)
    if err:
        return err

    return jsonify({'success': True, 'data': label_print_service.LabelRenderer.render(dict(coil))})


@coil_bp.route('/api/requests/<int:request_id>/coils/preview', methods=['GET'])
def request_coils_preview(request_id):
    """申请单卷标预览数据（开发任务清单约定）；仅限 warehouse/admin（P2-3）"""
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    coil_id = (request.args.get('coil_id') or '').strip()
    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if coil_id:
            cursor.execute(
                "SELECT * FROM kr_wire_coil WHERE request_id = %s AND coil_id = %s AND is_deleted = 0",
                (request_id, coil_id)
            )
        else:
            cursor.execute(
                "SELECT * FROM kr_wire_coil WHERE request_id = %s AND is_deleted = 0 ORDER BY id",
                (request_id,)
            )
        rows = cursor.fetchall()
        cursor.close()

    data = [label_print_service.LabelRenderer.render(dict(r)) for r in rows]
    return jsonify({'success': True, 'data': data})


# ================= 8. 出库消耗登记 =================

@coil_bp.route('/api/requests/<int:request_id>/consumption', methods=['POST'])
def create_consumption(request_id):
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    data = request.get_json() or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'success': False, 'message': '请至少登记一条出库记录'}), 400
    if len(items) > MAX_BATCH:
        return jsonify({'success': False, 'message': f'单次最多登记 {MAX_BATCH} 条出库记录'}), 400

    with get_db_connection() as db:
        cursor = db.cursor()

        # 申请单校验
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err
        if req['status'] != 'prepping':
            cursor.close()
            return jsonify({'success': False, 'message': '仅备料中（prepping）状态的申请单可登记出库'}), 400

        siteref = req['siteref']
        operator = user['username']

        # 校验并写入
        inserted = 0
        issued_coils = []
        warnings = []
        try:
            for i, it in enumerate(items):
                coil_id = str(it.get('coil_id') or '').strip()
                job_order = (it.get('job_order') or '').strip() or None
                try:
                    out_length = float(it.get('out_length'))
                except (TypeError, ValueError):
                    out_length = None
                remark = (it.get('remark') or '').strip() or None

                if not coil_id:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': f'第{i + 1}行缺少卷号'}), 400
                if out_length is None or out_length <= 0:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': f'第{i + 1}行出库长度必须大于0'}), 400

                # R12：宽表字段解析与取值校验（未填写字段保存为 NULL）
                extra, err = _parse_extra_fields(it, i + 1)
                if err:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': err}), 400

                # R9：卷标必须属于本单且 in_stock
                cursor.execute(
                    "SELECT * FROM kr_wire_coil WHERE coil_id = %s AND request_id = %s AND is_deleted = 0",
                    (coil_id, request_id)
                )
                coil = cursor.fetchone()
                if not coil:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': f'卷标 {coil_id} 不属于该申请单'}), 400
                if coil['status'] != 'in_stock':
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': f'卷标 {coil_id} 当前状态为 {coil["status"]}，不能出库'}), 400

                # R9.5：盘点锁定检查——卷标在活跃盘点中（盘点单 counting）不允许消耗
                cursor.execute(
                    "SELECT COUNT(*) AS n FROM kr_inventory_count_item i "
                    "JOIN kr_inventory_count c ON c.id = i.count_id "
                    "WHERE i.coil_id = %s AND c.status = 'counting'",
                    (coil_id,)
                )
                if cursor.fetchone()['n'] > 0:
                    db.rollback()
                    cursor.close()
                    return jsonify({'success': False, 'message': f'卷标 {coil_id} 正在盘点中（已锁定），不允许消耗'}), 400

                # job_order 格式校验（可空；不强制工单存在）
                if job_order:
                    from app.services.csi_service import parse_job
                    j, _s = parse_job(job_order)
                    if not j:
                        db.rollback()
                        cursor.close()
                        return jsonify({'success': False, 'message': f'第{i + 1}行工单号格式无效: {job_order}'}), 400

                # R11：单位换算（服务端计算并覆盖，不信任前端传值）
                unit = (coil.get('unit') or '').strip() or None
                converted_length, converted_unit = _convert_length(out_length, unit)
                if not converted_length:
                    warnings.append(f'卷标 {coil_id} 单位 {unit or "未知"} 未收录换算系数，转换结果已置空')

                # R8：单次 + 累计 ≤ 卷长（统一换算 mm 比较，文档 7.1 R8）
                #   卷长(mm) = coil_length × 换算系数；out_length / SUM(out_length) 原始单位为 mm
                total_length = float(coil['coil_length'])
                factor = Config.UNIT_CONVERT_FACTOR.get(unit.upper()) if unit else None
                cursor.execute(
                    """SELECT COALESCE(SUM(out_length), 0) AS used FROM kr_wire_coil_consumption
                       WHERE coil_id = %s AND consume_type = 'issue'""",
                    (coil_id,)
                )
                used = float(cursor.fetchone()['used'])
                if factor:
                    total_mm = total_length * factor
                    if out_length > total_mm - used + 0.0001:
                        db.rollback()
                        cursor.close()
                        return jsonify({
                            'success': False,
                            'message': f'卷标 {coil_id} 出库长度 {out_length:g}mm 超过剩余长度 {total_mm - used:g}mm'
                        }), 400
                else:
                    # 单位未知时无法换算校验，仅警告不阻断（与 R11 降级策略一致）
                    warnings.append(f'卷标 {coil_id} 单位 {unit or "未知"} 未收录换算系数，已跳过长度校验')

                # 写入消耗记录（冗余 part_number/unit；不再写 request_id，追溯经 coil_id 上查 kr_wire_coil.request_id）
                cols = (['coil_id', 'job_order', 'part_number', 'consume_type', 'out_length',
                         'unit', 'converted_length', 'converted_unit']
                        + list(CONSUMPTION_EXTRA_FIELDS.keys())
                        + ['operator', 'remark', 'created_at'])
                vals = [coil_id, job_order, coil['part_number'], 'issue', round(out_length, 2),
                        unit, converted_length, converted_unit]
                vals += [extra[k] for k in CONSUMPTION_EXTRA_FIELDS]
                vals += [operator, remark, datetime.now()]
                cursor.execute(
                    "INSERT INTO kr_wire_coil_consumption (%s) VALUES (%s)"
                    % (', '.join(cols), ', '.join(['%s'] * len(cols))),
                    vals
                )
                inserted += 1

                # 整卷出完 → 状态置 issued（单位未知无法换算时保守不更新）
                new_used = used + out_length
                if factor and new_used >= total_mm - 0.0001:
                    cursor.execute(
                        "UPDATE kr_wire_coil SET status = 'issued' WHERE id = %s",
                        (coil['id'],)
                    )
                    issued_coils.append(coil_id)

            _add_log(cursor, request_id, operator, 'OUTBOUND_REGISTER',
                     f"出库登记: {inserted} 条，整卷出库: {len(issued_coils)} 卷（{','.join(issued_coils[:10])}）",
                     request.remote_addr)
            db.commit()
            cursor.close()
        except Exception as e:
            db.rollback()
            cursor.close()
            return jsonify({'success': False, 'message': f'登记失败: {e}'}), 500

    message = f'已登记 {inserted} 条出库记录'
    if warnings:
        message += '；' + '；'.join(warnings[:3])
    return jsonify({'success': True, 'message': message,
                    'inserted': inserted, 'issued': issued_coils, 'warnings': warnings})


# ================= 9. 申请单消耗记录查询 =================

@coil_bp.route('/api/requests/<int:request_id>/consumption', methods=['GET'])
def list_consumption(request_id):
    # 消耗记录含工单/操作人/加工参数，仅限 warehouse/admin 查看（P2-3）
    user, err_resp, err_code = _check_warehouse_or_admin()
    if err_resp:
        return err_resp, err_code

    with get_db_connection() as db:
        cursor = db.cursor()
        req = _get_request(cursor, request_id)
        if not req:
            cursor.close()
            return jsonify({'success': False, 'message': '单据不存在'}), 404
        err = _check_site(req, user)
        if err:
            cursor.close()
            return err

        # 消耗表不再冗余 request_id，按 coil_id 关联 kr_wire_coil 追溯本单
        cursor.execute(
            """SELECT c.* FROM kr_wire_coil_consumption c
               JOIN kr_wire_coil w ON c.coil_id = w.coil_id
               WHERE w.request_id = %s ORDER BY c.id""",
            (request_id,)
        )
        rows = cursor.fetchall()
        cursor.close()

    # 数值字段统一转 float（含宽表数值列），created_at 转字符串
    numeric_fields = ['out_length', 'converted_length', 'shear_length', 'length_tolerance']
    result = []
    for r in rows:
        d = dict(r)
        for f in numeric_fields:
            if d.get(f) is not None:
                d[f] = float(d[f])
        d['created_at'] = str(d['created_at']) if d.get('created_at') else None
        result.append(d)
    return jsonify({'success': True, 'data': result})
