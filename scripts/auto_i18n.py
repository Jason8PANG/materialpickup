"""
批量添加 data-i18n 属性到模板文件
"""
import re, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

TEMPLATES_DIR = 'D:/Workbuddy/多智能体/物料领取看板/app/templates/'
chinese = re.compile(r'[\u4e00-\u9fff]+')

PHRASE_MAP = {
    '加载中': 'common.loading', '提交中': 'form.submitting',
    '查询中': 'common.fetching', '验证中': 'common.validating',
    '已获取': 'common.fetched', '查询失败': 'common.fetch_failed',
    '暂无数据': 'common.no_data', '暂无明细': 'detail.no_items',
    '无数据': 'common.no_data', '操作成功': 'common.success',
    '操作失败': 'common.error', '提交成功': 'common.submit_success',
    '提交失败': 'common.submit_failed', '保存成功': 'common.success',
    '保存失败': 'common.save_failed', '保存': 'common.save',
    '取消': 'common.cancel', '确认': 'common.confirm',
    '确定': 'action.ok', '查询': 'common.search', '搜索': 'common.search',
    '刷新': 'kanban.refresh', '全屏': 'kanban.fullscreen',
    '退出全屏': 'kanban.exit_fullscreen',
    '物料明细': 'detail.items', '操作日志': 'detail.logs',
    '审批意见': 'detail.approve_comment',
    '申请人': 'detail.requester', '申请时间': 'detail.time',
    '审批人': 'detail.supervisor', '审批时间': 'detail.approve_time',
    '备料员': 'detail.warehouse_operator',
    '缺料原因': 'detail.short_reason', '缺料时间': 'detail.short_time',
    '签字人': 'detail.signer', '签字图片': 'detail.signature_img',
    '备注': 'detail.remark',
    '操作': 'admin.col_actions', '状态': 'admin.col_status',
    '工单号': 'form.job_order', '工单号码': 'form.job_order',
    '数量': 'form.quantity', '单价': 'form.price',
    '总金额': 'form.total_amount', '库存量': 'form.stock_qty',
    '库存位置': 'form.stock_loc', '库存': 'form.stock_qty',
    '补料原因': 'form.replenish_reason', '批次号': 'detail.batch_no',
    '物料': 'form.part_number',
    '站点': 'admin.col_site', '域账号': 'admin.col_domain',
    '显示名称': 'admin.col_display', '角色': 'admin.col_role',
    '所属站点': 'admin.col_site', '邮箱': 'admin.col_email',
    '急料': 'kanban.urgent',
    '添加物料': 'form.add_item', '添加行': 'form.add_row',
    '输入Part Number': 'form.enter_part',
    '自动获取': 'form.auto_fetch_short',
    '验证后自动填充': 'form.auto_fetch',
    '删除此行': 'form.delete_row',
    '其他原因说明': 'form.reason_other',
    '报废': 'form.scrap', '不良': 'form.defective',
    '来料不足': 'form.short_supply', '其他': 'form.other',
    '请选择': 'form.please_select',
    '全部状态': 'history.all_status', '已完成': 'history.completed',
    '已驳回': 'history.rejected',
    '待审批': 'status.pending_approval', '待备料': 'status.pending_prep',
    '备料中': 'status.prepping', '缺料': 'status.short',
    '待取料': 'status.ready_pickup',
    '全部站点': 'admin.all_sites', '全部角色': 'admin.all_roles',
    '管理员': 'admin.role_admin', '领料员': 'admin.role_requester',
    '主管': 'admin.role_supervisor', '仓库': 'admin.role_warehouse',
    '搜索工单号': 'pending.search_job',
    '申请单': 'approve.request_no',
    '批准通过': 'approve.approve_btn', '驳回': 'action.reject',
    '审批通过': 'action.approve', '开始备料': 'action.start_prep',
    '完成备料': 'action.complete_prep', '缺料登记': 'action.short',
    '转为待取料': 'action.restore_from_short',
    '签字确认': 'action.sign', '签字取料': 'action.sign',
    '取消申请': 'action.cancel', '指定备料员': 'action.assign_worker',
    '已指定': 'detail.worker_assigned', '操作确认': 'action.confirm',
    '签字板': 'detail.sign_title', '清除': 'detail.sign_clear',
    '确认签字': 'detail.sign_confirm',
    '已批准通过': 'approve.success_approve',
    '已驳回该申请': 'approve.desc_reject',
    '操作未完成': 'approve.fail',
    '令牌无效或已过期': 'approve.invalid_token',
    '创建时间起': 'records.date_from', '创建时间止': 'records.date_to',
    '创建时间': 'form.time',
    '导出CSV': 'records.export_csv', '导出': 'records.export_csv',
    '正在导出': 'records.exporting', '导出成功': 'records.export_success',
    '导出失败': 'records.export_failed',
    '无数据可导出': 'records.no_export_data',
    '取料人': 'detail.signer', '取料时间': 'detail.sign_time',
    '备料人': 'detail.warehouse_operator',
    '新增账号': 'admin.modal_add', '启用': 'admin.status_active',
    '禁用': 'admin.status_disabled',
    '管理员可选': 'admin.site_admin_hint',
    '请选择站点': 'admin.site_placeholder',
    '请输入用户名': 'login.username', '请输入密码': 'login.password',
}

files_to_fix = [
    'request_form.html', 'minpack_request_form.html', 'pending_list.html',
    'history.html', 'records.html', 'request_detail.html', 'kanban.html',
    'approval_email.html', 'admin_mappings.html', 'login.html',
    'public_kanban.html'
]

for fname in files_to_fix:
    fpath = os.path.join(TEMPLATES_DIR, fname)
    if not os.path.exists(fpath):
        continue
    with open(fpath, 'r', encoding='utf-8') as f:
        content = f.read()
    original = content
    
    tag_pattern = re.compile(
        r'(<(?:th|td|label|h[1-6]|span|p|option|button|a|div|small|strong|li)'
        r'(?:\s[^>]*?)?>)([^<]{1,80})(</\w+?>)'
    )
    
    for match in tag_pattern.finditer(content):
        open_tag = match.group(1)
        text = match.group(2)
        close_tag = match.group(3)
        full = match.group(0)
        if 'data-i18n' in open_tag:
            continue
        if '{{' in text or '{%' in text:
            continue
        if not chinese.search(text):
            continue
        
        clean = text.strip()
        matched_key = None
        for phrase, key in PHRASE_MAP.items():
            if phrase in clean:
                matched_key = key
                break
        
        if matched_key:
            new_open = open_tag.rstrip('>') + f' data-i18n="{matched_key}">'
            content = content.replace(full, new_open + text + close_tag, 1)
    
    if content != original:
        cnt = content.count('data-i18n=') - original.count('data-i18n=')
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'{fname}: +{cnt} data-i18n')
    else:
        print(f'{fname}: no change')

print('Done!')
