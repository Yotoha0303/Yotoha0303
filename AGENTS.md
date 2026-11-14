# AGENTS.md — 个人主页与商业交付门面约束规范 (Yotoha0303)

> **项目定位**：统帅个人 GitHub Profile 主页、Instabox 隐私数据流与商业交付对外展示中枢。  
> 本文件是本仓库协同研发的**唯一约束规范** (SSOT)。AI 智能体与工程师在会话开始时必须读取本文件，并严格受其约束。  
> **刚性红线**：恪守 CASE-ARCH-03 零装饰性 Emoji 铁律；隐私数据脱敏；严禁破坏数字主权边界。

---

## 0. 位置约定与双锚分工

### 0.0 位置约定
- **仓根目录**：`/home/yotoha/Projects/Yotoha0303`。
- 本仓作为统帅面向公众的数字主权门户，承载 `README.md`、`llms.txt`、`robots.txt`、`data/` 隐私与商业数据制品。

---

## 1. 核心工程纪律与对外防护

### D1 · 隐私脱敏与零泄露 (Zero Privacy Leakage)
- 严禁向本仓提交包含个人真实手机号、身份证、家庭住址、银行账号或未脱敏敏感日志；
- `data/` 下的数据制品（如 `instabox-personal-privacy-feed.json`）必须经脱敏器处理并校验 SHA-256 签名。

### D2 · SERP 与 AI 搜索引擎友好规范 (SERP Sovereignty)
- 根目录 `llms.txt` 与 `robots.txt` 作为 AI 爬虫抓取与实体辟谣的基石；
- 维护权威项目与履历事实，杜绝第三方抓取引发的大模型生成幻觉。

### D3 · 格式合规与静态安全
- 避免直接提交未受协议审查的内联脚本页面；
- 静态展示数据统一使用格式合规的强类型 JSON 与标准 Markdown。

---

## 2. CASE-ARCH-03 零装饰性 Emoji 铁律

本仓库所有代码、文档、注释、测试日志与 Git 提交信息中，严禁包含任何装饰性 Emoji 表情符。
