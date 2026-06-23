# Yotoha | Go Backend Developer

我目前专注于 **Go 后端开发**，主要训练方向是业务后端系统、MySQL 数据建模、事务一致性、Redis 缓存、接口分层、测试验证与工程化交付。

当前重点不是堆功能，而是把后端项目做到：

* 业务流程清晰
* 表结构设计合理
* 接口行为可验证
* 事务边界明确
* 错误处理统一
* 配置、日志、测试、Docker、CI 能形成闭环

---

## 技术栈

### Backend

* Go
* Gin
* GORM
* RESTful API
* Middleware
* Context
* Error Handling

### Database & Cache

* MySQL
* Redis
* SQL Migration
* Transaction
* Row Lock
* Index Design
* Table Design

### Engineering

* Handler / Service / DAO / Model 分层
* Unified Response
* Business Error Code
* Request Validation
* Docker / Docker Compose
* Makefile
* GitHub Actions
* Unit Test / Integration Test
* Structured Logging
* Graceful Shutdown

---

## Featured Projects

### go-order-inventory

一个基于 **Go + Gin + GORM + MySQL + Redis** 的轻量级订单库存管理系统。

项目重点是训练真实业务后端中的 **订单创建、库存扣减、库存回滚、库存流水、订单状态机、事务一致性和 Redis 缓存**。

Repository: [go-order-inventory](https://github.com/Yotoha0303/go-order-inventory)

#### Core Features

* 商品创建、查询、上架、下架
* 库存初始化、增加库存、查询库存
* 库存流水记录
* 创建订单时扣减库存
* 库存不足时事务回滚
* 订单支付、完成、取消
* 取消待支付订单时回滚库存
* 订单状态机限制非法流转
* 商品详情 Redis cache-aside 缓存
* Redis 不可用时自动降级到 MySQL 主流程

#### Backend Highlights

* 使用 Handler / Service / DAO / Model 分层组织代码
* 使用 MySQL 事务保证订单、订单项、库存和库存流水的一致性
* 使用行级锁控制并发库存扣减
* 使用库存流水追踪每一次库存变化
* 使用订单状态机限制非法业务流转
* 使用 Redis 做商品详情缓存，并在商品状态变化时删除缓存
* 使用 Goose 管理数据库迁移
* 使用 Docker Compose 编排 App、MySQL、Redis
* 使用 Makefile 统一封装运行、测试、构建、迁移和 Docker 命令
* 使用 GitHub Actions 执行测试、race test、vet、lint、migration 校验和构建

---

### go-user-system

一个基于 **Go + Gin + GORM + MySQL** 的用户认证与基础用户管理系统。

项目重点是训练 Go 后端中的 **用户注册、登录、JWT 鉴权、密码安全、统一响应、错误码、SQL Migration、测试和基础工程化**。

Repository: [go-user-system](https://github.com/Yotoha0303/go-user-system)

#### Core Features

* 用户注册
* 用户名唯一性校验
* bcrypt 密码哈希存储
* 用户登录
* JWT access token 签发
* JWT 鉴权中间件
* 当前登录用户信息查询
* 当前用户昵称修改
* 用户状态校验
* `/ping`、`/livez`、`/readyz` 健康检查

#### Backend Highlights

* 使用 Handler / Service / DAO 分层组织业务
* 使用统一响应结构和业务错误码
* 使用 SQL Migration 管理数据库结构版本
* 使用 Request Context 向 Service 和 DAO 层传递
* 使用结构化日志记录请求与异常
* 使用 HTTP Server 超时配置提升服务稳定性
* 使用单元测试、Handler 测试和 MySQL 集成测试验证核心流程
* 使用 Docker 多阶段构建和 Docker Compose 编排本地环境
* 使用 GitHub Actions 自动执行测试、vet、构建和镜像构建
* 支持 SIGTERM 信号处理与 HTTP Server 优雅关闭

---

## What I Can Explain in Interviews

### Backend Design

* 为什么要做 Handler / Service / DAO 分层
* Service 层应该放什么业务逻辑
* DAO 层为什么不处理业务状态
* 如何设计统一响应和业务错误码
* 如何从业务流程反推表结构和接口

### MySQL & Transaction

* 订单创建为什么需要事务
* 库存扣减为什么需要行级锁
* 库存不足时如何保证事务回滚
* 取消订单时如何回滚库存
* 库存流水表的作用
* 为什么使用 SQL Migration 而不是只依赖 AutoMigrate

### Redis Cache

* 商品详情缓存的 cache-aside 流程
* 缓存命中、未命中和写入逻辑
* 商品状态变化时为什么要删除缓存
* Redis 不可用时如何保证主流程可用

### Engineering

* Docker Compose 中 App 为什么不能连接 `127.0.0.1:3306`
* 本地运行和 Docker 运行如何通过环境变量区分配置
* Makefile 如何统一项目命令
* GitHub Actions 如何作为质量门禁
* HTTP Server 超时和优雅关闭的作用

---

## Current Focus

我当前继续围绕 Go 后端就业项目补强以下能力：

* MySQL 表结构设计与索引设计
* 事务一致性与并发库存扣减
* Redis 缓存和缓存失效策略
* Handler / Service / DAO 测试覆盖
* Docker Compose 本地开发环境
* SQL Migration 工作流
* README、接口文档和项目讲解

---

## Engineering Principles

* 先理解业务流程，再设计表结构和接口
* 区分简单 CRUD 与核心业务流程
* 多表写入优先明确事务边界
* 状态流转必须显式建模，避免隐式判断散落在代码中
* 缓存只做加速，不影响主业务正确性
* 代码、测试、配置、文档和启动方式必须保持一致
* 在轻量项目中避免过度设计，同时保留后续演进空间
