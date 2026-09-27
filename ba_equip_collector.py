# -*- coding: utf-8 -*-
"""BA 裝備（装备设计图）页面采集器：固定网格点击 + 左侧名称/数量 OCR
用法: python ba_equip_collector.py collect   （用 本地识图OCR\\ocr-venv 的 python 运行）
输出: equip_rows.json [[TW名称, 数量], ...]
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
ENGINE = RapidOCR()

TEMP = os.path.dirname(os.path.abspath(__file__))
ROWS_OUT = os.path.join(TEMP, "equip_rows.json")

# 裝備页面几何（1280x800 游戏窗口位于主屏左上，坐标 = 窗口内 ×1）
COLS = [1506, 1742, 1978, 2214, 2450]
ROWS = [450, 654, 860, 1064, 1268]
NAME_STRIP = (60, 1290, 1000, 1415)     # 名称横幅
COUNT_STRIP = (1010, 1275, 1250, 1400)  # 持有數量 xN
GRID_CENTER = (1978, 860)
CLICK_SLEEP = 0.03
SKIP_EXACT = {"強化石", "强化石", "主能力值", "攻擊力", "攻击力", "裝備", "装备",
              "獲得處", "获得处", "手錶", "手表", "帽子", "手套", "鞋子", "書包",
              "书包", "項鍊", "项链", "徽章", "髮夾", "发夹", "護身符", "护身符",
              "包包", "口袋", "彈簧", "弹簧", "螺絲刀", "螺丝刀"}


def fast_click(x, y):
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(2, 0, 0, 0, 0)   # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)   # LEFTUP


def fast_wheel(notches):
    user32.mouse_event(0x0800, 0, 0, int(notches * 120), 0)


def ocr(region):
    raw = np.asarray(SCT.grab({"left": region[0], "top": region[1],
                               "width": region[2] - region[0],
                               "height": region[3] - region[1]}), dtype=np.uint8)
    img = np.ascontiguousarray(raw[:, :, :3])
    res, _ = ENGINE(img)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append(it[1].strip())
    return [l for l in out if l]


def parse_name(lines):
    for l in lines:
        if any("\u4e00" <= ch <= "\u9fff" for ch in l) and l not in SKIP_EXACT \
                and "持有" not in l and "數量" not in l and "数量" not in l \
                and "主能力" not in l and len(l) >= 2:
            return l
    return None


def parse_count(lines):
    for l in reversed(lines):
        m = re.fullmatch(r"[xX×]?\s*(\d[\d,]*)", l)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def main():
    rows = []
    fp_rows = os.path.join(TEMP, "equip_rows.json")
    if os.path.exists(fp_rows):
        rows = [tuple(r) for r in json.load(open(fp_rows, encoding="utf-8"))]
    known = set(rows)

    def record(name, count):
        if name and count is not None:
            row = (str(name), int(count))
            if row not in known:
                known.add(row)
                rows.append(row)
                return True
        return False

    print("scroll to top...", flush=True)
    for _ in range(10):
        fast_wheel(6)
        time.sleep(0.3)
    time.sleep(0.5)

    empty = 0
    for screen in range(1, 40):
        t0 = time.time()
        new_here = 0
        for ry in ROWS:
            for cx in COLS:
                fast_click(cx, ry)
                time.sleep(CLICK_SLEEP)
                name = parse_name(ocr(NAME_STRIP))
                count = parse_count(ocr(COUNT_STRIP))
                if record(name, count):
                    new_here += 1
        json.dump([list(x) for x in rows], open(fp_rows, "w", encoding="utf-8"),
                  ensure_ascii=False)
        print(f"screen {screen}: new={new_here} rows={len(rows)} ({time.time()-t0:.1f}s)",
              flush=True)
        if new_here == 0:
            empty += 1
        else:
            empty = 0
        if empty >= 2:
            print("bottom reached")
            break
        fast_wheel(-4)
        time.sleep(0.6)
    print(f"done: {len(rows)} rows", flush=True)


if __name__ == "__main__":
    main()
