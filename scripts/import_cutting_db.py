# -*- coding: utf-8 -*-
"""
导入备料数据库 MP1.xlsx 的 BOM 表 → kr_cutting_ref（裁线参考表）。
- 核心字段映射为命名列（裁线调用参考用）
- 整行原始数据保留在 raw_data JSON（防信息丢失）
- 幂等：表不存在自动创建；重复导入前可先清空（--force）
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import pymysql
from app.config import Config

EXCEL_PATH = r'C:\Users\jason.pang\Desktop\备料数据库MP1.xlsx'
SHEET = 'BOM'
BATCH = 500

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS kr_cutting_ref (
  id              BIGINT        NOT NULL AUTO_INCREMENT,
  finished_part   VARCHAR(128)  DEFAULT NULL COMMENT '成品料号',
  wire_part       VARCHAR(128)  DEFAULT NULL COMMENT '线材（套管）料号',
  wire_awg        VARCHAR(32)   DEFAULT NULL COMMENT '线材AWG',
  color           VARCHAR(64)   DEFAULT NULL COMMENT '颜色',
  qty_per_group   DECIMAL(10,2) DEFAULT NULL COMMENT '单组剪切数量',
  cut_length_mm   DECIMAL(12,2) DEFAULT NULL COMMENT '剪切长度(mm)',
  length_tol      VARCHAR(32)   DEFAULT NULL COMMENT '长度公差',
  cut_device      VARCHAR(64)   DEFAULT NULL COMMENT '剪切设备',
  device_no       VARCHAR(64)   DEFAULT NULL COMMENT '设备序号',
  strip_len_a     DECIMAL(10,2) DEFAULT NULL COMMENT '去皮尺寸A端(mm)',
  strip_tol_a     VARCHAR(32)   DEFAULT NULL COMMENT '去皮尺寸公差A端',
  strip_len_b     DECIMAL(10,2) DEFAULT NULL COMMENT '去皮尺寸B端(mm)',
  strip_tol_b     VARCHAR(32)   DEFAULT NULL COMMENT '去皮尺寸公差B端',
  term_a          VARCHAR(128)  DEFAULT NULL COMMENT 'A端端子料号',
  term_b          VARCHAR(128)  DEFAULT NULL COMMENT 'B端端子料号',
  raw_data        JSON          DEFAULT NULL COMMENT '整行原始数据',
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_finished_part (finished_part),
  INDEX idx_wire_part (wire_part),
  INDEX idx_finished_wire (finished_part, wire_part)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='成品对应物料裁线数据库（备料MP1导入，裁线时调用参考）'
"""


def to_dec(v):
    if v is None or str(v).strip() == '':
        return None
    try:
        return float(str(v).strip().replace(',', ''))
    except (TypeError, ValueError):
        return None


def to_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def main():
    force = '--force' in sys.argv

    # 1. 读 Excel
    print('读取 Excel...', flush=True)
    wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
    ws = wb[SHEET]
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h).replace('\n', ' ').strip() if h else '' for h in next(rows_iter)]
    print(f'表头 {len([h for h in header if h])} 列', flush=True)

    # 2. 建表
    conn = pymysql.connect(
        host=Config.MYSQL_HOST, port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    cur = conn.cursor()
    cur.execute(CREATE_TABLE)
    conn.commit()
    if force:
        cur.execute('TRUNCATE TABLE kr_cutting_ref')
        conn.commit()
        print('已清空旧数据', flush=True)

    # 3. 逐行提取核心字段
    INSERT_SQL = """
        INSERT INTO kr_cutting_ref
        (finished_part, wire_part, wire_awg, color, qty_per_group, cut_length_mm,
         length_tol, cut_device, device_no, strip_len_a, strip_tol_a,
         strip_len_b, strip_tol_b, term_a, term_b, raw_data)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    t0 = time.time()
    total = 0
    skipped = 0
    batch = []
    for row in rows_iter:
        vals = list(row)
        fp = to_str(vals[0]) if len(vals) > 0 else None
        wp = to_str(vals[1]) if len(vals) > 1 else None
        if not fp and not wp:
            skipped += 1
            continue  # 跳过完全空行
        raw = {header[i]: (str(v) if v is not None else None) for i, v in enumerate(vals) if i < len(header) and header[i] and v is not None}
        batch.append((
            fp,
            wp,
            to_str(vals[2]) if len(vals) > 2 else None,
            to_str(vals[3]) if len(vals) > 3 else None,
            to_dec(vals[4]) if len(vals) > 4 else None,
            to_dec(vals[5]) if len(vals) > 5 else None,
            to_str(vals[6]) if len(vals) > 6 else None,
            to_str(vals[7]) if len(vals) > 7 else None,
            to_str(vals[8]) if len(vals) > 8 else None,
            to_dec(vals[12]) if len(vals) > 12 else None,
            to_str(vals[13]) if len(vals) > 13 else None,
            to_dec(vals[18]) if len(vals) > 18 else None,
            to_str(vals[19]) if len(vals) > 19 else None,
            to_str(vals[34]) if len(vals) > 34 else None,
            to_str(vals[44]) if len(vals) > 44 else None,
            json.dumps(raw, ensure_ascii=False) if raw else None,
        ))
        total += 1
        if len(batch) >= BATCH:
            cur.executemany(INSERT_SQL, batch)
            conn.commit()
            batch = []
            print(f'  已导入 {total} 行（{time.time()-t0:.0f}s）', flush=True)

    if batch:
        cur.executemany(INSERT_SQL, batch)
        conn.commit()
    conn.close()

    print(f'\n导入完成: {total} 行（跳过空行 {skipped}），耗时 {time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
