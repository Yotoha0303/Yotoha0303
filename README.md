# Yotoha | Go Backend Developer

我目前专注于 **Go 后端开发**，重点训练业务系统、认证与权限、MySQL 事务一致性、Redis、消息队列、测试验证以及容器化交付。

当前项目不以堆叠 CRUD 为目标，而是持续验证：

- 业务流程是否闭环
- 数据模型与事务边界是否合理
- 并发、幂等、缓存与异步任务是否可验证
- 认证、权限和安全策略是否完整
- 测试、迁移、日志、容器和 CI/CD 是否形成交付闭环

---

## Technical Stack

### Backend

- Go / Gin / GORM
- RESTful API / Middleware / Context
- JWT Authentication / RBAC
- Error Handling / Request Validation

### Data & Messaging

- MySQL / Redis
- SQL Migration / Transaction / Row Lock / Index Design
- RabbitMQ / Outbox Pattern / TTL / DLX

### Engineering

- Handler / Service / DAO / Model 分层
- Docker / Docker Compose / Kubernetes
- Makefile / GitHub Actions / GHCR
- Unit Test / Integration Test / E2E Test
- Race Test / CodeQL / Dependabot / Vulnerability Check
- Structured Logging / Request ID / Graceful Shutdown
- Prometheus Metrics / 基础 Trace Context

---

# Featured Projects

## go-order-management-system

一个基于 **Go + Gin + GORM + MySQL + Redis + RabbitMQ** 的订单库存一致性管理服务，重点展示真实业务后端中的事务一致性、并发安全、异步任务和工程化交付能力。

Repository:  
https://github.com/Yotoha0303/go-order-management-system

### Core Business

- 用户注册、登录、JWT 鉴权与 RBAC
- 商品创建、查询、上下架
- 库存初始化、入库、库存流水
- 用户级幂等订单创建
- MySQL 事务 + 行锁 + 条件更新防止超卖
- Redis Lua 库存预扣与 reservation 补偿
- 订单支付、完成、取消状态机
- 取消订单库存回补
- RabbitMQ TTL/DLX 实现订单超时自动取消
- Transactional Outbox 保证数据库事件与消息发布一致性

### Engineering Highlights

- Redis/MySQL 库存对账与 Redis 可售库存重建
- 多商品按固定顺序加锁，降低死锁风险
- Redis Cluster 固定 hash tag 规避多 key 跨 slot
- 管理员操作审计日志与 Request ID 关联
- Prometheus `/metrics` 暴露 HTTP、订单和库存预扣指标
- W3C `traceparent` 兼容，日志输出 `trace_id/span_id`
- GORM 慢 SQL 日志与压测/性能分析入口
- Goose Migration、Docker Compose、Makefile、GitHub Actions
- Docker 多阶段构建、非 root 运行，并支持云主机一键部署

---

## go-user-system

一个可自托管的 **Go + Gin + GORM + MySQL + Redis + React** 用户认证与 RBAC 系统，当前公开交付版本为 `v1.0.0-rc.3`。

Repository:  
https://github.com/Yotoha0303/go-user-system

### Core Business

- 用户注册、登录、资料查询、昵称修改、密码修改和登出
- JWT Access / Refresh 双 Token
- Refresh Token HttpOnly Cookie、哈希存储与 Rotation
- Token Family 重放检测
- 用户 `auth_version`，改密后全会话失效
- 当前 Access JTI 吊销
- Redis JTI 吊销与账号/IP 登录失败限流
- RBAC 角色、权限、用户角色和角色权限模型
- 一次性管理员初始化，普通注册用户仅绑定 `user` 角色
- 可关闭公开注册入口

### Engineering & Delivery

- React + TypeScript 管理界面与会话恢复
- Swagger、健康检查、结构化日志、Request ID、超时和优雅关闭
- Goose Migration、完整 Docker Compose 栈
- Kubernetes 清单、迁移 Job、Service 与 Ingress
- Go 单元/集成测试、Vitest、Testing Library、Playwright E2E
- GitHub Actions、CodeQL、Dependabot、secret scanning
- GHCR 多架构镜像与版本发布
- 生产环境 Secure Cookie、可信代理 CIDR 和启动安全校验

---

# Current Focus

当前继续补强：

- MySQL 索引、查询优化与慢 SQL 分析
- Redis 一致性与高并发场景
- 分布式事务与消息一致性
- Kubernetes / 云原生部署与可观测性
- Go 网络与系统编程
- AI 应用后端：LLM API、Tool Calling、RAG、Agent Worker

---

# Engineering Principles

- 先理解业务，再设计表结构、接口和状态流转
- 区分核心业务能力与工程化能力
- 多表写入明确事务边界
- 并发场景显式处理幂等、锁和一致性
- 缓存是性能与流量保护层，数据库仍是事实源
- 认证不仅是签发 JWT，还要考虑刷新、吊销、重放和会话失效
- 代码、测试、迁移、配置、文档与部署保持一致
- 避免为了技术数量过度设计，以可验证和可维护为优先
