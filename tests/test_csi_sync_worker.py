# -*- coding: utf-8 -*-
"""CSI 库位转移同步器单元测试

覆盖点：
  1. QUEUED → PENDING → SUCCESS 状态推进（ion 对象 / 数组两种结构）
  2. HTTP 400 入队被拒 → retry_count+1、**单号不变**、只换 traceId、is_sync=0
  3. HTTP 409 CONFLICT → 单号不变、只换 traceId
  4. 连续 3 次失败 → is_sync=2/FAILED_MANUAL + 邮件一次，之后不再被扫描，单号全程不变
  5. from_location 为空 → 查 CSI 库位补值
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
