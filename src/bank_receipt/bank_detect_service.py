"""
银行模板判定服务（与模板加载拆分）。
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from .bank_fields import section_contains_keywords
from .bank_profile_service import BankProfile

logger = logging.getLogger(__name__)
_MIN_ACCEPT_SCORE = 35


def _blob_has_any(blob: str, needles: Tuple[str, ...]) -> bool:
    return any(n and n in blob for n in needles)


def _find_line_matches(lines: List[str], keyword: str) -> List[str]:
    if not keyword:
        return []
    return [ln for ln in lines if keyword in ln]


def _score_contains_any(text: str, rule: Dict[str, object]) -> int:
    """
    contains_any 改为评分制，降低“开户行字段误命中”：
    - 标题/页脚/域名行命中加高分
    - 仅在“付款开户行/收款开户行”行命中仅给低分
    """
    keywords = tuple(str(k).strip() for k in (rule or {}).get("keywords", []) if str(k).strip())
    if not keywords:
        return 0

    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]
    if not lines:
        return 0

    # 头尾行通常承载“回单归属银行”信息
    head = lines[:8]
    tail = lines[-6:] if len(lines) > 6 else []

    score = 0
    for kw in keywords:
        matched_lines = _find_line_matches(lines, kw)
        if not matched_lines:
            continue
        score += 20  # 基础命中分
        for ln in matched_lines:
            # URL/域名证据权重更高
            if "http" in ln or ".com" in ln or ".cn" in ln:
                score += 70
            # 标题区、页脚区权重更高
            if ln in head or ln in tail:
                score += 40
            # 显式回单标题词加分
            if any(t in ln for t in ("回单", "客户回单", "电子回单", "网银")):
                score += 50
            # 仅开户行字段命中，可能是交易对手开户行，降权
            if ("开户行" in ln or "开户银行" in ln) and ("付款" in ln or "收款" in ln):
                score -= 45
    return score


def _match_detect_rule(text: str, rule: Dict[str, object]) -> bool:
    rtype = (rule or {}).get("type")
    if rtype == "contains_any":
        return _score_contains_any(text, rule) >= 35
    if rtype == "section_contains":
        if rule.get("start_regex"):
            m_start = re.search(str(rule.get("start_regex", "")), text)
            if not m_start:
                return False
            tail = text[m_start.end() :]
            m_end = re.search(str(rule.get("end_regex", "")), tail)
            if not m_end:
                return False
            segment = tail[: m_end.start()]
            m_anchor = re.search(str(rule.get("anchor_regex", "")), segment)
            if not m_anchor:
                return False
            after_anchor = segment[m_anchor.end() :]
            return _blob_has_any(after_anchor, tuple(rule.get("keywords_after_anchor", [])))
        return section_contains_keywords(
            text,
            str(rule["start_label"]),
            rule.get("end_labels", []),
            str(rule["anchor_label"]),
            rule.get("keywords_after_anchor", []),
        )
    return False


def _score_profile(text: str, profile: BankProfile) -> int:
    rule = profile.detect_rule or {}
    rtype = rule.get("type")
    score = 0
    if rtype == "contains_any":
        score = _score_contains_any(text, rule)
    elif rtype == "section_contains":
        score = 120 if _match_detect_rule(text, rule) else 0

    # 银行专属强特征（标题/域名/系统标识）加分，避免被“开户行字段”误导。
    key = (profile.key or "").lower()
    if key == "cmb":
        if "cmbchina.com" in text or "fbc-web.paas.cmbchina.com" in text or "企业电子回单服务" in text:
            score += 120
    elif key == "cgb":
        if "cgbchina.com.cn" in text or "广发银行客户回单" in text:
            score += 120
    elif key == "boc":
        if "bank of china" in text.lower() or "中国银行" in text:
            score += 40
    elif key == "ccb":
        if "中国建设银行单位客户专用回单" in text or "中国建设银行" in text:
            score += 40
    return score


def _apply_global_evidence_bonus(text: str, profile: BankProfile, base_score: int) -> int:
    """
    使用跨模板的强证据进行加权：
    - 招商回单域名/系统标识 -> 强推招商模板，压低其他模板。
    """
    score = base_score
    key = (profile.key or "").lower()
    rule = profile.detect_rule or {}
    kws = [str(k).strip().lower() for k in rule.get("keywords", []) if str(k).strip()]
    has_cmb_marker = (
        "fbc-web.paas.cmbchina.com" in text
        or "cmbchina.com" in text
        or "企业电子回单服务" in text
    )
    if has_cmb_marker:
        is_cmb_profile = (key == "cmb") or any(("招商银行" in k) or ("china merchants" in k) or ("cmbchina" in k) for k in kws)
        if is_cmb_profile:
            score += 260
        else:
            score -= 40
    return score


def detect_bank(text: str, profiles: Tuple[BankProfile, ...]) -> Optional[BankProfile]:
    """
    模板识别改为“最高分命中”，不再“首个命中即返回”。
    """
    best: Optional[BankProfile] = None
    best_score = 0
    score_board = []
    for profile in profiles:
        score = _score_profile(text, profile)
        score = _apply_global_evidence_bonus(text, profile, score)
        score_board.append((profile.key, profile.name, score))
        if score > best_score:
            best = profile
            best_score = score
    if best and best_score >= _MIN_ACCEPT_SCORE:
        logger.debug("识别银行模板: %s (%s), score=%s", best.name, best.key, best_score)
        return best
    if score_board:
        top3 = sorted(score_board, key=lambda x: x[2], reverse=True)[:3]
        logger.debug("银行模板评分均未达阈值(%s)，TOP=%s", _MIN_ACCEPT_SCORE, top3)
    return None

