"""
按词块坐标划分左/右栏文本，供模板按 scope 取值，避免左右分栏时串栏。
"""
from typing import Dict, List

from .receipt_partition import group_words_by_y


def words_to_text_lines(words: List[dict], y_tolerance: float = 3.0) -> str:
    """将词块列表按行拼接为文本（与 receipt_partition.lines_from_words 逻辑一致）。"""
    if not words:
        return ''
    lines_dict = group_words_by_y(words, y_tolerance=float(y_tolerance))
    out: List[str] = []
    for y in sorted(lines_dict.keys()):
        row = sorted(lines_dict[y], key=lambda w: w['x0'])
        line = ''.join(w.get('text', '') for w in row)
        line = ' '.join(line.split())
        if line:
            out.append(line)
    return '\n'.join(out)


def build_page_scoped_texts(page, y_tolerance: float = 3.0) -> Dict[str, str]:
    """
    按页面中线将词块分为左栏 / 右栏；full 为整页词块行文本。
    """
    words = (
        page.extract_words(
            x_tolerance=3,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True,
        )
        or []
    )
    if not words:
        return {'full': '', 'left': '', 'right': ''}
    mid = float(page.width) / 2.0
    left_w = [w for w in words if (w['x0'] + w['x1']) / 2.0 < mid]
    right_w = [w for w in words if (w['x0'] + w['x1']) / 2.0 >= mid]
    return {
        'full': words_to_text_lines(words, y_tolerance),
        'left': words_to_text_lines(left_w, y_tolerance),
        'right': words_to_text_lines(right_w, y_tolerance),
    }
