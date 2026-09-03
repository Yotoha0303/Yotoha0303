# Yotoha | SRE / 后端工程师

我目前将职业与工程实践重点收敛到 **站点可靠性工程（SRE）/ 后端可靠性工程**：以 Go 后端能力为基础，继续补强 Linux、容器、CI/CD、可观测性、自动化运维、备份恢复、故障演练与稳定性治理。

我关注的不只是“服务能跑起来”，而是持续验证：

- 服务如何构建、发布、部署与回滚
- 指标、日志与链路追踪（Trace）如何帮助发现和定位故障
- 数据如何备份、恢复并验证完整性
- 分布式业务如何处理幂等、一致性、消息可靠性与故障恢复
- 自动化测试、CI/CD、运行手册（Runbook）与故障演练如何形成可重复的运行保障
- 如何从指标（Metrics）、日志（Logs）、链路追踪（Traces）进一步演进到 SLI、SLO、错误预算（Error Budget）与事故复盘（Incident Review）

---

## SRE / 可靠性技术栈

### 系统与运行环境

- Linux / Ubuntu
- Go / Gin / GORM
- Docker / Docker Compose
- Kubernetes / kind / Kustomize
- Nginx / HTTP / TCP/IP

### 数据与消息

- MySQL / PostgreSQL / Redis
- RabbitMQ / Transactional Outbox
- SQL Migration / Transaction / Row Lock / Idempotency

### 交付与自动化

- GitHub Actions / GHCR
- 不可变容器镜像 / 基于 Digest 的部署
- CI 门禁 / 冒烟测试（Smoke Test）/ 回滚
- Makefile / Shell 运维脚本
- 备份 / 恢复 / SHA-256 完整性校验

### 可观测性与可靠性

- Prometheus / Grafana / Alert Rules
- OpenTelemetry / OTLP / Tempo
- 结构化日志 / Request ID / Trace Context
- 健康检查 / Readiness / Graceful Shutdown
- 故障演练 / Runbook / Postmortem Template
- 有界压测 / P50 / P95 / P99 / 容量分析

### 下一阶段

- Prometheus + Grafana + Alertmanager
- ELK / EFK 集中式日志
- Python + Shell 运维自动化
- 基于 Ansible 的主机初始化与部署
- Ubuntu VM 类生产环境部署
- SLI / SLO / Error Budget / Incident Management

---

# 核心可靠性项目

## 1. go-order-management-system-cloudnative-lab

**当前最主要的 SRE / 云原生工程实验项目。**

从 Go 订单系统演进出的多服务运行环境，重点验证微服务边界、消息可靠性、应用韧性、Kubernetes 交付、可观测性、备份恢复、故障演练与自动 CD。

仓库：  
https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab

### 可靠性亮点

- 7 个运行单元、4 个独立服务数据库
- Order Saga / Inventory Reservation / Compensation / Reconciliation
- Transactional Outbox + RabbitMQ TTL/DLX + Publisher Confirm + Manual ACK
- Deadline / Retry / Exponential Backoff / Circuit Breaker / Rate Limit
- Kubernetes + Kustomize + Probe + Resources + PDB + Ingress
- Prometheus + Grafana + OpenTelemetry Collector + Tempo
- GHCR 不可变镜像、Commit SHA、OCI Digest 与发布清单
- 基于 Digest 的自动部署、Smoke Test、失败版本检测与完整回滚
- 四库逻辑备份、SHA-256 校验、隔离恢复验证
- RabbitMQ / HTTP / Worker Lease / Migration 故障演练
- 运维手册（Operator Runbook）、事故复盘模板与有界压测

---

## 2. KnowTrace

一个“记录优先、AI 辅助整理”的知识采集与可靠知识工作流系统，目前作为 **AI 应用 + 多服务交付 + 运维/SRE 演进项目** 持续开发。

仓库：  
https://github.com/Yotoha0303/KnowTrace

### 当前工程范围

- Next.js / TypeScript + PostgreSQL / Drizzle
- 独立 Go 认证服务 + MySQL + Redis
- Docker Compose 多服务编排与健康检查
- Migration、单元测试、E2E、GitHub CI
- Workspace 数据隔离、版本化知识链与审计边界
- PostgreSQL / MySQL / Uploads 统一备份与恢复流程
- 下一阶段重点：Ubuntu VM 部署、完整 CI/CD、Prometheus/Grafana、ELK、自动化运维与故障演练

---

## 3. go-user-system

一个可自托管的 **Go + Gin + MySQL + Redis** 用户认证与 RBAC 系统，重点体现身份安全、可交付性与最小可运维能力。

仓库：  
https://github.com/Yotoha0303/go-user-system

### 可靠性与安全亮点

- JWT Access / Refresh、Rotation、Token Family 重放检测
- Redis JTI 吊销、账号/IP 双维度登录失败限流
- RBAC、Secure Cookie、可信代理与生产启动校验
- Prometheus HTTP / Runtime / Readiness 指标与基础告警规则
- MySQL 备份、SHA-256 manifest 与隔离恢复演练
- Docker Compose、Kubernetes、CI、CodeQL、Dependabot、GHCR 发布

---

## 4. go-order-management-system

一个面向真实业务约束的 Go 订单库存一致性服务，重点训练 **事务、并发、幂等、消息可靠性与可观测性基础**。

仓库：  
https://github.com/Yotoha0303/go-order-management-system

### 后端可靠性亮点

- MySQL Transaction + Row Lock + Conditional Update
- Redis Lua 预扣、reservation 补偿与 Redis/MySQL 对账
- RabbitMQ 延迟取消 + Transactional Outbox
- Publisher Confirm / Manual ACK / Idempotent Consumer
- Prometheus `/metrics`
- W3C `traceparent`、Request ID、结构化日志与慢 SQL 日志
- Docker Compose / Migration / CI / Health Check

---

# SRE 能力路线

```text
可靠后端
   ↓
CI / 不可变构建
   ↓
自动化交付 / 回滚
   ↓
Ubuntu 运行环境
   ↓
Metrics + Logs + Traces
   ↓
告警 + Runbook
   ↓
备份 / 恢复 / 故障演练
   ↓
SLI / SLO / Error Budget
   ↓
SRE / 可靠性工程
```

当前重点不是继续堆叠工具数量，而是把已有项目真正推进到 **可部署、可观测、可恢复、可回滚、可演练、可复盘** 的状态。

---

# 工程原则

- 可靠性是系统属性，不是一个监控 Dashboard。
- 先定义故障边界，再决定监控、告警和恢复策略。
- 数据库是事实源时，缓存与消息系统必须有明确的一致性与补偿边界。
- 发布流程必须具备可验证的构建产物、健康检查和回滚路径。
- 备份只有经过恢复验证（Restore Verification）后才真正具有恢复价值。
- Metrics、Logs、Traces 应围绕故障定位建立关联，而不是独立堆叠。
- 自动化应建立在已经理解并手工验证过的操作流程之上。
- 不把实验环境包装成生产环境；明确系统能力、证据与边界。
