# -*- coding: utf-8 -*-
"""CSIClient 工单物料 Backflush 查询 / 取消（sl-job-issue 前置条件）

覆盖点：
  1. get_job_matl_backflush：'1'/'0'/1/0/True 各种形态统一成 bool，并带出 _ItemId
  2. 查不到 → None
  3. clear_job_matl_backflush：已为 0 不写库；缺 _ItemId 不放行；更新成功返回 (True, ...)
  4. update 401 → 刷新 token 重试一次
  5. update 非 2xx / 异常 → (False, ...)，不抛出

全程 mock IDO/HTTP，绝不真实修改 ERP 数据。
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.csi_service import (  # noqa: E402
    BF_MSG_ALREADY_ZERO,
    BF_MSG_CLEARED,
    CSIClient,
    _to_bool,
)


def _client():
    c = CSIClient('410')
    c._token = 'tok_old'
    c._token_expires_at = 9e18
    return c


def _resp(code=200, payload=None):
    r = MagicMock()
    r.status_code = code
    if payload is None:
        r.json.side_effect = ValueError('not json')
    else:
        r.json.return_value = payload
    r.raise_for_status.side_effect = (
        None if 200 <= code < 300 else RuntimeError(f'HTTP {code}'))
    return r


# ====================================================================== #
#  get_job_matl_backflush
# ====================================================================== #
class TestGetJobMatlBackflush:
    @pytest.mark.parametrize('raw,expected', [
        ('1', True), ('0', False), (1, True), (0, False), (True, True), (False, False),
        ('true', True), ('Y', True), (None, False),
    ])
    def test_backflush_value_normalized(self, raw, expected):
        c = _client()
        c._get_ido = MagicMock(return_value=[
            {'Job': 'J000004862', 'Suffix': '5', 'Item': 'F010013',
             'Backflush': raw, '_ItemId': 'PBT=[jobmatl] jm.ID=[8967ba90]'}])
        assert c.get_job_matl_backflush('J000004862', 5, 'F010013') == {
            'backflush': expected, 'item_id': 'PBT=[jobmatl] jm.ID=[8967ba90]'}

    def test_filter_and_properties(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[])
        c.get_job_matl_backflush('J000004862', '0005', 'F010013')
        _args, kwargs = c._get_ido.call_args
        assert _args[0] == 'SLJobMatls' or kwargs.get('ido_name') == 'SLJobMatls'
        assert kwargs['properties'] == ['Job', 'Suffix', 'Item', 'Backflush']
        assert kwargs['filter_str'] == (
            "Job = N'J000004862' And Suffix = 5 And Item = N'F010013'")

    def test_not_found_returns_none(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[])
        assert c.get_job_matl_backflush('J000004862', 5, 'F010013') is None

    def test_query_exception_returns_none(self):
        c = _client()
        c._get_ido = MagicMock(side_effect=RuntimeError('ido down'))
        assert c.get_job_matl_backflush('J000004862', 5, 'F010013') is None

    def test_to_bool_helper(self):
        assert _to_bool('1') is True and _to_bool(1) is True
        assert _to_bool('0') is False and _to_bool(0) is False
        assert _to_bool(None) is False and _to_bool('') is False


# ====================================================================== #
#  clear_job_matl_backflush
# ====================================================================== #
class TestClearJobMatlBackflush:
    def test_already_zero_does_not_write(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[
            {'Backflush': '0', '_ItemId': 'x'}])
        with patch('app.services.csi_service.httpx.post') as post:
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')
        assert (ok, msg) == (True, BF_MSG_ALREADY_ZERO)
        post.assert_not_called()

    def test_not_found_fails(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[])
        with patch('app.services.csi_service.httpx.post') as post:
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')
        assert ok is False
        assert msg.startswith('未找到工单物料 J000004862-0005 / F010013')
        post.assert_not_called()

    def test_missing_item_id_fails(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[{'Backflush': '1'}])
        with patch('app.services.csi_service.httpx.post') as post:
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')
        assert ok is False
        assert '_ItemId' in msg
        post.assert_not_called()

    def test_update_payload_and_success(self, caplog):
        c = _client()
        c._get_ido = MagicMock(return_value=[
            {'Backflush': '1', '_ItemId': 'PBT=[jobmatl] jm.ID=[8967ba90]'}])
        with patch('app.services.csi_service.httpx.post',
                   return_value=_resp(200, {'Items': []})) as post, \
             caplog.at_level('INFO', logger='app.services.csi_service'):
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')

        assert (ok, msg) == (True, BF_MSG_CLEARED)
        args, kwargs = post.call_args
        assert args[0].endswith('/ido/update/SLJobMatls')
        assert kwargs['params'] == {'refresh': 'true'}
        assert kwargs['timeout'] == 60
        assert kwargs['verify'] is False
        assert kwargs['headers']['X-Infor-MongooseConfig'] == c._company
        assert kwargs['json'] == {'Changes': [{
            'Action': 2,
            '_ItemId': 'PBT=[jobmatl] jm.ID=[8967ba90]',
            'Properties': [
                {'IsNull': False, 'Modified': True, 'Name': 'Backflush', 'Value': 0},
                {'IsNull': False, 'Modified': False, 'Name': 'Job', 'Value': 'J000004862'},
                {'IsNull': False, 'Modified': False, 'Name': 'Suffix', 'Value': 5},
                {'IsNull': False, 'Modified': False, 'Name': 'Item', 'Value': 'F010013'},
            ],
            'UpdateLocking': 1,
        }]}
        assert '工单 J000004862-0005 物料 F010013 Backflush 1->0' in caplog.text

    def test_update_401_refresh_and_retry(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[{'Backflush': '1', '_ItemId': 'id1'}])
        c._fetch_token = MagicMock(return_value=('tok_new', 3600))
        with patch('app.services.csi_service.httpx.post',
                   side_effect=[_resp(401), _resp(200, {})]) as post:
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')

        assert (ok, msg) == (True, BF_MSG_CLEARED)
        assert post.call_count == 2
        assert c._fetch_token.call_count == 1
        assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer tok_new'

    def test_update_non_2xx_fails(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[{'Backflush': '1', '_ItemId': 'id1'}])
        with patch('app.services.csi_service.httpx.post', return_value=_resp(500)):
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')
        assert ok is False
        assert 'HTTP 500' in msg

    def test_update_exception_fails(self):
        c = _client()
        c._get_ido = MagicMock(return_value=[{'Backflush': '1', '_ItemId': 'id1'}])
        with patch('app.services.csi_service.httpx.post', side_effect=RuntimeError('boom')):
            ok, msg = c.clear_job_matl_backflush('J000004862', 5, 'F010013')
        assert ok is False
        assert 'boom' in msg
