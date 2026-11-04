"""卷标长度基准（初始长度）工具。

背景
----
kr_wire_coil.coil_length 一个字段被两种语义共用：

  1. **登记时**写入的是「初始长度」（原始单位，如 1000 FT）；
     CSI 接口 MP331405 的 quantity 即取该值原值（见 app/services/csi_interface.py）。
  2. **退料流程**（return_bp 确认 / 退回复核）会把 coil_length **回写成当前剩余长度**。

新增 initial_length 列后：

  - initial_length = 初始长度，登记时写入，**后续永不修改**；
  - coil_length    = 可能被回写成剩余长度的历史字段，仅作回退兜底。

所有「消耗超限校验」与「剩余长度计算」一律以 initial_length 为基准：

  - initial_length 存在 → 基准恒为初始长度，不受 coil_length 被回写影响；
  - initial_length 为 NULL（历史行）→ 回退 coil_length，行为与改动前完全一致。

单位换算系数见 Config.UNIT_CONVERT_FACTOR（M=1000 / FT=304.8 / CM=10 / IN=25.4）。
"""
from app.config import Config


def get_base_length(coil):
    """卷标长度基准（原始单位）：优先 initial_length（初始长度），回退 coil_length（历史数据）。

    返回 float；两者都缺失时返回 None。
    """
    if not coil:
        return None
    base = coil.get('initial_length')
    if base is None:
        base = coil.get('coil_length')
    if base is None:
        return None
    try:
        return float(base)
    except (TypeError, ValueError):
        return None


def get_base_length_mm(coil):
    """卷标长度基准（mm）：初始长度 × 换算系数。

    返回 float；单位未收录换算系数或长度缺失时返回 None（调用方据此跳过校验）。
    """
    base = get_base_length(coil)
    if base is None:
        return None
    factor = Config.UNIT_CONVERT_FACTOR.get((coil.get('unit') or '').strip().upper())
    if not factor:
        return None
    return base * factor
