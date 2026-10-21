# -*- coding: utf-8 -*-
"""
线卷全库存管理功能测试（Wire Coil Inventory）

设计基准：docs/wire_coil_inventory_spec.md（第3节 API、第5节验收、第7节校验）
代码审查：docs/review_wire_coil.md（参考已知问题 P1/P2/P3）

覆盖范围：
  1. 卷号生成：格式 YYMMDD+3位、每日 >=999 上限、uk_coil_id 冲突重取
  2. A/B 开头物料强制校验
  3. 单位换算（mm → converted_length/converted_unit）
  4. 卷标录入：合法录入、字段缺失、非法长度
  5. 出库登记：整卷置 issued、消耗超卷长校验、累计校验、consume_type='issue'
  6. 标签打印：渲染内容、3"×1"、打印失败降级
  7. 接口权限：未登录 401、非 warehouse/admin 403

测试方式：
  - 纯函数单测（不依赖 DB）
  - Flask test client + conftest Mock 数据库（不触碰生产数据）
"""
import re
from datetime import datetime
from decimal import Decimal

import pytest

from app.routes.coil import (
    COIL_ID_RE,
    DAILY_LIMIT,
    MAX_BATCH,
    CONSUMPTION_EXTRA_FIELDS,
    _convert_length,
    _gen_next_id,
    _parse_extra_fields,
    _coil_to_dict,
)
from app.services.label_print_service import (
    LabelRenderer,
    build_zpl,
    build_tspl,
    _clean_fd,
    LABEL_WIDTH_MM,
    LABEL_HEIGHT_MM,
    build_bartender_file,
    write_bartender_file,
)
from app.services.csi_service import parse_job
from tests.conftest import MOCK_WIRE_COILS, MOCK_CONSUMPTIONS


# ================= 测试数据 helper =================

@pytest.fixture(autouse=True)
def _no_real_csi(monkeypatch):
    """隔离外部 CSI API：所有测试禁止真实网络请求。

    背景：部分测试路径（如卷标录入 POST /api/requests/<id>/coils、
    单位查询 GET /api/requests/<id>/coil-units）会经 coil._get_unit_cached
    构造真实 CSIClient 并调用 get_item_unit/_fetch_token，未 mock 时会对
    生产 CSI 发起 httpx 请求（超时 30~60s + 重试），导致测试套件长时间挂起。

    此处：
      1. 直接替换 coil._get_unit_cached —— 任何未显式 mock 的路径默认返回 'M'，
         不再创建 CSIClient、不写模块级 _unit_cache、不发网络请求；
      2. 同时替换 coil/csi_service 两个命名空间里的 CSIClient 为打桩类，
         即使有代码绕过 _get_unit_cached 直接 new CSIClient 也不会联网。

    单个测试如需定制返回，可再使用下方 mock_units fixture（monkeypatch 为
    LIFO 撤销，函数级 fixture 的替换优先，teardown 后自动恢复本兜底）。
    """
    from app.routes import coil as coil_mod
    from app.services import csi_service as csi_mod

    def _fake_get_unit_cached(siteref, part_number):
        return 'M'

    class _FakeCSIClient:
        """离线打桩：全部方法返回安全默认值，绝不访问网络"""
        def __init__(self, *args, **kwargs):
            pass

        def get_item_unit(self, item):
            return 'M'

        def get_inventory(self, item):
            return []

        def get_item_cost(self, item):
            return None

        def get_item_backflush(self, item):
            return None

        def get_item_lots(self, item, exclude_floor=True):
            return []

        def validate_job(self, job, suffix):
            return None

        def validate_material(self, job, suffix, item):
            return None

        def _fetch_token(self):
            return ('fake-token', 3600)

        def _ensure_token(self):
            return 'fake-token'

    monkeypatch.setattr(coil_mod, '_get_unit_cached', _fake_get_unit_cached)
    monkeypatch.setattr(coil_mod, 'CSIClient', _FakeCSIClient)
    monkeypatch.setattr(csi_mod, 'CSIClient', _FakeCSIClient)


def _make_coil(coil_id='260814001', part_number='A123456', length=250.50, unit='M',
               status='in_stock', request_id=100, siteref='310'):
    return {
        'id': 2000 + len(MOCK_WIRE_COILS),
        'coil_id': coil_id,
        'part_number': part_number,
        'coil_length': length,
        'unit': unit,
        'status': status,
        'request_id': request_id,
        'siteref': siteref,
        'operator': 'warehouse1',
        'remark': None,
        'created_at': datetime(2026, 8, 14, 9, 0, 0),
    }


def _seed_coils(*coils):
    for c in coils:
        MOCK_WIRE_COILS.append(c)


def _seed_daily_999(prefix='260814'):
    """预置当日 999 条卷号，用于验证每日上限"""
    for i in range(1, DAILY_LIMIT + 1):
        MOCK_WIRE_COILS.append(_make_coil(
            coil_id=f'{prefix}{i:03d}', part_number='A123456',
            length=10.0, unit='M', status='in_stock', request_id=100))


# ============================================================
# 一、卷号格式 / 卷号生成（纯函数）
# ============================================================

class TestCoilIdFormat:
    """R1 卷号格式：^\d{9}$（YYMMDD+3位）"""

    def test_valid_9_digits(self):
        assert COIL_ID_RE.match('260814001')
        assert COIL_ID_RE.match('000101999')

    def test_invalid_short(self):
        assert not COIL_ID_RE.match('123')          # 长度不足
        assert not COIL_ID_RE.match('26081400')     # 8位

    def test_invalid_non_digit(self):
        assert not COIL_ID_RE.match('2608140a1')
        assert not COIL_ID_RE.match('26a814001')

    def test_invalid_blank(self):
        assert not COIL_ID_RE.match('')
        assert not COIL_ID_RE.match('           ')


class TestCoilIdGeneration:
    """R2/R3 卷号生成：当日计数 + 999 上限"""

    def test_gen_first_of_day(self, mock_db):
        cursor = mock_db.cursor()
        assert _gen_next_id(cursor, datetime(2026, 8, 14)) == '260814001'

    def test_gen_follows_seq(self, mock_db):
        _seed_coils(_make_coil(coil_id='260814001', part_number='A123456'))
        _seed_coils(_make_coil(coil_id='260814002', part_number='B999999'))
        cursor = mock_db.cursor()
        assert _gen_next_id(cursor, datetime(2026, 8, 14)) == '260814003'

    def test_gen_pad_three_digits(self, mock_db):
        # 已有 998 条 → 下一号 999（3位补零边界）
        for i in range(1, 999):
            MOCK_WIRE_COILS.append(_make_coil(
                coil_id=f'260814{i:03d}', part_number='A123456'))
        cursor = mock_db.cursor()
        assert _gen_next_id(cursor, datetime(2026, 8, 14)) == '260814999'

    def test_gen_daily_limit_raises(self, mock_db):
        """R3：当日已用 999 卷 → 抛 ValueError"""
        _seed_daily_999()
        cursor = mock_db.cursor()
        with pytest.raises(ValueError, match='当日卷号已用完'):
            _gen_next_id(cursor, datetime(2026, 8, 14))

    def test_gen_prefix_depends_on_date(self, mock_db):
        cursor = mock_db.cursor()
        assert _gen_next_id(cursor, datetime(2026, 1, 1)) == '260101001'
        assert _gen_next_id(cursor, datetime(2024, 12, 31)) == '241231001'


# ============================================================
# 二、单位换算（R11 / 文档 2.3.1）
# ============================================================

class TestUnitConversion:
    """converted_length = out_length(mm) ÷ 系数；未收录单位置 (None, None)"""

    @pytest.mark.parametrize('unit,expected', [
        ('M', (0.2505, 'M')),      # 250.50 ÷ 1000
        ('FT', (0.8219, 'FT')),    # 250.50 ÷ 304.8 = 0.82185... → round 4 = 0.8219
        ('CM', (25.05, 'CM')),     # 250.50 ÷ 10
        ('IN', (9.8622, 'IN')),    # 250.50 ÷ 25.4 = 9.86220... → round 4
    ])
    def test_known_units(self, unit, expected):
        assert _convert_length(250.50, unit) == expected

    def test_case_insensitive(self):
        # 系数查表大小写不敏感；返回的 converted_unit 保持原样
        assert _convert_length(250.50, 'm') == (0.2505, 'm')
        assert _convert_length(250.50, 'ft') == (0.8219, 'ft')

    def test_unknown_unit_null(self):
        assert _convert_length(250.50, 'EA') == (None, None)
        assert _convert_length(250.50, 'KG') == (None, None)

    def test_empty_unit_null(self):
        assert _convert_length(250.50, None) == (None, None)
        assert _convert_length(250.50, '') == (None, None)
        assert _convert_length(250.50, '   ') == (None, None)

    def test_tiny_value_kept_with_4dp(self):
        """P3-2 边界：4 位小数后 0.1mm 换算为 0.0001M 不再被抹成 0（2 位时会被抹平）
        注：0.0001mm ÷ 1000 = 1e-7 M，低于 4 位小数的最小刻度，必然为 0.0"""
        result = _convert_length(0.1, 'M')
        assert result == (0.0001, 'M')
        assert result[0] is not None  # 单位已收录，不应置 None
        assert _convert_length(0.0001, 'M') == (0.0, 'M')  # 仍低于 4 位最小刻度


# ============================================================
# 三、宽表字段解析（R12）
# ============================================================

class TestExtraFieldParsing:
    """出库登记宽表字段：类型校验 / 枚举校验 / 空值转 None"""

    def test_all_text_and_empty_to_none(self):
        values, err = _parse_extra_fields({'job_part_number': 'PN-A001',
                                           'shear_equipment': ' SL-01 ', 'length_tolerance': ''}, 1)
        assert err is None
        assert values['job_part_number'] == 'PN-A001'
        assert values['shear_equipment'] == 'SL-01'   # strip
        assert values['length_tolerance'] is None      # 空串转 None

    def test_numeric_round(self):
        values, err = _parse_extra_fields({'shear_length': '12.345'}, 1)
        assert err is None
        assert values['shear_length'] == 12.345  # round 4

    def test_int_field(self):
        values, err = _parse_extra_fields({'shear_qty': '10'}, 1)
        assert err is None
        assert values['shear_qty'] == 10

    def test_bad_numeric(self):
        _, err = _parse_extra_fields({'shear_length': 'abc'}, 2)
        assert err is not None and '第2行 shear_length 必须为数值' in err

    def test_bad_int(self):
        _, err = _parse_extra_fields({'shear_qty': 'xyz'}, 3)
        assert err is not None and '第3行 shear_qty 必须为整数' in err

    def test_field_set_matches_db_spec(self):
        """宽表字段键集合与迁移脚本/Doc 2.3 一致（防止清单漂移）

        说明：converted_length/converted_unit 为服务端计算字段（不信任前端传值），
        不在前端可传字段清单 CONSUMPTION_EXTRA_FIELDS 中，但必须在迁移脚本中存在；
        actual_shear_length/shear_device_no/scrap_length_actual/stage/is_manual/checker
        为外部集成（naiwiptrack）专用字段，同样不在前端可传清单中。
        因此断言：migrate_cols 包含 code_cols 全部字段，且额外只多出服务端/外部集成字段。
        """
        from scripts.migrate_wire_coil import CONSUMPTION_WIDE_COLUMNS
        migrate_cols = {name for name, _ in CONSUMPTION_WIDE_COLUMNS}
        code_cols = set(CONSUMPTION_EXTRA_FIELDS.keys())
        # 允许出现在迁移脚本但不在前端可传清单：服务端计算字段 + 外部集成字段
        server_only = {'converted_length', 'converted_unit',
                       'actual_shear_length', 'shear_device_no',
                       'scrap_length_actual', 'stage', 'is_manual', 'checker'}
        # 1. 迁移脚本必须包含全部前端可传字段
        assert code_cols <= migrate_cols, f'迁移脚本缺少字段: {code_cols - migrate_cols}'
        # 2. 迁移脚本多余字段只能是服务端/外部集成字段
        extra = migrate_cols - code_cols
        assert extra <= server_only, f'迁移脚本存在未知多余字段: {extra}'
        # 3. 服务端计算字段必须在迁移脚本中（确保表结构含转换列）
        assert {'converted_length', 'converted_unit'} <= migrate_cols, f'迁移脚本缺少服务端计算字段'


# ============================================================
# 四、标签渲染（文档 5.7 版式：卷号/条码 + 物料/条码 + 长度/单位，3"×1"）
# ============================================================

class TestLabelRender:

    def test_render_fields(self):
        data = LabelRenderer.render({'coil_id': '260814001', 'part_number': 'A123456',
                                     'coil_length': 250.50, 'unit': 'M'})
        assert data['coil_id'] == '260814001'
        assert data['part_number'] == 'A123456'
        assert data['barcode_coil'] == '260814001'   # 条码内容 = 卷号
        assert data['barcode_part'] == 'A123456'     # 条码内容 = 物料
        assert data['length_text'] == '250.5 M'
        assert data['length'] == 250.5

    def test_render_no_unit(self):
        data = LabelRenderer.render({'coil_id': '260814001', 'part_number': 'W0303408',
                                     'coil_length': 100.0, 'unit': None})
        assert data['length_text'] == '100'

    def test_render_bad_length(self):
        data = LabelRenderer.render({'coil_id': '260814001', 'part_number': 'A1',
                                     'coil_length': 'abc', 'unit': 'M'})
        assert data['length'] == 0.0

    def test_label_dimensions(self):
        """3"×1" = 75mm × 25mm"""
        assert LABEL_WIDTH_MM == 75
        assert LABEL_HEIGHT_MM == 25

    def test_build_zpl_contains_key_elements(self):
        zpl = build_zpl({'barcode_coil': '260814001', 'barcode_part': 'A123456',
                         'length_text': '250.5 M'}).decode('utf-8')
        assert '^XA' in zpl and '^XZ' in zpl
        assert '260814001' in zpl
        assert 'A123456' in zpl
        assert '250.5 M' in zpl
        assert '^BCN,45' in zpl          # Code128 条码（紧凑高度）
        assert '^PW609^LL203' in zpl     # 203dpi 3"×1" 尺寸

    def test_build_tspl_contains_key_elements(self):
        tsp = build_tspl({'barcode_coil': '260814001', 'barcode_part': 'A123456',
                          'length_text': '250.5 M'}).decode('utf-8')
        assert 'SIZE 75 mm,25 mm' in tsp
        assert 'BARCODE 20,15,"128"' in tsp
        assert '260814001' in tsp and 'A123456' in tsp and '250.5 M' in tsp

    def test_clean_fd_strips_control_chars(self):
        assert _clean_fd('AB^C~D\\E') == 'ABCDE'
        assert _clean_fd('') == ''
        assert _clean_fd(None) == ''

    def test_build_zpl_cleans_injection_chars(self):
        """P3-7 关注：^/~ 等指令字符被清理，避免标签指令注入"""
        zpl = build_zpl({'barcode_coil': '260814001', 'barcode_part': 'A^XZ',
                         'length_text': '1 M'}).decode('utf-8')
        assert '^XZ' in zpl  # 结束标记仍在
        assert 'A^XZ' not in zpl  # 物料号中的 ^ 被清理


# ============================================================
# 五、工单解析 / 字典转换
# ============================================================

class TestJobParsing:

    def test_parse_valid_job(self):
        assert parse_job('J000002124-0004') == ('J000002124', 4)
        assert parse_job('ENG0002124-0001') == ('ENG0002124', 1)

    def test_parse_invalid_job(self):
        assert parse_job('xxx') == (None, None)
        assert parse_job('J000002124') == (None, None)
        assert parse_job('') == (None, None)


class TestCoilDict:

    def test_status_label(self):
        d = _coil_to_dict({'status': 'in_stock', 'coil_length': 250.50})
        assert d['status_label'] == '在库'
        d2 = _coil_to_dict({'status': 'issued', 'coil_length': 100.0})
        assert d2['status_label'] == '已出库'
        d3 = _coil_to_dict({'status': 'scrapped', 'coil_length': 10.0})
        assert d3['status_label'] == '报废'


# ============================================================
# 六、接口：卷号生成 GET /api/coils/next-id
# ============================================================

class TestNextIdAPI:

    def test_unauthenticated_401(self, client):
        resp = client.get('/api/coils/next-id')
        assert resp.status_code == 401

    def test_ok(self, client, login_as_warehouse):
        resp = client.get('/api/coils/next-id?date=2026-08-14')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['coil_id'] == '260814001'
        assert data['date_prefix'] == '260814'
        assert data['seq'] == 1
        assert data['daily_count'] == 0
        assert data['daily_limit'] == 999

    def test_invalid_date_400(self, client, login_as_warehouse):
        resp = client.get('/api/coils/next-id?date=2026-13-99')
        assert resp.status_code == 400
        assert '日期格式' in resp.get_json()['message']

    def test_daily_limit_400(self, client, login_as_warehouse):
        _seed_daily_999()
        resp = client.get('/api/coils/next-id?date=2026-08-14')
        assert resp.status_code == 400
        assert '当日卷号已用完' in resp.get_json()['message']

    def test_requester_forbidden_403(self, client, login_as_requester):
        """P2-5：卷号接口已限制 warehouse/admin 角色，requester 返回 403"""
        resp = client.get('/api/coils/next-id?date=2026-08-14')
        assert resp.status_code == 403


# ============================================================
# 七、接口：卷标录入 POST /api/requests/<id>/coils
# ============================================================

@pytest.fixture
def mock_units(monkeypatch):
    """mock CSI 单位查询（避免真实网络）"""
    def _patch(unit_map):
        from app.routes import coil as coil_mod
        monkeypatch.setattr(coil_mod, '_get_unit_cached',
                            lambda siteref, part: unit_map.get(part))
    return _patch


class TestCreateCoilsAPI:
    """卷标录入：R1/R4/R5/R6/R7 + minpack/prepping/权限"""

    A_ITEM = {'part_number': 'A123456', 'coil_id': '260814001', 'length': 250.50, 'unit': 'M'}
    B_ITEM = {'part_number': 'B999999', 'coil_id': '260814002', 'length': 100.00, 'unit': 'M'}
    W_ITEM = {'part_number': 'W0303408', 'coil_id': '260814003', 'length': 80.00, 'unit': 'M'}

    def test_unauthenticated_401(self, client):
        resp = client.post('/api/requests/100/coils', json={'items': [self.A_ITEM]})
        assert resp.status_code == 401

    def test_requester_forbidden_403(self, client, login_as_requester):
        resp = client.post('/api/requests/100/coils', json={'items': [self.A_ITEM]})
        assert resp.status_code == 403

    def test_request_not_minpack_400(self, client, login_as_warehouse):
        """R10：非 minpack 申请单拒绝（request 5 无 request_type）"""
        resp = client.post('/api/requests/5/coils', json={'items': [self.A_ITEM]})
        assert resp.status_code == 400
        assert '仅最小包装' in resp.get_json()['message']

    def test_request_status_400(self, client, login_as_warehouse):
        """R10：minpack 但非 prepping 拒绝（request 101 为 ready_pickup）"""
        resp = client.post('/api/requests/101/coils', json={'items': [self.A_ITEM]})
        assert resp.status_code == 400
        assert '仅备料中' in resp.get_json()['message']

    def test_request_not_found_404(self, client, login_as_warehouse):
        resp = client.post('/api/requests/999999/coils', json={'items': [self.A_ITEM]})
        assert resp.status_code == 404

    def test_empty_items_400(self, client, login_as_warehouse):
        resp = client.post('/api/requests/100/coils', json={'items': []})
        assert resp.status_code == 400
        assert '至少录入一行' in resp.get_json()['message']

    def test_ab_part_missing_400(self, client, login_as_warehouse, mock_units):
        """R4：A/B 开头物料未录入 → 400"""
        mock_units({'W0303408': 'M'})
        resp = client.post('/api/requests/100/coils', json={'items': [self.W_ITEM]})
        assert resp.status_code == 400
        msg = resp.get_json()['message']
        # 提示可能命中任一 A/B 开头物料（set 顺序不固定）
        assert '必须录入卷标信息' in msg
        assert ('A123456' in msg) or ('B999999' in msg)

    def test_b_part_missing_400(self, client, login_as_warehouse, mock_units):
        """R4：B 开头物料同样强制（request 100 含 B999999）"""
        mock_units({'A123456': 'M', 'W0303408': 'M'})
        resp = client.post('/api/requests/100/coils', json={'items': [self.A_ITEM, self.W_ITEM]})
        assert resp.status_code == 400
        assert 'B999999' in resp.get_json()['message']

    def test_ok_insert_three(self, client, login_as_warehouse, mock_units):
        """合法录入：A/B/普通 全录入 → 200"""
        mock_units({'A123456': 'M', 'B999999': 'M', 'W0303408': 'M'})
        resp = client.post('/api/requests/100/coils', json={
            'items': [self.A_ITEM, self.B_ITEM, self.W_ITEM]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['inserted'] == 3
        assert len(MOCK_WIRE_COILS) == 3
        for row in data['data']:
            assert row['status'] == 'in_stock'
        assert MOCK_WIRE_COILS[0]['request_id'] == 100
        assert MOCK_WIRE_COILS[0]['operator'] == 'warehouse1'

    def test_unit_overridden_by_backend(self, client, login_as_warehouse, mock_units):
        """R7：单位由后端 CSI 覆盖，不信任前端传值（前端传 EA → 后端回填 M）"""
        mock_units({'A123456': 'M', 'B999999': 'M', 'W0303408': 'M'})
        item_a = dict(self.A_ITEM, unit='EA')   # 前端篡改为 EA
        resp = client.post('/api/requests/100/coils', json={
            'items': [item_a, self.B_ITEM, self.W_ITEM]})
        assert resp.status_code == 200
        # 找到 A123456 对应的卷记录，单位应被后端覆盖为 M
        a_rows = [w for w in MOCK_WIRE_COILS if w['part_number'] == 'A123456']
        assert a_rows and a_rows[0]['unit'] == 'M'

    def test_unit_fail_warning_not_block(self, client, login_as_warehouse, mock_units):
        """R7 降级：CSI 查不到单位 → 留空 + 警告，不阻断录入"""
        mock_units({'A123456': None, 'B999999': None, 'W0303408': None})
        resp = client.post('/api/requests/100/coils', json={
            'items': [self.A_ITEM, self.B_ITEM, self.W_ITEM]})
        assert resp.status_code == 200
        assert MOCK_WIRE_COILS[0]['unit'] is None
        assert resp.get_json()['warnings']

    def test_invalid_coil_format_400(self, client, login_as_warehouse, mock_units):
        """R1：卷号格式非法"""
        mock_units({'A123456': 'M'})
        item = dict(self.A_ITEM, coil_id='123')
        resp = client.post('/api/requests/100/coils', json={'items': [item]})
        assert resp.status_code == 400
        assert '格式无效' in resp.get_json()['message']

    def test_zero_length_400(self, client, login_as_warehouse, mock_units):
        """R5：长度必须 > 0"""
        mock_units({'A123456': 'M'})
        resp = client.post('/api/requests/100/coils',
                           json={'items': [dict(self.A_ITEM, length=0)]})
        assert resp.status_code == 400
        assert '长度必须大于0' in resp.get_json()['message']

    def test_non_numeric_length_400(self, client, login_as_warehouse, mock_units):
        mock_units({'A123456': 'M'})
        resp = client.post('/api/requests/100/coils',
                           json={'items': [dict(self.A_ITEM, length='abc')]})
        assert resp.status_code == 400
        assert '长度必须大于0' in resp.get_json()['message']

    def test_part_not_in_request_400(self, client, login_as_warehouse, mock_units):
        """R6：物料不属于该申请单明细"""
        mock_units({'XXXXXX': 'M'})
        resp = client.post('/api/requests/100/coils',
                           json={'items': [dict(self.A_ITEM, part_number='XXXXXX')]})
        assert resp.status_code == 400
        assert '不属于该申请单明细' in resp.get_json()['message']

    def test_too_many_rows_400(self, client, login_as_warehouse, mock_units):
        mock_units({'A123456': 'M'})
        items = [dict(self.A_ITEM, coil_id=f'260814{i:03d}') for i in range(1, MAX_BATCH + 2)]
        resp = client.post('/api/requests/100/coils', json={'items': items})
        assert resp.status_code == 400
        assert f'最多录入 {MAX_BATCH} 行' in resp.get_json()['message']

    def test_duplicate_retry_new_id(self, client, login_as_warehouse, mock_units):
        """R2 并发处理：uk_coil_id 冲突后自动重取新号（文档 3.3.1 重试逻辑）"""
        mock_units({'A123456': 'M', 'B999999': 'M', 'W0303408': 'M'})
        _seed_coils(_make_coil(coil_id='260814001', part_number='A123456', request_id=99))
        resp = client.post('/api/requests/100/coils', json={
            'items': [self.A_ITEM, self.B_ITEM, self.W_ITEM]})
        assert resp.status_code == 200
        # 找到 A123456 对应记录，冲突后应重取新号 260814002
        a_rows = [w for w in MOCK_WIRE_COILS if w['part_number'] == 'A123456' and w['request_id'] == 100]
        assert a_rows and a_rows[0]['coil_id'] == '260814002'  # 重取新号

    def test_duplicate_all_conflict_400(self, client, login_as_warehouse, mock_units):
        """R2 兜底：当日已满且冲突 → 400 当日卷号已用完"""
        mock_units({'A123456': 'M', 'B999999': 'M', 'W0303408': 'M'})
        _seed_daily_999()  # 当日已有 999 条（含 260814001）
        resp = client.post('/api/requests/100/coils', json={
            'items': [self.A_ITEM, self.B_ITEM, self.W_ITEM]})
        assert resp.status_code == 400
        assert '当日卷号已用完' in resp.get_json()['message']


# ============================================================
# 八、接口：查询（申请单卷标 / 库存列表 / 单位）
# ============================================================

class TestQueryAPI:

    def test_list_request_coils_ok(self, client, login_as_warehouse):
        _seed_coils(_make_coil(coil_id='260814001', part_number='A123456'),
                    _make_coil(coil_id='260814002', part_number='B999999'))
        resp = client.get('/api/requests/100/coils')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert len(data) == 2
        assert data[0]['status_label'] == '在库'

    def test_list_request_coils_unauthenticated_401(self, client):
        assert client.get('/api/requests/100/coils').status_code == 401

    def test_list_request_coils_not_found_404(self, client, login_as_warehouse):
        assert client.get('/api/requests/999999/coils').status_code == 404

    def test_list_coils_ok_paged(self, client, login_as_warehouse):
        _seed_coils(_make_coil(coil_id='260814001'), _make_coil(coil_id='260814002'))
        resp = client.get('/api/coils?page=1&size=20')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['total'] == 2
        assert body['page'] == 1
        assert len(body['data']) == 2

    def test_list_coils_bad_page_400(self, client, login_as_warehouse):
        resp = client.get('/api/coils?page=abc')
        assert resp.status_code == 400
        assert '分页参数无效' in resp.get_json()['message']

    def test_list_coils_bad_request_id_500(self, client, login_as_warehouse):
        """P2-4 已修复：request_id=abc 应返回 400 而非 500"""
        resp = client.get('/api/coils?request_id=abc')
        assert resp.status_code == 400
        assert 'request_id 必须为整数' in resp.get_json()['message']

    def test_list_coils_site_filter(self, client, login_as_warehouse):
        """非 admin 自动加 siteref 过滤"""
        MOCK_WIRE_COILS.clear()  # 清理共享状态，避免测试间污染
        _seed_coils(_make_coil(coil_id='260814001', siteref='310'),
                    _make_coil(coil_id='260814002', siteref='410'))
        resp = client.get('/api/coils')
        body = resp.get_json()
        assert body['total'] == 1
        assert body['data'][0]['siteref'] == '310'

    def test_coil_units_ok(self, client, login_as_warehouse, mock_units):
        mock_units({'A123456': 'M', 'B999999': 'M', 'W0303408': 'FT'})
        resp = client.get('/api/requests/100/coil-units')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['A123456'] == 'M'
        assert data['W0303408'] == 'FT'

    def test_coil_units_empty_for_unknown(self, client, login_as_warehouse, mock_units):
        """CSI 查不到 → 空字符串，前端显示 '-'"""
        mock_units({'A123456': None})
        resp = client.get('/api/requests/100/coil-units')
        assert resp.get_json()['data']['A123456'] == ''

    def test_coil_units_unauthenticated_401(self, client):
        assert client.get('/api/requests/100/coil-units').status_code == 401


# ============================================================
# 九、接口：标签预览 / 打印
# ============================================================

class TestLabelAPI:

    def test_preview_ok(self, client, login_as_warehouse):
        _seed_coils(_make_coil(coil_id='260814001'))
        resp = client.get('/api/coils/260814001/label')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['coil_id'] == '260814001'
        assert data['barcode_coil'] == '260814001'
        assert data['barcode_part'] == 'A123456'
        assert data['length'] == 250.5

    def test_preview_not_found_404(self, client, login_as_warehouse):
        assert client.get('/api/coils/999999999/label').status_code == 404

    def test_preview_unauthenticated_401(self, client):
        assert client.get('/api/coils/260814001/label').status_code == 401

    def test_preview_site_filter_403(self, client, login_as_warehouse):
        """410 站点 warehouse 访问 310 卷 → 403（站点隔离）"""
        _seed_coils(_make_coil(coil_id='260814001', siteref='410'))
        resp = client.get('/api/coils/260814001/label')
        assert resp.status_code == 403


@pytest.fixture
def mock_print(monkeypatch):
    """mock Bartender 触发文件打印服务（避免真实写入共享目录/驱动打印）"""
    def _patch(result):
        from app.services import label_print_service as lps
        monkeypatch.setattr(lps, 'write_bartender_file',
                            lambda coils, printer_name=None, file_dir=None: result)
    return _patch


class TestPrintAPI:

    def test_unauthenticated_401(self, client):
        resp = client.post('/api/coils/print', json={'coil_ids': ['260814001']})
        assert resp.status_code == 401

    def test_requester_forbidden_403(self, client, login_as_requester):
        resp = client.post('/api/coils/print', json={'coil_ids': ['260814001']})
        assert resp.status_code == 403

    def test_empty_coil_ids_400(self, client, login_as_warehouse):
        resp = client.post('/api/coils/print', json={'coil_ids': []})
        assert resp.status_code == 400
        assert '请选择需要打印的卷标' in resp.get_json()['message']

    def test_print_ok(self, client, login_as_warehouse, mock_print):
        _seed_coils(_make_coil(coil_id='260814001'))
        mock_print({'success': True, 'message': '已生成 Bartender 打印文件（1 卷）',
                    'path': r'\\172.26.1.7\Coil_Label_Scanned\260814001_20260903000000.dd',
                    'count': 1, 'errors': []})
        resp = client.post('/api/coils/print', json={'coil_ids': ['260814001']})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['printed'] == 1
        assert body['file_path'].endswith('.dd')

    def test_print_failure_degraded(self, client, login_as_warehouse, mock_print):
        """B4 打印容错：写入共享目录失败 → 可读错误信息，主流程不中断（仍返回 200）"""
        _seed_coils(_make_coil(coil_id='260814001'))
        mock_print({'success': False, 'message': '无权限写入打印共享目录',
                    'path': '', 'count': 0,
                    'errors': ['260814001: 打印机离线']})
        resp = client.post('/api/coils/print', json={'coil_ids': ['260814001']})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is False
        assert body['errors'] == ['260814001: 打印机离线']

    def test_print_no_coils_404(self, client, login_as_warehouse):
        resp = client.post('/api/coils/print', json={'coil_ids': ['999999999']})
        assert resp.status_code == 404


class TestBartenderTriggerFile:
    """Bartender .dd 触发文件：内容格式（用户逐字确认）与写入行为"""

    # 真实站点打印机配置示例（kr_site_printer 410）
    PRINTER = r'\\172.26.1.7\png-zt231-08'

    def _coil(self, **overrides):
        coil = {
            'coil_id': '260902001',
            'part_number': 'A080507',
            'coil_length': Decimal('200.00'),
            'unit': 'FT',
            'is_initial_half': 0,
            'lot_no': '202607070000035',
        }
        coil.update(overrides)
        return coil

    def test_build_single_content_exact(self):
        """单卷：逐字比对整个文件内容（第1~3行固定、标题行固定、数据行 字段名|值 成对行尾 |、CRLF）"""
        content = build_bartender_file([self._coil(coil_id='260828001')], self.PRINTER)
        assert content == (
            '%BTW%\r\n'
            '/AF="F:\\Labels\\Coil_Label.btw" /PRN="\\\\172.26.1.7\\png-zt231-08" '
            '/P /D="%Trigger File Name%" /C=1 /R=3\r\n'
            '%END%\r\n'
            'CoilId|CoilIdString|Part|PartString|Length|LengthString|Unit|UnitString|'
            'IsInitial|IsInitialString|Lot|LotString|\r\n'
            'CoilId|260828001|Part|A080507|Length|200|Unit|FT|IsInitial|0|Lot|202607070000035|\r\n'
        )

    def test_build_multiple_rows_length_format_and_defaults(self):
        """多卷单文件多行：长度去尾零、期初半卷=1、lot_no 为空保留空值段、is_initial 字符串形态"""
        coils = [
            self._coil(coil_id='260902002', coil_length='50.00',
                       is_initial_half='1', lot_no=None),
            self._coil(coil_id='260902001', coil_length=Decimal('85.250'),
                       is_initial_half='0', lot_no=''),
        ]
        lines = build_bartender_file(coils, 'printer_x').split('\r\n')
        assert lines[0] == '%BTW%'
        assert lines[2] == '%END%'
        assert lines[4] == 'CoilId|260902002|Part|A080507|Length|50|Unit|FT|IsInitial|1|Lot||'
        assert lines[5] == 'CoilId|260902001|Part|A080507|Length|85.25|Unit|FT|IsInitial|0|Lot||'
        assert lines[6] == ''  # 末行 CRLF 产生的空串

    def test_build_integer_length_keeps_zeros(self):
        """整数长度（如 DB 返回 Decimal('50.00') 之外 int 200）不能被 rstrip 误删尾零"""
        lines = build_bartender_file([self._coil(coil_length=200)], 'p').split('\r\n')
        assert 'Length|200|' in lines[4]

    def test_build_validation_errors(self):
        with pytest.raises(ValueError):
            build_bartender_file([], self.PRINTER)
        with pytest.raises(ValueError):
            build_bartender_file([self._coil()], '')
        with pytest.raises(ValueError):
            build_bartender_file([self._coil(coil_id='')], self.PRINTER)

    def test_write_to_dir(self, tmp_path):
        result = write_bartender_file([self._coil(coil_id='260902001')], self.PRINTER,
                                      file_dir=str(tmp_path))
        assert result['success'] is True
        assert result['count'] == 1
        assert result['errors'] == []
        import os
        assert os.path.isfile(result['path'])
        name = os.path.basename(result['path'])
        assert re.match(r'^260902001_\d{14}\.dd$', name)
        # 文件内容与 build 输出一致，UTF-8 无 BOM
        raw = open(result['path'], 'rb').read()
        assert not raw.startswith(b'\xef\xbb\xbf')
        assert raw.decode('utf-8') == build_bartender_file(
            [self._coil(coil_id='260902001')], self.PRINTER)

    def test_write_dir_not_exist(self):
        result = write_bartender_file([self._coil()], self.PRINTER,
                                      file_dir='Z:\\\\no_such_dir_btw')
        assert result['success'] is False
        assert '共享目录' in result['message'] or '不可访问' in result['message']

    def test_write_no_printer(self, tmp_path):
        result = write_bartender_file([self._coil()], '', file_dir=str(tmp_path))
        assert result['success'] is False
        assert '打印机' in result['message']


# ============================================================
# 十、接口：出库消耗登记 POST /api/requests/<id>/consumption
# ============================================================

class TestConsumptionAPI:
    """R8/R9/R11/R12 + 整卷置 issued + consume_type='issue'"""

    # 卷长 250.50 M → 250500 mm
    def _seed_coil(self, unit='M', length=250.50, coil_id='260814001',
                   status='in_stock', request_id=100):
        _seed_coils(_make_coil(coil_id=coil_id, part_number='A123456',
                               length=length, unit=unit, status=status,
                               request_id=request_id))

    def _item(self, coil_id='260814001', out_length=250500, **extra):
        item = {'coil_id': coil_id, 'job_order': 'J000002124-0004',
                'out_length': out_length}
        item.update(extra)
        return item

    def test_unauthenticated_401(self, client):
        assert client.post('/api/requests/100/consumption',
                           json={'items': [self._item()]}).status_code == 401

    def test_requester_forbidden_403(self, client, login_as_requester):
        resp = client.post('/api/requests/100/consumption', json={'items': [self._item()]})
        assert resp.status_code == 403

    def test_empty_items_400(self, client, login_as_warehouse):
        resp = client.post('/api/requests/100/consumption', json={'items': []})
        assert resp.status_code == 400
        assert '至少登记一条' in resp.get_json()['message']

    def test_status_not_prepping_400(self, client, login_as_warehouse):
        """R10：非 prepping 拒绝出库登记"""
        resp = client.post('/api/requests/101/consumption', json={'items': [self._item()]})
        assert resp.status_code == 400
        assert '仅备料中' in resp.get_json()['message']

    def test_full_issue_marks_issued(self, client, login_as_warehouse):
        """A10：整卷出完 → 卷状态置 issued，消耗记录 consume_type='issue'"""
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption', json={'items': [self._item()]})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['inserted'] == 1
        assert body['issued'] == ['260814001']
        assert MOCK_WIRE_COILS[0]['status'] == 'issued'
        rec = MOCK_CONSUMPTIONS[0]
        assert rec['consume_type'] == 'issue'
        assert rec['out_length'] == 250500
        # A14：converted_length = 250500 ÷ 1000 = 250.5，converted_unit = M
        assert rec['converted_length'] == 250.5
        assert rec['converted_unit'] == 'M'
        assert rec['part_number'] == 'A123456'
        assert rec['job_order'] == 'J000002124-0004'

    def test_over_length_400(self, client, login_as_warehouse):
        """R8：单次出库超卷长 → 400"""
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(out_length=250501)]})
        assert resp.status_code == 400
        assert '超过剩余长度' in resp.get_json()['message']
        assert len(MOCK_CONSUMPTIONS) == 0

    def test_partial_issue_stays_in_stock(self, client, login_as_warehouse):
        """部分出库：状态保持 in_stock"""
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(out_length=100000)]})
        assert resp.status_code == 200
        assert resp.get_json()['issued'] == []
        assert MOCK_WIRE_COILS[0]['status'] == 'in_stock'

    def test_cumulative_exceed_400(self, client, login_as_warehouse):
        """R8 累计：多次部分出库，累计 > 卷长 → 400"""
        self._seed_coil()
        resp1 = client.post('/api/requests/100/consumption',
                            json={'items': [self._item(out_length=200000)]})
        assert resp1.status_code == 200
        resp2 = client.post('/api/requests/100/consumption',
                            json={'items': [self._item(out_length=100000)]})
        assert resp2.status_code == 400
        assert '超过剩余长度' in resp2.get_json()['message']
        assert len(MOCK_CONSUMPTIONS) == 1

    def test_cumulative_ok_then_issued(self, client, login_as_warehouse):
        """多次部分出库合计到整卷 → 状态置 issued"""
        self._seed_coil()
        client.post('/api/requests/100/consumption',
                    json={'items': [self._item(out_length=200000)]})
        resp2 = client.post('/api/requests/100/consumption',
                            json={'items': [self._item(out_length=50500)]})
        assert resp2.status_code == 200
        assert resp2.get_json()['issued'] == ['260814001']
        assert MOCK_WIRE_COILS[0]['status'] == 'issued'

    def test_coil_not_in_request_400(self, client, login_as_warehouse):
        """R9：卷标不属于本申请单"""
        self._seed_coil(request_id=5)   # 该卷属于申请单 5
        resp = client.post('/api/requests/100/consumption', json={'items': [self._item()]})
        assert resp.status_code == 400
        assert '不属于该申请单' in resp.get_json()['message']

    def test_issued_coil_rejected_400(self, client, login_as_warehouse):
        """R9：已出库（issued）卷不能再次出库"""
        self._seed_coil(status='issued')
        resp = client.post('/api/requests/100/consumption', json={'items': [self._item()]})
        assert resp.status_code == 400
        assert '不能出库' in resp.get_json()['message']

    def test_zero_out_length_400(self, client, login_as_warehouse):
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(out_length=0)]})
        assert resp.status_code == 400
        assert '出库长度必须大于0' in resp.get_json()['message']

    def test_bad_numeric_400(self, client, login_as_warehouse):
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(shear_length='abc')]})
        assert resp.status_code == 400
        assert 'shear_length 必须为数值' in resp.get_json()['message']

    def test_bad_job_format_400(self, client, login_as_warehouse):
        """job_order 格式校验"""
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(job_order='xxx')]})
        assert resp.status_code == 400
        assert '工单号格式无效' in resp.get_json()['message']

    def test_unknown_unit_degrades(self, client, login_as_warehouse):
        """R11 降级：单位未收录 → 转换置空 + 警告，跳过长度校验不阻断"""
        self._seed_coil(unit='EA')
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(out_length=250500)]})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['warnings']
        assert MOCK_CONSUMPTIONS[0]['converted_length'] is None
        assert MOCK_CONSUMPTIONS[0]['converted_unit'] is None
        assert MOCK_WIRE_COILS[0]['status'] == 'in_stock'  # 无法换算，保守不置 issued

    def test_wide_fields_saved(self, client, login_as_warehouse):
        """A15：宽表字段保存逐字段一致"""
        self._seed_coil()
        item = self._item(
            job_part_number='PN-A001',
            shear_qty=100, shear_length=250.50, length_tolerance=1.00,
            shear_equipment='SL-01', actual_shear_equipment='SL-01')
        resp = client.post('/api/requests/100/consumption', json={'items': [item]})
        assert resp.status_code == 200
        rec = MOCK_CONSUMPTIONS[0]
        assert rec['job_part_number'] == 'PN-A001'
        assert rec['shear_qty'] == 100
        assert rec['shear_length'] == 250.50
        assert rec['length_tolerance'] == 1.00
        assert rec['shear_equipment'] == 'SL-01'
        assert rec['actual_shear_equipment'] == 'SL-01'
        # 未填写字段存 NULL
        for f in CONSUMPTION_EXTRA_FIELDS:
            if item.get(f) in (None, ''):
                assert rec[f] is None

    def test_p3_2_tiny_out_length_warns(self, client, login_as_warehouse):
        """P3-2 实证：极小出库长度 round 成 0.0 → 被误判为未收录系数，产生多余警告"""
        self._seed_coil()
        resp = client.post('/api/requests/100/consumption',
                           json={'items': [self._item(out_length=0.0001)]})
        assert resp.status_code == 200
        assert resp.get_json()['warnings']  # 本不应警告（M 已收录）


class TestListConsumptionAPI:

    def test_list_ok(self, client, login_as_warehouse):
        _seed_coils(_make_coil(coil_id='260814001'))
        MOCK_CONSUMPTIONS.append({
            'id': 1, 'coil_id': '260814001', 'job_order': 'J000002124-0004',
            'part_number': 'A123456', 'consume_type': 'issue', 'out_length': 250500,
            'unit': 'M', 'converted_length': 250.5, 'converted_unit': 'M',
            'operator': 'warehouse1', 'created_at': datetime(2026, 8, 14, 10, 0, 0),
        })
        resp = client.get('/api/requests/100/consumption')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert len(data) == 1
        assert data[0]['coil_id'] == '260814001'
        assert data[0]['converted_length'] == 250.5
        assert data[0]['consume_type'] == 'issue'

    def test_list_unauthenticated_401(self, client):
        assert client.get('/api/requests/100/consumption').status_code == 401
