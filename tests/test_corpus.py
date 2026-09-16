"""语料级回归:用 algorithm.md 的真实样例锁定整体准确率。

与 test_intent.py 的区别:那些是逐条断言具体标签,这里是端到端跑整份语料,
并守住两个最关键的指标——供给方不得被误判为客户、客户不得被漏检。
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.intent import IntentEngine
from src.models import CommentItem
from src.pipeline import _comment_from_dict

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "eval_corpus.json")


def load_cases():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["cases"]


def predict(config=None):
    """跑整份语料,返回 {text: 判定结果}。"""
    cs = [CommentItem(platform="redbook", video_id=f"n{i}", text=c["text"],
                      video_title=c["post"], author=f"u{i}", author_id=f"id{i}")
          for i, c in enumerate(load_cases())]
    IntentEngine(config or {}).run(cs)
    return {c.text: c for c in cs}


class TestCorpusAccuracy(unittest.TestCase):
    """整体准确率与关键指标的下限守护。

    这些阈值是实测值留出的余量:规则调整不应让指标倒退。
    """

    # 实测:三分类准确 57/57;供给方误判客户 0;客户漏检 0
    MIN_ACCURACY = 0.94
    MAX_PROVIDER_AS_CUSTOMER = 0
    MAX_MISSED_CUSTOMER = 0

    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()
        cls.pred = predict()

    def test_three_class_accuracy(self):
        ok = sum(1 for c in self.cases
                 if self.pred[c["text"]].intent == c["label"])
        acc = ok / len(self.cases)
        self.assertGreaterEqual(
            acc, self.MIN_ACCURACY,
            f"三分类准确率 {ok}/{len(self.cases)} = {acc:.1%} 低于下限 "
            f"{self.MIN_ACCURACY:.0%}")

    def test_provider_never_misjudged_as_customer(self):
        """最关键指标:把同行当成客户会浪费真实的沟通成本。"""
        bad = [(c["text"], self.pred[c["text"]].reason)
               for c in self.cases
               if c["label"] == "provider"
               and self.pred[c["text"]].intent == "customer"]
        self.assertLessEqual(
            len(bad), self.MAX_PROVIDER_AS_CUSTOMER,
            f"供给方被误判为客户 {len(bad)} 条: {bad[:5]}")

    def test_no_missed_customers(self):
        """客户漏检直接损失商机。"""
        missed = [(c["text"], self.pred[c["text"]].intent,
                   self.pred[c["text"]].reason)
                  for c in self.cases
                  if c["label"] == "customer"
                  and self.pred[c["text"]].intent != "customer"]
        self.assertLessEqual(
            len(missed), self.MAX_MISSED_CUSTOMER,
            f"客户被漏检 {len(missed)} 条: {missed[:5]}")

    def test_customers_are_ranked_at_least_medium(self):
        """真实客户样本应基本落在 MEDIUM 及以上,便于优先跟进。"""
        high = [c for c in self.cases if c["label"] == "customer"
                and self.pred[c["text"]].lead_level in ("HIGH", "MEDIUM")]
        total = [c for c in self.cases if c["label"] == "customer"]
        self.assertGreaterEqual(len(high) / len(total), 0.9,
                                "客户样本过多落在 LOW/EXCLUDE")

    def test_providers_are_excluded(self):
        """供给方不应出现在线索表(HIGH/MEDIUM/LOW)。"""
        leaked = [c["text"] for c in self.cases
                  if c["label"] == "provider"
                  and self.pred[c["text"]].lead_level
                  in ("HIGH", "MEDIUM", "LOW")]
        self.assertEqual(leaked, [], f"供给方泄漏进线索表: {leaked[:5]}")


class TestKeywordModeIsPreserved(unittest.TestCase):
    """旧模式可达性:mode=keyword 必须仍按老语义工作(升级可回退)。"""

    def test_keyword_mode_matches_old_semantics(self):
        from src.pipeline import apply_intent
        cases = load_cases()
        cfg = {"match": {"mode": "keyword",
                         "patterns": ["陪玩", "陪陪", "接陪", "代打", "代练", "接单", "陪", "技术"],
                         "exclude_patterns": [], "keep_all": True}}
        comments = [CommentItem(platform="redbook", video_id="v",
                                text=c["text"], video_title=c["post"])
                    for c in cases]
        rows = apply_intent(cfg, comments, None)
        by_text = {r["text"]: r for r in rows}
        # 旧行为:命中任一关键词即视为"客户"
        self.assertTrue(by_text["怎么点"]["matched_patterns"] == []
                        or by_text["怎么点"]["lead_level"] == "MATCH")
        self.assertEqual(by_text["陪玩接单很累"]["lead_level"], "MATCH",
                         "带'陪'的评论在旧模式下应命中")
        self.assertEqual(by_text["哈哈哈哈哈"]["lead_level"], "",
                         "无关键词的评论在旧模式下不应命中")

    def test_exclude_wins_even_with_keep_all(self):
        """修复:keep_all 下被排除的评论不应再与'未命中'混淆。"""
        from src.pipeline import apply_intent
        cfg = {"match": {"mode": "keyword", "patterns": ["陪玩"],
                         "exclude_patterns": ["私我"], "keep_all": True}}
        comments = [CommentItem(platform="redbook", video_id="v",
                                text="陪玩私我"),
                    CommentItem(platform="redbook", video_id="v",
                                text="陪玩")]
        rows = apply_intent(cfg, comments, None)
        texts = [r["text"] for r in rows]
        self.assertNotIn("陪玩私我", texts, "被排除的评论即使 keep_all 也应剔除")
        self.assertIn("陪玩", texts)


class TestOfflineRerun(unittest.TestCase):
    """离线重跑:调规则不应需要重爬(--step match 的核心价值)。"""

    def test_step_match_reads_raw_and_outputs(self):
        import tempfile
        from src import pipeline
        raw_dir = os.path.join(pipeline.DATA_DIR, "comments")
        os.makedirs(raw_dir, exist_ok=True)
        path = os.path.join(raw_dir, "raw_comments_unittest_19700101_000000.json")
        rows = [{"platform": "unittest", "video_id": "v1",
                 "video_title": c["post"], "url": "", "text": c["text"],
                 "author": "a", "author_id": "i", "rpid": f"c{i}",
                 "parent_rpid": None, "likes": 0, "replies": 0,
                 "time": None, "matched_patterns": [], "fetched_at": "t",
                 "extra": {}}
                for i, c in enumerate(load_cases()[:8])]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            cfg = {"platforms": ["unittest"],
                   "match": {"mode": "intent", "keep_all": True}}
            out = pipeline.step_match(cfg, None)
            self.assertEqual(len(out), 8)
            self.assertTrue(all("lead_level" in r for r in out))
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
