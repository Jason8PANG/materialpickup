# -*- coding: utf-8 -*-
"""CSIClient.is_lot_tracked + LotTracked 缓存 单测（不发真实 HTTP）"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.services import csi_service  # noqa: E402
from app.services.csi_service import CSIClient  # noqa: E402


class DummyClient(CSIClient):
    """记录调用次数的假客户端：_get_ido 不触网"""

    def __init__(self, records=None, exc=None):
        super().__init__('310')
        self.records = records
        self.exc = exc
        self.calls = 0
        self.last = None

    def _get_ido(self, ido_name, properties=None, filter_str=None, **kwargs):
        self.calls += 1
        self.last = (ido_name, properties, filter_str)
        if self.exc:
            raise self.exc
        return self.records or []


@pytest.fixture(autouse=True)
def _clean_cache():
    csi_service._clear_lot_tracked_cache()
    yield
    csi_service._clear_lot_tracked_cache()


@pytest.mark.parametrize('value,expected', [
    ('1', True), (1, True),
    ('0', False), (0, False),
    (None, False), ('', False),
])
def test_value_parsing(value, expected):
    c = DummyClient([{'Item': 'X1', 'LotTracked': value}])
    assert c.is_lot_tracked('X1') is expected


def test_query_shape_and_default():
    c = DummyClient([{'Item': 'X1', 'LotTracked': '1'}])
    c.is_lot_tracked('X1')
    ido, props, filt = c.last
    assert ido == 'SLItems'
    assert props == ['Item', 'LotTracked']
    assert filt == "Item = N'X1'"


def test_quote_escaped():
    c = DummyClient([{'Item': "A'B", 'LotTracked': '1'}])
    c.is_lot_tracked("A'B")
    assert c.last[2] == "Item = N'A''B'"


def test_no_record_returns_none_and_not_cached():
    c = DummyClient([])
    assert c.is_lot_tracked('X1') is None
    assert c.is_lot_tracked('X1') is None
    assert c.calls == 2  # None 不入缓存


def test_exception_returns_none_and_not_cached():
    c = DummyClient(exc=RuntimeError('boom'))
    assert c.is_lot_tracked('X1') is None
    assert c.is_lot_tracked('X1') is None
    assert c.calls == 2


def test_cache_hit_skips_http():
    c = DummyClient([{'Item': 'X1', 'LotTracked': '1'}])
    assert c.is_lot_tracked('X1') is True
    assert c.is_lot_tracked('X1') is True
    assert c.calls == 1


def test_cache_expires_by_ttl(monkeypatch):
    monkeypatch.setattr(Config, 'CSI_LOT_TRACKED_TTL_SECONDS', 300)
    c = DummyClient([{'Item': 'X1', 'LotTracked': '0'}])
    assert c.is_lot_tracked('X1') is False
    # 手工把时间戳拨老 301 秒
    key = ('310', 'X1')
    tracked, _ = csi_service._LOT_TRACKED_CACHE[key]
    csi_service._LOT_TRACKED_CACHE[key] = (tracked, 0)
    assert c.is_lot_tracked('X1') is False
    assert c.calls == 2


def test_cache_isolated_by_site():
    c1 = DummyClient([{'Item': 'X1', 'LotTracked': '1'}])
    c2 = DummyClient([{'Item': 'X1', 'LotTracked': '0'}])
    c2._siteref = '410'
    assert c1.is_lot_tracked('X1') is True
    assert c2.is_lot_tracked('X1') is False
    assert c1.calls == 1 and c2.calls == 1


def test_default_ttl_is_300():
    assert Config.CSI_LOT_TRACKED_TTL_SECONDS == 300
