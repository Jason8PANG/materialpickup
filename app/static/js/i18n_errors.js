/**
 * 为所有 showToast 和 showError 调用自动添加翻译
 * 拦截 apiGet/apiPost 的通用 onError，使错误信息显示英文时用 i18n 映射
 */
// 错误消息 i18n 映射表（中文→i18n key）
const ERROR_I18N_MAP = {
    '未登录': __('nav.login'),
    '操作成功': __('common.success'),
    '操作失败': __('common.error'),
    '提交成功': __('common.submit_success'),
    '提交失败': __('common.submit_failed'),
    '保存成功': __('common.success'),
    '保存失败': __('common.save_failed'),
    '查询失败': __('common.error'),
    '加载失败': __('common.error'),
    '提交中...': __('form.submitting'),
    '登录中...': __('login.logging'),
};
