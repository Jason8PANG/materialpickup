# -*- coding: utf-8 -*-
"""CSI 库位转移同步器单元测试

覆盖点：
  1. QUEUED → PENDING → SUCCESS 状态推进（ion 对象 / 数组两种结构）
  2. HTTP 400 入队被拒 → retry_count+1、**单号不变**、只换 traceId、is_sync=0
  3. HTTP 409 CONFLICT → 单号不变、只换 traceId
  4. 连续 3 次失败 → is_sync=2/FAILED_MANUAL + 邮件一次，之后不再被扫描，单号全程不变
  5. from_location 为空 → 查 CSI 库位补值
  5b. sl-misc-receipt / sl-misc-issue（盘点差异调整）：按行 api_name 组装 location/account 契约
  6. 并发抢占 → 同一行只提交一次
  7. 真实库：2967/2973 两条 PENDING 行能被选中；条件 UPDATE 具备抢占语义

全程 mock requests.post，绝不真实过账。
"""
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402

from app import csi_sync_worker as w  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.csi_service import BF_MSG_ALREADY_ZERO, BF_MSG_CLEARED  # noqa: E402

# 在 conftest 的 autouse monkeypatch 生效前捕获真实 connect（真实库用例专用）
_REAL_CONNECT = pymysql.connect


# ====================================================================== #
#  测试替身
# ====================================================================== #
class FakeRepo:
    """按 kr_csi_interface 的行语义模拟的最小仓储"""

    def __init__(self, rows):
        self.rows = {r['id']: dict(r) for r in rows}
        self.claim_attempts = 0
        self.submitted_ids = []

    # -- 与 _Repo 相同签名 --------------------------------------------------
    def fetch_candidates(self, limit=None):
        limit = limit or 20
        out = [dict(r) for r in self.rows.values()
               if r.get('is_sync') == 0
               and r.get('status') in ('PENDING', 'QUEUED', 'SUBMITTING')
               and r.get('last_poll_at') is None]
        return out[:limit]

    def claim(self, row_id):
        self.claim_attempts += 1
        row = self.rows.get(row_id)
        if not row:
            return False
        if row.get('is_sync') != 0 or row.get('status') not in ('PENDING', 'QUEUED', 'SUBMITTING'):
            return False
        row['status'] = 'SUBMITTING'
        return True

    def update(self, row_id, **fields):
        self.rows[row_id].update(fields)

    # -- 断言辅助 ----------------------------------------------------------
    def row(self, row_id):
        return self.rows[row_id]


def _row(**over):
    base = {
        'id': 1, 'request_id': 2967, 'item_id': 4403, 'coil_id': '261016004', 'lot_id': None,
        'siteRef': 410, 'item': 'A000871', 'lot': '202510210000006', 'quantity': 6.5,
        'warehouse': 'M1M1', 'from_location': 'A-01-01', 'to_location': 'Floor-Core',
        'reasoncode': 'C1N', 'unitcode1': 'FT', 'documentnumber': 'MP2967',
        'status': 'PENDING', 'retry_count': 0, 'traceid': 'MP2967-261016004-ce830e42a267',
        'is_sync': 0, 'last_poll_at': None,
    }
    base.update(over)
    return base


def _misc_row(**over):
    """盘点差异调整写入的杂项收发行（api_count_adjust 的落库形态）"""
    base = {
        'id': 2, 'request_id': None, 'item_id': 8801, 'coil_id': '261017001', 'lot_id': None,
        'api_name': w.API_MISC_RECEIPT, 'siteRef': 410, 'item': 'A000871',
        'lot': '202510210000006', 'quantity': 12.5, 'um': 'KG', 'warehouse': 'M1M1',
        'from_location': None, 'to_location': 'C-03-02', 'reasoncode': 'CC',
        'account': '505100', 'unitcode1': None, 'unitcode2': None,
        'documentnumber': 'INV260001001', 'line_seq': 1, 'direction': 'F',
        'status': 'PENDING', 'retry_count': 0,
        'traceid': 'INV260001001-261017001-1a2b3c4d5e6f', 'is_sync': 0, 'last_poll_at': None,
    }
    base.update(over)
    return base


def _job_issue_row(**over):
    """外部工单发料写入的行（api_name=sl-job-issue）"""
    base = {
        'id': 3, 'request_id': None, 'item_id': None, 'coil_id': None, 'lot_id': None,
        'api_name': w.API_JOB_ISSUE, 'siteRef': 310, 'item': 'B052552',
        'lot': '202505090000036', 'quantity': 68, 'um': 'EA', 'job': 'J000029867',
        'warehouse': 'C1M1', 'from_location': None, 'to_location': 'Floor-Core',
        'reasoncode': None, 'account': None, 'unitcode1': None, 'unitcode2': None,
        'documentnumber': 'JOB00001', 'line_seq': None, 'direction': 'F',
        'status': 'PENDING', 'retry_count': 0,
        'traceid': 'JIJ000029867-B052552-0f1e2d3c4b5a', 'is_sync': 0, 'last_poll_at': None,
    }
    base.update(over)
    return base


def _resp(code=200, payload=None, text=''):
    r = MagicMock()
    r.status_code = code
    r.text = text
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


def _queued(status='QUEUED'):
    return {'success': True, 'data': {'status': status}}


def _failed(err='科学计数法解析失败'):
    return {'success': False, 'data': {'status': 'FAILED', 'error': err}}


@pytest.fixture(autouse=True)
def _fast_config(monkeypatch):
    """测试环境：轮询不真的 sleep，超时设置与生产一致"""
    monkeypatch.setattr(Config, 'CSI_SYNC_POLL_INTERVAL', 0)
    monkeypatch.setattr(Config, 'CSI_SYNC_POLL_ATTEMPTS', 4)
    monkeypatch.setattr(Config, 'CSI_SYNC_MAX_RETRY', 3)
    monkeypatch.setattr(Config, 'CSI_SYTELINE_TOKEN', 'slt_test')
    monkeypatch.setattr(Config, 'CSI_ALERT_EMAIL', 'jason.pang@nai-group.com')
    yield


@pytest.fixture(autouse=True)
def _no_real_csi(monkeypatch):
    """默认不访问真实 CSI：实时库位查不到 → 走行内快照兜底（与原语义一致）。

    需要实时库位的用例在用例内再 patch app.csi_sync_worker.CSIClient 覆盖本 fixture。
    """
    client = MagicMock()
    client.get_lot_location.return_value = None
    # 工单物料 Backflush 默认视为「已为 0」：不阻断提交，也不会真的写 ERP
    client.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
    monkeypatch.setattr(w, 'CSIClient', MagicMock(return_value=client))
    return client


def _run(repo):
    return w.run_once(repo=repo, sleep_func=lambda s: None)


# ====================================================================== #
#  1/5. 成功推进 + ion 结构兼容
# ====================================================================== #
class TestSuccess:
    def test_queued_pending_success_ion_dict(self):
        """QUEUED → PENDING → SUCCESS，ion 为对象"""
        repo = FakeRepo([_row()])
        responses = [
            _resp(200, _queued('QUEUED')),
            _resp(200, _queued('PENDING')),
            _resp(200, _ok('SUCCESS', {'Success': True, 'Message': 'ok'})),
        ]
        with patch('app.csi_sync_worker.requests.post', side_effect=responses) as post:
            stats = _run(repo)

        assert post.call_count == 3
        # 同一 traceId 重复 POST 轮询
        assert {c.kwargs['json']['traceId'] for c in post.call_args_list} == {'MP2967-261016004-ce830e42a267'}
        assert post.call_args.kwargs['headers']['X-Syteline-Token'] == 'slt_test'
        row = repo.row(1)
        assert row['is_sync'] == 1 and row['status'] == 'SUCCESS'
        assert row['errormessage'] is None
        assert stats['success'] == 1

    def test_success_with_ion_list(self):
        """SUCCESS 且 ion 为数组 → 判定成功（防 AttributeError）"""
        repo = FakeRepo([_row()])
        ion = [{'Success': True, 'Message': 'a'}, {'Success': True, 'Message': 'b'}]
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _ok('SUCCESS', ion))):
            stats = _run(repo)
        assert repo.row(1)['is_sync'] == 1
        assert stats['success'] == 1

    def test_ion_list_with_failed_item_is_failure(self):
        """ion 数组中出现 Success=False → 失败（不误判成功）"""
        repo = FakeRepo([_row()])
        ion = [{'Success': True}, {'Success': False, 'Message': 'lot not found'}]
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _ok('SUCCESS', ion))):
            _run(repo)
        row = repo.row(1)
        assert row['retry_count'] == 1
        assert 'lot not found' in row['errormessage']
        assert row['is_sync'] == 0

    def test_payload_contract(self):
        """请求体字段严格符合宝鼎契约"""
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            _run(repo)
        body = post.call_args.kwargs['json']
        assert body == {
            'traceId': 'MP2967-261016004-ce830e42a267',
            'site': '410',
            'documentNumber': 'MP2967',
            'item': 'A000871',
            'quantity': 6.5,
            'warehouse': 'M1M1',
            'fromLocation': 'A-01-01',
            'toLocation': 'Floor-Core',
            'lot': '202510210000006',
            'reasonCode': 'C1N',
        }


# ====================================================================== #
#  2/3. 400 / 409 → 保留单号，仅换 traceId 重试
# ====================================================================== #
class TestFailAndRetryKeepDocumentNumber:
    def test_http_400_keeps_document_number(self):
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(400, {'success': False, 'message': 'documentNumber 超过 12 位'})):
            stats = _run(repo)

        row = repo.row(1)
        assert row['retry_count'] == 1
        # 单号原样沿用，只更换 traceId
        assert row['documentnumber'] == 'MP2967'
        assert row['traceid'] != 'MP2967-261016004-ce830e42a267'
        assert row['traceid'].startswith('MP2967-261016004-')
        assert len(row['traceid']) < 128
        assert row['is_sync'] == 0 and row['status'] == 'PENDING'
        assert 'documentNumber' in row['errormessage']
        assert stats['retried'] == 1

    def test_http_409_keeps_document_number(self):
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(409, {'message': 'conflict'})):
            _run(repo)

        row = repo.row(1)
        assert row['retry_count'] == 1
        assert row['documentnumber'] == 'MP2967'
        assert row['traceid'] != 'MP2967-261016004-ce830e42a267'
        assert '409' in row['errormessage']
        assert row['is_sync'] == 0

    def test_reverse_row_keeps_reverse_document_number(self):
        """反向行（MP{rid}B{seq}）重试同样保持单号不变"""
        repo = FakeRepo([_row(documentnumber='MP2973B01', traceid='MP2973B01-L21-aaaa')])
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(409, {'message': 'conflict'})):
            _run(repo)

        row = repo.row(1)
        assert row['documentnumber'] == 'MP2973B01'
        assert row['traceid'].startswith('MP2973B01-')

    def test_retry_trace_id_is_unique_and_bounded(self):
        """每次重试的 traceId 都不同，且长度 < 128"""
        row = _row(documentnumber='MP29679999')
        traces = {w._retry_trace_id(row) for _ in range(5)}
        assert len(traces) == 5
        assert all(len(t) < 128 for t in traces)
        assert all(t.startswith('MP29679999-261016004-') for t in traces)


# ====================================================================== #
#  4. 连续 3 次失败 → 转人工 + 邮件（且只发一次）
# ====================================================================== #
class TestManualFailAfterThreeRetries:
    def test_three_failures_then_email_and_stop(self):
        repo = FakeRepo([_row()])
        traces = []
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _failed())), \
             patch('app.csi_sync_worker.send_email') as mail:
            _run(repo)
            assert repo.row(1)['retry_count'] == 1
            assert repo.row(1)['documentnumber'] == 'MP2967'
            traces.append(repo.row(1)['traceid'])
            assert mail.call_count == 0

            _run(repo)
            assert repo.row(1)['retry_count'] == 2
            assert repo.row(1)['documentnumber'] == 'MP2967'
            traces.append(repo.row(1)['traceid'])
            assert mail.call_count == 0

            stats = _run(repo)
            row = repo.row(1)

        assert row['retry_count'] == 3
        # 三次重试单号始终未变，只有 traceId 变化
        assert row['documentnumber'] == 'MP2967'
        assert traces[0] != traces[1]
        assert row['traceid'] == traces[1]   # 转人工不再更换 traceId
        assert row['is_sync'] == 2 and row['status'] == 'FAILED_MANUAL'
        assert stats['manual'] == 1
        assert mail.call_count == 1

        to_email, subject, body = mail.call_args.args
        assert to_email == 'jason.pang@nai-group.com'
        assert '单号2967' in subject
        assert 'MP2967' in body             # 单号保持不变
        assert '科学计数法解析失败' in body
        assert '已停止自动重试' in body
        assert 'A000871' in body and '6.5' in body

        # 转人工后不再被扫描、不再提交
        assert repo.fetch_candidates() == []
        with patch('app.csi_sync_worker.requests.post') as post:
            stats2 = _run(repo)
        post.assert_not_called()
        assert stats2['scanned'] == 0

    def test_unconfirmed_counts_as_failure(self):
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, {'data': {'status': 'UNCONFIRMED'}})):
            _run(repo)
        row = repo.row(1)
        assert row['retry_count'] == 1
        assert 'UNCONFIRMED' in row['errormessage']

    def test_email_failure_does_not_break_run(self):
        repo = FakeRepo([_row(retry_count=2)])
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _failed())), \
             patch('app.csi_sync_worker.send_email', side_effect=RuntimeError('smtp down')):
            stats = _run(repo)
        assert stats['manual'] == 1
        assert repo.row(1)['status'] == 'FAILED_MANUAL'


# ====================================================================== #
#  5. 网络异常不计 retry
# ====================================================================== #
class TestTransient:
    def test_timeout_does_not_increment_retry(self):
        import requests as _requests
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post',
                   side_effect=_requests.Timeout('read timeout')):
            stats = _run(repo)
        row = repo.row(1)
        assert row['retry_count'] == 0
        assert row['status'] == 'SUBMITTING'
        assert row['is_sync'] == 0
        assert '请求异常' in row['errormessage']
        assert stats['transient'] == 1


# ====================================================================== #
#  6. from_location 补值
# ====================================================================== #
class TestFromLocation:
    def test_empty_from_location_looked_up(self):
        repo = FakeRepo([_row(from_location=None)])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = 'B-02-03'
            _run(repo)

        client_cls.return_value.get_lot_location.assert_called_once_with('A000871', '202510210000006')
        assert post.call_args.kwargs['json']['fromLocation'] == 'B-02-03'
        assert repo.row(1)['from_location'] == 'B-02-03'
        assert repo.row(1)['is_sync'] == 1

    def test_lookup_failure_counts_as_retry(self):
        repo = FakeRepo([_row(from_location='')])
        with patch('app.csi_sync_worker.requests.post') as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = None
            _run(repo)

        post.assert_not_called()          # 不发出必然失败的请求
        row = repo.row(1)
        assert row['retry_count'] == 1
        assert row['errormessage'] == '无法获取 Lot 库位'
        assert row['is_sync'] == 0


# ====================================================================== #
#  6b. 提交前一律用 CSI 实时库位覆盖（本地快照仅兜底）
# ====================================================================== #
class TestLiveLocationOverride:
    def test_qty_move_from_location_overridden(self, caplog):
        """行内 from=RM-P015-1（旧），CSI 实际 RM-P016-2 → payload 与库内都改成实时值"""
        repo = FakeRepo([_row(from_location='RM-P015-1', to_location='Floor-Core')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls, \
             caplog.at_level('INFO', logger='app.csi_sync_worker'):
            client_cls.return_value.get_lot_location.return_value = 'RM-P016-2'
            stats = _run(repo)

        body = post.call_args.kwargs['json']
        assert body['fromLocation'] == 'RM-P016-2'
        assert body['toLocation'] == 'Floor-Core'
        assert repo.row(1)['from_location'] == 'RM-P016-2'
        assert '库位校正' in caplog.text and 'RM-P015-1' in caplog.text
        assert stats['success'] == 1

    def test_qty_move_same_location_marked_noop(self, caplog):
        """CSI 实际库位 = toLocation → 物料已在目标库位：不发请求、不发邮件、is_sync=2/NOOP"""
        repo = FakeRepo([_row(from_location='RM-P015-1', to_location='Floor-Core')])
        with patch('app.csi_sync_worker.requests.post') as post, \
             patch('app.csi_sync_worker.send_email') as mail, \
             patch('app.csi_sync_worker.CSIClient') as client_cls, \
             caplog.at_level('INFO', logger='app.csi_sync_worker'):
            client_cls.return_value.get_lot_location.return_value = 'Floor-Core'
            stats = _run(repo)

        post.assert_not_called()
        mail.assert_not_called()
        row = repo.row(1)
        assert row['is_sync'] == 2 and row['status'] == 'NOOP'
        assert row['errormessage'] == '物料已在目标库位，无需移库'
        assert stats['noop'] == 1
        assert '物料已在目标库位' in caplog.text
        # NOOP 后不再被扫描，也不会再提交
        assert repo.fetch_candidates() == []
        assert _run(repo)['scanned'] == 0

    def test_misc_issue_location_overridden(self):
        """盘点盘亏行 to=RM-P015-1（旧），CSI 实际 Floor-Core → payload location 用实时值"""
        repo = FakeRepo([_misc_row(api_name=w.API_MISC_ISSUE, to_location='RM-P015-1')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = 'Floor-Core'
            stats = _run(repo)

        assert post.call_args.kwargs['json']['location'] == 'Floor-Core'
        assert repo.row(2)['to_location'] == 'Floor-Core'
        assert stats['success'] == 1

    def test_csi_failure_falls_back_to_row_location(self):
        """CSI 查询异常 → 用行内已有库位兜底，不阻断提交"""
        repo = FakeRepo([_row(from_location='RM-P015-1')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.side_effect = RuntimeError('csi down')
            stats = _run(repo)

        assert post.call_args.kwargs['json']['fromLocation'] == 'RM-P015-1'
        assert repo.row(1)['from_location'] == 'RM-P015-1'
        assert stats['success'] == 1

    def test_same_item_lot_queried_once_per_round(self):
        """同一轮内同 (item, lot) 的多个卷标只查一次 CSI"""
        rows = [
            _row(id=1, coil_id='261016004'),
            _row(id=2, coil_id='261016005'),
            _row(id=3, coil_id='261016006', item='B000002', lot='202510220000001'),
        ]
        repo = FakeRepo(rows)
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = 'RM-P015-1'
            _run(repo)

        assert post.call_count == 3
        assert client_cls.return_value.get_lot_location.call_count == 2
        assert repo.row(1)['is_sync'] == 1 and repo.row(2)['is_sync'] == 1


# ====================================================================== #
#  5b. 杂项收发（盘点差异调整）：sl-misc-receipt / sl-misc-issue
# ====================================================================== #
class TestMiscReceiptAndIssue:
    def test_receipt_payload_and_url(self):
        """盘盈行 → location/reasonCode/account/lot 契约，QUEUED→SUCCESS"""
        repo = FakeRepo([_misc_row()])
        responses = [_resp(200, _queued('QUEUED')),
                     _resp(200, _ok('SUCCESS', {'Success': True, 'Message': 'ok'}))]
        with patch('app.csi_sync_worker.requests.post', side_effect=responses) as post:
            stats = _run(repo)

        assert post.call_count == 2
        assert post.call_args.args[0].endswith('/api/cmd/sl-misc-receipt')
        # 同一 traceId 轮询
        assert {c.kwargs['json']['traceId'] for c in post.call_args_list} == \
               {'INV260001001-261017001-1a2b3c4d5e6f'}
        body = post.call_args.kwargs['json']
        assert body == {
            'traceId': 'INV260001001-261017001-1a2b3c4d5e6f',
            'site': '410',
            'documentNumber': 'INV260001001',
            'item': 'A000871',
            'quantity': 12.5,
            'warehouse': 'M1M1',
            'location': 'C-03-02',
            'reasonCode': 'CC',
            'account': '505100',
            'lot': '202510210000006',
        }
        # 不得混入库位转移字段
        assert 'fromLocation' not in body and 'toLocation' not in body
        assert 'unitcode1' not in body and 'um' not in body

        row = repo.row(2)
        assert row['is_sync'] == 1 and row['status'] == 'SUCCESS'
        assert row['errormessage'] is None
        assert stats['success'] == 1

    def test_issue_payload_and_url(self):
        """盘亏行 → sl-misc-issue 端点，其余契约相同"""
        repo = FakeRepo([_misc_row(api_name=w.API_MISC_ISSUE)])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            stats = _run(repo)

        assert post.call_args.args[0].endswith('/api/cmd/sl-misc-issue')
        assert post.call_args.kwargs['json'] == {
            'traceId': 'INV260001001-261017001-1a2b3c4d5e6f',
            'site': '410',
            'documentNumber': 'INV260001001',
            'item': 'A000871',
            'quantity': 12.5,
            'warehouse': 'M1M1',
            'location': 'C-03-02',
            'reasonCode': 'CC',
            'account': '505100',
            'lot': '202510210000006',
        }
        assert repo.row(2)['is_sync'] == 1
        assert stats['success'] == 1

    def test_qty_move_regression_unchanged(self):
        """回归：sl-qty-move 端点与字段集合保持不变（无 account/location）"""
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            _run(repo)

        assert post.call_args.args[0].endswith('/api/cmd/sl-qty-move')
        body = post.call_args.kwargs['json']
        assert set(body) == {'traceId', 'site', 'documentNumber', 'item', 'quantity',
                             'warehouse', 'fromLocation', 'toLocation', 'lot', 'reasonCode'}
        assert body['fromLocation'] == 'A-01-01' and body['toLocation'] == 'Floor-Core'

    def test_empty_location_looked_up_and_written_back(self):
        """location 为空 → 查 CSI 库位补值并写回 to_location"""
        repo = FakeRepo([_misc_row(to_location=None)])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = 'C-09-09'
            _run(repo)

        client_cls.return_value.get_lot_location.assert_called_once_with('A000871', '202510210000006')
        assert post.call_args.kwargs['json']['location'] == 'C-09-09'
        assert repo.row(2)['to_location'] == 'C-09-09'
        assert repo.row(2)['is_sync'] == 1

    def test_location_lookup_failure_counts_as_retry(self):
        """库位补不到 → 不提交、retry_count+1"""
        repo = FakeRepo([_misc_row(to_location='')])
        with patch('app.csi_sync_worker.requests.post') as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = None
            _run(repo)

        post.assert_not_called()
        row = repo.row(2)
        assert row['retry_count'] == 1
        assert row['errormessage'] == '无法获取 Lot 库位'
        assert row['is_sync'] == 0

    def test_empty_account_blocks_request_then_manual_with_email(self):
        """account 为空 → 不发请求，3 次后 FAILED_MANUAL，邮件含接口名"""
        repo = FakeRepo([_misc_row(account=None)])
        with patch('app.csi_sync_worker.requests.post') as post, \
             patch('app.csi_sync_worker.send_email') as mail:
            _run(repo)
            assert repo.row(2)['retry_count'] == 1
            assert repo.row(2)['is_sync'] == 0
            assert mail.call_count == 0

            _run(repo)
            assert repo.row(2)['retry_count'] == 2
            assert mail.call_count == 0

            stats = _run(repo)

        post.assert_not_called()          # account 必填，绝不发必然失败的请求
        row = repo.row(2)
        assert row['retry_count'] == 3
        assert row['is_sync'] == 2 and row['status'] == 'FAILED_MANUAL'
        assert 'account' in row['errormessage']
        assert stats['manual'] == 1

        assert mail.call_count == 1
        to_email, subject, body = mail.call_args.args
        assert to_email == 'jason.pang@nai-group.com'
        assert 'sl-misc-receipt' in subject       # 主题可辨接口
        assert 'sl-misc-receipt' in body          # 正文含接口名
        assert 'INV260001001' in body             # request_id 为空时用单号标识
        assert 'account' in body
        assert '已停止自动重试' in body

        # 转人工后不再被扫描
        assert repo.fetch_candidates() == []

    def test_failed_response_retries_new_trace_same_document(self):
        """FAILED（data.error）→ 换 traceId、单号不变"""
        repo = FakeRepo([_misc_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _failed('科目 505100 无效'))):
            _run(repo)

        row = repo.row(2)
        assert row['retry_count'] == 1
        assert row['documentnumber'] == 'INV260001001'
        assert row['traceid'] != 'INV260001001-261017001-1a2b3c4d5e6f'
        assert row['traceid'].startswith('INV260001001-261017001-')
        assert '科目 505100 无效' in row['errormessage']
        assert row['is_sync'] == 0 and row['status'] == 'PENDING'

    def test_misc_success_with_ion_list(self):
        """ion 为数组且全部 Success → 判定成功"""
        repo = FakeRepo([_misc_row(api_name=w.API_MISC_ISSUE)])
        ion = [{'Success': True, 'Message': 'a'}, {'Success': True, 'Message': 'b'}]
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', ion))):
            stats = _run(repo)
        assert repo.row(2)['is_sync'] == 1
        assert stats['success'] == 1


# ====================================================================== #
#  5c. 工单发料（外部 API）：sl-job-issue
# ====================================================================== #
class TestJobIssue:
    def test_payload_and_url(self):
        """QUEUED → SUCCESS；payload 为工单发料契约（无 reasonCode/account/from/to）"""
        repo = FakeRepo([_job_issue_row()])
        responses = [
            _resp(200, _queued('QUEUED')),
            _resp(200, _ok('SUCCESS', {'Success': True, 'Message': 'ok'})),
        ]
        with patch('app.csi_sync_worker.requests.post', side_effect=responses) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
            stats = _run(repo)

        assert post.call_count == 2
        assert post.call_args.args[0].endswith('/api/cmd/sl-job-issue')
        assert post.call_args.kwargs['json'] == {
            'traceId': 'JIJ000029867-B052552-0f1e2d3c4b5a',
            'site': '310',
            'documentNumber': 'JOB00001',
            'job': 'J000029867',
            'item': 'B052552',
            'quantity': 68,
            'lot': '202505090000036',
            'warehouse': 'C1M1',
            'location': 'Floor-Core',
        }
        # 目标库位已给（Floor-Core）→ 不需要实时查 Lot 库位
        client_cls.return_value.get_lot_location.assert_not_called()
        row = repo.row(3)
        assert row['is_sync'] == 1 and row['status'] == 'SUCCESS'
        assert row['errormessage'] is None
        assert stats['success'] == 1

    def test_job_with_suffix_split(self):
        """job 带 -0001 后缀 → payload 拆成 job + suffix"""
        repo = FakeRepo([_job_issue_row(job='J000029867-0001', documentnumber='JOB00002')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            _run(repo)

        body = post.call_args.kwargs['json']
        assert body['job'] == 'J000029867'
        assert body['suffix'] == '0001'
        # 接口行保持原始工单号（含后缀），便于追溯
        assert repo.row(3)['job'] == 'J000029867-0001'

    def test_job_without_suffix_has_no_suffix_key(self):
        body = w._build_payload(_job_issue_row())
        assert 'suffix' not in body

    def test_location_empty_looked_up_and_written_back(self):
        """to_location 为空 → 实时查 Lot 库位兜底并写回"""
        repo = FakeRepo([_job_issue_row(to_location=None)])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = 'C-09-09'
            client_cls.return_value.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
            _run(repo)

        client_cls.return_value.get_lot_location.assert_called_once_with('B052552', '202505090000036')
        assert post.call_args.kwargs['json']['location'] == 'C-09-09'
        assert repo.row(3)['to_location'] == 'C-09-09'
        assert repo.row(3)['is_sync'] == 1

    def test_location_lookup_failure_counts_as_retry(self):
        """库位补不到 → 不发请求、retry_count+1"""
        repo = FakeRepo([_job_issue_row(to_location='')])
        with patch('app.csi_sync_worker.requests.post') as post, \
             patch('app.csi_sync_worker.CSIClient') as client_cls:
            client_cls.return_value.get_lot_location.return_value = None
            client_cls.return_value.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
            _run(repo)

        post.assert_not_called()
        row = repo.row(3)
        assert row['retry_count'] == 1
        assert '库位' in row['errormessage']
        assert row['is_sync'] == 0

    def test_missing_job_blocks_request(self):
        """job 为空 → 不发请求、retry_count+1"""
        repo = FakeRepo([_job_issue_row(job=None)])
        with patch('app.csi_sync_worker.requests.post') as post:
            _run(repo)

        post.assert_not_called()
        row = repo.row(3)
        assert row['retry_count'] == 1
        assert 'job' in row['errormessage']
        assert row['is_sync'] == 0

    def test_failed_keeps_document_and_swaps_trace(self):
        """平台 FAILED → 单号不变、只换 traceId"""
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _failed('工单已关闭'))):
            _run(repo)

        row = repo.row(3)
        assert row['retry_count'] == 1
        assert row['documentnumber'] == 'JOB00001'
        assert row['traceid'] != 'JIJ000029867-B052552-0f1e2d3c4b5a'
        assert '工单已关闭' in row['errormessage']
        assert row['is_sync'] == 0 and row['status'] == 'PENDING'

    def test_manual_fail_email_has_job(self):
        """连续 3 次失败 → 转人工，邮件带接口名与工单号"""
        repo = FakeRepo([_job_issue_row(retry_count=2)])
        with patch('app.csi_sync_worker.requests.post', return_value=_resp(200, _failed('工单不存在'))), \
             patch('app.csi_sync_worker.send_email') as mail:
            stats = _run(repo)

        row = repo.row(3)
        assert row['is_sync'] == 2 and row['status'] == 'FAILED_MANUAL'
        assert stats['manual'] == 1
        _to, subject, body = mail.call_args.args
        assert 'sl-job-issue' in subject
        assert 'J000029867' in body
        assert 'JOB00001' in body

    def test_supported_apis_contains_job_issue(self):
        assert w.API_JOB_ISSUE in w.SUPPORTED_APIS
        assert w._row_api(_job_issue_row()) == 'sl-job-issue'


# ====================================================================== #
#  5d. 工单发料前置：先取消工单物料 Backflush（倒冲）
# ====================================================================== #
class TestJobIssueBackflush:
    def test_backflush_cleared_then_submits(self, _no_real_csi, caplog):
        """Backflush=1 → 先清 0（IDO update），清成功后才提交发料并记 1->0 日志"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (True, BF_MSG_CLEARED)
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post, \
             caplog.at_level('INFO', logger='app.csi_sync_worker'):
            stats = _run(repo)

        client.clear_job_matl_backflush.assert_called_once_with('J000029867', 0, 'B052552')
        # 清 0 之后才发料，且 payload 未被 Backflush 逻辑改动
        assert post.call_args.kwargs['json'] == {
            'traceId': 'JIJ000029867-B052552-0f1e2d3c4b5a',
            'site': '310',
            'documentNumber': 'JOB00001',
            'job': 'J000029867',
            'item': 'B052552',
            'quantity': 68,
            'lot': '202505090000036',
            'warehouse': 'C1M1',
            'location': 'Floor-Core',
        }
        assert '工单 J000029867 物料 B052552 Backflush 1->0' in caplog.text
        assert repo.row(3)['is_sync'] == 1 and stats['success'] == 1

    def test_backflush_already_zero_submits_without_update(self, _no_real_csi):
        """Backflush 已为 0 → 直接提交（CSIClient 内部不会再写库）"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            stats = _run(repo)

        client.clear_job_matl_backflush.assert_called_once()
        assert post.call_count == 1
        assert repo.row(3)['is_sync'] == 1 and stats['success'] == 1

    def test_backflush_lookup_failure_blocks_submission(self, _no_real_csi):
        """查不到工单物料 → 不提交，走失败流程（原因含「取消 Backflush 失败」）"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (
            False, '未找到工单物料 J000029867-0000 / B052552（或查询失败）')
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post') as post:
            stats = _run(repo)

        post.assert_not_called()
        row = repo.row(3)
        assert row['retry_count'] == 1
        assert row['errormessage'].startswith('取消 Backflush 失败')
        assert '未找到工单物料' in row['errormessage']
        assert row['is_sync'] == 0 and stats['retried'] == 1

    def test_backflush_update_failure_blocks_submission(self, _no_real_csi):
        """IDO update 非 2xx → 不提交（避免与倒冲冲突）"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (
            False, '更新工单物料 J000029867-0000 / B052552 失败：HTTP 500')
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post') as post:
            _run(repo)

        post.assert_not_called()
        row = repo.row(3)
        assert row['retry_count'] == 1
        assert row['errormessage'].startswith('取消 Backflush 失败')
        assert 'HTTP 500' in row['errormessage']

    def test_backflush_exception_blocks_submission(self, _no_real_csi):
        """CSIClient 抛异常 → 不放行，计一次失败"""
        client = _no_real_csi
        client.clear_job_matl_backflush.side_effect = RuntimeError('ido down')
        repo = FakeRepo([_job_issue_row()])
        with patch('app.csi_sync_worker.requests.post') as post:
            _run(repo)

        post.assert_not_called()
        assert repo.row(3)['retry_count'] == 1
        assert 'ido down' in repo.row(3)['errormessage']

    def test_same_job_matl_processed_once_per_round(self, _no_real_csi):
        """同一轮同 (job, suffix, item) 只检查/清除一次，三行都正常提交"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (True, BF_MSG_CLEARED)
        rows = [
            _job_issue_row(id=3, documentnumber='JOB00001'),
            _job_issue_row(id=4, documentnumber='JOB00002'),
            _job_issue_row(id=5, documentnumber='JOB00003'),
        ]
        repo = FakeRepo(rows)
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            stats = _run(repo)

        assert post.call_count == 3
        assert client.clear_job_matl_backflush.call_count == 1
        assert stats['success'] == 3

    def test_suffix_split_passed_to_backflush(self, _no_real_csi):
        """job 带 -0005 → 调用时拆成 job + int(suffix)"""
        client = _no_real_csi
        client.clear_job_matl_backflush.return_value = (True, BF_MSG_ALREADY_ZERO)
        repo = FakeRepo([_job_issue_row(job='J000004862-0005', item='F010013',
                                        documentnumber='JOB00009')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))):
            _run(repo)

        client.clear_job_matl_backflush.assert_called_once_with('J000004862', 5, 'F010013')

    def test_qty_move_and_misc_untouched(self, _no_real_csi):
        """回归：qty-move / misc 行不触发 Backflush 检查"""
        client = _no_real_csi
        repo = FakeRepo([_row(), _misc_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            stats = _run(repo)

        client.clear_job_matl_backflush.assert_not_called()
        assert post.call_count == 2
        assert stats['success'] == 2


# ====================================================================== #
#  7. 抢占：同一行只提交一次
# ====================================================================== #
class TestClaim:
    def test_second_process_does_not_resubmit(self):
        repo = FakeRepo([_row()])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))) as post:
            _run(repo)          # 进程 A 处理完成
            _run(repo)          # 进程 B 再扫（行已 is_sync=1）
        assert post.call_count == 1

    def test_claim_loss_skips_submission(self):
        """claim 返回 False（已被别的线程抢占）→ 不提交"""
        repo = FakeRepo([_row()])
        repo.claim = lambda row_id: False   # 模拟另一进程先抢占成功
        with patch('app.csi_sync_worker.requests.post') as post:
            stats = _run(repo)
        post.assert_not_called()
        assert stats['skipped'] == 1

    def test_stale_submitting_row_is_retried(self):
        """上次中断在 SUBMITTING 的行，超时后可再次被选中"""
        repo = FakeRepo([_row(status='SUBMITTING')])
        with patch('app.csi_sync_worker.requests.post',
                   return_value=_resp(200, _ok('SUCCESS', {'Success': True}))):
            stats = _run(repo)
        assert stats['claimed'] == 1
        assert repo.row(1)['is_sync'] == 1
        assert repo.row(1)['status'] == 'SUCCESS'


# ====================================================================== #
#  8. 真实库（只读 + 一次性抢占验证）
# ====================================================================== #
def _real_conn():
    """绕过 conftest 的 pymysql.connect monkeypatch，取真实连接；不可达则 skip"""
    import pymysql
    from pymysql.cursors import DictCursor
    try:
        conn = _REAL_CONNECT(
            host=Config.MYSQL_HOST, port=Config.MYSQL_PORT,
            user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
            database=Config.MYSQL_DB, charset='utf8mb4',
            cursorclass=DictCursor, connect_timeout=8,
        )
        return conn
    except Exception as e:      # 数据库不可达时跳过，不影响其余用例
        pytest.skip(f'真实数据库不可用：{e}')


class TestRealDatabase:
    def test_sample_pending_rows_selected(self):
        """2967 / 2973 两条 PENDING 样例行能被选中（只读，不发请求）"""
        conn = _real_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM kr_csi_interface WHERE request_id IN (2967, 2973) "
                "AND is_sync = 0 AND status = 'PENDING'"
            )
            expected = {r['id'] for r in cur.fetchall()}
            cur.close()
            if not expected:
                pytest.skip('库中已无 2967/2973 的 PENDING 样例行（可能已被真实同步处理）')
            ids = {r['id'] for r in w._Repo(conn).fetch_candidates(limit=20)}
        finally:
            conn.close()
        assert expected.issubset(ids)

    def test_claim_is_exclusive(self):
        """条件 UPDATE 抢占语义：双方都 fetch 到同一行时只有一方能 claim 成功"""
        conn = _real_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO kr_csi_interface "
                "(request_id, api_name, siteRef, item, lot, quantity, warehouse, "
                " from_location, to_location, reasoncode, documentnumber, is_sync, status, "
                " retry_count, traceid, createdate, createby) "
                "VALUES (999999, %s, 410, 'TEST-ITEM', 'TEST-LOT', 1, 'M1M1', "
                " 'A-01-01', 'Floor-Core', 'C1N', 'MP999999', 0, 'PENDING', 0, 'TEST-TRACE', NOW(), 'pytest')",
                (w.API_QTY_MOVE,)
            )
            conn.commit()
            test_id = cur.lastrowid
            repo = w._Repo(conn)

            assert repo.claim(test_id) is True
            assert repo.claim(test_id) is False      # 同一行 45s 内不能再被抢占

            cur.execute("SELECT status FROM kr_csi_interface WHERE id = %s", (test_id,))
            assert cur.fetchone()['status'] == 'SUBMITTING'
            # 已被抢占的行不会立刻再被 fetch 出来
            assert test_id not in {r['id'] for r in repo.fetch_candidates(limit=50)}
        finally:
            try:
                cur.execute("DELETE FROM kr_csi_interface WHERE request_id = 999999")
                conn.commit()
            finally:
                cur.close()
                conn.close()
