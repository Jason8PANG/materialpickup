"""CSI 失败记录定时邮件调度器（每天 09:00 / 12:00 / 15:00 / 17:00）。

运行方式
--------
常驻：CSI_ALERT_ENABLED=true（默认）时由 create_app() 起守护线程（见 app/__init__.py）
单次：python -m app.csi_alert_scheduler --once 09:00            # 跳过时间判定，但走幂等
      python -m app.csi_alert_scheduler --once 09:00 --force    # 忽略幂等（调试/验证用）
初始化：python -m app.csi_alert_scheduler --init                # 建表 + 灌默认规则

并发（重要）
-----------
部署是 gunicorn -w 4 → create_app() 执行 4 次 → 本调度线程起 4 份。
内存里的 _done 只能防止单进程内同一时段重复触发；跨进程互斥靠
kr_csi_alert_log 的 UNIQUE KEY (send_date, slot, siteref, api_name)：
只有 INSERT IGNORE 成功（rowcount=1）的那份 worker 发信，其余直接跳过。
"""
import logging
import sys
import time
from datetime import date, datetime, timedelta

from app.config import Config
from app.services.csi_alert_service import ensure_schema, send_alert

logger = logging.getLogger(__name__)

ALERT_SLOTS = ('09:00', '12:00', '15:00', '17:00')
# 命中窗口：slot 起 N 分钟内触发（默认 5 分钟，覆盖 30 秒轮询的抖动/跨分钟）
ALERT_WINDOW_MINUTES = int(getattr(Config, 'CSI_ALERT_WINDOW_MINUTES', 5) or 5)
CHECK_SECONDS = int(getattr(Config, 'CSI_ALERT_CHECK_SECONDS', 30) or 30)


def _hits(now, slot, window_minutes=None):
    """now 是否落在 slot 起 window_minutes 分钟内"""
    window = ALERT_WINDOW_MINUTES if window_minutes is None else window_minutes
    try:
        hour, minute = (int(x) for x in str(slot).split(':'))
    except (TypeError, ValueError):
        return False
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= now < start + timedelta(minutes=window)


def hit_slot(now=None):
    """返回当前命中的时段（'09:00' 等），未命中返回 None"""
    now = now or datetime.now()
    for slot in ALERT_SLOTS:
        if _hits(now, slot):
            return slot
    return None


# 兼容旧名（验证脚本/人工调试用）
_hit_slot = hit_slot


def run_slot(slot, force=False, send_date=None, now=None):
    """执行一个时段的发送（调用方负责时间判定）"""
    ensure_schema()
    summary = send_alert(slot, send_date=send_date, force=force, now=now)
    logger.info('CSI 告警时段 %s 完成：记录 %s 条，分组 %s 个 → %s',
                slot, summary['total_rows'], len(summary['groups']),
                ', '.join(f"{g['siteref']}/{g['api_name']}:{g['status']}({g['count']})"
                          for g in summary['groups']) or '无失败记录')
    return summary


def _ensure_logging():
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        )


def main():
    """常驻循环：每 CHECK_SECONDS 秒看一眼是否命中时段；顶层异常不退出"""
    _ensure_logging()
    if not Config.CSI_ALERT_ENABLED:
        logger.warning('CSI_ALERT_ENABLED=false，CSI 失败邮件调度器不启动')
        return

    try:
        info = ensure_schema()
        logger.info('CSI 失败邮件调度器启动：slots=%s window=%smin 检查间隔=%ss 规则共 %s 条',
                    '/'.join(ALERT_SLOTS), ALERT_WINDOW_MINUTES, CHECK_SECONDS, info['rules_total'])
    except Exception as e:                          # 建表失败也要继续跑（下轮再试）
        logger.exception('CSI 告警：初始化建表失败（继续运行）：%s', e)

    done = set()                                    # {(date, slot)}：仅进程内去重
    while True:
        try:
            now = datetime.now()
            slot = hit_slot(now)
            key = (now.date(), slot)
            if slot and key not in done:
                done.add(key)                       # 先标记：本进程不重复触发
                run_slot(slot, send_date=now.date(), now=now)
            # 清掉昨天的标记，避免常驻数周后集合膨胀
            done = {k for k in done if k[0] >= now.date()}
        except Exception as e:                      # 任何异常都不能让线程死掉
            logger.exception('CSI 告警：调度循环异常，%ss 后继续：%s', CHECK_SECONDS, e)
        try:
            time.sleep(CHECK_SECONDS)
        except Exception:
            break


def _cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    force = '--force' in argv

    if '--init' in argv:
        print(ensure_schema())
        return 0

    if '--once' in argv:
        idx = argv.index('--once')
        slot = argv[idx + 1] if len(argv) > idx + 1 and not argv[idx + 1].startswith('--') else ALERT_SLOTS[0]
        _ensure_logging()
        summary = run_slot(slot, force=force)
        print(f"时段 {summary['slot']} {summary['send_date']}：失败记录 {summary['total_rows']} 条")
        for g in summary['groups']:
            print(f"  {g['siteref']}/{g['api_name']} → {g['status']} count={g['count']} "
                  f"emails={g.get('emails', '')} subject={g.get('subject', '')}")
            if g.get('error'):
                print(f"    error={g['error']}")
        return 0

    main()
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
