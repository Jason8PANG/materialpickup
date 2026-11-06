# -*- coding: utf-8 -*-
"""源库位可用性判定（is_usable_source_loc）单测

业务约束：卷标库位 / 移库源库位（from_location）不能是
  1) floor* 车间在制库位（Floor-Core / FLoor-Core / FLOOR-X000，大小写不敏感）
  2) Non-Nettable 库位（CSI NonNetable=1；ue_NAI_SLLotLocs 用 MrbFlag 表达）

实测（NAIGROUP_PRD_410）：B055292 / 202511050000022 的两个库位
  FLoor-Core  QtyOnHand=230.377 MrbFlag=0
  FLOOR-X000  QtyOnHand=319.900 MrbFlag=1（= DerTotalNonNettableStock）
旧实现按「数量最大」会选中 FLOOR-X000，等于从车间库位再往车间库位移库（空转）。
"""
import pytest

from app.services.csi_service import CSIClient, is_usable_source_loc
from app.services.csi_interface import _usable_loc_or_none


class TestIsUsableSourceLoc:
    @pytest.mark.parametrize('loc', [
        'Floor-Core', 'FLOOR-X000', 'floor-core', 'FLoor-Core', 'FLoor-X001', '  Floor ',
    ])
    def test_floor_variants_rejected(self, loc):
        assert is_usable_source_loc(loc) is False

    @pytest.mark.parametrize('loc', ['Stock', 'M1M1', 'RM-P040-1', 'RM-A010-1-001', 'FG-RMA'])
    def test_normal_locations_accepted(self, loc):
        assert is_usable_source_loc(loc) is True

    @pytest.mark.parametrize('value', [1, '1', True, 'true', 'TRUE', 'Y'])
    def test_non_netable_rejected(self, value):
        assert is_usable_source_loc('RM-P040-1', value) is False

    @pytest.mark.parametrize('value', [0, '0', False, 'false', None, '', 'N'])
    def test_nettable_accepted(self, value):
        assert is_usable_source_loc('RM-P040-1', value) is True

    @pytest.mark.parametrize('loc', [None, '', '   '])
    def test_empty_rejected(self, loc):
        assert is_usable_source_loc(loc) is False

    def test_floor_wins_over_non_netable(self):
        # 两类都命中时仍是 False，不因参数多少改变结论
        assert is_usable_source_loc('FLOOR-X000', 1) is False


class _FakeIDOClient(CSIClient):
    """桩：不触网，_get_ido 返回预设记录"""

    def __init__(self, lot_locs=None):
        super().__init__(siteref='410')
        self._lot_locs = lot_locs or []

    def _get_ido(self, ido_name, properties=None, filter_str=None, retry_on_timeout=True):
        if ido_name == 'ue_NAI_SLLotLocs':
            return list(self._lot_locs)
        return []


def _row(loc, qty, mrb='0'):
    return {'Loc': loc, 'QtyOnHand': qty, 'MrbFlag': mrb}


class TestLotLocFromBalances:
    def test_non_netable_not_picked_even_if_largest(self):
        """B055292 实况：FLOOR-X000 数量最大但 MrbFlag=1，不能选它"""
        client = _FakeIDOClient(lot_locs=[
            _row('RM-E048', 100.0, '0'),
            _row('FLOOR-X000', 319.9, '1'),
        ])
        assert client._lot_loc_from_balances('B055292', '202511050000022') == 'RM-E048'

    def test_all_candidates_unusable_returns_none(self):
        client = _FakeIDOClient(lot_locs=[
            _row('FLoor-Core', 230.377, '0'),
            _row('FLOOR-X000', 319.9, '1'),
        ])
        assert client._lot_loc_from_balances('B055292', '202511050000022') is None

    def test_get_lot_location_returns_none_for_floor_only_lot(self):
        """A000444 实况：只有 Floor-Core 有货 → 必须返回 None，不能带出车间库位"""
        client = _FakeIDOClient(lot_locs=[_row('FLoor-Core', 217.7821, '0')])
        client.get_lot_info = lambda item, lot: {'loc': '', 'loc_source': '',
                                                'loc_excluded_mrb': False}
        assert client.get_lot_location('A000444', '202611010000045') is None


class TestUsableLocOrNone:
    def test_usable_passthrough(self):
        assert _usable_loc_or_none('RM-P040-1', '261030034', 'B055292', 'L1', 'kr_wire_coil') == 'RM-P040-1'

    @pytest.mark.parametrize('loc', ['FLOOR-X000', 'FLoor-Core', None, ''])
    def test_unusable_becomes_none(self, loc):
        assert _usable_loc_or_none(loc, '261030034', 'B055292', 'L1', 'kr_wire_coil') is None
