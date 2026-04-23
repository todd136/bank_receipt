"""
主程序入口
"""
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.bank_receipt.receipt_processor import ReceiptProcessor
from src.bank_receipt.logger_config import setup_logger


def get_executable_dir():
    executable_path = Path(sys.argv[0])
    return executable_path.parent.absolute()


def main():
    base_path = get_executable_dir()

    # 可按需修改为你的银行回单目录
    # base_path = '/Volumes/share/temp/bank_receipt'

    setup_logger(
        base_path,
        global_level=logging.INFO,
        module_levels={'src.bank_receipt': logging.INFO}
    )

    logging.info(f'开始在主路径： {str(base_path)} 下处理银行回单...')

    processor = ReceiptProcessor(base_path)
    processor.process_batch()

    logging.info('银行回单解析完成，即将退出...')


if __name__ == '__main__':
    main()
