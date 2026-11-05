# -*- coding: utf-8 -*-
"""CSI 同步前「批次/货位存货」自动对齐单元测试

两层：
  1. 纯函数 align_batch_stock_quantity（不碰 CSI / DB）
  2. 接入逻辑 _align_batch_stock + _process_row（FakeRepo + mock CSIClient，绝不访问真实 CSI / 数据库）

业务规则（**双向对齐**）：FT 消耗量换算是 ceil 向上进位（13.2419 → 13.242），提交时比批次
存货多 0.0001，CSI 报「发料数量 13.242 大于批次存货 13.2419」拒收。只要本笔数量与批次存货
差异小于 0.01（略大略小都算），就取批次存货量作为消耗数量（向下截断到 3 位小数）；
差异 ≥ 0.01 视为真实差异，照常提交转人工。
"""
import sys
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import csi_sync_worker as w  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.csi_floor_align import align_batch_stock_quantity  # noqa: E402


# ====================================================================== #
#  测试替身
# ====================================================================== #
class FakeCursor:
    """记录 SQL；UPDATE / SUM 都返回空结果集"""

    def __init__(self):
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return 1

    def fetchone(self):
        return None

    def close(self):
        self.closed = True


class FakeRepo:
    """最小仓储替身：暴露 _cursor / conn.commit / conn.rollback / claim / update"""

    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.conn = self
        self.updates = []
        self.claimed = []

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def _cursor(self):
        return self.cursor_obj

    def claim(self, row_id):
        self.claimed.append(row_id)
        return True

    def update(self, row_id, **kw):
        self.updates.append((row_id, kw))

    # -- 断言辅助 ----------------------------------------------------------
    def update_params(self):
        return [p for s, p in self.cursor_obj.executed if 'SET quantity = %s' in s]


def _row(**over):
    """待同步行（默认模拟 sl-qty-move：M1M1 → Floor-Core）"""
    base = {
        'id': 1966, 'request_id': None, 'api_name': w.API_QTY_MOVE, 'siteRef': 410,
        'item': 'A000747', 'lot': '202610200000011', 'quantity': Decimal('13.2420'),
        'um': 'FT', 'job': None, 'warehouse': 'M1M1',
        'from_location': 'M1M1', 'to_location': 'Floor-Core',
        'documentnumber': 'MP1966', 'status': 'SUBMITTING',
        'retry_count': 0, 'traceid': 'MP1966-A000747-bbb', 'is_sync': 0,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _align_on(monkeypatch):
    monkeypatch.setattr(Config, 'CSI_BATCH_STOCK_ALIGN', True)
    monkeypatch.setattr(Config, 'CSI_BATCH_ALIGN_TOLERANCE', 0.01)


def _patch_client(qty=13.2419, um='FT'):
    client = MagicMock()
    client.get_lot_loc_qty.return_value = {'qty': qty, 'um': um, 'whse': 'M1M1',
                                          'loc': 'Floor-Core'}
    return patch.object(w, 'CSIClient', MagicMock(return_value=client)), client


# ====================================================================== #
#  1. 纯函数
# ====================================================================== #
class TestPureFunction:
    def test_overshoot_tiny_aligns_down(self):
        """略大：13.242 vs 13.2419（差异 0.0001）→ 13.241，且 ≤ 存货、≤ 3 位小数"""
        new, reason = align_batch_stock_quantity(13.242, 13.2419, 'FT', 'FT')
        assert new == 13.241
        assert new <= 13.2419
        assert round(new, 3) == new
        assert reason['action'] == 'align'
        assert reason['batch_qty'] == 13.2419      # 原始存货值留档
        assert reason['new_qty'] == 13.241
        assert reason['diff'] == pytest.approx(0.0001)

    def test_undershoot_tiny_aligns_too(self):
        """略小：13.2417 vs 13.2419（差异 0.0002）→ 双向对齐，同样取存货量"""
        new, reason = align_batch_stock_quantity(13.2417, 13.2419, 'FT', 'FT')
        assert new is not None                     # 双向核心：略小也要对齐
        assert new == 13.241
        assert new <= 13.2419
        assert reason['action'] == 'align'
        assert reason['diff'] == pytest.approx(-0.0002)

    def test_equal_quantity_not_touched(self):
        """本笔已等于批次存货 → 不动（避免把 13.2419 截断成 13.241）"""
        new, reason = align_batch_stock_quantity(13.2419, 13.2419, 'FT', 'FT')
        assert new is None
        assert '已等于' in reason['reason']

    def test_beyond_tolerance_not_aligned(self):
        """差异 0.2581 > 0.01 → 不调整（照常提交，被拒后转人工）"""
        new, reason = align_batch_stock_quantity(13.5, 13.2419, 'FT', 'FT')
        assert new is None
        assert reason['action'] == 'skip'
        assert '超过阈值' in reason['reason']

    def test_far_below_not_aligned(self):
        """远小于存货（13.0 vs 13.2419，差异 0.2419）→ 属于真实少发，不放大数量"""
        new, reason = align_batch_stock_quantity(13.0, 13.2419, 'FT', 'FT')
        assert new is None
        assert '超过阈值' in reason['reason']

    def test_unit_mismatch_not_aligned(self):
        new, reason = align_batch_stock_quantity(13.242, 13.2419, 'FT', 'M')
        assert new is None
        assert '单位不一致' in reason['reason']

    def test_missing_unit_not_aligned(self):
        assert align_batch_stock_quantity(13.242, 13.2419, None, 'FT')[0] is None
        assert align_batch_stock_quantity(13.242, 13.2419, 'FT', None)[0] is None

    def test_zero_or_negative_batch_not_aligned(self):
        """存货 0 / 负数 → 不替业务决定扣 0"""
        assert align_batch_stock_quantity(13.242, 0, 'FT', 'FT')[0] is None
        assert align_batch_stock_quantity(13.242, -1, 'FT', 'FT')[0] is None

    def test_non_positive_row_qty_not_aligned(self):
        assert align_batch_stock_quantity(0, 13.2419, 'FT', 'FT')[0] is None
        assert align_batch_stock_quantity(-1, 13.2419, 'FT', 'FT')[0] is None

    def test_invalid_numbers_not_aligned(self):
        assert align_batch_stock_quantity('abc', 13.2419, 'FT', 'FT')[0] is None
        assert align_batch_stock_quantity(13.242, None, 'FT', 'FT')[0] is None

    def test_result_never_exceeds_batch(self):
        """随机抽查（两个方向）：对齐结果都必须 ≤ 存货，且 ≤ 3 位小数"""
        cases = [(13.242, 13.2419), (13.2417, 13.2419), (100.005, 100.0), (99.9995, 100.0),
                 (0.1001, 0.1), (0.0999, 0.1), (1000.0009, 1000.0001), (2.0001, 2.0)]
        for row_qty, stock in cases:
            new, reason = align_batch_stock_quantity(row_qty, stock, 'FT', 'FT')
            if new is not None:
                assert new <= stock, (row_qty, stock, new)
                assert round(new, 3) == new


# ====================================================================== #
#  2. 接入逻辑（_align_batch_stock）
# ====================================================================== #
class TestAlignBatchStockHook:
    def test_qty_move_row_aligned_end_to_end(self):
        """qty-move 差异 0.0001：接口表 quantity 被改写、内存行同步、审计留痕"""
        repo = FakeRepo()
        row = _row()
        stats = {}
        ctx, client = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation') as audit:
            out = w._align_batch_stock(repo, row, w.API_QTY_MOVE, stats)

        assert out['quantity'] == 13.241
        assert repo.update_params() == [(13.241, 1966)]
        assert repo.commits == 1
        assert stats['batch_aligned'] == 1
        # qty-move 查的是源库位
        client.get_lot_loc_qty.assert_called_once_with('A000747', '202610200000011', 'M1M1')
        assert audit.call_args.args[:2] == ('sync_worker', 'sync_worker/batch_align')

    def test_job_issue_row_aligned(self):
        """job-issue 查目标库位（Floor-Core）存货，同样对齐"""
        repo = FakeRepo()
        row = _row(api_name=w.API_JOB_ISSUE, from_location=None, job='J000005098-0000')
        stats = {}
        ctx, client = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'):
            out = w._align_batch_stock(repo, row, w.API_JOB_ISSUE, stats)
        assert out['quantity'] == 13.241
        assert repo.update_params() == [(13.241, 1966)]
        assert stats['batch_aligned'] == 1
        client.get_lot_loc_qty.assert_called_once_with('A000747', '202610200000011', 'Floor-Core')

    def test_undershoot_row_aligned(self):
        """略小（13.2417 vs 13.2419）同样被对齐到存货量"""
        repo = FakeRepo()
        row = _row(quantity=Decimal('13.2417'))
        ctx, _client = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'):
            out = w._align_batch_stock(repo, row, w.API_QTY_MOVE, {})
        assert out['quantity'] == 13.241
        assert repo.update_params() == [(13.241, 1966)]

    def test_stock_far_larger_not_aligned(self):
        """不误伤：存货 100 远大于本笔 13.242 → 数量不变、不写库"""
        repo = FakeRepo()
        row = _row()
        ctx, client = _patch_client(qty=100.0)
        with ctx, patch.object(w, 'log_operation') as audit:
            out = w._align_batch_stock(repo, row, w.API_QTY_MOVE, {})

        assert out['quantity'] == Decimal('13.2420')
        assert repo.update_params() == []
        assert repo.commits == 0
        audit.assert_not_called()
        client.get_lot_loc_qty.assert_called_once()

    def test_switch_off(self, monkeypatch):
        """开关关闭 → 不查 CSI、不改量"""
        monkeypatch.setattr(Config, 'CSI_BATCH_STOCK_ALIGN', False)
        repo = FakeRepo()
        ctx, client = _patch_client(qty=13.2419)
        with ctx:
            out = w._align_batch_stock(repo, _row(), w.API_QTY_MOVE, {})
        assert out['quantity'] == Decimal('13.2420')
        client.get_lot_loc_qty.assert_not_called()

    def test_other_api_not_aligned(self):
        """只覆盖 job-issue / qty-move；misc 一律原样返回"""
        repo = FakeRepo()
        ctx, client = _patch_client(qty=13.2419)
        with ctx:
            out = w._align_batch_stock(repo, _row(api_name=w.API_MISC_ISSUE),
                                       w.API_MISC_ISSUE, {})
        assert out['quantity'] == Decimal('13.2420')
        client.get_lot_loc_qty.assert_not_called()

    def test_csi_returns_none_row_unchanged(self):
        """查不到该库位余额 → 不阻断同步，原样返回且不碰数据库"""
        repo = FakeRepo()
        client = MagicMock()
        client.get_lot_loc_qty.return_value = None
        with patch.object(w, 'CSIClient', MagicMock(return_value=client)):
            out = w._align_batch_stock(repo, _row(), w.API_QTY_MOVE, {})
        assert out['quantity'] == Decimal('13.2420')
        assert repo.cursor_obj.executed == []
        assert repo.commits == 0

    def test_csi_exception_returns_row_unchanged(self):
        """CSI 抛异常 → 吞掉异常，原样返回不抛出"""
        repo = FakeRepo()
        client = MagicMock()
        client.get_lot_loc_qty.side_effect = RuntimeError('timeout')
        with patch.object(w, 'CSIClient', MagicMock(return_value=client)):
            out = w._align_batch_stock(repo, _row(), w.API_QTY_MOVE, {})
        assert out['quantity'] == Decimal('13.2420')
        assert repo.commits == 0

    def test_missing_loc_row_unchanged(self):
        """缺库位（from_location / to_location 为空）→ 不查 CSI、不改量"""
        repo = FakeRepo()
        ctx, client = _patch_client(qty=13.2419)
        with ctx:
            assert w._align_batch_stock(repo, _row(from_location=None),
                                        w.API_QTY_MOVE, {})['quantity'] == Decimal('13.2420')
            assert w._align_batch_stock(repo, _row(from_location=None, to_location=None),
                                        w.API_QTY_MOVE, {})['quantity'] == Decimal('13.2420')
        client.get_lot_loc_qty.assert_not_called()

    def test_same_lot_loc_queried_once(self):
        """同一轮内同 (site,item,lot,loc) 只查一次 CSI"""
        cache = {}
        ctx, client = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'):
            w._align_batch_stock(FakeRepo(), _row(id=1), w.API_QTY_MOVE, {}, cache)
            w._align_batch_stock(FakeRepo(), _row(id=2), w.API_QTY_MOVE, {}, cache)
        assert client.get_lot_loc_qty.call_count == 1

    def test_never_touches_consumption_table(self):
        """只写 kr_csi_interface，绝不动实耗表"""
        repo = FakeRepo()
        ctx, _ = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'):
            w._align_batch_stock(repo, _row(), w.API_QTY_MOVE, {})
        sqls = ' '.join(s for s, _ in repo.cursor_obj.executed)
        assert 'kr_wire_coil_consumption' not in sqls


# ====================================================================== #
#  3. 端到端：_process_row 提交前改量，payload 用的是新数量
# ====================================================================== #
class TestProcessRowEndToEnd:
    def test_payload_uses_aligned_quantity(self):
        repo = FakeRepo()
        row = _row()
        stats = {'skipped': 0, 'claimed': 0, 'success': 0, 'batch_aligned': 0}
        captured = {}

        def fake_submit(payload, api_name=None, sleep_func=None):
            captured['payload'] = payload
            return {'kind': 'success'}

        ctx, _ = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'), \
                patch.object(w, '_submit_and_poll', side_effect=fake_submit):
            w._process_row(repo, row, stats, lambda s: None, loc_cache={}, floor_cache={})

        assert captured['payload']['quantity'] == 13.241
        assert captured['payload']['quantity'] <= 13.2419
        assert stats['batch_aligned'] == 1
        assert repo.update_params() == [(13.241, 1966)]

    def test_payload_keeps_quantity_when_stock_is_ample(self):
        repo = FakeRepo()
        row = _row()
        stats = {'skipped': 0, 'claimed': 0, 'success': 0, 'batch_aligned': 0}
        captured = {}

        def fake_submit(payload, api_name=None, sleep_func=None):
            captured['payload'] = payload
            return {'kind': 'success'}

        ctx, _ = _patch_client(qty=100.0)
        with ctx, patch.object(w, 'log_operation'), \
                patch.object(w, '_submit_and_poll', side_effect=fake_submit):
            w._process_row(repo, row, stats, lambda s: None, loc_cache={}, floor_cache={})

        assert captured['payload']['quantity'] == 13.242
        assert stats['batch_aligned'] == 0

    def test_switch_off_keeps_quantity(self, monkeypatch):
        monkeypatch.setattr(Config, 'CSI_BATCH_STOCK_ALIGN', False)
        repo = FakeRepo()
        row = _row()
        stats = {'skipped': 0, 'claimed': 0, 'success': 0, 'batch_aligned': 0}
        captured = {}

        def fake_submit(payload, api_name=None, sleep_func=None):
            captured['payload'] = payload
            return {'kind': 'success'}

        ctx, client = _patch_client(qty=13.2419)
        with ctx, patch.object(w, 'log_operation'), \
                patch.object(w, '_submit_and_poll', side_effect=fake_submit):
            w._process_row(repo, row, stats, lambda s: None, loc_cache={}, floor_cache={})

        assert captured['payload']['quantity'] == 13.242
        assert stats['batch_aligned'] == 0
        client.get_lot_loc_qty.assert_not_called()
