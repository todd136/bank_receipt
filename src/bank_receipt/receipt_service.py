"""
银行回单解析：先按表格/词块分行，再按 bank_templates.json 识别银行并提取字段。
运行时优先读取 base_path/bank_templates.json（与回单目录并列，便于用户编辑）；
若不存在则使用代码内嵌的默认模板。仓库仅在项目根目录维护 bank_templates.json。
JSON 仅使用标签名与关键词；正则仅在代码内由标签生成（见 bank_fields）。
"""
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import pdfplumber

from .bank_fields import label_flex_pattern, pick_scoped_text
from .bank_profile_service import BankProfile, _normalize_layout, bank_profiles_for_base, detect_bank
from .receipt import Receipt
from .receipt_layout import build_page_scoped_texts
from .receipt_partition import build_receipt_lines

logger = logging.getLogger(__name__)


# 付款人：避免纯数字账号被当成名称（后续 _clean_payer 也会过滤）
# 注意：勿使用「付款人名称…(?=收款人名称)」跨整段匹配——合并成一行时会吞到文末。
# 广发「付款人+收款人」同一行请用 _payer_from_line_cgb 按行截取。
_PAYER_GENERIC = [
    re.compile(r'付\s*款\s*人\s*名\s*称\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付款人名称\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付款人户名\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付款户名\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付\s*款\s*方\s*名\s*称\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付款人\s*[(（][^)）]*[)）]\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    # 允许「付款人 张三」，但排除「付款人账号/付款人卡号」被误识别为名称
    re.compile(
        r'付\s*款\s*人(?!\s*名\s*称)(?!\s*账\s*号)(?!\s*卡\s*号)(?!\s*开\s*户\s*行)\s*[:：]?\s*([^\n\r]+?)(?=\s*收\s*款\s*人|$)',
        re.MULTILINE,
    ),
    re.compile(r'付款人\s*[:：]\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付\s*款\s*方\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'付款方\s*[:：]\s*([^\n\r]+)', re.MULTILINE),
]

_PAYER_ACCOUNT_GENERIC = [
    re.compile(r'付\s*款\s*人\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'付\s*款\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'付\s*款\s*方\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'付\s*款\s*人\s*卡\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
]

_PAYEE_ACCOUNT_GENERIC = [
    re.compile(r'收\s*款\s*人\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'收\s*款\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'收\s*款\s*方\s*账\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
    re.compile(r'收\s*款\s*人\s*卡\s*号\s*[:：]?\s*([0-9][0-9\s]{5,})', re.MULTILINE),
]

_AMOUNT_GENERIC = [
    # 北京银行等：PDF 字间空格「金 额 ( 小 写 ) 263.78」
    re.compile(
        r'金\s*额\s*[\(（]\s*小\s*写\s*[\)）]\s*([\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE | re.MULTILINE,
    ),
    # 通用：小写金额（允许“字间空格”）
    re.compile(
        r'小\s*写\s*金\s*额\s*[:：]?\s*(CNY\s*[\d,]+(?:\.\d{1,2})?|¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?|[\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE | re.MULTILINE,
    ),
    # 兴业等：金额:壹拾元零捌分 小写:10.08（仅小写字段）
    re.compile(
        r'小\s*写\s*[:：]?\s*(CNY\s*[\d,]+(?:\.\d{1,2})?|¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?|[\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE | re.MULTILINE,
    ),
    # 通用：金额(小写) / 金额（小写）
    re.compile(
        r'金\s*额\s*[\(（]\s*小\s*写\s*[\)）]\s*[:：]?\s*(CNY\s*[\d,]+(?:\.\d{1,2})?|¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?|[\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE | re.MULTILINE,
    ),
    # 通用：人民币 263.78（金额标签后）
    re.compile(
        r'金\s*额\s*[:：]?\s*(?:人民\s*币|RMB)?\s*([\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE | re.MULTILINE,
    ),
    # 招行：交易金额(小写) CNY4,376.80
    re.compile(
        r'交易金额\s*[（(]小写[)）]\s*[:：]?\s*(CNY\s*[\d,]+(?:\.\d{1,2})?|¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE,
    ),
    re.compile(
        r'[（(]小写[)）]\s*[:：]?\s*(¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?|CNY\s*[\d,]+(?:\.\d{1,2})?)',
        re.IGNORECASE,
    ),
    re.compile(r'小写金额\s*[:：]\s*([^\n\r]+)', re.MULTILINE),
    re.compile(r'金额\s*[:：]\s*(CNY\s*[\d,]+(?:\.\d{1,2})?|¥\s*[\d,]+(?:\.\d{1,2})?|￥\s*[\d,]+(?:\.\d{1,2})?)', re.MULTILINE),
    # 交行等：币种 人民币 金额 614.16
    re.compile(r'金额\s*[:：]?\s*([\d,]+(?:\.\d{1,2})?)(?=\s|金额大写|$)', re.MULTILINE),
]

def _sanitize_pdf_text(text: str) -> str:
    """
    去掉 PDF 字体子集 / ToUnicode 映射异常产生的 \\x00、U+FFFD 等占位。
    部分银行回单里阿拉伯数字在文本层被映射成空字节（日志里像 \\x00），
    清洗后该位置会变成“空”，正则抓不到小写金额——中文「金额大写」通常仍可读，
    交行等走 _amount_upper_cn_fallback 用大写还原。
    """
    if not text:
        return ''
    cleaned = text.replace('\x00', '').replace('\ufffd', '')
    if cleaned != text and logger.isEnabledFor(logging.DEBUG):
        null_count = text.count('\x00')
        repl_count = text.count('\ufffd')
        logger.debug(
            'sanitize_pdf_text: 原长=%s, 新长=%s, 移除\\x00=%s, 移除\\ufffd=%s',
            len(text),
            len(cleaned),
            null_count,
            repl_count,
        )
        logger.debug('sanitize_pdf_text: 清洗前预览=%r', text[:200])
        logger.debug('sanitize_pdf_text: 清洗后预览=%r', cleaned[:200])
    return cleaned


_CN_DIGIT = {'零': 0, '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5, '陆': 6, '柒': 7, '捌': 8, '玖': 9, '两': 2}
_CN_UNIT = {'十': 10, '拾': 10, '佰': 100, '百': 100, '仟': 1000, '千': 1000}


def _cn_int_to_arabic(s: str) -> int:
    """将「陆佰壹拾肆」「壹万」等中文整数转为阿拉伯数字（不含元角分）。"""
    if not s:
        return 0
    s = s.strip()
    if '万' in s:
        left, _, right = s.partition('万')
        return _cn_int_to_arabic(left or '壹') * 10000 + _cn_int_to_arabic(right)
    result = 0
    section = 0
    number = 0
    for c in s:
        if c in _CN_DIGIT:
            number = _CN_DIGIT[c]
        elif c in _CN_UNIT:
            u = _CN_UNIT[c]
            section += (number if number else 1) * u
            number = 0
    return result + section + number


def _parse_cn_upper_amount(s: str) -> str:
    """
    从「陆佰壹拾肆元壹角陆分」「人民币壹万元整」解析出小写金额字符串。
    用于交行等 PDF 小写数字丢失、仅大写可读时的兜底。
    """
    s = _clean_value(s.replace('人民币', '').replace('整', '').strip())
    if '元' in s:
        yuan_s, _, rest = s.partition('元')
    elif '圆' in s:
        yuan_s, _, rest = s.partition('圆')
    else:
        return ''
    yuan_int = _cn_int_to_arabic(yuan_s)
    jiao = 0
    fen = 0
    if '角' in rest:
        ja, _, rest = rest.partition('角')
        ja = ja.strip()
        if ja:
            jiao = _cn_int_to_arabic(ja) if len(ja) > 1 else _CN_DIGIT.get(ja, 0)
    if '分' in rest:
        fb, _, _ = rest.partition('分')
        fb = fb.strip()
        if fb:
            fen = _cn_int_to_arabic(fb) if len(fb) > 1 else _CN_DIGIT.get(fb, 0)
    total = round(yuan_int + jiao * 0.1 + fen * 0.01, 2)
    if total <= 0:
        return ''
    return f'{total:.2f}'


def _amount_upper_cn_fallback(text: str) -> str:
    """
    根据「金额大写」中文解析小写金额（交行、北京银行等 PDF 小写层异常时的兜底）。
    支持：连续「金额大写」、以及字间空格「金 额 ( 大 写 ) …」。
    """
    text = _sanitize_pdf_text(text)
    spaced_upper = re.compile(
        r'金\s*额\s*[\(（]\s*大\s*写\s*[\)）]\s*([^\n\r]+)',
        re.MULTILINE,
    )
    for line in text.splitlines():
        if '金额大写' in line:
            m = re.search(r'金额大写\s*([^\s\n]+)', line)
            if m:
                parsed = _parse_cn_upper_amount(m.group(1))
                if parsed:
                    return _normalize_amount(parsed)
        m = spaced_upper.search(line)
        if m:
            parsed = _parse_cn_upper_amount(m.group(1).strip())
            if parsed:
                return _normalize_amount(parsed)

    m = re.search(r'金额大写\s*([^\s\n]+)', text)
    if m:
        parsed = _parse_cn_upper_amount(m.group(1))
        if parsed:
            return _normalize_amount(parsed)
    m = spaced_upper.search(text)
    if m:
        parsed = _parse_cn_upper_amount(m.group(1).strip())
        if parsed:
            return _normalize_amount(parsed)

    return ''


def _truncate_payer_rest_at_stops(rest: str, stops: List[str]) -> str:
    """在 rest 中取最先出现的截断标签之前文本（支持拆字标签）。"""
    if not rest or not stops:
        return rest.strip()
    best = len(rest)
    for sep in stops:
        if sep in rest:
            idx = rest.index(sep)
            if idx < best:
                best = idx
        m = re.search(label_flex_pattern(sep), rest)
        if m and m.start() < best:
            best = m.start()
    return rest[:best].strip() if best < len(rest) else rest.strip()


def _payer_label_value(
    text: str,
    label: str,
    stop_before: Optional[List[str]] = None,
) -> str:
    """
    通用：在含 label 的行上取「标签后」内容，并在任一 stop_before 标签前截断。
    标签名由 JSON 配置，代码内用 label_flex_pattern 容忍拆字。
    """
    stops = stop_before or ['收款人名称']
    text = _sanitize_pdf_text(text)
    for line in text.splitlines():
        line = line.strip()
        m = re.search(label_flex_pattern(label) + r'\s*[:：]?\s*(.+)', line)
        if not m:
            continue
        rest = m.group(1).strip()
        rest = _truncate_payer_rest_at_stops(rest, stops)
        return _clean_payer(rest)
    return ''


def _purpose_usage_field_only(text: str) -> str:
    """
    仅从「用途」栏取值（含建行：结算方式同行、拆字「用」「途」）。
    无该栏或值为空则返回空；不使用摘要、附言、备注、交易摘要等。
    """
    text = _sanitize_pdf_text(text)
    m = re.search(r'结算方式\s+\S+\s+用\s*途\s+(\S+)', text)
    if m:
        v = (m.group(1) or '').strip()
        if v:
            return _clean_value(v)
    for pat in (
        r'用\s*途\s*[:：]\s*([^\n\r]*)',
        r'用\s*途\s*[:：]?\s*([^\n\r]+)',
        r'用途\s*[:：]\s*([^\n\r]*)',
    ):
        m = re.search(pat, text, re.MULTILINE)
        if m:
            break
    else:
        m = None
    if not m:
        return ''
    chunk = (m.group(1) or '').strip()
    if not chunk:
        return ''
    if re.match(label_flex_pattern('附言') + r'\s*[:：]', chunk):
        return ''
    m = re.search(label_flex_pattern('附言') + r'\s*[:：]', chunk)
    if m:
        chunk = chunk[:m.start()].strip()
    m = re.search(label_flex_pattern('摘要') + r'\s*[:：]', chunk)
    if m:
        chunk = chunk[:m.start()].strip()
    m = re.search(label_flex_pattern('备注') + r'\s*[:：]', chunk)
    if m:
        chunk = chunk[:m.start()].strip()
    chunk = _clean_value(chunk) if chunk else ''
    if chunk in ('-', '－', '—', '无', '无。', 'N/A', 'n/a'):
        return ''
    return chunk


def _transaction_summary_field_only(text: str) -> str:
    """
    仅从「交易摘要」栏取值；无该栏或值为空则返回空。
    """
    text = _sanitize_pdf_text(text)
    for pat in (
        r'交\s*易\s*摘\s*要\s*[:：]\s*([^\n\r]*)',
        r'交\s*易\s*摘\s*要\s*[:：]?\s*([^\n\r]+)',
        r'交易摘要\s*[:：]\s*([^\n\r]*)',
    ):
        m = re.search(pat, text, re.MULTILINE)
        if m:
            break
    else:
        m = None
    if not m:
        return ''
    chunk = (m.group(1) or '').strip()
    if not chunk:
        return ''
    for stop in ('用途', '附言', '摘要', '备注', '金额', '回单编号'):
        m_stop = re.search(label_flex_pattern(stop) + r'\s*[:：]', chunk)
        if m_stop:
            chunk = chunk[:m_stop.start()].strip()
            break
    chunk = _clean_value(chunk) if chunk else ''
    if chunk in ('-', '－', '—', '无', '无。', 'N/A', 'n/a'):
        return ''
    return chunk


def _normalize_currency_value(raw: str) -> str:
    v = _clean_value(raw).upper().replace(' ', '')
    if not v:
        return ''
    if any(k in v for k in ('人民币', 'CNY', 'RMB', '¥', '￥')):
        return '人民币'
    if any(k in v for k in ('港币', '港元', 'HKD', 'HK$', 'HK＄')):
        return '港币'
    if any(k in v for k in ('美元', 'USD', 'US$', 'US＄')):
        return '美元'
    return ''


def _extract_currency(text: str) -> str:
    """
    提取币种（人民币/美元/港币）：
    1) 优先从「币种」字段提取；
    2) 再从金额相关字段（大写/小写）中的货币标识提取。
    """
    text = _sanitize_pdf_text(text)

    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        m = re.search(r'币\s*种\s*[:：]?\s*([^\n\r]+)', ln, re.IGNORECASE)
        if not m:
            continue
        value = (m.group(1) or '').strip()
        for stop in ('金额', '金额大写', '金额小写', '用途', '附言', '摘要', '备注'):
            m_stop = re.search(label_flex_pattern(stop), value)
            if m_stop:
                value = value[:m_stop.start()].strip()
                break
        c = _normalize_currency_value(value)
        if c:
            return c

    amount_like_patterns = (
        r'交易金额\s*[（(]小写[)）]\s*[:：]?\s*([^\n\r]+)',
        r'金\s*额\s*[\(（]\s*小\s*写\s*[\)）]\s*[:：]?\s*([^\n\r]+)',
        r'小\s*写\s*金\s*额\s*[:：]?\s*([^\n\r]+)',
        r'小\s*写\s*[:：]?\s*([^\n\r]+)',
        r'金\s*额\s*[\(（]\s*大\s*写\s*[\)）]\s*[:：]?\s*([^\n\r]+)',
        r'金\s*额\s*大\s*写\s*[:：]?\s*([^\n\r]+)',
    )
    for pat in amount_like_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            c = _normalize_currency_value((m.group(1) or '').strip())
            if c:
                return c

    c = _normalize_currency_value(text)
    if c:
        return c
    return ''


def _first_match(patterns: List[re.Pattern], text: str) -> str:
    for pat in patterns:
        m = pat.search(text)
        if m:
            val = (m.group(1) or '').strip()
            if val:
                return val
    return ''


def _best_payer_from_patterns(patterns: List[re.Pattern], text: str) -> str:
    """
    付款人在同一页可能出现多次（分行预览里常有截断行 + 完整行）。
    这里汇总所有命中后，优先选择更完整的候选，避免拿到首个截断值。
    """
    candidates: List[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            raw = (m.group(1) or '').strip()
            if not raw:
                continue
            cleaned = _clean_payer(raw)
            if cleaned:
                candidates.append(cleaned)
    if not candidates:
        return ''
    # 优先长度更长的候选；同长度保持原出现顺序
    best = max(candidates, key=lambda x: len(x))
    return best


def _payer_from_ccb_fullname(text: str) -> str:
    """
    建行：付款人/收款人「全称」常同一行，且 PDF 可能在字间加空格（如 付 款 人 全 称）。
    必须按「付款人全称 … 收款人全称」成对截取，避免 全\\s*称 贪婪匹配到收款人一侧。
    """
    m = re.search(
        r'付\s*款\s*人\s*全\s*称\s*(.+?)\s*收\s*款\s*人\s*全\s*称',
        text,
        re.DOTALL,
    )
    if m:
        return _clean_payer(m.group(1))
    m = re.search(
        r'付款人\s*全称\s*[:：]?\s*(.+?)\s*收款人\s*全称',
        text,
        re.DOTALL,
    )
    if m:
        return _clean_payer(m.group(1))
    block = text
    if re.search(r'收\s*款\s*人', text):
        block = re.split(r'收\s*款\s*人', text, maxsplit=1)[0]
    m = re.search(r'全\s*称\s*[:：]?\s*([^\n\r]+)', block)
    if not m:
        m = re.search(r'全称\s*[:：]?\s*([^\n\r]+)', block)
    if not m:
        return ''
    return _clean_payer(m.group(1))


def _clean_payer(raw: str) -> str:
    s = _clean_value(raw)
    # 兼容宽泛正则误吸收标签前缀的情况：如「名称：康彦鹏」
    s = re.sub(r'^(名\s*称|名称)\s*[:：]\s*', '', s)
    # 防止把字段标签值（账号/卡号/开户行等）当作付款人名称
    if re.match(r'^(账\s*号|账号|卡\s*号|卡号|开\s*户\s*行|开户行)\s*[:：]?', s):
        return ''
    if '收款人名称' in s:
        s = s.split('收款人名称', 1)[0].strip()
    # 建行等：字间带空格时「收 款 人」
    if re.search(r'收\s*款\s*人', s):
        s = re.split(r'收\s*款\s*人', s, maxsplit=1)[0].strip()
    # 去掉常见尾随标签碎片
    s = re.split(r'\s{2,}|\t', s)[0].strip()
    # 去掉后续拼接字段（常见于“整行连在一起”）
    for stop in ('账号', '账 号', '开户行', '币种', '用途', '金额', '附言', '摘要', '备注'):
        m = re.search(label_flex_pattern(stop), s)
        if m and m.start() > 1:
            s = s[:m.start()].strip()
            break
    if s in ('-', '－', '—', '/', '无'):
        return ''
    if re.fullmatch(r'[\d\s\-]+', s.replace(' ', '')):
        return ''
    return s


def _clean_payee(raw: str) -> str:
    s = _clean_value(raw)
    # 去掉“全称/收款人全称”等前缀标签（兼容字间空格：全 称）
    s = re.sub(r'^(收\s*款\s*人\s*)?全\s*称\s*[:：]?\s*', '', s).strip()
    s = re.sub(r'^收\s*款\s*人\s*[:：]?\s*', '', s).strip()
    if re.match(r'^(账\s*号|账号|卡\s*号|卡号|开\s*户\s*行|开户行)\s*[:：]?', s):
        return ''
    if '付款人名称' in s:
        s = s.split('付款人名称', 1)[0].strip()
    if re.search(r'付\s*款\s*人', s):
        s = re.split(r'付\s*款\s*人', s, maxsplit=1)[0].strip()
    # 不再按连续空白直接截断：部分 PDF 会在公司名中间插入异常空白，导致名称被截短
    s = re.sub(r'\s+', ' ', s).strip()
    # 中文名称里的字间空格通常是排版噪声，合并掉（保留英文/数字间空格）
    s = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', s)
    # 截断后续字段标签与常见印章文字，避免把“会计业务章”等噪声并入收款人名称
    for stop in (
        '账号', '账 号', '开户行', '币种', '用途', '金额', '附言', '摘要', '备注',
        '会计业务章', '会计业务', '业务章', '财务专用章', '公章', '电子回单专用章', '回单专用章'
    ):
        m = re.search(label_flex_pattern(stop), s)
        if m and m.start() > 1:
            s = s[:m.start()].strip()
            break
    s = re.sub(r'\s*(会计业务章|会计业务|业务章|财务专用章|公章|电子回单专用章|回单专用章)\s*$', '', s).strip()
    if s in ('-', '－', '—', '/', '无'):
        return ''
    if re.fullmatch(r'[\d\s\-]+', s.replace(' ', '')):
        return ''
    return s


def _extract_payee_name(text: str) -> str:
    text = _sanitize_pdf_text(text)
    candidates: List[str] = []
    # 优先按标签截取（收集候选，不再命中即返回）
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        m = re.search(r'收\s*款\s*人\s*名\s*称\s*[:：]?\s*(.+)', ln)
        if m:
            v = _clean_payee(m.group(1))
            if v:
                candidates.append(v)
        m = re.search(r'收\s*款\s*人\s*[:：]?\s*(.+)', ln)
        if m:
            v = _clean_payee(m.group(1))
            if v:
                candidates.append(v)
    # 回退：全文正则
    for pat in (
        re.compile(r'收\s*款\s*人\s*名\s*称\s*[:：]?\s*([^\n\r]+)', re.MULTILINE),
        re.compile(r'收\s*款\s*人\s*[:：]?\s*([^\n\r]+?)(?=\s*付\s*款\s*人|$)', re.MULTILINE),
    ):
        for m in pat.finditer(text):
            v = _clean_payee(m.group(1))
            if v:
                candidates.append(v)
    if not candidates:
        return ''
    # 选优：优先长度更长（一般更完整），再按出现频次与字典序稳定排序
    freq: Dict[str, int] = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    uniq = list(freq.keys())
    uniq.sort(key=lambda x: (len(x), freq[x], x), reverse=True)
    return uniq[0]


def _extract_payer_account(text: str) -> str:
    text = _sanitize_pdf_text(text)
    # 先用“付款人账号/付款账号/付款方账号/付款人卡号”等强标签，
    # 避免被通用“账号”误命中到收款账号。
    strong_patterns = _PAYER_ACCOUNT_GENERIC[:-1] if len(_PAYER_ACCOUNT_GENERIC) > 1 else _PAYER_ACCOUNT_GENERIC
    raw = _first_match(strong_patterns, text)
    if not raw:
        return ''
    return re.sub(r'\D+', '', raw)


def _extract_payee_account(text: str) -> str:
    text = _sanitize_pdf_text(text)
    raw = _first_match(_PAYEE_ACCOUNT_GENERIC, text)
    if not raw:
        return ''
    return re.sub(r'\D+', '', raw)


def _rebalance_accounts_for_generic_layout(
    payer: str,
    payee: str,
    payer_account: str,
    payee_account: str,
) -> Tuple[str, str]:
    """
    通用模板兜底：左右栏样式里，常出现仅一个账号被读到，且误归到付款账号。
    当“付款人为空 + 收款人有值 + 仅识别到付款账号”时，将其归到收款账号。
    """
    if payer_account and not payee_account and not (payer or '').strip() and (payee or '').strip():
        return '', payer_account
    return payer_account, payee_account


def extract_fields_from_text(
    scoped: Dict[str, str],
    profile: Optional[BankProfile],
) -> Tuple[str, str, str, str, str, str, str, str]:
    """scoped 含 full / left / right，由 bank_templates.json 的 scope 选择栏位。"""
    payer_ps = profile.payer_patterns if profile else _PAYER_GENERIC

    payer_cfg = profile.payer if profile else {'strategy': 'generic_first', 'scope': 'full'}
    layout = _normalize_layout(profile.layout if profile else 'horizontal')
    payer_scope = payer_cfg.get('scope', 'full')
    if layout == 'vertical':
        payer_scope = 'full'

    t_payer = _sanitize_pdf_text(pick_scoped_text(scoped, payer_scope))
    t_amount = _sanitize_pdf_text(scoped.get('full', ''))
    t_purpose = _sanitize_pdf_text(pick_scoped_text(scoped, 'full'))
    t_summary = _sanitize_pdf_text(pick_scoped_text(scoped, 'full'))
    t_currency = _sanitize_pdf_text(pick_scoped_text(scoped, 'full'))

    raw_payer_st = payer_cfg.get('strategy', 'generic_first')
    payer_st = {
        'ccb_fullname_first': 'pair_between_labels',
        'cgb_line_first': 'label_value',
        'bcm_line_first': 'label_value',
    }.get(raw_payer_st, raw_payer_st)

    if payer_st == 'pair_between_labels':
        payer = _clean_payer(_payer_from_ccb_fullname(t_payer))
        if not payer:
            payer = _best_payer_from_patterns(payer_ps, t_payer)
    elif payer_st == 'label_value':
        label = payer_cfg.get('label', '付款人名称')
        stops = payer_cfg.get('stop_before')
        if stops is None:
            if raw_payer_st == 'cgb_line_first':
                stops = ['收款人名称']
            elif raw_payer_st == 'bcm_line_first':
                stops = ['开户行名称', '收款人名称', '币种', '付款人账号']
            else:
                stops = ['收款人名称', '开户行名称', '币种', '付款人账号']
        payer = _payer_label_value(t_payer, label, stops)
        if not payer:
            payer = _best_payer_from_patterns(payer_ps, t_payer)
    else:
        payer = _best_payer_from_patterns(payer_ps, t_payer)

    payee = _extract_payee_name(t_payer)
    payer_account = _extract_payer_account(t_payer)
    payee_account = _extract_payee_account(t_payer)
    payer_account, payee_account = _rebalance_accounts_for_generic_layout(
        payer,
        payee,
        payer_account,
        payee_account,
    )
    amount = _extract_amount_regex_then_upper(t_amount)

    # purpose 固化在代码：仅按“用途”字段提取；不从 JSON 配置
    purpose = _purpose_usage_field_only(t_purpose)
    summary = _transaction_summary_field_only(t_summary)
    currency = _extract_currency(t_currency)

    return payer, payee, payer_account, payee_account, amount, purpose, summary, currency


def _clean_value(value: str) -> str:
    if not value:
        return ''
    value = re.sub(r'\s+', ' ', value)
    return value.strip(' :：;；，,')


def _normalize_amount(value: str) -> str:
    if not value:
        return ''
    v = _clean_value(value).upper().replace('CNY', '').replace('¥', '').replace('￥', '').replace(',', '').strip()
    number_match = re.search(r'\d+(?:\.\d{1,2})?', v)
    return number_match.group() if number_match else v.strip()


def _amount_numeric_equal(a: str, b: str) -> bool:
    """比较两个金额字符串是否表示同一数值（小数两位）。"""
    if not a or not b:
        return False
    try:
        xa = float(a.replace(',', ''))
        xb = float(b.replace(',', ''))
        return round(xa, 2) == round(xb, 2)
    except ValueError:
        return a.strip() == b.strip()


def _extract_amount_regex_then_upper(text: str) -> str:
    """
    先按通用正则取小写金额，再按「金额大写」解析；
    两者都有且一致则采用；无值或不等时优先采用「金额大写」解析结果（无大写则退回正则）。
    """
    text = _sanitize_pdf_text(text)
    raw = _first_match(_AMOUNT_GENERIC, text)
    r = _normalize_amount(raw) if raw else ''
    u = _amount_upper_cn_fallback(text)
    if r and u:
        if _amount_numeric_equal(r, u):
            return r
        return u
    if u:
        return u
    if r:
        return r
    return ''


def extract_invoice_by_table_and_text(
    pdf_file_path: str,
    base_path: Optional[str] = None,
) -> Receipt:
    """读取单个银行回单 PDF：分行 → 识别银行 → 提取付款人/小写金额/用途/交易摘要，并输出日志。

    base_path：与批量处理一致的工作目录；模板优先从 base_path/bank_templates.json 读取。
    """
    logger.info(f'开始处理银行回单 {pdf_file_path} ...')
    profiles = bank_profiles_for_base(base_path, _PAYER_GENERIC)
    result = Receipt()
    result.name = os.path.basename(pdf_file_path)
    result.code = result.name

    all_lines: List[str] = []
    left_parts: List[str] = []
    right_parts: List[str] = []

    with pdfplumber.open(pdf_file_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            lines, meta = build_receipt_lines(page)
            for line in lines:
                all_lines.append(line)

            st = build_page_scoped_texts(page)
            if st['left']:
                left_parts.append(st['left'])
            if st['right']:
                right_parts.append(st['right'])

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f'--- 第 {page_idx} 页 分行预览（前 25 行）---')
                for i, ln in enumerate(lines[:25], 1):
                    preview = _sanitize_pdf_text(ln[:200])
                    logger.debug(f'  L{i:02d}: {preview}')

    full_text = '\n'.join(all_lines)

    scoped = {
        'full': _sanitize_pdf_text(full_text),
        'left': _sanitize_pdf_text('\n'.join(left_parts)),
        'right': _sanitize_pdf_text('\n'.join(right_parts)),
    }
    profile = detect_bank(scoped['full'][:8000], profiles)
    payer, payee, payer_account, payee_account, amount, purpose, summary, currency = extract_fields_from_text(scoped, profile)

    result.buyer = payer
    result.payee = payee
    result.payer_account = payer_account
    result.payee_account = payee_account
    result.amount = amount
    result.invoice_type = purpose
    result.transaction_summary = summary
    result.currency = currency

    bank_label = f'{profile.name}({profile.key})' if profile else '通用'
    out_line = (
        f'【解析输出】文件={result.name} | 模板={bank_label} | '
        f'付款人名称={result.buyer!r} | 收款人名称={result.payee!r} | '
        f'付款账号={result.payer_account!r} | '
        f'收款账号={result.payee_account!r} | '
        f'币种={result.currency!r} | '
        f'小写金额={result.amount!r} | 用途={result.invoice_type!r} | '
        f'交易摘要={result.transaction_summary!r}'
    )
    logger.info(out_line)

    if not any([result.buyer, result.amount, result.invoice_type]):
        raise Exception('未能从回单中提取到目标字段（付款人名称/小写金额/用途）')

    return result
