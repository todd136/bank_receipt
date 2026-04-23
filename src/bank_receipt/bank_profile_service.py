"""
银行模板加载与识别服务。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bank_fields import section_contains_keywords

logger = logging.getLogger(__name__)

# 与项目根目录 bank_templates.json 保持一致；base_path 下无文件时使用此默认
_DEFAULT_BANK_TEMPLATES_JSON = """
[
  {
    "key": "ccb",
    "name": "中国建设银行",
    "layout": "horizontal",
    "detect": {
      "type": "contains_any",
      "keywords": ["中国建设银行", "建设银行"]
    },
    "payer": { "strategy": "pair_between_labels", "scope": "full" }
  },
  {
    "key": "cgb",
    "name": "广发银行",
    "layout": "horizontal",
    "detect": {
      "type": "contains_any",
      "keywords": ["广发银行", "广发银行客户回单"]
    },
    "payer": {
      "strategy": "label_value",
      "scope": "full",
      "label": "付款人名称",
      "stop_before": ["收款人名称"]
    }
  },
  {
    "key": "cmb",
    "name": "招商银行",
    "layout": "horizontal",
    "detect": {
      "type": "contains_any",
      "keywords": ["招商银行", "CHINA MERCHANTS"]
    },
    "payer": { "strategy": "generic_first", "scope": "full" }
  },
  {
    "key": "boc",
    "name": "中国银行",
    "layout": "horizontal",
    "detect": {
      "type": "contains_any",
      "keywords": ["中国银行", "BANK OF CHINA", "国内支付业务收款回单"]
    },
    "payer": { "strategy": "generic_first", "scope": "full" }
  },
  {
    "key": "icbc",
    "name": "中国工商银行",
    "layout": "horizontal",
    "detect": {
      "type": "contains_any",
      "keywords": ["中国工商银行", "INDUSTRIAL AND COMMERCIAL BANK OF CHINA"]
    },
    "payer": { "strategy": "generic_first", "scope": "full" }
  },
  {
    "key": "bcm",
    "name": "交通银行",
    "layout": "vertical",
    "detect": {
      "type": "section_contains",
      "start_label": "收款人名称",
      "end_labels": ["付款人账号", "付款人名称"],
      "anchor_label": "开户行名称",
      "keywords_after_anchor": ["交通银行"]
    },
    "payer": {
      "strategy": "label_value",
      "scope": "full",
      "label": "付款人名称",
      "stop_before": ["开户行名称", "收款人名称", "币种", "付款人账号"]
    }
  }
]
"""

_DEFAULT_PROFILES_CACHE_KEY = "__default__"


@dataclass
class BankProfile:
    """layout：horizontal=左右分栏；vertical=上下排列。"""

    key: str
    name: str
    layout: str
    detect_rule: Dict[str, Any]
    payer: Dict[str, Any]
    payer_patterns: List[re.Pattern]


def _blob_has_any(blob: str, needles: Tuple[str, ...]) -> bool:
    return any(n in blob for n in needles)


def _normalize_layout(layout: Optional[str]) -> str:
    l = (layout or "horizontal").strip().lower()
    if l in ("horizontal", "vertical"):
        return l
    return "horizontal"


def _coerce_template_row(row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row)
    if "payer" not in r:
        r["payer"] = {"strategy": r.get("payer_mode", "generic_first"), "scope": "full"}
    return r


def _rows_to_profiles(rows: List[Dict[str, Any]], payer_patterns: List[re.Pattern]) -> Tuple[BankProfile, ...]:
    profiles: List[BankProfile] = []
    for row in rows:
        row = _coerce_template_row(row)
        profiles.append(
            BankProfile(
                key=row["key"],
                name=row["name"],
                layout=_normalize_layout(row.get("layout")),
                detect_rule=row["detect"],
                payer=row["payer"],
                payer_patterns=payer_patterns,
            )
        )
    return tuple(profiles)


@lru_cache(maxsize=32)
def _load_bank_profiles_cached(cache_key: str, payer_patterns_key: str) -> Tuple[BankProfile, ...]:
    """
    从用户路径或内置默认加载银行模板。
    payer_patterns_key 仅用于区分缓存键，实际 pattern 由调用处再组装。
    """
    _ = payer_patterns_key
    if cache_key == _DEFAULT_PROFILES_CACHE_KEY:
        rows = json.loads(_DEFAULT_BANK_TEMPLATES_JSON)
        source_label = "内置默认模板"
    else:
        cfg_path = Path(cache_key)
        with cfg_path.open("r", encoding="utf-8") as f:
            rows = json.load(f)
        source_label = str(cfg_path)
    # 注意：这里先返回“无 pattern”轮廓，调用处会二次构建
    profiles = _rows_to_profiles(rows, [])
    logger.info("已加载银行模板 %s 条，来源: %s", len(profiles), source_label)
    return profiles


def bank_profiles_for_base(base_path: Optional[str], payer_patterns: List[re.Pattern]) -> Tuple[BankProfile, ...]:
    """
    供单次解析使用的模板列表：优先 base_path/bank_templates.json，否则内置默认。
    """
    if base_path:
        user = Path(base_path).expanduser().resolve() / "bank_templates.json"
        if user.is_file():
            base = _load_bank_profiles_cached(str(user.resolve()), "payer_patterns")
            return tuple(
                BankProfile(
                    key=p.key,
                    name=p.name,
                    layout=p.layout,
                    detect_rule=p.detect_rule,
                    payer=p.payer,
                    payer_patterns=payer_patterns,
                )
                for p in base
            )
        logger.info("base_path 下未找到 bank_templates.json，使用内置默认模板")
    base = _load_bank_profiles_cached(_DEFAULT_PROFILES_CACHE_KEY, "payer_patterns")
    return tuple(
        BankProfile(
            key=p.key,
            name=p.name,
            layout=p.layout,
            detect_rule=p.detect_rule,
            payer=p.payer,
            payer_patterns=payer_patterns,
        )
        for p in base
    )


def _match_detect_rule(text: str, rule: Dict[str, Any]) -> bool:
    rtype = (rule or {}).get("type")
    if rtype == "contains_any":
        return _blob_has_any(text, tuple(rule.get("keywords", [])))
    if rtype == "section_contains":
        if rule.get("start_regex"):
            m_start = re.search(rule.get("start_regex", ""), text)
            if not m_start:
                return False
            tail = text[m_start.end() :]
            m_end = re.search(rule.get("end_regex", ""), tail)
            if not m_end:
                return False
            segment = tail[: m_end.start()]
            m_anchor = re.search(rule.get("anchor_regex", ""), segment)
            if not m_anchor:
                return False
            after_anchor = segment[m_anchor.end() :]
            return _blob_has_any(after_anchor, tuple(rule.get("keywords_after_anchor", [])))
        return section_contains_keywords(
            text,
            rule["start_label"],
            rule.get("end_labels", []),
            rule["anchor_label"],
            rule.get("keywords_after_anchor", []),
        )
    return False


def detect_bank(text: str, profiles: Tuple[BankProfile, ...]) -> Optional[BankProfile]:
    for profile in profiles:
        if _match_detect_rule(text, profile.detect_rule):
            logger.debug("识别银行模板: %s (%s)", profile.name, profile.key)
            return profile
    return None
