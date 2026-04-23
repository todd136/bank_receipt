"""
PyMuPDF 账号兜底服务：
1) 方案1：按 raw glyph code 映射翻译（基于 texttrace 的 glyph id）；
2) 方案2：PyMuPDF 定位/截图 + ddddocr 识别；
并输出两方案对比结果。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_GLYPH_MAP: Dict[str, Dict[str, str]] = {
    # 该映射可在 base_path/code_map.json 中覆盖；以下为常见样本占位
    "SegoeUI-Semibold": {
        "099a": "1",
        "0999": "0",
        "099f": "6",
        "099b": "2",
        "099c": "3",
        "09a1": "8",
        "099e": "5"
    }
}

_GLYPH_MAP_FILE = "code_map.json"
_GLYPH_LEARNING_FILE = "code_map_learning.json"


def _import_pymupdf() -> Optional[ModuleType]:
    err_pymupdf: Optional[Exception] = None
    try:
        import pymupdf as fitz_mod  # type: ignore
        return fitz_mod
    except Exception as e:
        err_pymupdf = e
        try:
            import fitz as fitz_mod  # type: ignore
            return fitz_mod
        except Exception as e:
            logger.warning(
                "PyMuPDF 不可用，跳过账号兜底: pymupdf_err=%r, fitz_err=%r",
                err_pymupdf,
                e,
            )
            return None


def _normalize_digits(v: str) -> str:
    return re.sub(r"\D+", "", v or "")


def _extract_longest_digits(v: str, min_len: int = 6) -> str:
    if not v:
        return ""
    hits = re.findall(r"\d{%d,}" % min_len, v)
    if not hits:
        return ""
    return max(hits, key=len)


def _extract_all_digit_candidates(v: str, min_len: int = 6) -> List[str]:
    if not v:
        return []
    return re.findall(r"\d{%d,}" % min_len, v)


def _denoise_account_noise(v: str) -> str:
    """
    账号去噪：对 21 位且包含长零串的结果，尝试去掉一位可疑零，优先得到 20 位。
    """
    d = _normalize_digits(v)
    if len(d) != 21 or "0000" not in d:
        return d
    idxs = [i for i, ch in enumerate(d) if ch == "0"]
    if not idxs:
        return d
    cands = [d[:i] + d[i + 1 :] for i in idxs]
    cands.sort(key=lambda x: (len(x) == 20, _score_account(x), len(x)), reverse=True)
    return cands[0] if cands else d


def _score_account(v: str) -> int:
    """
    账号可信度打分：长度越接近常见银行卡/对公账号越高。
    """
    d = _normalize_digits(v)
    if not d:
        return 0
    n = len(d)
    score = n * 10
    if 16 <= n <= 21:
        score += 120
    elif 13 <= n <= 24:
        score += 60
    # 连续重复数字较多时扣分，防止 OCR 噪声
    if re.search(r"(\d)\1{5,}", d):
        score -= 40
    return score


def _edit_distance(a: str, b: str) -> int:
    x = _normalize_digits(a)
    y = _normalize_digits(b)
    n, m = len(x), len(y)
    if n == 0:
        return m
    if m == 0:
        return n
    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, m + 1):
            tmp = dp[j]
            cost = 0 if x[i - 1] == y[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = tmp
    return dp[m]


def _synthesize_zero_insert_candidates(vals: List[str]) -> List[str]:
    """
    当多个候选都接近但有漏 0/错位时，尝试插入 1 个 0 生成新候选。
    """
    out: List[str] = []
    uniq = sorted(set(_normalize_digits(v) for v in vals if _normalize_digits(v)))
    for v in uniq:
        # 重点修复 20 位 -> 21 位
        if len(v) == 20:
            for i in range(1, len(v)):
                out.append(v[:i] + "0" + v[i:])
    return out


def _best_account(candidates: List[str]) -> str:
    vals: List[str] = []
    for x in candidates:
        d = _normalize_digits(x)
        if not d:
            continue
        vals.append(d)
        # 保守策略：不在这里默认注入去噪候选，避免把正确 21 位误压成 20 位
    # 再追加“插 0 修复”候选，解决漏 0 场景
    vals.extend(_synthesize_zero_insert_candidates(vals))
    vals = [v for v in vals if v]
    if not vals:
        return ""
    freq: Dict[str, int] = {}
    for v in vals:
        freq[v] = freq.get(v, 0) + 1
    uniq = list(freq.keys())
    # 加入与原始候选的相似度（编辑距离）作为决策信号
    base = [d for d in (_normalize_digits(c) for c in candidates) if d]
    def _sim_score(v: str) -> int:
        if not base:
            return 0
        return -sum(_edit_distance(v, b) for b in base)

    uniq.sort(key=lambda x: (_score_account(x), _sim_score(x), freq[x], len(x)), reverse=True)
    return uniq[0]


def _align_two(a: str, b: str) -> Tuple[str, str]:
    """
    对齐两个数字串（全局对齐，返回带 '-' 的对齐结果）。
    """
    x = _normalize_digits(a)
    y = _normalize_digits(b)
    n, m = len(x), len(y)
    if not x:
        return "", y
    if not y:
        return x, ""
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = -i
    for j in range(1, m + 1):
        dp[0][j] = -j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = 2 if x[i - 1] == y[j - 1] else -1
            dp[i][j] = max(
                dp[i - 1][j - 1] + s,
                dp[i - 1][j] - 1,
                dp[i][j - 1] - 1,
            )
    i, j = n, m
    ax: List[str] = []
    ay: List[str] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            s = 2 if x[i - 1] == y[j - 1] else -1
            if dp[i][j] == dp[i - 1][j - 1] + s:
                ax.append(x[i - 1])
                ay.append(y[j - 1])
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] - 1:
            ax.append(x[i - 1])
            ay.append("-")
            i -= 1
            continue
        ax.append("-")
        ay.append(y[j - 1])
        j -= 1
    return "".join(reversed(ax)), "".join(reversed(ay))


def _merge_two_accounts(a: str, b: str) -> str:
    """
    合并两个账号候选：
    - 相同位直接保留
    - 冲突位若任一为 0 则优先 0（常见漏/错位）
    - 插入位保留非 '-' 字符
    - 其他冲突位默认偏向第二个序列（便于做双向合并）
    """
    ax, ay = _align_two(a, b)
    if not ax and not ay:
        return ""
    out: List[str] = []
    for ca, cb in zip(ax, ay):
        if ca == cb and ca != "-":
            out.append(ca)
        elif ca == "-":
            out.append(cb)
        elif cb == "-":
            out.append(ca)
        else:
            if ca == "0" or cb == "0":
                out.append("0")
            else:
                out.append(cb)
    return _normalize_digits("".join(out))


def _map_lookup(fmap: Dict[str, str], gid: int) -> str:
    k1 = f"{gid:04x}".lower()
    k2 = str(gid)
    k3 = f"0x{gid:04x}".lower()
    return fmap.get(k1) or fmap.get(k2) or fmap.get(k3) or ""


def _safe_read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _safe_write_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("写入 %s 失败: %s", path.name, e)


def _normalize_map_value(v: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, val in v.items():
        out[str(k).lower()] = str(val)
    return out


def _parse_glyph_map_cfg(data: Dict[str, Any], template_key: str) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {f: dict(mp) for f, mp in _DEFAULT_GLYPH_MAP.items()}
    if not data:
        return merged

    if "global" in data or "templates" in data:
        gm = data.get("global", {})
        if isinstance(gm, dict):
            for font, mp in gm.items():
                if isinstance(mp, dict):
                    merged.setdefault(str(font), {})
                    merged[str(font)].update(_normalize_map_value(mp))
        tm = data.get("templates", {})
        if isinstance(tm, dict) and template_key and template_key in tm:
            one = tm.get(template_key, {})
            if isinstance(one, dict):
                for font, mp in one.items():
                    if isinstance(mp, dict):
                        merged.setdefault(str(font), {})
                        merged[str(font)].update(_normalize_map_value(mp))
        return merged

    # 兼容旧版扁平结构
    for font, mp in data.items():
        if isinstance(mp, dict):
            merged.setdefault(str(font), {})
            merged[str(font)].update(_normalize_map_value(mp))
    return merged


def _best_digit_from_learning(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    pairs = [(str(k), int(v)) for k, v in entry.items() if isinstance(v, int)]
    if not pairs:
        return ""
    pairs.sort(key=lambda x: x[1], reverse=True)
    if len(pairs) > 1 and pairs[0][1] == pairs[1][1]:
        return ""
    return pairs[0][0]


def _load_learning_map(base_path: Optional[str], template_key: str) -> Dict[str, Dict[str, str]]:
    if not base_path:
        return {}
    p = Path(base_path) / _GLYPH_LEARNING_FILE
    data = _safe_read_json(p)
    out: Dict[str, Dict[str, str]] = {}
    if not data:
        return out
    gm = data.get("global", {})
    if isinstance(gm, dict):
        for font, mp in gm.items():
            if isinstance(mp, dict):
                for gid, entry in mp.items():
                    d = _best_digit_from_learning(entry)
                    if d:
                        out.setdefault(str(font), {})[str(gid).lower()] = d
    tm = data.get("templates", {})
    if isinstance(tm, dict) and template_key and template_key in tm:
        one = tm.get(template_key, {})
        if isinstance(one, dict):
            for font, mp in one.items():
                if isinstance(mp, dict):
                    for gid, entry in mp.items():
                        d = _best_digit_from_learning(entry)
                        if d:
                            out.setdefault(str(font), {})[str(gid).lower()] = d
    return out


def _load_glyph_map(base_path: Optional[str], template_key: str) -> Dict[str, Dict[str, str]]:
    cfg = {}
    if base_path:
        cfg = _safe_read_json(Path(base_path) / _GLYPH_MAP_FILE)
    merged = _parse_glyph_map_cfg(cfg, template_key)
    learned = _load_learning_map(base_path, template_key)
    for f, mp in learned.items():
        merged.setdefault(f, {})
        merged[f].update(mp)
    return merged


def _update_learning_map(base_path: Optional[str], template_key: str, runtime_map: Dict[str, Dict[str, str]]) -> None:
    if not base_path or not runtime_map:
        return
    p = Path(base_path) / _GLYPH_LEARNING_FILE
    data = _safe_read_json(p)
    data.setdefault("global", {})
    data.setdefault("templates", {})
    target = data["global"]
    if template_key:
        data["templates"].setdefault(template_key, {})
        target = data["templates"][template_key]
    for font, mp in runtime_map.items():
        target.setdefault(font, {})
        for gid_hex, digit in mp.items():
            gk = str(gid_hex).lower()
            target[font].setdefault(gk, {})
            target[font][gk][str(digit)] = int(target[font][gk].get(str(digit), 0)) + 1
    _safe_write_json(p, data)


def _maybe_promote_learning(base_path: Optional[str], template_key: str, threshold: int = 20) -> None:
    if not base_path:
        return
    lp = Path(base_path) / _GLYPH_LEARNING_FILE
    cp = Path(base_path) / _GLYPH_MAP_FILE
    ld = _safe_read_json(lp)
    if not ld:
        return
    cfg = _safe_read_json(cp)
    if "global" not in cfg and "templates" not in cfg:
        cfg = {"global": cfg if isinstance(cfg, dict) else {}, "templates": {}}
    cfg.setdefault("global", {})
    cfg.setdefault("templates", {})

    sources: List[Tuple[str, Any]] = [("global", ld.get("global", {}))]
    if template_key:
        tm = ld.get("templates", {})
        if isinstance(tm, dict):
            sources.append((template_key, tm.get(template_key, {})))

    changed = False
    for scope, sm in sources:
        if not isinstance(sm, dict):
            continue
        for font, gid_map in sm.items():
            if not isinstance(gid_map, dict):
                continue
            for gid_hex, cnt_map in gid_map.items():
                if not isinstance(cnt_map, dict):
                    continue
                pairs = [(str(d), int(c)) for d, c in cnt_map.items() if isinstance(c, int)]
                if not pairs:
                    continue
                pairs.sort(key=lambda x: x[1], reverse=True)
                if pairs[0][1] < threshold:
                    continue
                if len(pairs) > 1 and pairs[0][1] == pairs[1][1]:
                    continue
                digit = pairs[0][0]
                if scope == "global":
                    cfg["global"].setdefault(font, {})
                    if cfg["global"][font].get(gid_hex) != digit:
                        cfg["global"][font][gid_hex] = digit
                        changed = True
                else:
                    cfg["templates"].setdefault(scope, {})
                    cfg["templates"][scope].setdefault(font, {})
                    if cfg["templates"][scope][font].get(gid_hex) != digit:
                        cfg["templates"][scope][font][gid_hex] = digit
                        changed = True
    if changed:
        _safe_write_json(cp, cfg)


def _rect_hit(char_rect: Tuple[float, float, float, float], label_rect: Any) -> bool:
    x0, y0, x1, y1 = char_rect
    return (
        x0 >= float(label_rect.x1) + 1.0
        and y1 >= float(label_rect.y0) - 3.0
        and y0 <= float(label_rect.y1) + 8.0
    )


def _build_runtime_digit_map(traces: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """
    从同页可读文本中自动学习：font + gid -> digit。
    用于补充手工 glyph map。
    """
    runtime: Dict[str, Dict[str, str]] = {}
    for trace in traces:
        font = str(trace.get("font", ""))
        if not font:
            continue
        for uni, gid, _ in _iter_trace_chars(trace):
            if gid < 0 or not (48 <= uni <= 57):
                continue
            key = f"{gid:04x}".lower()
            runtime.setdefault(font, {})[key] = chr(uni)
    return runtime


def _iter_trace_chars(trace: Dict[str, Any]) -> List[Tuple[int, int, Tuple[float, float, float, float]]]:
    """
    返回 (unicode_codepoint, glyph_id, (x0,y0,x1,y1)) 列表。
    兼容 PyMuPDF 不同版本 texttrace 结构。
    """
    out: List[Tuple[int, int, Tuple[float, float, float, float]]] = []
    chars = trace.get("chars") or []
    for item in chars:
        uni = -1
        gid = -1
        rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        if isinstance(item, (list, tuple)):
            if len(item) > 0 and isinstance(item[0], int):
                uni = item[0]
            if len(item) > 1 and isinstance(item[1], int):
                gid = item[1]
            if len(item) > 3 and isinstance(item[3], (list, tuple)) and len(item[3]) == 4:
                rect = tuple(float(x) for x in item[3])  # type: ignore
        elif isinstance(item, dict):
            uni = int(item.get("c", -1))
            gid = int(item.get("gid", -1))
            b = item.get("bbox") or (0, 0, 0, 0)
            if isinstance(b, (list, tuple)) and len(b) == 4:
                rect = tuple(float(x) for x in b)  # type: ignore
        out.append((uni, gid, rect))
    return out


def _decode_by_glyph_map_for_label(page: Any, label: str, glyph_map: Dict[str, Dict[str, str]]) -> str:
    rects = page.search_for(label)
    if not rects:
        return ""
    try:
        traces = page.get_texttrace() or []
    except Exception:
        return ""
    runtime_map = _build_runtime_digit_map(traces)
    chars: List[str] = []
    unknown_gid: Dict[str, int] = {}
    for trace in traces:
        font = str(trace.get("font", ""))
        fmap = {}
        fmap.update(glyph_map.get(font) or {})
        fmap.update(runtime_map.get(font) or {})
        for uni, gid, rect in _iter_trace_chars(trace):
            if not any(_rect_hit(rect, lr) for lr in rects):
                continue
            if 48 <= uni <= 57:
                chars.append(chr(uni))
                continue
            if uni in (0, 65533) and gid >= 0:
                mapped = _map_lookup(fmap, gid)
                if mapped:
                    chars.append(mapped)
                else:
                    uk = f"{font}:{gid:04x}"
                    unknown_gid[uk] = unknown_gid.get(uk, 0) + 1
    # 方案1也按候选打分，避免拼接噪声
    candidate = _extract_longest_digits("".join(chars))
    if not candidate and unknown_gid and logger.isEnabledFor(logging.DEBUG):
        top = sorted(unknown_gid.items(), key=lambda kv: kv[1], reverse=True)[:8]
        logger.debug("glyph未映射统计(%s): %s", label, top)
    return _best_account([candidate])


def _build_account_clips(page: Any, label: str, next_label: Optional[str]) -> List[Tuple[float, float, float, float]]:
    rects = page.search_for(label)
    if not rects:
        return []
    lr = rects[0]
    clips: List[Tuple[float, float, float, float]] = []
    page_w = float(page.rect.width)
    base_x0 = float(lr.x1) + 1.0
    next_top = None
    if next_label:
        nrs = [r for r in page.search_for(next_label) if float(r.y0) > float(lr.y0)]
        if nrs:
            next_top = float(nrs[0].y0)
    # 更贴近账号单行，减少吃到下一行噪声
    y0 = max(0.0, float(lr.y0) - 3.0)
    y1 = min(float(page.rect.height), float(lr.y1) + 7.0)
    if next_top is not None:
        y1 = min(y1, next_top - 2.0)
    windows = [220.0, 300.0, 380.0, 460.0]
    for w in windows:
        if w <= 10:
            continue
        x0 = base_x0
        x1 = min(page_w - 4.0, x0 + w)
        if x1 > x0 and y1 > y0:
            clips.append((x0, y0, x1, y1))
    # 再加一个略高区域，防止基线偏移
    y0b = max(0.0, y0 - 2.0)
    y1b = min(float(page.rect.height), y1 + 4.0)
    if y1b > y0b:
        clips.append((base_x0, y0b, min(page_w - 4.0, base_x0 + 460.0), y1b))
    return clips


def _ocr_variants(png_bytes: bytes) -> List[bytes]:
    """
    生成 OCR 图像变体：原图 / 放大 / 二值化。
    """
    outs = [png_bytes]
    try:
        from PIL import Image, ImageOps, ImageFilter  # type: ignore

        img = Image.open(BytesIO(png_bytes)).convert("L")
        # 放大提高细字（0）连通性
        up = img.resize((img.width * 3, img.height * 3))
        # 轻度平滑，减少锯齿假笔画
        sm = up.filter(ImageFilter.MedianFilter(size=3))
        # 多阈值二值化
        b1 = sm.point(lambda p: 255 if p > 185 else 0)
        b2 = sm.point(lambda p: 255 if p > 165 else 0)
        # 反相 + 二值，适配浅色细字
        inv_bin = ImageOps.invert(sm).point(lambda p: 255 if p > 170 else 0)

        # “膨胀”近似：先 max filter 再二值，补齐断笔0
        d1 = sm.filter(ImageFilter.MaxFilter(size=3)).point(lambda p: 255 if p > 170 else 0)
        buf1 = BytesIO()
        b1.save(buf1, format="PNG")
        outs.append(buf1.getvalue())
        buf2 = BytesIO()
        b2.save(buf2, format="PNG")
        outs.append(buf2.getvalue())
        buf3 = BytesIO()
        inv_bin.save(buf3, format="PNG")
        outs.append(buf3.getvalue())
        buf4 = BytesIO()
        d1.save(buf4, format="PNG")
        outs.append(buf4.getvalue())
    except Exception:
        pass
    return outs


def _ocr_for_label(page: Any, label: str, next_label: Optional[str], ocr_engine: Any) -> str:
    # 方案2增强：按标签行构造多裁剪窗口 + 多 DPI + 多图像预处理
    clips = _build_account_clips(page, label, next_label)
    if not clips:
        return ""
    candidates: List[str] = []
    dpis = [280, 340, 420]
    for clip in clips:
        for dpi in dpis:
            try:
                pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
                png = pix.tobytes("png")
                for variant in _ocr_variants(png):
                    text = ocr_engine.classification(variant)
                    # 账号至少 10 位，低于阈值不采用，避免短截断误识别
                    cands = _extract_all_digit_candidates(_normalize_digits(text), min_len=10)
                    candidates.extend(cands)
            except Exception:
                continue
    # 结果过短时，再放宽一次（兜底）
    if not candidates:
        for clip in clips:
            try:
                pix = page.get_pixmap(dpi=300, clip=clip, alpha=False)
                text = ocr_engine.classification(pix.tobytes("png"))
                candidates.extend(_extract_all_digit_candidates(_normalize_digits(text), min_len=6))
            except Exception:
                continue
    return _best_account(candidates)


class _OcrProgress:
    """OCR 进度显示（省略号风格日志）。"""

    def __init__(self, total_steps: int):
        self.total = max(1, int(total_steps))
        self.current = 0
        self.start = time.perf_counter()

    def tick(self, stage: str) -> None:
        self.current = min(self.current + 1, self.total)
        pct = int((self.current / self.total) * 100)
        logger.info("OCR进度 %3d%% (%d/%d) %s", pct, self.current, self.total, stage)

    def done(self) -> None:
        logger.info("OCR识别完成，总耗时 %.2fs", time.perf_counter() - self.start)


def _run_with_heartbeat(stage: str, fn):
    """
    执行耗时任务时，每 1 秒输出一次 OCR 心跳日志（. .. ... 循环）。
    """
    stop_event = threading.Event()

    def _beat():
        i = 0
        # 立即输出一次，避免任务<1s时看不到心跳
        logger.info("OCR处理中. %s", stage)
        while not stop_event.wait(1.0):
            i += 1
            dots = "." * ((i - 1) % 3 + 1)
            logger.info("OCR处理中%s %s", dots, stage)

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop_event.set()
        t.join(timeout=0.2)


def recover_accounts_by_dual_strategy(
    pdf_file_path: str,
    base_path: Optional[str] = None,
    template_key: str = "",
) -> Dict[str, str]:
    """
    返回:
      {
        "payer_account": "...",
        "payee_account": "...",
        "scheme1_payer": "...",
        "scheme1_payee": "...",
        "scheme2_payer": "...",
        "scheme2_payee": "...",
      }
    """
    ret = {
        "payer_account": "",
        "payee_account": "",
        "scheme1_payer": "",
        "scheme1_payee": "",
        "scheme2_payer": "",
        "scheme2_payee": "",
    }
    fitz_mod = _import_pymupdf()
    if not fitz_mod:
        return ret

    glyph_map = _load_glyph_map(base_path, template_key)
    ocr_engine = None
    try:
        import ddddocr  # type: ignore
        ocr_engine = ddddocr.DdddOcr(show_ad=False)
        logger.info("OCR引擎已加载，开始方案2识别: %s", Path(pdf_file_path).name)
    except Exception as e:
        logger.warning("ddddocr 不可用，方案2跳过: %s", e)

    try:
        with fitz_mod.open(pdf_file_path) as doc:
            page_count = len(doc)
            ocr_progress = _OcrProgress(total_steps=page_count * 2) if ocr_engine else None
            for page_idx, page in enumerate(doc, start=1):
                if not ret["scheme1_payee"]:
                    ret["scheme1_payee"] = _decode_by_glyph_map_for_label(page, "收款人账号", glyph_map)
                if not ret["scheme1_payer"]:
                    ret["scheme1_payer"] = _decode_by_glyph_map_for_label(page, "付款人账号", glyph_map)
                # 学习池更新：仅记录同页可读数字映射，可靠且可持续累积
                try:
                    traces = page.get_texttrace() or []
                    runtime_map = _build_runtime_digit_map(traces)
                    _update_learning_map(base_path, template_key, runtime_map)
                except Exception:
                    pass
                if ocr_engine:
                    if not ret["scheme2_payee"]:
                        t0 = time.perf_counter()
                        ret["scheme2_payee"] = _run_with_heartbeat(
                            f"第{page_idx}页 收款账号",
                            lambda: _ocr_for_label(page, "收款人账号", "收款人名称", ocr_engine),
                        )
                        logger.info(
                            "OCR页%d 收款账号结果=%r 耗时=%.2fs",
                            page_idx,
                            ret["scheme2_payee"],
                            time.perf_counter() - t0,
                        )
                    if ocr_progress:
                        ocr_progress.tick(f"第{page_idx}页 收款账号")
                    if not ret["scheme2_payer"]:
                        t0 = time.perf_counter()
                        ret["scheme2_payer"] = _run_with_heartbeat(
                            f"第{page_idx}页 付款账号",
                            lambda: _ocr_for_label(page, "付款人账号", "付款人名称", ocr_engine),
                        )
                        logger.info(
                            "OCR页%d 付款账号结果=%r 耗时=%.2fs",
                            page_idx,
                            ret["scheme2_payer"],
                            time.perf_counter() - t0,
                        )
                    if ocr_progress:
                        ocr_progress.tick(f"第{page_idx}页 付款账号")
                if (
                    (ret["scheme1_payee"] or ret["scheme2_payee"])
                    and (ret["scheme1_payer"] or ret["scheme2_payer"])
                ):
                    break
            if ocr_progress:
                ocr_progress.done()
    except Exception as e:
        logger.warning("双方案账号兜底失败: %s", e)
        return ret

    # 最终值：始终基于 s1+s2 候选融合选优，避免“先选一路”错失更优去噪结果。
    payer_candidates = [ret["scheme1_payer"], ret["scheme2_payer"]]
    payee_candidates = [ret["scheme1_payee"], ret["scheme2_payee"]]
    if ret["scheme1_payer"] and ret["scheme2_payer"]:
        payer_candidates.append(_merge_two_accounts(ret["scheme1_payer"], ret["scheme2_payer"]))
        payer_candidates.append(_merge_two_accounts(ret["scheme2_payer"], ret["scheme1_payer"]))
    if ret["scheme1_payee"] and ret["scheme2_payee"]:
        payee_candidates.append(_merge_two_accounts(ret["scheme1_payee"], ret["scheme2_payee"]))
        payee_candidates.append(_merge_two_accounts(ret["scheme2_payee"], ret["scheme1_payee"]))
    ret["payer_account"] = _best_account(payer_candidates)
    ret["payee_account"] = _best_account(payee_candidates)
    _maybe_promote_learning(base_path, template_key, threshold=20)
    return ret
