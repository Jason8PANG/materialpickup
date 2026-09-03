"""
线卷标签打印服务 - 通道解耦

设计：
  - LabelRenderer : 纯函数，将线卷记录渲染为标签数据（文本 + 条码内容）
  - LabelPrinter  : 组合逻辑：查库存 → 渲染 → 按配置选择打印通道
  - 打印通道       : gdi（win32ui 驱动绘制，通用）
                    raw_zpl（win32print RAW 直发 ZPL，Zebra）
                    raw_tspl（win32print RAW 直发 TSPL，TSC）
                    gateway（转发到 Windows 打印网关，主后端跑 Linux 时的过渡方案）

容错：
  - 环境无 pywin32 / python-barcode 时优雅降级（记录日志，返回可读错误），
    不影响申请单其他操作。
  - GDI/RAW 打印用 threading.Lock 串行化，避免并发驱动错乱。
  - 单卷打印失败只记录该卷错误，不中断整批。

标签版式（方案 C：80mm × 26mm @203dpi = 640×208 dot，5mm margin=40dot，内容区 x40~600 / y40~168）：
  ┌────────────────────────────────────────────────────────┐
  │ 右上角标「始」y16~40 x576~600（仅期初半卷；随条码左移仍贴内容右缘，不覆盖条码/不出纸边）│
  │ Coil ID:（键名小字 20dot）        ▆▆▆▆▆ Code128 条码      │ 行1（顶 y40，条码底 y84）：
  │ 260828001（卷号大字 36dot）        ▆▆▆ x300 y40 高44dot   │   键名 x40 y40；卷号 x40 y64（右缘 ~234）
  │                                  ▆▆▆（下方不印可读卷号）  │   < 条码左缘 x300（留 ~66dot ≥10dot）
  │ ············ 行1↔行2 间隔 3mm/24dot ··············      │   卷号底 y100 / 条码底 y84 → 行2 顶 y108
  │ Part : A080507                  Lenght : 10 FT          │ 行2（y108~144）：左 x40 | 右 x300
  │ ·············· 底缘余量 24dot（至 y168） ···········     │   （与条码左缘对齐；Lenght 右缘 ≤600）
  └────────────────────────────────────────────────────────┘
  字号：卷号 / Part / Lenght = 36dot(4.5mm) 加粗（现状 24dot 放大 1.5 倍，预览 18px）；
        "Coil ID:" 键名缩小为 20dot(2.5mm) 标签小字置于卷号上方（预览 10px）。
  TSPL 卡点：TSC 内置 CJK 位图字体 TSS24.BF2 只能整数放大（1×=24 / 2×=48），1.5×=36dot 无法表达；
        → raw_tspl 通道文字沿用 TSS24.BF2 1×1=24dot（版式坐标与其它通道完全一致，2×=48 会越界）。
        若现场为 TSPL2 机型（内置 "0" 号 TrueType，宽/高按 pt 设定），36dot≈13pt(4.58mm)，
        建议启用 TTF 或以默认 gdi / raw_zpl 通道保证与 ZPL/预览完全一致。
  条码：Code128 高 44dot(5.5mm) 不变，仅左移 10mm：左缘 x380→x300；Lenght 同步 x300（与条码左缘对齐）。
  预览：80×26mm → 320×104px（4px/mm），行1 键名 10px、卷号/行2 18px，条码高 22px、左缘 150px(=x300)。
"""
import io
import logging
import os
import threading
from datetime import datetime

from app.config import Config

logger = logging.getLogger(__name__)

# 打印串行化锁（GDI/RAW 并发会导致驱动错乱）
_print_lock = threading.Lock()

# ================= 环境能力检测 =================
try:
    import win32print
    _HAS_WIN32PRINT = True
except ImportError:
    win32print = None
    _HAS_WIN32PRINT = False

try:
    import win32ui
    import win32con
    _HAS_WIN32UI = True
except ImportError:
    win32ui = None
    win32con = None
    _HAS_WIN32UI = False

try:
    import barcode as _barcode
    from barcode.writer import ImageWriter
    _HAS_BARCODE = True
except ImportError:
    _barcode = None
    ImageWriter = None
    _HAS_BARCODE = False

try:
    from PIL import Image, ImageWin
    _HAS_PIL = True
except ImportError:
    Image = None
    ImageWin = None
    _HAS_PIL = False

# 标签物理尺寸（mm）
LABEL_WIDTH_MM = 80
LABEL_HEIGHT_MM = 26

# ZPL/TSPL 模板映射（按打印机型号扩展）
LABEL_TEMPLATES = {
    'zpl': 'Zebra',
    'tspl': 'TSC',
}


def _clean_fd(text: str) -> str:
    """清理 ZPL/TSPL 字段中可能导致指令歧义的字符"""
    return (text or '').replace('^', '').replace('~', '').replace('\\', '')


class LabelRenderer:
    """纯函数渲染：线卷记录 → 标签数据"""

    @staticmethod
    def render(coil: dict) -> dict:
        coil_id = str(coil.get('coil_id') or '').strip()
        part_number = str(coil.get('part_number') or '').strip()
        try:
            length = float(coil.get('coil_length') or 0)
        except (TypeError, ValueError):
            length = 0.0
        unit = (coil.get('unit') or '').strip()
        length_text = f"{length:g}{(' ' + unit) if unit else ''}"
        is_initial_half = 1 if coil.get('is_initial_half') else 0

        return {
            'coil_id': coil_id,
            'part_number': part_number,
            'length': length,
            'unit': unit,
            'length_text': length_text,
            'barcode_coil': coil_id,
            'barcode_part': part_number,
            'is_initial_half': is_initial_half,  # 期初半卷：1=是（右上角打「始」角标）
        }


# ================= ZPL / TSPL 指令构建 =================

def build_zpl(label: dict) -> bytes:
    """
    构建 Zebra ZPL（203dpi：80mm ≈ 640dot，26mm ≈ 208dot，5mm margin ≈ 40dot）。
    条码由打印机固件生成，Code128（9 位卷号实测约 202dot 宽）。
    版式（方案 C：文字放大 1.5×=36dot，条码与 Lenght 同步左移 10mm）：
      行1（顶 y40，条码底 y84）：
        左列：键名 "Coil ID:" A0N 20/10 小字 @x40,y40（右缘 ~150）；
              卷号 9 位大字 A0N 36/18 @x40,y64（右缘 ~234 < 条码左缘 x300，留 ≥10dot 安全间距）
        右：  Code128 条码 ^FO300,40 高 44dot（interpret line=N，条码下方不印卷号；右缘 ~502 ≤ 600）
      行1↔行2 间隔 24dot(3mm)：行1 底（y100/条码 y84）→ 行2 文字顶 y108
      行2（y108，同字号 A0N 36/18）：左下 "Part : xxx" x40 | 右下 "Lenght :xxx" x300（左缘=条码左缘）
    期初半卷角标「始」（y16~40，右缘贴 x600）仍在右上角，条码左移后不与条码重叠、不出纸边。
    """
    coil_id = _clean_fd(label['barcode_coil'])
    part = _clean_fd(label['barcode_part'])
    length_text = _clean_fd(label['length_text'])

    zpl = "^XA\n^PW640^LL208^LH0,0\n"
    if label.get('is_initial_half'):
        # 期初半卷「始」角标：右上角 y16~40，x576~600（贴内容右缘；条码已左移 x300~502，二者不相交）
        zpl += "^FO576,16^A@N,24,24,E:ARIALUNI.TTF^CI28^FD始^FS\n"
    zpl += (
        # 行1 右：Code128 条码（内容 = 卷标ID；顶 y40，高 44dot → 底 y84；interpret=N 不印可读行；左缘 300=内容中部 37.5mm）
        f"^FO300,40^BCN,44,N,N,N^FD{coil_id}^FS\n"
        # 行1 左上：键名小字 "Coil ID:"（A0N 20/10=2.5mm，标签式键名，置于卷号上方）
        f"^FO40,40^A0N,20,10^FDCoil ID:^FS\n"
        # 行1 左：卷号大字（A0N 36/18=4.5mm，y64 位于键名之下；9 位右缘 ~234 < 条码左缘 300）
        f"^FO40,64^A0N,36,18^FD{coil_id}^FS\n"
        # 行2 左下：Part（y108 = 行1 底 y100 + 8dot / 条码底 y84 + 24dot；与行1 卷号同字号 36dot）
        f"^FO40,108^A0N,36,18^FDPart : {part}^FS\n"
        # 行2 右下：Lenght（"Lenght :" 与参考图一致；左缘 x300 与条码左缘对齐；右缘 ≤600）
        f"^FO300,108^A0N,36,18^FDLenght :{length_text}^FS\n"
        "^XZ\n"
    )
    return zpl.encode('utf-8')


def build_tspl(label: dict) -> bytes:
    """
    构建 TSC TSPL（80mm × 26mm，5mm margin = 40dot @203dpi，总幅面 640×208 dot）。
    条码由打印机固件生成，Code128（9 位卷号实测约 202dot 宽）。
    版式（与 ZPL 坐标完全一致，方案 C：文字 36dot 版式 / 条码与 Lenght 同步左移 10mm）：
      行1（顶 y40，条码底 y84）：
        左列：键名 "Coil ID:" 小字 y40 | 卷号大字 y64（右缘 ~234 < 条码左缘 x300）
        右：  Code128 条码 BARCODE 300,40 高 44dot（readable=0，条码下方不印卷号；右缘 ~502 ≤ 600）
      行1↔行2 间隔 24dot(3mm)：行1 底（y100/条码 y84）→ 行2 文字顶 y108
      行2（y108）：左下 "Part : xxx" x40 | 右下 "Lenght :xxx" x300（左缘=条码左缘）
    TSPL 位图字体卡点：内置 CJK 位图字体 TSS24.BF2 仅支持整数放大（1×=24 / 2×=48），
      "24×1.5=36dot" 不可表达，且 2×=48 会使文字超出本版式的行高/坐标（互相越界）。
      故本通道文字沿用 TSS24.BF2 1×1=24dot（布局/坐标与其他通道一致，可安全打印）。
      若现场为 TSPL2 机型（内置 "0" 号 / ROMAN.TTF TrueType），TEXT 的高参数按 pt 设定：
      36dot @203dpi ≈ 13pt（36.7dot，偏差 <2%）；建议此时改用默认 gdi / raw_zpl 或开启 TTF 保证视觉一致。
    期初半卷角标（y16~40，右缘贴 x600）在右上角，条码左移后不与条码重叠、不出纸边。
    """
    coil_id = _clean_fd(label['barcode_coil'])
    part = _clean_fd(label['barcode_part'])
    length_text = _clean_fd(label['length_text'])

    tsp = (
        "SIZE 80 mm,26 mm\n"
        "GAP 2 mm,0\n"
        "CLS\n"
    )
    if label.get('is_initial_half'):
        # 期初半卷「始」角标：右上角 y16~40，x576~600（贴内容右缘；条码已左移 x300~502，二者不相交）
        tsp += f'TEXT 576,16,"TSS24.BF2",0,1,1,"始"\n'
    tsp += (
        # 行1 右：Code128 条码（内容 = 卷标ID；顶 y40，高 44dot → 底 y84；readable=0 不印可读行；左缘 300）
        f'BARCODE 300,40,"128",44,0,0,2,2,"{coil_id}"\n'
        # 行1 左上：键名小字 "Coil ID:"（TSS24.BF2 1×1，置于卷号上方；同 ZPL 键名语义）
        f'TEXT 40,40,"TSS24.BF2",0,1,1,"Coil ID:"\n'
        # 行1 左：卷号（y64 位于键名之下；注：位图字体 1×1=24dot，见函数 docstring 的 TSPL 卡点说明）
        f'TEXT 40,64,"TSS24.BF2",0,1,1,"{coil_id}"\n'
        # 行2 左下：Part（y108 同 ZPL 新版式坐标）
        f'TEXT 40,108,"TSS24.BF2",0,1,1,"Part : {part}"\n'
        # 行2 右下：Lenght（左缘 x300 与条码左缘对齐）
        f'TEXT 300,108,"TSS24.BF2",0,1,1,"Lenght :{length_text}"\n'
        "PRINT 1\n"
    )
    return tsp.encode('utf-8')


# ================= 条码位图生成（GDI 通道用） =================

def _barcode_image(data: str):
    """生成 Code128 条码 PNG（PIL Image），环境缺 python-barcode 时返回 None"""
    if not (_HAS_BARCODE and _HAS_PIL):
        return None
    try:
        rv = io.BytesIO()
        writer = ImageWriter()
        code = _barcode.get('code128', data, writer=writer)
        code.write(rv, {
            'module_width': 0.3,
            'module_height': 12.0,
            'font_size': 0,
            'quiet_zone': 1.5,
            'write_text': False,
            'format': 'PNG',
        })
        rv.seek(0)
        return Image.open(rv)
    except Exception as e:
        logger.warning(f"[LABEL] 条码位图生成失败: {e}")
        return None


# ================= GDI 通道（win32ui 驱动绘制，通用） =================

def _gdi_print(printer_name: str, label: dict) -> None:
    """通过 Windows 打印驱动 GDI 绘制标签（80mm × 26mm，5mm margin）"""
    if not _HAS_WIN32UI:
        raise RuntimeError('当前环境缺少 pywin32（win32ui），无法 GDI 打印')
    if not _HAS_PIL:
        raise RuntimeError('当前环境缺少 Pillow，无法 GDI 打印条码')

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer_name)

    # 尝试设置自定义纸型 80mm×26mm（best-effort，失败则用驱动默认纸型）
    _set_custom_paper(dc, printer_name, LABEL_WIDTH_MM, LABEL_HEIGHT_MM)

    dpi_x = dc.GetDeviceCaps(88)   # LOGPIXELSX
    dpi_y = dc.GetDeviceCaps(90)   # LOGPIXELSY

    def mm_x(mm):
        return int(round(mm * dpi_x / 25.4))

    def mm_y(mm):
        return int(round(mm * dpi_y / 25.4))

    width_px = mm_x(LABEL_WIDTH_MM)
    height_px = mm_y(LABEL_HEIGHT_MM)

    # 黑色实心画刷（占位/底条）
    # 注意：win32ui.CreateBrush(style, color, hatch) 必须传第 3 个参数 hatch，
    #       否则报 "function takes exactly 3 arguments (2 given)"
    black_brush = win32ui.CreateBrush(win32con.BS_SOLID, 0, 0)
    white_brush = win32ui.CreateBrush(win32con.BS_SOLID, 0x00FFFFFF, 0)

    try:
        dc.StartDoc('WireCoilLabel')
        dc.StartPage()

        # 白底（FillRect 需要 PyCBrush 对象，不能传句柄）
        dc.FillRect((0, 0, width_px, height_px), white_brush)

        # 字体（方案 C：卷号/Part/Lenght 放大 1.5×=36dot=4.5mm；"Coil ID:" 键名缩小 20dot=2.5mm；
        #   角标「始」保持 24dot=3.0mm 不变；全部 Arial 加粗）
        body_font = win32ui.CreateFont({
            'name': 'Arial', 'height': mm_y(4.5), 'weight': 700, 'charset': 1,
        })
        caption_font = win32ui.CreateFont({
            'name': 'Arial', 'height': mm_y(2.5), 'weight': 700, 'charset': 1,
        })
        badge_font = win32ui.CreateFont({
            'name': 'Arial', 'height': mm_y(3.0), 'weight': 700, 'charset': 1,
        })

        coil_id = label['barcode_coil']
        part = label['barcode_part']

        # 版式（右缘 5mm margin = 75mm；80×26mm，与 ZPL/TSPL 坐标一致）：
        #   行1（顶 y5mm，条码底 10.5mm）：
        #     左列：键名 "Coil ID:"（x5mm y5mm，caption_font 2.5mm）→ 卷号大字（x5mm y8mm，body 4.5mm；
        #           9 位右缘 ~29mm 内容 x300=37.5mm 左侧，安全）
        #     右：  Code128 位图（x37.5~62.5mm，y5~10.5mm；位图不含可读文字）
        #     角标：期初半卷「始」（x72mm y2mm，右上角贴边，右缘 75mm）
        #   行1↔行2 间隔 3mm（24dot）：行1 底 y12.5mm/条码底 10.5mm → 行2 文字顶 y13.5mm
        #   行2（body 4.5mm，底 y18mm ≤ 内容底 21mm）：左下 "Part : xxx" x5mm；右下 "Lenght :xxx" x37.5mm
        barcode_top = mm_y(5.0)
        barcode_height = mm_y(5.5)
        barcode_left = mm_x(37.5)  # 37.5mm = 300dot：与 ZPL/TSPL 同步左移 10mm
        barcode_w = mm_x(25)  # 右缘 = 37.5 + 25 = 62.5mm ≤ 75mm（对应 ZPL 实测 ~502dot）
        img1 = _barcode_image(coil_id)

        if img1:
            # draw(hdc, destRect, srcRect)：win32ui 的 Dib.draw 需 3 个参数（新版 pywin32 强制）
            ImageWin.Dib(img1).draw(dc.GetHandleOutput(),
                                    (barcode_left, barcode_top,
                                     barcode_left + barcode_w, barcode_top + barcode_height),
                                    (0, 0, img1.size[0], img1.size[1]))
        else:
            dc.FillRect((barcode_left, barcode_top,
                         barcode_left + barcode_w, barcode_top + barcode_height),
                        black_brush)

        # 期初半卷「始」角标：右上角（y2mm，高 3mm → 底 5mm），贴右缘 x72mm；条码左移后不与条码重叠
        if label.get('is_initial_half'):
            dc.SelectObject(badge_font)
            dc.TextOut(mm_x(72), mm_y(2.0), '始')

        # 行1 左：键名小字 "Coil ID:"（caption_font 2.5mm，x5mm y5mm，置于卷号上方）
        dc.SelectObject(caption_font)
        dc.TextOut(mm_x(5), mm_y(5.0), 'Coil ID:')

        # 行1 左：卷号大字（body_font 4.5mm，x5mm y8mm = y64dot，位于键名之下）
        dc.SelectObject(body_font)
        dc.TextOut(mm_x(5), mm_y(8.0), coil_id)

        # 行2：左下 Part、右下 Lenght（y13.5mm = 108dot；Lenght 左缘 x37.5mm 与条码左缘对齐）
        dc.TextOut(mm_x(5), mm_y(13.5), f'Part : {part}')
        dc.TextOut(mm_x(37.5), mm_y(13.5), f'Lenght :{label["length_text"]}')

        dc.EndPage()
        dc.EndDoc()
    finally:
        try:
            dc.DeleteDC()
        except Exception:
            pass


def _set_custom_paper(dc, printer_name: str, width_mm: float, height_mm: float) -> None:
    """best-effort 设置自定义纸型（DEVMODE dmPaperWidth/dmPaperLength，单位 0.1mm）"""
    if not _HAS_WIN32PRINT:
        return
    try:
        h = win32print.OpenPrinter(printer_name)
        try:
            devmode = win32print.GetPrinter(h, 2)['pDevMode']
            devmode.PaperSize = 256        # DMPAPER_USER
            devmode.PaperWidth = int(width_mm * 10)
            devmode.PaperLength = int(height_mm * 10)
            dc.ResetDC(devmode)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        logger.warning(f"[LABEL] 设置自定义纸型失败（将使用驱动默认纸型）: {e}")


# ================= RAW 通道（ZPL / TSPL 指令直发） =================

def _raw_print(printer_name: str, data: bytes) -> None:
    """通过 win32print RAW 直发指令（StartDocPrinter 'RAW' + WritePrinter）"""
    if not _HAS_WIN32PRINT:
        raise RuntimeError('当前环境缺少 pywin32（win32print），无法 RAW 直发打印')
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, ("WireCoilLabel", None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


# ================= 网关通道（Windows 打印网关代理，方案 C） =================

def _gateway_print(labels: list[dict], printer_name: str) -> None:
    """转发到 Windows 打印网关（HTTP + token 鉴权）"""
    import httpx
    url = (Config.LABEL_PRINT_GATEWAY_URL or '').rstrip('/')
    if not url:
        raise RuntimeError('未配置 LABEL_PRINT_GATEWAY_URL')
    resp = httpx.post(
        url + '/print',
        headers={
            'Authorization': f'Bearer {Config.LABEL_PRINT_GATEWAY_TOKEN}',
            'Content-Type': 'application/json',
        },
        json={'printer': printer_name, 'labels': labels},
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get('success'):
        raise RuntimeError(body.get('message') or '打印网关返回失败')


# ================= 环境检查 / 默认打印机 =================

def get_default_printer() -> str:
    if not _HAS_WIN32PRINT:
        return ''
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return ''


def check_print_environment() -> dict:
    """返回打印环境可用性（供接口给出可读提示，不影响其他功能）"""
    channel = (Config.LABEL_PRINT_CHANNEL or 'gdi').lower()
    gateway = (Config.LABEL_PRINT_GATEWAY_URL or '').strip()
    if gateway:
        return {'available': True, 'channel': 'gateway', 'message': '使用 Windows 打印网关'}
    if channel in ('raw_zpl', 'raw_tspl'):
        ok = _HAS_WIN32PRINT
        return {
            'available': ok,
            'channel': channel,
            'message': 'RAW 指令直发可用' if ok else '当前环境缺少 pywin32，无法直发打印（不影响其他功能）',
        }
    # gdi
    ok = _HAS_WIN32UI and _HAS_PIL
    return {
        'available': ok,
        'channel': 'gdi',
        'message': 'GDI 驱动打印可用' if ok else '当前环境缺少 pywin32/Pillow，无法打印（不影响其他功能）',
    }


# ================= 对外统一接口 =================

def print_labels(coils: list[dict], printer_name: str = None, channel: str = None) -> dict:
    """
    打印一批线卷标签。

    Args:
        coils: 线卷记录 dict 列表，每项含 coil_id / part_number / coil_length / unit
        printer_name: 目标打印机；为空则取 Config.LABEL_PRINTER_NAME 或系统默认打印机
        channel: 打印通道；为空则取 Config.LABEL_PRINT_CHANNEL 或 'gdi'

    Returns:
        {
          'printed': int,      # 成功打印张数
          'errors': [str, ...] # 失败卷的错误信息（含卷号）
          'success': bool,     # 全部成功为 True，部分/全部失败为 False
        }
    """
    printer = (printer_name or Config.LABEL_PRINTER_NAME or '').strip()
    if not printer:
        printer = get_default_printer()

    errors = []
    printed = 0

    if not coils:
        return {'printed': 0, 'errors': ['没有需要打印的标签'], 'success': False}

    gateway = (Config.LABEL_PRINT_GATEWAY_URL or '').strip()
    channel = (channel or Config.LABEL_PRINT_CHANNEL or 'gdi').lower()

    # 网关通道：一次性批量转发
    if gateway:
        rendered = [LabelRenderer.render(c) for c in coils]
        try:
            _gateway_print(rendered, printer)
            printed = len(coils)
        except Exception as e:
            logger.error(f"[LABEL] 网关打印失败: {e}")
            for c in coils:
                errors.append(f"{c.get('coil_id', '?')}: 打印失败 - {e}")
        return {'printed': printed, 'errors': errors, 'success': not errors}

    # GDI / RAW：逐卷串行打印
    with _print_lock:
        for coil in coils:
            coil_id = coil.get('coil_id', '?')
            try:
                label = LabelRenderer.render(coil)
                if channel == 'raw_zpl':
                    _raw_print(printer, build_zpl(label))
                elif channel == 'raw_tspl':
                    _raw_print(printer, build_tspl(label))
                else:
                    _gdi_print(printer, label)
                printed += 1
            except Exception as e:
                logger.error(f"[LABEL] 卷 {coil_id} 打印失败: {e}")
                errors.append(f"{coil_id}: 打印失败 - {e}")

    return {'printed': printed, 'errors': errors, 'success': not errors}


# ================= Bartender 触发文件（.dd）通道 =================
#
# 卷标打印改为 Bartender 触发文件方式：系统不再向打印机发 ZPL/TSPL/GDI 指令，
# 而是生成一个 .dd 触发文本文件写入打印服务器共享目录，由 Bartender 监视该目录自动打印。
#
# 文件内容格式（需求逐字确认，注意行尾为 CRLF）：
#   %BTW%                                                   # 第1行 固定
#   /AF="F:\Labels\Coil_Label.btw" /PRN="<打印机名>" /P /D="%Trigger File Name%" /C=1 /R=3  # 第2行：仅 /PRN 动态
#   %END%                                                   # 第3行 固定
#   CoilId|CoilIdString|Part|PartString|Length|LengthString|Unit|UnitString|IsInitial|IsInitialString|Lot|LotString|  # 第4行 标题行 固定
#   CoilId|<id>|Part|<part>|Length|<length>|Unit|<unit>|IsInitial|<1/0>|Lot|<lot>|   # 第5行起 每卷一行
#
# 编码：UTF-8 无 BOM（卷标内容为 ASCII 数字/字母/单位，Bartender 兼容；若未来出现中文值
#       需改 GBK 或与 Bartender 现场确认）。行尾：CRLF（Windows/Bartender 原生行尾）。

_BTW_OPTION_LINE_TMPL = '/AF="F:\\Labels\\Coil_Label.btw" /PRN="{printer}" /P /D="%Trigger File Name%" /C=1 /R=3'
_BTW_HEADER_LINE = 'CoilId|CoilIdString|Part|PartString|Length|LengthString|Unit|UnitString|IsInitial|IsInitialString|Lot|LotString|'


def _btw_bool(value) -> str:
    """is_initial_half → '1'/'0'（兼容 int 0/1、'0'/'1'、'true'/'yes' 等形态；缺失/空 → '0'）"""
    return '1' if str(value or '').strip().lower() in ('1', 'true', 'yes') else '0'


def _btw_length_text(value) -> str:
    """coil_length → 数值字符串：整数不带小数点、小数去掉尾 0（如 200.00→'200'、85.25→'85.25'）"""
    if value is None or value == '':
        return ''
    try:
        from decimal import Decimal, InvalidOperation
        text = format(Decimal(str(value)), 'f')
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text


def build_bartender_file(coils: list[dict], printer_name: str) -> str:
    """
    按 Bartender 触发文件（.dd）格式纯生成文件内容字符串（UTF-8 无 BOM，CRLF 行尾）。

    Args:
        coils: 卷标 dict 列表（原始 coil dict 即可，字段名与 kr_wire_coil 一致），
               每项须含 coil_id / part_number / coil_length / unit / is_initial_half / lot_no；
               lot_no / is_initial_half 缺失时给默认值（空 / 0），不抛错。
        printer_name: 站点打印机名（kr_site_printer.printer_name），写入 /PRN="..."。

    Returns:
        完整 .dd 文件内容字符串（第 1~3 行固定头 + 第 4 行标题 + N 行数据，末行带换行）。

    Raises:
        ValueError: coils 为空 / printer_name 为空 / 某卷缺少 coil_id（中文可读信息）。
    """
    printer = (printer_name or '').strip()
    if not printer:
        raise ValueError('未配置目标打印机（站点打印机表未配置，或请求未显式指定 printer）')
    if not coils:
        raise ValueError('没有需要打印的卷标')

    lines = [
        '%BTW%',
        _BTW_OPTION_LINE_TMPL.format(printer=printer),
        '%END%',
        _BTW_HEADER_LINE,
    ]
    for idx, coil in enumerate(coils, 1):
        coil_id = str(coil.get('coil_id') or '').strip()
        if not coil_id:
            raise ValueError(f'第 {idx} 卷缺少 coil_id，无法生成打印文件')
        part = str(coil.get('part_number') or '').strip()
        length = _btw_length_text(coil.get('coil_length'))
        unit = str(coil.get('unit') or '').strip()
        is_initial = _btw_bool(coil.get('is_initial_half'))
        lot = str(coil.get('lot_no') or '').strip()
        lines.append(
            f'CoilId|{coil_id}|Part|{part}|Length|{length}|'
            f'Unit|{unit}|IsInitial|{is_initial}|Lot|{lot}|'
        )
    return '\r\n'.join(lines) + '\r\n'


def _btw_write_error_text(err: Exception) -> str:
    """把写 .dd 触发文件过程的异常转成可读中文（保留原始错误便于排查）"""
    raw = str(err)
    if isinstance(err, ValueError):
        return raw  # build 阶段抛出的中文校验错误，直接透出
    winerror = getattr(err, 'winerror', None)
    if isinstance(err, PermissionError) or winerror == 5:
        return f'无权限写入打印共享目录（需在打印服务器侧开放共享写权限，WinError 5）：{raw}'
    if isinstance(err, NotADirectoryError) or winerror == 267:
        return f'打印共享目录不存在或不可访问（请检查 LABEL_PRINT_BTW_DIR）：{raw}'
    if winerror in (3, 53):
        return f'打印共享目录不可达（网络路径/共享名错误）：{raw}'
    if isinstance(err, OSError) and err.errno:
        return f'写入打印共享目录失败（errno={err.errno}）：{raw}'
    return f'生成 Bartender 触发文件失败：{raw}'


def write_bartender_file(coils: list[dict], printer_name: str, file_dir: str = None) -> dict:
    """
    Bartender 触发文件打印入口：组装内容 → 命名 .dd 文件 → 写入打印服务器共享目录。

    批量打印 N 卷只生成 1 个文件：第 4 行标题 + N 行数据；单卷同理 1 个文件 1 行数据。

    - 文件名：<首卷 coil_id>_<YYYYMMDDHHMMSS>.dd（多卷也以首卷 id 命名）
    - 目标目录：file_dir 优先；否则取 Config.LABEL_PRINT_BTW_DIR
      （默认 \\\\172.26.1.7\\Coil_Label_Scanned，可用环境变量 LABEL_PRINT_BTW_DIR 覆盖）
    - 编码：UTF-8 无 BOM；行尾：CRLF

    Args:
        coils: 卷标 dict 列表（原始 coil dict，字段含 coil_id/part_number/coil_length/
               unit/is_initial_half/lot_no；后两者缺失时给默认值 0/空）。
        printer_name: 站点打印机名（kr_site_printer.printer_name），如 \\\\172.26.1.7\\png-zt231-08。
        file_dir: 输出目录（None 用配置默认值；测试/调试可传入临时目录）。

    Returns:
        {'success': bool, 'message': 中文说明, 'path': 完整文件路径(成功)或 '',
         'count': 卷数, 'errors': [中文错误, ...]（失败时含原因）}
    """
    try:
        content = build_bartender_file(coils, printer_name)
        first_coil_id = str(coils[0].get('coil_id') or '').strip()
        filename = f"{first_coil_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.dd"
        target = (file_dir or Config.LABEL_PRINT_BTW_DIR or '').strip()
        if not target:
            raise ValueError('未配置 Bartender 打印共享目录（LABEL_PRINT_BTW_DIR）')
        if not os.path.isdir(target):
            raise NotADirectoryError(target)
        full_path = os.path.join(target, filename)
        with open(full_path, 'wb') as fh:
            fh.write(content.encode('utf-8'))
        logger.info(f"[BTW] 已写入 Bartender 触发文件 {full_path}（{len(coils)} 卷）")
        return {
            'success': True,
            'message': f'已生成 Bartender 打印文件（{len(coils)} 卷）',
            'path': full_path,
            'count': len(coils),
            'errors': [],
        }
    except Exception as e:
        logger.error(f"[BTW] 生成/写入 Bartender 触发文件失败: {e}")
        text = _btw_write_error_text(e)
        return {'success': False, 'message': text, 'path': '', 'count': 0, 'errors': [text]}


# ================= 打印机连接测试 / 测试页打印 =================

# win32print 打印机状态位（GetPrinter level2 -> Status），0 = PRINTER_STATUS_READY
_PRINTER_STATUS_BITS = (
    (0x00000001, '已暂停(PAUSED)'),
    (0x00000002, '出错(ERROR)'),
    (0x00000004, '删除中(PENDING_DELETION)'),
    (0x00000008, '卡纸(PAPER_JAM)'),
    (0x00000010, '缺纸(PAPER_OUT)'),
    (0x00000040, '纸张问题(PAPER_PROBLEM)'),
    (0x00000080, '脱机(OFFLINE)'),
    (0x00000100, 'I/O 活动中(IO_ACTIVE)'),
    (0x00000200, '忙(BUSY)'),
    (0x00000400, '打印中(PRINTING)'),
    (0x00000800, '输出盒满(OUTPUT_BIN_FULL)'),
    (0x00001000, '不可用(NOT_AVAILABLE)'),
    (0x00008000, '初始化中(INITIALIZING)'),
    (0x00010000, '预热中(WARMING_UP)'),
    (0x00020000, '耗材余量低(TONER_LOW)'),
    (0x00040000, '无耗材(NO_TONER)'),
    (0x00100000, '需人工干预(USER_INTERVENTION)'),
    (0x00400000, '盖门打开(DOOR_OPEN)'),
    (0x01000000, '省电(POWER_SAVE)'),
)


def _printer_status_text(status: int) -> str:
    """把 win32print 状态位转成可读文本"""
    if not status:
        return '就绪(READY)'
    parts = [name for bit, name in _PRINTER_STATUS_BITS if status & bit]
    return ', '.join(parts) if parts else f'状态码 {status}'


def _printer_probe_error(err: Exception) -> str:
    """把 win32print.OpenPrinter 抛出的 WinError 中文化（附原始错误便于排查）"""
    winerr = getattr(err, 'winerror', None)
    raw = str(err)
    if winerr == 1801:
        text = '找不到该打印机（打印机名无效：本地无此打印机或共享名不存在）'
    elif winerr == 2:
        text = '找不到打印机或共享名不可达'
    elif winerr == 1722:
        text = 'RPC 服务不可用（网络打印机不可达）'
    elif winerr == 5:
        text = '拒绝访问（无权限访问该打印机）'
    elif winerr == 53:
        text = '网络路径找不到（主机不可达或共享不存在）'
    elif winerr:
        text = f'无法连接打印机（WinError {winerr}）'
    else:
        text = '无法连接打印机'
    return f'{text}（原始错误: {raw}）'


def _open_printer_probe(printer_name: str) -> dict:
    """用 win32print.OpenPrinter 探测打印机：能打开即驱动/共享可达，并回读名称/状态"""
    if not _HAS_WIN32PRINT:
        return {
            'success': False,
            'message': '当前环境缺少 pywin32（win32print），无法探测 GDI/RAW 打印机',
            'detail': '未安装 pywin32',
        }
    h = None
    try:
        h = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(h, 2)
        except Exception as e:
            # 能打开句柄即认为可达；读不到详细信息不视为失败
            return {
                'success': True,
                'message': '打印机可达（未能读取详细信息）',
                'detail': f'OpenPrinter 成功，GetPrinter(level=2) 失败: {e}',
            }
        resolved = info.get('pPrinterName') or info.get('pName') or printer_name
        port = info.get('pPortName') or ''
        status = int(info.get('Status') or 0)
        return {
            'success': True,
            'message': f'打印机可达，状态：{_printer_status_text(status)}',
            'detail': f'pPrinterName={resolved!r}, port={port!r}, status_code={status}',
        }
    except Exception as e:
        return {'success': False, 'message': _printer_probe_error(e), 'detail': str(e)}
    finally:
        if h is not None:
            try:
                win32print.ClosePrinter(h)
            except Exception:
                pass


def _gateway_probe() -> dict:
    """探测打印网关可达性：GET 网关根路径，收到任意 HTTP 响应即视为服务连通"""
    url = (Config.LABEL_PRINT_GATEWAY_URL or '').strip().rstrip('/')
    if not url:
        return {
            'success': False,
            'message': '未配置 LABEL_PRINT_GATEWAY_URL',
            'detail': '',
        }
    import httpx
    try:
        resp = httpx.get(url, timeout=5.0, verify=False)
        return {
            'success': True,
            'message': f'打印网关可达（HTTP {resp.status_code}）',
            'detail': f'GET {url} -> {resp.status_code}',
        }
    except Exception as e:
        return {
            'success': False,
            'message': f'无法连接打印网关（{url}）',
            'detail': f'{type(e).__name__}: {e}',
        }


def test_printer_connection(printer_name: str, channel: str = 'gdi') -> dict:
    """
    探测打印机配置可达性（不实际打印）。

    Args:
        printer_name: 打印机名/共享路径（gateway 通道只关心网关可达性）
        channel: gdi / raw_zpl / raw_tspl / gateway

    Returns:
        {'success': bool, 'message': 中文说明, 'detail': 附加信息（成功时含名称/状态，失败时为原始错误）}
    """
    printer = (printer_name or '').strip()
    ch = (channel or 'gdi').lower()
    if not printer:
        return {'success': False, 'message': '打印机名称为空，请先填写', 'detail': 'printer_name 为空'}
    if ch not in ('gdi', 'raw_zpl', 'raw_tspl', 'gateway'):
        return {'success': False, 'message': f'未知打印通道: {ch}', 'detail': ''}
    if ch == 'gateway':
        return _gateway_probe()
    return _open_printer_probe(printer)


def print_test_page(printer_name: str = None, channel: str = None) -> dict:
    """
    按配置通道实际打印一张固定测试标签（不依赖数据库卷标）。

    内部组装最小测试卷并复用 print_labels 的 gdi/raw_zpl/raw_tspl/gateway 路径；
    送打前先做连接探测，给出中文化可读错误。

    Returns:
        {'success': bool, 'message': 中文说明, 'detail': ...,
         'printed': int, 'errors': [str, ...]}
    """
    printer = (printer_name or '').strip()
    ch = (channel or Config.LABEL_PRINT_CHANNEL or 'gdi').lower()
    if not printer:
        return {'success': False, 'message': '打印机名称为空，请先填写', 'detail': '', 'printed': 0, 'errors': []}
    if ch == 'gateway' and not (Config.LABEL_PRINT_GATEWAY_URL or '').strip():
        return {
            'success': False,
            'message': '通道为 gateway，但未配置 LABEL_PRINT_GATEWAY_URL',
            'detail': '', 'printed': 0, 'errors': [],
        }

    # 送打前先探测连接，失败直接返回中文可读错误（避免把 WinError 原文抛给前端）
    conn = test_printer_connection(printer, ch)
    if not conn.get('success'):
        return {
            'success': False,
            'message': '测试页未发送：' + conn.get('message', '连接探测失败'),
            'detail': conn.get('detail', ''),
            'printed': 0, 'errors': [],
        }

    fake_coil = {
        'coil_id': 'TESTPAGE',
        'part_number': 'TEST',
        'coil_length': 0,
        'unit': '',
        'is_initial_half': 0,
    }
    try:
        result = print_labels([fake_coil], printer, ch)
    except Exception as e:
        logger.error(f"[LABEL] 测试页打印异常: {e}")
        return {
            'success': False,
            'message': f'测试页打印异常：{e}',
            'detail': str(e), 'printed': 0, 'errors': [],
        }

    if result.get('success'):
        return {
            'success': True,
            'message': f"测试页已发送（{result.get('printed', 0)} 张）",
            'detail': '', 'printed': result.get('printed', 0), 'errors': [],
        }
    errors = result.get('errors') or ['未知打印错误']
    return {
        'success': False,
        'message': '测试页打印失败：' + errors[0],
        'detail': '; '.join(errors),
        'printed': result.get('printed', 0),
        'errors': errors,
    }
