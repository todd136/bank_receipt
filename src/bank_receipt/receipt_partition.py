"""
银行回单版面解析：优先按表格读取，再按词块坐标拼行，得到按行分割的文本列表。
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def group_words_by_y(words: List[dict], y_tolerance: float = 3.0) -> Dict[float, List[dict]]:
    """将单词按 Y 坐标分组为同一视觉行。"""
    lines_dict: Dict[float, List[dict]] = {}
    for word in words:
        y_key = round(word['top'] / y_tolerance) * y_tolerance
        lines_dict.setdefault(y_key, []).append(word)
    return lines_dict


def _cell_to_str(cell) -> str:
    if cell is None:
        return ''
    return str(cell).replace('\n', ' ').strip()


def lines_from_extract_tables(page) -> List[str]:
    """
    使用 pdfplumber 的表格检测，将每个非空单元格行拼成一行文本。
    对有线框的建行等版式更有效；无表格矢量时可能返回空列表。
    """
    lines: List[str] = []
    settings_list = (
        {
            'vertical_strategy': 'lines',
            'horizontal_strategy': 'lines',
            'intersection_tolerance': 5,
            'snap_tolerance': 3,
        },
        {
            'vertical_strategy': 'text',
            'horizontal_strategy': 'text',
            'intersection_tolerance': 5,
            'snap_tolerance': 3,
            'text_x_tolerance': 3,
            'text_y_tolerance': 3,
        },
    )
    tables = []
    try:
        for settings in settings_list:
            tables = page.extract_tables(settings) or []
            if tables:
                break
    except Exception as e:
        logger.debug(f'extract_tables 失败，将仅用词块分行: {e}')
        return lines

    for table in tables:
        if not table:
            continue
        for row in table:
            if not row:
                continue
            parts = [_cell_to_str(c) for c in row if _cell_to_str(c)]
            if parts:
                line = ' '.join(parts)
                if line.strip():
                    lines.append(line.strip())
    return lines


def lines_from_words(page, x_tolerance: int = 3, y_tolerance: int = 3) -> List[str]:
    """从词块坐标按行拼接（与发票场景类似，适用于无表格线的回单）。"""
    words = page.extract_words(
        x_tolerance=x_tolerance,
        y_tolerance=y_tolerance,
        keep_blank_chars=False,
        use_text_flow=True,
    ) or []
    if not words:
        return []

    lines_dict = group_words_by_y(words, y_tolerance=float(y_tolerance))
    out: List[str] = []
    for y in sorted(lines_dict.keys()):
        row = sorted(lines_dict[y], key=lambda w: w['x0'])
        line = ''.join(w.get('text', '') for w in row)
        line = _normalize_whitespace(line)
        if line:
            out.append(line)
    return out


def _normalize_whitespace(s: str) -> str:
    return ' '.join(s.split())


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        key = x.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(x.strip())
    return out


def build_receipt_lines(page) -> Tuple[List[str], dict]:
    """
    合并「表格行 + 词块行」，去重保序，得到回单全文按行分割的列表。

    Returns:
        (lines, meta) meta 含 table_line_count / word_line_count 便于调试
    """
    table_lines = lines_from_extract_tables(page)
    word_lines = lines_from_words(page)

    # 表格识别有效时仍以词块行为主（覆盖面更广），表格行插入补充
    merged = _dedupe_preserve_order(table_lines + word_lines)
    if not merged and word_lines:
        merged = word_lines
    if not merged and table_lines:
        merged = table_lines

    meta = {
        'table_line_count': len(table_lines),
        'word_line_count': len(word_lines),
        'merged_line_count': len(merged),
    }
    logger.debug(
        f'回单分行: 表格行={meta["table_line_count"]}, '
        f'词块行={meta["word_line_count"]}, 合并后={meta["merged_line_count"]}'
    )
    return merged, meta
