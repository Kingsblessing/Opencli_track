"""意图识别 golden 用例:锁死 algorithm.md 的判定目标。

运行: .venv/bin/python -m unittest discover -s tests -v
用例中的帖子标题默认取无关类型,以隔离"文本规则"本身的判定;
帖子上下文、方向消歧、作者画像、去重各自单列测试。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.intent import (AuthorProfiler, Deduper, IntentEngine, classify_post,
                        normalize)
from src.models import CommentItem

# 无关主贴:隔离文本规则
NEUTRAL_POST = "今天天气不错随便聊聊"


def judge(texts, title=NEUTRAL_POST, cfg=None):
    cs = [CommentItem(platform="t", video_id="v", text=t, video_title=title)
          for t in texts]
    IntentEngine(cfg or {}).run(cs)
    return cs


class TestCustomerIntent(unittest.TestCase):
    """买家意图:必须被识别为 customer,且强意图达到 HIGH。"""

    HIGH_CASES = [
        # algorithm.md §4 下单类(数据中漏检最严重、价值最高)
        "怎么点", "怎么点单", "怎么下单", "点单", "怎么约", "想点单",
        # §5 询问是否还有人接
        "现在有人接吗", "现在接吗", "还接吗", "新手接吗", "能接吗", "今天接吗",
        "晚上接吗", "还能点嘛", "现在接不接",
        # §6 明确找陪玩
        "找个陪", "来个女陪", "来个男陪", "有没有陪", "有陪吗", "需要陪",
        "想找个娱乐陪", "有没有技术女陪钻石", "有没有技术陪",
        # §7 价格咨询
        "多少钱一把", "多少钱一小时", "多少一把", "啥价格", "怎么收费", "贵不贵",
        # §13 代打需求(带寻求动词)
        "有没有靠谱代打呀，现在来", "找代打", "有没有代练",
    ]

    def test_high_intent_cases(self):
        for c in judge(self.HIGH_CASES):
            with self.subTest(text=c.text):
                self.assertEqual(c.intent, "customer", f"{c.text} | {c.reason}")
                self.assertEqual(c.lead_level, "HIGH", f"{c.text} | {c.reason}")
                self.assertGreaterEqual(c.intent_score, 6)

    def test_weak_intent_is_not_high(self):
        """一起玩/求带 属弱-中意向,不应直接判 HIGH。"""
        for c in judge(["一起玩吗", "可以一起玩", "有人一起玩吗"]):
            with self.subTest(text=c.text):
                self.assertNotEqual(c.lead_level, "HIGH", f"{c.text} | {c.reason}")
                self.assertLess(c.intent_score, 6)

    def test_game_demand_detected(self):
        for c in judge(["带我上黄金", "帮我上分", "求带带上钻石"]):
            with self.subTest(text=c.text):
                self.assertEqual(c.intent, "customer", f"{c.text} | {c.reason}")


class TestProviderExclusion(unittest.TestCase):
    """陪玩提供方自我营销:必须被排除。"""

    CASES = [
        # algorithm.md §16 示例
        "娱乐女陪20/h欢迎私信",
        "主页有战绩，可试音",
        "本人神话二，青年音，可试音，主页有战绩",
        # §10 卖家词库
        "历史赋能 青年音 可教学",
        "15一把 包c 在线等单",
        "我是陪玩，全天在线",
        "还招人吗",
        "招陪～找板～新店开业",
        "进团私聊，俱乐部直招",
        "无畏契约代打默认直播，秒上号",
        "蹲板板，希望可以陪到你",
        "双服神话，主页看报价",
        # 段位+陪 的自我描述,以及 §10 点名的"希望可以陪到"
        "希望可以陪到你",
        "赋能陪……",
        "下午5点以后双神话陪",
        "钻石陪在线等",
    ]

    def test_provider_excluded(self):
        for c in judge(self.CASES):
            with self.subTest(text=c.text):
                self.assertEqual(c.intent, "provider", f"{c.text} | {c.reason}")
                self.assertEqual(c.lead_level, "EXCLUDE", f"{c.text} | {c.reason}")

    def test_rank_plus_pei_with_demand_verb_is_customer(self):
        """段位+陪 的负向规则不能误伤带需求动词的问法。

        "赋能陪"是自我描述,但"有没有赋能陪"是买家在问有没有该段位的陪玩。
        """
        for text in ["有没有赋能陪", "来个赋能陪", "有没有钻石陪", "找个神话陪"]:
            c = judge([text])[0]
            with self.subTest(text=text):
                self.assertEqual(c.intent, "customer", f"{text} | {c.reason}")
                self.assertNotEqual(c.lead_level, "EXCLUDE", f"{text} | {c.reason}")

    def test_no_negative_hits_is_not_provider(self):
        c = judge(["陪玩接单很累"])[0]
        self.assertEqual(c.intent, "irrelevant", c.reason)
        self.assertEqual(c.lead_level, "EXCLUDE", c.reason)


class TestBidirectionalWords(unittest.TestCase):
    """接单/代打/技术/陪 是双向词,方向判定是核心(§11/§12/§13)。"""

    def test_seller_direction(self):
        c = judge(["我一般接单就是晚上八点以后"])[0]
        self.assertEqual(c.intent, "provider", c.reason)
        self.assertEqual(c.lead_level, "EXCLUDE", c.reason)

    def test_buyer_direction(self):
        c = judge(["现在有人接单吗"])[0]
        self.assertEqual(c.intent, "customer", c.reason)
        self.assertEqual(c.lead_level, "HIGH", c.reason)

    def test_bare_technology_is_not_a_lead(self):
        """单独出现"技术"只是泛话题,不能单独构成客户判据(§12)。"""
        for text in ["这技术不行", "技术好服务好", "娱乐技术 什么声线都有 来试试吗哥哥"]:
            c = judge([text])[0]
            with self.subTest(text=text):
                self.assertNotEqual(c.lead_level, "HIGH", f"{text} | {c.reason}")

    def test_technology_with_demand_verb_is_a_lead(self):
        c = judge(["有没有技术女陪钻石"])[0]
        self.assertEqual(c.intent, "customer", c.reason)
        self.assertEqual(c.lead_level, "HIGH", c.reason)


class TestPostContext(unittest.TestCase):
    """主贴类型影响同一条评论的可信度(§19)。"""

    def test_classify(self):
        cases = {
            "瓦陪9.9h": "service_selling",
            "瓦点单｜乖乖电竞性价比封神": "service_selling",
            "避雷无畏契约陪": "discussion",
            "转账完被拉黑": "discussion",
            "找个无畏契约陪玩": "user_seeking",
        }
        for title, expect in cases.items():
            with self.subTest(title=title):
                self.assertEqual(classify_post(title), expect)

    def test_discussion_post_suppresses_chatter(self):
        cs = judge(["陪玩接单很累", "这价格也太高了吧"], title="避雷无畏契约陪")
        for c in cs:
            with self.subTest(text=c.text):
                self.assertEqual(c.lead_level, "EXCLUDE", f"{c.text} | {c.reason}")
                self.assertEqual(c.post_type, "discussion")

    def test_strong_intent_survives_any_post(self):
        for title in ["瓦陪9.9h", "避雷无畏契约陪", "找个无畏契约陪玩", NEUTRAL_POST]:
            c = judge(["怎么点"], title=title)[0]
            with self.subTest(title=title):
                self.assertEqual(c.lead_level, "HIGH", f"{title} | {c.reason}")

    def test_post_bonus_cannot_manufacture_a_lead(self):
        """正向加成只放大已有意图,不能把泛话题抬成线索。"""
        for post in ["瓦陪9.9h", NEUTRAL_POST]:
            c = judge(["陪玩接单很累"], title=post)[0]
            with self.subTest(post=post):
                self.assertEqual(c.lead_level, "EXCLUDE", c.reason)


class TestAuthorRole(unittest.TestCase):
    """同一账号的历史行为比单条评论更可靠(§17)。"""

    def _mk(self, texts, author="用户A", author_id="uid1"):
        return [CommentItem(platform="redbook", video_id="v", text=t,
                            author=author, author_id=author_id,
                            video_title=NEUTRAL_POST) for t in texts]

    def test_provider_author_penalised(self):
        cs = self._mk(["15一把", "欢迎私信", "可试音", "主页有战绩"])
        IntentEngine({}).run(cs)
        for c in cs:
            with self.subTest(text=c.text):
                self.assertEqual(c.author_role, "provider")
                self.assertEqual(c.author_comment_count, 4)
                self.assertGreaterEqual(c.author_seller_ratio, 0.6)

    def test_customer_author_boosted(self):
        cs = self._mk(["怎么点", "多少钱一把", "现在有人接吗"])
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].author_role, "customer")
        self.assertGreater(cs[0].intent_score, 0)

    def test_single_comment_author_is_unknown(self):
        cs = self._mk(["怎么点"])
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].author_role, "unknown")

    def test_author_keys_are_isolated(self):
        """不同 author_id 不能被合并统计。"""
        cs = (self._mk(["15一把", "欢迎私信"], author="甲", author_id="uidA")
              + self._mk(["怎么点"], author="乙", author_id="uidB"))
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].author_comment_count, 2)
        self.assertEqual(cs[2].author_comment_count, 1)

    def test_author_role_can_be_disabled(self):
        cs = self._mk(["15一把", "欢迎私信"])
        IntentEngine({"author_role": {"enabled": False}}).run(cs)
        self.assertEqual(cs[0].author_role, "unknown")


class TestDedup(unittest.TestCase):
    """重复与刷屏会让线索数虚高(§18)。"""

    def test_exact_duplicate_grouped(self):
        cs = [CommentItem(platform="t", video_id="v", text="怎么点",
                          author=f"u{i}", video_title=NEUTRAL_POST) for i in range(3)]
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].dup_count, 3)
        self.assertTrue(cs[0].is_representative)
        self.assertFalse(cs[1].is_representative)
        self.assertEqual(cs[0].dup_group, cs[2].dup_group)

    def test_same_author_near_duplicate_merged(self):
        cs = [CommentItem(platform="t", video_id="v", text=t, author="甲",
                          author_id="id1", video_title=NEUTRAL_POST)
              for t in ["怎么点", "怎么点啊", "怎么点呀"]]
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].dup_count, 3, "同作者近似重复应归并")

    def test_different_text_not_merged(self):
        cs = [CommentItem(platform="t", video_id="v", text=t, author="甲",
                          author_id="id1", video_title=NEUTRAL_POST)
              for t in ["怎么点", "多少钱一把"]]
        IntentEngine({}).run(cs)
        self.assertEqual(cs[0].dup_count, 1)

    def test_cross_author_spam_flagged(self):
        cs = [CommentItem(platform="t", video_id="v", text="招陪～找板～新店开业",
                          author=f"号{i}", author_id=f"id{i}",
                          video_title=NEUTRAL_POST) for i in range(4)]
        IntentEngine({}).run(cs)
        self.assertTrue(cs[0].is_spam)

    def test_singleton_not_spam(self):
        cs = [CommentItem(platform="t", video_id="v", text="怎么点",
                          video_title=NEUTRAL_POST)]
        IntentEngine({}).run(cs)
        self.assertFalse(cs[0].is_spam)

    def test_dedup_can_be_disabled(self):
        cs = [CommentItem(platform="t", video_id="v", text="怎么点",
                          video_title=NEUTRAL_POST) for _ in range(3)]
        IntentEngine({"dedup": {"enabled": False}}).run(cs)
        self.assertEqual(cs[0].dup_group, "")


class TestPriceDetection(unittest.TestCase):
    """价格识别:中文相邻时 \\b / (?!\\w) 会失效,是真实踩过的坑。

    Python3 中中文属于 \\w,所以 "5r一把" 里 r 与 一 之间没有词边界,
    用 \\b 或 (?!\\w) 写的价格正则全部匹配不到——而中文里价格后面几乎
    总是紧跟中文("20元一把"),这类漏检会让大量供给方广告逃过排除。
    """

    CASES = ["15r一把", "20元一把", "10米一局", "30一小时", "白菜价陪玩",
             "主页看报价 15r", "20/h", "15r"]

    def test_prices_followed_by_chinese_detected(self):
        for text in self.CASES:
            c = judge([text])[0]
            with self.subTest(text=text):
                self.assertEqual(c.intent, "provider", f"{text} | {c.reason}")
                self.assertEqual(c.lead_level, "EXCLUDE", f"{text} | {c.reason}")

    def test_price_not_confused_with_words(self):
        """数字+字母的普通词不应被当作报价(如型号/ID)。"""
        for text in ["打了15把", "段位钻石2", "我是新手"]:
            c = judge([text])[0]
            with self.subTest(text=text):
                self.assertNotEqual(c.intent, "provider", f"{text} | {c.reason}")

    def test_price_post_classified_as_service_selling(self):
        from src.intent import classify_post
        self.assertEqual(classify_post("瓦赋能黑奴陪5r一把，可教学，不C不要钱！！"),
                         "service_selling")
        self.assertEqual(classify_post("瓦陪9.9h"), "service_selling")


class TestNormalize(unittest.TestCase):
    """归一化:表情码/全角/规避写法(§1 数据里真实存在的写法)。"""

    def test_strips_bilibili_emoji_codes(self):
        self.assertEqual(normalize("求个陪玩[doge]"), "求个陪玩")
        self.assertEqual(normalize("还接吗[汤圆][打call]"), "还接吗")

    def test_evasion_symbol_mapped(self):
        self.assertIn("陪玩", normalize("陪🥣来说"))
        self.assertIn("陪玩", normalize("培🥣"))

    def test_fullwidth_normalised(self):
        self.assertEqual(normalize("１５一把"), "15一把")

    def test_empty_and_none(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_normalised_text_is_what_matches(self):
        """规避写法归一后仍能被识别为客户。"""
        c = judge(["有无陪🥣 现在能点吗"])[0]
        self.assertEqual(c.intent, "customer", c.reason)


class TestEngineContract(unittest.TestCase):
    """字段契约与向后兼容。"""

    def test_all_fields_populated(self):
        c = judge(["怎么点"])[0]
        for name in ("positive_patterns", "negative_patterns", "intent",
                     "intent_score", "lead_level", "reason", "post_type",
                     "author_role", "author_comment_count",
                     "author_seller_ratio", "dup_group", "dup_count",
                     "is_representative", "is_spam"):
            self.assertTrue(hasattr(c, name), f"缺少字段 {name}")

    def test_matched_patterns_kept_for_compat(self):
        c = judge(["怎么点"])[0]
        self.assertEqual(c.matched_patterns, c.positive_patterns)
        self.assertTrue(c.matched_patterns)

    def test_to_dict_includes_new_fields(self):
        d = judge(["怎么点"])[0].to_dict()
        for k in ("intent", "intent_score", "lead_level", "reason",
                  "post_type", "author_role", "dup_group", "is_spam"):
            self.assertIn(k, d)

    def test_empty_input(self):
        stat = IntentEngine({}).run([])
        self.assertEqual(stat["total"], 0)

    def test_reason_is_human_readable(self):
        c = judge(["怎么点"])[0]
        self.assertIn("召回", c.reason)
        self.assertNotEqual(c.reason, "无命中")

    def test_custom_weights_from_config(self):
        """配置可覆盖权重:提高 social_weak 权重后,弱意图评论也能达到 HIGH。"""
        cfg = {"thresholds": {"high": 6, "medium": 3, "low": 1},
               "recall": {"social_weak": {"weight": 8, "patterns": [r"一起玩"]}}}
        c = judge(["一起玩吗"], cfg=cfg)[0]
        self.assertEqual(c.lead_level, "HIGH", c.reason)
        self.assertEqual(c.intent_score, 8.0, c.reason)

    def test_strong_group_hit_forces_high_per_design(self):
        """algorithm.md §21:强客户词命中且无卖家词 → 高优先级,
        该规则独立于权重(权重影响分数,不改变"强意图"的定性)。"""
        cfg = {"thresholds": {"high": 6, "medium": 3, "low": 1},
               "recall": {"order": {"weight": 1, "patterns": [r"怎么点"]}}}
        c = judge(["怎么点"], cfg=cfg)[0]
        self.assertEqual(c.lead_level, "HIGH", c.reason)
        self.assertEqual(c.intent_score, 1.0, c.reason)

    def test_strong_group_with_seller_signal_not_high(self):
        """强客户词 + 明显卖家词 → 不得判 HIGH(§21)。"""
        c = judge(["怎么点？本人可接，主页有战绩"])[0]
        self.assertNotEqual(c.lead_level, "HIGH", c.reason)

    def test_thresholds_configurable(self):
        cfg = {"thresholds": {"high": 1, "medium": 1, "low": 1}}
        c = judge(["一起玩吗"], cfg=cfg)[0]
        self.assertEqual(c.lead_level, "HIGH", c.reason)


class TestStats(unittest.TestCase):
    def test_stats_shape(self):
        stat = IntentEngine({}).run(
            judge(["怎么点", "欢迎私信主页", "陪玩接单很累"]))
        self.assertEqual(stat["total"], 3)
        self.assertIn("HIGH", stat["levels"])
        self.assertEqual(stat["intents"]["customer"], 1)
        self.assertIn("unique_leads", stat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
