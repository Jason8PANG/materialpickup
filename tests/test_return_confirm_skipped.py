# -*- coding: utf-8 -*-
"""整单确认退料：回写未命中 in_shop 时必须留痕（warning + skipped 计数）。

背景：261102001 事故 —— 复核确认时卷标已不在 in_shop，UPDATE ... AND status='in_shop'
命中 0 行被静默跳过，coil_length 停在登记值 1000（实际剩余 217.7822），
外部系统按 coil_length 取移库量导致接口表数量错误。

不打真实库：用 FakeCursor 注入 app.routes.return_bp.get_db_connection，
只校验「未命中 → 有 warning 且 JSON 带 skipped」。
"""
import logging

import pytest

from app.routes import return_bp as rb

RETURN_ID = 1000000026
COIL_ID = '261102001'
REMAIN = 217.7822


class FakeCursor:
    def __init__(self, coil_update_rowcount=0, actual_status='in_stock'):
        self.coil_update_rowcount = coil_update_rowcount
        self.actual_status = actual_status
        self.rowcount = 0
        self._rows = []
        self.log_rows = []
        self.executed = []

    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        self.executed.append(s)
        if s.startswith('SELECT * FROM kr_material_request'):
            self._rows = [{
                'id': RETURN_ID, 'request_type': 'return', 'status': 'pending_return',
                'siteref': '310', 'is_deleted': 0,
            }]
            self.rowcount = 1
        elif s.startswith('SELECT coil_id, remain_length, review_status FROM kr_return_item'):
            self._rows = [{'coil_id': COIL_ID, 'remain_length': REMAIN, 'review_status': None}]
            self.rowcount = 1
        elif s.startswith('UPDATE kr_wire_coil'):
            self._rows = []
            self.rowcount = self.coil_update_rowcount
        elif s.startswith('SELECT status FROM kr_wire_coil'):
            self._rows = [{'status': self.actual_status}] if self.actual_status else []
            self.rowcount = len(self._rows)
        elif s.startswith('INSERT INTO kr_operation_log'):
            self.log_rows.append(params)
            self._rows = []
            self.rowcount = 1
        else:
            self._rows = []
            self.rowcount = 1
        return self.rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def patch_db(monkeypatch):
    def _use(cur):
        monkeypatch.setattr(rb, 'get_db_connection', lambda: FakeConn(cur))
        return cur
    return _use


@pytest.fixture
def warehouse_client(client, login_as_warehouse):
    return login_as_warehouse


def test_confirm_return_rowcount_zero_warns_and_returns_skipped(
        warehouse_client, patch_db, caplog):
    cur = patch_db(FakeCursor(coil_update_rowcount=0, actual_status='in_stock'))

    with caplog.at_level(logging.WARNING, logger='app.routes.return_bp'):
        resp = warehouse_client.post(f'/api/returns/{RETURN_ID}/confirm')

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['confirmed'] == 0
    assert body['skipped'] == 1
    assert '未回写' in body['message']

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, '未命中 in_shop 时必须打 warning'
    assert any(s.startswith('SELECT status FROM kr_wire_coil') for s in cur.executed)
    msg = warnings[0].getMessage()
    assert COIL_ID in msg
    assert str(REMAIN) in msg
    assert 'in_shop' in msg
    assert 'in_stock' in msg  # 实际状态

    # 操作日志里也要能看出有几条没回写
    detail = cur.log_rows[0][2]
    assert '1 卷回写失败' in detail


def test_confirm_return_rowcount_one_no_warning(warehouse_client, patch_db, caplog):
    cur = patch_db(FakeCursor(coil_update_rowcount=1))

    with caplog.at_level(logging.WARNING, logger='app.routes.return_bp'):
        resp = warehouse_client.post(f'/api/returns/{RETURN_ID}/confirm')

    body = resp.get_json()
    assert body['confirmed'] == 1
    assert body['skipped'] == 0
    assert '未回写' not in body['message']
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    # 命中时不再补查实际状态
    assert not any(s.startswith('SELECT status FROM kr_wire_coil') for s in cur.executed)
