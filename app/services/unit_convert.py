"""单位换算公共模块（mm ↔ 目标单位）。

背景：CSI（Infor）侧数量只支持 3 位小数，超出即被拒收
（实测告警：发料数量 87.9921 大于批次 ... 存货 0，CSI 直接拒收）。
因此 mm → 目标单位统一改用 **ceil 向上进位** 保留 3 位小数（不是四舍五入），
既保证换算结果不小于真实值（不多扣料），又满足 CSI 的 3 位小数精度。

系数表：Config.UNIT_CONVERT_FACTOR（M=1000 / FT=304.8 / CM=10 / IN=25.4）。

用法：
    from app.services.unit_convert import convert_from_mm, convert_to_mm
    convert_from_mm(1000, 'FT')   # -> 3.281（向上进位到 3 位）
    convert_to_mm(3.281, 'FT')    # -> 1000.0488（原单位 → mm，保留 4 位）
"""
from decimal import Decimal, ROUND_CEILING

from app.config import Config

# 仅这些单位改用 ceil 向上进位保留 3 位小数：
# FT 的系数 304.8 除不尽，mm→FT 必然产生 4 位小数（如 1000mm → 3.2808），
# 而 CSI 侧数量只支持 3 位小数，超出即被拒收（实测：发料数量 87.9921 被 CSI 拒）。
# M(1000) / CM(10) 常规录入为整毫米，保持原 round 4 语义不变；IN 极少使用，同样保持原状。
CEIL_UNITS = ('FT',)


def get_factor(unit):
    """取换算系数（大小写不敏感）；单位为空或未收录返回 None。"""
    u = (unit or '').strip().upper()
    return Config.UNIT_CONVERT_FACTOR.get(u) if u else None


def convert_from_mm(mm_value, unit, decimals=3):
    """mm → 目标单位换算。返回 float 或 None（单位未收录 / 值为空）。

    规则：raw = mm_value / factor。
      - 单位在 CEIL_UNITS（FT）内：**ceil 向上进位**保留 3 位小数（不是四舍五入），
        既保证换算结果不小于真实值（不多扣料），又满足 CSI 的 3 位小数精度；
      - 其他单位保持原语义 round(raw, 4)。

    注意：必须走 Decimal 量化，不能直接 math.ceil(raw * 1000) / 1000，
    否则二进制浮点误差会把 1.0000000000000002 进位成 1.001。
    """
    factor = get_factor(unit)
    if factor is None or mm_value is None or mm_value == '':
        return None
    try:
        raw = float(mm_value) / float(factor)
    except (TypeError, ValueError):
        return None
    u = (unit or '').strip().upper()
    if u not in CEIL_UNITS:
        return round(raw, 4)
    quantum = Decimal(1).scaleb(-decimals)
    value = Decimal(str(round(raw, 9))).quantize(quantum, rounding=ROUND_CEILING)
    return float(value)


def convert_to_mm(value, unit):
    """目标单位 → mm（乘法方向）。返回 float 或 None（单位未收录 / 值为空）。

    保持原语义 round(value * factor, 4)；×304.8 / ×25.4 结果精确，无浮点精度问题。
    """
    factor = get_factor(unit)
    if factor is None or value is None or value == '':
        return None
    try:
        return round(float(value) * float(factor), 4)
    except (TypeError, ValueError):
        return None
