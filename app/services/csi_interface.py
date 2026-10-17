"""完成备料 → CSI 库位转移接口表（kr_csi_interface）入队

完成备料时把需同步给宝鼎 Syteline（Infor CSI）的库位转移数据写入接口表，
由常驻同步器（另一进程）每分钟提交 sl-qty-move 接口。

粒度：按「卷标 / 批次」拆行
  - A/B 料：kr_wire_coil 每个未软删卷标一行（coil_id 有值、lot_id 为 NULL）
  - 非 A/B 料：kr_request_item_lot 每条批次一行（lot_id 有值、coil_id 为 NULL）
  - 非 A/B 料既无卷标也无批次：只记 warning，不阻断完成备料（批次为可选维护）

调用方须在自己的事务内调用：写入与本函数使用同一 cursor，随完成备料一起 commit；
异常由调用方 rollback。
"""
import logging
import uuid
from datetime import datetime

from app.config import Config

logger = logging.getLogger(__name__)

API_QTY_MOVE = 'sl-qty-move'


def _is_ab_part(part_number):
    """A/B 开头物料走卷标粒度"""
    return (part_number or '')[:1].upper() in ('A', 'B')


def _num(value):
    """DECIMAL -> float（保留 NULL）"""
    return None if value is None else float(value)


def _traceid(request_id, key):
    """唯一追踪号：MP{request_id}-{coil_id 或 L{lot_id}}-{uuid12}，长度 < 128"""
    return f'MP{request_id}-{key}-{uuid.uuid4().hex[:12]}'


def _coil_rows(cursor, request_id, item):
    """A/B 料：每个卷标一行"""
    cursor.execute(
        "SELECT coil_id, lot_no, coil_length, unit FROM kr_wire_coil "
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
            'unitcode1': coil.get('unit') or item.get('unit'),
            'coil_id': coil_id,
            'lot_id': None,
            'item_id': item['id'],
        })
    return rows


def _lot_rows(cursor, request_id, item):
    """非 A/B 料：每个批次分配一行"""
    cursor.execute(
        "SELECT id, lot_no, qty FROM kr_request_item_lot "
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
            'unitcode1': item.get('unit'),
            'coil_id': None,
            'lot_id': lot['id'],
            'item_id': item['id'],
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

    # 幂等：同一单据已入队的 (coil_id, lot_id) 不重复插入
    # （uk_row 中 lot_id/coil_id 为 NULL 时 MySQL 不判重，故在此显式去重）
    cursor.execute(
        "SELECT coil_id, lot_id FROM kr_csi_interface WHERE request_id = %s AND api_name = %s",
        (request_id, API_QTY_MOVE)
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
            "(request_id, item_id, coil_id, lot_id, api_name, siteRef, item, lot, quantity, "
            " warehouse, from_location, to_location, reasoncode, unitcode1, documentnumber, "
            " is_sync, status, retry_count, traceid, createdate, createby) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            " 0, 'PENDING', 0, %s, %s, %s)",
            (
                request_id, row['item_id'], row['coil_id'], row['lot_id'],
                API_QTY_MOVE, int(siteref), row['item'], row['lot'], _num(row['quantity']),
                Config.CSI_WAREHOUSE_MAP[siteref], None, Config.CSI_TO_LOCATION,
                Config.CSI_REASON_CODE, row['unitcode1'], documentnumber,
                _traceid(request_id, trace_key), now, username,
            )
        )
        existing.add(key)
        inserted += 1

    logger.info('完成备料：CSI 接口表写入 %s 行（request_id=%s, 跳过已存在 %s 行）',
                inserted, request_id, len(rows) - inserted)
    return inserted
