#!/usr/bin/env python3
"""
PDF 文本/CID 诊断脚本（独立工具，不影响主流程）。

用途：
1) 查看 pdfplumber 抽到的文本片段（验证是否出现 \\x00）；
2) 查看关键标签附近字符明细（字符、码点、字体、坐标）；
3) 若安装了 pikepdf，额外输出页面内容流中 Tj/TJ 的原始字节（hex），用于 CID/编码诊断。

示例：
python3 scripts/diagnose_pdf_text_cid.py \
  --pdf "/path/to/file.pdf" \
  --page 1 \
  --label "收款人账号"
"""
from __future__ import annotations

import argparse
import re
from itertools import combinations

import pdfplumber


def _preview(s: str, n: int = 180) -> str:
    s = s.replace("\n", "\\n")
    return s[:n] + ("..." if len(s) > n else "")


def _print_text_diagnostics(pdf_path: str, page_no: int, label: str) -> None:
    with pdfplumber.open(pdf_path) as pdf:
        if page_no < 1 or page_no > len(pdf.pages):
            raise ValueError(f"页码越界: page={page_no}, total={len(pdf.pages)}")
        page = pdf.pages[page_no - 1]
        text = page.extract_text() or ""
        print("=== [A] pdfplumber.extract_text ===")
        print(f"page={page_no}, text_len={len(text)}")
        print(f"contains_label={label in text}, null_count={text.count(chr(0))}, repl_count={text.count(chr(0xfffd))}")
        if label in text:
            idx = text.index(label)
            seg = text[max(0, idx - 80): idx + 220]
            print(f"label_segment={_preview(seg, 500)}")
        else:
            print("label_segment=<label not found>")
        print()

        print("=== [B] page.chars 附近字符明细 ===")
        chars = page.chars or []
        if not chars:
            print("chars=<empty>")
            print()
            return
        # 用 chars 拼一个连续串，定位标签附近字符范围
        full = "".join((c.get("text") or "") for c in chars)
        pos = full.find(label)
        if pos < 0:
            print("chars_full: 未找到标签，输出前 80 个字符样本：")
            for i, c in enumerate(chars[:80], start=1):
                t = c.get("text") or ""
                cp = ord(t) if len(t) == 1 else None
                print(
                    f"{i:03d} text={t!r} cp={cp} font={c.get('fontname')} "
                    f"x0={c.get('x0'):.2f} top={c.get('top'):.2f}"
                )
            print()
            return

        # 取标签前后窗口
        start = max(0, pos - 20)
        end = min(len(chars), pos + len(label) + 80)
        for i in range(start, end):
            c = chars[i]
            t = c.get("text") or ""
            cp = ord(t) if len(t) == 1 else None
            flag = ""
            if t == "\x00":
                flag = " <NUL>"
            elif t == "\ufffd":
                flag = " <REPLACEMENT>"
            print(
                f"{i:04d} text={t!r} cp={cp} font={c.get('fontname')} "
                f"size={c.get('size')} x0={c.get('x0'):.2f} x1={c.get('x1'):.2f} top={c.get('top'):.2f}{flag}"
            )
        print()


def _bytes_to_hex(b: bytes, limit: int = 80) -> str:
    h = b.hex()
    if len(h) > limit * 2:
        return h[: limit * 2] + "..."
    return h


def _print_raw_stream_diagnostics(pdf_path: str, page_no: int) -> None:
    print("=== [C] 原始内容流 Tj/TJ（需 pikepdf） ===")
    try:
        import pikepdf  # type: ignore
    except Exception as e:
        print(f"pikepdf 不可用，跳过原始流诊断: {e}")
        print("可安装: pip install pikepdf")
        print()
        return

    with pikepdf.open(pdf_path) as doc:
        if page_no < 1 or page_no > len(doc.pages):
            raise ValueError(f"页码越界: page={page_no}, total={len(doc.pages)}")
        page = doc.pages[page_no - 1]
        ops = list(pikepdf.parse_content_stream(page))
        shown = 0
        for ins in ops:
            op = str(ins.operator)
            if op not in ("Tj", "TJ"):
                continue
            shown += 1
            operands = ins.operands
            print(f"[{shown}] op={op}")
            if op == "Tj":
                if operands:
                    s = operands[0]
                    try:
                        raw = bytes(s)
                    except Exception:
                        raw = b""
                    print(f"  raw_hex={_bytes_to_hex(raw, 120)}")
                    try:
                        print(f"  utf16be_try={raw.decode('utf-16-be', errors='ignore')!r}")
                    except Exception:
                        pass
            else:  # TJ
                # TJ 是数组：字符串片段 + kerning 数字
                arr = operands[0] if operands else []
                for j, item in enumerate(arr):
                    if hasattr(item, "__bytes__"):
                        raw = bytes(item)
                        print(f"  part[{j}] raw_hex={_bytes_to_hex(raw, 80)}")
            if shown >= 60:
                print("  ...仅显示前 60 条 Tj/TJ")
                break
        if shown == 0:
            print("未发现 Tj/TJ 文本绘制指令（可能是图形化文本）。")
        print()


def _print_font_resource_diagnostics(pdf_path: str, page_no: int) -> None:
    print("=== [D] 字体资源分析（需 pikepdf） ===")
    try:
        import pikepdf  # type: ignore
    except Exception as e:
        print(f"pikepdf 不可用，跳过字体资源分析: {e}")
        print("可安装: pip install pikepdf")
        print()
        return

    with pikepdf.open(pdf_path) as doc:
        if page_no < 1 or page_no > len(doc.pages):
            raise ValueError(f"页码越界: page={page_no}, total={len(doc.pages)}")
        page = doc.pages[page_no - 1]
        resources = page.get("/Resources", None)
        if resources is None:
            print("页面无 /Resources")
            print()
            return
        fonts = resources.get("/Font", None)
        if fonts is None:
            print("页面无 /Font 资源")
            print()
            return

        # fonts 是一个字典：/F1 -> 字体对象
        count = 0
        for font_tag, font_ref in fonts.items():
            count += 1
            try:
                font_obj = font_ref.get_object()
            except Exception:
                font_obj = font_ref

            subtype = str(font_obj.get("/Subtype", ""))
            base_font = str(font_obj.get("/BaseFont", ""))
            encoding = font_obj.get("/Encoding", None)
            to_unicode = font_obj.get("/ToUnicode", None)
            descendant = font_obj.get("/DescendantFonts", None)

            has_to_unicode = to_unicode is not None
            has_encoding = encoding is not None
            has_descendant = descendant is not None

            print(f"[{count}] {font_tag}")
            print(f"  Subtype={subtype} BaseFont={base_font}")
            print(f"  HasEncoding={has_encoding} HasToUnicode={has_to_unicode} HasDescendantFonts={has_descendant}")
            if has_encoding:
                print(f"  Encoding={encoding}")

            if has_to_unicode:
                try:
                    stream_bytes = bytes(to_unicode.read_bytes())
                    print(f"  ToUnicodeStreamBytes={len(stream_bytes)}")
                    print(f"  ToUnicodePreviewHex={_bytes_to_hex(stream_bytes, 80)}")
                except Exception as e:
                    print(f"  ToUnicodeStreamReadError={e}")

            # Type0 字体常见真实信息在 DescendantFonts[0]
            if has_descendant:
                try:
                    d0 = descendant[0].get_object()
                    cid_sys_info = d0.get("/CIDSystemInfo", None)
                    print(f"  DescendantSubtype={d0.get('/Subtype', '')}")
                    print(f"  CIDSystemInfo={cid_sys_info}")
                except Exception as e:
                    print(f"  DescendantParseError={e}")
        if count == 0:
            print("页面 /Font 为空")
        print()


def _import_pymupdf():
    try:
        import pymupdf as fitz_mod  # type: ignore
        return fitz_mod
    except Exception:
        try:
            import fitz as fitz_mod  # type: ignore
            return fitz_mod
        except Exception:
            return None


def _iter_trace_chars(trace):
    chars = trace.get("chars") or []
    for item in chars:
        uni = -1
        gid = -1
        rect = (0.0, 0.0, 0.0, 0.0)
        if isinstance(item, (list, tuple)):
            if len(item) > 0 and isinstance(item[0], int):
                uni = item[0]
            if len(item) > 1 and isinstance(item[1], int):
                gid = item[1]
            if len(item) > 3 and isinstance(item[3], (list, tuple)) and len(item[3]) == 4:
                rect = tuple(float(x) for x in item[3])
        elif isinstance(item, dict):
            uni = int(item.get("c", -1))
            gid = int(item.get("gid", -1))
            b = item.get("bbox") or (0, 0, 0, 0)
            if isinstance(b, (list, tuple)) and len(b) == 4:
                rect = tuple(float(x) for x in b)
        yield uni, gid, rect


def _rect_hit(char_rect, label_rect):
    x0, y0, x1, y1 = char_rect
    return (
        x0 >= float(label_rect.x1) + 1.0
        and y1 >= float(label_rect.y0) - 3.0
        and y0 <= float(label_rect.y1) + 10.0
    )


def _dump_gid_sequence_for_label(pdf_path: str, page_no: int, label: str):
    fitz_mod = _import_pymupdf()
    if not fitz_mod:
        print("PyMuPDF 不可用，跳过 gid 序列导出。")
        return []
    out = []
    with fitz_mod.open(pdf_path) as doc:
        if page_no < 1 or page_no > len(doc):
            raise ValueError(f"页码越界: page={page_no}, total={len(doc)}")
        page = doc[page_no - 1]
        rects = page.search_for(label)
        if not rects:
            print(f"未在页面定位到标签: {label}")
            return []
        traces = page.get_texttrace() or []
        for tr in traces:
            font = str(tr.get("font", ""))
            for uni, gid, rect in _iter_trace_chars(tr):
                if gid < 0:
                    continue
                if not any(_rect_hit(rect, lr) for lr in rects):
                    continue
                out.append((font, uni, gid))
    return out


def _print_gid_diagnostics(pdf_path: str, page_no: int, label: str, expected: str = "") -> None:
    print("=== [E] PyMuPDF texttrace gid 序列 ===")
    seq = _dump_gid_sequence_for_label(pdf_path, page_no, label)
    if not seq:
        print("gid_seq=<empty>")
        print()
        return
    # 先打印前 120 个
    for i, (font, uni, gid) in enumerate(seq[:120], start=1):
        uni_ch = chr(uni) if 32 <= uni <= 126 else ""
        print(f"{i:03d} font={font} uni={uni} ch={uni_ch!r} gid=0x{gid:04x}")
    if len(seq) > 120:
        print("...仅显示前 120 个 gid")

    # 自动生成候选映射：使用不可读位（uni=0 或 uni=65533）与 expected 对齐
    expected_digits = re.sub(r"\D+", "", expected or "")
    if expected_digits:
        null_gids = [(font, gid) for (font, uni, gid) in seq if uni in (0, 65533)]
        print(f"\nexpected_digits={expected_digits}")
        print(f"null_gids_count={len(null_gids)}")
        n = len(expected_digits)
        if len(null_gids) >= n:
            # 动态对齐：允许在窗口内跳过 0~2 个噪声 gid，挑选冲突最少的路径
            best = None
            best_cost = 10**9
            max_skip = 2
            for skip in range(0, max_skip + 1):
                wlen = n + skip
                if len(null_gids) < wlen:
                    continue
                for off in range(0, len(null_gids) - wlen + 1):
                    window = null_gids[off: off + wlen]
                    skip_idx_sets = [()] if skip == 0 else combinations(range(wlen), skip)
                    for skip_idx in skip_idx_sets:
                        kept = [window[i] for i in range(wlen) if i not in set(skip_idx)]
                        if len(kept) != n:
                            continue
                        mapping = {}
                        conflict = 0
                        for (font, gid), d in zip(kept, expected_digits):
                            mapping.setdefault(font, {})
                            key = f"{gid:04x}"
                            if key in mapping[font] and mapping[font][key] != d:
                                conflict += 1
                            mapping[font][key] = d
                        # 成本：先看冲突，再尽量少跳过，再偏好更短偏移
                        cost = conflict * 100 + skip * 10 + off
                        if cost < best_cost:
                            best_cost = cost
                            best = {
                                "off": off,
                                "skip": skip,
                                "skip_idx": tuple(skip_idx),
                                "conflict": conflict,
                                "mapping": mapping,
                                "path": kept,
                            }
                            if conflict == 0 and skip == 0:
                                break
            if best is not None:
                print(
                    f"best_path: offset={best['off']}, skip={best['skip']}, "
                    f"skip_idx={best['skip_idx']}, conflict={best['conflict']}"
                )
                # 输出最优路径（gid -> expected digit）
                print("best_alignment_pairs:")
                for i, ((font, gid), d) in enumerate(zip(best["path"], expected_digits), start=1):
                    print(f"  {i:02d}. {font} gid=0x{gid:04x} -> {d}")
                mapping = best["mapping"]
                print("candidate_glyph_map (请人工复核后写入 code_map.json):")
                print("{")
                for font, mp in mapping.items():
                    print(f'  "{font}": {{')
                    for k, v in mp.items():
                        print(f'    "{k}": "{v}",')
                    print("  },")
                print("}")
                if best["conflict"] > 0:
                    print("WARNING: 候选映射存在冲突，请手工校验。")
        else:
            print("null_gids 数量不足，无法自动对齐 expected。")
    print()


def main() -> None:
    raise SystemExit(
        "scripts/diagnose_pdf_text_cid.py 已禁用。"
        "如需重新启用，请在代码中移除此退出逻辑。"
    )

    p = argparse.ArgumentParser(description="诊断 PDF 文本层/CID 问题")
    p.add_argument("--pdf", required=True, help="PDF 文件路径")
    p.add_argument("--page", type=int, default=1, help="页码（从 1 开始）")
    p.add_argument("--label", default="收款人账号", help="要定位的标签文本")
    p.add_argument("--expected", default="", help="可选：该标签期望账号（用于自动生成候选映射）")
    args = p.parse_args()

    print(f"PDF={args.pdf}")
    print(f"PAGE={args.page}, LABEL={args.label}")
    print()

    _print_text_diagnostics(args.pdf, args.page, args.label)
    _print_raw_stream_diagnostics(args.pdf, args.page)
    _print_font_resource_diagnostics(args.pdf, args.page)
    _print_gid_diagnostics(args.pdf, args.page, args.label, args.expected)


if __name__ == "__main__":
    main()
