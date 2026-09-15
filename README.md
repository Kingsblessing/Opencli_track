# Opencli_track — 多平台视频/评论采集管道

基于 [opencli](https://github.com/jackwener/opencli) 的三平台(bilibili / douyin / redbook·小红书)采集管道:

1. **① 采集**:按关键词搜索视频/帖子,统一 schema 存为 JSON;
2. **② 评论**:读取 ① 的产出,抓取评论(含楼中楼),正则匹配后输出 JSON + CSV 表格;
3. **③ 配置**:`config.yaml` 集中控制平台、关键词、时间窗、数量、正则、限速等。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install pyyaml
.venv/bin/python main.py --list-platforms            # 查看已注册平台
.venv/bin/python main.py --step collect              # ① 按默认 config.yaml 采集视频
.venv/bin/python main.py --step comments             # ② 对最近一次采集结果抓评论+匹配
.venv/bin/python main.py --step all                  # ①+② 一步到位
.venv/bin/python main.py --config config.test.yaml --step all   # 小规模冒烟测试
```

前提:已 `npm install -g @jackwener/opencli` 并完成 `opencli setup`;
**douyin / redbook 需要先在 Chrome 登录对应网站**(bilibili 已登录可用)。
未登录时程序会检测 `AUTH_REQUIRED` 并给出提示后跳过该平台,不影响其他平台。

## 目录结构

```
config.yaml            # ③ 全部可调配置(见下)
main.py                # CLI 入口
src/
  models.py            # VideoItem / CommentItem 统一数据模型
  platforms/
    base.py            # 平台抽象基类 PlatformBase
    bilibili.py        # B站:API 搜索 / 浏览器时间窗搜索 / 评论+楼中楼
    douyin.py          # 抖音:搜索(评论暂不支持,上游无通用命令)
    redbook.py         # 小红书:搜索 / 评论(--with-replies)
    __init__.py        # 平台注册表 REGISTRY
  pipeline.py          # 步骤编排 + 正则匹配 + json/csv 落盘
  utils.py             # opencli 子进程封装(限速/重试/错误分类)、时间与数值解析
data/videos/           # ① 输出 videos_<platform>_<时间戳>.json
data/comments/         # ② 输出 comments_<platform>_<时间戳>.json
output/                # ② 表格 comments_<platform>_<时间戳>.csv
```

## 配置项说明(config.yaml)

| 键 | 说明 | 默认 |
|---|------|------|
| `platforms` | 平台列表,可选 `bilibili` / `douyin` / `redbook` | `[bilibili]` |
| `keywords` | 搜索关键词数组 | `["无畏契约 陪玩"]` |
| `publish_window_hours` | 只保留最近 N 小时(0=不过滤);仅 bilibili 服务端生效 | `24` |
| `crawl_count` | 每个关键词最多采集条数 | `20` |
| `max_comments_per_video` | 每视频一级评论上限(平台 API 上限 50) | `50` |
| `max_replies` | 每条一级评论展开的楼中楼上限(0=不展开) | `10` |
| `match.patterns` | 命中任一即保留,Python 正则 | 陪玩/陪陪/接陪/代打/代练/接单 |
| `match.exclude_patterns` | 命中任一则丢弃 | `[]` |
| `match.keep_all` | true=未命中的也保留(`matched_patterns` 为空) | `false` |
| `output.formats` | `json` 固定输出;`csv` 表格 | `[json, csv]` |
| `output.table_columns` | CSV 列,可增删(取自评论字段名) | 见配置文件 |
| `runtime.request_interval_seconds` | 相邻 opencli 调用间隔(限速防风控) | `3` |
| `runtime.retry` / `timeout` | 瞬时错误重试次数 / 单次调用超时 | `2` / `180` |
| `runtime.browser_window` | 浏览器窗口模式 | `background` |

新增配置项:在 `pipeline.py` 读取处给默认值即可向后兼容。

## 统一数据格式

**VideoItem**(`data/videos/*.json` 数组元素):
`platform / id / url / title / author / author_id / publish_time(ISO8601,可为 null) / search_keyword / stats(平台各自的数值指标) / fetched_at / extra`

**CommentItem**(`data/comments/*.json` 数组元素):
`platform / video_id / video_title / video_url / rpid / parent_rpid(null=一级评论) / author / author_id / text / likes / replies / time / matched_patterns(命中的正则串) / fetched_at / extra`

## 新增平台

1. 在 `src/platforms/` 新建文件,继承 `PlatformBase`,实现 `search(keyword)` 与 `comments(video)`;
2. 在 `src/platforms/__init__.py` 的 `REGISTRY` 注册名称;
3. 将名称加进 `config.yaml` 的 `platforms` 即可,其余代码零改动。

平台能力声明(`supports_time_filter` / `supports_comments`)会让 pipeline 自动跳过
不支持的能力并给出警告,而不是报错中断。

## 已知约束

- **bilibili 时间窗过滤**走浏览器搜索页 `pubtime_begin_s/pubtime_end_s` 参数 + DOM 抽取,
  页面渲染耗时波动(已做轮询等待);无时间窗时走官方 API。
- **douyin** 搜索结果不含发布时间(时间窗不生效);opencli 暂无通用评论命令,评论步骤跳过。
- **redbook** 评论命令需要笔记完整 URL(含 `xsec_token`),已由 VideoItem.url 透传;
  输出列名做了防御性映射,若上游字段变化只需调 `redbook.py` 的 `_map_comment_row`。
- 单视频评论单次上限 50,超出部分不翻页;全量采集请提高 `crawl_count` 并耐心等待限速间隔。
- 仅采集公开数据做分析;评论中出现的联系方式请勿用于骚扰或二次分发。
