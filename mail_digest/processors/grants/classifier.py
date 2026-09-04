"""grants 域邮件识别：基金/项目申报通知（processor.matches）。

按主题关键词识别，不看发件人；可信发件人白名单在 processor 层强制执行。
"""
from __future__ import annotations

from ...core.models import Mail

# ---------- 场景二：基金/项目申报通知 ----------
# 强信号词：命中任一即视为基金/项目申报类
_GRANT_STRONG = (
    "申报指南", "申报通知", "组织申报", "项目申报", "基金",
    "重点研发计划", "科技计划项目", "专项项目", "立项", "揭榜挂帅",
    "申报意向", "申报工作", "申请指南", "项目指南",
)
# 弱信号：需与"项目/指南/需求/专项"搭配
_GRANT_WEAK = ("征集", "指南", "项目", "通知", "通告")
# 排除词：命中任一即不是项目申报机会
_GRANT_EXCLUDE = (
    "考核", "培训", "讲座", "论坛", "会议通知", "报销", "体检",
    "项目申报机会清单", "ADS 文献简报", "mail-digest",  # 自推送邮件排除（防循环）
    "安全", "党建", "消防", "专家库", "专家征集", "专家", "答辩", "评审结果",
    "立项结果", "结题", "验收", "汇报", "开题", "中期检查", "调研邀请",
)


def is_grant_email(mail: Mail) -> bool:
    """按主题关键词识别基金/项目申报通知（不看发件人）。"""
    subj = mail.subject or ""
    if len(subj) < 4:
        return False
    if any(w in subj for w in _GRANT_EXCLUDE):
        return False
    if any(w in subj for w in _GRANT_STRONG):
        return True
    # 弱信号：征集/指南 + 项目语境
    hits = sum(1 for w in _GRANT_WEAK if w in subj)
    return hits >= 2


