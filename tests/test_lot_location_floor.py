# -*- coding: utf-8 -*-
"""卷标录入库位：Lot 余额库位不得为车间线边（Floor*）

背景：卷标录入时按 lot 带出的库位曾经取「QtyOnHand 最大的库位」，而车间线边
（CSI 字面值 'FLoor-Core'，另有 'FLoor-X001' 等）经常是数量最大的那个，
导致录入卷标的库位变成 floor。业务上线边库位的货已从仓库领出，不能作为
发料来源库位，因此 _lot_loc_from_balances 必须排除 floor 库位；
SLLots.Loc 的回退同理。
"""
from app.services.csi_service import CSIClient


class _FakeIDOClient(CSIClient):
    """桩：构造不触网，_get_ido 直接返回预设记录"""

    def __init__(self, lot_locs=None, sl_lots=None):
        super().__init__(siteref='310')
        self._lot_locs = lot_locs or []
        self._sl_lots = sl_lots or []
        self._mrb_flag = False

    def _get_ido(self, ido_name, properties=None, filter_str=None, retry_on_timeout=True):
        if ido_name == 'ue_NAI_SLLotLocs':
            return list(self._lot_locs)
        if ido_name == 'SLLots':
            return list(self._sl_lots)
        return []

    def _loc_mrb_flag(self, item, lot, loc):
        return self._mrb_flag

    def _lot_has_mrb_stock(self, item, lot):
        return False


def _row(loc, qty, mrb='0'):
    return {'Loc': loc, 'QtyOnHand': qty, 'MrbFlag': mrb}


class TestLotLocFromBalancesExcludesFloor:
    def test_picks_largest_non_floor_even_if_floor_is_bigger(self):
        # 线上 5000 > 仓库 2759，旧实现会返回 FLOOR-Core
        client = _FakeIDOClient(lot_locs=[
            _row('FLOOR-Core', 5000),
            _row('RM-E048', 2759.44),
        ])
        assert client._lot_loc_from_balances('B053167', 'INV000000001782') == 'RM-E048'

    def test_all_floor_variants_excluded(self):
        # 'FLoor-Core' / 'FLOOR-CORE' / 'FLoor-X001' 都是线边库位（含 'floor' 子串）
        client = _FakeIDOClient(lot_locs=[
            _row('FLoor-Core', 869.8917),
            _row('FLOOR-CORE', 500),
            _row('FLoor-X001', 514.41),
        ])
        assert client._lot_loc_from_balances('A080507', '202607070000035') is None

    def test_only_floor_returns_none(self):
        client = _FakeIDOClient(lot_locs=[_row('FLOOR-Core', 3782)])
        assert client._lot_loc_from_balances('A080507', '202609050000022') is None

    def test_largest_wins_among_non_floor(self):
        client = _FakeIDOClient(lot_locs=[
            _row('FLoor-Core', 9999),
            _row('RM-D026', 11000),
            _row('RM-D068', 17000),
        ])
        assert client._lot_loc_from_balances('B053340', '202609050000054') == 'RM-D068'

    def test_only_floor_does_not_flag_mrb(self):
        # floor-only 不是「不合格库位」，不能误报 MRB 提示
        client = _FakeIDOClient(
            lot_locs=[_row('FLOOR-Core', 3700)],
            sl_lots=[{'Item': 'A1', 'Lot': 'L1', 'Loc': '',
                      'DerQtyOnHand': 1, 'QtyOnHand': 1, 'LotStatus': 'A', 'Whse': 'W'}],
        )
        info = client.get_lot_info('A1', 'L1')
        assert info['loc'] == ''
        assert info['loc_excluded_mrb'] is False


class TestGetLotInfoFallbackExcludesFloor:
    def test_sllots_loc_floor_not_adopted(self):
        # 余额库位只有 floor → 回退 SLLots.Loc，而它也是 floor → 结果为 ''
        client = _FakeIDOClient(
            lot_locs=[_row('FLOOR-Core', 100)],
            sl_lots=[{'Item': 'A080507', 'Lot': 'L1', 'Loc': 'FLoor-Core',
                      'DerQtyOnHand': 100, 'QtyOnHand': 100, 'LotStatus': 'A', 'Whse': 'W'}],
        )
        info = client.get_lot_info('A080507', 'L1')
        assert info is not None
        assert info['loc'] == ''
        assert info['loc_source'] == ''

    def test_sllots_loc_used_when_non_floor(self):
        client = _FakeIDOClient(
            lot_locs=[],
            sl_lots=[{'Item': 'A1', 'Lot': 'L1', 'Loc': 'RM-D060',
                      'DerQtyOnHand': 10, 'QtyOnHand': 10, 'LotStatus': 'A', 'Whse': 'W'}],
        )
        info = client.get_lot_info('A1', 'L1')
        assert info['loc'] == 'RM-D060'
        assert info['loc_source'] == 'slLots'

    def test_lotlocs_non_floor_wins_over_sllots_floor(self):
        client = _FakeIDOClient(
            lot_locs=[_row('RM-D060', 15000)],
            sl_lots=[{'Item': 'A080507', 'Lot': 'L1', 'Loc': 'FLOOR-Core',
                      'DerQtyOnHand': 15000, 'QtyOnHand': 15000, 'LotStatus': 'A', 'Whse': 'W'}],
        )
        info = client.get_lot_info('A080507', 'L1')
        assert info['loc'] == 'RM-D060'
        assert info['loc_source'] == 'lotlocs'

    def test_der_qty_unaffected_by_loc_filter(self):
        client = _FakeIDOClient(
            lot_locs=[_row('FLoor-Core', 100)],
            sl_lots=[{'Item': 'A1', 'Lot': 'L1', 'Loc': 'FLoor-Core',
                      'DerQtyOnHand': 88.5, 'QtyOnHand': 100, 'LotStatus': 'A', 'Whse': 'W'}],
        )
        info = client.get_lot_info('A1', 'L1')
        assert info['der_qty_on_hand'] == 88.5
        assert info['loc'] == ''
