"""操作审计日志（kr_operation_log）。

用途：记录「谁在什么时候对哪条业务记录做了什么处理、结果如何」，用于事后调查
（例如某条 CSI 接口记录被谁重发过、失败原因原文是什么、系统何时抢占了它）。

设计原则
--------
1. **绝不影响主业务**：独立短连接 + autocommit，不加入调用方事务；
   所有异常在内部吞掉，只 logger.warning，永不 raise。
2. **后台进程可用**：同步器（app/csi_sync_worker.py）没有 Flask request/session，
   直接调 log_operation()，IP 留空、operator 固定 SYSTEM。
3. **失败可追溯**：失败分支同样落日志（result='fail'），detail 里放平台/接口错误原文。

表结构说明：kr_operation_log 是历史「系统日志」表（request_id/operator/action/detail/
ip_address），本次增量扩展了 siteref/module/target_type/target_id/target_key/
operator_ip/result 列，旧功能不受影响；新写入同时回填 ip_address，保证旧的系统日志
页面也能看到来源 IP。
"""
import json
import logging

import pymysql
from pymysql.cursors import DictCursor

from app.config import Config

logger = logging.getLogger(__name__)

# 与表字段长度保持一致，超长截断（避免 DataError 把日志写丢）
MAX_SITEREF = 16
MAX_MODULE = 32
MAX_ACTION = 48
MAX_TARGET_TYPE = 32
MAX_TARGET_KEY = 128
MAX_OPERATOR = 64
MAX_IP = 64
MAX_RESULT = 16
MAX_DETAIL = 60000

_INSERT_SQL = (
    "INSERT INTO kr_operation_log "
    "(siteref, module, action, target_type, target_id, target_key, "
    " operator, operator_ip, ip_address, result, detail) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _clip(value, limit):
    text = '' if value is None else str(value)
    return text[:limit]


def _pack_detail(detail):
    """detail 支持 dict/list（JSON 序列化）或任意对象（str 降级）"""
    if detail is None:
        return None
    if isinstance(detail, (dict, list, tuple)):
        try:
            text = json.dumps(detail, ensure_ascii=False, default=str)
        except Exception:
            text = str(detail)
    else:
        text = str(detail)
    return text[:MAX_DETAIL]


def client_ip():
    """当前请求的客户端 IP；无请求上下文（后台进程/定时任务）返回空串"""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return ''
        ip = (request.remote_addr or '').strip()
        if ip:
            return ip[:MAX_IP]
        # 反向代理场景兜底（remote_addr 为空时取 X-Forwarded-For 首段）
        fwd = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
        return fwd[:MAX_IP]
    except Exception:
        return ''


def session_operator():
    """当前登录用户名；无 Flask 上下文或未登录返回 SYSTEM"""
    try:
        from flask import has_request_context, session
        if not has_request_context():
            return 'SYSTEM'
        user = session.get('user') or {}
        return (user.get('username') or user.get('display_name') or 'SYSTEM')[:MAX_OPERATOR]
    except Exception:
        return 'SYSTEM'


def log_operation(module, action, *, siteref='', target_type='', target_id=None,
                  target_key='', operator='SYSTEM', result='ok', detail=None,
                  operator_ip=None):
    """写一条审计日志。任何异常都必须吞掉，绝不影响主业务。

    Args:
        module: csi_interface / sync_worker / wire_count / external
        action: resend / cancel / submit / success / fail / count_adjust / count_gain
        siteref: 站点（记录本身的站点，不是操作人的站点）
        target_type/target_id: 目标表与主键（如 csi_interface / 12345）
        target_key: 业务单号（documentnumber / count_no / coil_id）
        operator: 操作人用户名；后台进程传 SYSTEM
        result: ok / fail
        detail: dict/list/str，存旧值→新值、错误原文等
        operator_ip: 不传则自动取 request.remote_addr（无上下文则空）

    Returns:
        True 写入成功 / False 失败（失败原因只记 warning，不抛异常）
    """
    try:
        ip = _clip(operator_ip if operator_ip is not None else client_ip(), MAX_IP)
        params = (
            _clip(siteref, MAX_SITEREF),
            _clip(module, MAX_MODULE),
            _clip(action, MAX_ACTION),
            _clip(target_type, MAX_TARGET_TYPE),
            int(target_id) if target_id not in (None, '') else None,
            _clip(target_key, MAX_TARGET_KEY),
            _clip(operator, MAX_OPERATOR) or 'SYSTEM',
            ip,
            ip or None,          # 旧系统日志页面读 ip_address，顺带回填
            _clip(result, MAX_RESULT) or 'ok',
            _pack_detail(detail),
        )
        conn = pymysql.connect(
            host=Config.MYSQL_HOST,
            port=Config.MYSQL_PORT,
            user=Config.MYSQL_USER,
            password=Config.MYSQL_PASSWORD,
            database=Config.MYSQL_DB,
            charset='utf8mb4',
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, params)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception as e:                       # noqa: BLE001 - 审计日志绝不能影响业务
        logger.warning('写操作日志失败（已忽略）module=%s action=%s target=%s/%s: %s',
                       module, action, target_type, target_id, e)
        return False


def log_from_request(module, action, **kw):
    """在 Flask 请求里记录日志：自动补 operator（当前登录用户）与 operator_ip。

    用法：
        log_from_request('csi_interface', 'resend', siteref=..., target_id=...,
                         target_key=..., result='ok', detail={...})
    """
    kw.setdefault('operator', session_operator())
    kw.setdefault('operator_ip', client_ip())
    if not kw.get('operator'):
        kw['operator'] = 'SYSTEM'
    return log_operation(module, action, **kw)
