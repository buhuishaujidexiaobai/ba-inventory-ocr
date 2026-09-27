# -*- coding: utf-8 -*-
"""图标识别扫描器 v6（自适应版）：零点击。角标 OCR 定位+计数，图标查库定身份。
自适应：启动时全屏扫角标，自动推算网格几何（支持全屏/窗口任意大小位置）。
用法:
    python ba_icon_scanner.py calib   # 单屏验证：每格 top2 匹配 + 站点库存对照
    python ba_icon_scanner.py run     # 自动滚动全列表扫描
输出: collected_icons.json {物品Id: 数量}
"""
import ctypes
import json
import os
import re
import sys
import time

import mss

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

user32 = ctypes.windll.user32
SCT = mss.mss()
ENGINE = RapidOCR()

TEMP = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(TEMP, "icons")
ITEMS_JSON = os.path.join(TEMP, "cache", "items.min.json")
EQUIP_JSON = os.path.join(TEMP, "cache", "equipment.min.json")
SITE_INV = os.path.join(TEMP, "site_inventory.json")
OUT = os.path.join(TEMP, "collected_icons.json")

# 全屏基准几何（2560x1600 全屏游戏时标定），窗口模式按 scale 缩放
BASE_CELL_W, BASE_CELL_H = 230, 205
BASE_BADGE_H = 55
BADGE_RE = re.compile(r"[xX×]\s*(\d[\d,]*)")   # 数量角标一定带 x/X 前缀

G = {"GRID": (1300, 340, 2530, 1460), "BADGE2CELL": (-55, -56),
     "ICON_HALF": (56, 50), "scale": 1.0}


def fast_wheel(notches):
    user32.mouse_event(0x0800, 0, 0, int(notches * 120), 0)


def grab_bgr(region=None):
    if region is None:
        m = SCT.monitors[1]                     # 主屏
    else:
        m = {"left": region[0], "top": region[1],
             "width": region[2] - region[0], "height": region[3] - region[1]}
    raw = np.asarray(SCT.grab(m), dtype=np.uint8)
    return np.ascontiguousarray(raw[:, :, :3])


def ocr(img):
    res, _ = ENGINE(img)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append({"box": it[0], "text": it[1].strip()})
    return out


def badge_boxes(img):
    """带 x/X 前缀的数量角标 → [(cx, cy, count, box_h)]"""
    out = []
    for b in ocr(img):
        m = BADGE_RE.fullmatch(b["text"])
        if not m:
            continue
        xs = [q[0] for q in b["box"]]
        ys = [q[1] for q in b["box"]]
        out.append((sum(xs) / 4, sum(ys) / 4,
                    int(m.group(1).replace(",", "")),
                    max(ys) - min(ys)))
    return out


def autocalib():
    """全屏找角标 → 推算网格几何。返回 False 表示找不到游戏界面。"""
    full = grab_bgr(None)
    badges = badge_boxes(full)
    if len(badges) < 6:
        return False
    cxs = sorted(b[0] for b in badges)
    cys = sorted(b[1] for b in badges)

    def cluster(vals, gap):
        groups = [[vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][-1] <= gap:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [sum(g) / len(g) for g in groups]

    # 先用近似间隔聚类出列/行中心，列间距=实测格宽，行间距=实测格高
    col_c = cluster(cxs, 120)
    row_c = cluster(cys, 110)
    if len(col_c) < 3 or len(row_c) < 2:
        return False
    col_d = np.diff(sorted(col_c))
    row_d = np.diff(sorted(row_c))
    cell_w = float(np.median(col_d))
    cell_h = float(np.median(row_d))

    def cluster2(vals, gap):
        vals = sorted(vals)
        groups = [[vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][-1] <= gap:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [sum(g) / len(g) for g in groups]

    col_c = cluster2(cxs, cell_w * 0.5)
    row_c = cluster2(cys, cell_h * 0.5)
    if len(col_c) < 3 or len(row_c) < 2:
        return False

    # 锚定偏移实测：每格的角标中心相对其列/行中心的偏移取中位数
    dxs, dys = [], []
    for b in badges:
        c = min(col_c, key=lambda cc: abs(cc - b[0]))
        r = min(row_c, key=lambda rr: abs(rr - b[1]))
        dxs.append(c - b[0])
        dys.append(r - b[1])
    G["GRID"] = (int(min(col_c) - cell_w * 0.75), int(min(row_c) - cell_h * 0.78),
                 int(max(col_c) + cell_w * 0.55), int(max(row_c) + cell_h * 0.50))
    G["BADGE2CELL"] = (float(np.median(dxs)), float(np.median(dys)))
    G["ICON_HALF"] = (int(cell_w * 0.42), int(cell_h * 0.40))
    G["scale"] = cell_w / BASE_CELL_W
    print(f"autocalib: badges={len(badges)} "
          f"cell={cell_w:.0f}x{cell_h:.0f} grid={G['GRID']}", flush=True)
    return True


def grab_gray_strip():
    raw = grab_bgr(G["GRID"])
    return cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY).astype(np.int16)


def measure_shift(a, b):
    best, best_ssd = 0, None
    for s in range(10, int(a.shape[0] * 0.9), 2):
        ssd = np.mean((a[s:, :] - b[:-s, :]) ** 2)
        if best_ssd is None or ssd < best_ssd:
            best_ssd, best = ssd, s
    return best


def load_refs():
    """参考库：物品 + 装备图标 → 64x64 彩色标准化模板，meta 存完整键名"""
    src = []
    for v in json.load(open(ITEMS_JSON, encoding="utf-8")).values():
        src.append(("item_" + str(v["Id"]), v["Icon"], v["Name"]))
    for v in json.load(open(EQUIP_JSON, encoding="utf-8")).values():
        src.append(("equipment_" + str(v["Id"]), v["Icon"], v["Name"]))
    refs, meta = [], []
    for key, icon, name in src:
        fp = os.path.join(ICONS_DIR, icon + ".webp")
        if not os.path.exists(fp) or os.path.getsize(fp) < 500:
            continue
        data = np.fromfile(fp, dtype=np.uint8)   # cv2.imread 不支持中文路径
        im = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if im is None:
            continue
        if im.ndim == 3 and im.shape[2] == 4:
            a = im[:, :, 3:4] / 255.0
            im = (im[:, :, :3] * a + 255 * (1 - a)).astype(np.uint8)  # 透明区合成白底
        im = cv2.resize(im, (64, 64))
        f = im.flatten().astype(np.float32)
        f = (f - f.mean()) / (f.std() + 1e-6)   # 全局标准化，保留通道间颜色差异（区分同形异色票据）
        refs.append(f)
        meta.append((key, name))
    return np.array(refs), meta


def match_cell(cell_bgr, R, meta):
    c = cv2.resize(cell_bgr, (64, 64))
    v = c.flatten().astype(np.float32)
    v = (v - v.mean()) / (v.std() + 1e-6)
    corr = R @ v / R.shape[1]                          # 彩色归一化相关
    order = np.argsort(-corr)
    return [(float(corr[i]),) + meta[i] for i in order[:2]]


def scan_screen(R, meta, site_inv, results):
    GRID = G["GRID"]
    ihw, ihh = G["ICON_HALF"]
    grid = grab_bgr(GRID)
    badges = badge_boxes(grid)
    report = []
    for bcx, bcy, cnt, _bh in badges:
        gx = bcx + G["BADGE2CELL"][0]          # 网格裁剪图内的图标中心
        gy = bcy + G["BADGE2CELL"][1]
        x0, y0 = int(gx - ihw), int(gy - ihh)
        cell = grid[y0:y0 + ihh * 2, x0:x0 + ihw * 2]
        if cell.size == 0:
            continue
        top = match_cell(cell, R, meta)
        (s1, iid1, nm1), (s2, iid2, nm2) = top
        exp = site_inv.get(iid1) if site_inv else None
        gt_ok = (exp == cnt) if site_inv else None
        if cnt > 0:
            results[iid1] = max(results.get(iid1, 0), cnt)
        report.append((cnt, iid1, nm1, round(s1, 3), round(s2, 3), exp, gt_ok))
    return report


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "calib"
    R, meta = load_refs()
    print(f"refs: {len(R)}", flush=True)

    if not autocalib():
        print("未找到游戏道具网格：请确认游戏显示在主屏前台、停在道具页面。", flush=True)
        return
    site_inv = {}
    if os.path.exists(SITE_INV):
        site_inv = json.load(open(SITE_INV, encoding="utf-8"))

    results = {}
    out_fp = os.path.join(TEMP, "collected_icons.json")
    if os.path.exists(out_fp):
        results = json.load(open(out_fp, encoding="utf-8"))

    if mode == "calib":
        report = scan_screen(R, meta, site_inv, results)
        print(f"{'数量':>7} {'top1 Id':>8} {'score1':>7} {'score2':>7} {'站点值':>8}  名称")
        for cnt, iid1, nm1, s1, s2, exp, ok in report:
            mark = "" if ok is None else ("  [站点一致]" if ok else "  [与站点不符]")
            print(f"{cnt:>7} {iid1:>8} {s1:>7.3f} {s2:>7.3f} {str(exp):>8}  {nm1}{mark}")
        json.dump(results, open(out_fp, "w", encoding="utf-8"), ensure_ascii=False)
        return

    print("scroll to top...", flush=True)
    for _ in range(8):
        fast_wheel(6)
        time.sleep(0.3)
    time.sleep(0.5)

    # 滚动标定（网格内灰度条带，2 格滚轮的位移 → 每屏滚约 4.1 行）
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
    cell_h = BASE_CELL_H * G["scale"]
    notches = max(1, round(cell_h * 4.1 * 2 / per_notch))
    print(f"calib: 2notch={s2}px per_notch={per_notch}px -> {notches} notches/screen(4.1行)", flush=True)

    seen_ids = set()
    empty_rounds = 0
    for screen in range(1, 40):
        t0 = time.time()
        report = scan_screen(R, meta, site_inv, results)
        json.dump(results, open(out_fp, "w", encoding="utf-8"), ensure_ascii=False)
        cur_ids = set(r[1] for r in report)
        print(f"screen {screen}: cells={len(report)} new={len(cur_ids - seen_ids)} "
              f"items={len(results)} ({time.time()-t0:.1f}s)", flush=True)
        # 到底判定：本轮扫到的 Id 全部都已见过（无新增）连续 2 轮
        if cur_ids and cur_ids <= seen_ids:
            empty_rounds += 1
        else:
            empty_rounds = 0
        seen_ids |= cur_ids
        if empty_rounds >= 2:
            print("bottom reached")
            break
        fast_wheel(-notches)
        time.sleep(0.5)
    json.dump(results, open(out_fp, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"done: {len(results)} items", flush=True)


if __name__ == "__main__":
    main()
