# -*- coding: utf-8 -*-
"""队列版采集器 v10（确定性点阵遍历）：
- 自标定：全屏角标 → 行/列聚类（过滤左侧面板孤行）→ 列中心列表 cols + 缩放 s（一次性）
- 点击目标与识别解耦：角标 OCR 只用于标定与每屏行检测；
  每屏点击目标 = cols × 行中心 全组合，行优先遍历，一格一次（不漏、不乱、不重）
- 空格子无副作用：点击后左侧面板不变 → 条带与上一格相同 → 哈希去重跳过 OCR
- 条带哈希去重：重叠行/空格重复条带不入队，消费者只 OCR 新内容
- 到底判定（覆盖率口径）：同一页连续 ≥2 屏、且页内每个被点击的格子都产出有效读数/
  确认重复/重试上限后放弃 → 本页构造性完整即停；兜底：排空后连续 3 轮零新增、
  或连续 2 屏无角标
- 滚轮：先 SetCursorPos 到网格质心，确保滚动作用于游戏列表
- 名称条带与截图区域均锚定游戏窗口矩形（GetWindowRect），支持任意窗口位置/分辨率
用法: python ba_queue_collector.py [--fresh]
  --fresh  忽略旧 rows2.json 重新扫描（旧文件自动备份）
输出: rows2.json [[TW名称, 数量], ...]（与 ba_map_items.py 兼容，跨页累积）
      扫描结束后自动调用 ba_map_items 生成 输出\什亭之匣库存导入_OCR采集.json
日志: 采集日志.txt（控制台输出同步落盘）
"""
import argparse
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque

import mss

import cv2
import numpy as np
import onnxruntime as ort
import rapidocr_onnxruntime.utils as r_utils
from rapidocr_onnxruntime import RapidOCR

# DirectML GPU 加速适配（若检测到 DirectML 则自动启用显卡推理）
HAS_DML = "DmlExecutionProvider" in ort.get_available_providers()

def make_engine(use_dml=False):
    """构建独立 RapidOCR 引擎：主线程走 CPU（防 DirectX 争用），消费线程走 GPU（极速推理）"""
    sess_opt = ort.SessionOptions()
    sess_opt.log_severity_level = 4
    sess_opt.enable_cpu_mem_arena = False
    sess_opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    ep_list = []
    if use_dml and HAS_DML:
        ep_list.append(("DmlExecutionProvider", {"device_id": 0}))
    ep_list.append(("CPUExecutionProvider", {}))

    _orig_init = r_utils.OrtInferSession.__init__
    def _custom_init(self, config):
        self._verify_model(config["model_path"])
        self.session = ort.InferenceSession(config["model_path"], sess_options=sess_opt, providers=ep_list)
    r_utils.OrtInferSession.__init__ = _custom_init

    eng = RapidOCR()
    r_utils.OrtInferSession.__init__ = _orig_init
    return eng

if HAS_DML:
    print("[GPU] DirectML 加速已激活 (DirectX 12 / NVIDIA RTX)", flush=True)
else:
    print("[CPU] 未检测到 DirectML 支持，使用默认 CPU 模式", flush=True)

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()   # 与 mss 的 DPI 感知对齐：窗口/光标/截图坐标统一为物理像素
SCT = mss.mss()
SCT_LOCK = threading.Lock()
ENGINE = None                 # 主线程 CPU 引擎（自标定 + 每屏行检测），main() 里创建
WIN = None                    # 游戏窗口矩形 (left, top, right, bottom)，main() 里定位

TEMP = os.path.dirname(os.path.abspath(__file__))
ROWS_OUT = os.path.join(TEMP, "rows2.json")

BASE_COL_SPACING = 239.0
BASE_ROW_SPACING = 205.0
# 角标/面板数量：游戏对 ≥10000 缩写为 10K / 1.2K / 1M。小数点仅在带 K/M 后缀时
# 合法（防 OCR 把千分位逗号误读成小数点，如 x1,234 → x1.234，此时整体拒配）。
# 分组：1=整数位, 2=小数位(带K/M分支), 3=字母(带K/M分支), 4=字母(整数直连分支)
_NUM_PART = r"(\d[\d,]*)(?:\.(\d+)([KkMm])|([KkMm]))?"
BADGE_RE = re.compile(r"[xX×]\s*" + _NUM_PART)      # 角标一定带 x/X 前缀
COUNT_RE = re.compile(r"[xX×]?\s*" + _NUM_PART)     # 面板持有数量行可不带前缀
SCHOOL_RE = re.compile(r"[（(]([^（）()]{1,8})[）)]")  # 名称横幅中的学校后缀（（三一）/（狂獵）等）


def scaled_value(m):
    """BADGE_RE/COUNT_RE 的 match → 数量。K/M 缩写会截断末位（10K=10000~10049），
    仅用于行定位与兜底；精确数量以面板读数为准。"""
    int_part, dec, letter_a, letter_b = m.groups()
    num = float((int_part + ("." + dec if dec else "")).replace(",", ""))
    return int(round(num * {"K": 1e3, "M": 1e6}.get((letter_a or letter_b or "").upper(), 1)))
CLICK_SLEEP = 0.03
SCROLL_NOTCHES = 4                   # 实测 2 格 ≈ 2.2 行 → 4 格 ≈ 4.4 行（<5 行窗口）
SCROLL_POLL = 0.2                    # 滚动后停稳轮询步长（秒）
SCROLL_POLL_MAX = 10                 # 停稳轮询上限（×0.2s）。到底后滚轮会触发回弹动画，
                                     # 固定等待（0.55s/1.0s 都实测过）偶尔截到弹跳中的帧，
                                     # 页哈希误判换页 + 点击坐标随位移偏移，只能轮询等停稳
SKIP_EXACT = {"道具", "持有數量", "持有数量", "主能力值", "攻擊力", "攻击力"}
WIDE = (0, 1050, 1300, 1450)         # 左侧信息条带（名称横幅+持有數量）；main() 按游戏窗口矩形与缩放重算
BADGE_CLICK_DY = 56.0                # 角标在格子中心下方 56px*s 处
HASH_THRESH = 0.2                    # 条带缩略图 mean|diff| 判重阈值（面板静态，同内容 diff≈0）
MAX_SCREENS = 80
RETRY_LIMIT = 3                      # 台账中单格读取失败的重试上限，超过后放弃（giveup）
# 弹窗关键词（顯示設定/設定对话框等）：同屏命中 ≥2 个才判定为弹窗，避免物品名误伤
DIALOG_KEYS = ("顯示設定", "显示设定", "全部重置", "過濾器", "过滤器",
               "確認", "取消", "關閉", "关闭")


def send_esc():
    """关闭游戏内弹窗（BA 的对话框 Esc=取消/关闭，未确认的改动会被丢弃）"""
    user32.keybd_event(0x1B, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(0x1B, 0, 2, 0)


def fast_click(x, y):
    # 内部坐标均为窗口相对坐标，发往系统时加窗口原点
    user32.SetCursorPos(int(x + WIN[0]), int(y + WIN[1]))
    user32.mouse_event(2, 0, 0, 0, 0)
    user32.mouse_event(4, 0, 0, 0, 0)


def fast_wheel(notches, x, y):
    user32.SetCursorPos(int(x + WIN[0]), int(y + WIN[1]))
    time.sleep(0.05)
    user32.mouse_event(0x0800, 0, 0, int(notches * 120), 0)


def grab_bgr(region=None):
    if region is None:
        m = {"left": WIN[0], "top": WIN[1],
             "width": WIN[2] - WIN[0], "height": WIN[3] - WIN[1]}   # 游戏窗口区域
    else:
        m = {"left": int(WIN[0] + region[0]), "top": int(WIN[1] + region[1]),
             "width": int(region[2] - region[0]), "height": int(region[3] - region[1])}
    raw = np.asarray(SCT.grab(m), dtype=np.uint8)
    return np.ascontiguousarray(raw[:, :, :3])


def ocr(arr):
    res, _ = ENGINE(arr)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append({"box": it[0], "text": it[1].strip()})
    return out


def xbadges(arr):
    out = []
    for b in ocr(arr):
        m = BADGE_RE.fullmatch(b["text"])
        if not m:
            continue
        xs = [q[0] for q in b["box"]]
        ys = [q[1] for q in b["box"]]
        out.append((sum(xs) / 4, sum(ys) / 4, scaled_value(m)))
    return out


def cluster_vals(vals, gap):
    vals = sorted(vals)
    if not vals:
        return []
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def autocalib():
    """全屏角标 → 行/列聚类 → 点阵节奏与原点。失败返回 None。"""
    badges = xbadges(grab_bgr(None))
    if len(badges) < 10:
        return None
    s0 = 0.91                        # 初值只影响聚类 gap，最终 s 由列距决定

    # 行聚类：网格行每行 ≥3 角标；左侧面板孤行过滤
    row_groups = cluster_vals([b[1] for b in badges], BASE_ROW_SPACING * s0 * 0.5)
    row_c = [sum(g) / len(g) for g in row_groups]
    row_map = {}
    for b in badges:
        r = min(row_c, key=lambda rc: abs(rc - b[1]))
        row_map.setdefault(r, []).append(b)
    grid_badges = [b for rc, bs in row_map.items() if len(bs) >= 3 for b in bs]
    if len(grid_badges) < 10:
        return None

    # 列聚类：网格列每列 ≥3 角标
    col_groups = cluster_vals([b[0] for b in grid_badges], BASE_COL_SPACING * s0 * 0.5)
    col_c = [sum(g) / len(g) for g in col_groups]
    col_map = {}
    for b in grid_badges:
        c = min(col_c, key=lambda cc: abs(cc - b[0]))
        col_map.setdefault(c, []).append(b)
    grid_cols = sorted(c for c, bs in col_map.items() if len(bs) >= 3)
    if len(grid_cols) < 4:
        return None
    spacing = float(np.median(np.diff(grid_cols)))
    s = spacing / BASE_COL_SPACING
    if not 0.3 <= s <= 1.2:
        return None

    # 行中心精确化（用 s）
    row_groups = cluster_vals([b[1] for b in grid_badges], BASE_ROW_SPACING * s * 0.5)
    row_c = [sum(g) / len(g) for g in row_groups]
    row_spacing = float(np.median(np.diff(row_c))) if len(row_c) > 1 else BASE_ROW_SPACING * s

    grid_bbox = (int(min(grid_cols) - spacing * 0.95), int(min(row_c) - row_spacing * 0.80),
                 int(max(grid_cols) + spacing * 0.55), int(max(row_c) + row_spacing * 0.45))
    gcx = int((grid_bbox[0] + grid_bbox[2]) / 2)
    gcy = int((grid_bbox[1] + grid_bbox[3]) / 2)
    return {"s": s, "grid_bbox": grid_bbox, "cols": grid_cols, "spacing": spacing,
            "row_spacing": row_spacing, "gcx": gcx, "gcy": gcy}


def detect_rows(grid, geo):
    """每屏行检测：角标 cy 聚类 → 行中心列表（点击 y = 角标 cy 均值 - 56*s）。
    - 角标整行漏检时用 row_spacing 插值补行（识别只影响行发现，不影响点击正确性）
    - 行 y 钳制：y_lo 防顶部 UI 伪角标把点击引到設定等按钮；y_hi 放宽 0.8 行距以容纳
      autocalib 未计入的末行（半遮挡/单物品行不进 grid_bbox，见下方钳制处注释）
    - 同时检测弹窗关键词（≥2 命中 = 弹窗打开）
    返回 (row_ys, badges, dialog_open)"""
    s = geo["s"]
    row_step = geo["row_spacing"]
    badges = []
    dialog_hits = 0
    for b in ocr(grid):
        t = b["text"]
        if any(k in t for k in DIALOG_KEYS):
            dialog_hits += 1
        m = BADGE_RE.fullmatch(t)
        if not m:
            continue
        xs = [q[0] for q in b["box"]]
        ys = [q[1] for q in b["box"]]
        badges.append((sum(xs) / 4, sum(ys) / 4, scaled_value(m)))
    dialog_open = dialog_hits >= 2
    if not badges:
        return [], [], dialog_open
    # 只保留网格列范围内的角标（过滤左侧面板）
    x0, x1 = geo["cols"][0] - geo["spacing"] * 0.6, geo["cols"][-1] + geo["spacing"] * 0.6
    badges = [b for b in badges if x0 <= b[0] <= x1]
    if not badges:
        return [], [], dialog_open
    groups = cluster_vals([b[1] for b in badges], row_step * 0.5)
    # 每行点击 y = 行内角标 cy 均值 - 56*s
    rows = []
    for g in groups:
        if len(g) >= 2:                      # 网格行至少 2 个角标（5 列漏 3 个仍可见）
            rows.append(sum(g) / len(g) - BADGE_CLICK_DY * s)
    # 末行特例：排序列表垫底的行可能只有 1 个物品（如 x10K 万能設計圖）。
    # 仅当单角标组位于所有多角标行下方（只可能是列表末行）时才采纳为行，
    # 避免屏内散点误建行。cluster_vals 升序返回，singles[-1] 即最下方的单角标。
    multi_cys = [sum(g) / len(g) for g in groups if len(g) >= 2]
    singles = [g[0] for g in groups if len(g) == 1]
    if singles and (not multi_cys or singles[-1] > max(multi_cys) + row_step * 0.25):
        rows.append(singles[-1] - BADGE_CLICK_DY * s)
    # 插值补行：相邻行距 > 1.5 倍行距说明整行漏检
    filled = []
    for i, ry in enumerate(rows):
        if i > 0:
            gap = ry - rows[i - 1]
            n_missing = int(round(gap / row_step)) - 1
            for k in range(1, n_missing + 1):
                filled.append(rows[i - 1] + row_step * k)
        filled.append(ry)
    # 行 y 钳制：上界防顶部 UI 伪角标把点击引到表头；下界 y_hi 放宽 0.8 个行距——
    # autocalib 对"不足 3 角标的行"（半遮挡的第 6 行、只有 1 个物品的末行）不计入
    # grid_bbox，导致真实末行的点击 y 超出旧钳制被整行裁掉（实测：垫底的 x10K
    # 万能設計圖因此从未被点击）。放宽后多出的行仍须有真实角标才会生成，无幻行风险。
    y_lo = 300.0 * s
    y_hi = geo["grid_bbox"][3] + row_step * 0.8
    filled = [ry for ry in filled if y_lo <= ry <= y_hi]
    return filled, badges, dialog_open


def strip_hash(img):
    small = cv2.resize(img, (64, 24), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def parse_wide(lines):
    """(text, height) 行列表 → (名称, 数量)。
    名称 = 行高最大的 CJK 行（名称横幅字体最大，标签/描述/主能力值被高度过滤；
    长度 >20 或含句号的行视为物品描述文本，直接排除——道具名不会这么长）。
    名称横幅过长会换行：学校后缀（如（三一））的捕获形态有三种——独立成行、
    与持有數量同行（（三一）持有數量x159）、或并入主名（高級技術筆記（三一）），
    三种都要把学校提取出来拼回主名——高級技術筆記（三一） 与 （千年） 是不同物品。
    数量 = 最后一个独立数字行（支持 10K/1.2K/1M 缩写）。"""
    texts = [t for t, _h in lines]

    def is_school(t):
        return bool(re.fullmatch(r"[（(][^（）()]{1,8}[）)]", t))

    def base_ok(t):
        return (t and any("一" <= ch <= "鿿" for ch in t) and len(t) <= 20 and "。" not in t
                and t not in SKIP_EXACT
                and "持有" not in t and "數量" not in t and "数量" not in t
                and "主能力" not in t and "獲得" not in t and "获得" not in t
                and "攻擊力" not in t and "攻击力" not in t
                and not re.match(r"^20\d{2}-", t))

    name, best_h, name_idx = None, 0, -1
    for idx, (text, h) in enumerate(lines):
        if is_school(text):
            continue                     # 学校后缀行不当主名
        if base_ok(text) and h > best_h:
            name, best_h, name_idx = text, h, idx

    # 学校后缀提取：主名自带 > 横幅内独立行 > 与持有數量合并的行（自上而下首个命中）
    count_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        if COUNT_RE.fullmatch(texts[idx]):
            count_idx = idx
            break
    school = None
    for t in texts[:count_idx if count_idx > 0 else len(texts)]:
        m = SCHOOL_RE.search(t)
        if m:
            school = m.group(1)
            break
    if name and school and f"（{school}）" not in name and f"({school})" not in name:
        name += f"（{school}）"

    count = None
    for text in reversed(texts):
        m = COUNT_RE.fullmatch(text)
        if m:
            count = scaled_value(m)
            break
    return name, count


def window_rect(hwnd):
    """游戏窗口矩形（物理像素）；取不到时回退主屏"""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    rc = RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rc)) \
            and rc.right > rc.left and rc.bottom > rc.top:
        return (rc.left, rc.top, rc.right, rc.bottom)
    m = SCT.monitors[1]
    return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])


def activate_game():
    """找到 BlueArchive 窗口 → 还原（若最小化）→ 置前。返回 hwnd 或 None"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process BlueArchive -ErrorAction SilentlyContinue | "
         "Where-Object { $_.MainWindowHandle -ne 0 } | "
         "Select-Object -First 1).MainWindowHandle"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    if not out.isdigit():
        print("未找到 BlueArchive 窗口：请先启动游戏并进入道具页面", flush=True)
        return None
    hwnd = int(out)
    user32.ShowWindow(hwnd, 9)            # SW_RESTORE：最小化则还原
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.2)
    ok = user32.GetForegroundWindow() == hwnd
    print(f"game window hwnd={hwnd} foreground={'OK' if ok else 'FAILED(请手动点一下游戏窗口)'}",
          flush=True)
    time.sleep(0.8)
    return hwnd


def dialog_open_full():
    """整屏 OCR 查弹窗关键词（autocalib 失败路径用）"""
    with SCT_LOCK:
        g = grab_bgr(None)
    hits = sum(1 for b in ocr(g) if any(k in b["text"] for k in DIALOG_KEYS))
    return hits >= 2


class Tee:
    """控制台输出同步写入日志文件（采集日志.txt）"""

    def __init__(self, stdout, fp):
        self.stdout, self.fp = stdout, fp

    def write(self, s):
        self.stdout.write(s)
        self.fp.write(s)
        self.fp.flush()

    def flush(self):
        self.stdout.flush()
        self.fp.flush()


def main():
    ap = argparse.ArgumentParser(description="BA 库存采集器（队列版 v10）")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略旧 rows2.json 重新扫描（旧文件自动备份为 rows2_backup_*.json）")
    args = ap.parse_args()

    global ENGINE, WIN, WIDE
    ENGINE = make_engine(use_dml=False)   # 主线程（自标定 + 每屏行检测用 CPU，杜绝显卡争用）
    log_fp = open(os.path.join(TEMP, "采集日志.txt"), "a", encoding="utf-8")
    log_fp.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    sys.stdout = Tee(sys.stdout, log_fp)

    if args.fresh and os.path.exists(ROWS_OUT):
        bak = ROWS_OUT[:-5] + "_backup_%s.json" % time.strftime("%Y%m%d_%H%M%S")
        os.replace(ROWS_OUT, bak)
        print(f"--fresh: 旧采集数据已备份为 {os.path.basename(bak)}", flush=True)

    hwnd = activate_game()
    if hwnd is None:
        print("未找到游戏窗口，退出。请先启动游戏。", flush=True)
        return
    WIN = window_rect(hwnd)
    print(f"game window rect: {WIN}", flush=True)
    time.sleep(0.5)
    geo = None
    for attempt in range(6):
        geo = autocalib()
        if geo is not None:
            break
        if dialog_open_full():
            print(f"autocalib retry {attempt+1}/6: 检测到弹窗，发送 ESC 关闭...", flush=True)
            send_esc()
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
    # 名称条带按游戏窗口底部锚定（窗口相对坐标）。右界不随 s 缩放——面板文本位置
    # 基本不随网格缩放系数走，s<1 时 1300*s 会把持有数量第 4 位起的数字裁掉
    # （实测 5469→546、10686→106，且截断读数稳定复现，会在众数投票中压过正确值）。
    # 右界取网格首列角标文本左侧：既覆盖数量文本，又不把首列角标卷进条带。
    wh = WIN[3] - WIN[1]
    WIDE = (0.0, wh - 550.0 * s, cols[0] - 0.45 * geo["spacing"], wh - 150.0 * s)
    print(f"autocalib: s={s:.3f} cols={len(cols)} col_step={geo['spacing']:.0f} "
          f"row_step={geo['row_spacing']:.0f} wheel=({gcx},{gcy}) "
          f"panel=({WIDE[0]:.0f},{WIDE[1]:.0f},{WIDE[2]:.0f},{WIDE[3]:.0f})", flush=True)

    rows = []
    if os.path.exists(ROWS_OUT):
        with open(ROWS_OUT, encoding="utf-8") as f:
            rows = [tuple(r) for r in json.load(f)]
    known = set(rows)
    rows_lock = threading.Lock()
    print(f"resume: rows2.json 已有 {len(rows)} 行", flush=True)

    def save_rows():
        with rows_lock:                    # 与消费者线程的并发 append 互斥
            snap = [list(x) for x in rows]
        with open(ROWS_OUT, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)

    new_rows_ct = [0]                  # 消费端实际新增行计数（兜底判定用）
    ledger = {}                        # 页内格子台账 (行序号, 列序号) -> {state, attempts}
                                       # state ∈ pending/dup/valid/invalid/giveup
                                       # 键用格位索引而非像素坐标：角标定位有 ±1-2px 抖动，
                                       # 像素键会让 invalid 条目成为永远点不到的孤儿，
                                       # 覆盖率检查永不通过（2026-09-29 实测）
    prev_row_count = None              # 上一屏检测到的行数（行数变化 = 页面结构变，清台账）

    q = queue.Queue(maxsize=200)

    # 消费端使用专用 GPU DirectML 引擎（未启用 DML 则自动降级 CPU）
    consumer_engine = make_engine(use_dml=True)

    def consumer():
        def ocr_png(png_bytes):
            arr = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            res, _ = consumer_engine(arr)
            lines = []
            if res:
                for it in res:
                    if isinstance(it, (list, tuple)) and len(it) >= 3:
                        xs = [p[0] for p in it[0]]
                        ys = [p[1] for p in it[0]]
                        lines.append((it[1].strip(), int(max(ys) - min(ys))))
            return [x for x in lines if x[0]]

        debug_strip = os.environ.get("BA_DEBUG_STRIP")
        debug_dumped = [0]

        while True:
            job = q.get()
            if job is None:
                q.task_done()
                return
            strip_png, cell_key = job
            name = count = None
            try:
                lines = ocr_png(strip_png)
                if debug_strip and debug_dumped[0] < 3:
                    debug_dumped[0] += 1
                    os.makedirs(os.path.join(TEMP, "_strip_debug"), exist_ok=True)
                    base = os.path.join(TEMP, "_strip_debug", f"strip{debug_dumped[0]}")
                    with open(base + ".png", "wb") as f:
                        f.write(strip_png)
                    with open(base + ".txt", "w", encoding="utf-8") as f:
                        f.write(repr(lines))
                name, count = parse_wide(lines)
            except Exception as exc:
                print(f"  [consumer] parse error: {exc!r}", flush=True)
            with rows_lock:
                if name and count is not None:
                    row = (str(name), int(count))
                    if row not in known:
                        known.add(row)
                        rows.append(row)
                        new_rows_ct[0] += 1
                if cell_key is not None:
                    st = ledger.setdefault(cell_key, {"state": "pending", "attempts": 0})
                    st["state"] = "valid" if (name and count is not None) else "invalid"
            q.task_done()

    # GPU 模式下单专职线程独占显卡带宽吞吐更高且无争用；CPU 模式起 2 线程发挥多核优势
    num_consumers = 1 if HAS_DML else 2
    consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(num_consumers)]
    for t in consumers:
        t.start()

    # ===== 滚回顶部：滚到网格内容不再变化为止（固定次数不可靠——实测滚动 ~1.1 行/格）=====
    print(f"scroll to top... (wheel at {gcx},{gcy})", flush=True)
    prev_sig = None
    stable = 0
    for i in range(40):
        fast_wheel(6, gcx, gcy)
        time.sleep(0.4)
        with SCT_LOCK:
            gimg = grab_bgr(tuple(geo["grid_bbox"]))
        sig = strip_hash(gimg)
        if prev_sig is not None and float(np.mean(
                np.abs(sig.astype(np.int16) - prev_sig.astype(np.int16)))) < HASH_THRESH:
            stable += 1
        else:
            stable = 0
        if stable >= 2:
            print(f"  top reached after {i + 1} wheel batches", flush=True)
            break
        prev_sig = sig
    time.sleep(0.5)

    # ===== 生产者：确定性点阵遍历 =====
    hash_hist = deque(maxlen=800)    # 最近条带缩略图（判重）
    page_hash = None                 # 上一屏起始的网格哈希（换页检测）
    unchanged_streak = 0             # 连续同一页屏数（覆盖率终止要求 ≥2）
    quiet = 0                        # 兜底：排空后零新增轮数
    empty_screens = 0

    def click_cell(cx, cy, cell_key=None):
        """点击一格。条带与历史重复 → 台账记 dup（无新信息可学）；新条带 → 记 pending
        入队，由消费者回写 valid/invalid。返回 True=新条带入队。"""
        fast_click(cx, cy)
        time.sleep(CLICK_SLEEP)
        with SCT_LOCK:
            wide_img = grab_bgr(WIDE)
        h = strip_hash(wide_img)
        if any(float(np.mean(np.abs(h.astype(np.int16) - p.astype(np.int16)))) < HASH_THRESH
               for p in hash_hist):
            if cell_key is not None and cell_key not in ledger:
                ledger[cell_key] = {"state": "dup", "attempts": 0}
            return False                 # 空格/重复条带：不入队
        hash_hist.append(h)
        if cell_key is not None:
            ledger.setdefault(cell_key, {"state": "pending", "attempts": 0})["state"] = "pending"
        ok_png, png = cv2.imencode(".png", wide_img)
        q.put((png.tobytes(), cell_key))
        return True

    for screen in range(1, MAX_SCREENS + 1):
        t0 = time.time()
        with SCT_LOCK:
            grid = grab_bgr(None)
        gl, gt, gr, gb = geo["grid_bbox"]
        h_page = strip_hash(grid[gt:gb, gl:gr])
        same_page = (page_hash is not None and float(np.mean(
            np.abs(h_page.astype(np.int16) - page_hash.astype(np.int16)))) < HASH_THRESH)
        page_hash = h_page
        unchanged_streak = unchanged_streak + 1 if same_page else 0
        row_ys, badges, dialog_open = detect_rows(grid, geo)
        if dialog_open and not row_ys:
            # 弹窗打开（误触設定等）：ESC 取消（未确认改动会被丢弃），本屏重扫
            print("  dialog detected -> ESC, rescan screen", flush=True)
            send_esc()
            time.sleep(0.8)
            continue

        # 点击目标：换页（sweep）→ 全格遍历；同一页（verify，含滚轮丢失的罕见情形）
        # → 只点台账未解决的格子（invalid 重试至 RETRY_LIMIT，dup/valid 不重复），
        # 并附末行投影探测（格子同样入账，读取失败会被重试而不是漏掉）。
        # 台账键 = (行序号, 列序号)，对角标定位的 ±1-2px 抖动免疫。
        if len(row_ys) != prev_row_count:
            ledger.clear()               # 行数变化 = 页面结构变，旧台账全部作废
        prev_row_count = len(row_ys)

        def pick(key):
            """返回该格子是否需要（重新）点击；顺便维护重试计数与放弃标记"""
            st = ledger.get(key)
            if st is not None:
                if st["state"] in ("dup", "valid", "giveup"):
                    return False
                if st["state"] == "invalid" and st["attempts"] >= RETRY_LIMIT:
                    st["state"] = "giveup"
                    return False
                if st["state"] == "invalid":
                    st["attempts"] += 1
            return True

        targets = []                     # (cell_key, (cx, cy))
        if same_page:
            for i, ry in enumerate(row_ys):
                for j, cx in enumerate(cols):
                    if pick((i, j)):
                        targets.append(((i, j), (cx, ry)))
            if row_ys:
                probe_y = max(row_ys) + row_spacing
                if probe_y < (WIN[3] - WIN[1]) - 10:     # 投影行须仍在游戏窗口内
                    for j, cx in enumerate(cols):
                        if pick(("p", j)):
                            targets.append((("p", j), (cx, probe_y)))
        else:
            targets = [((i, j), (cx, ry))
                       for i, ry in enumerate(row_ys) for j, cx in enumerate(cols)]

        clicked = skipped = 0
        for key, (cx, cy) in targets:
            if click_cell(cx, cy, cell_key=key):
                clicked += 1
            else:
                skipped += 1
        uncovered = sum(1 for v in ledger.values() if v["state"] not in ("dup", "valid", "giveup"))
        mode = "verify" if same_page else "sweep "
        print(f"screen {screen} [{mode}]: rows_detected={len(row_ys)} badges={len(badges)} "
              f"new_strips={clicked} dup={skipped} uncovered={uncovered} "
              f"({time.time()-t0:.1f}s) rows={len(rows)}",
              flush=True)
        save_rows()

        # ===== 到底判定（覆盖率口径为主，新颖性口径兜底）=====
        # 覆盖率：同一页连续 ≥2 屏（抵御单次滚轮丢失），且台账内每个格子都已产出
        # 有效读数、确认重复、或重试上限后放弃 → 本页构造性完整，整个列表扫描完成。
        # 排空后才评估，保证台账与行计数是最终态。
        # 新颖性兜底：排空后连续 3 轮零新增（台账异常时的回退路径，原为 ×2）。
        if any(not t.is_alive() for t in consumers):
            print("consumer thread died unexpectedly, abort scan", flush=True)
            break
        if q.unfinished_tasks == 0:
            with rows_lock:
                new_rows = new_rows_ct[0]
                new_rows_ct[0] = 0
            if new_rows == 0:
                quiet += 1
            else:
                quiet = 0
            if (same_page and unchanged_streak >= 2 and ledger
                    and all(v["state"] in ("dup", "valid", "giveup") for v in ledger.values())):
                n_valid = sum(1 for v in ledger.values() if v["state"] == "valid")
                n_dup = sum(1 for v in ledger.values() if v["state"] == "dup")
                n_give = sum(1 for v in ledger.values() if v["state"] == "giveup")
                print(f"coverage complete: {len(ledger)} cells (valid={n_valid}, dup={n_dup}, "
                      f"giveup={n_give}), rows={len(rows)}", flush=True)
                if n_give:
                    print("── 放弃的格子（重试 3 次仍读不出，建议跑 ba_icon_linker.py 对齐）──",
                          flush=True)
                    for k, v in sorted(ledger.items(), key=lambda kv: str(kv[0])):
                        if v["state"] == "giveup":
                            where = (f"probe col{k[1]}" if k[0] == "p"
                                     else f"row{k[0]} col{k[1]}")
                            print(f"  {where} attempts={v['attempts']}", flush=True)
                break
            if quiet >= 3:
                print(f"bottom reached (fallback: no new rows x{quiet}, rows={len(rows)})",
                      flush=True)
                stuck = {k: v for k, v in ledger.items()
                         if v["state"] not in ("dup", "valid", "giveup")}
                if stuck:
                    print("── 未覆盖的台账条目（覆盖率终止被它们卡住）──", flush=True)
                    for k, v in sorted(stuck.items(), key=lambda kv: str(kv[0])):
                        where = f"probe col{k[1]}" if k[0] == "p" else f"row{k[0]} col{k[1]}"
                        print(f"  {where} state={v['state']} attempts={v['attempts']}",
                              flush=True)
                break
        if not badges:
            empty_screens += 1
        else:
            empty_screens = 0
        if empty_screens >= 2:
            q.join()                     # 清空 OCR 积压后再退出
            print(f"bottom reached (no badges x{empty_screens}, rows={len(rows)})",
                  flush=True)
            break
        fast_wheel(-SCROLL_NOTCHES, gcx, gcy)
        # 等滚动/回弹彻底停稳：0.2s 步长轮询网格哈希，连续两次一致即认为稳定。
        # 中段列表滚完即稳（~0.4s 通过）；底部回弹可能持续 1s 以上，按需等待。
        prev_sig = None
        for _ in range(SCROLL_POLL_MAX):
            time.sleep(SCROLL_POLL)
            with SCT_LOCK:
                sig = strip_hash(grab_bgr(tuple(geo["grid_bbox"])))
            if prev_sig is not None and float(np.mean(
                    np.abs(sig.astype(np.int16) - prev_sig.astype(np.int16)))) < HASH_THRESH:
                break
            prev_sig = sig

    for _ in range(len(consumers)):
        q.put(None)
    for t in consumers:
        t.join(timeout=120)
    save_rows()
    print(f"done: {len(rows)} rows", flush=True)

    # ===== 扫完直接生成导入文件（rows2 已落盘，就地调用映射器，报告同样进日志）=====
    try:
        import ba_map_items
        ba_map_items.main()
        print(f"导入文件已生成: {ba_map_items.OUT}", flush=True)
        os.startfile(os.path.dirname(ba_map_items.OUT))   # 自动打开输出文件夹
    except Exception as exc:
        print(f"[警告] 自动生成导入文件失败（rows2.json 已保存，不受影响）: {exc!r}\n"
              f"可稍后双击 2-生成什亭之匣导入文件.bat 或运行 python ba_map_items.py 重试",
              flush=True)


if __name__ == "__main__":
    main()
