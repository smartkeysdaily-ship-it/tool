# UPI Plus QR Automation — Handover / 交接文档

> 面向接手者与二次开发者：项目结构、核心链路原理、已解决的坑、风控结论、部署与排错。

---

## 1. 项目是干什么的

- **功能**：用 ChatGPT 账号的 Access Token（AT），自动走 ChatGPT Plus 结账流程，提取 **UPI 支付链接 / 二维码**。
  目标产物：`https://payments.stripe.com/upi/instructions/...` 这类 instructions 链接（可转二维码）。
- **技术栈**：Python FastAPI + SQLite + curl_cffi（浏览器 TLS 指纹）+ 自研 sentinel-sdk 求解器（Node 运行官方 sdk.js）+ nginx 反代。

**默认只接印度（IN）UPI。**

---

## 2. 核心提取链 —— 链接是怎么抠出来的（重点）

**链接不是靠浏览器点出来的，是 approve 后从 Stripe 后端 API 轮询出来的。**
整条链在 `app/upi_core_local.py` 的 `generate_upi_qr_link()`（7 阶段流水线）。

### 7 个阶段
```text
1. ChatGPT checkout   → 创建 cs_live_... （0 元优惠券在这里生效）
2. Stripe init        → payment_pages/{cs}/init，拿到应付金额/total_summary
3. 免费试用检测       → require_zero 时校验 due == 0
4. Tax region update  → 设置 IN 账单地址
5. Stripe confirm     → 提交 UPI 支付方式，state 变为 requires_approval
6. ChatGPT approve    → 点“Subscribe”（带 sentinel + so-token）
7. 轮询 payment page  → 从 Stripe 抠 UPI QR / instructions → hydrate → 渲染二维码
```

### 3 个 Stripe payment_page 端点
```python
STRIPE_PAYMENT_PAGE_INIT_URL_T    = "https://api.stripe.com/v1/payment_pages/{cs_id}/init"
STRIPE_PAYMENT_PAGE_CONFIRM_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
STRIPE_PAYMENT_PAGE_GET_URL_T     = "https://api.stripe.com/v1/payment_pages/{cs_id}"
```
请求带 `?key={stripe_pk}&_stripe_version={STRIPE_VERSION}`。

### 提取顺序（approve 通过后）
1. **先查 confirm/approve 响应本身** —— `_upi_extract_next_action()`，递归找 `next_action.upi_handle_redirect_or_display_qr_code.*`
2. **轮询 payment page** —— `GET payment_pages/{cs_id}`，最多 `UPI_QR_POLL_MAX_ATTEMPTS=40` 次、间隔 `2.5s`，每次抠：
   - `hosted_instructions_url`（`https://payments.stripe.com/upi/instructions/...`）★目标
   - `upi_uri`（`upi://...` 深链）
   - `qr_image_url_svg` / `qr_image_url_png`（`https://qr.stripe.com/...`）
   - `expires_at`
3. **还没有就 re-init** —— 再 `POST .../init` 刷一次
4. **hydrate** —— 只有 `hosted_instructions_url` 没 `upi://` 时，`_upi_hydrate_qr_data()` 抓 instructions HTML（`Referer: https://js.stripe.com/`），从 `<meta id="payload">`（base64）解 `mobile_auth_url`/`upi_uri`，或从 `<img src="qr.stripe.com/...">` 抠二维码图

### 最终输出优先级（业务要求）
```text
1) hosted_instructions_url  → link_type = "upi_instructions"   （首选）
2) upi:// 深链              → link_type = "upi_deep_link"
3) pay.openai.com 托管链接  → link_type = "upi_hosted_fallback"（兜底，不算成功）
```

> **关键理解**：approve 成功后，二维码/链接的“载体”是 Stripe 的 `payment_pages` API 和
> `payments.stripe.com/upi/instructions/...` 页面，**与 chatgpt.com 收银台页面无关**。
> 瓶颈只在 approve 能不能过。

---

## 3. 代码结构

| 文件 | 职责 |
|---|---|
| `app/upi_core_local.py` | **核心提取逻辑**。sentinel 求解、创建 checkout、approve、轮询 Stripe payment page 抠 UPI 链接、0 元检测 |
| `app/job_manager.py` | 任务队列、排队名次、软错误重试、链接校验、统计事件 |
| `app/models.py` | pydantic 模型、`public_dict`、`concurrency_limit` 校验 |
| `app/config.py` | 配置加载 |
| `app/db.py` | SQLite + `stat_events` 统计表 |
| `app/sentinel_sdk/` | sentinel 求解：`bundle.py`（SDK 版本/hash）、`runtime/sdk.js`、`client.py`、`runner.py` |
| `app/proxy_pool.py` | 代理池 |
| `app/scan_site.py` | 备用扫码站点集成 |
| `static/index.html` | 前端（Alpine.js，SSE 实时刷新） |
| `requirements.txt` | 依赖（含 `PySocks>=1.7.1`、`curl_cffi`） |

### 关键常量/函数
- `_SENTINEL_CHECKOUT_FLOW = "chatgpt_checkout"` — 创建订单用的 sentinel flow
- `_get_checkout_sentinel()` / `_get_approve_sentinel_pair()` — 求解 token（approve 返回 `(token, so_token)`）
- `_account_device_id()` — 按 AT 派生 device id：`uuid5(NAMESPACE_URL, "oai-did:"+AT[:200])`
- `_chatgpt_checkout_post()` — 创建订单（sentinel + 全套 OAI 头 + `impersonate`）
- `_approve_post()` — approve（带 `openai-sentinel-token` + `openai-sentinel-so-token`）
- `_upi_extract_next_action(data)` — 递归遍历 Stripe 响应抠 UPI 数据
- `_upi_hydrate_qr_data(qr_data, proxy)` — 抓 HTML 补水
- `UPI_QR_POLL_MAX_ATTEMPTS = 40` / `UPI_QR_POLL_INTERVAL = 2.5`

### 全套 OAI 头（创建订单/approve 必带，少了会 400/403）
```text
oai-device-id / oai-session-id / oai-language
oai-client-version = prod-71c4ba1079ebac861d68a9bec3ce2e36fd733c1a
oai-client-build-number = 10961682
oai-telemetry / x-openai-target-path / x-openai-target-route
x-openai-web-frontend / x-oai-is-client-observation
openai-sentinel-token / openai-sentinel-so-token(approve 用)
```
UA 固定用 `sentinel._SENTINEL_UA`（和指纹一致）。

---

## 4. 已解决的坑（重要，别回退）

1. **创建订单 400** —— 根因不是“账号风控”，是缺 `openai-sentinel-token`（flow=`chatgpt_checkout`）+ 缺上面那套 OAI 头。✅ 已修
2. **approve blocked** —— flow 要用 `checkout_session_approval`，且必须带 `openai-sentinel-so-token`。✅ 已修
3. **输出链接优先级** —— `upi_instructions` 优先，其次 `upi://`，最后才托管兜底。
4. **假成功 bug** —— approve 被拦时兜底链接**不再算成功**；只有 `upi_instructions` / `upi_deep_link` / 含 `payments.stripe.com/upi/instructions/` 才算成功。✅ 已修
5. **0 元检测** —— `require_zero=True`，创建订单后严格校验 `amount != 0` 即失败（错误码 `not_zero_due`，不重试）。
6. **sentinel SDK 版本** —— `bundle.py` 记录 `SDK_SHA256` 与 `DEFAULT_SENTINEL_VERSION`，升级时同步更新。
7. **SOCKS 代理** —— 需装 `PySocks`（已写入 requirements.txt）。

---

## 5. 风控结论（血泪教训，必读）

**₹0 优惠是“两层一次性”，点一次 Subscribe 就全烧掉：**

- **账号级**：ChatGPT 记录“该账号已发起过试用结账” → 之后新建任何 checkout session 资格判定不命中 → 显示原价 ₹1,694.07。
- **会话级**：₹0 绑定在创建那一刻的具体 `cs_live_...` session 上。一旦提交，session 从 `open` 进入“已尝试”：
  - 第 1 次 approve = **approved**，但 Stripe SetupIntent 可能 `generic_decline`（如地址没填完）
  - **从第 2 次起，同 session 的 approve 全部 `blocked`**（换 IP 也没用，是会话级风控）
  - 离开/关闭/刷新页面 = session 作废 = 0 元消失

**正确姿势（单发流程）：**
```text
进收银台（当场看到 ₹0）→ 一次性把姓名+地址+全部信息填完 → 只点一次 Subscribe → 出二维码
```
中间任何一步失败（地址没填完就点、Stripe decline），这一发就废——账号和会话都回不去。

**Stripe 侧**：₹0 免费流走的是 **SetupIntent**（`usage=off_session`）不是 PaymentIntent；失败报 `setup_attempt_failed / generic_decline`，页面 “Your card was declined”。

**对自动化的启示**：approve 是“一次性”动作，**不要对同一个 session 反复重试**，否则会加速烧掉 0 元资格。

---

## 6. 配置说明

`config.yaml` 首次启动自动生成（示例见 `config.example.yaml`），**已被 gitignore，切勿提交真实 token**。

关键项：

| Key | 说明 |
|---|---|
| `rust_bot_base_url` | UPI bot API 基址 |
| `rust_bot_token` | API key（**必填**） |
| `concurrency_limit` | 并发 worker 数 |
| `flow_mode` | `auto` 立即入队 / `manual` 手动开始 |
| `proxy_list` | 代理池（支持 country/session 轮换） |
| `extraction_mode` | `local`（本地提链）/ 远端 |
| `scan_site_*` | 备用扫码站点配置 |

---

## 7. 运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
# 浏览器打开 http://127.0.0.1:8787 （不要用 0.0.0.0）
```

---

## 8. 协作/开发约定

1. 只做印度（IN）UPI，不测其它地区。
2. 认证用 Access Token（AT），不用邮箱密码登录。
3. `config.yaml` / `data/` / `sessions/` / `logs/` 属敏感运行时数据，不提交、不打包。

---

*本文件为脱敏后的开源版交接文档。*
