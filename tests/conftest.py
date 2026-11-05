# -*- coding: utf-8 -*-
"""
conftest.py - 物料领取看板系统测试基础设施 (V2)

提供 Flask 测试客户端、Mock 数据库、Mock LDAP 等公共 fixtures。
通过 monkeypatch 模拟 pymysql 数据库连接和 LDAP 认证，使测试无需真实数据库即可运行。
"""

import sys
import os
import re
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from werkzeug.security import generate_password_hash
from pymysql.err import IntegrityError

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试用密码哈希（基于 'kanban2024'）
TEST_PASSWORD_HASH = generate_password_hash('kanban2024')


# ============================================================
# Mock 数据常量 (V2 - 含 siteref)
# ============================================================

# 测试用角色映射数据 (含 siteref)
MOCK_MAPPINGS = {
    'admin': {'id': 1, 'domain_account': 'admin', 'display_name': '管理员',
              'role': 'admin', 'siteref': None, 'email': 'admin@test.com', 'is_active': 1,
              'password_hash': TEST_PASSWORD_HASH},
    'supervisor1': {'id': 2, 'domain_account': 'supervisor1', 'display_name': '主管张三',
                    'role': 'supervisor', 'siteref': '310', 'email': 'supervisor1@test.com', 'is_active': 1,
                    'password_hash': TEST_PASSWORD_HASH},
    'worker1': {'id': 3, 'domain_account': 'worker1', 'display_name': '领料员李四',
                'role': 'requester', 'siteref': '310', 'email': 'worker1@test.com', 'is_active': 1,
                'password_hash': TEST_PASSWORD_HASH},
    'warehouse1': {'id': 4, 'domain_account': 'warehouse1', 'display_name': '仓管王五',
                   'role': 'warehouse', 'siteref': '310', 'email': 'warehouse1@test.com', 'is_active': 1,
                   'password_hash': TEST_PASSWORD_HASH},
    'inactive_user': {'id': 5, 'domain_account': 'inactive_user', 'display_name': '已禁用用户',
                      'role': 'requester', 'siteref': '310', 'email': '', 'is_active': 0,
                      'password_hash': TEST_PASSWORD_HASH},
    'no_role_user': None,  # 无角色映射的用户
}

# 测试用申请单主表数据 (V2 - 无物料字段)
MOCK_REQUESTS = {
    1: {'id': 1, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 6, 1, 9, 0, 0),
        'status': 'pending_approval', 'supervisor': None,
        'approve_time': None, 'approve_comment': None,
        'warehouse_operator': None, 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 6, 1, 9, 0, 0),
        'updated_at': datetime(2024, 6, 1, 9, 0, 0)},
    2: {'id': 2, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 6, 1, 10, 0, 0),
        'status': 'pending_prep', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 6, 1, 10, 30, 0), 'approve_comment': '同意',
        'warehouse_operator': None, 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 6, 1, 10, 0, 0),
        'updated_at': datetime(2024, 6, 1, 10, 30, 0)},
    3: {'id': 3, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 6, 2, 9, 0, 0),
        'status': 'prepping', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 6, 2, 9, 30, 0), 'approve_comment': '同意',
        'warehouse_operator': 'warehouse1', 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 6, 2, 9, 0, 0),
        'updated_at': datetime(2024, 6, 2, 10, 0, 0)},
    4: {'id': 4, 'siteref': '310', 'requester': 'worker2',
        'request_time': datetime(2024, 6, 3, 9, 0, 0),
        'status': 'short', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 6, 3, 9, 30, 0), 'approve_comment': '同意',
        'warehouse_operator': 'warehouse1', 'short_reason': '库存不足',
        'short_time': datetime(2024, 6, 3, 10, 0, 0),
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 6, 3, 9, 0, 0),
        'updated_at': datetime(2024, 6, 3, 10, 0, 0)},
    5: {'id': 5, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 6, 4, 9, 0, 0),
        'status': 'ready_pickup', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 6, 4, 9, 30, 0), 'approve_comment': '同意',
        'warehouse_operator': 'warehouse1', 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 6, 4, 9, 0, 0),
        'updated_at': datetime(2024, 6, 4, 10, 0, 0)},
    6: {'id': 6, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 5, 1, 9, 0, 0),
        'status': 'completed', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 5, 1, 10, 0, 0), 'approve_comment': '同意',
        'warehouse_operator': 'warehouse1', 'short_reason': None, 'short_time': None,
        'signer': 'worker1', 'sign_time': datetime(2024, 5, 1, 11, 0, 0),
        'remark': None, 'is_deleted': 0,
        'created_at': datetime(2024, 5, 1, 9, 0, 0),
        'updated_at': datetime(2024, 5, 1, 11, 0, 0)},
    7: {'id': 7, 'siteref': '310', 'requester': 'worker2',
        'request_time': datetime(2024, 5, 2, 9, 0, 0),
        'status': 'rejected', 'supervisor': 'supervisor1',
        'approve_time': datetime(2024, 5, 2, 9, 30, 0), 'approve_comment': '库存充足，无需补料',
        'warehouse_operator': None, 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 0, 'created_at': datetime(2024, 5, 2, 9, 0, 0),
        'updated_at': datetime(2024, 5, 2, 9, 30, 0)},
    8: {'id': 8, 'siteref': '310', 'requester': 'worker1',
        'request_time': datetime(2024, 5, 3, 9, 0, 0),
        'status': 'pending_approval', 'supervisor': None,
        'approve_time': None, 'approve_comment': None,
        'warehouse_operator': None, 'short_reason': None, 'short_time': None,
        'signer': None, 'sign_time': None, 'remark': None,
        'is_deleted': 1, 'created_at': datetime(2024, 5, 3, 9, 0, 0),
        'updated_at': datetime(2024, 5, 3, 9, 0, 0)},
}

# 测试用明细数据
MOCK_ITEMS_BY_REQUEST = {
    1: [
        {'id': 1, 'request_id': 1, 'job_order': 'WO-2024-001', 'part_number': 'PN-A001',
         'quantity': 10.00, 'price': 5.50, 'total_amount': 55.00, 'stock_qty': 100.00,
         'replenish_reason': '报废', 'replenish_reason_other': None,
         'created_at': datetime(2024, 6, 1, 9, 0, 0), 'updated_at': datetime(2024, 6, 1, 9, 0, 0)},
    ],
    2: [
        {'id': 2, 'request_id': 2, 'job_order': 'WO-2024-002', 'part_number': 'PN-A002',
         'quantity': 20.00, 'price': 3.00, 'total_amount': 60.00, 'stock_qty': 50.00,
         'replenish_reason': '不良', 'replenish_reason_other': None,
         'created_at': datetime(2024, 6, 1, 10, 0, 0), 'updated_at': datetime(2024, 6, 1, 10, 30, 0)},
    ],
    3: [
        {'id': 3, 'request_id': 3, 'job_order': 'WO-2024-003', 'part_number': 'PN-A003',
         'quantity': 5.00, 'price': 100.00, 'total_amount': 500.00, 'stock_qty': 200.00,
         'replenish_reason': '来料不足', 'replenish_reason_other': None,
         'created_at': datetime(2024, 6, 2, 9, 0, 0), 'updated_at': datetime(2024, 6, 2, 10, 0, 0)},
    ],
    4: [
        {'id': 4, 'request_id': 4, 'job_order': 'WO-2024-004', 'part_number': 'PN-A004',
         'quantity': 3.00, 'price': 200.00, 'total_amount': 600.00, 'stock_qty': 0.00,
         'replenish_reason': '报废', 'replenish_reason_other': None,
         'created_at': datetime(2024, 6, 3, 9, 0, 0), 'updated_at': datetime(2024, 6, 3, 10, 0, 0)},
    ],
    5: [
        {'id': 5, 'request_id': 5, 'job_order': 'WO-2024-005', 'part_number': 'PN-A005',
         'quantity': 8.00, 'price': 12.50, 'total_amount': 100.00, 'stock_qty': 30.00,
         'replenish_reason': '其他', 'replenish_reason_other': '紧急补料',
         'created_at': datetime(2024, 6, 4, 9, 0, 0), 'updated_at': datetime(2024, 6, 4, 10, 0, 0)},
    ],
}

# 默认明细行（用于没有明细的请求）
DEFAULT_MOCK_ITEMS = []

# 批次分配（kr_request_item_lot）：非 A/B 物料完成备料前必须维护批次。
# 申请单 3 的明细 3（PN-A003，5.00）配一条等量批次，保持既有完成备料用例通过。
MOCK_ITEM_LOTS = {
    3: [{'id': 1, 'request_id': 3, 'item_id': 3, 'lot_no': 'LOT-TEST-0001',
         'qty': 5.00, 'loc': 'RM-A01', 'siteref': '310'}],
}

# ============================================================
# 线卷模块 mock 数据（kr_wire_coil / kr_wire_coil_consumption）
# ============================================================

# 线卷测试专用申请单：minpack + prepping（siteref=310，可用于卷标录入/出库登记）
MOCK_REQUESTS[100] = {
    'id': 100, 'siteref': '310', 'requester': 'worker1', 'request_type': 'minpack',
    'request_time': datetime(2026, 8, 14, 9, 0, 0), 'status': 'prepping',
    'supervisor': 'supervisor1', 'approve_time': datetime(2026, 8, 14, 9, 30, 0),
    'approve_comment': '同意', 'warehouse_operator': 'warehouse1',
    'short_reason': None, 'short_time': None, 'signer': None, 'sign_time': None,
    'remark': None, 'is_deleted': 0, 'created_at': datetime(2026, 8, 14, 9, 0, 0),
    'updated_at': datetime(2026, 8, 14, 9, 30, 0),
}
# 线卷测试专用申请单：minpack 但非 prepping（用于校验仅 prepping 可录入）
MOCK_REQUESTS[101] = {
    'id': 101, 'siteref': '310', 'requester': 'worker1', 'request_type': 'minpack',
    'request_time': datetime(2026, 8, 14, 10, 0, 0), 'status': 'ready_pickup',
    'supervisor': 'supervisor1', 'approve_time': datetime(2026, 8, 14, 10, 30, 0),
    'approve_comment': '同意', 'warehouse_operator': 'warehouse1',
    'short_reason': None, 'short_time': None, 'signer': None, 'sign_time': None,
    'remark': None, 'is_deleted': 0, 'created_at': datetime(2026, 8, 14, 10, 0, 0),
    'updated_at': datetime(2026, 8, 14, 10, 30, 0),
}
# 申请单 100 明细：A/B 开头物料（强制卷标）+ 普通开头物料（可选）
MOCK_ITEMS_BY_REQUEST[100] = [
    {'id': 1001, 'request_id': 100, 'job_order': None, 'part_number': 'A123456',
     'quantity': 10.00, 'price': 5.50, 'total_amount': 55.00, 'stock_qty': 100.00,
     'replenish_reason': '最小包装', 'replenish_reason_other': None,
     'created_at': datetime(2026, 8, 14, 9, 0, 0), 'updated_at': datetime(2026, 8, 14, 9, 0, 0)},
    {'id': 1002, 'request_id': 100, 'job_order': None, 'part_number': 'B999999',
     'quantity': 5.00, 'price': 3.00, 'total_amount': 15.00, 'stock_qty': 50.00,
     'replenish_reason': '最小包装', 'replenish_reason_other': None,
     'created_at': datetime(2026, 8, 14, 9, 0, 0), 'updated_at': datetime(2026, 8, 14, 9, 0, 0)},
    {'id': 1003, 'request_id': 100, 'job_order': None, 'part_number': 'W0303408',
     'quantity': 8.00, 'price': 12.50, 'total_amount': 100.00, 'stock_qty': 30.00,
     'replenish_reason': '最小包装', 'replenish_reason_other': None,
     'created_at': datetime(2026, 8, 14, 9, 0, 0), 'updated_at': datetime(2026, 8, 14, 9, 0, 0)},
]

# 内存线卷库存表（每卷一行）/ 消耗记录表（模拟数据库）
MOCK_WIRE_COILS = []
MOCK_CONSUMPTIONS = []
_wire_coil_id_counter = [1000]
_consumption_id_counter = [1000]

# Mock 操作日志
MOCK_LOGS = [
    {'id': 1, 'request_id': 1, 'operator': 'worker1', 'action': 'SUBMIT',
     'detail': '提交领料申请: PN-A001 x 10', 'ip_address': '127.0.0.1',
     'created_at': datetime(2024, 6, 1, 9, 0, 0)},
    {'id': 2, 'request_id': 2, 'operator': 'worker1', 'action': 'SUBMIT',
     'detail': '提交领料申请: PN-A002 x 20', 'ip_address': '127.0.0.1',
     'created_at': datetime(2024, 6, 1, 10, 0, 0)},
    {'id': 3, 'request_id': 2, 'operator': 'supervisor1', 'action': 'APPROVE',
     'detail': '审批通过: 同意', 'ip_address': '127.0.0.1',
     'created_at': datetime(2024, 6, 1, 10, 30, 0)},
]

# Mock 审批令牌
MOCK_TOKENS = {
    'valid-token-123': {'id': 1, 'request_id': 1, 'token': 'valid-token-123',
                        'supervisor': 'supervisor1', 'is_used': 0,
                        'expires_at': datetime(2027, 12, 31, 23, 59, 59),
                        'created_at': datetime(2024, 6, 1, 9, 0, 0)},
}

# 请求 ID 自增计数器
request_id_counter = [100]
item_id_counter = [100]


# ============================================================
# Mock Cursor
# ============================================================

class MockCursor:
    """模拟 pymysql 游标 (V2 - 支持 siteref 和 kr_request_item)"""

    def __init__(self):
        self._rows = []
        self._row_index = 0
        self.lastrowid = None
        self._sql_log = []
        self._inserted_data = {}
        # 单用例可开：置 True 后 UPDATE kr_material_request 的状态会回写 MOCK_REQUESTS，
        # 便于断言接口调用后的状态流转；默认关闭保持既有用例共享 mock 状态的语义。
        self.track_request_status = False
        # pymysql 游标的 rowcount：默认 0，UPDATE 分支按实际影响行数回写
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._sql_log.append((sql, params))
        sql_upper = sql.upper().strip()
        sql_lower = sql.lower().strip()

        # ================= 线卷模块（kr_wire_coil / kr_wire_coil_consumption） =================
        if 'kr_wire_coil_consumption' in sql:
            if sql_upper.startswith('INSERT'):
                m = re.search(r'\(([^)]+)\)\s*VALUES', sql, re.IGNORECASE)
                col_names = [c.strip().strip('`') for c in m.group(1).split(',')] if m else []
                rec = dict(zip(col_names, params)) if col_names else {}
                _consumption_id_counter[0] += 1
                rec['id'] = _consumption_id_counter[0]
                MOCK_CONSUMPTIONS.append(rec)
                self.lastrowid = rec['id']
                self._rows = []
                self._row_index = 0
                return 1
            if 'COALESCE(SUM(out_length)' in sql:
                cid = params[0] if params else None
                used = sum(float(c.get('out_length') or 0) for c in MOCK_CONSUMPTIONS
                           if c.get('coil_id') == cid and c.get('consume_type') == 'issue')
                self._rows = [{'used': used}]
                self._row_index = 0
                return 1
            if 'JOIN kr_wire_coil w' in sql:
                rid = params[0] if params else None
                coil_ids = {w.get('coil_id') for w in MOCK_WIRE_COILS if w.get('request_id') == rid}
                self._rows = [dict(c) for c in MOCK_CONSUMPTIONS if c.get('coil_id') in coil_ids]
                self._row_index = 0
                return len(self._rows)
            self._rows = []
            self._row_index = 0
            return 0

        if 'kr_inventory_count' in sql:
            # R9.5 盘点锁定检查：测试环境默认无活跃盘点单 → 返回 0
            self._rows = [{'n': 0}]
            self._row_index = 0
            return 1

        if 'kr_wire_coil' in sql:
            if sql_upper.startswith('INSERT'):
                # 真实 SQL: INSERT ... (coil_id, part_number, lot_no, coil_length, unit, status,
                #                      request_id, siteref, operator, item_id, is_initial_half,
                #                      loc, remark, created_at)
                #           VALUES (%s, %s, %s, %s, %s, 'in_stock', %s, %s, %s, %s, %s, %s, %s, %s)
                # status 为硬编码 'in_stock'，参数共 13 个：
                # params[0]=coil_id [1]=part_number [2]=lot_no [3]=coil_length [4]=unit [5]=request_id
                # [6]=siteref [7]=operator [8]=item_id [9]=is_initial_half [10]=loc [11]=remark [12]=created_at
                coil_id = params[0]
                if any(w.get('coil_id') == coil_id for w in MOCK_WIRE_COILS):
                    raise IntegrityError(1062, f"Duplicate entry '{coil_id}' for key 'uk_coil_id'")
                _wire_coil_id_counter[0] += 1
                new_id = _wire_coil_id_counter[0]
                MOCK_WIRE_COILS.append({
                    'id': new_id, 'coil_id': coil_id, 'part_number': params[1],
                    'lot_no': params[2], 'coil_length': params[3], 'unit': params[4],
                    'status': 'in_stock', 'request_id': params[5], 'siteref': params[6],
                    'operator': params[7], 'item_id': params[8],
                    'is_initial_half': params[9], 'loc': params[10],
                    'remark': params[11], 'created_at': params[12],
                })
                self.lastrowid = new_id
                self._rows = []
                self._row_index = 0
                return 1
            if 'MAX(coil_id)' in sql:
                # _gen_next_id 取号：含软删最大卷号（coil_id LIKE 'YYMMDD%'）
                prefix = ''
                if params and params[0]:
                    prefix = str(params[0])
                    if prefix.endswith('%'):
                        prefix = prefix[:-1]
                matches = [str(w.get('coil_id', '')) for w in MOCK_WIRE_COILS
                           if str(w.get('coil_id', '')).startswith(prefix)]
                self._rows = [{'max_id': max(matches)}] if matches else [{'max_id': ''}]
                self._row_index = 0
                return 1
            if sql_upper.startswith('SELECT COUNT(*)'):
                if 'FROM kr_wire_coil s' in sql:
                    # list_coils 分页总数（非 admin 带 s.siteref = %s 站点过滤）
                    rows = [dict(w) for w in MOCK_WIRE_COILS]
                    if 'siteref' in sql and params:
                        site = params[0]
                        rows = [w for w in rows if w.get('siteref') == site]
                    self._rows = [{'total': len(rows)}]
                else:
                    # 其余 kr_wire_coil COUNT（按 request_id 前缀近似），保留原语义，返回 cnt 键
                    prefix = ''
                    if params and params[0] is not None:
                        prefix = str(params[0])
                    cnt = sum(1 for w in MOCK_WIRE_COILS if str(w.get('coil_id', '')).startswith(prefix))
                    self._rows = [{'cnt': cnt}]
                self._row_index = 0
                return 1
            if sql_upper.startswith('UPDATE'):
                if "status = 'in_shop'" in sql:
                    # sign 取料完成：request 关联的在库卷标 → 在车间
                    rid = params[-1] if params else None
                    affected = 0
                    for w in MOCK_WIRE_COILS:
                        if w.get('request_id') == rid and w.get('status') == 'in_stock':
                            w['status'] = 'in_shop'
                            affected += 1
                    self.rowcount = affected
                    return affected
                new_status = 'issued'  # 现有代码仅用于置 issued
                cid = params[-1] if params else None
                self.rowcount = 0
                for w in MOCK_WIRE_COILS:
                    if w.get('id') == cid:
                        w['status'] = new_status
                        self.rowcount += 1
                return 1
            if 'WHERE coil_id = %s AND request_id = %s' in sql:
                cid, rid = params[0], params[1]
                self._rows = [dict(w) for w in MOCK_WIRE_COILS
                              if w.get('coil_id') == cid and w.get('request_id') == rid]
                self._row_index = 0
                return len(self._rows)
            if 'WHERE request_id = %s AND coil_id = %s' in sql:
                rid, cid = params[0], params[1]
                self._rows = [dict(w) for w in MOCK_WIRE_COILS
                              if w.get('request_id') == rid and w.get('coil_id') == cid]
                self._row_index = 0
                return len(self._rows)
            if 'WHERE coil_id = %s' in sql:
                cid = params[0]
                self._rows = [dict(w) for w in MOCK_WIRE_COILS if w.get('coil_id') == cid]
                self._row_index = 0
                return len(self._rows)
            if 'WHERE coil_id IN' in sql:
                if 'siteref = %s' in sql:
                    site = params[-1]
                    ids = params[:-1]
                else:
                    site = None
                    ids = params
                self._rows = [dict(w) for w in MOCK_WIRE_COILS
                              if w.get('coil_id') in ids and (site is None or w.get('siteref') == site)]
                self._row_index = 0
                return len(self._rows)
            if 'FROM kr_wire_coil s' in sql:
                rows = [dict(w) for w in MOCK_WIRE_COILS]
                if 'siteref' in sql and params:
                    # list_coils 站点隔离计数/数据查询（非 admin 带 s.siteref = %s）
                    site = params[0]
                    rows = [w for w in rows if w.get('siteref') == site]
                if sql_upper.startswith('SELECT COUNT(*)'):
                    self._rows = [{'total': len(rows)}]
                else:
                    self._rows = rows
                self._row_index = 0
                return len(self._rows)
            if 'WHERE request_id =' in sql:
                rid = params[0]
                self._rows = [dict(w) for w in MOCK_WIRE_COILS if w.get('request_id') == rid]
                self._row_index = 0
                return len(self._rows)
            self._rows = []
            self._row_index = 0
            return 0

        # INSERT INTO kr_request_item
        if sql_upper.startswith('INSERT') and 'kr_request_item' in sql:
            item_id_counter[0] += 1
            self.lastrowid = item_id_counter[0]
            self._inserted_data['last_insert'] = params
            return 1

        # INSERT INTO kr_material_request
        if sql_upper.startswith('INSERT') and 'kr_material_request' in sql:
            request_id_counter[0] += 1
            self.lastrowid = request_id_counter[0]
            self._inserted_data['last_insert'] = params
            # 记录插入的 request 以便后续查询
            if params:
                new_id = request_id_counter[0]
                MOCK_REQUESTS[new_id] = {
                    'id': new_id, 'siteref': params[0], 'requester': params[1],
                    'request_time': datetime.now(), 'status': 'pending_approval',
                    'supervisor': None, 'approve_time': None, 'approve_comment': None,
                    'warehouse_operator': None, 'short_reason': None, 'short_time': None,
                    'signer': None, 'sign_time': None, 'remark': params[2],
                    'is_deleted': 0, 'created_at': datetime.now(), 'updated_at': datetime.now()
                }
            return 1

        # SELECT COUNT(*) - 简单模拟
        if sql_upper.startswith('SELECT COUNT(*)'):
            self._rows = [{'total': 3}]
            self._row_index = 0
            return 1

        # 查找角色映射
        if 'kr_role_mapping' in sql and sql_upper.startswith('SELECT'):
            if params:
                param = params[0]
                if isinstance(param, (int, float)):
                    param_int = int(param)
                    for v in MOCK_MAPPINGS.values():
                        if v and v.get('id') == param_int:
                            self._rows = [dict(v)]
                            break
                    else:
                        self._rows = []
                else:
                    mapping = MOCK_MAPPINGS.get(param)
                    if mapping and mapping.get('is_active'):
                        self._rows = [dict(mapping)]
                    else:
                        self._rows = []
            else:
                self._rows = [dict(v) for v in MOCK_MAPPINGS.values() if v]
            self._row_index = 0
            return len(self._rows)

        # 查找申请单详情 (包含子查询)
        if 'kr_material_request' in sql and sql_upper.startswith('SELECT') and 'COUNT' not in sql_upper:
            if 'kr_approval_token' in sql:
                # 令牌 + 申请表联合查询
                token_val = params[0] if params else None
                token = MOCK_TOKENS.get(token_val)
                if token and token['is_used'] == 0:
                    req = MOCK_REQUESTS.get(token['request_id'])
                    if req:
                        result = dict(token)
                        result.update(dict(req))
                        result['req_status'] = req['status']
                        self._rows = [result]
                    else:
                        self._rows = []
                else:
                    self._rows = []
                self._row_index = 0
                return len(self._rows)

            # 查找 kr_request_item 明细
            if 'kr_request_item' in sql and 'WHERE request_id =' in sql:
                rid = params[0] if params else None
                self._rows = [dict(v) for v in MOCK_ITEMS_BY_REQUEST.get(rid, DEFAULT_MOCK_ITEMS)]
                self._row_index = 0
                return len(self._rows)

            # 按 ID 查单条
            if 'WHERE id =' in sql and 'AND is_deleted' in sql:
                rid = params[0] if params else None
                if isinstance(rid, (int, float)):
                    rid = int(rid)
                req = MOCK_REQUESTS.get(rid)
                if req and not req.get('is_deleted', 0):
                    self._rows = [dict(req)]
                else:
                    self._rows = []
                self._row_index = 0
                return len(self._rows)

            # 列表查询
            all_reqs = [dict(v) for v in MOCK_REQUESTS.values()
                       if not v.get('is_deleted', 0)]
            self._rows = all_reqs
            self._row_index = 0
            return len(self._rows)

        # 批次分配表（kr_request_item_lot）：完成备料前的批次校验聚合查询
        if 'kr_request_item_lot' in sql:
            if 'COALESCE(SUM(qty)' in sql:
                rid = params[0] if params else None
                item_ids = set(params[1:]) if params else set()
                agg = {}
                for lot in MOCK_ITEM_LOTS.get(rid, []):
                    if lot['item_id'] not in item_ids:
                        continue
                    total, cnt = agg.get(lot['item_id'], (0.0, 0))
                    qty = float(lot.get('qty') or 0)
                    agg[lot['item_id']] = (total + qty, cnt + (1 if qty > 0 else 0))
                self._rows = [{'item_id': k, 'total_qty': v[0], 'positive_cnt': v[1]}
                              for k, v in agg.items()]
                self._row_index = 0
                return len(self._rows)
            if sql_upper.startswith('SELECT'):
                rid = params[0] if params else None
                rows = [dict(lot) for lot in MOCK_ITEM_LOTS.get(rid, [])]
                if params and len(params) > 1:
                    rows = [r for r in rows if r['item_id'] == params[1]]
                self._rows = rows
                self._row_index = 0
                return len(self._rows)
            self._rows = []
            self._row_index = 0
            return 0

        # 申请单明细查询（纯 kr_request_item，不含 kr_material_request 联合查询）
        if 'kr_request_item' in sql and 'WHERE request_id =' in sql:
            rid = params[0] if params else None
            self._rows = [dict(v) for v in MOCK_ITEMS_BY_REQUEST.get(rid, DEFAULT_MOCK_ITEMS)]
            self._row_index = 0
            return len(self._rows)

        # 令牌表查询
        if 'kr_approval_token' in sql and sql_upper.startswith('SELECT'):
            self._rows = [dict(v) for v in MOCK_TOKENS.values()]
            self._row_index = 0
            return len(self._rows)

        # UPDATE kr_material_request：状态变更回写 mock（需用例开启 track_request_status）
        if self.track_request_status and 'kr_material_request' in sql and sql_upper.startswith('UPDATE'):
            m = re.search(r"status\s*=\s*'([^']*)'", sql)
            rid = params[-1] if params else None
            if m and rid in MOCK_REQUESTS:
                MOCK_REQUESTS[rid]['status'] = m.group(1)
            self.rowcount = 1
            return 1

        # UPDATE
        if sql_upper.startswith('UPDATE'):
            self.rowcount = 1
            return 1

        # DELETE
        if sql_upper.startswith('DELETE'):
            return 1

        # 操作日志查询
        if 'kr_operation_log' in sql and sql_upper.startswith('SELECT') and 'COUNT' not in sql_upper:
            self._rows = [dict(l) for l in MOCK_LOGS]
            self._row_index = 0
            return len(self._rows)

        # 操作日志插入（kr_operation_log）
        if sql_upper.startswith('INSERT') and 'kr_operation_log' in sql:
            if params and len(params) >= 6:
                MOCK_LOGS.append({
                    'id': (max((l.get('id') or 0) for l in MOCK_LOGS) + 1) if MOCK_LOGS else 1,
                    'request_id': params[0], 'operator': params[1], 'action': params[2],
                    'detail': params[3], 'ip_address': params[4],
                    'created_at': params[5] if len(params) > 5 else datetime.now(),
                })
            return 1

        # executemany 批量插入
        if sql_upper.startswith('INSERT') and hasattr(params, '__iter__'):
            if params and isinstance(params[0], (list, tuple)):
                item_id_counter[0] += len(params)
                self.lastrowid = item_id_counter[0]
            return len(params) if params else 0

        self._rows = []
        self._row_index = 0
        return 0

    def executemany(self, sql, params):
        self._sql_log.append((sql, params))
        if params:
            item_id_counter[0] += len(params)
            self.lastrowid = item_id_counter[0]
        return len(params) if params else 0

    def fetchone(self):
        if self._row_index < len(self._rows):
            row = self._rows[self._row_index]
            self._row_index += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows
        self._row_index = len(self._rows)
        return rows

    def close(self):
        pass

    def _get_status_label(self, status):
        labels = {
            'pending_approval': '待审批', 'rejected': '已驳回',
            'pending_prep': '待备料', 'prepping': '备料中',
            'short': '缺料', 'ready_pickup': '待取料', 'completed': '已完成'
        }
        return labels.get(status, status)


class MockDB:
    """模拟数据库连接"""

    def __init__(self):
        self.cursor_obj = MockCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def app():
    """创建 Flask 测试应用"""
    # 必须先于 create_app 修改 Config，Session(app) 在 create_app 内即按配置初始化后端。
    # 使用内存会话缓存（cachelib/simple），避免 filesystem 后端在沙箱/CI 环境
    # 清理 session 临时文件时因回收站不可用而抛 OSError，导致响应 500。
    from app.config import Config
    Config.SESSION_TYPE = 'cachelib'
    Config.SESSION_CACHELIB_TYPE = 'simple'
    from app import create_app
    app = create_app()
    app.config.update({
        'TESTING': True,
        'SECRET_KEY': 'test-secret-key',
        'SERVER_NAME': 'localhost:5000',
    })
    return app


@pytest.fixture
def client(app):
    """Flask 测试客户端"""
    return app.test_client()


@pytest.fixture(autouse=True)
def csi_alert_production_semantics(monkeypatch):
    """单测描述的是生产行为，不受开发机 .env 的 APP_ENV=local 影响。

    Config.CSI_ALERT_ENABLED 现在是「显式开关 AND 生产环境」的组合结果，
    而本地 .env 常设 APP_ENV=local → False，会让「失败要发告警邮件」这类
    断言全部失效。测试里统一强制为 True（等价生产环境），保证用例与机器无关。
    """
    from app.config import Config
    monkeypatch.setattr(Config, 'CSI_ALERT_ENABLED', True, raising=False)


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    """测试绝不发真实邮件。

    csi_alert_production_semantics 会把 CSI_ALERT_ENABLED 强制为 True（描述生产行为），
    个别用例若漏了 patch('...send_email') 就会真的通过 SMTP 发出去（已发生事故：
    2026-11-05 向 jason.pang@nai-group.com 发了真实告警）。这里在**所有模块入口**上
    统一替换为哨兵：记录调用但绝不触网。需要断言发信行为的用例，自身 patch 会覆盖本 fixture。
    """
    calls = []

    def _fake_send_email(to_email, subject, html_content, **kwargs):
        calls.append({'to': to_email, 'subject': subject, 'kwargs': kwargs})
        return True

    # email_service.send_email 是源头；各模块多用 `from ... import send_email`，
    # 必须在「已 import 的模块属性」上也替换，否则改源头无效。
    # 显式列出消费方（grep "from app.services.email_service import send_email"）：
    #   app/csi_sync_worker.py  —— 模块顶层 from-import（失败告警/主流程）
    #   app/routes/request_bp.py、app/services/csi_alert_service.py —— 函数内 import，
    #                                                               调用时取源头，改源头即生效
    monkeypatch.setattr('app.services.email_service.send_email', _fake_send_email)
    monkeypatch.setattr('app.csi_sync_worker.send_email', _fake_send_email, raising=False)
    # 兜底：任何已经 import 的 app.* 模块里若持有 send_email 名字（顶层 from-import），一并替换。
    # 只看 sys.modules、不再导入新模块，避免 walk_packages 的导入副作用/耗时。
    import sys
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith('app.') and getattr(mod, 'send_email', None) is not None \
                and mod_name != 'app.services.email_service':
            monkeypatch.setattr(mod, 'send_email', _fake_send_email, raising=False)
    return calls


@pytest.fixture(autouse=True)
def mock_db(monkeypatch):
    """Mock 数据库连接 - 直接 monkeypatch pymysql.connect"""
    mock_db_instance = MockDB()
    # 重置线卷模块内存数据，保证用例隔离
    MOCK_WIRE_COILS.clear()
    MOCK_CONSUMPTIONS.clear()
    _wire_coil_id_counter[0] = 1000
    _consumption_id_counter[0] = 1000

    def _mock_connect(*args, **kwargs):
        return mock_db_instance
    
    monkeypatch.setattr('pymysql.connect', _mock_connect)
    return mock_db_instance


@pytest.fixture
def auth_headers(client):
    """登录辅助函数 - 返回登录后的 session (V2 - 含 siteref)"""

    def _login(username):
        with client.session_transaction() as sess:
            mapping = MOCK_MAPPINGS.get(username)
            if mapping and mapping.get('is_active', 1):
                siteref = mapping.get('siteref')
                sess['user'] = {
                    'username': mapping['domain_account'],
                    'display_name': mapping['display_name'],
                    'role': mapping['role'],
                    'email': mapping['email'],
                    'siteref': siteref or '310',
                    'original_siteref': siteref,
                    'available_sites': ['310', '410'] if mapping['role'] == 'admin' else [siteref] if siteref else []
                }
                sess.permanent = True
        return client

    return _login


@pytest.fixture
def login_as_supervisor(client, auth_headers):
    """以主管身份登录"""
    return auth_headers('supervisor1')


@pytest.fixture
def login_as_requester(client, auth_headers):
    """以领料员身份登录"""
    return auth_headers('worker1')


@pytest.fixture
def login_as_warehouse(client, auth_headers):
    """以仓库人员身份登录"""
    return auth_headers('warehouse1')


@pytest.fixture
def login_as_admin(client, auth_headers):
    """以管理员身份登录"""
    return auth_headers('admin')
