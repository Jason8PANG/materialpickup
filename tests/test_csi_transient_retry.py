# -*- coding: utf-8 -*-
"""网络类错误（transient）同 traceId 自动重试的回归测试

需求：
  业务错误（failed：400/409/校验不过）→ 立即 FAILED_MANUAL（不重试）
  网络类错误（transient：超时/非 JSON/未知状态等）→ 同一 traceId 最多重试 3 次，
  达上限才转 FAILED_MANUAL + 告警邮件（文案区分「网络异常」）
  pending（平台处理中）→ 保持现状

全程 mock requests.post，绝不真实过账；仓储使用内存替身，不写真实库。
"""
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import csi_sync_worker as w  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.csi_service import BF_MSG_ALREADY_ZERO  # noqa: E402

TRACE = 'MP2967-261016004-ce830e42a267'


# ====================================================================== #
#  替身
# ====================================================================== #
class FakeRepo:
    """按 kr_csi_interface 行语义模拟的最小仓储（内存）"""

    def __init__(self, row):
        self.rows = {row['id']: dict(row)}

    def fetch_candidates(self, limit=None):
        return [dict(r) for r in self.rows.values()
                if r.get('is_sync') == 0
                and r.get('status') in ('PENDING', 'QUEUED', 'SUBMITTING')]

    def claim(self, row_id):
        row = self.rows.get(row_id)
        if not row or row.get('is_sync') != 0:
            return False
        row['status'] = 'SUBMITTING'
        return True

    def update(self, row_id, **fields):
        self.rows[row_id].update(fields)

    def row(self):
        return self.rows[1]


def _row(**over):
    base = {
        'id': 1, 'request_id': 2967, 'item_id': 4403, 'coil_id': '261016004', 'lot_id': None,
        'siteRef': 410, 'item': 'A000871', 'lot': '202510210000006', 'quantity': 6.5,
        'warehouse': 'M1M1', 'from_location': 'A-01-01', 'to_location': 'Floor-Core',
        'reasoncode': 'C1N', 'unitcode1': 'FT', 'documentnumber': 'MP2967',
        'status': 'PENDING', 'retry_count': 0, 'transient_count': 0, 'traceid': TRACE,
        'is_sync': 0, 'last_poll_at': None,
    }
    base.update(over)
    return base


def _resp(code=200, payload=None):
    r = MagicMock()
    r.status_code = code
    r.text = ''
    if payload is None:
        r.json.side_effect = ValueError('not json')
    else:
        r.json.return_value = payload
    return r


def _ok(status='SUCCESS', ion=None):
    data = {'status': status}
    if ion is not None:
        data['ion'] = ion
    return {'success': True, 'data': data}


def _failed(err='科学计数法解析失败'):
    return {'success': False, 'data': {'status': 'FAILED', 'error': err}}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(Config, 'CSI_SYNC_POLL_INTERVAL', 0)
    monkeypatch.setattr(Config, 'CSI_SYNC_POLL_ATTEMPTS', 4)
    monkeypatch.setattr(Config, 'CSI_SYNC_MAX_TRANSIENT', 3)
    monkeypatch.setattr(Config, 'CSI_SYTELINE_TOKEN', 'slt_test')
    monkeypatch.setattr(Config, 'CSI_ALERT_EMAIL', 'jason.pang@nai-group.com')
    client = MagicMock()
    client.get_lot_location.return_value = None
    client.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
    monkeypatch.setattr(w, 'CSIClient', MagicMock(return_value=client))
    yield client


def _run(repo):
    return w.run_once(repo=repo, sleep_func=lambda s: None)


# ====================================================================== #
#  1. 第 1 次 transient：计数 +1，traceId/状态不变
# ====================================================================== #
class TestTransientRetry:
    def test_first_timeout_keeps_trace_and_counts(self):
        repo = FakeRepo(_row())
        with patch('app.csi_sync_worker._http_post',
                   side_effect=requests.Timeout('read timeout')), \
             patch('app.csi_sync_worker.send_email'):
            stats = _run(repo)

        row = repo.row()
        assert row['transient_count'] == 1
        assert row['retry_count'] == 0          # 网络类不计入 retry_count
        assert row['status'] == 'SUBMITTING' and row['is_sync'] == 0
        assert row['traceid'] == TRACE
        assert '请求异常' in row['errormessage']
        assert stats['transient'] == 1 and stats['manual'] == 0

    # ---------------------------------------------------------------- #
    #  2. 连续 3 次 transient → 转人工，traceId 三次完全一致
    # ---------------------------------------------------------------- #
    def test_three_transients_then_manual_with_same_trace(self):
        repo = FakeRepo(_row())
        with patch('app.csi_sync_worker._http_post',
                   side_effect=requests.Timeout('read timeout')) as post, \
             patch('app.csi_sync_worker.send_email') as mail:
            for _ in range(3):
                _run(repo)

        row = repo.row()
        assert post.call_count == 3
        assert {c.kwargs['json']['traceId'] for c in post.call_args_list} == {TRACE}
        assert row['is_sync'] == 2 and row['status'] == 'FAILED_MANUAL'
        assert '网络/服务异常重试 3 次仍失败' in row['errormessage']
        assert row['transient_count'] == 0       # 终态后清零
        assert row['retry_count'] == 1           # 只有一次「终态失败」记录
        assert row['traceid'] == TRACE
        assert mail.call_count == 1

        # 转人工后不再被扫描
        assert _run(repo)['scanned'] == 0

    # ---------------------------------------------------------------- #
    #  3. 2 次 transient 后成功 → SUCCESS 且计数清零
    # ---------------------------------------------------------------- #
    def test_success_after_two_transients_resets(self):
        repo = FakeRepo(_row())
        responses = [
            requests.Timeout('read timeout'),
            requests.Timeout('read timeout'),
            _resp(200, _ok('SUCCESS', {'Success': True, 'Message': 'ok'})),
        ]
        with patch('app.csi_sync_worker._http_post', side_effect=responses), \
             patch('app.csi_sync_worker.send_email'):
            _run(repo)
            assert repo.row()['transient_count'] == 1
            _run(repo)
            assert repo.row()['transient_count'] == 2
            stats = _run(repo)

        row = repo.row()
        assert row['is_sync'] == 1 and row['status'] == 'SUCCESS'
        assert row['transient_count'] == 0
        assert row['errormessage'] is None
        assert stats['success'] == 1 and stats['manual'] == 0

    # ---------------------------------------------------------------- #
    #  4. 业务失败（400）→ 立即转人工，不累加 transient_count
    # ---------------------------------------------------------------- #
    def test_business_failure_is_immediate(self):
        repo = FakeRepo(_row())
        with patch('app.csi_sync_worker._http_post',
                   return_value=_resp(400, _failed('存在重复数据'))) as post, \
             patch('app.csi_sync_worker.send_email'):
            stats = _run(repo)

        row = repo.row()
        assert post.call_count == 1              # 不重试
        assert row['is_sync'] == 2 and row['status'] == 'FAILED_MANUAL'
        assert row['transient_count'] == 0
        assert row['retry_count'] == 1
        assert stats['manual'] == 1 and stats['transient'] == 0

    # ---------------------------------------------------------------- #
    #  5. 邮件文案区分网络异常 / 业务失败
    # ---------------------------------------------------------------- #
    def test_alert_email_copy_for_network_failure(self):
        repo = FakeRepo(_row())
        with patch('app.csi_sync_worker._http_post',
                   side_effect=requests.Timeout('read timeout')), \
             patch('app.csi_sync_worker.send_email') as mail:
            for _ in range(3):
                _run(repo)

        assert mail.call_count == 1
        body = mail.call_args.args[2]
        assert '网络异常重试 3 次仍失败' in body
        assert '网络连通性' in body

    def test_alert_email_copy_for_business_failure(self):
        repo = FakeRepo(_row())
        with patch('app.csi_sync_worker._http_post',
                   return_value=_resp(400, _failed('存在重复数据'))), \
             patch('app.csi_sync_worker.send_email') as mail:
            _run(repo)

        body = mail.call_args.args[2]
        assert '已停止自动重试' in body
        assert '网络异常重试' not in body


# ====================================================================== #
#  6. 「重新发送」重置 transient_count
# ====================================================================== #
def test_resend_resets_transient_count(app, monkeypatch):
    from app.routes import csi_interface as r

    failed_row = {
        'id': 1, 'request_id': 2967, 'api_name': w.API_QTY_MOVE, 'siteRef': 410,
        'is_sync': 2, 'status': 'FAILED_MANUAL', 'traceid': TRACE,
        'transient_count': 3, 'retry_count': 1, 'errormessage': '网络/服务异常重试 3 次仍失败',
        'documentnumber': 'MP2967', 'coil_id': '261016004', 'item': 'A000871',
        'lot': '202510210000006', 'lot_id': None,
    }
    fake_repo = MagicMock()

    monkeypatch.setattr(r, '_check_login', lambda: ({'role': 'admin', 'username': 'tester'}, None))
    monkeypatch.setattr(r, 'get_db', lambda: MagicMock())
    monkeypatch.setattr(r, '_check_site_scope', lambda user, row: None)
    monkeypatch.setattr(r, '_fetch_row', lambda conn, rid: dict(failed_row))
    monkeypatch.setattr(r.sync_worker, '_retry_trace_id', lambda row: 'NEW-TRACE')
    monkeypatch.setattr(r.sync_worker, '_process_row', lambda *a, **k: None)
    monkeypatch.setattr(r.sync_worker, '_Repo', lambda conn: fake_repo)

    with app.test_request_context('/api/csi-interface/1/resend', method='POST'):
        r.api_resend(1)

    kwargs = fake_repo.update.call_args.kwargs
    assert kwargs['transient_count'] == 0
    assert kwargs['status'] == 'PENDING' and kwargs['is_sync'] == 0
    assert kwargs['retry_count'] == 0
    assert kwargs['traceid'] == 'NEW-TRACE'
