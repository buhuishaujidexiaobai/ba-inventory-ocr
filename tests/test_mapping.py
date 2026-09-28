# -*- coding: utf-8 -*-
"""映射与解析逻辑回归测试（不需要游戏窗口，可随时运行）。

运行（用采集依赖所在 Python 环境，见 requirements.txt）：
    cd 项目根目录
    python -m unittest discover -s tests -v

test_parse_wide 会 import ba_queue_collector（需要 rapidocr/mss/cv2 可导入；
引擎只在 main() 里创建，import 不会加载 OCR 模型）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ba_map_items import ALIAS, MANUAL, MANUAL_EQ, norm  # noqa: E402


class TestNorm(unittest.TestCase):
    def test_trad_to_simp(self):
        self.assertEqual(norm("選擇券"), "选择券")
        self.assertEqual(norm("裝備設計圖"), "装备设计图")

    def test_alias_corrections(self):
        self.assertEqual(norm("乙太碎片"), "以太碎片")
        self.assertEqual(norm("沃普赛克铁"), "沃尔夫塞格铁矿石")
        self.assertEqual(norm("一般活动告"), "初级活动报告书")

    def test_fullwidth_punct_and_spaces(self):
        self.assertEqual(norm("手表（高级）"), "手表(高级)")
        self.assertEqual(norm("  活动 报告  "), "活动报告")


class TestAliasTables(unittest.TestCase):
    def test_select_ticket_family_is_normalizable(self):
        # 选择券家族无论 OCR 成什么变体，norm 后都必须含「选择」，
        # 这样才会被 ba_map_items 送进 ambiguous 人工复核而不是误映射
        for variant in ("選擇券", "選擇箱", "選摆箱", "暹摆箱"):
            self.assertIn("选择", norm(variant), f"{variant!r} 未落入选择券家族")

    def test_manual_eq_covers_both_scripts(self):
        # MANUAL_EQ 按原始行名做子串匹配（不经 norm），繁简变体要成对提供
        for simp, trad in (("下级强化石", "下級強化石"), ("高级强化石", "高級強化石"),
                           ("最高级强化石", "最高級強化石")):
            self.assertIn(simp, MANUAL_EQ, f"MANUAL_EQ 缺简体键 {simp!r}")
            self.assertIn(trad, MANUAL_EQ, f"MANUAL_EQ 缺繁体键 {trad!r}")

    def test_manual_keys_cover_both_scripts(self):
        # MANUAL 键会过 norm()，但繁简成对提供更稳（防单侧被改名）
        self.assertIn("肌膚清透", MANUAL)
        self.assertIn("肌肤清透", MANUAL)


class TestParseWide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ba_queue_collector import parse_wide
        cls.parse = staticmethod(parse_wide)

    def test_name_and_count(self):
        # 名称 = 行高最大的 CJK 行；数量 = 倒数第一个独立数字行
        name, count = self.parse([("道具", 15), ("攻擊力", 18),
                                  ("精金石", 45), ("持有數量", 16), ("x1,234", 20)])
        self.assertEqual((name, count), ("精金石", 1234))

    def test_skips_labels_and_dates(self):
        name, count = self.parse([("2026-09-27 至", 14), ("活动报告", 40), ("x5", 18)])
        self.assertEqual((name, count), ("活动报告", 5))

    def test_count_missing(self):
        name, count = self.parse([("精金石", 45), ("持有數量", 16)])
        self.assertEqual((name, count), ("精金石", None))

    def test_name_missing(self):
        name, count = self.parse([("x10", 20)])
        self.assertEqual((name, count), (None, 10))

    def test_tallest_line_wins(self):
        # 两行 CJK 时取行高更大的（名称横幅字体最大）
        name, _ = self.parse([("小字描述", 14), ("真正的名字", 44), ("x3", 18)])
        self.assertEqual(name, "真正的名字")

    def test_count_k_abbr(self):
        # 游戏对 ≥10000 的数量在角标和面板都可能缩写为 10K
        name, count = self.parse([("鞋萬能設計圖", 40), ("x10K", 20)])
        self.assertEqual((name, count), ("鞋萬能設計圖", 10000))

    def test_count_decimal_requires_suffix(self):
        # 千分位逗号被 OCR 误读成小数点时（x1.234）必须判为非数量行，而不是解析成 1
        name, count = self.parse([("某物品", 40), ("x1.234", 20)])
        self.assertEqual((name, count), ("某物品", None))

    def test_description_lines_rejected(self):
        # 面板描述文本（超长行/含句号）不能被当成物品名
        name, count = self.parse([("弦生留下的杏仁巧克力°上頭绑著可愛的红色蝴蝶结，或不", 44), ("x3", 18)])
        self.assertEqual((name, count), (None, 3))
        name, count = self.parse([("來自蓮實的子彈造型巧克力。尺寸小", 44), ("x5", 18)])
        self.assertEqual((name, count), (None, 5))
        # 正常长度的名字不受影响
        name, count = self.parse([("特級技術筆記（女武神）", 44), ("x761", 18)])
        self.assertEqual((name, count), ("特級技術筆記（女武神）", 761))


class TestBadgeRegex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ba_queue_collector import BADGE_RE, scaled_value
        cls.badge_re = BADGE_RE
        cls.val = staticmethod(scaled_value)

    def test_plain_numbers(self):
        self.assertEqual(self.val(self.badge_re.fullmatch("x792")), 792)
        self.assertEqual(self.val(self.badge_re.fullmatch("x1,234")), 1234)
        self.assertEqual(self.val(self.badge_re.fullmatch("X12")), 12)

    def test_k_abbr(self):
        self.assertEqual(self.val(self.badge_re.fullmatch("x10K")), 10000)
        self.assertEqual(self.val(self.badge_re.fullmatch("x1.2K")), 1200)
        self.assertEqual(self.val(self.badge_re.fullmatch("x99k")), 99000)

    def test_m_abbr(self):
        self.assertEqual(self.val(self.badge_re.fullmatch("x1M")), 1000000)

    def test_non_badges_rejected(self):
        for t in ("48/240", "10K", "x10Kg", "x--", "持有數量", "2026-09-27"):
            self.assertIsNone(self.badge_re.fullmatch(t), t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
