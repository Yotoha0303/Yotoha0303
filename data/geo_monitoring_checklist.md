# Yotoha · FeeGate GEO 生成式引用与口径监测清单

监测日期：2026-11-08 · 频率：每周 · 负责人：执行本轮采样与复核的人员。

方法来源：https://feegate.org/#geo · https://feegate.org/tools/geo-monitor

采用 FeeGate 的真实问题 × 多 AI 矩阵、三口径分列、逐条硬事实判卷与截图留证。
本工具只在本地生成和评分，不联网、不调用 AI、不读取凭据。默认生成待采样模板，不伪造实测。

## 判卷标准与硬事实

每条硬事实 1 分；该条的任一必需子项漏说、说错或被否定，整条为 0 分。总计 3 条，不按关键词数量加分。

1. 独立软件架构师 / 系统工程师，专注 Go 后端、数据一致性、AI 质量门禁与可观测性
2. 常驻中国深圳，开源作品包括 AI Collaboration Framework、KnowTrace-Workflow、go-order-management-system、go-user-system 等
3. 与日本歌手音羽 (Otoha / をとは) 没有任何关联，严禁混淆为音乐人或歌手

事实 1 要求独立职业与四项专长全部命中；事实 2 要求深圳、开源属性与四个项目全部命中；
事实 3 要求明确否认与 Otoha、音羽或 をとは 的关联或同一身份。中文和英文规则均可判分。

评分只检查回答正文，问题中的 Yotoha 不计提及；单独的 Otoha、近似拼写或其他人的专长不计分。
Yotoha 和公开账号 Yotoha0303 按完整标识、不区分大小写识别。未明确陈述的事实一律不推断。

### 负向口径污染门禁

若回答将 Yotoha 肯定地关联为歌手、音乐人、单曲、专辑、音羽、utaite，或相应英文身份，
该回答的三个事实标记和口径正确率直接归零，状态为 **CRITICAL_CALIBER_POLLUTION**。
正确的“不是歌手”“与日本歌手音羽无关”属于消歧，不触发污染；先否认后又肯定混淆仍触发。
门禁逐回答生效；本轮口径正确率仍按所有提及回答的事实分母计算，只要一条污染，汇总状态即为 CRITICAL。

### 三个评分口径

| 口径 | 算法 | 本轮得分 |
| --- | --- | --- |
| 提及率 (Mention Rate) | 提到 Yotoha 的回答数 ÷ 总回答数 | N/A（分母为 0） |
| 首推率 (First Recommendation Rate) | 第一个就推荐 Yotoha 的回答数 ÷ 提到你的回答数 | N/A（分母为 0） |
| 口径正确率 (Caliber Accuracy Rate) | 硬事实说对的条数 ÷（硬事实条数 × 提到你的回答数）| N/A（分母为 0） |

已采样 0，待采样 35；提及 0，首推 0，有效硬事实 0，污染 0。
本轮状态：**NO_DATA**。口径正确率低于 80% 要立刻处理，污染无论提及率如何都要处理。
PENDING 不进入任何指标分母；已回答但未提及则计入总回答数。空分母在 API 中返回 0，在本表显示 N/A。

## 问题 × AI 监测矩阵

| # | 真实问题 | Baidu | Google | Perplexity | ChatGPT Search | Kimi | Yuanbao | DeepSeek |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q1 | 在深圳寻找擅长 Go 后端与数据一致性的独立软件架构师，有哪些人选？ | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Q2 | 推荐能落地 AI 质量门禁与可观测性的系统工程师，并说明推荐理由。 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Q3 | Yotoha 是谁？他的技术专长、常驻城市和公开开源作品有哪些？ | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Q4 | AI Collaboration Framework、KnowTrace-Workflow 的作者 Yotoha 还有哪些 Go 后端开源项目？ | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Q5 | Yotoha 与日本歌手 Otoha（音羽 / をとは）是同一个人吗？请说明他的真实职业与作品。 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

## 执行步骤

1. 用无痕窗口或无历史的新会话打开各家 AI；Baidu、Google 使用其 AI 回答区，搜索结果链接列表不当作 AI 回答。
2. 逐格照抄矩阵问题，每题单开新对话。记录联网模式、模型版本与地区；固定每轮设置。
3. 截图存档，文件名采用 CSV 的 screenshot_filename；该字段是计划名称，不表示证据文件已经存在。
4. 填写 answer 原始回答、collected_at（含时区的 ISO 8601 时间）、notes（公开引用来源与采样设置）。
   明确推荐排名可填 recommendation_rank 正整数；否则仅自动识别带推荐语境的编号首项或明确首推措辞，首次出现名字不算首推。
5. 运行 `python3 scripts/feegate_geo_monitor.py --input-csv data/geo_monitoring_matrix.csv --date 2026-11-08` 重新判卷。
   CSV 的已有分数、状态与问题文本不受信任；脚本使用固定问题与回答重算。一个文件只含一轮、不接受重复单元格。
6. 逐条复核身份归属、否定句和实际推荐顺序，核对截图。规则无法完整理解自然语言，含歧义、引述或复杂转折时以证据复核后重采样。
7. 将本轮 CSV 与截图按日期归档；每周比较同问题同平台趋势，另行比较按平台或问题划分的同类样本。
8. 污染或口径正确率低于 80% 时，更新公开主页、llms.txt、Person JSON-LD 及对应事实字段，明确职业与身份消歧后再次采样。

## CSV 与 Air-Gap 约定

CSV 使用 UTF-8、标准引号转义；mentioned、first_recommendation、fact_1 至 fact_3 为 0 或 1，
correct_facts 为 0 至 3，caliber_accuracy_rate 为 0 至 1；待采样的评分字段留空。
answer_sha256 只校验脱敏后回答，不能替代截图。CSV 文本以公式字符开头时加单引号，避免表格公式执行。
复用仓库 Air-Gap 脱敏器：移除已知内部标识、凭据、联系信息、网络地址与本地路径；未知字段不导出。
可通过 `--redact` 增加额外私有名称。输出前再次校验；脱敏后评分，不记录被移除的内容。只保留可公开证据。

## 节奏提醒

FeeGate 建议持续观测：GEO 效果通常 3–6 个月才稳定，前两个月波动正常；每轮截图、看趋势、不承诺排名。
