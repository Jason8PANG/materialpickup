"""CSI Floor-Core 余量对齐（纯计算，可脱离 CSI / 数据库单测）

背景
----
CSI 侧发料（sl-job-issue）会校验「发料数量 ≤ 货位 Floor-Core 存货」，
车间实耗与 CSI 账面余量之间常有零点几 FT 的出入（取整口径 / 换算 / 换卷标残留），
接口提交因此被平台拒收（实测告警：发料数量 87.9921 大于批次 ... 存货 0）。

本模块只做纯计算，不碰 CSI、不碰数据库：
    available = Floor-Core 实时余量 − 同 item+lot 其它「尚未成功扣减」的消耗量
    |available − 本笔消耗量| ≤ 阈值  →  本笔消耗量直接改成 available
    否则不调整（照常提交 → CSI 拒收 → FAILED_MANUAL → 告警邮件 → 转人工）

安全约定
--------
1. **单位不一致一律放弃对齐**（不做任何换算）：双方单位不同就说明两个数不可比，
   强行写值等于往接口表塞一个量纲错误的数量，比不调整更危险。
2. available ≤ 0 不调整：CSI 没有余量时照常提交，让平台拒收并转人工，
   而不是替业务方决定「扣 0」。
3. 数量收敛到 CSI 支持的 3 位小数；FT 用 ceil 向上进位
   （与 app/services/unit_convert.py 的 CEIL_UNITS 语义一致）。
"""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

# 对齐阈值（单位同原始单位，通常是 FT）
CSI_FLOOR_ALIGN_TOLERANCE = 0.1

# CSI 数量只支持 3 位小数（超出即被拒收）
CSI_QTY_DECIMALS = 3

# 仅这些单位用 ceil 向上进位保留 3 位小数（与 unit_convert.CEIL_UNITS 一致）
CEIL_UNITS = ('FT',)

# 浮点比较容差：0.1 的二进制表示误差会让「恰好等于阈值」被判成「大于阈值」
_FLOAT_EPS = 1e-9


def _round_csi_qty(value, unit):
    """把数量收敛到 CSI 支持的 3 位小数；FT 向上进位，其它单位四舍五入。

    返回 float；输入非法返回 None。
    """
    try:
        d = Decimal(str(round(float(value), 9)))
    except (TypeError, ValueError):
        return None
    quantum = Decimal(1).scaleb(-CSI_QTY_DECIMALS)
    u = (unit or '').strip().upper()
    rounding = ROUND_CEILING if u in CEIL_UNITS else ROUND_HALF_UP
    try:
        return float(d.quantize(quantum, rounding=rounding))
    except Exception:
        return None


def align_floor_stock_quantity(row_qty, floor_qty, pending_qty, um, floor_um,
                                tolerance=CSI_FLOOR_ALIGN_TOLERANCE):
    """计算本次可提交的数量（纯函数）。

    参数
    ----
    row_qty     本笔接口表 quantity（原始单位，如 FT）
    floor_qty   CSI Floor-Core 该 item+lot 的实时余额（同单位）
    pending_qty 同 item+lot 其它尚未成功扣减的行 quantity 之和（同单位）
    um          接口表行内单位
    floor_um    CSI 余额单位（IDO 的 ItemUM）
    tolerance   对齐阈值

    返回
    ----
    (new_qty, reason)
    new_qty 为 None 表示「不调整，照常提交」；
    reason 为 dict，记录判定依据（写审计日志用）。
    """
    reason = {
        'action': 'skip',
        'reason': '',
        'row_qty': None,
        'floor_qty': None,
        'pending_qty': None,
        'available': None,
        'diff': None,
        'um': (um or '').strip().upper() or None,
        'floor_um': (floor_um or '').strip().upper() or None,
        'tolerance': tolerance,
    }

    try:
        row_qty = float(row_qty)
        floor_qty = float(floor_qty)
        pending_qty = float(pending_qty or 0)
        tolerance = float(tolerance)
    except (TypeError, ValueError):
        reason['reason'] = '数量非法，无法比较'
        return None, reason

    reason['row_qty'] = row_qty
    reason['floor_qty'] = floor_qty
    reason['pending_qty'] = pending_qty

    if row_qty <= 0:
        reason['reason'] = '本笔数量非正，不对齐'
        return None, reason

    # 1) 单位不一致（或任一方单位未知）→ 放弃对齐，不做任何换算
    u_row = (um or '').strip().upper()
    u_floor = (floor_um or '').strip().upper()
    if not u_row or not u_floor:
        reason['reason'] = '单位未知（接口表 um / CSI ItemUM 缺失），不对齐'
        return None, reason
    if u_row != u_floor:
        reason['reason'] = f'单位不一致（接口表 {u_row} / CSI {u_floor}），不对齐'
        return None, reason

    # 2) 本笔可用余额 = CSI 实时余额 − 其它未成功扣减的消耗量
    available = floor_qty - pending_qty
    reason['available'] = available
    if available <= 0:
        reason['reason'] = '按余额计算的可用量 ≤ 0（CSI 无余量或已被其它待扣行占用），不对齐'
        return None, reason

    # 3) 差异判定（方向不限：略大略小都按剩余量改）
    diff = abs(available - row_qty)
    reason['diff'] = diff
    if diff - tolerance > _FLOAT_EPS:
        reason['reason'] = f'差异 {diff:.4f} 超过阈值 {tolerance}，不对齐'
        return None, reason

    # 4) 收敛到 3 位小数并保证 > 0
    new_qty = _round_csi_qty(available, u_row)
    if new_qty is None or new_qty <= 0:
        reason['reason'] = '对齐值收敛后 ≤ 0，不对齐'
        return None, reason

    reason['action'] = 'align'
    reason['reason'] = f'差异 {diff:.4f} ≤ 阈值 {tolerance}，消耗量改为 Floor-Core 剩余量'
    reason['new_qty'] = new_qty
    return new_qty, reason
