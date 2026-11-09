<!-- PHALANX-SEC: PUBLIC_SANITIZED -->
# 个人多信息聚合平台与跨平台全景视窗规范
# (PERSONAL_MULTI_PLATFORM_INTELLIGENCE_AGGREGATOR.md)

> **归属仓库**：`Yotoha0303/Yotoha0303` (个人专属主权仓库)  
> **组织隔离原则**：组织仓库严格仅存放组织级集体基建与协同项目；个人多信息聚合阅读平台全量迁移并收敛于个人母港。  
> **生效时间**：2026-11-09 起生效  

---

## 一、平台背景与边界公理

1. **组织与个人资产绝对解耦**：
   - 组织仓库恪守“极简与纯粹”原则，不存放除了组织所需外的任何项目；
   - 个人日常信息阅读、跨平台资讯聚合、极客晨报与商业雷达，完全归属并托管于个人专属主权仓库 `Yotoha0303/Yotoha0303`。
2. **摆脱外部云端依赖**：
   - 彻底摒弃外部免费虚拟机（Instabox）的数据截流与模型预训练风险；
   - 采用本地单机常驻服务 + GitHub 个人主权托管架构，确保个人阅读偏好绝对私密、自主可控。

---

## 二、平台核心功能矩阵

### 1. 跨平台全景直达门户 (Direct Platform Access)
一键直达全球核心技术与商业生态：
- **[HN] Hacker News**：全球极客讨论与 LLM 突破性实践；
- **[GH] GitHub Trending**：开源智能体与系统架构热门项目；
- **[36Kr] 实时商业快讯**：国内 AI 商业化变现、创投资本与出海动态；
- **[LOB] Lobste.rs**：纯粹系统架构与底层工程社区；
- **[ARXIV] cs.AI 论文流**：大模型前沿顶会与学术前瞻；
- **[PH] Product Hunt**：全球新发布 AI 产品雷达；
- **[V2EX] 程序员社区**：开发者一线实战与生态反馈。

### 2. 多源信息流聚合与多维标签体系 (Multi-Source Stream)
- **多平台聚合**：聚合 Hacker News、GitHub、36Kr、Lobste.rs 等信源；
- **多维标签智能分类**：
  - `[AI/智能体]`：大模型推理、自主 Agent、工具调用；
  - `[商业变现/出海]`：SaaS 营收、定价策略、现金流与出海实战；
  - `[芯片硬件]`：端侧算力、ESP32-S3、GPU 与嵌入式协同；
  - `[创投资本]`：天使轮、种子轮与行业投融资风向。

### 3. 三重视图人机协作基石
- **人类晨读与全景聚合视图**：高密度现代卡片排版，3 分钟晨读防线；
- **人机共用 Markdown 视图**：与本地笔记知识库双向同步，智能体与人类共享知识图谱；
- **商业化交付 JSON 视图**：符合 FeeGate 标准，支持 x402 协议原子定价。

---

## 三、本地工程结构与运行动线

- **单页仪表盘**：[`web/today-briefing/index.html`](../web/today-briefing/index.html)
- **本地服务进程**：[`scripts/serve_today_briefing.py`](../scripts/serve_today_briefing.py)（严格监听 `127.0.0.1:8765`）
- **常驻自启服务**：`~/.config/systemd/user/phalanx-today-briefing.service`
- **桌面快捷方式**：`~/.local/share/applications/phalanx-today-briefing.desktop`
- **全局终端命令**：`today-briefing`
- **固定访问地址**：`http://127.0.0.1:8765`
