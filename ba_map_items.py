# -*- coding: utf-8 -*-
"""把采集的 (TW名称, 数量) 行模糊映射到 SchaleDB 物品/設計圖 Id，生成什亭之匣导入 JSON
- 物品 → item_<Id>（cache/items.min.json）
- 裝備設計圖 → equipment_<Id>（cache/equipment.min.json）
"""
import difflib
import json
import os
import re
from collections import Counter, defaultdict

from zhconv import convert as t2s

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS_TOOL = os.path.join(HERE, "rows2.json")
ITEMS_JSON = os.path.join(HERE, "cache", "items.min.json")
EQUIP_JSON = os.path.join(HERE, "cache", "equipment.min.json")
# 输出路径：默认工具目录下 输出\，可用环境变量 BA_OUT 覆盖
OUT = os.environ.get("BA_OUT", os.path.join(HERE, "输出", "什亭之匣库存导入_OCR采集.json"))

# TW→CN 及 OCR 常见误读的别名修正（人工累积）
ALIAS = {
    "戟": "战", "幣": "币", "硬": "币", "肇记": "笔记", "肇": "笔",
    "摆箱": "选择券", "選摆箱": "选择券", "選箱": "选择券", "暹摆箱": "选择券",
    "選擇箱": "选择券", "選擇券": "选择券",
    "乙太": "以太", "曼陀罗草": "曼德拉草", "安迪基西拉": "安提基瑟拉",
    "罹洪特抄本": "罗洪特写本", "罹洪特": "罗洪特写本", "斐斯托斯": "费斯托斯",
    "沃普赛克铁": "沃尔夫塞格铁矿石", "沃普赛克": "沃尔夫塞格", "铁瑰": "铁片",
    "女武神": "瓦尔基丽",
    "羊毛黄金": "黄金毛线", "羊毛织物": "黄金毛线团", "修復的回撃飾": "修好的圆盘吊坠",
    "修复的回击饰": "修好的圆盘吊坠",
    "一般活动告": "初级活动报告书", "活动告": "活动报告书",
    "一般合成用动石": "初级合成石", "动石": "中级合成石",
    "体育培育WB": "体育辅导手册", "射擎培育WB": "射击辅导手册", "衡生培育WB": "卫生辅导手册",
    "射擎培育": "射击辅导手册", "衡生培育": "卫生辅导手册", "体育培育": "体育辅导手册",
    "手錶": "手表", "皮革": "皮带",
}


def norm(s):
    s = t2s(s.strip(), "zh-hans")
    s = s.replace("（", "(").replace("）", ")").replace(" ", "").replace("\n", "")
    for a, b in ALIAS.items():
        s = s.replace(a, b)
    return s


def main():
    rows_fp = ROWS_TOOL
    rows = [tuple(r) for r in json.load(open(rows_fp, encoding="utf-8"))]
    print(f"rows source: {rows_fp}")
    items = json.load(open(ITEMS_JSON, encoding="utf-8"))
    equip = json.load(open(EQUIP_JSON, encoding="utf-8"))

    # 归一化候选名：norm_name -> (kind, iid)，kind ∈ {"item", "equipment"}
    norm_names = {}
    raw_name = {}
    for iid, v in items.items():
        norm_names.setdefault(norm(v["Name"]), ("item", iid))
        raw_name[("item", iid)] = v["Name"]
    for iid, v in equip.items():
        norm_names.setdefault(norm(v["Name"]), ("equipment", iid))
        raw_name[("equipment", iid)] = v["Name"]

    # 人工确认的礼物/杂项映射（OCR 名称片段 → item Id，包含式双向匹配）
    MANUAL = {
        "肌膚清透": "5006", "肌肤清透": "5006", "粉底霜": "5007",
        "甜點口味": "5012", "甜点口味": "5012", "软垫": "5014", "軟墊": "5014",
        "圈圈眼镜": "5018", "圈圈眼鏡": "5018", "熊娃娃": "5020",
        "顏料组合": "5022", "颜料组合": "5022", "手帕": "5031", "百科": "5032",
        "针组合": "5109", "针線组合": "5109", "桌游": "5112", "手工蛋糕": "5026",
    }
    # 人工确认的設計圖/装备映射（2026-09-27 审计定案，优先级高于模糊匹配）
    # 用途：① OCR 拉丁名/异名行归位；② 短名模糊匹配串行的纠正
    MANUAL_EQ = {
        # 强化珠四级（模糊匹配把四个等级全串到 3 上）
        "下级强化石": "1", "下級強化石": "1",
        "高级强化石": "3", "高級強化石": "3",
        "最高级强化石": "4", "最高級強化石": "4",
        # 拉丁名装备（模糊匹配无法命中）
        "Lorelei手": "108007",        # 罗蕾莱手表设计图 T8
        "Lorelei徽章": "105007",      # 罗蕾莱徽章设计图 T8
        "Veronica刺": "105003",       # 维罗妮卡刺绣徽章设计图 T4
        "Manaslu": "105001",          # 玛纳斯卢毛毡徽章设计图 T2
        "Coco Devil": "105005",       # 可可恶魔徽章设计图 T6
        "Kazeyama": "105004",         # 风山纹章设计图 T5
        # 异名纠正
        "魔鬼翅膀托特包": "104005",   # 恶魔之翼挎包设计图 T6（曾误入 106003 翅膀发夹）
        "古典法樂福鞋": "103003",     # 复古漆皮豆豆鞋设计图 T4（乐福鞋=豆豆鞋）
        "荷葉複迷你帽": "101005",     # 褶边小礼帽设计图 T6
        "術俊背式皮革書包": "104004", # 战术双肩包设计图 T5（曾误入 104003 藏蓝书包）
        "海軍藍書包": "104003",       # 藏蓝书包设计图 T4
        "填万能": "503000",           # 鞋万能设计图（鞋→填 OCR 误读，排除法唯一剩余）
        "髪灰萬能": "506000",         # 发夹万能设计图（髪灰=髮飾=发夹，曾误入 503000）
    }
    # 归属存疑、禁止模糊匹配吞并的行（进 unmapped 人工复核，宁可缺不可错）
    BLOCK_EQ = ("调節器保護套", "式頂設计", "骨董設计", "水蜜桃髪灰", "控手設计", "十字架頂")
    # MANUAL 键同样过 norm()（键是繁体、行名会被转成简体，不规范化永远匹配不上）
    MANUAL_N = {norm(k): v for k, v in MANUAL.items()}
    by_key = defaultdict(Counter)   # (kind, iid) -> Counter(count)
    last_idx = {}                   # (kind, iid, count) -> 最后出现的行号（新近度决胜用）
    unmapped, ambiguous = [], []
    for ridx, (name, count) in enumerate(rows):
        n = norm(name)
        n = re.sub(r"^\d{4}-\d{2}-\d{2}.*", "_date", n)  # 期限横幅行
        if n == "_date":
            continue
        # 学生神名文字 / 选择券-箱 家族：CN 名称体系与 TW 不同，纯名称匹配会串线 → 单列复核
        if ("的神名文字" in n or "神名文字" in n or "选择" in n or "選擇" in n):
            ambiguous.append((name, count))
            continue
        # 归属存疑行：禁止模糊匹配吞并，进人工复核
        if any(b in name for b in BLOCK_EQ):
            unmapped.append((name, count))
            continue
        # 人工确认的設計圖/装备映射（优先于一切自动匹配；长键优先防子串吞并，如「最高级」含「高级」）
        hit_eq = next((iid for mk, iid in sorted(MANUAL_EQ.items(), key=lambda kv: -len(kv[0]))
                       if mk in name), None)
        if hit_eq:
            by_key[("equipment", hit_eq)][count] += 1
            last_idx[("equipment", hit_eq, count)] = ridx
            continue
        hit = next((iid for mk, iid in sorted(MANUAL_N.items(), key=lambda kv: -len(kv[0]))
                    if mk in n or n in mk), None)
        if hit:
            by_key[("item", hit)][count] += 1
            last_idx[("item", hit, count)] = ridx
            continue
        if n in norm_names:
            key = norm_names[n]
            by_key[key][count] += 1
            last_idx[(key[0], key[1], count)] = ridx
            continue
        close = difflib.get_close_matches(n, list(norm_names), n=1, cutoff=0.55)
        if close:
            key = norm_names[close[0]]
            by_key[key][count] += 1
            last_idx[(key[0], key[1], count)] = ridx
        else:
            unmapped.append((name, count))

    results, conflicts = {}, []
    for key, counter in by_key.items():
        # 众数优先，同票取 rows2.json 中最后出现的读数（最新扫描轮次）；
        # 低置信条目保留进结果（丢弃会让站点导入清空后变 0），仅列入冲突清单供人工核对
        best = max(counter, key=lambda c: (counter[c], last_idx.get((key[0], key[1], c), -1)))
        results[key] = best
        if len(counter) > 1:
            conflicts.append((key, raw_name[key], dict(counter), best))

    # 早前运行已人工验证的条目——直接覆盖 OCR 读数（已验证值优先于众数/新近度）
    RECOVER = {
        "11": 0,   # 中级活动报告书
        "12": 0,    # 高级活动报告书
        "61": 0,    # 悬赏通缉奖币
        "120": 0, "121": 0, "122": 0, "123": 0,  # 沃尔夫塞格系列
        "81": 0,     # 中级合成石（置信度中）
        "220": 0, "221": 0, "222": 0,  # 黄金毛线系列（置信度中）
    }
    for k, v in RECOVER.items():
        results[("item", k)] = v

    out = {}
    for (kind, iid), cnt in results.items():
        out[f"{kind}_{iid}"] = cnt
    out = dict(sorted(out.items(), key=lambda kv: -kv[1]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    n_item = sum(1 for k in results if k[0] == "item")
    n_eq = sum(1 for k in results if k[0] == "equipment")
    print(f"rows: {len(rows)}  ->  items: {n_item}  equipment: {n_eq}")
    print(f"saved: {OUT}\n")
    with open(os.path.join(HERE, "ambiguous.json"), "w", encoding="utf-8") as f:
        json.dump(ambiguous, f, ensure_ascii=False, indent=1)
    print(f"── 待复核（神名文字/选择券家族，共 {len(ambiguous)} 行，见 ambiguous.json）──")
    if conflicts:
        print("── 数量冲突（取众数，需人工瞄一眼）──")
        for key, nm, c, best in conflicts:
            print(f"  {key[0]}_{key[1]:<8} {nm}  {c} -> {best}")
    print("── 未映射行 ──")
    for name, count in unmapped:
        print(f"  {name} x{count}")
    print("\n── 設計圖映射结果 ──")
    for (kind, iid), cnt in sorted(results.items()):
        if kind == "equipment":
            print(f"  equipment_{iid:<8} {cnt:>5}  {raw_name[(kind, iid)]}")
    print("\n── 数量 Top25（物品）──")
    top = sorted(((k, v) for k, v in results.items() if k[0] == "item"), key=lambda kv: -kv[1])
    for (kind, iid), cnt in top[:25]:
        print(f"  item_{iid:<8} {cnt:>8}  {raw_name[(kind, iid)]}")


if __name__ == "__main__":
    main()
