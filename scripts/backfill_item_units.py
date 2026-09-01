# -*- coding: utf-8 -*-
"""
回填 kr_request_item.unit：根据 CSI ue_GDL_SLItems 的 UM 字段补齐历史空值。
- 串行查询（共享 CSIClient，token 一次）：并发共享 client 会 read timeout（线程不安全）
- 幂等可重复执行：仅更新 unit 为空的行，已完成物料自动跳过
- 失败物料记录到 backfill_fail.txt（可重跑补齐）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.routes.coil import _csi_clients
from app.services.csi_service import CSIClient
import pymysql


def main():
    conn = pymysql.connect(
        host=Config.MYSQL_HOST, port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
        database=Config.MYSQL_DB, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=8,
    )
    cur = conn.cursor()

    # 1. 取空 unit 的去重物料（按站点），串行查询
    cur.execute(
        "SELECT DISTINCT r.siteref, i.part_number FROM kr_request_item i "
        "JOIN kr_material_request r ON r.id = i.request_id "
        "WHERE (i.unit IS NULL OR i.unit = '') AND i.part_number IS NOT NULL AND i.part_number != ''"
    )
    rows = cur.fetchall()
    print(f'空 unit 去重物料: {len(rows)} 个（串行查询）', flush=True)
    if not rows:
        print('无需回填')
        conn.close()
        return

    # 2. 按站点串行查询（共享 client）
    by_site = {}
    for r in rows:
        by_site.setdefault(r['siteref'], []).append(r['part_number'])

    unit_map = {}
    t0 = time.time()
    done = 0
    for siteref, parts in by_site.items():
        client = _csi_clients.get(siteref) or CSIClient(siteref=siteref)
        _csi_clients[siteref] = client
        for p in parts:
            try:
                unit_map[(siteref, p)] = client.get_item_unit(p)
            except Exception as e:
                print(f'  [{p}] 查询失败: {e}', flush=True)
                unit_map[(siteref, p)] = None
            done += 1
            if done % 50 == 0:
                print(f'  进度 {done}/{len(rows)}（{(time.time()-t0)/60:.1f}min）', flush=True)

    # 3. 更新入库
    updated = 0
    failed = []
    for (siteref, part), unit in unit_map.items():
        if not unit:
            failed.append(f'{siteref}|{part}')
            continue
        cur.execute(
            "UPDATE kr_request_item i JOIN kr_material_request r ON r.id = i.request_id "
            "SET i.unit = %s WHERE r.siteref = %s AND i.part_number = %s AND (i.unit IS NULL OR i.unit = '')",
            (unit, siteref, part)
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()

    print(f'\n回填完成: 更新 {updated} 行，总耗时 {(time.time()-t0)/60:.1f} 分钟', flush=True)
    if failed:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backfill_fail.txt'), 'w') as f:
            f.write('\n'.join(failed))
        print(f'查询失败 {len(failed)} 个（已写入 backfill_fail.txt，可重跑补齐）', flush=True)
        print('失败示例:', failed[:10], flush=True)
    else:
        print('全部物料单位查询成功', flush=True)


if __name__ == '__main__':
    main()
