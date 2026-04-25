"""
银行回单数据模型定义
"""
from dataclasses import dataclass


@dataclass
class Receipt:
    """银行回单结构（沿用 Invoice 命名以保持兼容）"""
    name: str = ""  # 回单文件名
    code: str = ""  # 回单唯一标识（默认文件名）
    date: str = ""  # 保留兼容字段
    buyer: str = ""  # 付款人名称
    payee: str = ""  # 收款人名称
    payee_bank_name: str = ""  # 收款人开户行名称
    payer_account: str = ""  # 付款账号
    payee_account: str = ""  # 收款账号
    invoice_type: str = ""  # 用途
    transaction_summary: str = ""  # 交易摘要
    currency: str = ""  # 币种（人民币/美元/港币）
    amount: str = ""  # 小写金额
