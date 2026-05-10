# wechat-passcode-login

微信公众号扫码口令登录 — 扫码关注公众号 + 发送口令验证码完成网页登录，不依赖微信认证服务号资质。

## 为什么是「关注 + 验证码」而不是「扫码登录」

微信官方的"扫码登录"流程依赖 [带参二维码 `qrcode/create`](https://developers.weixin.qq.com/doc/service/api/qrcode/qrcodes/api_createqrcode.html)：

> **本接口支持「服务号（仅认证）」账号类型调用。其他账号类型如无特殊说明，均不可调用。**

御坂实测证实：未认证服务号 / 订阅号调用全部返回 `errcode: 48001 api unauthorized`。同样的限制还卡死了 `user/info`、`user/get`、`tags/get`、网页授权 `sns/userinfo` 等几乎所有"用户相关"接口 —— 拿用户昵称头像也走不通。

**绕过路径**：用所有公众号都开放的两个接口拼一个登录方案：

| 接口 | 资质要求 | 用途 |
|---|---|---|
| 接收普通消息（POST 回调） | 任何公众号 ✅ | 收到用户发的验证码 |
| 被动回复用户消息（XML 响应） | 任何公众号 ✅ | 回"登录成功"提示 |

代价：用户多一步「在公众号里把 6 位数字发出去」，不再是单纯扫码。但**对未认证号是唯一可行的方案**。

## 验证码设计

### 形态

- **6 位纯数字**：100 万空间，对手机端微信输入友好（数字键盘）
- **TTL 300 秒**：用户从扫到关注到输码的真实耗时窗口，过短易失败、过长扩大攻击面
- **一次性**：`status: pending → scanned`，consume 后立即从可匹配池移除
- **进一步幂等**：scanned → `issued` 状态机锁防止前端重复轮询签发多份 cookie

### 与 session_id 的关系

```
session_id (32B URL-safe 随机) ←→ code (6 位数字)
```

为什么不直接用一个：
- **session_id** 是 *给浏览器轮询用* 的会话标识，用户不需要看到 → 越长越安全（32 字节随机）
- **code** 是 *给用户在微信里输入* 的，必须短、必须只是数字 → 6 位
- 两者解耦让 code 可以重生成（过期自动刷新），而 session 一旦过期整个链路重启

### 防爆破

单纯 6 位 = 100 万空间，理论上人肉撞库概率不高，但**多个 openid 并行撞**能把成功概率推高。三层防御：

| 层 | 策略 | 实现 |
|---|---|---|
| 1. 时间窗 | code 5 分钟过期 | `LoginSession.created_at` 过期即失效 |
| 2. 一次性 | 命中即消费 | `mark_issued` 状态机 |
| 3. 单点限速 | 单 openid 错 5 次锁 10 分钟 | `FailCount` 表 |

> ⚠️ 当前**没做** IP 维度限速 + 全局速率（多 openid 并发撞）。生产规模下应再加：
> - 同 IP 1 分钟最多 N 次失败
> - 全局每秒 consume 调用上限（限制并行尝试速度）

### 状态机

```
LoginSession.status:
  pending  ──consume(code, openid)── scanned ──mark_issued()── issued
                  ↓ 失败
              FailCount++
                  ↓ ≥5
              拒绝该 openid 10 分钟
```

`mark_issued` 是幂等签发的关键 —— 即便前端 2 秒轮询一次 `/login/status`，只有第一次能把 `scanned → issued`，后续轮询走 `s.status != "scanned"` 分支，不再签发新 cookie。

### 为什么不用 JWT / 短链 / 短信

- **JWT**：无服务端撤销能力（除非加 jti 表，相当于 session 复杂度），且 token 过长不适合让人在微信里手输
- **短链 redirect**：微信公众号不支持点击外链直接 callback 回 web（要走未认证号没有的网页授权）
- **短信**：要短信通道资质 + 钱，且额外引入手机号绑定流程

---

## 流程

```
1. 浏览器打开 /                   → 显示二维码 + 6 位验证码 + 倒计时
2. 用户微信扫码 → 关注公众号       → 公众号回复"请发验证码"
3. 用户在公众号给该号发 6 位数字   → 服务收到 callback，匹配 code
4. 网页轮询 /login/status → scanned → 调 /me → 显示已登录
5. 浏览器拿到 HttpOnly cookie，刷新页面仍登录，TTL 7 天滑动续期
```

## 模块

```
src/
  config.py        Settings + SQLModel engine + init_db
  models.py        LoginSession / FailCount / AuthSession
  session_store.py 6 位 code -> session 状态机；防爆破（5 次/openid）
  auth_store.py    cookie token <-> openid，token 存 SHA-256 hash
  crypto.py        WXBizMsgCrypt（AES-CBC，安全模式 AES + msg_signature）
  main.py          FastAPI：/login/start /login/status /me /logout /wechat
static/
  index.html       单文件前端（卡片 UI + 状态机 + 倒计时 + 自动刷新）
  qrcode.jpg       公众号二维码（gitignore，本地放）
```

## 配置

复制 `.env.example` → `.env`，填：

```
WECHAT_APP_ID=wx...
WECHAT_TOKEN=<3-32 位英数>    # 与微信后台「服务器配置」Token 一致
WECHAT_AES_KEY=<43 位>        # 安全模式必填；明文模式留空
HOST=0.0.0.0
PORT=8000                    # 公网部署用 80 或 443
```

**fail-fast**：缺 `WECHAT_APP_ID` / `WECHAT_TOKEN` 启动直接 raise。

将公众号二维码图片放到 `static/qrcode.jpg`（从微信公众平台后台「公众号二维码」下载）。

## 微信公众平台后台配置

**设置与开发 → 基本配置 → 服务器配置**：

| 字段 | 值 |
|---|---|
| URL | `http://<your-domain-or-ip>/wechat`（80/443 端口） |
| Token | 与 `.env` 的 `WECHAT_TOKEN` 一字不差 |
| EncodingAESKey | 点「随机生成」，写入 `.env` 的 `WECHAT_AES_KEY` |
| 消息加密方式 | **安全模式** 或 **明文模式**（御坂代码两者都支持） |
| 数据格式 | XML |

提交时微信发 `GET /wechat?signature=...&echostr=...`，服务做 SHA1 校验通过后回 `echostr`。

> ⚠️ 服务号需先把服务器**公网出口 IP** 加到「IP 白名单」，否则调微信 API 报 `errcode 40164`。

## 启动

```bash
uv sync
uv run python -m src.main
```

## API

| Method | Path | 用途 |
|---|---|---|
| GET | `/` | 登录页 HTML |
| GET | `/static/qrcode.jpg` | 公众号二维码 |
| GET | `/login/start` | 创建 session，返回 `{session_id, code, ttl}` |
| POST | `/login/status` | body `{session_id}` → `{status: pending\|scanned\|expired}`；scanned 时下发 cookie |
| GET | `/me` | 读 cookie，返回 `{openid, expires_at}` 或 401；命中即续期 |
| POST | `/logout` | 撤销 cookie + db 行 |
| GET | `/wechat` | 微信验签 echostr |
| POST | `/wechat` | 微信事件回调（subscribe / text 6 位数字） |

## 安全要点

- cookie：`HttpOnly` + 可配 `secure` / `samesite`；token 在 db 存 SHA-256 hash，不存原值
- 同 openid 登录会自动撤销旧 session（防多端冒用）
- session_id 通过 POST body 传递，不进 access log query
- 签名校验全部用 `hmac.compare_digest` 防定时攻击
- XML 回复使用 `ElementTree` 自动转义，无 CDATA 注入
- 防爆破：6 位 code 5 分钟 TTL，单 openid 错 5 次锁定 10 分钟
- 安全模式 AES-CBC：解密后校验 `receive_id == app_id`，加密回包重算 `msg_signature`

## 局限

- 订阅号 / 未认证服务号**不能**调 `user/info`，所以登录后只有 `openid`，没有昵称/头像
- 想拿用户资料 → 需做微信认证（¥300/年）→ 改用 `qrcode/create` 带参二维码 + `cgi-bin/user/info`

## 部署示例（http :80 + IP 直连）

```bash
rsync -av --exclude='.venv' --exclude='data' . user@host:~/wechat-passcode-login/
ssh user@host 'cd ~/wechat-passcode-login && uv sync && nohup sudo -E uv run python -m src.main > /tmp/wechat_passcode_login.log 2>&1 &'
```

> 微信后台 URL 不接受 IP，需要至少一个域名（可用 nip.io 等伪域名服务测试）。
