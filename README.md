# 📱 微博 & 抖音更新自动转发 Telegram (weibo-tg-forwarder)

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Platform-Weibo%20%7C%20Douyin-E6162D?logo=sina-weibo" alt="Platforms" />
  <img src="https://img.shields.io/badge/Telegram-Bot%20API-2CA5E0?logo=telegram" alt="Telegram API" />
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License" />
</p>

高效、轻量且高度稳定的社交媒体转发机器人。自动轮询监控指定的**微博博主**与**抖音作者**，抓取最新发布的高清图片、**Live Photo 实况动图**及**高清视频**，自动完成媒体格式适配与尺寸规范化，秒级推送到指定的 Telegram 频道。

---

## ✨ 核心特性

- 📸 **实况动图（Live Photo）原生九宫格融合**
  - 针对 Telegram 客户端深度优化：将 Live Photo 的动态视频与静态高清照片打包为同一个 3×3 九宫格媒体组（`sendMediaGroup`）。
  - 点开任意一张即可在画廊内流畅播放动效，告别相册底下刷屏散落多条短视频的糟糕体验。
- 🎬 **高清长视频原生大播放器自适应**
  - 发送前自动提取视频真实分辨率（宽、高）、旋转角度与播放秒数，并生成合规轻量缩略图。
  - **电脑端与手机端（iOS / Android）完美自适应**：原生大窗口流媒体播放，彻底解决手机端出现黑边、压缩缩水或畸形方框的问题。
- 🔄 **智能双轨抓取（免登录匿名优先 + Cookie 备用）**
  - 默认采用免登录匿名接口快速轮询，保护个人主账号，降低风控概率；
  - 遇到风控、限流或博主设置仅粉丝可见时，自动毫秒级切入 Cookie 登录态备用链路。
- 📲 **Telegram 交互式自动扫码续期 Cookie**
  - Cookie 失效时，直接在 Telegram 私聊或通知频道发送 `/refresh_cookie`，服务自动调起无头 Chromium 浏览器生成登录二维码推送到 TG；
  - 手机 App 扫码确认后自动持久化新登录态并热重载生效，无需手动抓包复制。
- 🚀 **微博 + 抖音双引擎独立运行**
  - 单仓库集成 `weibo-tg` 与 `douyin-tg` 两套独立服务，共用同一个 Telegram Bot 和频道；
  - 采用独立进程、独立 SQLite 去重数据库（`state.sqlite3` 与 `douyin-state.sqlite3`）与媒体缓存目录，容错隔离，互不干扰。
- 🛡️ **生产级健壮性与告警系统**
  - 自动检测并规范化图片尺寸（严格限制 < 10MB，防止 Telegram 降级为文件）；
  - 连续失败梯级告警，防止断网时刷屏；发送完成后自动清理本地媒体文件，磁盘零负担。

---

## 🛠️ 快速上手

### 1. 准备配置

克隆仓库并复制模板文件：

```bash
git clone https://github.com/your_username/weibo-tg-forwarder.git
cd weibo-tg-forwarder
cp .env.example .env
cp config.example.yml config.yml
```

编辑 `.env` 填入 Telegram 基础信息：

```dotenv
# Telegram Bot Token（向 @BotFather 申请获得）
TELEGRAM_BOT_TOKEN=your_bot_token_here

# 目标转发频道（公开频道填写 @ChannelUsername，私有频道填写 -100 开头的数字ID）
TELEGRAM_CHANNEL_ID=@your_channel_name

# 管理员与 Bot 私聊的用户数字 ID（用于接收告警和扫码授权）
TELEGRAM_ALERT_CHAT_ID=123456789

# 微博 Cookie（选填，留空默认纯匿名抓取）
WEIBO_COOKIE=
```

编辑 `config.yml` 添加需要监控的目标博主：

```yaml
weibo:
  targets:
    - uid: "1234567890"    # 微博数字 UID
    - uid: "2345678901"
  poll_interval_seconds: 120
  poll_jitter_seconds: 60

douyin:
  targets:
    - uid: "sample_author" # 抖音号
      sec_uid: "MS4wLjABAAAA..." # 对应的 sec_uid
  poll_interval_seconds: 180
  poll_jitter_seconds: 60

telegram:
  notifications_use_channel: true  # 告警和二维码是否直接发到频道（方便直接扫码）

storage:
  keep_media_after_send: false     # 发送成功后自动删除本地下载的临时视频和图片
```

---

### 2. 生产部署（Docker Compose 推荐）

一键构建并后台启动全部监控容器：

```bash
docker compose up -d --build
```

查看运行日志与状态：

```bash
# 查看全部容器状态
docker compose ps

# 实时跟踪微博监控日志
docker compose logs -f weibo-tg

# 实时跟踪抖音监控日志
docker compose logs -f douyin-tg
```

---

### 3. 本地开发与 CLI 调试

如果你需要在本地 Mac 或 Linux 环境直接运行调试：

```bash
# 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,douyin]'

# 运行全链路健康自检
weibo-tg doctor
douyin-tg --config config.yml doctor

# 预览抓取（仅拉取数据，不下载媒体，测试接口可用性）
weibo-tg preview --no-download --limit 1

# 实测单条真实发送到 Telegram 频道（不影响正式去重库）
weibo-tg test-send --uid 1234567890

# 运行自动化测试套件
pytest
```

---

## 🔑 Cookie 自动维护指南

1. **自动告警触发**：当某一微博账号因风控或登录失效连续 3 轮抓取失败，系统会自动启动无头浏览器生成扫码二维码，直接推送至 Telegram。
2. **主动扫码指令**：随时在 Telegram 中向 Bot 发送 `/refresh_cookie`（或 `/refresh`），Bot 会立刻生成最新的微博登录二维码供你用手机扫码。
3. **安全保障**：程序不保存任何账号密码，仅保存扫码后由微博官方下发的登录令牌；新登录态持久化在 `data/weibo_cookie.txt`，热更新无需重启服务。

---

## 📂 项目结构

```text
weibo-tg-forwarder/
├── src/weibo_tg/
│   ├── config.py             # 配置加载与校验
│   ├── models.py             # 数据结构定义 (Post, MediaItem 等)
│   ├── telegram.py           # Telegram 客户端、Live Photo 打包与视频尺寸探测
│   ├── media.py              # 高并发媒体下载、压缩与尺寸规范化
│   ├── weibo.py              # 微博 API 客户端（匿名/Cookie 双轨降级）
│   ├── douyin.py             # 抖音 API 客户端与作品解析
│   ├── service.py            # 微博监控长轮询服务
│   ├── douyin_service.py     # 抖音监控长轮询服务
│   ├── cookie_refresh.py     # Playwright 无头浏览器自动扫码续期
│   └── cli.py                # 命令行交互入口
├── tests/                    # 单元与集成测试套件
├── docker-compose.yml        # Docker 生产编排
├── Dockerfile                # 运行镜像打包定义
├── pyproject.toml            # 项目元数据与依赖管理
└── README.md
```

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议，欢迎提交 Issue 与 Pull Request 共同改进！
