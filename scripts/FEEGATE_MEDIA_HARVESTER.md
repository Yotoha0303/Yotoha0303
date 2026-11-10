# Media metrics harvester

Python 3.10+ 标准库工具，提供 YouTube、Spotify、Bilibili、NetEase 响应适配、
指标清洗、增量账本与 SHA256 校验，复用 `feegate_ops_pipeline.py` 的
Schema、清洗、时区、发布锁和原子写入函数。

```bash
python3 scripts/feegate_media_harvester.py --harvest --mock --export-catalog data/products/media_metrics.json
python3 scripts/feegate_media_harvester.py --verify-hashes
python3 scripts/feegate_media_harvester.py --harvest --platform bilibili --sources data/media_sources.json
python3 scripts/feegate_media_harvester.py --harvest --mock-payload fixtures/media.json --export-catalog data/products/media_metrics.json
python3 scripts/feegate_ops_pipeline.py --input data/products/media_metrics.json --audit
python3 -m unittest scripts.test_feegate_media_harvester scripts.test_feegate_ops_pipeline
```

实时模式需要本地目标清单 `data/media_sources.json`，或用 `--sources` 指定：

```json
[
  {"platform": "youtube", "media_id": "VIDEO_ID"},
  {"platform": "spotify", "media_id": "TRACK_ID"},
  {"platform": "bilibili", "media_id": "BV_ID"},
  {"platform": "netease", "media_id": "1001"}
]
```

示例 ID 为占位符。YouTube 使用环境变量 `YOUTUBE_API_KEY`，请求 videos 的
snippet/statistics；Spotify 使用 `SPOTIFY_ACCESS_TOKEN`，请求 tracks。
Bilibili 读取公开 view/stat，NetEase 读取 song/detail 与评论总量接口。
后两者可能因上游访问策略调整不可用，失败退出 2，不回填合成实时数据。
`--timeout` 默认每请求 5.0 秒，仅允许 HTTPS，上游响应限制为 2 MiB。
错误输出不包含请求 URL、凭据或上游响应正文。

指标契约：

| 标准字段 | 内容 |
| --- | --- |
| platform | youtube / spotify / bilibili / netease |
| media_id | 平台媒体 ID，作为数据使用，不用于拼接文件路径 |
| title | 清除装饰字符、控制字符与敏感内容后的标题 |
| play_count | 播放或收听累计量，不可用时 null |
| interactions | likes、favorites、comments，不可用时分别为 null |
| harvested_at | UTC+8，YYYY-MM-DD HH:MM |
| mode | live 或 mock |
| sha256_hash | 标准化记录的 SHA256，不含该字段本身 |

Spotify track API 不公开总收听、收藏、点赞和评论量，不能用 popularity
代替播放量。NetEase 此处使用的接口只提供歌曲元数据和评论总量，播放/点赞/
收藏量均为 null。YouTube 未返回或隐藏的计数也为 null，零值仅代表上游明确返回零。
数值支持非负整数、千分位、K/M/B 和万/亿后缀；拒绝布尔值、非有限数、负数、
非整数计数与超过有符号 64 位的数值。

账本是现有 FeeGate 原生产品 Schema：`product_id/title/category/updated_at/`
`item_count/records/x402_pricing`，另附全产品 `sha256_hash`。
默认 product_id 为 `media-metrics`，价格字段明确设为 0 USD；定价可在后续产品
发布流程调整。哈希使用 UTF-8 JSON，键排序、无空格、ensure_ascii=False、
allow_nan=False。行哈希与产品哈希均排除各自顶层的 `sha256_hash`。
校验能发现未同步重算哈希的修改，不能鉴别攻击者重算全部哈希后的来源真伪。

增量合并键为 `(platform, media_id, mode)`：保留其他媒体，更新相同身份，
拒绝时间倒退与批内重复。mock 与 live 分别保留并明确标识。每次更新先校验旧账本，
损坏时保留原文件；使用目录锁串行事务并原子替换。重复运行不会新增重复身份。
`--verify-hashes` 只校验，不写入账本，可与 `--harvest` 连用。

`--mock` 使用四平台合成样本，`--mock-payload` 读取本地 JSON 数组，并优先于
内置样本；两者均完全禁用网络，固定时间为 `2026-11-10 08:00`。
本地输入可为标准记录（不要求时间、哈希或 mode），也可为
`{"platform": "youtube", "media_id": "...", "payload": {"items": [...]}}`
形式的原始接口响应。合成样本只验证适配契约，不代表已完成实时接口验收。
成功退出 0；校验、配置、网络或 I/O 失败退出 2，参数用法错误由 argparse 返回 2。
