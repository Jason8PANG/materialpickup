"""邮件发送日志（kr_email_log）。

用途：所有经 app.services.email_service.send_email 发出的邮件（含因 SMTP
未配置而跳过、以及发送失败的情况）在 MySQL 留一条记录，供 /admin/email-log
页面与 /api/admin/email-log 查询，便于排查「提醒/告警邮件到底发没发、发给谁、
内容是什么、为什么失败」。

设计原则（与 operation_log.py 一致）
----------------------------------
1. **绝不影响发信**：独立短连接 + autocommit，整段 try/except 吞异常，
   失败只 logger.warning，永不 raise。
2. **三种结果都落库**：ok（发送成功）/ fail（异常）/ skip（SMTP 未配置）。
3. **正文摘要**：去 HTML 标签 + 反转义 + 压缩空白，截前 2000 字，只存纯文本。
"""
import html as html_lib
import logging
import re

import pymysql
from pymysql.cursors import DictCursor

from app.config import Config

logger = logging.getLogger(__name__)

# 与表字段长度保持一致，超长截断（避免 DataError 把日志写丢）
MAX_MODULE = 32
MAX_SUBJECT = 512
MAX_SITEREF = 16
MAX_RELATED_TYPE = 32
MAX_RELATED_KEY = 128
MAX_RESULT = 16
MAX_ERROR = 500
MAX_PREVIEW = 2000

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kr_email_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  module VARCHAR(32) NOT NULL DEFAULT ''       COMMENT '来源模块: csi_alert/request_notify/approval/csi_alert_scheduler/manual',
  subject VARCHAR(512) NOT NULL DEFAULT '',
  to_emails TEXT                               COMMENT '收件人，逗号分隔',
  siteref VARCHAR(16) NOT NULL DEFAULT '',
  related_type VARCHAR(32) NOT NULL DEFAULT '' COMMENT '关联对象类型 csi_interface/material_request',
  related_id BIGINT NULL,
  related_key VARCHAR(128) NOT NULL DEFAULT '' COMMENT '关联单号 documentnumber/request_id',
  result VARCHAR(16) NOT NULL DEFAULT 'ok'     COMMENT 'ok/fail/skip',
  errormessage VARCHAR(500) NOT NULL DEFAULT '',
  body_preview TEXT                            COMMENT '正文摘要（去标签纯文本，前 2000 字）',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_time (created_at),
  KEY idx_module (module),
  KEY idx_related (related_type, related_id),
  KEY idx_result (result)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邮件发送日志'
"""

_INSERT_SQL = (
    "INSERT INTO kr_email_log "
    "(module, subject, to_emails, siteref, related_type, related_id, related_key, "
    " result, errormessage, body_preview) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')

# 建表只尝试一次（失败也吞掉，不拖慢每次发信）
_schema_ready = False


def _connect():
    return pymysql.connect(
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


def ensure_schema():
    """幂等建表；返回 True/False（异常吞掉，不影响调用方）"""
    global _schema_ready
    if _schema_ready:
        return True
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        _schema_ready = True
        return True
    except Exception as e:                       # noqa: BLE001
        logger.warning('邮件日志建表失败（已忽略）: %s', e)
        return False


def _clip(value, limit):
    text = '' if value is None else str(value)
    return text[:limit]


def html_to_text(html_content, limit=MAX_PREVIEW):
    """HTML 正文 → 纯文本摘要：去标签、反转义、压缩空白、截断"""
    if html_content is None:
        return ''
    text = _TAG_RE.sub(' ', str(html_content))
    try:
        text = html_lib.unescape(text)
    except Exception:
        pass
    text = _WS_RE.sub(' ', text).strip()
    return text[:limit]


def log_email(module='', subject='', to_emails='', siteref='', related_type='',
              related_id=None, related_key='', result='ok', errormessage='',
              html_content=''):
    """写一条邮件发送日志。任何异常都吞掉，绝不打断发信流程。

    Args:
        module: csi_alert / request_notify / approval / csi_alert_scheduler / manual
        subject: 邮件主题
        to_emails: 收件人（逗号分隔）
        siteref: 站点 310/410
        related_type / related_id / related_key: 关联对象（csi_interface/material_request）
        result: ok / fail / skip
        errormessage: 失败原因（异常原文 / SMTP 未配置）
        html_content: 邮件 HTML 正文，内部转纯文本摘要存 body_preview

    Returns:
        True 写入成功 / False 失败（失败原因只记 warning）
    """
    try:
        if not _schema_ready:
            ensure_schema()
        try:
            rid = int(related_id) if related_id not in (None, '') else None
        except (TypeError, ValueError):
            rid = None
        params = (
            _clip(module, MAX_MODULE),
            _clip(subject, MAX_SUBJECT),
            str(to_emails or ''),
            _clip(siteref, MAX_SITEREF),
            _clip(related_type, MAX_RELATED_TYPE),
            rid,
            _clip(related_key, MAX_RELATED_KEY),
            _clip(result, MAX_RESULT) or 'ok',
            _clip(errormessage, MAX_ERROR),
            html_to_text(html_content),
        )
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, params)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception as e:                       # noqa: BLE001 - 日志绝不能影响发信
        logger.warning('写邮件日志失败（已忽略）module=%s subject=%s to=%s: %s',
                       module, subject, to_emails, e)
        return False
