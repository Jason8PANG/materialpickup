# -*- coding: utf-8 -*-
"""外部工单发料 API（POST /api/external/job-issue）测试

覆盖点：
  1. 正常提交 → 201，接口行字段映射正确
     （api_name/siteRef/job/warehouse/to_location/documentnumber/um/createby）
  2. documentNumber 为空 → 取 job[:12]（10 位不截断 / 14 位截断到 12）
  3. documentNumber 13 位 → 400「超过 CSI 长度 12」
  4. 鉴权与站点：缺 X-API-Key → 401；X-Site-Ref=999 → 400
  5. quantity 非法（0 / -1 / 非数字）→ 400
  6. 重复提交（同 站点+单号+工单+物料+批号）→ 200 duplicated=true，且不新增行
  7. lot 可选（非 lot 跟踪物料）：不传/空串 → 201，落库 NULL；空 lot 重复提交同样 duplicated

写入路径用假连接注入（不触达真实库、不发任何 CSI 请求）；
真实库校验用例在数据库不可达时自动 skip。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402

from app.config import Config  # noqa: E402
from app.routes import external as ext  # noqa: E402

_REAL_CONNECT = pymysql.connect
API_KEY = ext.EXTERNAL_KEY
URL = '/api/external/job-issue'

BODY = {
    'job': 'J000029867',
    'item': 'B052552',
    'lot': '202505090000036',
    'quantity': 68,
    'unit': 'EA',
    'submitter': 'wang.wu',
    'documentNumber': 'JOB00001',
}


# ====================================================================== #
#  假数据库（记录 SQL/参数，可模拟已存在行）
# ====================================================================== #
class FakeCursor:
    def __init__(self, existing=None):
        self.existing = existing
        self.executed = []
        self.lastrowid = 9001
        self._one = None
        self.committed = False

    def execute(self, sql, params=None):
        self.executed.append((' '.join(sql.split()), params))
        self._one = self.existing if 'SELECT id FROM kr_csi_interface' in sql else None
        return 1

    def fetchone(self):
        return self._one

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self.cur = cursor
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True
        self.cur.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    cursor = FakeCursor()

    def _use(existing=None):
        c = FakeCursor(existing)
        monkeypatch.setattr(ext, 'get_db_connection', lambda: FakeConn(c))
        return c

    return _use


def _headers(site='310', key=API_KEY):
    h = {'Content-Type': 'application/json'}
    if key is not None:
        h['X-API-Key'] = key
    if site is not None:
        h['X-Site-Ref'] = site
    return h


def _post(client, body=None, site='310', key=API_KEY):
    return client.post(URL, json=BODY if body is None else body, headers=_headers(site, key))


def _insert(cur):
    rows = [(s, p) for s, p in cur.executed if s.upper().startswith('INSERT')]
    assert len(rows) == 1, f'期望 1 条 INSERT，实际 {len(rows)}'
    return rows[0][1]


# ====================================================================== #
#  1/2. 正常提交与 documentNumber 规则
# ====================================================================== #
class TestCreate:
    def test_create_success_maps_all_columns(self, client, fake_db):
        cur = fake_db()
        resp = _post(client)

        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body['success'] is True
        assert body['id'] == 9001
        assert body['documentnumber'] == 'JOB00001'
        assert body['message'] == '已入队'

        p = _insert(cur)
        assert len(p) == 13
        assert p[0] == 'sl-job-issue'          # api_name
        assert p[1] == 310                     # siteRef
        assert p[2] == 'B052552'               # item
        assert p[3] == '202505090000036'       # lot
        assert p[4] == 68                      # quantity
        assert p[5] == 'EA'                    # um = body.unit
        assert p[6] == 'J000029867'            # job（含后缀时原样）
        assert p[7] == 'C1M1'                  # warehouse（310 映射）
        assert p[8] == Config.CSI_TO_LOCATION  # to_location = Floor-Core
        assert p[9] == 'JOB00001'              # documentnumber
        assert p[10].startswith('JIJ000029867-B052552-')
        assert len(p[10]) <= 128               # traceid 上限
        assert p[12] == 'wang.wu'              # createby = submitter
        assert cur.committed is True

        insert_sql = [s for s, _ in cur.executed if s.upper().startswith('INSERT')][0]
        assert "from_location, to_location" in insert_sql.replace('  ', ' ')
        assert 'NULL, %s' in insert_sql        # from_location 为 NULL
        assert "'F'" in insert_sql and "'PENDING'" in insert_sql

    def test_site_410_uses_m1m1(self, client, fake_db):
        cur = fake_db()
        resp = _post(client, site='410')

        assert resp.status_code == 201
        assert _insert(cur)[7] == 'M1M1'

    def test_document_number_defaults_to_job_when_empty(self, client, fake_db):
        cur = fake_db()
        body = dict(BODY, job='J123456789', documentNumber='')
        resp = _post(client, body)

        assert resp.status_code == 201
        assert resp.get_json()['documentnumber'] == 'J123456789'
        assert _insert(cur)[9] == 'J123456789'

    def test_document_number_defaults_to_job_when_missing(self, client, fake_db):
        cur = fake_db()
        body = dict(BODY)
        body.pop('documentNumber')
        resp = _post(client, body)

        assert resp.status_code == 201
        assert resp.get_json()['documentnumber'] == 'J000029867'

    def test_document_number_truncates_long_job_to_12(self, client, fake_db):
        cur = fake_db()
        job = 'J0000298670001'                 # 14 位
        body = dict(BODY, job=job, documentNumber='')
        resp = _post(client, body)

        assert resp.status_code == 201
        assert resp.get_json()['documentnumber'] == job[:12]
        assert _insert(cur)[9] == job[:12]

    def test_document_number_13_chars_rejected(self, client, fake_db):
        fake_db()
        resp = _post(client, dict(BODY, documentNumber='1234567890123'))

        assert resp.status_code == 400
        msg = resp.get_json()['message']
        assert 'documentNumber 超过 CSI 长度 12' in msg


# ====================================================================== #
#  4/5. 鉴权、站点、quantity 校验
# ====================================================================== #
class TestValidation:
    def test_missing_api_key(self, client, fake_db):
        fake_db()
        resp = _post(client, key=None)
        assert resp.status_code == 401
        assert resp.get_json() == {'success': False, 'message': 'API Key 无效'}

    def test_wrong_api_key(self, client, fake_db):
        fake_db()
        assert _post(client, key='wrong-key').status_code == 401

    def test_invalid_site(self, client, fake_db):
        fake_db()
        resp = _post(client, site='999')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert 'X-Site-Ref' in resp.get_json()['message']

    def test_missing_site(self, client, fake_db):
        fake_db()
        assert _post(client, site=None).status_code == 400

    @pytest.mark.parametrize('qty', [0, -1, 'abc', None, ''])
    def test_invalid_quantity(self, client, fake_db, qty):
        fake_db()
        resp = _post(client, dict(BODY, quantity=qty))
        assert resp.status_code == 400
        assert 'quantity' in resp.get_json()['message']

    @pytest.mark.parametrize('field', ['job', 'item', 'unit', 'submitter'])
    def test_missing_required_field(self, client, fake_db, field):
        fake_db()
        body = dict(BODY)
        body.pop(field)
        resp = _post(client, body)
        assert resp.status_code == 400
        assert field in resp.get_json()['message']

    # -- lot 可选（非 lot 跟踪物料） ----------------------------------------
    @pytest.mark.parametrize('lot', [None, '', '   '])
    def test_lot_optional_stores_null(self, client, fake_db, lot):
        """不传 / 空串 / 全空白 lot → 201，落库 lot 为 NULL（非空串）"""
        cur = fake_db()
        body = dict(BODY)
        if lot is None:
            body.pop('lot')
        else:
            body['lot'] = lot
        resp = _post(client, body)

        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()['message'] == '已入队'
        assert _insert(cur)[3] is None

    def test_lot_absent_message_never_mentions_lot(self, client, fake_db):
        """缺 job 时 message 只报 job，不含 lot"""
        fake_db()
        body = dict(BODY)
        body.pop('job')
        body.pop('lot')
        resp = _post(client, body)

        assert resp.status_code == 400
        msg = resp.get_json()['message']
        assert 'job' in msg and 'lot' not in msg


# ====================================================================== #
#  6. 幂等
# ====================================================================== #
class TestIdempotency:
    def test_duplicate_returns_200_and_skips_insert(self, client, fake_db):
        cur = fake_db(existing={'id': 777})
        resp = _post(client)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {
            'success': True, 'duplicated': True, 'id': 777,
            'message': '已存在相同发料记录，未重复入队',
        }
        assert not [s for s, _ in cur.executed if s.upper().startswith('INSERT')]
        # 查重条件覆盖 站点+单号+工单+物料+批号（lot 允许 NULL，按空串等价比较）
        select_sql, select_params = cur.executed[0]
        assert 'siteRef = %s' in select_sql and 'documentnumber = %s' in select_sql
        assert 'job = %s' in select_sql and 'item = %s' in select_sql
        assert "COALESCE(lot, '') = COALESCE(%s, '')" in select_sql
        assert select_params[:6] == ('sl-job-issue', 310, 'JOB00001', 'J000029867',
                                     'B052552', '202505090000036')

    def test_duplicate_with_null_lot_sends_none_param(self, client, fake_db):
        """空 lot 复查：参数传 None，NULL 与空串都能命中（不会重复入队）"""
        cur = fake_db(existing={'id': 778})
        body = dict(BODY)
        body.pop('lot')
        resp = _post(client, body)

        assert resp.status_code == 200
        assert resp.get_json()['duplicated'] is True
        select_sql, select_params = cur.executed[0]
        assert "COALESCE(lot, '') = COALESCE(%s, '')" in select_sql
        assert select_params[5] is None
        assert not [s for s, _ in cur.executed if s.upper().startswith('INSERT')]


# ====================================================================== #
#  真实库（不可达时 skip）：落库字段与幂等复查
# ====================================================================== #
def _real_conn():
    from pymysql.cursors import DictCursor
    try:
        return _REAL_CONNECT(
            host=Config.MYSQL_HOST, port=Config.MYSQL_PORT,
            user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
            database=Config.MYSQL_DB, charset='utf8mb4',
            cursorclass=DictCursor, connect_timeout=8,
        )
    except Exception as e:
        pytest.skip(f'真实数据库不可用：{e}')


class _RealConnCtx:
    """把真实连接包成 contextmanager（不关闭，用例自己管）"""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


class TestRealDatabase:
    DOC = 'PYTESTJI0001'

    def _cleanup(self, conn):
        cur = conn.cursor()
        cur.execute("DELETE FROM kr_csi_interface WHERE documentnumber = %s", (self.DOC,))
        conn.commit()
        cur.close()

    def test_insert_and_duplicate_detected(self, client, monkeypatch):
        conn = _real_conn()
        try:
            self._cleanup(conn)
            monkeypatch.setattr(ext, 'get_db_connection', lambda: _RealConnCtx(conn))

            body = dict(BODY, documentNumber=self.DOC)
            r1 = _post(client, body)
            r2 = _post(client, body)

            assert r1.status_code == 201, r1.get_json()
            assert r2.status_code == 200 and r2.get_json()['duplicated'] is True

            cur = conn.cursor()
            cur.execute(
                "SELECT api_name, siteRef, job, warehouse, to_location, documentnumber, um, createby "
                "FROM kr_csi_interface WHERE documentnumber = %s", (self.DOC,)
            )
            rows = cur.fetchall()
            cur.close()
            assert len(rows) == 1
            row = rows[0]
            assert row['api_name'] == 'sl-job-issue'
            assert int(row['siteRef']) == 310
            assert row['job'] == 'J000029867'
            assert row['warehouse'] == 'C1M1'
            assert row['to_location'] == Config.CSI_TO_LOCATION
            assert row['um'] == 'EA'
            assert row['createby'] == 'wang.wu'
        finally:
            try:
                self._cleanup(conn)
            finally:
                conn.close()

    def test_null_lot_inserted_and_duplicate_detected(self, client, monkeypatch):
        """不传 lot → 落库 NULL；再提交同 (站点+单号+工单+物料+空 lot) → duplicated"""
        doc = 'PYTESTJI0002'
        conn = _real_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM kr_csi_interface WHERE documentnumber = %s", (doc,))
            conn.commit()
            cur.close()
            monkeypatch.setattr(ext, 'get_db_connection', lambda: _RealConnCtx(conn))

            body = dict(BODY, documentNumber=doc)
            body.pop('lot')
            r1 = _post(client, body)
            r2 = _post(client, body)

            assert r1.status_code == 201, r1.get_json()
            assert r2.status_code == 200 and r2.get_json()['duplicated'] is True

            cur = conn.cursor()
            cur.execute(
                "SELECT id, lot, lot IS NULL AS lot_null FROM kr_csi_interface "
                "WHERE documentnumber = %s", (doc,)
            )
            rows = cur.fetchall()
            cur.close()
            assert len(rows) == 1
            assert rows[0]['lot'] is None
            assert int(rows[0]['lot_null']) == 1
        finally:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM kr_csi_interface WHERE documentnumber = %s", (doc,))
                conn.commit()
                cur.close()
            finally:
                conn.close()
