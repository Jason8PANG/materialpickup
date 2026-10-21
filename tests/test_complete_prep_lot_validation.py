# -*- coding: utf-8 -*-
"""完成备料前的批次维护校验（app/routes/warehouse.py: validate_item_lots）单元测试。

覆盖点：
  1. A/B 物料不要求批次（走卷标粒度）
  2. 非 A/B 无批次 → 拦截
  3. 非 A/B 批次 qty=0 → 视为未维护 → 拦截
  4. 批次累计超申请量 → 拦截，提示含物料号与行号
  5. 批次累计等于申请量 / 在浮点容差内 → 放行
  6. 缺料行（short_reason 非空）无批次 → 豁免
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.routes.warehouse import validate_item_lots  # noqa: E402


class FakeCursor:
    def __init__(self, items, lots):
        self.items = [dict(i) for i in items]
        self.lots = [dict(l) for l in lots]
        self._rows = []

    def execute(self, sql, params=None):
        s = ' '.join(sql.split()).lower()
        if s.startswith('select id, part_number, quantity, short_reason from kr_request_item'):
            self._rows = [dict(i) for i in self.items]
        elif s.startswith('select item_id, coalesce(sum(qty)'):
            ids = set(params[1:])
            agg = {}
            for lot in self.lots:
                if lot['item_id'] in ids:
                    total, cnt = agg.get(lot['item_id'], (0.0, 0))
                    total += float(lot['qty'] or 0)
                    cnt += 1 if float(lot['qty'] or 0) > 0 else 0
                    agg[lot['item_id']] = (total, cnt)
            self._rows = [{'item_id': k, 'total_qty': v[0], 'positive_cnt': v[1]}
                          for k, v in agg.items()]
        else:
            self._rows = []
        return len(self._rows)

    def fetchall(self):
        return [dict(r) for r in self._rows]


@pytest.fixture(scope='module')
def flask_app():
    return create_app()


def run(flask_app, items, lots, lang=None):
    with flask_app.test_request_context(headers={'X-Lang': lang} if lang else None):
        return validate_item_lots(FakeCursor(items, lots), 1)


def item(iid, part, qty, short=None):
    return {'id': iid, 'part_number': part, 'quantity': qty, 'short_reason': short}


def lot(item_id, qty):
    return {'item_id': item_id, 'qty': qty}


def test_ab_part_no_lot_required(flask_app):
    assert run(flask_app, [item(1, 'A12345', 5)], []) is None
    assert run(flask_app, [item(1, 'b12345', 5)], []) is None


def test_missing_lot_blocked(flask_app):
    msg = run(flask_app, [item(11, 'F010013', 5)], [])
    assert msg and '未维护批次' in msg and 'F010013' in msg and '11' in msg


def test_zero_qty_treated_as_missing(flask_app):
    msg = run(flask_app, [item(12, 'F010013', 5)], [lot(12, 0)])
    assert msg and '未维护批次' in msg


def test_over_qty_blocked(flask_app):
    msg = run(flask_app, [item(13, 'F010013', 5)], [lot(13, 6.5)])
    # 数量展示改为最多 4 位小数并去尾随零：6.50 -> 6.5、5.00 -> 5
    assert msg and '超过申请数量' in msg and '批次累计 6.5 ' in msg and '申请数量 5' in msg


def test_equal_and_tolerance_passes(flask_app):
    assert run(flask_app, [item(14, 'F010013', 5)], [lot(14, 5)]) is None
    assert run(flask_app, [item(15, 'F010013', 5)], [lot(15, 5.0001)]) is None
    assert run(flask_app, [item(16, 'F010013', 5)], [lot(16, 4), lot(16, 1)]) is None


def test_short_reason_exempt(flask_app):
    assert run(flask_app, [item(17, 'F010013', 5, short='库存不足')], []) is None


def test_english_message(flask_app):
    msg = run(flask_app, [item(18, 'F010013', 5)], [], lang='en')
    assert msg and 'without lot allocation' in msg and 'row18' in msg
