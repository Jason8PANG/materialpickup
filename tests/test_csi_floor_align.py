# -*- coding: utf-8 -*-
"""CSI 同步前 Floor-Core 余量对齐单元测试

两层：
  1. 纯函数 align_floor_stock_quantity（不碰 CSI / DB）
  2. 接入逻辑 _align_floor_stock（FakeRepo + FakeCursor，绝不访问真实 CSI / 数据库）

业务规则：消耗类工单发料提交前查该 lot 在 CSI Floor-Core 的实时余量，
与本笔消耗量差异 ≤ 0.1 时把本笔数量改成实际余量；超过阈值照常提交（失败转人工）。
"""
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import csi_sync_worker as w  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.csi_floor_align import align_floor_stock_quantity  # noqa: E402


# ====================================================================== #
#  测试替身
# ====================================================================== #
class FakeCursor:
    """记录 SQL；就绪返回 SUM(quantity) 的 pending 值"""

    def __init__(self, pending=0.0):
        self.pending = pending
        self.executed = []
        self.closed = False
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if 'COALESCE(SUM(quantity)' in sql:
            self._rows = [{'pending': self.pending}]
        else:
            self._rows = []
        return 1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        self.closed = True


class FakeRepo:
    """最小仓储替身：暴露 _cursor / conn.commit / conn.rollback（_Repo 的用法）"""

    def __init__(self, pending=0.0):
        self.cursor_obj = FakeCursor(pending)
        self.commits = 0
        self.rollbacks = 0
        self.conn = self

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def _cursor(self):
        return self.cursor_obj

    # -- 断言辅助 ----------------------------------------------------------
    def update_params(self):
        return [p for s, p in self.cursor_obj.executed if 'SET quantity = %s' in s]

    def pending_params(self):
        return [p for s, p in self.cursor_obj.executed if 'COALESCE(SUM(quantity)' in s]


def _row(**over):
    """消耗类工单发料行：from_location 为空、库位是 Floor-Core"""
    base = {
        'id': 9001, 'request_id': None, 'api_name': w.API_JOB_ISSUE, 'siteRef': 410,
        'item': 'A000871', 'lot': '202510210000006', 'quantity': 100.0, 'um': 'FT',
        'job': 'J000029867-0001', 'warehouse': 'C1M1',
        'from_location': None, 'to_location': 'Floor-Core',
        'documentnumber': 'MP9001', 'status': 'SUBMITTING',
        'retry_count': 0, 'traceid': 'MP9001-A000871-aaa', 'is_sync': 0,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _align_on(monkeypatch):
    monkeypatch.setattr(Config, 'CSI_FLOOR_STOCK_ALIGN', True)
    monkeypatch.setattr(Config, 'CSI_FLOOR_ALIGN_TOLERANCE', 0.1)


def _client(qty, um='FT'):
    client = MagicMock()
    client.get_lot_floor_core_qty.return_value = {'qty': qty, 'um': um, 'whse': 'C1M1'}
    return client


def _patch_client(qty=100.0, um='FT'):
    client = _client(qty, um)
    return patch.object(w, 'CSIClient', MagicMock(return_value=client)), client


# ====================================================================== #
#  1. 纯函数
# ====================================================================== #
class TestPureFunction:
    def test_within_tolerance_downward(self):
        """余量略小于本笔（100 vs 99.95）→ 改成余量"""
        new, reason = align_floor_stock_quantity(100.0, 99.95, 0, 'FT', 'FT')
        assert new == 99.95
        assert reason['action'] == 'align'

    def test_within_tolerance_upward(self):
        """余量略大于本笔（100 vs 100.05）→ 改成余量"""
        new, reason = align_floor_stock_quantity(100.0, 100.05, 0, 'FT', 'FT')
        assert new == 100.05
        assert reason['action'] == 'align'

    def test_exactly_at_tolerance_aligns(self):
        """恰好等于阈值（0.1）→ 对齐（浮点误差不得误判为超出）"""
        new, _ = align_floor_stock_quantity(100.0, 100.1, 0, 'FT', 'FT')
        assert new == 100.1

    def test_beyond_tolerance_not_aligned(self):
        """差异 20 > 0.1 → 不调整，照常提交"""
        new, reason = align_floor_stock_quantity(100.0, 80.0, 0, 'FT', 'FT')
        assert new is None
        assert reason['action'] == 'skip'

    def test_zero_floor_not_aligned(self):
        """CSI 无余量（0）→ 不替业务决定扣 0"""
        new, _ = align_floor_stock_quantity(100.0, 0.0, 0, 'FT', 'FT')
        assert new is None

    def test_pending_exhausts_stock_not_aligned(self):
        """余量 10 已被其它待扣行占用 20 → available ≤ 0，不对齐"""
        new, reason = align_floor_stock_quantity(10.0, 10.0, 20.0, 'FT', 'FT')
        assert new is None
        assert reason['available'] == -10.0

    def test_pending_taken_into_account(self):
        """余量 100、待扣 30、本笔 70 → available=70 对齐；本笔 95 → 差异 25 不对齐"""
        new, _ = align_floor_stock_quantity(70.0, 100.0, 30.0, 'FT', 'FT')
        assert new == 70.0
        new2, _ = align_floor_stock_quantity(95.0, 100.0, 30.0, 'FT', 'FT')
        assert new2 is None

    def test_unit_mismatch_not_aligned(self):
        """单位不一致（FT vs M）→ 放弃对齐，不做换算"""
        new, reason = align_floor_stock_quantity(100.0, 100.02, 0, 'FT', 'M')
        assert new is None
        assert '单位不一致' in reason['reason']

    def test_missing_unit_not_aligned(self):
        new, _ = align_floor_stock_quantity(100.0, 100.02, 0, None, 'FT')
        assert new is None

    def test_three_decimals_ft_ceil(self):
        """FT 3 位小数向上进位：87.9921 → 87.993"""
        new, _ = align_floor_stock_quantity(87.99, 87.9921, 0, 'FT', 'FT')
        assert new == 87.993

    def test_non_numeric_qty_not_aligned(self):
        new, reason = align_floor_stock_quantity('abc', 100.0, 0, 'FT', 'FT')
        assert new is None
        assert '数量非法' in reason['reason']

    def test_non_positive_row_qty_not_aligned(self):
        new, _ = align_floor_stock_quantity(0, 100.0, 0, 'FT', 'FT')
        assert new is None


# ====================================================================== #
#  2. 接入逻辑（_align_floor_stock）
# ====================================================================== #
class TestAlignFloorStockHook:
    def test_consumption_row_aligned_end_to_end(self):
        """消耗行差异 ≤0.1：接口表 quantity 被改写、内存行同步、审计留痕"""
        repo = FakeRepo(pending=0.0)
        row = _row(quantity=100.0)
        stats = {}
        ctx, client = _patch_client(qty=99.95, um='FT')
        with ctx, patch.object(w, 'log_operation') as audit:
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, stats, {})

        assert out['quantity'] == 99.95          # payload 取的就是这个
        assert repo.update_params() == [(99.95, 9001)]
        assert repo.commits == 1
        assert stats['floor_aligned'] == 1
        client.get_lot_floor_core_qty.assert_called_once_with('A000871', '202510210000006')
        assert audit.call_args.args[:2] == ('sync_worker', 'sync_worker/floor_align')

    def test_pending_rows_reduce_available(self):
        """同 lot 其它待扣行占用量参与计算：available = 余量 − 待扣"""
        repo = FakeRepo(pending=30.0)
        row = _row(quantity=70.0)
        stats = {}
        ctx, client = _patch_client(qty=100.0)
        with ctx, patch.object(w, 'log_operation'):
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, stats, {})

        assert out['quantity'] == 70.0
        assert repo.pending_params() == [('A000871', '202510210000006', 9001)]
        client.get_lot_floor_core_qty.assert_called_once()

    def test_non_floor_target_not_aligned(self):
        """目标库位不是 Floor（如仓库库位）→ 不查 CSI、不改数量"""
        repo = FakeRepo()
        row = _row(to_location='RM-A01')
        stats = {}
        ctx, client = _patch_client(qty=99.95)
        with ctx, patch.object(w, 'log_operation') as audit:
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, stats, {})

        assert out['quantity'] == 100.0
        assert repo.cursor_obj.executed == []
        assert stats == {}
        client.get_lot_floor_core_qty.assert_not_called()
        audit.assert_not_called()

    def test_completed_prep_qty_move_not_aligned(self):
        """完成备料的 job-issue 有 from_location（仓库→Floor-Core 入库）→ 绝不能动"""
        repo = FakeRepo()
        row = _row(from_location='RM-A01')
        ctx, client = _patch_client(qty=99.95)
        with ctx:
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, {}, {})
        assert out['quantity'] == 100.0
        client.get_lot_floor_core_qty.assert_not_called()

    def test_other_api_not_aligned(self):
        """只有 sl-job-issue 才处理；misc / qty-move 一律原样返回"""
        repo = FakeRepo()
        row = _row(api_name=w.API_MISC_ISSUE, from_location=None, to_location='Floor-Core')
        ctx, client = _patch_client(qty=99.95)
        with ctx:
            out = w._align_floor_stock(repo, row, w.API_MISC_ISSUE, {}, {})
        assert out['quantity'] == 100.0
        client.get_lot_floor_core_qty.assert_not_called()

    def test_switch_off(self, monkeypatch):
        """开关关闭 → 不查 CSI"""
        monkeypatch.setattr(Config, 'CSI_FLOOR_STOCK_ALIGN', False)
        repo = FakeRepo()
        row = _row()
        ctx, client = _patch_client(qty=99.95)
        with ctx:
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, {}, {})
        assert out['quantity'] == 100.0
        client.get_lot_floor_core_qty.assert_not_called()

    def test_csi_none_returns_row_unchanged(self):
        """CSI 查不到（None/超时）→ 不阻断同步，原样返回且不碰数据库"""
        repo = FakeRepo()
        row = _row()
        client = MagicMock()
        client.get_lot_floor_core_qty.return_value = None
        with patch.object(w, 'CSIClient', MagicMock(return_value=client)):
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, {}, {})

        assert out['quantity'] == 100.0
        assert repo.cursor_obj.executed == []
        assert repo.commits == 0

    def test_csi_exception_returns_row_unchanged(self):
        """CSI 客户端抛异常 → 吞掉异常，原样返回"""
        repo = FakeRepo()
        row = _row()
        client = MagicMock()
        client.get_lot_floor_core_qty.side_effect = RuntimeError('timeout')
        with patch.object(w, 'CSIClient', MagicMock(return_value=client)):
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, {}, {})
        assert out['quantity'] == 100.0

    def test_beyond_tolerance_submits_original(self):
        """差异超阈值 → 保留原数量（照常提交，由 CSI 拒收后转人工）"""
        repo = FakeRepo()
        row = _row(quantity=100.0)
        ctx, _ = _patch_client(qty=80.0)
        with ctx, patch.object(w, 'log_operation') as audit:
            out = w._align_floor_stock(repo, row, w.API_JOB_ISSUE, {}, {})
        assert out['quantity'] == 100.0
        assert repo.update_params() == []
        audit.assert_not_called()

    def test_invalid_quantity_skipped(self):
        """数量为 None / 非数字 → 跳过（不查 CSI）"""
        repo = FakeRepo()
        ctx, client = _patch_client(qty=99.95)
        with ctx:
            assert w._align_floor_stock(repo, _row(quantity=None), w.API_JOB_ISSUE, {}, {})[
                'quantity'] is None
            assert w._align_floor_stock(repo, _row(quantity='abc'), w.API_JOB_ISSUE, {}, {})[
                'quantity'] == 'abc'
        client.get_lot_floor_core_qty.assert_not_called()

    def test_same_lot_queried_once_per_round(self):
        """同一轮内同 (site,item,lot) 只查一次 CSI（缓存跨行共享）"""
        cache = {}
        ctx, client = _patch_client(qty=100.0)
        with ctx, patch.object(w, 'log_operation'):
            w._align_floor_stock(FakeRepo(), _row(id=1, quantity=100.0),
                                 w.API_JOB_ISSUE, {}, cache)
            w._align_floor_stock(FakeRepo(), _row(id=2, quantity=100.0),
                                 w.API_JOB_ISSUE, {}, cache)
        assert client.get_lot_floor_core_qty.call_count == 1

    def test_never_touches_consumption_table(self):
        """只写 kr_csi_interface，绝不动 kr_wire_coil_consumption 的实耗字段"""
        repo = FakeRepo()
        ctx, _ = _patch_client(qty=99.95)
        with ctx, patch.object(w, 'log_operation'):
            w._align_floor_stock(repo, _row(quantity=100.0), w.API_JOB_ISSUE, {}, {})
        sqls = ' '.join(s for s, _ in repo.cursor_obj.executed)
        assert 'kr_wire_coil_consumption' not in sqls
        assert 'out_length' not in sqls and 'converted_length' not in sqls
