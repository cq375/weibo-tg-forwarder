# 微博更新转发到 Telegram

监控配置中的微博账号。发现新微博后下载原图、普通视频和 Live Photo 的静态原图及动态 `.mov/.mp4` 文件，再转发到指定 Telegram 频道。

同一个项目还提供独立的 `douyin-tg` 服务，可监控指定抖音账号，转发普通视频、图集静态图和 Live 图的动态视频。两个服务共用一个 Telegram Bot 和频道，但分别使用独立进程、SQLite 和媒体目录。

抓取顺序为匿名移动端、匿名桌面端、Cookie 移动端、Cookie 桌面端。匿名接口被风控后会进入 10 分钟冷却期，其间直接使用 Cookie；未配置 Cookie 时会输出明确错误。

## Mac 本地测试

```bash
git clone https://github.com/cq375/weibo-tg-forwarder.git
cd weibo-tg-forwarder
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,douyin]'
cp .env.example .env
cp config.example.yml config.yml
```

先检查匿名抓取，不下载媒体：

```bash
weibo-tg doctor
weibo-tg preview --no-download --limit 1
```

如果匿名接口返回 `432`、`403` 或要求登录，在 `.env` 中填写 `WEIBO_COOKIE`。Cookie 可从浏览器开发者工具的微博网络请求中复制完整的 `Cookie` 请求头，建议使用专用微博账号，不需要把账号密码写进配置。

验证原图、视频和 Live Photo 下载：

```bash
weibo-tg preview --limit 1
```

文件会保存到 `data/media/<UID>/<微博ID>/`。Live Photo 会包含一张原图和一个 `.mov` 或 `.mp4` 动态文件。Telegram Bot API 不支持原生 Live Photo 对象，因此频道中会按“静态照片 + 动态视频/文件”发送，原始资源均保留。

## 配置 Telegram

在 `.env` 中设置：

```dotenv
WEIBO_COOKIE=完整微博Cookie，可留空
TELEGRAM_BOT_TOKEN=BotFather提供的Token
TELEGRAM_CHANNEL_ID=@频道用户名或-100开头的频道ID
TELEGRAM_ALERT_CHAT_ID=管理员与 Bot 私聊的数字 ID
```

Bot 必须加入目标频道并具有发布消息权限。运行检查：

```bash
weibo-tg doctor
```

管理员先在 Telegram 私聊 Bot 并发送 `/start`，然后查询私聊 ID：

```bash
weibo-tg telegram-chats
```

把输出的数字写入 `.env` 的 `TELEGRAM_ALERT_CHAT_ID`。当前项目已在 `config.yml` 设置 `notifications_use_channel: true`，因此健康告警、登录二维码和普通微博都会发送到 `TELEGRAM_CHANNEL_ID`；这个私聊 ID 仍然用于授权管理员命令。

立即将某个账号的最新微博发送到频道，用于完整链路测试：

```bash
weibo-tg test-send --uid <配置中的微博UID> --keep-media
```

该命令会真实发布 Telegram 消息，但不会写入正式监控的去重数据库。去掉 `--keep-media` 后，发送成功即删除本次下载文件。

首次正式启动只记录当前已有微博，不会转发历史内容：

```bash
weibo-tg run --once
```

持续监控：

```bash
weibo-tg run
```

默认每 `120 ± 60` 秒轮询一次，即每 `1-3` 分钟检查。成功发送后删除本地媒体；将 `config.yml` 中的 `keep_media_after_send` 改为 `true` 可长期保留。

任一目标账号连续 `3` 轮抓取失败时，服务会向目标 Telegram 频道发送告警；故障持续时每 `6` 小时提醒一次；全部恢复后发送恢复通知。告警状态保存在 SQLite 中，容器重启不会重复刷屏。

### Cookie 自动扫码续期

连续抓取失败 `3` 轮后，服务会启动一次无头 Chromium，在微博转发频道发送登录二维码。使用微博 App 扫码并确认后，程序会验证两个目标 UID，把新登录态保存到 `data/weibo_cookie.txt`，并立即恢复当前监控进程。二维码在成功或超时后会从服务器删除，系统不会保存微博账号、密码或验证码。登录二维码相当于一次性登录凭证，只有在转发频道成员完全可信时才应启用 `notifications_use_channel: true`；关闭后会恢复管理员私聊安全模式。

扫码等待时间默认 `300` 秒；超时后 `6` 小时再尝试。持久化 Cookie 的优先级高于 `.env`。需要改回手工 Cookie 时，先删除 `data/weibo_cookie.txt`，再修改 `.env` 并重启容器。

可以主动测试扫码流程：

```bash
docker compose run --rm weibo-tg \
  weibo-tg --config /app/config.yml refresh-cookie
docker compose restart weibo-tg
```

测试命令会把二维码发送到 Telegram，并等待扫码。验证成功后重启后台容器，使其读取新 Cookie 文件。

常驻服务运行时，可以直接在上面配置的管理员私聊中发送以下任一命令重新扫码：

```text
/refresh_cookie
```

也接受 `/refresh-cookie` 和 `/refresh`。二维码仍按现有通知路由发送：`notifications_use_channel: true` 时发送到微博转发频道，否则发送到管理员私聊。扫码成功后，运行中的监控会立即替换内存中的登录态并写入 `data/weibo_cookie.txt`，不需要重启容器。服务启动时会忽略启动前积压的旧 Telegram 消息，避免旧 `/start` 或旧刷新命令被误执行。

不要在常驻服务运行时同时执行 `telegram-chats`，两个进程都会读取 Telegram `getUpdates`，可能互相抢消息。

## Docker 部署

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

数据保存在宿主机的 `data/`。日本 IP 可以使用；实际稳定性取决于微博风控，建议保留可用 Cookie 作为回退。

### 上传到 Linux 服务器

在 Mac 项目目录执行，替换服务器地址和用户名：

```bash
SERVER=user@服务器IP
ssh "$SERVER" 'mkdir -p ~/weibo-tg'
rsync -az \
  --exclude '.git' \
  --exclude '.idea' \
  --exclude '.venv' \
  --exclude '.pytest_cache' \
  --exclude 'data' \
  --exclude '.env' \
  ./ "$SERVER:~/weibo-tg/"
scp .env "$SERVER:~/weibo-tg/.env"
ssh "$SERVER" 'chmod 600 ~/weibo-tg/.env'
```

服务器安装并启动 Docker 后执行：

```bash
cd ~/weibo-tg
docker compose build
docker compose run --rm weibo-tg \
  weibo-tg --config /app/config.yml doctor
docker compose run --rm weibo-tg \
  weibo-tg --config /app/config.yml run --once
docker compose up -d
docker compose logs -f --tail=100
```

`run --once` 在服务器创建首次基线，不转发历史微博。Compose 使用 `restart: unless-stopped`，并将日志限制为最多 `5 × 10 MB`。还需要确保 Docker 服务随系统启动：

```bash
sudo systemctl enable --now docker
```

## 测试

```bash
pytest
```

## 抖音监控

在 `config.yml` 的 `douyin.targets` 中配置抖音号和对应的 `sec_uid`：

```yaml
douyin:
  targets:
    - uid: "LL2003.1221"
      sec_uid: "MS4wLjABAAAA..."
  poll_interval_seconds: 120
  poll_jitter_seconds: 60
  max_pages: 2
  request_timeout_seconds: 25
```

抖音默认使用匿名签名请求，不需要登录 Cookie。配置 `DOUYIN_COOKIE` 或 `data/douyin_cookie.txt` 后会优先使用登录态，因为未登录访客和已登录用户看到的作品集合可能不同；登录态请求失败时自动退回匿名接口。抖音 Cookie 与微博 Cookie 不通用。配置了登录态后，`doctor` 会逐个账号对比两种身份首页的作品差异，并列出“登录独有”和“匿名独有”的作品。可以先解析抖音号：

```bash
douyin-tg resolve LL2003.1221
douyin-tg --config config.yml doctor
```

只抓取和解析，不下载媒体：

```bash
douyin-tg --config config.yml preview --no-download --limit 1
```

下载真实媒体但不发送 Telegram：

```bash
douyin-tg --config config.yml preview --uid LL2003.1221 --limit 1
```

普通视频、图集静态图和 Live 图动态视频都会保存到 `data/douyin-media`。Live 图会先发送静态图相册，再发送对应的动态视频。Telegram Bot API 普通上传上限约为 50MB；超过上限的原文件保留在服务器，并在频道发送说明和原文链接。

首次启动只记录当前已有作品，不转发历史内容。之后每 `120 ± 60` 秒轮询一次，即每 `1-3` 分钟检查，按发布时间升序转发新作品。状态保存在独立的 `data/douyin-state.sqlite3`，以作品 `aweme_id` 去重，因此不会和微博数据库混用。

本地完整测试发送（会真实发布到频道）：

```bash
douyin-tg --config config.yml test-send --id LL2003.1221 --keep-media
```

服务器首次建立抖音基线，再启动独立容器：

```bash
cd /root/weibo-tg
docker compose run --rm douyin-tg \
  douyin-tg --config /app/config.yml doctor
docker compose run --rm douyin-tg \
  douyin-tg --config /app/config.yml preview --no-download --limit 1
docker compose run --rm douyin-tg \
  douyin-tg --config /app/config.yml run --once
docker compose up -d douyin-tg
docker compose logs -f --tail=100 douyin-tg
```

不要用不带服务名的 `docker compose up -d --force-recreate`，否则会同时重建微博容器。抖音服务不会调用 Telegram `getUpdates`，因此不会抢微博的 `/refresh_cookie` 命令。

每个抖音账号独立记录抓取健康状态。连续 `3` 轮失败后向配置的 Telegram 通知频道发出首个告警，持续失败时只在第 `50`、`100`、`150` 等轮次再次提醒，避免服务器断网时刷屏。单个账号失效不会让整个服务退出。
