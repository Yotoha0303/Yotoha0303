# FeeGate 数据资产与爬虫私有服务器迁移与部署指南 (Private Server Migration SOP)

> **适用场景**：将 FeeGate 数据资产与自动化采集爬虫整体迁移至私人物理机、Homelab NAS 或云端私有 VPS (Ubuntu / Debian / Arch Linux)。

---

## 🚀 方式一：Docker 一键容器化部署 (推荐 · 跨平台零污染)

### 1. 迁移准备
在私人服务器上克隆仓库：
```bash
git clone https://github.com/Yotoha0303/Yotoha0303.git /opt/feegate-service
cd /opt/feegate-service/deploy
```

### 2. 启动服务与后台守护
```bash
docker compose up -d --build
```
- **数据提供网关 (`feegate-web`)**：暴露端口 `8402`，带全套 CORS 标头与防陈旧缓存配置；
- **定时爬虫守护 (`feegate-crawler`)**：容器内置 Cron 每天 06:00 UTC 自动抓取更新数据。

### 3. 本地验证
```bash
curl -I http://localhost:8402/data/feegate-catalog-index.json
```
返回 `HTTP/1.1 200 OK`，且包含 `Access-Control-Expose-Headers` 与 `application/json` 即可对外服务！

---

## 🛠️ 方式二：原生 Linux Systemd 定时服务部署 (免 Docker)

Systemd 每 6 小时采集 sovereign yields，并校验全部 4 个产品。
完整的命令、日志与指标位置、失败处理说明见 [HARVESTER.md](HARVESTER.md)。

### 1. 安装 Service 与 Timer
```bash
# 复制脚本到 systemd 用户目录
mkdir -p ~/.config/systemd/user/
cp deploy/feegate-harvester.service ~/.config/systemd/user/
cp deploy/feegate-harvester.timer ~/.config/systemd/user/

# 重载并启用定时器
systemctl --user daemon-reload
systemctl --user enable --now feegate-harvester.timer
```

### 2. 查看定时器运行状态
```bash
systemctl --user list-timers | grep feegate
```

---

## 🔒 数据永久不可变保障规范 (Data Immutability Guarantee)

1. **历史固定快照 (Frozen Snapshot)**：
   FeeGate 端点填写固定的 Git Release 标签或 Commit 哈希：
   `https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/v2.0.0-feegate-release/data/...`
   此地址由 Git 强哈希固化，内容物理不可被修改；
2. **滚动数据流 (Live Daily Stream)**：
   FeeGate 端点填写 `main` 分支地址：
   `https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/...`
   此地址每天 14:00 自动滚动更新最新模型价格与安全漏洞；
3. **私有化服务器地址 (Self-Hosted Private IP/Domain)**：
   若使用自有服务器，地址可配置为：
   `http://your-server-ip:8402/data/feegate-catalog-index.json`
