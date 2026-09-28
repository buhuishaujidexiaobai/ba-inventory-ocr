# -*- coding: utf-8 -*-
"""图标-名称对齐工具：游戏内逐格点击，抓「面板名称 + 格子图标」，
图标比对 SchaleDB 图标库（icons/）得出 Id，产出 icon_links.json。
ba_map_items.py 会加载其中高置信（score≥0.8）链接做精确映射——
专治名字被 OCR 误读到认不出、文本匹配无能为力的行（如 技術肇記/x10K 家族）。

用法: python ba_icon_linker.py   （游戏停在道具或裝備页；运行方式与采集器相同）
依赖: icons/ 图标库（1231 张 SchaleDB webp，可从 schaledb.com/images/item/ 批量获取）
输出: icon_links.json {norm名称: {kind, id, score, top2, name, cn}}
"""
import json
import os
import queue
import threading
import time
from collections import deque

import cv2
import numpy as np

import ba_queue_collector as qc
from ba_map_items import norm

TEMP = qc.TEMP
OUT = os.path.join(TEMP, "icon_links.json")
ICONS_DIR = os.path.join(TEMP, "icons")
ITEMS_JSON = os.path.join(TEMP, "cache", "items.min.json")
EQUIP_JSON = os.path.join(TEMP, "cache", "equipment.min.json")
MIN_SCORE = 0.8                      # ba_map_items 自动应用阈值（打分仅供参考）


def load_refs():
    """icons/ 图标库 → 标准化 64x64 模板矩阵 + (kind, id, cn_name) 元表"""
    src = []
    for kind, fp in (("item", ITEMS_JSON), ("equipment", EQUIP_JSON)):
        with open(fp, encoding="utf-8") as f:
            for iid, v in json.load(f).items():
                src.append((kind, iid, v["Icon"], v["Name"]))
    refs, meta = [], []
    for kind, iid, icon, name in src:
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
        f = (f - f.mean()) / (f.std() + 1e-6)   # 全局标准化，保留颜色差异（区分同形异色票据）
        refs.append(f)
        meta.append((kind, iid, name))
    return np.array(refs), meta


def match_cell(cell, R, meta):
    """格子图标 → [(score1, (kind, id, name)), (score2, ...)] 彩色归一化相关"""
    c = cv2.resize(cell, (64, 64))
    v = c.flatten().astype(np.float32)
    v = (v - v.mean()) / (v.std() + 1e-6)
    corr = R @ v / R.shape[1]
    order = np.argsort(-corr)
    return [(float(corr[i]), meta[i]) for i in order[:2]]


def ocr_lines(eng, png_bytes):
    """条带 PNG → [(text, height)] 行列表"""
    arr = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    res, _ = eng(arr)
    lines = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                ys = [p[1] for p in it[0]]
                lines.append((it[1].strip(), int(max(ys) - min(ys))))
    return [x for x in lines if x[0]]


def main():
    if not os.path.isdir(ICONS_DIR) or len(os.listdir(ICONS_DIR)) < 100:
        print("icons/ 图标库缺失或过少——请先把 SchaleDB 图标 webp 放入 icons/ 再运行", flush=True)
        return
    R, meta = load_refs()
    print(f"refs: {len(R)}", flush=True)

    qc.ENGINE = qc.make_engine(use_dml=False)      # 主线程行检测用 CPU
    hwnd = qc.activate_game()
    if hwnd is None:
        return
    qc.WIN = qc.window_rect(hwnd)
    geo = None
    for attempt in range(6):
        geo = qc.autocalib()
        if geo is not None:
            break
        if qc.dialog_open_full():
            print(f"autocalib retry {attempt+1}/6: 检测到弹窗，发送 ESC 关闭...", flush=True)
            qc.send_esc()
            time.sleep(0.8)
            continue
        print(f"autocalib retry {attempt+1}/6: 游戏可能还在加载，10 秒后重试...", flush=True)
        time.sleep(10)
    if geo is None:
        print("autocalib failed after retries", flush=True)
        return
    s = geo["s"]
    cols = geo["cols"]
    row_spacing = geo["row_spacing"]
    gcx, gcy = geo["gcx"], geo["gcy"]
    wh = qc.WIN[3] - qc.WIN[1]
    wide = (0.0, wh - 550.0 * s, cols[0] - 0.45 * geo["spacing"], wh - 150.0 * s)
    ihw, ihh = int(56 * s), int(50 * s)            # 格子图标裁剪半径（同 legacy 扫描器）
    print(f"autocalib: s={s:.3f} panel_right={wide[2]:.0f}", flush=True)

    links = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            links = json.load(f)
        print(f"resume: icon_links.json 已有 {len(links)} 条链接", flush=True)
    links_lock = threading.Lock()
    q = queue.Queue(maxsize=100)
    consumer_engine = qc.make_engine(use_dml=True)
    consumers = [threading.Thread(target=consumer_main, daemon=True,
                                  args=(q, consumer_engine, R, meta, links, links_lock))
                 for _ in range(1 if qc.HAS_DML else 2)]
    for t in consumers:
        t.start()

    # ===== 滚回顶部 =====
    print("scroll to top...", flush=True)
    prev_sig = None
    stable = 0
    for i in range(40):
        qc.fast_wheel(6, gcx, gcy)
        time.sleep(0.4)
        with qc.SCT_LOCK:
            gimg = qc.grab_bgr(tuple(geo["grid_bbox"]))
        sig = qc.strip_hash(gimg)
        if prev_sig is not None and float(np.mean(
                np.abs(sig.astype(np.int16) - prev_sig.astype(np.int16)))) < qc.HASH_THRESH:
            stable += 1
        else:
            stable = 0
        if stable >= 2:
            break
        prev_sig = sig
    time.sleep(0.5)

    # ===== 逐屏遍历：点击 → 条带(名称) + 格子图标 入队 =====
    hash_hist = deque(maxlen=800)
    quiet = 0
    prev_links = len(links)
    for screen in range(1, qc.MAX_SCREENS + 1):
        t0 = time.time()
        with qc.SCT_LOCK:
            grid = qc.grab_bgr(None)
        row_ys, badges, dialog_open = qc.detect_rows(grid, geo)
        if dialog_open and not row_ys:
            print("  dialog detected -> ESC, rescan screen", flush=True)
            qc.send_esc()
            time.sleep(0.8)
            continue
        new_strips = 0
        for ry in row_ys:
            for cx in cols:
                qc.fast_click(cx, ry)
                time.sleep(qc.CLICK_SLEEP)
                with qc.SCT_LOCK:
                    wide_img = qc.grab_bgr(wide)
                h = qc.strip_hash(wide_img)
                if any(float(np.mean(np.abs(h.astype(np.int16) - p.astype(np.int16)))) < qc.HASH_THRESH
                       for p in hash_hist):
                    continue
                hash_hist.append(h)
                x0, y0 = int(cx - ihw), int(ry - ihh)
                cell = grid[y0:y0 + 2 * ihh, x0:x0 + 2 * ihw]
                if cell.size == 0:
                    continue
                ok1, sp = cv2.imencode(".png", wide_img)
                ok2, cp = cv2.imencode(".png", cell)
                q.put((sp.tobytes(), cp.tobytes()))
                new_strips += 1
        q.join()                               # 本屏全部解析完再评估（链接数精确）
        with links_lock:
            n_links = len(links)
        print(f"screen {screen}: rows={len(row_ys)} badges={len(badges)} "
              f"strips={new_strips} links={n_links} ({time.time()-t0:.1f}s)", flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(links, f, ensure_ascii=False, indent=1)
        if n_links == prev_links:
            quiet += 1
        else:
            quiet = 0
        prev_links = n_links
        if quiet >= 2:
            print(f"bottom reached (no new links x{quiet})", flush=True)
            break
        qc.fast_wheel(-qc.SCROLL_NOTCHES, gcx, gcy)
        time.sleep(0.55)

    for _ in range(len(consumers)):
        q.put(None)
    for t in consumers:
        t.join(timeout=60)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=1)
    hi = sum(1 for v in links.values() if v["score"] >= MIN_SCORE)
    print(f"done: {len(links)} 条链接，其中 score≥{MIN_SCORE} 可自动应用 {hi} 条", flush=True)
    low = sorted(links.items(), key=lambda kv: kv[1]["score"])[:8]
    if low:
        print("── 低置信样例（人工核对 icon_links.json 后可删改）──", flush=True)
        for n, v in low:
            print(f"  {v['score']:.3f} {n!r} -> {v['kind']}_{v['id']} {v['cn']}", flush=True)


def consumer_main(q, eng, R, meta, links, links_lock):
    """消费线程：条带 OCR 出名称，格子图标比对出 Id，按名称聚合保留最高分"""
    while True:
        job = q.get()
        if job is None:
            q.task_done()
            return
        strip_png, cell_png = job
        try:
            name, _count = qc.parse_wide(ocr_lines(eng, strip_png))
            cell = cv2.imdecode(np.frombuffer(cell_png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if not name or cell.size == 0:
                q.task_done()
                continue
            (s1, (kind, iid, cn_name)), (s2, _meta2) = match_cell(cell, R, meta)
            key = norm(name)
            if key:
                with links_lock:
                    old = links.get(key)
                    if old is None or s1 > old["score"]:
                        links[key] = {"kind": kind, "id": iid, "score": round(s1, 3),
                                      "top2": round(s2, 3), "name": name, "cn": cn_name}
        except Exception as exc:
            print(f"  [consumer] {exc!r}", flush=True)
        q.task_done()


if __name__ == "__main__":
    main()
