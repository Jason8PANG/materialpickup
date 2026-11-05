"""守护用例：测试进程绝不发真实邮件。

背景（事故）：csi_alert_production_semantics（autouse）把 Config.CSI_ALERT_ENABLED
强制为 True，个别用例漏了 patch('...send_email') 就真的通过 SMTP 把「单号2967」
失败告警发到了 jason.pang@nai-group.com。conftest 的 _no_real_email（autouse）
在所有模块入口统一替换为哨兵，本文件负责守住这条底线。
"""
from unittest.mock import patch

from app import csi_sync_worker as w
from app.config import Config


def _row():
    return {
        'id': 1,
        'api_name': w.API_QTY_MOVE,
        'request_id': 2967,
        'siteRef': 410,
        'item': 'A000871',
        'lot': '202510210000006',
        'quantity': 6.5,
        'warehouse': 'M1M1',
        'from_location': 'A-01-01',
        'to_location': 'Floor-Core',
        'unitcode1': 'FT',
        'documentnumber': 'MP2967',
        'traceid': 'MP2967-261016004-ce830e42a267',
    }


def test_tests_never_send_real_email(_no_real_email):
    """即使 CSI_ALERT_ENABLED=True，测试进程也不得真的发信。"""
    # 前置条件：本用例运行在「等价生产」语义下（否则告警会被业务守卫提前拦掉，测不到东西）
    assert Config.CSI_ALERT_ENABLED is True

    with patch('smtplib.SMTP') as smtp:
        w._send_failure_alert(_row(), 1, '测试：验证不触网')

        # 关键断言：绝无 SMTP 连接
        smtp.assert_not_called()

    # 哨兵收到调用（若业务守卫仍拦截则为 0，两种都接受）
    assert len(_no_real_email) <= 1
    if _no_real_email:
        call = _no_real_email[-1]
        assert '单号2967' in call['subject']
        assert call['kwargs'].get('module') == 'csi_alert'


def test_email_service_entry_is_patched(_no_real_email):
    """源头 app.services.email_service.send_email 也必须被替换为哨兵。"""
    from app.services import email_service

    assert email_service.send_email('a@b.com', 's', '<p>x</p>') is True
    assert _no_real_email[-1]['to'] == 'a@b.com'
