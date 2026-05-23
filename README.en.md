# Hi, I'm Yotoha

I am currently focused on **Go backend development**, building hands-on projects around API design, database modeling, transaction consistency, Redis caching, service-layer testing, and project delivery.

My current goal is to join a backend engineering role and steadily deliver maintainable, scalable backend modules in real business scenarios.

---

## Tech Stack

### Backend
- Go
- Gin
- GORM
- RESTful API
- Java / Spring (basic)

### Database & Cache
- MySQL
- Redis
- Transaction handling
- Basic index design
- Table schema design

### Engineering Practices
- Layered architecture: handler / service / dao / model
- Unified response structure
- Business error code design
- Configuration management
- Service-layer testing
- REST Client API self-testing
- Docker Compose (basic)
- Git / GitHub

---

## Featured Projects

### 1. go-order-inventory

A lightweight order and inventory management system built with **Go + Gin + GORM + MySQL + Redis**.

It covers core business flows including product management, inventory initialization, stock logs, order creation, payment, completion, cancellation, and inventory rollback.

Key features:
- Product create/query/on-sale/off-sale operations
- Inventory initialization and manual stock increase
- Stock log records
- Order create/query/pay/complete/cancel flows
- Inventory rollback after order cancellation
- Redis product detail caching
- Unified business error code and response structure
- Service-layer tests and REST Client API self-testing

Repository: [go-order-inventory](https://github.com/Yotoha0303/go-order-inventory)

### 2. go-user-system

A user authentication system built with **Go + Gin + GORM + MySQL**, focused on backend fundamentals and engineering layering.

Key features:
- User registration
- User login
- bcrypt password hashing
- JWT authentication
- User profile query
- Layered architecture: handler / service / dao / model
- Unified response structure

Repository: [go-user-system](https://github.com/Yotoha0303/go-user-system)

---

## Current Learning Focus

- Go backend project structure
- MySQL schema design and transaction handling
- Redis caching and cache invalidation strategies
- Service-layer business testing
- Business error codes and unified response design
- API docs and README writing
- Docker Compose local development setup
- Project walkthrough and interview communication

---

## Engineering Principles

- Understand business rules before design
- Design schema and APIs around actual workflows
- Prioritize transaction consistency for multi-table writes and state transitions
- Test both success and failure paths
- Keep API, code, tests, and docs aligned
- Avoid overengineering in lightweight projects while keeping room for evolution
