# Yotoha | Go Backend Developer

我目前专注于 **Go 后端开发**，主要训练方向是业务后端系统、MySQL 数据建模、事务一致性、Redis 缓存、接口设计、测试验证与工程化交付。

当前重点不是简单堆 CRUD，而是将项目做到：

- 业务流程清晰
- 数据模型合理
- 事务边界明确
- 接口行为可验证
- 错误处理统一
- 配置、日志、测试、Docker、CI 形成完整闭环

---

## 技术栈

### Backend

- Go
- Gin
- GORM
- RESTful API
- Middleware
- Context
- Error Handling

### Database & Cache

- MySQL
- Redis
- SQL Migration
- Transaction
- Row Lock
- Index Design
- Table Design

### Message Queue

- RabbitMQ
- Outbox Pattern
- Delay Queue
- Dead Letter Queue

### Engineering

- Handler / Service / DAO / Model 分层
- Unified Response
- Business Error Code
- Request Validation
- Docker / Docker Compose
- Makefile
- GitHub Actions
- Unit Test / Integration Test
- Structured Logging
- Graceful Shutdown

---

# Featured Projects

## go-order-management-system

一个基于 **Go + Gin + GORM + MySQL + Redis + RabbitMQ** 的订单库存管理系统。

项目重点训练真实业务后端中的：

- 订单流程设计
- 库存一致性
- 事务控制
- 幂等处理
- 状态机建模
- 异步任务处理
- 缓存设计
- 工程化部署

Repository:
https://github.com/Yotoha0303/go-order-management-system

### Core Features

- 用户注册、登录、JWT 鉴权
- 用户角色权限控制（RBAC）
- 商品创建、查询、上下架
- 库存初始化、增加库存、库存流水
- 创建订单时库存扣减
- 库存不足事务回滚
- 订单支付、完成、取消状态流转
- 订单取消库存回补
- 幂等 Key 防止重复创建订单
- Redis 商品详情缓存
- Redis 不可用时降级 MySQL 主流程
- RabbitMQ 订单超时自动取消
- Outbox 保证数据库事件一致性

### Backend Highlights

- 使用 Handler / Service / DAO / Model 分层组织业务
- 使用 MySQL 事务保证订单、订单项、库存和流水一致性
- 使用行级锁和条件更新处理并发库存扣减
- 使用订单状态机限制非法状态流转
- 使用幂等机制处理重复请求和并发请求
- 使用 RBAC 控制管理员操作权限
- 使用 Redis 实现 cache-aside 缓存策略
- 使用 RabbitMQ 实现订单超时异步处理
- 使用 Goose 管理数据库迁移
- 使用 Docker Compose 编排应用环境
- 使用 Makefile 管理运行、测试、迁移和构建流程
- 使用 GitHub Actions 执行测试、lint、race test、migration 校验和构建

---

## go-user-system

一个基于 **Go + Gin + GORM + MySQL** 的用户认证与基础用户管理系统。

主要训练：

- 用户注册与登录
- JWT 鉴权
- 密码安全存储
- 统一响应
- 错误码设计
- SQL Migration
- 基础工程化

Repository:
https://github.com/Yotoha0303/go-user-system

---

# Interview Topics

## Backend Design

- 为什么采用 Handler / Service / DAO 分层
- 如何从业务流程设计表结构和接口
- Service 层应该承担哪些职责
- 如何设计统一错误处理

## MySQL & Transaction

- 为什么订单创建需要事务
- 库存扣减如何避免超卖
- 行锁和条件更新的使用场景
- 为什么使用 SQL Migration

## Redis

- Cache Aside 模式
- 缓存失效策略
- Redis 不可用时如何保证主流程

## Message Queue

- 为什么订单超时需要异步处理
- RabbitMQ 延迟队列设计
- Outbox Pattern 解决什么问题

## Engineering

- Docker Compose 中服务如何通信
- 环境变量如何管理不同环境配置
- GitHub Actions 如何作为质量门禁
- HTTP Server 超时与优雅关闭

---

# Current Focus

继续补强：

- MySQL 索引与查询优化
- 分布式事务与消息一致性
- Redis 高级使用
- 云服务器部署
- Docker / Compose / Kubernetes 基础
- AI 应用后端能力

---

# Engineering Principles

- 先理解业务，再设计表结构和接口
- 区分简单 CRUD 与核心业务流程
- 多表写入明确事务边界
- 状态流转显式建模
- 缓存只提升性能，不影响业务正确性
- 代码、测试、配置、文档保持一致
- 避免过度设计，同时保留系统演进空间
