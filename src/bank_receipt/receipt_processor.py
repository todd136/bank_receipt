"""
回单处理流程控制器
负责批量处理回单的完整流程：扫描、解析
"""
import logging
from typing import List, Optional
from pathlib import Path

from .receipt import Receipt
from .receipt_service import extract_invoice_by_table_and_text
from .file_service import (
    find_files,
    rename_receipt_file,
    load_receipt_assignment_rules,
    match_receipt_owner,
    move_receipt_to_owner_folder,
)


class ReceiptProcessor:
    """银行回单处理流程控制器（沿用类名保持入口兼容）"""

    def __init__(self, base_path: str):
        self.base_path = base_path
        self.assignment_cfg = load_receipt_assignment_rules(base_path)

    def process_batch(self) -> List[Receipt]:
        logging.info(f'开始从 {self.base_path} 解析银行回单文件...')

        try:
            receipt_file_list = find_files(self.base_path)
        except Exception as e:
            logging.error(f'扫描银行回单发生错误: {e}')
            return []

        logging.info(f'在目录 {self.base_path} 中，找到 {len(receipt_file_list)} 张回单...')
        if len(receipt_file_list) == 0:
            logging.info(f'在目录 {self.base_path} 未找到回单，程序将退出...')
            return []

        receipt_list = []
        for receipt_file in receipt_file_list:
            receipt = self.process_single(receipt_file, receipt_list)
            if receipt:
                receipt_list.append(receipt)

        if len(receipt_list) == 0:
            logging.error(
                f'在目录 {self.base_path} 中，找到 {len(receipt_file_list)} 张回单，但未能读取到有效信息，程序将退出...'
            )
            return []

        logging.info('批量处理完成')
        return receipt_list

    def process_single(self, pdf_path: str, existing_invoices: Optional[List[Receipt]] = None) -> Optional[Receipt]:
        try:
            invoice = extract_invoice_by_table_and_text(pdf_path, self.base_path)
            renamed_path = rename_receipt_file(
                pdf_path,
                invoice.buyer,
                invoice.amount,
                invoice.currency,
                invoice.invoice_type,
                invoice.transaction_summary,
            )
            owner = match_receipt_owner(
                invoice.buyer,
                invoice.payee,
                invoice.payer_account,
                invoice.payee_account,
                self.assignment_cfg.get('owners', []),
            )
            if owner:
                renamed_path = move_receipt_to_owner_folder(
                    renamed_path,
                    self.base_path,
                    owner,
                    self.assignment_cfg.get('target_root', '财务分配'),
                )

            invoice.name = Path(renamed_path).name
            invoice.code = invoice.name

            if existing_invoices and invoice and invoice.code:
                invoice_code = invoice.code.strip()
                for existing_invoice in existing_invoices:
                    if existing_invoice.code.strip() == invoice_code:
                        logging.info(
                            f'回单 {invoice_code} 已存在 (文件名: {existing_invoice.name})，跳过 {pdf_path} 的处理'
                        )
                        return None

            return invoice

        except Exception as e:
            logging.error(f'读取回单 {pdf_path} 发生错误: {e}')
            return None
