# Yotoha | SRE / Backend Engineer

我目前将职业与工程实践重点收敛到 **Site Reliability Engineering（SRE）/ 后端可靠性工程**：以 Go 后端能力为基础，继续补强 Linux、容器、CI/CD、可观测性、自动化运维、备份恢复、故障演练与稳定性治理。

我关注的不只是“服务能跑起来”，而是持续验证：

- 服务如何构建、发布、部署与回滚
- 指标、日志与 Trace 如何帮助发现和定位故障
- 数据如何备份、恢复并验证完整性
- 分布式业务如何处理幂等、一致性、消息可靠性与故障恢复
- 自动化测试、CI/CD、Runbook 与故障演练如何形成可重复的运行保障
- 如何从 Metrics / Logs / Traces 进一步演进到 SLI、SLO、Error Budget 与 Incident Review

---

## SRE / Reliability Stack

### Systems & Runtime

- Linux / Ubuntu
- Go / Gin / GORM
- Docker / Docker Compose
- Kubernetes / kind / Kustomize
- Nginx / HTTP / TCP/IP

### Data & Messaging

- MySQL / PostgreSQL / Redis
- RabbitMQ / Transactional Outbox
- SQL Migration / Transaction / Row Lock / Idempotency

### Delivery & Automation

- GitHub Actions / GHCR
- Immutable Container Images / Digest-based Deployment
- CI Gates / Smoke Test / Rollback
- Makefile / Shell-based Operations
- Backup / Restore / SHA-256 Verification

### Observability & Reliability

- Prometheus / Grafana / Alert Rules
- OpenTelemetry / OTLP / Tempo
- Structured Logging / Request ID / Trace Context
- Health Check / Readiness / Graceful Shutdown
- Fault Drill / Runbook / Postmortem Template
- Bounded Load Test / P50 / P95 / P99 / Capacity Analysis

### Building Next

- Prometheus + Grafana + Alertmanager
- ELK / EFK centralized logging
- Python + Shell operations automation
- Ansible-based host provisioning and deployment
- Ubuntu VM production-like deployment
- SLI / SLO / Error Budget / Incident Management

---

# Featured Reliability Projects

## 1. go-order-management-system-cloudnative-lab

**当前最主要的 SRE / 云原生工程实验项目。**

从 Go 订单系统演进出的多服务运行环境，重点验证微服务边界、消息可靠性、应用韧性、Kubernetes 交付、可观测性、备份恢复、故障演练与自动 CD。

Repository:  
https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab

### Reliability Highlights

- 7 个运行单元、4 个独立服务数据库
- Order Saga / Inventory Reservation / Compensation / Reconciliation
- Transactional Outbox + RabbitMQ TTL/DLX + Publisher Confirm + Manual ACK
- Deadline / Retry / Exponential Backoff / Circuit Breaker / Rate Limit
- Kubernetes + Kustomize + Probe + Resources + PDB + Ingress
- Prometheus + Grafana + OpenTelemetry Collector + Tempo
- GHCR 不可变镜像、Commit SHA、OCI Digest 与发布清单
- Digest-based 自动部署、Smoke Test、失败版本检测与完整回滚
- 四库逻辑备份、SHA-256 校验、隔离恢复验证
- RabbitMQ / HTTP / Worker Lease / Migration 故障演练
- Operator Runbook、事故复盘模板与有界压测

---

## 2. KnowTrace

一个“记录优先、AI 辅助整理”的知识采集与可靠知识工作流系统，目前作为 **AI 应用 + 多服务交付 + 运维/SRE 演进项目** 持续开发。

Repository:  
https://github.com/Yotoha0303/KnowTrace

### Current Engineering Scope

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

Repository:  
https://github.com/Yotoha0303/go-user-system

### Reliability & Security Highlights

- JWT Access / Refresh、Rotation、Token Family 重放检测
- Redis JTI 吊销、账号/IP 双维度登录失败限流
- RBAC、Secure Cookie、可信代理与生产启动校验
- Prometheus HTTP / Runtime / Readiness 指标与基础告警规则
- MySQL 备份、SHA-256 manifest 与隔离恢复演练
- Docker Compose、Kubernetes、CI、CodeQL、Dependabot、GHCR 发布

---

## 4. go-order-management-system

一个面向真实业务约束的 Go 订单库存一致性服务，重点训练 **事务、并发、幂等、消息可靠性与可观测性基础**。

Repository:  
https://github.com/Yotoha0303/go-order-management-system

### Backend Reliability Highlights

- MySQL Transaction + Row Lock + Conditional Update
- Redis Lua 预扣、reservation 补偿与 Redis/MySQL 对账
- RabbitMQ 延迟取消 + Transactional Outbox
- Publisher Confirm / Manual ACK / Idempotent Consumer
- Prometheus `/metrics`
- W3C `traceparent`、Request ID、结构化日志与慢 SQL 日志
- Docker Compose / Migration / CI / Health Check

---

# Current SRE Roadmap

```text
Reliable Backend
      ↓
CI / Immutable Build
      ↓
Automated Delivery / Rollback
      ↓
Ubuntu Runtime Environment
      ↓
Metrics + Logs + Traces
      ↓
Alerting + Runbook
      ↓
Backup / Restore / Fault Drill
      ↓
SLI / SLO / Error Budget
      ↓
SRE / Reliability Engineering
```

当前重点不是继续堆叠工具数量，而是把已有项目真正推进到 **可部署、可观测、可恢复、可回滚、可演练、可复盘** 的状态。

---

# Engineering Principles

- Reliability is a system property, not a monitoring dashboard.
- 先定义故障边界，再决定监控、告警和恢复策略。
- 数据库是事实源时，缓存与消息系统必须有明确的一致性与补偿边界。
- 发布流程必须具备可验证的构建产物、健康检查和回滚路径。
- Backup 只有经过 Restore Verification 后才具有恢复价值。
- Metrics、Logs、Traces 应围绕故障定位建立关联，而不是独立堆叠。
- 自动化应建立在已经理解并手工验证过的操作流程之上。
- 不把实验环境包装成生产环境；明确系统能力、证据与边界。
