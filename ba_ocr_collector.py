# -*- coding: utf-8 -*-
"""确定性采集器 v5（极速版）：角标锚定 + mss 截图 + ctypes 点击 + 半分辨率条带 OCR
速度：约 2.6 格/秒，全列表 ~3 分钟。
用法: python ba_ocr_collector.py collect   （用 本地识图OCR\\ocr-venv 的 python 运行）
停止: 运行中在控制台按 Ctrl+C；连续 2 轮零新增自动停止。
安全: 点击事件发往当前前台窗口，运行期间必须保持游戏前台、不要动鼠标。
"""
import ctypes
import json
import os
import re
import time

import mss

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

user32 = ctypes.windll.user32
SCT = mss.mss()


def fast_click(x, y):
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(2, 0, 0, 0, 0)   # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)   # LEFTUP


def fast_wheel(notches):
    """notches 负值 = 向下滚动"""
    user32.mouse_event(0x0800, 0, 0, int(notches * 120), 0)


TEMP = os.path.dirname(os.path.abspath(__file__))
ROWS_OUT = os.path.join(TEMP, "rows2.json")
ENGINE = RapidOCR()

COLS = [1468, 1708, 1947, 2185, 2425]      # 兼容保留（锚定模式不依赖）
GRID = (1300, 340, 2530, 1460)             # 网格面板区域（屏幕坐标）
GRID_M = {"left": GRID[0], "top": GRID[1], "width": GRID[2] - GRID[0], "height": GRID[3] - GRID[1]}
BADGE2CELL = (-55, -56)                    # 角标中心 → 格子中心（实测）
NAME_M = {"left": 60, "top": 1100, "width": 1190, "height": 190}  # 名称横幅 + 持有数量（固定 UI 位置）
CELL_HALF = (60, 66)                       # 格子图标区（空格预检用）
SKIP = {"道具", "持有數量", "持有数量", "獲得處", "获得处"}
CLICK_SLEEP = 0.03                         # 实测点击抬起后游戏立即渲染，30ms 过渡保护
GRID_CENTER = (1950, 900)


def ocr(img):
    """img: BGR ndarray 或 PIL Image"""
    if isinstance(img, np.ndarray):
        arr = img
    else:
        arr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    res, _ = ENGINE(arr)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append({"box": it[0], "text": it[1]})
    return out


def has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def parse_left(img):
    lines = [it["text"].strip() for it in ocr(img) if it["text"].strip()]
    count, count_idx = None, -1
    for i, l in enumerate(lines):
        m = re.fullmatch(r"[xX×]?\s*(\d[\d,]*)", l)
        if m:
            count = int(m.group(1).replace(",", ""))
            count_idx = i
            break
    parts = []
    for i, l in enumerate(lines if count_idx < 0 else lines[:count_idx]):
        if not has_cjk(l) or any(k in l for k in SKIP) or "數量" in l or "数量" in l or "持有" in l:
            continue
        parts.append(l)
    return ("".join(parts) if parts else None), count


def grab_bgr(region):
    raw = np.asarray(SCT.grab(region), dtype=np.uint8)   # BGRA
    return np.ascontiguousarray(raw[:, :, :3])           # BGR


def grab_gray_strip():
    raw = grab_bgr(GRID_M)
    return cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY).astype(np.int16)


def measure_shift(a, b):
    best, best_ssd = 0, None
    for s in range(10, 500, 2):
        if s >= a.shape[0] - 10:
            break
        ssd = np.mean((a[s:, :] - b[:-s, :]) ** 2)
        if best_ssd is None or ssd < best_ssd:
            best_ssd, best = ssd, s
    return best


def cell_occupied(grid_bgr, cx, cy):
    """空格子图标区方差极低，跳过以省时。cx/cy 为屏幕坐标，grid_bgr 原点在 GRID。"""
    x0 = int(cx - GRID[0] - CELL_HALF[0])
    y0 = int(cy - GRID[1] - CELL_HALF[1])
    h, w = grid_bgr.shape[:2]
    if x0 < 0 or y0 < 0 or y0 + CELL_HALF[1] * 2 > h or x0 + CELL_HALF[0] * 2 > w:
        return True
    cell = cv2.cvtColor(grid_bgr[y0:y0 + CELL_HALF[1] * 2, x0:x0 + CELL_HALF[0] * 2],
                        cv2.COLOR_BGR2GRAY)
    return cell.std() > 18


def main():
    rows = []
    rows_fp = os.path.join(TEMP, "rows2.json")
    if os.path.exists(rows_fp):
        rows = [tuple(r) for r in json.load(open(rows_fp, encoding="utf-8"))]
    known = set(rows)
    known_names = {re.sub(r"\s+", "", n) for n, _ in rows}

    def norm_key(s):
        s = re.sub(r"\s+", "", s)
        return s

    def is_known_name(name):
        from difflib import get_close_matches
        k = norm_key(name)
        if k in known_names:
            return True
        close = get_close_matches(k, list(known_names), n=1, cutoff=0.85)
        return bool(close)

    def record(name, count):
        if name and count is not None:
            row = (str(name), int(count))
            if row in known:
                return False
            if is_known_name(name):
                known_names.add(norm_key(name))
                known.add(row)
                rows.append(row)
                return True
            known_names.add(norm_key(name))
            known.add(row)
            rows.append(row)
            return True
        return False

    print("scroll to top...", flush=True)
    for _ in range(8):
        fast_wheel(6)
        time.sleep(0.3)
    time.sleep(0.5)

    # 标定滚动：每 2 格滚轮的像素位移 → 换算每屏（约 4.1 行）需要的格数
    a = grab_gray_strip()
    fast_wheel(-2)
    time.sleep(0.6)
    b = grab_gray_strip()
    s2 = measure_shift(a, b)
    a = b
    fast_wheel(-2)
    time.sleep(0.6)
    b = grab_gray_strip()
    s4 = measure_shift(a, b) + s2
    per_notch = max(1, round((s4 - s2) / 2))
    notches_per_screen = max(1, round(410 * 2 / per_notch))  # 行高≈205px
    print(f"calib: 2notch={s2}px per_notch={per_notch}px -> {notches_per_screen} notches/screen",
          flush=True)

    empty_rounds = 0
    for screen in range(1, 40):
        t0 = time.time()
        grid_bgr = grab_bgr(GRID_M)
        # 角标重锚定：每屏用角标实际位置反推格子坐标，根治滚动累积漂移
        badges = [b for b in ocr(grid_bgr)
                  if re.fullmatch(r"[xX×]?\s*\d[\d,]*", b["text"].strip())]
        # 行聚类 + 5 列网格填充：角标漏检的格子也强制点击，保证全覆盖
        ys_sorted = sorted(sum(q[1] for q in b["box"]) / 4 + BADGE2CELL[1] for b in badges)
        rows_y = []
        for y in ys_sorted:
            if not rows_y or y - rows_y[-1] > 60:
                rows_y.append(y)
        targets = [(col, ry + GRID[1]) for ry in rows_y for col in COLS]
        new_here = 0
        for sx, sy in targets:
            if not cell_occupied(grid_bgr, sx, sy):
                continue
            fast_click(sx, sy)
            time.sleep(CLICK_SLEEP)
            name, count = parse_left(grab_bgr(NAME_M))   # 半分辨率识别
            if count is None or not name:                # 失败补拍重试一次
                time.sleep(0.15)
                name, count = parse_left(grab_bgr(NAME_M))
            if record(name, count):
                new_here += 1
        json.dump([list(x) for x in rows], open(rows_fp, "w", encoding="utf-8"),
                  ensure_ascii=False)
        print(f"screen {screen}: cells={len(targets)} new={new_here} rows={len(rows)} "
              f"({time.time()-t0:.1f}s)", flush=True)

        if new_here == 0:
            empty_rounds += 1
        else:
            empty_rounds = 0
        if empty_rounds >= 2:
            print("bottom reached")
            break
        fast_wheel(-notches_per_screen)
        time.sleep(0.5)

    json.dump([list(x) for x in rows], open(rows_fp, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"done: {len(rows)} rows", flush=True)


if __name__ == "__main__":
    main()
