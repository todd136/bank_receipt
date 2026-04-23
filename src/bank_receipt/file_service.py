"""
文件操作服务
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional


def find_files(root_path: str) -> List[str]:
    """
    递归地查找指定目录下的所有 PDF 文件
    
    Args:
        root_path: 根目录路径
        
    Returns:
        PDF 文件路径列表
    """
    root = Path(root_path)
    pdf_files = list(root.glob('*.pdf'))
    # pdf_files.sort(key=lambda x: x.stat().st_mtime)

    invoice_list = [str(f) for f in pdf_files]
    invoice_list.sort()
    return invoice_list


def _sanitize_filename_part(value: str, default: str) -> str:
    """清洗文件名片段，避免非法字符与空白值。"""
    v = (value or '').strip()
    if not v:
        return default
    v = re.sub(r'[\\/:*?"<>|]+', '_', v)
    v = re.sub(r'\s+', ' ', v).strip(' ._')
    return v or default


def _currency_to_symbol(currency: str) -> str:
    c = (currency or '').strip()
    if c == '美元':
        return '$'
    if c == '港币':
        return 'HK$'
    # 默认人民币
    return '¥'


def rename_receipt_file(
    source_path: str,
    payer: str,
    amount: str,
    currency: str,
    purpose: str,
    transaction_summary: str = '',
) -> str:
    """
    将回单重命名为：付款人名称_用途_交易摘要_货币符号+小写金额.pdf。
    若目标名已存在，则自动追加序号后缀避免覆盖。
    返回新文件的完整路径；若无需重命名，返回原路径。
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f'待重命名文件不存在: {source_path}')

    payer_raw = (payer or '').strip()
    if not payer_raw:
        logging.info("付款人为空，跳过重命名: '%s'", src.name)
        return str(src)

    payer_part = _sanitize_filename_part(payer_raw, '未知付款人')
    amount_part = _sanitize_filename_part(amount, '未知金额')
    currency_symbol = _currency_to_symbol(currency)
    purpose_raw = (purpose or '').strip()
    summary_raw = (transaction_summary or '').strip()

    base_parts = [payer_part]
    if purpose_raw:
        base_parts.append(_sanitize_filename_part(purpose_raw, '未知用途'))
    if summary_raw:
        base_parts.append(_sanitize_filename_part(summary_raw, '未知交易摘要'))
    base_parts.append(f'{currency_symbol}{amount_part}')
    base_name = '_'.join(base_parts)

    target = src.with_name(f'{base_name}.pdf')
    idx = 1
    while target.exists() and target.resolve() != src.resolve():
        target = src.with_name(f'{base_name}_{idx}.pdf')
        idx += 1

    if target.resolve() == src.resolve():
        return str(src)

    src.rename(target)
    logging.info("文件已重命名: '%s' -> '%s'", src.name, target.name)
    return str(target)


def load_receipt_assignment_rules(base_path: str) -> dict:
    """
    读取回单分配规则。
    支持两种写法：
    1) 简单 OR 规则：payer_matches / payee_matches / payer_account_matches / payee_account_matches
    2) and 规则：owner.and = [{by, value, op?}, ...]
    - 文件路径：{base_path}/receipt_assignments.json
    - 结构示例：
      {
        "target_root": "财务分配",
        "owners": [
          {
            "owner": "张三",
            "payer_matches": ["兴业银行", "招商银行"]
          }
        ]
      }
    """
    cfg_path = Path(base_path) / 'receipt_assignments.json'
    if not cfg_path.is_file():
        return {"target_root": "", "owners": []}
    with cfg_path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    target_root = (data.get('target_root') or '').strip()
    owners = data.get('owners', [])
    if not isinstance(owners, list):
        owners = []
    # 兼容旧版 rules：自动转换为 owners
    if not owners:
        rules = data.get('rules', [])
        if isinstance(rules, list) and rules:
            owner_map = {}
            for rule in rules:
                owner = str(rule.get('owner', '')).strip()
                if not owner:
                    continue
                if owner not in owner_map:
                    owner_map[owner] = {
                        'owner': owner,
                        'payer_matches': [],
                        'payee_matches': [],
                        'payer_account_matches': [],
                        'payee_account_matches': [],
                    }
                by = str(rule.get('by', '')).strip()
                match = str(rule.get('match', '')).strip()
                if not match:
                    continue
                if by == 'payer':
                    owner_map[owner]['payer_matches'].append(match)
                elif by == 'payee':
                    owner_map[owner]['payee_matches'].append(match)
                elif by == 'payer_account':
                    owner_map[owner]['payer_account_matches'].append(match)
                elif by == 'payee_account':
                    owner_map[owner]['payee_account_matches'].append(match)
            owners = list(owner_map.values())
    _validate_assignment_owners(owners)
    return {"target_root": target_root, "owners": owners}


def _validate_assignment_owners(owners: List[dict]) -> None:
    """
    启动时校验 owner 分配规则。
    - 支持简单 OR：payer_matches / payee_matches / payer_account_matches / payee_account_matches
    - 支持 and 组合：and=[{by,value,op?}, ...]
    """
    for idx, owner_cfg in enumerate(owners, start=1):
        owner = str(owner_cfg.get('owner', '')).strip() or f'第{idx}条'
        payer_matches = owner_cfg.get('payer_matches', [])
        payee_matches = owner_cfg.get('payee_matches', [])
        payer_account_matches = owner_cfg.get('payer_account_matches', [])
        payee_account_matches = owner_cfg.get('payee_account_matches', [])
        and_conditions = owner_cfg.get('and', [])
        if not isinstance(payer_matches, list):
            payer_matches = []
        if not isinstance(payee_matches, list):
            payee_matches = []
        if not isinstance(payer_account_matches, list):
            payer_account_matches = []
        if not isinstance(payee_account_matches, list):
            payee_account_matches = []
        if not isinstance(and_conditions, list):
            and_conditions = []

        has_simple = any(str(x).strip() for x in payer_matches) or any(str(x).strip() for x in payee_matches) or any(
            str(x).strip() for x in payer_account_matches
        ) or any(str(x).strip() for x in payee_account_matches)
        has_and = len(and_conditions) > 0

        if has_simple and has_and:
            logging.warning(
                "分配规则校验: owner='%s' 同时配置了简单匹配与 and 规则；将优先按 and 规则匹配",
                owner,
            )
        elif not has_simple and not has_and:
            logging.warning(
                "分配规则校验: owner='%s' 未配置任何匹配项（payer/payee/payer_account/payee_account/and），该条规则不会生效",
                owner,
            )


def _normalize_account(v: str) -> str:
    return re.sub(r'\D+', '', v or '')


def _match_condition(
    by: str,
    op: str,
    value,
    payer_v: str,
    payee_v: str,
    payer_account_v: str,
    payee_account_v: str,
) -> bool:
    by_v = (by or '').strip()
    op_v = (op or 'contains').strip().lower()
    if not by_v or value is None:
        return False

    if by_v == 'payer':
        if isinstance(value, list):
            values = [str(v).strip() for v in value if str(v).strip()]
        else:
            one = str(value).strip()
            values = [one] if one else []
        if not values:
            return False
        if op_v in ('eq', 'equals', 'exact'):
            return any(payer_v == v for v in values)
        # contains: value 为数组时，任一命中即为真
        return any(payer_v and v in payer_v for v in values)
    if by_v == 'payee':
        if isinstance(value, list):
            values = [str(v).strip() for v in value if str(v).strip()]
        else:
            one = str(value).strip()
            values = [one] if one else []
        if not values:
            return False
        if op_v in ('eq', 'equals', 'exact'):
            return any(payee_v == v for v in values)
        return any(payee_v and v in payee_v for v in values)

    raw = str(value).strip()
    if not raw:
        return False
    if by_v == 'payer_account':
        lhs = payer_account_v
        rhs = _normalize_account(raw)
        return bool(lhs and rhs and lhs == rhs)
    if by_v == 'payee_account':
        lhs = payee_account_v
        rhs = _normalize_account(raw)
        return bool(lhs and rhs and lhs == rhs)
    return False


def match_receipt_owner(
    payer: str,
    payee: str,
    payer_account: str,
    payee_account: str,
    owners: List[dict],
) -> Optional[str]:
    """按配置顺序匹配财务人员。"""
    payer_v = (payer or '').strip()
    payee_v = (payee or '').strip()
    payer_account_v = _normalize_account(payer_account)
    payee_account_v = _normalize_account(payee_account)
    for owner_cfg in owners:
        owner = str(owner_cfg.get('owner', '')).strip()
        if not owner:
            continue
        and_conditions = owner_cfg.get('and', [])
        if isinstance(and_conditions, list) and and_conditions:
            ok = True
            for cond in and_conditions:
                if not isinstance(cond, dict):
                    ok = False
                    break
                by = str(cond.get('by', '')).strip()
                op = str(cond.get('op', 'contains')).strip()
                value = cond.get('value', '')
                if not _match_condition(by, op, value, payer_v, payee_v, payer_account_v, payee_account_v):
                    ok = False
                    break
            if ok:
                return owner
            continue

        payer_matches = owner_cfg.get('payer_matches', [])
        if not isinstance(payer_matches, list):
            payer_matches = []
        payee_matches = owner_cfg.get('payee_matches', [])
        if not isinstance(payee_matches, list):
            payee_matches = []
        payer_account_matches = owner_cfg.get('payer_account_matches', [])
        if not isinstance(payer_account_matches, list):
            payer_account_matches = []
        payee_account_matches = owner_cfg.get('payee_account_matches', [])
        if not isinstance(payee_account_matches, list):
            payee_account_matches = []

        # 简单规则采用 OR：任一命中即归属 owner
        if payer_matches:
            for keyword in payer_matches:
                kw = str(keyword).strip()
                if kw and payer_v and kw in payer_v:
                    return owner
        if payee_matches:
            for keyword in payee_matches:
                kw = str(keyword).strip()
                if kw and payee_v and kw in payee_v:
                    return owner
        if payer_account_matches:
            for match_account in payer_account_matches:
                rule_account = _normalize_account(str(match_account))
                if rule_account and payer_account_v and rule_account == payer_account_v:
                    return owner
        if payee_account_matches:
            for match_account in payee_account_matches:
                rule_account = _normalize_account(str(match_account))
                if rule_account and payee_account_v and rule_account == payee_account_v:
                    return owner
    return None


def move_receipt_to_owner_folder(
        source_path: str,
        base_path: str,
        owner: str,
        target_root: str = '',
) -> str:
    """
    将回单移动到财务人员目录：
    - target_root 有值：{base_path}/{target_root}/{owner}/
    - target_root 为空：{base_path}/{owner}/
    若同名已存在，则自动追加序号避免覆盖。
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f'待分配文件不存在: {source_path}')
    owner_safe = _sanitize_filename_part(owner, '未分配')
    root_raw = (target_root or '').strip()
    if root_raw:
        root_safe = _sanitize_filename_part(root_raw, '财务分配')
        destination_dir = Path(base_path) / root_safe / owner_safe
    else:
        root_safe = ''
        destination_dir = Path(base_path) / owner_safe
    destination_dir.mkdir(parents=True, exist_ok=True)

    target = destination_dir / src.name
    idx = 1
    while target.exists() and target.resolve() != src.resolve():
        target = destination_dir / f'{src.stem}_{idx}{src.suffix}'
        idx += 1

    if target.resolve() == src.resolve():
        return str(src)

    src.rename(target)
    if root_safe:
        logging.info("文件已分配: '%s' -> '%s/%s'", src.name, root_safe, owner_safe)
    else:
        logging.info("文件已分配: '%s' -> '%s'", src.name, owner_safe)
    return str(target)
