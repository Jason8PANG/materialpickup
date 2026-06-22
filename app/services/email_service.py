"""
邮件服务 - SMTP 方式发送邮件
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from app.config import Config


def send_email(to_email, subject, html_content):
    """发送邮件"""
    if not Config.SMTP_USER or not Config.SMTP_PASSWORD:
        print(f"[邮件] SMTP 未配置，跳过发送: {subject} -> {to_email}")
        print(f"[邮件] 内容预览: {html_content[:200]}...")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = f"{Header(Config.MAIL_FROM_NAME, 'utf-8').encode()} <{Config.MAIL_FROM}>"
        msg['To'] = to_email
        msg['Subject'] = Header(subject, 'utf-8').encode()

        part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(part)

        server = smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT)
        if Config.SMTP_USE_TLS:
            server.starttls()
        server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
        server.sendmail(Config.MAIL_FROM, [to_email], msg.as_string())
        server.quit()

        print(f"[EMAIL] Sent OK: {subject} -> {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL] Send failed: {e}")
        return False


def send_approval_email(request_info, approve_url, reject_url):
    """
    发送审批通知邮件（含所有物料明细）
    """
    items = request_info.get('items', [])
    total_amount = request_info.get('total_amount', 0)
    site_name = {'310': '苏州工厂', '410': '槟城工厂'}.get(request_info.get('siteref', ''), request_info.get('siteref', ''))
    
    subject = f"[领料审批] {site_name} - {request_info['requester']} #{request_info['request_id']} 总金额:{total_amount}"
    
    # 生成明细表格行
    item_rows = ''
    for i, it in enumerate(items):
        item_rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #e8e8e8;text-align:center;">{i+1}</td>
            <td style="padding:8px;border:1px solid #e8e8e8;">{it.get('job_order', '')}</td>
            <td style="padding:8px;border:1px solid #e8e8e8;">{it.get('part_number', '')}</td>
            <td style="padding:8px;border:1px solid #e8e8e8;text-align:right;">{it.get('quantity', '')}</td>
            <td style="padding:8px;border:1px solid #e8e8e8;text-align:right;">{it.get('price', '-')}</td>
        </tr>"""
    
    html = f"""
    <div style="max-width:640px;margin:0 auto;font-family:Arial,sans-serif;padding:20px;background:#e3f2fd;">
        <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;padding:24px;border-radius:8px 8px 0 0;text-align:center;">
            <h2 style="margin:0 0 4px;">物料领料审批通知</h2>
            <p style="margin:0;opacity:0.9;font-size:14px;">{site_name} | 申请单 #{request_info['request_id']}</p>
        </div>
        <div style="background:#fff;padding:24px;border-radius:0 0 8px 8px;">
            <p style="margin:0 0 16px;color:#333;">您好，<strong>{request_info['requester']}</strong> 提交了一笔领料申请，共 <strong>{len(items)} 项</strong>物料，请审批：</p>
            
            <table style="width:100%;border-collapse:collapse;margin:0 0 16px;font-size:13px;">
                <thead>
                    <tr style="background:#f5f7fa;">
                        <th style="padding:8px;border:1px solid #e8e8e8;text-align:center;width:32px;">#</th>
                        <th style="padding:8px;border:1px solid #e8e8e8;text-align:left;">工单号</th>
                        <th style="padding:8px;border:1px solid #e8e8e8;text-align:left;">Part Number</th>
                        <th style="padding:8px;border:1px solid #e8e8e8;text-align:right;">数量</th>
                        <th style="padding:8px;border:1px solid #e8e8e8;text-align:right;">单价</th>
                    </tr>
                </thead>
                <tbody>
                    {item_rows}
                </tbody>
            </table>
            
            <p style="color:#999;font-size:12px;margin:0 0 20px;">
                <strong>备注：</strong>{request_info.get('remark', '无')}
            </p>
            
            <div style="text-align:center;margin:24px 0;">
                <a href="{approve_url}" style="display:inline-block;background:#67c23a;color:#fff;padding:12px 36px;border-radius:6px;text-decoration:none;font-size:15px;margin:0 6px;font-weight:600;">批准通过</a>
                <a href="{reject_url}" style="display:inline-block;background:#f56c6c;color:#fff;padding:12px 36px;border-radius:6px;text-decoration:none;font-size:15px;margin:0 6px;font-weight:600;">驳回</a>
            </div>
            <p style="color:#bbb;font-size:11px;text-align:center;margin:0;">此链接72小时内有效，请及时处理 | 物料领取看板系统</p>
        </div>
    </div>
    """
    
    return send_email(request_info.get('supervisor_email', ''), subject, html)
