# Yotoha | 全栈工程与 AI 应用交付

**Full-Stack Engineering · High-Concurrency Systems · AI Application Engineering**

专注于全栈业务闭环（Full-Stack End-to-End Delivery）、Go 高并发与数据一致性设计，以及 AI 应用工程化。围绕真实业务流程构建从响应式前端、API 与身份认证，到数据层、异步消息、容器化部署、自动化测试和可观测性的完整交付链路。

I focus on end-to-end product delivery, high-concurrency Go services, data consistency, and production-oriented AI application engineering. My work spans responsive frontends, APIs and authentication, data and messaging layers, containerized delivery, automated testing, and observability.

- **目标方向 | Focus:** Full-Stack Engineer — Go backend, Vue 3 / Next.js / TypeScript frontend, Docker-based delivery
- **工程重点 | Engineering:** Business workflow integrity, concurrency control, traceable AI workflows, security, and operational visibility
- **GitHub:** [@Yotoha0303](https://github.com/Yotoha0303)

---

## 技术栈矩阵 | Tech Stack

### 1. Frontend & UI
- **Frameworks:** Next.js (App Router), React, Vue 3
- **Language & Styling:** TypeScript, Tailwind CSS
- **UI & Visualization:** Element Plus, ECharts
- **Realtime UX:** SSE / WebSocket, streaming response rendering, responsive UI, authentication-state synchronization

### 2. Backend & Architecture
- **Languages & Frameworks:** Go (Gin, GORM), Java (Spring Boot)
- **Interfaces:** RESTful API, gRPC
- **Identity & Access:** JWT Access/Refresh Token, RBAC, session lifecycle and permission enforcement
- **Architecture:** Asynchronous event-driven workflows, idempotent APIs, unified error handling, service boundaries

### 3. Data & Middleware
- **Databases & ORM:** PostgreSQL, MySQL, Drizzle ORM
- **Caching & Concurrency:** Redis, Lua atomic operations, distributed locks, hot-key caching
- **Messaging & Consistency:** RabbitMQ, Transactional Outbox, retry and idempotent consumer patterns

### 4. DevOps & Quality
- **Container & Edge:** Docker, multi-stage builds, Docker Compose, Caddy / Nginx
- **Delivery:** GitHub Actions, Jenkins, CI/CD quality gates
- **Testing:** Go test, Vitest, Playwright E2E, integration testing, negative-path and mutation testing
- **Observability:** Prometheus, Grafana, Loki, OpenTelemetry / Tempo, structured logs and request correlation

---

## 生产协同实践 | Engineering Collaboration

### [NeutrinoGroup / koliance](https://github.com/NeutrinoGroup/koliance)
*Full-Stack Realtime Trading Dashboard & Event-Driven Settlement Infrastructure | 全栈实时交易看板与事件驱动结算基础设施*

- **前端可靠性 | Frontend resilience:** RPC 多端点故障切换、连接状态处理、登录态同步与响应式 UI 修复。
- **后端一致性 | Backend consistency:** 参与 Go 链上事件监听与水位重连、状态同步，并推动 ADR-006 统一错误处理体系。
- **质量门禁 | Quality gates:** CI 覆盖率门禁、Gitleaks 凭证扫描与关键路径验证，减少回归和配置风险。

- **Frontend resilience:** Multi-endpoint RPC failover, connection-state handling, authentication-state synchronization, and responsive UI fixes.
- **Backend consistency:** Go event listening and watermark-based reconnection, state synchronization, and the ADR-006 unified error-handling model.
- **Quality gates:** CI coverage thresholds, Gitleaks secret scanning, and critical-path verification to reduce regressions and configuration risks.

---

## 核心项目 | Featured Projects

### 1. [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)
*Multi-Tenant AI Knowledge Workflow | 多租户 AI 知识工作流*

基于 **Next.js App Router、TypeScript、Drizzle ORM、PostgreSQL 与 Go 认证服务**构建全栈工作流。将原始采集、AI 辅助提炼、证据链追溯与人工复核串联为端到端业务闭环；以 Workspace 服务端隔离、版本化记录和证据快照支持可追溯协作。交付侧集成 Docker Compose、GitHub Actions、自动化验证、备份恢复流程与 Prometheus / Grafana / Loki 可观测性组件。

Built with **Next.js App Router, TypeScript, Drizzle ORM, PostgreSQL, and a Go authentication service**. Connects raw capture, AI-assisted synthesis, evidence traceability, and human review in one end-to-end workflow. Workspace-level isolation, versioned records, and evidence snapshots support auditable collaboration. Delivery includes Docker Compose, GitHub Actions, automated checks, backup/recovery workflows, and Prometheus / Grafana / Loki observability components.

### 2. [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)
*High-Concurrency Order & Inventory Consistency | 高并发订单与库存一致性*

以 Go 构建订单、库存、状态流转和管理台业务闭环。通过 MySQL 事务与行锁、条件扣减、Redis Lua 原子预扣、请求幂等、Transactional Outbox、RabbitMQ TTL/DLX 与幂等消费者处理并发下单、超时取消和最终一致性。配套 React + TypeScript 管理台、Docker Compose、CI 与 Prometheus 指标。

A Go-based order and inventory workflow covering order creation, stock control, state transitions, and an admin UI. Uses MySQL transactions and row locks, conditional stock updates, atomic Redis Lua reservations, request idempotency, Transactional Outbox, RabbitMQ TTL/DLX, and idempotent consumers to handle concurrent orders, timeout cancellation, and eventual consistency. Includes a React + TypeScript admin UI, Docker Compose, CI, and Prometheus metrics.

### 3. [go-user-system](https://github.com/Yotoha0303/go-user-system)
*Full-Stack Authentication & RBAC | 全栈身份认证与权限控制*

Go / Gin / GORM、MySQL 与 Redis 驱动的认证服务，配套 React + TypeScript 响应式管理工作台。实现 JWT Access/Refresh 双 Token、Refresh Token Rotation、会话吊销与重放检测、RBAC、账号/IP 限流、接口级权限校验、自动化测试及容器化部署。

An authentication service built with Go / Gin / GORM, MySQL, and Redis, paired with a responsive React + TypeScript admin workspace. Implements JWT Access/Refresh tokens, refresh rotation, session revocation and replay detection, RBAC, account/IP rate limiting, endpoint-level authorization, automated tests, and containerized deployment.

### 4. [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)
*Cloud-Native Delivery, Resilience & Observability | 云原生交付、韧性与可观测性*

围绕服务边界与可靠交付构建云原生订单系统，涵盖 API Gateway、多服务数据隔离、Saga 补偿、Transactional Outbox、RabbitMQ、Worker 租约与恢复、Docker Compose / Kubernetes、不可变镜像、自动化测试环境 CD、备份恢复验证、故障演练和有界压测。集成 Prometheus、Grafana、OpenTelemetry 与 Tempo，形成可复现的部署和运行验证链路。

A cloud-native order system focused on service boundaries and reliable delivery. Covers an API Gateway, service-owned data, Saga compensation, Transactional Outbox, RabbitMQ, worker leases and recovery, Docker Compose / Kubernetes, immutable images, test-environment CD, backup/restore verification, fault drills, and bounded load tests. Prometheus, Grafana, OpenTelemetry, and Tempo support repeatable deployment and runtime validation.

### 5. [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)
*AI-DevEx Quality Gates & Developer Productivity Tooling | AI-DevEx 质量门禁与研发效能工具链*

将 AI 辅助开发规范转化为可执行的工程检查，通过 Pre-commit 与 CI 钩子验证交付条件，包括负向变异测试验证（Mutation Testing）、Git Commit SHA 与文档基线一致性校验、最小权限隔离（Least Privilege）和阶段门禁。目标是尽早发现无效断言、文档漂移与未经验证的变更，并让质量结论关联到可复核的测试证据。

Turns AI-assisted development conventions into executable checks through Pre-commit and CI hooks. The workflow includes negative mutation testing, Git Commit SHA and documentation-baseline consistency checks, least-privilege isolation, and stage gates. It is designed to catch ineffective assertions, documentation drift, and unverified changes early, with quality decisions tied to reviewable test evidence.

---

## 工程交付原则 | Engineering Principles

- **业务闭环优先 | End-to-end outcomes:** 从用户交互到数据持久化、异步处理和运行监控，按完整业务路径设计与验证。
- **一致性显式化 | Explicit consistency:** 对并发写入、重复请求、消息重试和跨服务状态变化定义明确的幂等与补偿策略。
- **安全默认开启 | Secure by default:** 使用最小权限、明确的认证授权边界、密钥管理和限流策略。
- **质量门禁可验证 | Verifiable quality gates:** CI/CD 质量门禁必须有明确判据、负向测试和可复核的运行结果。
- **可观测性服务于排障 | Observability for diagnosis:** 通过结构化日志、指标、追踪和 Request ID 关联业务状态与运行事件。
- **架构决策可追溯 | Traceable decisions:** 以 ADR 记录重要技术决策，并保持实现、测试与文档基线同步。

