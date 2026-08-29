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

标签版式（3 inch × 1 inch = 75mm × 25mm，迭代需求 C.7）：
  ┌────────────────────────────────────────────┐
  │ 卷号条码(Code128)           Part Number     │
  │ ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆          LEN: 250.50 M    │
  │ 260814001  ← 条码下方显示卷号ID               │
  └────────────────────────────────────────────┘
"""
import io
import logging
import threading

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
LABEL_WIDTH_MM = 75
LABEL_HEIGHT_MM = 25

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

        return {
            'coil_id': coil_id,
            'part_number': part_number,
            'length': length,
            'unit': unit,
            'length_text': length_text,
            'barcode_coil': coil_id,
            'barcode_part': part_number,
        }


# ================= ZPL / TSPL 指令构建 =================

def build_zpl(label: dict) -> bytes:
    """
    构建 Zebra ZPL（203dpi，3" 宽 ≈ 609 dot，1" 高 ≈ 203 dot）。
    条码由打印机固件生成，Code128。
    版式（C.7）：顶部条码 → 条码下方显示卷号ID → Item → Length + 单位，全部左对齐竖排。
    """
    coil_id = _clean_fd(label['barcode_coil'])
    part = _clean_fd(label['barcode_part'])
    length_text = _clean_fd(label['length_text'])

    zpl = (
        "^XA\n"
        "^PW609^LL203^LH0,0\n"
        f"^FO20,15^BCN,45,Y,N,N^FD{coil_id}^FS\n"        # 顶部条码 Code128（紧凑）
        f"^FO20,75^A0N,26,26^FD{coil_id}^FS\n"          # 条码正下方：卷号文字
        f"^FO20,115^A0N,24,24^FDPart : {part}^FS\n"       # Part（按参考图：冒号前空格）
        f"^FO20,150^A0N,24,24^FDLenght :{length_text}^FS\n"  # Lenght（按参考图）
        "^XZ\n"
    )
    return zpl.encode('utf-8')


def build_tspl(label: dict) -> bytes:
    """
    构建 TSC TSPL（75mm × 25mm）。
    条码由打印机固件生成，Code128。
    版式（C.7）：顶部条码 → 条码下方显示卷号ID → Part → Lenght + 单位，全部左对齐竖排。
    """
    coil_id = _clean_fd(label['barcode_coil'])
    part = _clean_fd(label['barcode_part'])
    length_text = _clean_fd(label['length_text'])

    tsp = (
        "SIZE 75 mm,25 mm\n"
        "GAP 2 mm,0\n"
        "CLS\n"
        f'BARCODE 20,15,"128",45,1,0,2,2,"{coil_id}"\n'   # 顶部条码 Code128（紧凑）
        f'TEXT 20,70,"TSS24.BF2",0,2,2,"{coil_id}"\n'    # 条码正下方：卷号文字
        f'TEXT 20,110,"TSS24.BF2",0,2,2,"Part : {part}"\n'  # Part
        f'TEXT 20,150,"TSS24.BF2",0,2,2,"Lenght :{length_text}"\n'  # Lenght
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
    """通过 Windows 打印驱动 GDI 绘制标签（75mm × 25mm）"""
    if not _HAS_WIN32UI:
        raise RuntimeError('当前环境缺少 pywin32（win32ui），无法 GDI 打印')
    if not _HAS_PIL:
        raise RuntimeError('当前环境缺少 Pillow，无法 GDI 打印条码')

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer_name)

    # 尝试设置自定义纸型 75mm×25mm（best-effort，失败则用驱动默认纸型）
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

        # 字体（标题 = 卷号/Part、正文 = Lenght）
        title_font = win32ui.CreateFont({
            'name': 'Arial', 'height': mm_y(3.2), 'weight': 700, 'charset': 1,
        })
        text_font = win32ui.CreateFont({
            'name': 'Arial', 'height': mm_y(2.6), 'weight': 400, 'charset': 1,
        })

        coil_id = label['barcode_coil']
        part = label['barcode_part']

        # 版式（左对齐竖排，与前端预览/ZPL 一致）：
        #   顶部条码 → 条码下方卷号 → Part : xxx → Lenght : xxx
        # 第1行：顶部 Code128 条码（紧凑，高度约 9mm，左对齐占 2~50mm 宽）
        barcode_top = mm_y(2)
        barcode_height = mm_y(9)
        barcode_w = mm_x(48)
        img1 = _barcode_image(coil_id)

        if img1:
            # draw(hdc, destRect, srcRect)：win32ui 的 Dib.draw 需 3 个参数（新版 pywin32 强制）
            ImageWin.Dib(img1).draw(dc.GetHandleOutput(),
                                    (mm_x(2), barcode_top, mm_x(2) + barcode_w, barcode_top + barcode_height),
                                    (0, 0, img1.size[0], img1.size[1]))
        else:
            dc.FillRect((mm_x(2), barcode_top, mm_x(2) + barcode_w, barcode_top + barcode_height),
                        black_brush)

        # 第2行：条码正下方显示卷号ID（加粗）
        dc.SelectObject(title_font)
        dc.TextOut(mm_x(2), mm_y(12), f'{coil_id}')

        # 第3行：Part
        dc.SelectObject(text_font)
        dc.TextOut(mm_x(2), mm_y(17), f'Part : {part}')

        # 第4行：Lenght
        dc.TextOut(mm_x(2), mm_y(21), f'Lenght :{label["length_text"]}')

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

def print_labels(coils: list[dict], printer_name: str = None) -> dict:
    """
    打印一批线卷标签。

    Args:
        coils: 线卷记录 dict 列表，每项含 coil_id / part_number / coil_length / unit
        printer_name: 目标打印机；为空则取 Config.LABEL_PRINTER_NAME 或系统默认打印机

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
    channel = (Config.LABEL_PRINT_CHANNEL or 'gdi').lower()

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
