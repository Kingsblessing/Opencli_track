"""评论意图识别:召回 → 方向消歧 → 帖子上下文 → 作者角色 → 去重 → 分级。

纯规则实现(仅 stdlib),不发起任何网络请求,便于离线重跑(--step match)与单元测试。
设计依据见 algorithm.md:目标不是判断"这条评论谈没谈到陪玩",而是判断
"这个作者有没有产生购买陪玩服务的意图"。

六个阶段:
  0 归一化      去表情码/全角归一/规避写法映射(陪🥣→陪玩)
  1 分组召回    按意图族分组计分,不再"命中即客户"
  2 方向消歧    接单/代打/陪玩 等双向词先判方向,卖家方向优先
  3 帖子上下文  按主贴类型加权(需求型主贴的评论多为同行)
  4 作者角色    跨评论聚合同一作者,识别其历史行为倾向
  5 去重        精确/同作者近似/跨作者刷屏
  6 打分分级    加权求和 → lead_level
"""
import difflib
import re
import unicodedata
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .models import CommentItem

# ---------------- 默认规则(配置可覆盖/扩充) ----------------
# 权重取自 algorithm.md §15 的评分表;阈值见 DEFAULT_THRESHOLDS。
DEFAULT_RECALL: Dict[str, dict] = {
    # 下单咨询 —— 数据中价值最高、却大量漏检的一类
    "order": {"weight": 6, "patterns": [
        r"(?:怎么|咋|如何|想|要)\s*(?:点(?:单|陪|一个)?|下单|约|订)",
        r"(?:点单|点陪|下单|下陪)",
    ]},
    # 询问是否还有人接 —— 接近成交
    "availability": {"weight": 6, "patterns": [
        r"(?:现在|目前|今天|今晚|明天|晚上|明早|白天|新手|新人|还)?\s*"
        r"(?:有人|有没|有没有|能|可以)?\s*接(?:单|陪|代)?\s*(?:吗|么|嘛|不)",
        r"(?:能|可以|还|现在|今天|晚上)\s*点(?:单|陪)?\s*(?:吗|么|嘛|不)",
        # 服务能力询问:能打吗/能带吗/能教吗/能不能玩(§5"能接吗"推广到其他服务动词)
        r"(?:能|可以|会)\s*(?:打|玩|带|教|接|点|陪|上分|开|教学)\s*"
        r"(?:吗|么|嘛|不(?!动|会|住|知))",
        # 服务动词 + 疑问助词("零基础的教吗")。
        # 不含"玩":裸"玩吗"几乎总是社交组队;服务询问走"能玩吗"那一支。
        # 后视排除"一起/一块",避免"一起打吗"这类免费组队被判为服务咨询。
        r"(?<!一起)(?<!一块)(?:打|带|教|陪|点)\s*(?:吗|么|嘛)",
        # 空闲/在线询问:必须带疑问助词,避免"全天有空"这类供给方自我描述
        r"(?<!全天)(?<!随时)(?<!长期)(?<!一直)\s*"
        r"(?:现在|今天|今晚|晚上|白天)?\s*(?:还|有)?\s*空\s*(?:吗|么|嘛)",
        r"(?:有人吗|ok吗|OK吗)\s*[?？]?",
    ]},
    # 弱询问:"在吗"过于通用(可能只是打招呼),故单列低权重,不与强意图同级
    "presence_weak": {"weight": 3, "patterns": [
        r"(?:在吗|在不在)\s*[?？]?",
    ]},
    # 咨询式提问("问问钻石")
    "inquiry": {"weight": 4, "patterns": [
        r"问问\s*(?:钻石|铂金|黄金|白银|超凡|神话|赋能|价格|多少|陪|代)",
    ]},
    # 明确"找陪玩"
    "looking_for": {"weight": 5, "patterns": [
        r"(?:找|求|来个|来一[个位]?|要个|需要|想找|想要|有没有|有没|有无|有)\s*"
        r".{0,8}(?:陪玩|陪陪|女陪|男陪|技术陪|娱乐陪|代打|代练|陪)",
    ]},
    # 价格咨询
    "price_query": {"weight": 5, "patterns": [
        r"(?:多少(?:钱)?|什么价(?:格)?|啥价(?:格)?|价格多少|怎么收费|贵不贵|几块|几十)\s*"
        r".{0,8}(?:一小时|一把|一局|一h|陪|陪玩)?",
    ]},
    # 指定技能需求(代打/技术陪)
    "skill_demand": {"weight": 4, "patterns": [
        r"(?:有没有|有没|有|找|求|来个|需要|想找|谁有|哪里有)\s*"
        r".{0,8}(?:代打|代练|技术陪|技术女陪|靠谱|打手)",
    ]},
    # 上分/带打需求
    "game_demand": {"weight": 3, "patterns": [
        r"(?:带我|带一下|带带|帮我|陪我)\s*.{0,10}"
        r"(?:上分|上黄金|上铂金|上钻石|上超凡|上神话|上赋能|晋级)",
        r"(?:求|想|要|需要|缺|找|来个|有无|救救|帮我)\s*.{0,6}"
        r"(?:上分|上黄金|上铂金|上钻石|上超凡|上神话|晋级|带带|带我|拉我|固定队|车位)",
    ]},
    # 自然语言弱意图 —— 可能是免费组队,不等于付费客户
    # 注:"有人吗"归 availability(服务是否可约),不在此重复计分
    "social_weak": {"weight": 1, "patterns": [
        r"(?:一起玩|一块玩|一起打|有人玩|没人一起|开黑|组队|找个队友|缺个队友|加水友)",
    ]},
    # 泛话题词:仅作为上下文特征,单独出现不足以构成判据(algorithm.md §22)
    "topic_generic": {"weight": 0.5, "patterns": [
        r"(?:陪|技术|代打|代练|上分|赋能|神话|老板|板板|价格|接单)",
    ]},
}

DEFAULT_NEGATIVE: Dict[str, dict] = {
    # 陪玩/打手自我营销
    "self_promo": {"weight": -5, "patterns": [
        r"(?:本人|我是(?:陪|打手|代)|国服|双服|双(?:神话|赋能|区)|多赛季|上过.{0,4}(?:神话|赋能|超凡)|"
        r"(?:上|本)?赛季.{0,4}(?:超凡|神话|赋能|神二|神三))",
        r"希望可以陪到",
        # 段位+陪 的自我描述(如"赋能陪""钻石陪");用固定宽度后视排除
        # "有没有赋能陪"这类带需求动词的问法,避免误伤买家
        r"(?<!有)(?<!要)(?<!求)(?<!找)(?<!来)(?<!个)(?<!缺)"
        r"(?:赋能|神话|超凡|钻石|铂金|黄金)\s*陪",
    ]},
    # 招募陪玩/俱乐部
    "recruitment": {"weight": -5, "patterns": [
        r"(?:招(?:陪|人|打手|新)|进团|入团|俱乐部|新店|开业|营业|平移|公会|工作室|战队招)",
    ]},
    # 引导联系(私信/主页/加好友)
    "contact_solicit": {"weight": -4, "patterns": [
        r"(?:主页|私(?:信|我|聊)|加(?:我|v|V|微|q|Q)|留(?:个)?(?:v|V|q|Q)|滴滴我|dd我|"
        r"抠|扣扣|微信|企微|vx|VX|qq|QQ|联系我|找我|可以加|随时加)",
    ]},
    # 报价/促销
    # 注意:Python3 中中文属于 \w,故数字/字母与中文相邻时 \b 与 (?!\w) 会失效
    # ("5r一把"里 r 与 一 之间无词边界)。故用 (?![A-Za-z0-9]) 只排除拉丁字母数字。
    "pricing_offer": {"weight": -4, "patterns": [
        r"(?:首单|优惠|白菜价|黑奴价|特价|打折|活动价|包月|套餐|价目表|价格表|价位|可议价|便宜出)",
        r"\d+\s*(?:r|h|元|米|块)(?![A-Za-z0-9])",
        r"\d+\s*(?:一把|一局|一小时|一h)",
        r"\d+\s*/\s*(?:h|把|局|小时)",
    ]},
    # 在线等单/秀战绩
    "wait_order": {"weight": -4, "patterns": [
        r"(?:在线等单|蹲(?:板板|老板|单)|等单|接单记录|战绩|可验枪|可验号|秒上号|"
        r"全天在线|长期在线|随时在线|欢迎来|求单|接单中|开始接单|直接来)",
    ]},
    # 声音/情绪价值类卖点
    "voice_sales": {"weight": -3, "patterns": [
        r"(?:(?:青年|少女|御姐|萝莉|奶音|正太|叔音|青受|磁性|极品)音|试音|声音好|"
        r"情绪价值|声线|会唱歌|会唠|陪聊|开麦)",
    ]},
    # 教学/包赢类卖点
    "teaching_offer": {"weight": -3, "patterns": [
        r"(?:可教学|包c|包C|不c包退|包赢|输了结|输前三|结前三|教学局|陪练教学|"
        r"包上分|不赢不结|保上|把杀|带躺)",
    ]},
}

# 双向词方向判定:卖家方向优先(algorithm.md §11/§13)
DEFAULT_DIRECTION = {
    "seller": {"weight": -5, "patterns": [
        r"(?:我|本人|一直|目前|平时|长期|全天|日常|随缘)\s*.{0,6}(?:接单|接陪|代打|代练|陪玩)",
    ]},
    "buyer": {"weight": 4, "patterns": [
        r"(?:现在|今天|今晚|明天|晚上|还|有人|有没|有没有|能|可以|新手)\s*"
        r".{0,4}(?:接单|接陪|接)\s*(?:吗|么|嘛|不)",
    ]},
}

# 主贴类型(algorithm.md §19):不同类型帖子下,同样的评论可信度不同
DEFAULT_POST_RULES = {
    "discussion": {"adjust": -2, "patterns": [
        r"(?:避雷|吐槽|曝光|被骗|拉黑|骗子|翻车|投稿|经历|难受|离谱|踩雷|维权)",
    ]},
    "service_selling": {"adjust": 1, "patterns": [
        r"(?:点单|俱乐部|电竞|陪玩店|瓦陪店|营业|接单|首单|优惠|"
        r"工作室|\d+\.\d+\s*(?:r|h|元|米)|"
        r"\d+\s*(?:r|h)(?![A-Za-z0-9]))",
    ]},
    "user_seeking": {"adjust": -1, "patterns": [
        r"(?:找个|求个|来个|有没有|需要一个|想找个|求推荐|急需).{0,10}(?:陪|代练|代打|带)",
    ]},
}

# 命中这些分组视为"强客户意图"(algorithm.md §21)
STRONG_GROUPS = {"order", "availability", "looking_for", "price_query", "skill_demand"}

DEFAULT_THRESHOLDS = {"high": 6, "medium": 3, "low": 1}

# 规避写法映射(平台常见谐音/符号替代)
EVASION_MAP = [
    (r"(?:陪|培)\s*🥣", "陪玩"),
    (r"🥣", "玩"),
    (r"\bpw\b", "陪玩"),
    (r"\bPW\b", "陪玩"),
]

_EMOJI_CODE = re.compile(r"\[[^\]\n]{1,8}\]")


def normalize(text: str) -> str:
    """归一化:去 B 站表情码、全角归一、折叠空白、还原规避写法。

    匹配跑在归一化文本上,原文始终保留在各条记录里。
    """
    if not text:
        return ""
    s = str(text)
    s = _EMOJI_CODE.sub(" ", s)          # [doge] [汤圆] [打call] …
    s = unicodedata.normalize("NFKC", s)  # 全角→半角、兼容字符归一
    for pat, rep in EVASION_MAP:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _hits(text: str, patterns: List[re.Pattern]) -> List[str]:
    """返回命中片段(用于 reason 展示证据),同一模式只取首个命中。"""
    out = []
    for p in patterns:
        m = p.search(text)
        if m:
            out.append(m.group(0)[:14])
    return out


def classify_post(title: str, rules: Optional[dict] = None) -> str:
    """主贴分类:discussion / service_selling / user_seeking / irrelevant。"""
    text = normalize(title)
    if not text:
        return "irrelevant"
    rules = rules or DEFAULT_POST_RULES
    # 讨论/避雷类最强,优先判定(避免被标题里的"陪"带偏)
    for name in ("discussion", "service_selling", "user_seeking"):
        cfg = rules.get(name) or {}
        for pat in cfg.get("patterns", []):
            if re.search(pat, text, re.IGNORECASE):
                return name
    return "irrelevant"


class IntentRules:
    """编译配置中的分组规则;缺省时回落到内置种子规则。"""

    def __init__(self, match_cfg: dict):
        m = match_cfg or {}
        self.recall = self._build(m.get("recall"), DEFAULT_RECALL)
        self.negative = self._build(m.get("negative"), DEFAULT_NEGATIVE)
        self.direction = self._build_dir(m.get("direction"), DEFAULT_DIRECTION)
        self.post_rules = m.get("post_context", {}).get("rules") or DEFAULT_POST_RULES
        th = m.get("thresholds") or {}
        self.thresholds = {
            "high": float(th.get("high", DEFAULT_THRESHOLDS["high"])),
            "medium": float(th.get("medium", DEFAULT_THRESHOLDS["medium"])),
            "low": float(th.get("low", DEFAULT_THRESHOLDS["low"])),
        }
        pc = m.get("post_context") or {}
        self.post_enabled = bool(pc.get("enabled", True))
        dr = m.get("direction") or {}
        self.direction_enabled = bool(dr.get("enabled", True))
        ar = m.get("author_role") or {}
        self.author_enabled = bool(ar.get("enabled", True))
        self.author_min = int(ar.get("min_comments", 2))
        self.author_ratio = float(ar.get("seller_ratio", 0.6))

    @staticmethod
    def _build(cfg, defaults) -> Dict[str, dict]:
        """配置提供则用配置(可只覆盖部分组),否则用内置种子规则。"""
        src = cfg if isinstance(cfg, dict) and cfg else defaults
        out = {}
        for name, spec in src.items():
            pats = spec.get("patterns", []) if isinstance(spec, dict) else []
            out[name] = {
                "weight": float(spec.get("weight", 0)) if isinstance(spec, dict) else 0.0,
                "compiled": [re.compile(p, re.IGNORECASE) for p in pats],
            }
        return out

    @staticmethod
    def _build_dir(cfg, defaults) -> Dict[str, dict]:
        src = cfg if isinstance(cfg, dict) and cfg else defaults
        out = {}
        for name in ("seller", "buyer"):
            spec = src.get(name) or defaults.get(name) or {}
            out[name] = {
                "weight": float(spec.get("weight", 0)),
                "compiled": [re.compile(p, re.IGNORECASE)
                             for p in spec.get("patterns", [])],
            }
        return out


class AuthorProfiler:
    """跨评论聚合作者行为:单条评论不可靠,账号历史更能说明角色。

    作者键优先用 (platform, author_id);缺失时退回 (platform, video_id, author)。
    注意:小红书评论有 author_id,但 B 站只返回昵称,故 B 站存在同名误合并的风险。
    """

    def __init__(self, min_comments: int = 2, ratio: float = 0.6):
        self.min_comments = min_comments
        self.ratio = ratio

    @staticmethod
    def key_of(c: CommentItem) -> Tuple[str, str]:
        if c.author_id:
            return (c.platform, f"id:{c.author_id}")
        return (c.platform, f"name:{c.video_id}:{c.author}")

    def analyze(self, comments: List[CommentItem]) -> Dict[Tuple[str, str], dict]:
        agg = defaultdict(lambda: {"total": 0, "seller": 0, "buyer": 0})
        for c in comments:
            k = self.key_of(c)
            a = agg[k]
            a["total"] += 1
            if getattr(c, "_prov_signal", False):
                a["seller"] += 1
            elif getattr(c, "_strong_hit", False):
                a["buyer"] += 1
        stats = {}
        for k, a in agg.items():
            if a["total"] < self.min_comments:
                stats[k] = {"role": "unknown", "count": a["total"], "ratio": 0.0}
                continue
            seller_ratio = a["seller"] / a["total"] if a["total"] else 0.0
            buyer_ratio = a["buyer"] / a["total"] if a["total"] else 0.0
            if seller_ratio >= self.ratio:
                role = "provider"
            elif buyer_ratio >= self.ratio:
                role = "customer"
            else:
                role = "unknown"
            stats[k] = {"role": role, "count": a["total"],
                        "ratio": round(seller_ratio, 2)}
        return stats


class Deduper:
    """三级去重:归一化精确 / 同作者近似 / 跨作者刷屏。

    algorithm.md §18:同一机构常在多个帖子下批量刷同样的话,
    不去重会导致"以为找到 20 个客户,实际只有 5 个人"。
    """

    def __init__(self, similarity: float = 0.85, spam_min_authors: int = 3):
        self.similarity = similarity
        self.spam_min_authors = spam_min_authors

    def run(self, comments: List[CommentItem]) -> dict:
        groups: List[dict] = []          # {key, text, members:[idx]}
        by_exact: Dict[str, int] = {}    # 归一化文本 → 组号
        by_author: Dict[tuple, List[int]] = defaultdict(list)
        assign: Dict[int, int] = {}

        for idx, c in enumerate(comments):
            text = getattr(c, "_norm", "") or normalize(c.text)
            setattr(c, "_norm", text)
            gid = None
            # 1) 完全相同的归一化文本
            if text and text in by_exact:
                gid = by_exact[text]
            else:
                # 2) 同作者近似文本(改了标点/语气词的复读)
                akey = AuthorProfiler.key_of(c)
                for cand in by_author[akey]:
                    if text and groups[cand]["text"] and \
                            difflib.SequenceMatcher(
                                None, text, groups[cand]["text"]).ratio() >= self.similarity:
                        gid = cand
                        break
            if gid is None:
                gid = len(groups)
                groups.append({"text": text, "members": [], "authors": set()})
                if text:
                    by_exact.setdefault(text, gid)
            groups[gid]["members"].append(idx)
            groups[gid]["authors"].add(AuthorProfiler.key_of(c))
            by_author[AuthorProfiler.key_of(c)].append(gid)
            assign[idx] = gid

        for idx, c in enumerate(comments):
            gid = assign[idx]
            g = groups[gid]
            setattr(c, "dup_group", f"g{gid}")
            setattr(c, "dup_count", len(g["members"]))
            setattr(c, "is_representative", g["members"][0] == idx)
            setattr(c, "is_spam", len(g["authors"]) >= self.spam_min_authors)
        return {
            "groups": len(groups),
            "duplicates": sum(len(g["members"]) - 1 for g in groups),
            "spam_groups": sum(1 for g in groups
                               if len(g["authors"]) >= self.spam_min_authors),
        }


class IntentEngine:
    """编排六个阶段,原地写入判定字段。"""

    def __init__(self, match_cfg: dict):
        m = match_cfg or {}
        self.rules = IntentRules(m)
        ar = m.get("author_role") or {}
        self.profiler = AuthorProfiler(min_comments=int(ar.get("min_comments", 2)),
                                       ratio=float(ar.get("seller_ratio", 0.6)))
        dd = m.get("dedup") or {}
        self.dedup_enabled = bool(dd.get("enabled", True))
        self.deduper = Deduper(similarity=float(dd.get("similarity", 0.85)),
                               spam_min_authors=int(dd.get("spam_min_authors", 3)))

    # ---- 单条评论的基础判定(不含作者级修正) ----
    def _base(self, c: CommentItem) -> dict:
        text = getattr(c, "_norm", "") or normalize(c.text)
        setattr(c, "_norm", text)
        pos, pos_groups = [], []
        for name, spec in self.rules.recall.items():
            h = _hits(text, spec["compiled"])
            if h:
                pos_groups.append((name, spec["weight"], h))
                pos.extend(h)
        neg, neg_groups = [], []
        for name, spec in self.rules.negative.items():
            h = _hits(text, spec["compiled"])
            if h:
                neg_groups.append((name, spec["weight"], h))
                neg.extend(h)
        # 方向消歧:卖家方向优先,命中后买家方向不再计分
        dir_name, dir_weight, dir_hits = None, 0.0, []
        if self.rules.direction_enabled:
            sh = _hits(text, self.rules.direction["seller"]["compiled"])
            if sh:
                dir_name, dir_weight, dir_hits = "seller", \
                    self.rules.direction["seller"]["weight"], sh
            else:
                bh = _hits(text, self.rules.direction["buyer"]["compiled"])
                if bh:
                    dir_name, dir_weight, dir_hits = "buyer", \
                        self.rules.direction["buyer"]["weight"], bh
        post_type = classify_post(c.video_title, self.rules.post_rules) \
            if self.rules.post_enabled else "irrelevant"
        post_adj = 0.0
        # 正向加成只放大"已存在的真实意图",不能把仅泛话题词的低信号抬成线索;
        # 负向(讨论/避雷帖)始终生效。
        real_recall = any(name != "topic_generic" for name, _, _ in pos_groups)
        if self.rules.post_enabled:
            raw = float((self.rules.post_rules.get(post_type) or {}).get("adjust", 0))
            post_adj = raw if (raw <= 0 or real_recall) else 0.0
        pos_total = sum(w for _, w, _ in pos_groups)
        neg_total = sum(w for _, w, _ in neg_groups) + dir_weight
        strong = any(g in STRONG_GROUPS for g, _, _ in pos_groups)
        # 卖家信号:卖家方向命中,或负向总量已达强标记阈值
        prov = (dir_name == "seller") or (neg_total <= -4)
        return {
            "text": text, "pos": pos, "neg": neg,
            "pos_groups": pos_groups, "neg_groups": neg_groups,
            "dir_name": dir_name, "dir_hits": dir_hits,
            "post_type": post_type, "post_adj": post_adj,
            "pos_total": pos_total, "neg_total": neg_total,
            "strong": strong, "prov": prov,
            "score": pos_total + neg_total + post_adj,
        }

    def run(self, comments: List[CommentItem], ctx=None) -> dict:
        if not comments:
            return {"total": 0}
        base = []
        for i, c in enumerate(comments):
            if ctx is not None and i % 200 == 0:
                ctx.checkpoint()
            b = self._base(c)
            # 供作者画像使用的初步信号
            setattr(c, "_prov_signal", b["prov"])
            setattr(c, "_strong_hit", b["strong"])
            base.append(b)

        author_stats = self.profiler.analyze(comments) if self.rules.author_enabled \
            else {}
        th = self.rules.thresholds
        counts = defaultdict(int)
        for c, b in zip(comments, base):
            adj = 0.0
            role, acount, aratio = "unknown", 0, 0.0
            if self.rules.author_enabled:
                st = author_stats.get(AuthorProfiler.key_of(c))
                if st:
                    role, acount, aratio = st["role"], st["count"], st["ratio"]
                    if role == "provider":
                        adj = -3.0
                    elif role == "customer":
                        adj = 2.0
            score = b["score"] + adj
            # 分级:强意图命中且无卖家信号 → 直接 HIGH(algorithm.md §21)
            if score < th["low"]:
                level = "EXCLUDE"
            elif b["strong"] and not b["prov"]:
                level = "HIGH"
            elif score >= th["high"]:
                level = "HIGH"
            elif score >= th["medium"]:
                level = "MEDIUM"
            else:
                level = "LOW"
            if level == "EXCLUDE":
                intent = "provider" if (b["prov"] or b["neg_total"] < 0) else "irrelevant"
            else:
                intent = "customer"

            c.positive_patterns = b["pos"]
            c.negative_patterns = b["neg"]
            c.matched_patterns = b["pos"]          # 兼容旧下游字段
            c.intent = intent
            c.intent_score = round(score, 1)
            c.lead_level = level
            c.post_type = b["post_type"]
            c.author_role = role
            c.author_comment_count = acount
            c.author_seller_ratio = aratio
            c.reason = self._reason(b, adj, role)
            counts[level] += 1
            counts[f"intent:{intent}"] += 1

        dstat = {}
        if self.dedup_enabled:
            dstat = self.deduper.run(comments)
        return {
            "total": len(comments),
            "levels": {k: v for k, v in counts.items() if not k.startswith("intent:")},
            "intents": {k.split(":", 1)[1]: v for k, v in counts.items()
                        if k.startswith("intent:")},
            "dedup": dstat,
            "unique_leads": counts.get("HIGH", 0) + counts.get("MEDIUM", 0),
        }

    @staticmethod
    def _reason(b: dict, adj: float, role: str) -> str:
        parts = []
        if b["pos_groups"]:
            parts.append("召回 " + "、".join(
                f"{name}『{'/'.join(h)}』({w:+g})" for name, w, h in b["pos_groups"]))
        if b["neg_groups"]:
            parts.append("负向 " + "、".join(
                f"{name}『{'/'.join(h)}』({w:+g})" for name, w, h in b["neg_groups"]))
        if b["dir_name"]:
            parts.append(f"方向 {b['dir_name']}『{'/'.join(b['dir_hits'])}』")
        if b["post_adj"]:
            parts.append(f"帖子 {b['post_type']}({b['post_adj']:+g})")
        if adj:
            parts.append(f"作者 {role}({adj:+g})")
        return " ; ".join(parts) if parts else "无命中"
