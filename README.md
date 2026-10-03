# Yotoha | SRE / 后端工程师

我目前将职业与工程实践重点收敛到 **SRE / 后端可靠性工程**，以 Go 后端为主轴，持续补强 Linux、容器、CI/CD、可观测性、自动化运维、备份恢复、故障演练与稳定性治理。

我的工程目标不是单纯“把功能写出来”，而是逐步具备：

```
设计 → 开发 → 测试 → 构建 → 部署 → 观测 → 排障 → 恢复 → 复盘 → 自动化
```

---

## 当前技术栈

### Backend

- Go / Gin / GORM
- MySQL / PostgreSQL
- Redis / RabbitMQ
- JWT / RBAC
- Transaction / Row Lock / Idempotency / Outbox

### Delivery & Operations

- Linux / Ubuntu
- Docker / Docker Compose
- Nginx / Caddy
- Git / GitHub Actions / GHCR
- Makefile / Shell / Python
- Backup / Restore / SHA-256 Verification
- Ansible：逐步用于主机初始化与部署自动化

### Observability & SRE

- Prometheus / Grafana / Alertmanager
- Loki / Alloy
- OpenTelemetry / OTLP / Tempo
- Blackbox Exporter
- Structured Logs / Request ID / Trace Context
- P50 / P95 / P99 / Capacity Analysis
- SLI / SLO / Error Budget
- Runbook / Incident / Postmortem

### Cloud Native

- Kubernetes / kind / Kustomize
- Helm / GitOps / ArgoCD 逐步学习

---

# 核心项目

## 1. go-order-management-system-cloudnative-lab

当前主要的 **SRE / 云原生工程实验项目**。

从 Go 订单系统演进出的多服务运行环境，重点验证：

- 微服务边界
- Order Saga / Compensation / Reconciliation
- Transactional Outbox + RabbitMQ
- Deadline / Retry / Backoff / Circuit Breaker / Rate Limit
- Kubernetes / Kustomize / Probe / Resources / PDB / Ingress
- Prometheus / Grafana / OpenTelemetry / Tempo
- GHCR 不可变镜像、Commit SHA、OCI Digest
- 自动部署、Smoke Test、失败检测与回滚
- 数据库备份、SHA-256 校验与隔离恢复
- 故障演练、Runbook、Postmortem 与有界压测

仓库：<https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab>

---

## 2. KnowTrace

**AI 应用 + 多服务交付 + 可靠性工程演进项目。**

KnowTrace 是“记录优先、AI 辅助整理”的知识采集与可靠知识工作流系统。

当前重点已经从单纯功能开发扩展到：

- Ubuntu / VPS 部署
- Docker Compose 多服务运行
- CI / 构建与发布验证
- Prometheus / Grafana / Alertmanager
- Loki / Alloy 日志
- OpenTelemetry / Trace
- Blackbox 故障演练
- 备份 / 恢复 / SHA-256 验证
- 压测与容量基线
- Runbook / Incident / Postmortem
- Python / Shell 自动化
- Ansible 主机初始化与部署自动化

当前项目明确区分：

> **建成的能力、已经验证的能力、以及尚未实现的能力。**

仓库：<https://github.com/Yotoha0303/KnowTrace>

---

## 3. go-user-system

可自托管的 **Go + Gin + MySQL + Redis** 用户认证与 RBAC 系统。

重点：

- JWT Access / Refresh
- Token Rotation / Replay Detection
- Redis JTI Revocation
- 登录失败限流
- RBAC
- Secure Cookie
- Docker Compose / Kubernetes
- CI / CodeQL / Dependabot / GHCR
- Prometheus 基础指标
- Backup / Restore

仓库：<https://github.com/Yotoha0303/go-user-system>

---

## 4. go-order-management-system

面向真实业务约束的 Go 订单库存一致性服务。

重点训练：

- MySQL Transaction / Row Lock
- Redis Lua / Reservation / Compensation
- RabbitMQ / Outbox
- Publisher Confirm / Manual ACK
- Idempotent Consumer
- Prometheus Metrics
- Request ID / Trace Context
- Docker Compose / Migration / CI / Health Check

仓库：<https://github.com/Yotoha0303/go-order-management-system>

---

# 技术成长路线

```
Go Backend
    ↓
Linux / Network / Database
    ↓
Docker / CI/CD
    ↓
Deployment / Automation
    ↓
Metrics / Logs / Traces
    ↓
Backup / Restore / Incident
    ↓
SLI / SLO / Error Budget
    ↓
Kubernetes / Cloud Native
    ↓
SRE / Platform Engineering
    ↓
AI Systems
```

当前阶段不追求无限扩张技术栈，而是持续把已有系统做到：

**可部署、可观测、可恢复、可回滚、可演练、可复盘。**

---

# 工程原则

- 可靠性是系统属性，不是 Dashboard 数量。
- 先定义故障边界，再决定监控、告警和恢复策略。
- 数据库、缓存、消息队列分别承担明确的事实、状态与传输职责。
- 发布必须有可验证的构建产物、健康检查和回滚路径。
- Backup 只有经过 Restore Verification 才具有恢复证据。
- Metrics、Logs、Traces 围绕故障定位建立关联。
- 自动化建立在已经理解并手工验证过的操作流程之上。
- 不把实验环境包装成生产环境；明确能力、证据和边界。
