# 开发者精选情报库：把个人书签整理成可检索的数据作品

发布位置：项目广场 / 个人作品

我是独立开发者，这个作品来自整理个人技术书签的实际需求：链接积累后，按目录翻找越来越费时间。
于是用 Python 做了离线封装，把公开链接及分类整理成「开发者精选情报库」（Developer Curated Intel v1），
便于在技术调研、独立产品选型和个人知识库建设时查询、导入。

## 项目与实现

当前版本：2,176 条记录，2,168 个不同 URL，1,746 个域名（保留子域名），14 个一级分类、54 条完整分类路径。跨分类的相同 URL 保留各自记录，因此记录数与不同 URL 数有区别。
主题覆盖 AI、架构开发、生产力、设计及技术研究等方向。

技术栈：Python 标准库 + SQLite FTS5 + JSONL。数据库包含 `bookmarks`、`bookmarks_fts`，
并为 `domain`、`category_path` 建立普通索引；全文索引由触发器随记录增删改同步。
六个数据字段为 `title`、`url`、`category_path`、`domain`、`add_date`、`tags`。
FTS5 使用 `unicode61`，支持词元与前缀查询；任意中文标题片段可用 `LIKE`，这会扫描记录。
`add_date` 是书签添加时间，不代表内容发布时间。

## 免费试用与交流

先下载[50 条免费 JSON 样例](https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/products/sample_developer_curated_intel_v1.json)，这是完整版顺序中的前 50 条，格式为 JSON。
可先核对分类、字段和元数据是否适合自己的工作流；试用文件不含完整数据库。
欢迎在本帖评论讨论分类粒度、SQLite 检索和个人知识库整理方式，也欢迎反馈链接或分类问题。

## 版本与交付

完整版建议价：29.9 元人民币 / 4.99 美元，按商品渠道选择一种币种结算，一次购买交付 v1。
需要完整格式时，可在本帖站内交流中了解商品页面与交付安排。

- `developer_curated_intel_v1.sqlite`：完整记录与 FTS5 索引。
- `developer_curated_intel_v1.jsonl`：完整记录，UTF-8，每行一个 JSON 对象。
- `DEVELOPER_INTEL_PRODUCT_MANIFEST.md`：字段说明、统计、查询示例与文件校验值。

交付的是公开资源链接、分类与元数据；目标网站内容与商标归其权利人所有。本版是静态快照，未在线验证链接可用性，不附带目标网站内容或账号访问权。当前价格不承诺终身更新，后续版本与团队授权可另行定价。
