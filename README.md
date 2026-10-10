# Yotoha | Go Backend Engineer

以 **Go 后端开发** 为主线，重点关注业务逻辑、数据一致性、测试与可维护性；通过 Linux、Docker 和 CI/CD 完成部署交付，并练习日志排查、备份恢复与故障验证。

Focused on **Go backend engineering**: business logic, data consistency, testing, and maintainability. I use Linux, Docker, and CI/CD to deliver services, while building practical skills in troubleshooting, backup/recovery, and failure verification.

---

## 当前方向 | Current Focus

- **主线 / Primary:** Go, Gin, SQL, Redis, API design, transactions, concurrency, automated testing.
- **配套 / Supporting:** Linux, networking basics, Docker Compose, CI/CD, logs, deployment, backup and restore.
- **暂缓扩展 / Deprioritized for now:** adding more frameworks, expanding Kubernetes/GitOps, and building increasingly complex multi-agent orchestration.

目标不是堆叠技术标签，而是把一个业务服务做到 **能实现、能测试、能部署、能排错、能解释设计取舍**。

The goal is not to collect technology labels, but to build services that I can implement, test, deploy, troubleshoot, and explain.

---

## 核心项目 | Featured Projects

### 1. [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)

**Go Backend | Order & Inventory Consistency**

订单与库存业务系统，重点实践 Go、Gin、MySQL、Redis、RabbitMQ，以及事务、行锁、幂等、状态流转和消息可靠性。

A Go order and inventory service focused on transactions, row locks, idempotency, state transitions, and reliable messaging.

### 2. [go-user-system](https://github.com/Yotoha0303/go-user-system)

**Go Backend | Authentication & Authorization**

用户认证与授权服务，实践 JWT、Access/Refresh Token、权限控制、Redis、限流、测试和交付流程。

A Go authentication and authorization service covering JWT access/refresh tokens, access control, Redis, rate limiting, testing, and delivery.

### 3. [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)

**Deployment & Reliability Lab | 部署与可靠性实验**

基于订单系统扩展的工程实验环境，用于验证容器化交付、CI/CD、可观测性、备份恢复和故障演练。它是实验项目，不代表生产级平台经验。

An engineering lab for validating container delivery, CI/CD, observability, backup/recovery, and fault drills. It is an experiment, not a claim of production-scale platform experience.

---

## 其他项目 | Additional Projects

- [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow) — AI 辅助知识工作流，强调证据追溯、人工核验和可维护交付。 / An AI-assisted knowledge workflow focused on evidence traceability, human verification, and maintainable delivery.
- [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework) — 人机协同工程治理框架，探索阶段门禁、证据约束和部分自动化验证；当前重点是 Claude Code 环境。 / An engineering governance framework exploring stage gates, evidence requirements, and partial automation, currently focused on Claude Code.

这些项目是补充探索，不改变当前以 Go 后端为主的方向。

These are supporting explorations, not a change to the primary Go backend focus.

---

## 学习路线 | Roadmap

```
Go / Gin / SQL
      ↓
Transactions / Concurrency / Testing
      ↓
Linux / Networking / Docker
      ↓
CI/CD / Logs / Deployment
      ↓
Backup & Restore / Troubleshooting
      ↓
Improve existing projects through measured evidence
```

当前优先事项：

1. 完成并验证核心业务流程、错误处理和自动化测试。
2. 为核心项目提供可复现的启动、部署、排错和恢复说明。
3. 记录实际问题、根因、修复和验证证据。
4. 以项目完成度和求职反馈决定下一项学习内容，而不是持续扩展技术栈。

Current priorities: verify core business flows and tests, make setup/deployment/troubleshooting/recovery reproducible, document incidents and evidence, and choose further learning based on project gaps and job-search feedback.

---

## 工程原则 | Engineering Principles

- 优先验证正确性，再增加架构复杂度。 / Verify correctness before adding architectural complexity.
- 自动化必须建立在理解并验证过的操作之上。 / Automate only workflows that are understood and verified.
- 用测试、日志和可复现步骤支撑结论。 / Support conclusions with tests, logs, and reproducible steps.
- 明确区分实验结果与生产经验。 / Distinguish lab results from production experience.
