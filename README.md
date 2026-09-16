# Opencli_track — 多平台视频/评论采集管道

基于 [opencli](https://github.com/jackwener/opencli) 的多平台(bilibili / douyin / redbook·小红书)采集管道,含 WebUI 控制台:

1. **① 采集**:按关键词搜索视频/帖子,支持**按时间排序**与发布时间窗过滤,统一 schema 存为 JSON + CSV;
2. **② 评论**:读取 ① 的产出,抓取评论(含楼中楼),**意图识别**判定客户/供给方/无关并分级(HIGH→EXCLUDE),输出 JSON + CSV 表格(含可点击 url 列);
3. **③ 配置**:`config.yaml` 集中控制,可在 WebUI 图形化编辑;
4. **④ WebUI**:图形化配置、启停任务、实时进度与日志、暂停/恢复/停止、结果表格预览。

## 快速开始(一键引导)

```bash
./run.sh          # macOS / Linux
run.ps1           # Windows PowerShell
```

脚本幂等地完成:安装 uv → 由 uv 自动下载 Python 并建 `.venv` → 装 Python 依赖 →
检测/安装 Node 与 `@jackwener/opencli` → `opencli doctor` 体检 → 启动 WebUI
(默认 http://127.0.0.1:8765)。

> **无法内置的部分**:Chrome 浏览器本体与各网站登录态。脚本会检测并提示,
> 需要你手动登录对应网站(浏览器采集依赖本机 Chrome)。

## 命令行用法

```bash
.venv/bin/python main.py --step collect              # ① 采集(读 config.yaml)
.venv/bin/python main.py --step comments             # ② 评论(用最近一次采集结果)
.venv/bin/python main.py --step all                  # ①+②
.venv/bin/python main.py --step match                # ③ 仅重跑意图判定(用已有评语料,不联网)
.venv/bin/python main.py --config config.test.yaml --step all   # 指定配置文件
.venv/bin/python main.py --webui                     # 启动 WebUI
.venv/bin/python main.py --list-platforms            # 查看已注册平台
```

**前提**:已 `npm install -g @jackwener/opencli` 并完成 `opencli setup`;
**douyin / redbook 需要先在 Chrome 登录对应网站**。未登录时程序检测到 `AUTH_REQUIRED`
会给出提示并跳过该平台,不影响其他平台。

## 目录结构

```
config.yaml            # ③ 全部可调配置
main.py                # CLI 入口(含 --webui)
run.sh / run.ps1       # 跨平台一键引导
requirements.txt       # Python 依赖
pyproject.toml         # 项目元数据与依赖声明
src/
  models.py            # VideoItem / CommentItem 统一数据模型
  intent.py            # 意图识别算法(召回/方向消歧/帖子上下文/作者/去重/分级)
  platforms/
    base.py            # 平台抽象基类 PlatformBase(能力声明 + 统一接口)
    bilibili.py        # B站:API 搜索 / 浏览器时间窗+排序搜索 / 评论+楼中楼
    douyin.py          # 抖音:搜索(评论上游暂不支持)
    redbook.py         # 小红书:API 搜索 / 浏览器"最新"排序 / 详情增强 / 评论
    __init__.py        # 平台注册表 REGISTRY
  pipeline.py          # 步骤编排 + 去重/时间过滤 + 意图判定 + json/csv 落盘
  jobs.py              # 任务管理:进度上报、暂停/恢复/停止(协作式检查点)
  utils.py             # opencli 封装(限速/重试/错误分类)、时间与数值解析
webui/
  server.py            # FastAPI 后端(REST + WebSocket,仅绑定 127.0.0.1)
  static/              # 单页前端(index.html / app.js / style.css)
tests/
  test_intent.py       # 逐条 golden 断言(规则级)
  test_corpus.py       # 语料级回归(准确率与关键指标下限)
  fixtures/eval_corpus.json  # 评测语料(取自 algorithm.md 的真实样例,带人工标签)
data/videos/           # ① 输出 videos_<platform>_<时间戳>.json
data/comments/         # ② 输出 comments_<platform>_<时间戳>.json
                       #    raw_comments_*.json = 未过滤原始语料(供离线重跑判定)
output/                # 表格输出 *.csv(采集与评论结果)
```

## 评论意图识别(algorithm.md 的实现)

原始方案把「评论里出现陪玩相关词」当成「客户」,问题是 `接单/代打/技术/陪` 这类词
**买家卖家都在用**,所以既有大量同行广告被误判为客户,又漏掉了真正的客户表达
(数据里「怎么点」出现 21 次全部漏检)。现行算法把任务改为
**判断「这个作者有没有购买意图」**,分六个阶段:

| 阶段 | 作用 |
|---|---|
| 0 归一化 | 去表情码、全角归一、还原规避写法(`陪🥣`→`陪玩`、`pw`) |
| 1 分组召回 | 按意图族分组计分(`order`/`availability`/`looking_for`/…),不再「命中即客户」 |
| 2 方向消歧 | 双向词先判方向,卖家方向优先(「我一般接单」≠「现在有人接单吗」) |
| 3 帖子上下文 | 避雷/讨论帖的评论降权,服务售卖帖提权(**只放大已有意图,不制造线索**) |
| 4 作者角色 | 按账号历史评论判断是同行还是客户(跨评论聚合) |
| 5 去重 | 归一化精确 + 同作者近似 + 跨作者刷屏标记 |
| 6 打分分级 | 加权求和 → `lead_level` = HIGH / MEDIUM / LOW / EXCLUDE |

**输出字段**:`lead_level`、`intent`(customer/provider/irrelevant)、`intent_score`、
`positive_patterns`、`negative_patterns`、`reason`(得分构成,便于人工复核)、
`post_type`、`author_role`、`dup_group`/`dup_count`/`is_spam`。
`matched_patterns` 保留为正向命中,兼容旧下游。

**实测效果**(`tests/fixtures/eval_corpus.json`,57 条取自 `algorithm.md` 的真实样例):

| 指标 | 旧(关键词命中) | 新(意图识别) |
|---|---:|---:|
| 三分类准确率 | 26.3% | **100%** |
| 供给方被误判为客户 | 9 | **0** |
| 客户被漏检 | 20 | **0** |

这些指标由 `tests/test_corpus.py` 守住下限(准确率 ≥94%、两类严重错误均为 0),
规则调整导致倒退会直接测试失败。

**调参闭环**:评论采集时原始语料始终落盘为 `raw_comments_*.json`,因此改规则后
可用 `--step match` **不联网重跑判定**并立即看指标变化——这是迭代这套规则的关键。

```bash
.venv/bin/python main.py --step match          # 只重跑判定,不发任何请求
.venv/bin/python -m unittest discover -s tests  # 51 条规则/语料测试
```


## 配置项说明(config.yaml)

| 键 | 说明 | 默认 |
|---|------|------|
| `platforms` | 平台列表:`bilibili` / `douyin` / `redbook`(别名 `xiaohongshu`) | `[bilibili, redbook]` |
| `keywords` | 搜索关键词数组 | `["无畏契约 陪玩"]` |
| `search_order` | 搜索排序:`relevance`综合 / `pubdate`最新 / `views`最多播放(仅B站) | `pubdate` |
| `publish_window_hours` | 只保留最近 N 小时(0=不过滤) | `24` |
| `crawl_count` | 每个关键词最多采集条数 | `50` |
| `dedup_across_runs` | 跨运行去重(详见下方说明) | `false` |
| `enrich_detail` | 详情数据增强(详见下方说明) | `false` |
| `max_comments_per_video` | 每视频一级评论上限(平台 API 上限 50) | `50` |
| `max_replies` | 每条一级评论展开的楼中楼上限(0=不展开) | `10` |
| `match.mode` | `intent`=意图识别(推荐) / `keyword`=旧的关键词命中行为 | `intent` |
| `match.recall` | 正向召回规则分组(组名→`{weight, patterns}`),权重越高越接近成交 | 见配置文件 |
| `match.negative` | 负向排除规则分组(供给方词库),命中扣分 | 见配置文件 |
| `match.direction` | 双向词方向判定(卖家向优先) | `enabled: true` |
| `match.post_context` | 帖子类型加权(避雷帖降权/售卖帖提权) | `enabled: true` |
| `match.author_role` | 作者角色识别(`min_comments`/`seller_ratio`) | `enabled: true` |
| `match.dedup` | 评论去重(`similarity`/`spam_min_authors`) | `enabled: true` |
| `match.thresholds` | 分级阈值 `high`/`medium`/`low` | `6` / `3` / `1` |
| `match.keep_all` | true=输出全部评论(含 EXCLUDE),便于复核 | `true` |
| `match.only_leads` | true=只输出客户线索(HIGH/MEDIUM/LOW) | `false` |
| `match.patterns` / `exclude_patterns` | 仅 `mode: keyword` 时使用(旧行为,保留以支持回退) | — |
| `output.formats` | `json` 固定输出;`csv` 表格 | `[json, csv]` |
| `output.table_columns` | CSV 列(含 `video_url` 可直接跳转原帖) | 见配置文件 |
| `runtime.request_interval_seconds` | 相邻 opencli 调用间隔(限速防风控) | `3` |
| `runtime.retry` / `timeout` | 瞬时错误重试次数 / 单次调用超时 | `2` / `180` |
| `runtime.browser_window` | 浏览器窗口模式 | `background` |
| `webui.host` / `port` / `open_browser` | WebUI 绑定与启动行为 | `127.0.0.1` / `8765` / `true` |

新增配置项:在 `pipeline.py` 读取处给默认值即可向后兼容。WebUI 保存配置前会校验
(正则合法性、阈值大小关系、权重与相似度范围等),非法配置不会写入磁盘。

### `dedup_across_runs` — 跨运行去重

**只作用于采集步骤,不作用于评论。** 开启后用 `data/seen.json` 记录已见过的
`[平台, 内容ID]` 二元组;下次运行时搜索命中的内容若已在文件中就跳过,只追加新的 ID。
关闭(默认)时每次运行都会重新采集搜索结果——热门帖子在多次搜索中反复出现,所以会重复。

三个容易忽略的细节:

- **同一次运行内的去重始终生效**,与该开关无关(同一 ID 在多个关键词下出现会被合并)。
- 记录的是「**搜索结果里出现过**」而非「**写进了输出文件**」——被时间窗过滤掉的 ID 也会被记录。
- `seen.json` **只在整轮采集结束时写一次**,中途停止或崩溃会导致本轮记录丢失。

### `enrich_detail` — 详情数据增强(仅 redbook)

搜索接口只返回标题/作者/点赞,拿不到**收藏数、评论数、正文、标签**。开启后会对
时间窗过滤后的候选逐条调用 `opencli xiaohongshu note`,把这些字段补进 `stats` 与
`extra`(如 `extra.content`、`extra.tags`),用于判断帖子是"服务售卖"还是"避雷讨论"。
代价是每条候选多一次请求(受 `request_interval_seconds` 限速),只处理过滤后的候选,
所以成本可控。关闭(默认)时不发任何额外请求。

> 该选项**不涉及发布时间**。小红书搜索结果的发布时间来自相对文本(如「3小时前」),
> 原始文本保留在 `extra.publish_time_text` 供追溯。


## 统一数据格式

**VideoItem**(`data/videos/*.json` 数组元素):
`platform / id / url / title / author / author_id / publish_time(ISO8601,可为 null) / search_keyword / stats(平台各自的数值指标) / fetched_at / extra`

**CommentItem**(`data/comments/*.json` 数组元素):
`platform / video_id / video_title / video_url / rpid / parent_rpid(null=一级评论) / author / author_id / text / likes / replies / time / matched_patterns(命中的正则串) / fetched_at / extra`

## 各平台能力与实现差异

| 平台 | 搜索 | 时间排序 | 发布时间 | 评论 |
|------|------|---------|---------|------|
| bilibili | ✅ API / 浏览器 | ✅ `order=pubdate`/`click` | ✅ 相对时间(可叠加服务端时间窗) | ✅ 含楼中楼 |
| redbook | ✅ API / 浏览器 | ✅ 浏览器点击「筛选→最新」 | ✅ API 含日期;浏览器含相对时间 | ✅ 含子回复 |
| douyin | ✅ API | ❌ 上游无排序参数 | ❌ 搜索结果不含 | ❌ 上游无通用评论命令 |

**降级策略**:平台不支持某能力时,pipeline 打印警告并退回该平台默认行为,不中断任务。
例如小红书「最新」面板点击失败(风控/改版)时,自动降级为「综合搜索 + 按发布时间客户端排序」。

## WebUI 功能

- **任务页**:选择步骤(全部/仅采集/仅评论)→ 启动;分阶段进度条 + 实时日志(WebSocket 推送,
  并带 HTTP 轮询兜底);暂停/恢复/停止按钮按状态自动启停。
- **配置页**:平台多选、关键词多行输入、排序下拉、各类数值与正则列表,保存即回写 `config.yaml`。
- **结果页**:列出 `data/`、`output/` 全部产物,表格分页预览(JSON/CSV),`url` 列渲染为
  「打开链接」可直接跳转原视频/帖子,并支持下载原文件。

### 任务控制语义

- **暂停**:协作式——在当前 opencli 调用结束后挂起,不丢中间数据;恢复后继续。
- **停止**:下一个检查点抛出终止信号,已完成的结果**保留**在磁盘。

## 新增平台

1. 在 `src/platforms/` 新建文件,继承 `PlatformBase`,实现 `search(keyword)` 与 `comments(video)`,
   并声明能力标志(`supports_time_filter` / `supports_order_sort` / `supports_comments` / `supports_enrich_detail`);
2. 在 `src/platforms/__init__.py` 的 `REGISTRY` 注册名称;
3. 将名称加进 `config.yaml` 的 `platforms` 即可,其余代码零改动。

平台能力声明会让 pipeline 自动跳过不支持的能力并给出警告,而不是报错中断。

## 已知约束

- **浏览器采集**依赖本机 Chrome + opencli 扩展(`opencli doctor` 可体检);扩展掉线时先
  `opencli doctor`,必要时 `opencli daemon restart` 并等待重连。
- **bilibili 搜索页**先渲染卡片骨架再填充内容,已做轮询等待;无时间窗且综合排序时走官方 API。
- **小红书风控**:短时间高频访问详情页会触发 `SECURITY_BLOCK`,该条评论会被跳过(按条容错,
  不中断整体)。建议保持 `request_interval_seconds ≥ 3`。
- **相对时间推算**:页面「3小时前」这类文本换算的绝对时间会随采集时刻轻微漂移,原始文本保留在
  `extra.publish_time_text` 可追溯。另实测发现小红书笔记被编辑/推广时其 ID 前缀时间与页面
  发布时间不一致,故 ID 时间戳仅作兜底排序依据。
- **意图识别的局限**:规则来自 `algorithm.md` 的经验总结 + 57 条样例的调参,真实语料上仍需迭代;
  `author_role` 依赖作者 ID,小红书有(上游 `userId`)、**B 站没有**(只有昵称),故 B 站的
  作者级判定按昵称匹配,存在同名误合并的风险。`--step match` 就是为快速迭代这套规则而设计的。
- **douyin** 搜索结果不含发布时间(`publish_window` 不生效);上游无通用评论命令,评论步骤跳过。
- 单视频评论单次上限 50,超出部分不翻页;全量采集请提高 `crawl_count` 并耐心等待限速间隔。
- 仅采集公开数据做分析;评论中出现的联系方式请勿用于骚扰或二次分发。
