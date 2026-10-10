# Yotoha | SRE / 后端工程师 | Backend / SRE Engineer

专注于 **Go 后端、SRE、云原生与 AI 应用系统**，持续把个人项目从“能运行”推进到“可部署、可观测、可恢复、可回滚、可验证”。

Focused on **Go backend systems, SRE, cloud-native engineering, and AI application systems**, with an emphasis on making systems deployable, observable, recoverable, rollback-safe, and verifiable.

---

## 工程目标 | Engineering Goal

```
设计 Design
  ↓
开发 Development
  ↓
测试 Testing
  ↓
构建 Build
  ↓
部署 Deploy
  ↓
观测 Observe
  ↓
排障 Troubleshoot
  ↓
恢复 Recover
  ↓
复盘 Postmortem
  ↓
自动化 Automate
```

---

## 当前技术栈 | Current Stack

### Backend / 后端

- Go / Gin / GORM
- MySQL / PostgreSQL
- Redis / RabbitMQ
- JWT / RBAC
- Transactions / Row Locks / Idempotency / Outbox

### Delivery & Operations / 交付与运维

- Linux / Ubuntu
- Docker / Docker Compose
- Nginx / Caddy
- Git / GitHub Actions / GHCR
- Makefile / Shell / Python
- Backup / Restore / SHA-256 Verification
- Ansible：逐步用于主机初始化与部署自动化 / gradually used for host initialization and deployment automation

### Observability & SRE / 可观测性与 SRE

- Prometheus / Grafana / Alertmanager
- Loki / Alloy
- OpenTelemetry / OTLP / Tempo
- Blackbox Exporter
- Structured Logs / Request ID / Trace Context
- P50 / P95 / P99 / Capacity Analysis
- SLI / SLO / Error Budget
- Runbook / Incident / Postmortem

### Cloud Native / 云原生

- Kubernetes / kind / Kustomize
- Helm / GitOps / ArgoCD — gradually learning / 逐步学习

---

# 核心项目 | Featured Projects

## 1. go-order-management-system-cloudnative-lab

**SRE / Cloud-Native Engineering Lab | SRE / 云原生工程实验项目**

从 Go 订单系统演进出的多服务运行环境，重点验证微服务边界、Saga/补偿、消息可靠性、Kubernetes 交付、可观测性、备份恢复、故障演练与有界压测。

A multi-service environment evolved from a Go order system, focusing on service boundaries, Saga/compensation, messaging reliability, Kubernetes delivery, observability, backup/recovery, fault drills, and bounded load testing.

Repository: [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)

## 2. KnowTrace-Workflow

**AI Application + Multi-Service Delivery + Reliability Engineering**

“记录优先、AI 辅助整理、证据可追溯”的知识工作流系统，同时作为 AI 应用工程、交付工程和可靠性工程实验环境。

An AI-assisted knowledge workflow system built around record-first design, traceable evidence, human verification, self-hosted deployment, observability, and reliability engineering.

Repository: [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)

## 3. ai-collaboration-framework

**AI Collaboration & Engineering Governance Framework | AI 协同与工程治理框架**

面向真实软件项目的人机协同工程框架，将任务认知模型、专业角色契约、阶段门禁、证据约束、负向证伪和单一事实源等原则转化为可检查的规则与部分机械化守卫。当前重点是 Claude Code 运行环境下的规则治理与验证，跨工具协同能力仍在演进中。

An engineering governance framework for human–AI collaboration on real software projects. It formalizes task models, role contracts, stage gates, evidence requirements, falsification checks, and a single source of truth into reviewable rules and partially automated guards. The current implementation focuses on governance within Claude Code; broader cross-tool orchestration remains a work in progress.

Repository: [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)

## 4. go-user-system

**Go Authentication / Authorization System**

可自托管的 Go + Gin + MySQL + Redis 用户认证与 RBAC 系统，覆盖 JWT Access/Refresh、Token Rotation、Replay Detection、JTI Revocation、登录限流、RBAC、CI/CD、Kubernetes 与备份恢复。

A self-hosted Go + Gin + MySQL + Redis authentication and RBAC system covering JWT Access/Refresh, token rotation, replay detection, JTI revocation, rate limiting, CI/CD, Kubernetes, and backup/recovery.

Repository: [go-user-system](https://github.com/Yotoha0303/go-user-system)

## 5. go-order-management-system

**Go Order & Inventory Consistency Service**

面向真实业务约束的 Go 订单库存一致性服务，重点训练事务、行锁、幂等、Redis Lua、RabbitMQ Outbox、状态机、数据隔离与工程化交付。

A Go order and inventory consistency service focused on transactions, row locks, idempotency, Redis Lua, RabbitMQ Outbox, state machines, data isolation, and engineering delivery.

Repository: [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)

---

# 技术成长路线 | Engineering Roadmap

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

The current focus is not collecting technologies indefinitely, but making existing systems:

**Deployable, observable, recoverable, rollback-safe, testable under failure, and reviewable.**

---

# 工程原则 | Engineering Principles

- 可靠性是系统属性，不是 Dashboard 数量。  
  Reliability is a system property, not a dashboard count.
- 先定义故障边界，再决定监控、告警和恢复策略。  
  Define failure boundaries before choosing monitoring, alerting, and recovery strategies.
- 数据库、缓存、消息队列分别承担明确的事实、状态与传输职责。  
  Databases, caches, and message queues have explicit responsibilities for facts, state, and transport.
- 发布必须有可验证的构建产物、健康检查和回滚路径。  
  Releases require verifiable artifacts, health checks, and rollback paths.
- Backup 只有经过 Restore Verification 才具有恢复证据。  
  A backup becomes recovery evidence only after restore verification.
- Metrics、Logs、Traces 围绕故障定位建立关联。  
  Metrics, logs, and traces should be correlated around failure diagnosis.
- 自动化建立在已经理解并手工验证过的操作流程之上。  
  Automation is built on manually understood and verified operational procedures.
- 不把实验环境包装成生产环境；明确能力、证据和边界。  
  Experimental evidence is not presented as production capability; scope and limitations remain explicit.
