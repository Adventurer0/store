from app import platform_feedback as feedback


def test_keyword_translation_keeps_original_code():
    text, code = feedback.translate('kuaishou', '40021', '类目属性不匹配')
    assert '类目' in text and '40021' in text
    assert code == '40021'
    text, code = feedback.translate('wechat', '', '品牌授权未通过')
    assert '品牌授权' in text


def test_unknown_message_is_bounded_and_not_fabricated():
    text, code = feedback.translate('douyin', 'x1', 'unexpected')
    assert 'unexpected' in text and 'x1' in text
    assert code == 'x1'


def test_audit_detail_classifies_status_and_code():
    status, message, code = feedback.audit_detail('商品审核中\n错误码：A123')
    assert status == 'pending' and '审核中' in message and code == 'A123'
    assert feedback.audit_detail('审核驳回：图片不合规')[0] == 'rejected'
    assert feedback.audit_detail('审核通过')[0] == 'approved'
    assert feedback.audit_detail('已下架')[0] == 'offline'
    assert feedback.audit_detail('无状态文本')[0] == 'unknown'
