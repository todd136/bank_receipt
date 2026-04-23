"""
业务可读的配置驱动字段逻辑：JSON 只写标签名与关键词，正则仅在代码内由标签生成。
"""
import re
from typing import Dict, List, Optional


def label_flex_pattern(label: str) -> str:
    """将「付款人名称」等转为可容忍 PDF 拆字的模式（代码内部使用）。"""
    label = label.strip()
    if not label:
        return ''
    return r'\s*'.join(re.escape(c) for c in label)


def extract_segment_between_labels(
    text: str,
    start_label: str,
    end_labels: List[str],
) -> Optional[str]:
    """取 start_label 匹配结束之后、任一 end_label 最先出现之前 的片段。"""
    m_start = re.search(label_flex_pattern(start_label), text)
    if not m_start:
        return None
    tail = text[m_start.end() :]
    best_pos: Optional[int] = None
    for el in end_labels:
        m = re.search(label_flex_pattern(el), tail)
        if m:
            p = m.start()
            if best_pos is None or p < best_pos:
                best_pos = p
    if best_pos is None:
        return tail
    return tail[:best_pos]


def section_contains_keywords(
    text: str,
    start_label: str,
    end_labels: List[str],
    anchor_label: str,
    keywords_after_anchor: List[str],
) -> bool:
    """区段内：在 anchor_label 之后须出现 keywords 之一（如交行收款人开户行）。"""
    seg = extract_segment_between_labels(text, start_label, end_labels)
    if seg is None:
        return False
    m = re.search(label_flex_pattern(anchor_label), seg)
    if not m:
        return False
    after = seg[m.end() :]
    return any(k in after for k in keywords_after_anchor)


def value_after_label(
    text: str,
    label: str,
    stop_before: Optional[List[str]] = None,
) -> str:
    """取 label 之后到行尾或下一个 stop 标签前的文本。"""
    m = re.search(label_flex_pattern(label) + r'\s*[:：]?\s*', text)
    if not m:
        return ''
    rest = text[m.end() :]
    stop_before = stop_before or []
    best: Optional[int] = None
    for s in stop_before:
        mp = re.search(label_flex_pattern(s), rest)
        if mp:
            if best is None or mp.start() < best:
                best = mp.start()
    if best is not None:
        rest = rest[:best]
    line = rest.splitlines()[0] if rest else ''
    return line.strip()


def pick_scoped_text(scoped: Dict[str, str], scope: str) -> str:
    """scope: full | left | right；空栏时回退 full。"""
    scope = (scope or 'full').lower()
    if scope not in ('left', 'right', 'full'):
        scope = 'full'
    t = scoped.get(scope, '') or ''
    if scope != 'full' and not t.strip():
        return scoped.get('full', '') or ''
    return t
