"""CSI 库位转移接口表（kr_csi_interface）入队

两个方向：
  - 完成备料 enqueue_qty_move()：仓库库位 → Floor-Core（正向）
  - 回到备料 enqueue_reverse_qty_move()：Floor-Core → 仓库库位（反向，from/to 对调）

完成备料时把需同步给宝鼎 Syteline（Infor CSI）的库位转移数据写入接口表，
由常驻同步器（另一进程）每分钟提交 sl-qty-move 接口。

粒度：按「卷标 / 批次」拆行
  - A/B 料：kr_wire_coil 每个未软删卷标一行（coil_id 有值、lot_id 为 NULL）
  - 非 A/B 料：kr_request_item_lot 每条批次一行（lot_id 有值、coil_id 为 NULL）
  - 非 A/B 料既无卷标也无批次：只记 warning，不阻断完成备料（批次为可选维护）

状态流转（接口表 status，is_sync 见同步器）：
  PENDING → QUEUED|SUBMITTING → SUCCESS（已过账）/ FAILED_MANUAL（失败转人工）
  PENDING|QUEUED|SUBMITTING --回到备料--> CANCELLED（作废，不再提交）
  SUCCESS --回到备料生成反向--> REVERSED（is_sync 保持 1，保留过账历史）
  完成备料去重只认 PENDING/QUEUED/SUBMITTING/SUCCESS，其余状态允许补发新正向。

调用方须在自己的事务内调用：写入与本函数使用同一 cursor，随业务操作一起 commit；
异常由调用方 rollback。
"""
import logging
import uuid
from datetime import datetime

from app.config import Config

logger = logging.getLogger(__name__)

API_QTY_MOVE = 'sl-qty-move'

# 尚未过账（同步器仍会扫描）的中间态：回到备料时须作废，避免退回后仍继续过账
INFLIGHT_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING')
CANCEL_MESSAGE = '已回到备料，取消同步'

# 仍然生效的正向记录（完成备料去重用）。
# REVERSED（已反向）/ CANCELLED（退回时作废）/ FAILED_MANUAL（未过账、转人工）
# 都不算生效，不应阻止再次完成备料生成新的正向记录。
ACTIVE_FORWARD_STATUS = ('PENDING', 'QUEUED', 'SUBMITTING', 'SUCCESS')

# 反向记录生成后，被反向的正向行置为该状态（is_sync 保持 1，保留过账历史）
REVERSED_STATUS = 'REVERSED'


def _is_ab_part(part_number):
    """A/B 开头物料走卷标粒度"""
    return (part_number or '')[:1].upper() in ('A', 'B')


def _num(value):
    """DECIMAL -> float（保留 NULL）"""
    return None if value is None else float(value)


def _traceid(request_id, key):
    """唯一追踪号：MP{request_id}-{coil_id 或 L{lot_id}}-{uuid12}，长度 < 128"""
    return f'MP{request_id}-{key}-{uuid.uuid4().hex[:12]}'


def _not_reverse_clause(request_id):
    """排除反向记录的 SQL 片段与参数。

    反向单号为 MP{request_id}B{n}（见 _reverse_document_number），正向为 MP{request_id}。
    反向行过账后同样是 is_sync=1 / SUCCESS，若不排除会被当成正向行再次反向。
    """
    return "(documentnumber IS NULL OR documentnumber NOT LIKE %s)", f'MP{request_id}B%'


def _coil_rows(cursor, request_id, item):
    """A/B 料：每个卷标一行"""
    cursor.execute(
        "SELECT coil_id, lot_no, coil_length, unit, loc FROM kr_wire_coil "
        "WHERE request_id = %s AND item_id = %s AND is_deleted = 0 ORDER BY id",
        (request_id, item['id'])
    )
    rows = []
    for coil in cursor.fetchall():
        coil_id = coil.get('coil_id')
        if not coil_id:
            continue
        rows.append({
            'item': item.get('part_number'),
            'lot': coil.get('lot_no'),
            'quantity': coil.get('coil_length'),
            # um 才是计量单位；unitcode1/unitcode2 是 account 的 unit code，保持 NULL
            'um': coil.get('unit') or item.get('unit'),
            'coil_id': coil_id,
            'lot_id': None,
            'item_id': item['id'],
            # 卷标维护时已持久化 Lot 所在库位；为空则交给同步器兜底
            'from_location': coil.get('loc'),
        })
    return rows


def _lot_rows(cursor, request_id, item):
    """非 A/B 料：每个批次分配一行"""
    cursor.execute(
        "SELECT id, lot_no, qty, loc FROM kr_request_item_lot "
        "WHERE request_id = %s AND item_id = %s ORDER BY id",
        (request_id, item['id'])
    )
    lot_records = cursor.fetchall()
    rows = []
    for lot in lot_records:
        lot_no = lot.get('lot_no')
        if not lot_no or lot.get('id') is None:
            # 兜底：无批号/无主键的行不写
            continue
        rows.append({
            'item': item.get('part_number'),
            'lot': lot_no,
            'quantity': lot.get('qty'),
            'um': item.get('unit'),
            'coil_id': None,
            'lot_id': lot['id'],
            'item_id': item['id'],
            'from_location': lot.get('loc'),
        })
    if not lot_records:
        logger.warning(
            '完成备料：非 A/B 物料无批次记录，跳过 CSI 同步 request_id=%s item_id=%s part=%s',
            request_id, item['id'], item.get('part_number')
        )
    return rows


def _collect_rows(cursor, request_id):
    """按卷标/批次拆行，返回待写入的行（不含单据级公共字段）"""
    cursor.execute(
        "SELECT id, part_number, unit FROM kr_request_item WHERE request_id = %s ORDER BY id",
        (request_id,)
    )
    rows = []
    for item in cursor.fetchall():
        if _is_ab_part(item.get('part_number')):
            rows.extend(_coil_rows(cursor, request_id, item))
        else:
            rows.extend(_lot_rows(cursor, request_id, item))
    return rows


def enqueue_qty_move(cursor, req, username, now=None):
    """把一张申请单的库位转移数据写入 kr_csi_interface。

    参数：
      cursor  - 完成备料事务内的游标（不在此提交/回滚）
      req     - kr_material_request 行（需 id / siteref）
      username- 操作人（createby）
    返回：本次新写入行数。
    """
    request_id = req['id']
    siteref = str(req.get('siteref') or '').strip()
    if siteref not in Config.CSI_WAREHOUSE_MAP:
        logger.warning('完成备料：站点 %s 未配置 CSI 仓库映射，跳过接口表写入 request_id=%s',
                       siteref, request_id)
        return 0

    rows = _collect_rows(cursor, request_id)
    if not rows:
        logger.warning('完成备料：无可同步的卷标/批次，request_id=%s', request_id)
        return 0

    # 幂等：仅当同一单据已存在「仍然生效」的正向记录时跳过该 (coil_id, lot_id)。
    # 只认 ACTIVE_FORWARD_STATUS：已反向（REVERSED）、退回作废（CANCELLED）、
    # 转人工未过账（FAILED_MANUAL）都不阻止再次完成备料补发新的正向记录。
    # （uk_row 唯一键已改为普通索引 idx_row，去重完全由这里的状态判断负责）
    active_ph = ','.join(['%s'] * len(ACTIVE_FORWARD_STATUS))
    not_rev_sql, not_rev_param = _not_reverse_clause(request_id)
    cursor.execute(
        "SELECT coil_id, lot_id FROM kr_csi_interface "
        "WHERE request_id = %s AND api_name = %s "
        f"AND status IN ({active_ph}) AND {not_rev_sql}",
        (request_id, API_QTY_MOVE) + ACTIVE_FORWARD_STATUS + (not_rev_param,)
    )
    existing = {(r.get('coil_id'), r.get('lot_id')) for r in cursor.fetchall()}

    now = now or datetime.now()
    documentnumber = f'MP{request_id}'[:12]
    inserted = 0
    for row in rows:
        key = (row['coil_id'], row['lot_id'])
        if key in existing:
            continue
        trace_key = row['coil_id'] or f"L{row['lot_id']}"
        cursor.execute(
            "INSERT INTO kr_csi_interface "
            "(request_id, item_id, coil_id, lot_id, api_name, siteRef, item, lot, quantity, um, "
            " warehouse, from_location, to_location, reasoncode, unitcode1, unitcode2, documentnumber, "
            " is_sync, status, retry_count, traceid, createdate, createby) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            " 0, 'PENDING', 0, %s, %s, %s)",
            (
                request_id, row['item_id'], row['coil_id'], row['lot_id'],
                API_QTY_MOVE, int(siteref), row['item'], row['lot'], _num(row['quantity']), row['um'],
                Config.CSI_WAREHOUSE_MAP[siteref], row['from_location'], Config.CSI_TO_LOCATION,
                Config.CSI_REASON_CODE, None, None, documentnumber,
                _traceid(request_id, trace_key), now, username,
            )
        )
        existing.add(key)
        inserted += 1

    logger.info('完成备料：CSI 接口表写入 %s 行（request_id=%s, 跳过已存在 %s 行）',
                inserted, request_id, len(rows) - inserted)
    return inserted


# ====================================================================== #
#  反向交易（回到备料）
# ====================================================================== #
def _reverse_document_number(request_id, n):
    """反向单号：MP{request_id}B{n}（n 从 1 递增），长度 ≤12（接口硬限制）"""
    return f'MP{request_id}B{n}'[:12]


def _reverse_traceid(request_id, n, key):
    """反向 traceId：MP{request_id}B{n}-{coil_id 或 L{lot_id}}-{uuid12}，长度 < 128"""
    return f'MP{request_id}B{n}-{key}-{uuid.uuid4().hex[:12]}'


def _cancel_inflight(cursor, request_id, now):
    """作废该单尚未过账的正向记录：is_sync=2 / CANCELLED。

    同步器只扫描 is_sync=0，标记后不会再被选中；已成功的（is_sync=1）与
    已转人工的（is_sync=2 / FAILED_MANUAL）都不受影响。
    返回作废行数。
    """
    placeholders = ','.join(['%s'] * len(INFLIGHT_STATUS))
    cursor.execute(
        f"UPDATE kr_csi_interface SET is_sync = 2, status = 'CANCELLED', errormessage = %s, "
        f"updatedate = %s "
        f"WHERE request_id = %s AND api_name = %s AND is_sync = 0 "
        f"AND status IN ({placeholders})",
        (CANCEL_MESSAGE, now, request_id, API_QTY_MOVE) + INFLIGHT_STATUS
    )
    return cursor.rowcount or 0


def enqueue_reverse_qty_move(cursor, req, username, now=None):
    """回到备料：为已成功过账的正向库位转移生成反向记录（from/to 对调）。

    规则：
      - 每条 is_sync=1 / SUCCESS 的正向记录 → 插入一条反向记录，
        from_location = 原 to_location、to_location = 原 from_location，其余字段照抄；
        单号用新号 MP{request_id}B{n}（原号已过账且被平台幂等锁定，不能复用）；
      - 被反向的正向记录同事务置 status='REVERSED'（is_sync 保持 1），
        之后不再参与反向查询，防止同一正向行被重复反向；
      - 未过账的中间态（is_sync=0 / PENDING|QUEUED|SUBMITTING）→ 置 CANCELLED；
      - FAILED_MANUAL 本来未过账 → 不动；
      - 原记录 from_location 为空 → 无法构造反向，跳过并 warning（不影响主流程）。

    参数：
      cursor  - 回到备料事务内的游标（不在此提交/回滚）
      req     - kr_material_request 行（需 id）
      username- 操作人（createby）
    返回：{'forward_count': 已成功正向记录数, 'inserted': 新插入反向行数,
           'cancelled': 作废未过账行数}
    """
    request_id = req['id']
    now = now or datetime.now()

    # 1) 先作废未过账的正向记录（必须在插入反向行之前，否则会把新反向行一起作废）
    cancelled = _cancel_inflight(cursor, request_id, now)

    # 2) 取已成功过账、且尚未被反向的正向记录（反向行 MP{rid}B* 排除在外）
    not_rev_sql, not_rev_param = _not_reverse_clause(request_id)
    cursor.execute(
        "SELECT id, item_id, coil_id, lot_id, siteRef, item, lot, quantity, um, job, "
        "       warehouse, from_location, to_location, reasoncode, account, unitcode1, unitcode2 "
        "FROM kr_csi_interface "
        "WHERE request_id = %s AND api_name = %s AND is_sync = 1 AND status = 'SUCCESS' "
        f"AND {not_rev_sql} "
        "ORDER BY id",
        (request_id, API_QTY_MOVE, not_rev_param)
    )
    forwards = cursor.fetchall()
    if not forwards:
        logger.info('回到备料：无已过账记录，未生成反向交易 request_id=%s（作废 %s 行）',
                    request_id, cancelled)
        return {'forward_count': 0, 'inserted': 0, 'cancelled': cancelled}

    # 3) 反向单号序号：按该单已有反向单号（MP{rid}B*）计数 +1，避免重号
    cursor.execute(
        "SELECT documentnumber FROM kr_csi_interface "
        "WHERE request_id = %s AND api_name = %s AND documentnumber LIKE %s",
        (request_id, API_QTY_MOVE, f'MP{request_id}B%')
    )
    used_docs = {(r.get('documentnumber') or '') for r in cursor.fetchall()}

    inserted = 0
    seq = 0
    for row in forwards:
        from_loc = (row.get('from_location') or '').strip()
        if not from_loc:
            logger.warning('回到备料：正向记录缺少 from_location，跳过反向 request_id=%s csi_id=%s',
                           request_id, row.get('id'))
            continue

        key = row.get('coil_id') or f"L{row.get('lot_id')}"

        # 先按状态抢占该正向行（REVERSED 后不会再被任何一轮反向选中），
        # 同事务内标记；rowcount=0 说明已被并发操作反向，跳过，避免重复反向。
        cursor.execute(
            "UPDATE kr_csi_interface SET status = %s, updatedate = %s, updateby = %s "
            "WHERE id = %s AND is_sync = 1 AND status = 'SUCCESS'",
            (REVERSED_STATUS, now, username, row.get('id'))
        )
        if not cursor.rowcount:
            logger.warning('回到备料：正向记录已被反向或状态已变更，跳过 request_id=%s csi_id=%s',
                           request_id, row.get('id'))
            continue

        # 序号按已用单号顺延，保证同单内不重号
        while True:
            seq += 1
            documentnumber = _reverse_document_number(request_id, seq)
            if documentnumber not in used_docs:
                break
            if len(documentnumber) >= 12:
                # 单号已顶到 12 位上限（申请单号过长），再顺延会溢出，宁可重号交给同步器换号
                logger.warning('回到备料：反向单号长度已达上限 request_id=%s doc=%s',
                               request_id, documentnumber)
                break
        used_docs.add(documentnumber)

        cursor.execute(
            "INSERT INTO kr_csi_interface "
            "(request_id, item_id, coil_id, lot_id, api_name, siteRef, item, lot, quantity, um, job, "
            " warehouse, from_location, to_location, reasoncode, account, unitcode1, unitcode2, "
            " documentnumber, is_sync, status, retry_count, traceid, errormessage, "
            " createdate, createby, submitted_at, last_poll_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            " 0, 'PENDING', 0, %s, NULL, %s, %s, NULL, NULL)",
            (
                request_id, row.get('item_id'), row.get('coil_id'), row.get('lot_id'),
                API_QTY_MOVE, row.get('siteRef'), row.get('item'), row.get('lot'),
                _num(row.get('quantity')), row.get('um'), row.get('job'),
                row.get('warehouse'),
                # from/to 对调：Floor-Core → 原仓库库位
                (row.get('to_location') or Config.CSI_TO_LOCATION), from_loc,
                row.get('reasoncode'), row.get('account'),
                row.get('unitcode1'), row.get('unitcode2'), documentnumber,
                _reverse_traceid(request_id, seq, key), now, username,
            )
        )
        inserted += 1

    logger.info('回到备料：生成反向库位转移 %s 笔（request_id=%s, 作废未过账 %s 行）',
                inserted, request_id, cancelled)
    return {'forward_count': len(forwards), 'inserted': inserted, 'cancelled': cancelled}
