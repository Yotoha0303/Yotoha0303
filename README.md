# Hi，我是 Yotoha

我目前专注于 **Go 后端开发**，围绕业务后端项目持续训练接口设计、数据库建模、事务一致性、Redis 缓存、服务层测试和项目交付能力。

当前目标是进入正式后端开发岗位，在真实业务场景中稳定交付可维护、可扩展的后端模块。

---

## 技术栈

### 后端开发
- Go
- Gin
- GORM
- RESTful API
- Java / Spring（基础）

### 数据库与缓存
- MySQL
- Redis
- 事务处理
- 基础索引设计
- 表结构设计

### 工程实践
- handler / service / dao / model 分层
- 统一响应结构
- 业务错误码设计
- 配置管理
- service 层测试
- REST Client 接口自测
- Docker Compose（基础）
- Git / GitHub

---

## 重点项目

### 1. go-order-inventory

基于 **Go + Gin + GORM + MySQL + Redis** 的轻量级订单库存管理系统。

覆盖商品管理、库存初始化、库存流水、订单创建、订单支付、订单完成、订单取消回滚等核心业务流程，重点训练业务建模、表结构设计、事务一致性、库存扣减、缓存与测试能力。

核心功能：
- 商品创建、查询、上架、下架
- 库存初始化与手动加库存
- 库存流水记录
- 订单创建、查询、支付、完成、取消
- 取消订单后的库存回滚
- Redis 商品详情缓存
- 统一业务错误码与响应结构
- service 层测试与 REST Client 自测

项目地址：[go-order-inventory](https://github.com/Yotoha0303/go-order-inventory)

### 2. go-user-system

一个基于 **Go、Gin、GORM 和 MySQL** 实现的用户认证与基础用户管理系统。

项目围绕用户注册、登录、JWT 鉴权、当前用户查询和资料修改等核心业务，采用 Handler、Service、DAO 分层结构，并补充 SQL Migration、自动化测试、Docker Compose、GitHub Actions、健康检查和优雅关闭等工程化能力。

项目目标是完整实践一个 Go 单体后端从业务开发、数据库管理、测试验证到容器化交付的基本流程。

#### 核心功能

* 用户注册与用户名唯一性校验
* bcrypt 密码哈希存储与密码校验
* 用户登录与 JWT `access_token` 签发
* JWT 鉴权中间件与用户身份上下文传递
* 当前登录用户信息查询
* 当前用户昵称修改
* 用户状态校验
* 统一响应结构、业务错误码与应用错误处理
* `/ping`、`/livez`、`/readyz` 健康检查

#### 工程化能力

* Handler、Service、DAO 分层设计
* 显式注入 `*gorm.DB`，避免依赖全局数据库对象
* HTTP Request Context 向 Service 和 DAO 层传递
* 使用 SQL Migration 管理数据库结构版本
* 单元测试、HTTP Handler 测试与 MySQL 集成测试
* Docker 多阶段构建与 Docker Compose 编排
* 非 root 用户运行容器
* GitHub Actions 自动执行测试、`go vet`、二进制构建和镜像构建
* SIGTERM 信号处理与 HTTP Server 优雅关闭
* Makefile 统一管理运行、测试、构建和容器命令


项目地址：[go-user-system](https://github.com/Yotoha0303/go-user-system)

---

## 当前学习重点

- Go 后端项目结构
- MySQL 表结构设计与事务处理
- Redis 缓存与缓存失效策略
- service 层业务测试
- 业务错误码与统一响应设计
- API 文档与 README 编写
- Docker Compose 本地开发环境
- 项目讲解与面试表达

---

## 工程理念

- 先理解业务，再设计表结构和接口
- 区分简单 CRUD 与核心业务流程
- 多表写入和状态流转优先保证事务一致性
- 测试覆盖成功流程与失败流程
- 保持接口、代码、测试、文档一致
- 在轻量项目中避免过度设计，同时保留演进空间

---

English version: [README.en.md](./README.en.md)
