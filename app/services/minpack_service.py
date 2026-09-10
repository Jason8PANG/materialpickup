# -*- coding: utf-8 -*-
"""最小包装（minpack）申请单创建共用逻辑。

internal（app/routes/request_bp.py 的 /api/requests/minpack）与
external（app/routes/external.py 的 /api/external/minpack-request）共用，
避免两处重复实现。

约定：
  - 只负责主表 + 明细插入 + CSI 单位回填，**不 commit、不写操作日志、不校验工单**；
  - 由调用方负责事务提交与 kr_operation_log 落库（两处 detail 文案不同）；
  - minpack 无工单，明细 job_order 固定 NULL。
"""
from datetime import datetime


def create_minpack_request_core(cursor, siteref, requester, items, remark='',
                                is_urgent=0, now=None):
    """创建一张最小包装申请单，返回 request_id。

    参数：
      cursor    : 已打开的数据库游标（由调用方管理连接与事务）
      siteref   : 站点号（'310'/'410'）
      requester : 申请人（varchar(64)，调用方负责长度约束）
      items     : 明细列表，每项含 part_number / quantity（>0）/ 可选 price、stock_qty、stock_loc
      remark    : 备注（可选）
      is_urgent : 是否加急（可选）
      now       : 请求时间；缺省取当前时间（调用方可传入以复用同一时间戳）

    返回：
      request_id (int)
    """
    if now is None:
        now = datetime.now()

    # 主表 - 状态直接到 pending_prep（待备料），minpack 无需审批
    cursor.execute(
        """INSERT INTO kr_material_request
        (siteref, request_type, requester, request_time, status, remark, is_urgent)
        VALUES (%s, 'minpack', %s, %s, 'pending_prep', %s, %s)""",
        (siteref, requester, now, remark, is_urgent)
    )
    request_id = cursor.lastrowid

    # 明细（job_order 固定 NULL）
    item_data = []
    for item in items:
        qty = float(item['quantity'])
        price = float(item['price']) if item.get('price') else 0
        total = qty * price
        stock_qty = float(item['stock_qty']) if item.get('stock_qty') else None
        stock_loc = item.get('stock_loc')
        item_data.append((
            request_id, item['part_number'], qty, price, total, stock_qty, stock_loc
        ))

    cursor.executemany(
        """INSERT INTO kr_request_item
        (request_id, job_order, part_number, quantity, price, total_amount, stock_qty, stock_loc)
        VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)""",
        item_data
    )

    # 发起时自动获取物料单位（CSI），写入 kr_request_item.unit
    # 后续卷标录入/出库直接读库，不再连 CSI（单位获取只做一次）
    try:
        from app.routes.coil import _get_unit_cached
        from concurrent.futures import ThreadPoolExecutor
        unique_parts = list(dict.fromkeys(
            str(item.get('part_number') or '').strip() for item in items if item.get('part_number')
        ))
        unit_map = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_get_unit_cached, siteref, p): p for p in unique_parts}
            for fut in futures:
                try:
                    unit_map[futures[fut]] = fut.result() or None
                except Exception:
                    pass
        for p, u in unit_map.items():
            if u:
                cursor.execute(
                    "UPDATE kr_request_item SET unit = %s WHERE request_id = %s AND part_number = %s AND (unit IS NULL OR unit = '')",
                    (u, request_id, p)
                )
    except Exception as e:
        # 单位获取失败不阻断申请创建，后续可再补
        print(f"[MINPACK] 发起时获取单位失败: {e}")

    return request_id
