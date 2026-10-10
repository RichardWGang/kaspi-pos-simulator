# Симулятор Kaspi POS-терминала / Kaspi POS 终端模拟器

用于在没有真实终端的情况下联调收银集成。完整模拟 Kaspi POS 的 HTTP API，并内置一个网页控制台 `/ui`。

> v2 说明：本版本修复了 6 个会影响联调的缺陷，详见下方「v2 修复清单」。
> **破坏性变更**：业务错误码（101 / 103 / 105 / 107 / 999）不再作为 HTTP 状态码返回，改为 HTTP 200 + body 内的 `statusCode`。仅 401、404 仍是真正的 HTTP 状态码。
>
> v3 说明：本版本按官方《Smart POS 集成文档》校准了一致性，详见「v3 修复清单（对照官方手册）」。
> **v3 破坏性变更**：
> - HTTP 状态码按手册 3.22 分层：参数错误 **400**、鉴权失败 **401**、令牌过期 **403**、无效 URL **404**；业务码（999 / 101 / 103 / 105 / 107）仍放 body。
> - `expirationDate` 改为 `YYYY-MM-DD HH:MM:SS`。
> - `subStatus` 只使用手册 3.18 的 11 个枚举值，不再出现 `OperationDeclined` / `UnknownStatus`。
> - **退款会校验**：方式必须与原支付一致、金额不得超过可用额度、`transactionId` 必须真实存在。
>
> v4 说明：按手册补齐第二批（P1 / P2），详见「v4 修复清单（第二批）」。
> **v4 破坏性变更**：
> - **默认改为 HTTPS**（手册 2.5）——启动时自动生成自签证书并缓存到 `.certs/`。需要明文 HTTP 请显式加 `--http`。
> - `/v2/status` **要求 `terminalId` 请求头**（手册 3.18），缺失或不匹配返回 400。可用 `--no-terminal-check` 关闭。
> - `actualize` 增加 10 秒节流，10 秒内重复调用返回 `105`。
> - 状态机新增 `Initialize` 阶段：交易发起后 1 秒内查询返回 `Initialize`（此前直接跳到 `WaitUser`）。

## 安装

零依赖，仅需 Python 3.7+（标准库即可，无需 `pip install`）：

```bash
git clone https://github.com/robotickz/kaspi_pos_terminal_simulator.git
cd kaspi_pos_terminal_simulator
```

## 启动

### 一键脚本（推荐，双击即可）

- **`start_https.bat`** —— HTTPS + token 鉴权启动，自动打开浏览器到控制台
- `start.bat` —— 旧入口，等价于 `start_https.bat`

脚本顶部集中了可调参数，改完保存、双击即可，不用再敲长命令：

```bat
set HOST=127.0.0.1
set PORT=8080
set SECURE=1
set EXTRA=
set OPEN_BROWSER=1
```

`SECURE=1` 时脚本实际执行 `kaspi_pos_simulator.py --host ... --port ... --secure --allow-open-reset`，
即除 `/v2/register` 外的所有端点都要 `accesstoken` 头，`/v2/reset` 保持开放以免控制台 Reset 按钮失效。

| 想做的事 | 改法 |
| --- | --- |
| 不需要 token 鉴权 | `set SECURE=0` |
| 真机 / 手机联调 | `set HOST=0.0.0.0` |
| 退回明文 HTTP | `set EXTRA=--http` |
| 时序加速（约 1 秒出结果） | `set EXTRA=--fast` |
| 不想自动弹浏览器 | `set OPEN_BROWSER=0` |

> 开启鉴权后先取 token：`GET https://127.0.0.1:8080/v2/register?name=Kashier1`，
> 之后每个请求带 `accesstoken: <token>`；控制台 `/ui` 打开后在「安全令牌」卡片点一次注册即可。

脚本还会自动找 `python`（找不到再试 `py -3`）、检测端口是否已被占用并给出 `taskkill` 命令。

> 脚本刻意写成**纯 ASCII + CRLF**：`cmd` 读取 UTF-8 批处理时会在内部缓冲区分裂多字节字符，
> 导致后面的中文行被截断报「不是内部或外部命令」。中文说明都放在本文件里。

### 命令行

```bash
python kaspi_pos_simulator.py
# 默认 HTTPS（自签证书），控制台 https://localhost:8080/ui
# 浏览器首次打开会提示证书不受信任，选择「继续访问」
```

> **访问不了 UI？先看协议**：默认走 HTTPS，用 `http://` 打开会失败。
> 现在服务端会回一段 `400 This port serves HTTPS only.`，而不是让浏览器只显示
> `ERR_CONNECTION_RESET`。想用明文就加 `--http` 重启。

| 参数 | 说明 |
| --- | --- |
| `--host` | 监听地址，默认 `0.0.0.0`（会打印局域网暴露警告） |
| `--port` | 端口，默认 `8080` |
| `--http` | 改用明文 HTTP（默认已是 HTTPS，此参数仅供本地调试） |
| `--ssl` | 强制 HTTPS；无法生成/找不到证书时直接报错退出，而不是降级 |
| `--cert` / `--key` | 使用自己的 PEM 证书与私钥，不再自动生成 |
| `--cert-dir` | 自签证书缓存目录，默认 `<脚本目录>/.certs` |
| `--secure` | 开启 token 鉴权，除 `/v2/register` 外所有端点需要 `accesstoken` |
| `--allow-open-reset` | 在 `--secure` 下仍允许 `/v2/reset` 免鉴权（不推荐） |
| `--no-terminal-check` | 不校验 `/v2/status` 的 `terminalId` 头（手册 3.18） |
| `--manual-completion` | **手动确认模式**：所有新交易都挂起，必须点控制台按钮或调 `/ui/complete` 才落定 |
| `--no-register-approval` | 注册不等控制台确认，直接签发 token（默认要 `/ui` 点「允许注册」） |
| `--register-timeout` | 注册请求等「允许/拒绝」的秒数，默认 `120`，超时返回 `105` |
| `--scan-timeout` | 扫码超时秒数，默认 `180`（手册 3.18） |
| `--confirm-timeout` | 支付确认超时秒数，默认 `60`（手册 3.18） |
| `--fast` | 压缩全部时序（约 1 秒出结果、超时 6/3 秒），便于手工快速验证 |
| `--no-refund-validation` | 跳过退款校验（方式一致 / 可用额度 / transactionId 存在），仅供随意手工测试 |
| `--no-ui` | 关闭内置网页控制台 |
| `--cors` | 返回宽松 CORS 头（供外部浏览器工具跨域调用） |
| `--store-name` / `--terminal-id` | 覆盖小票中的商户名 / 终端号 |
| `--log-file` | 日志路径，传空字符串关闭文件日志 |
| `-v/--verbose` | 调试日志 |

**HTTPS**：手册 2.5 要求集成必须走 HTTPS，所以默认就是 HTTPS。启动时若 `.certs/` 下没有证书，
会调用系统里的 `openssl`（Windows 上 Git for Windows 自带）自动生成一张自签证书，
SAN 里包含 `localhost`、本机局域网 IP 与 `127.0.0.1`。**这不是 Kaspi 的 `*.kaspipos.kz` 证书**，
收银机需要关闭证书校验（手册 3.1 也说明「若不需要校验证书，可以通过 IP 地址访问」）。

想自己签发：

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365
python kaspi_pos_simulator.py --cert cert.pem --key key.pem
```

## 网页控制台 `/ui`

启动后访问 `http://localhost:8080/ui`，可以：

- 查看终端信息、当前连接状态
- 配置**交易行为**（支付方式 / 交易结果 / 是否手动完成）—— 交易本身由收银机发起，页面不发起
- 展示收银机注册得到的设备名称、设备ID（终端号）、token（页面不发起注册）
- 收银软件发起注册时弹确认框：**允许注册** 才签发 token，**拒绝** 则这次注册失败
- 点「完成支付 / 完成退款」把挂起的交易落定（详见「手动完成」）
- 自动轮询状态并展示三段式进度（等待用户 → 处理中 → 完成）
- 查看小票 `chequeInfo` 与完整响应 JSON
- 交易列表，点击任意一行切换查看
- 一键 `reset` 清空卡住的交易

## Postman 调试

`postman/` 目录下有集合 + 两个环境文件，直接导入即可（Import → Upload Files）：

| 文件 | 作用 |
| --- | --- |
| `Kaspi_POS_Simulator.postman_collection.json` | 23 个请求，按 00 基础 / 01 支付 / 02 退款 / 03 补确认 / 04 令牌 / 05 异常分组 |
| `Kaspi_POS_Simulator.postman_environment.json` | HTTP 环境：`baseUrl=http://127.0.0.1:8080`（配 `--http` 启动） |
| `Kaspi_POS_Simulator_HTTPS.postman_environment.json` | HTTPS 环境：`baseUrl=https://127.0.0.1:8080`（配默认启动） |

集合自带脚本，会自动串起流程：`register` 写入 `accessToken`，`payment` 写入 `processId`，
`status` 成功后写入 `transactionId`（退款要用）。所以按文件夹从上往下点一遍就能跑通。

### 用 HTTPS 调试

1. 启动：`python kaspi_pos_simulator.py --host 127.0.0.1 --port 8080`（默认即 HTTPS）
2. 导入集合 + **HTTPS 环境**，右上角环境切到 `Kaspi POS Simulator - Local HTTPS`
3. 让 Postman 信任自签证书，二选一（推荐前者）：
   - Settings → **Certificates** → *CA Certificates* → 勾选开关并添加 `.certs/cert.pem`
   - Settings → **General** → 关掉 *SSL certificate verification*（全部请求都不校验，不推荐）
4. 发第一个请求 `00 基础 / deviceinfo`，返回 `200 / statusCode 0` 即通

建议用 `--http` 启动，省掉自签证书的麻烦：

```bash
python kaspi_pos_simulator.py --http --port 8080
python kaspi_pos_simulator.py --http --fast --port 8080   # 时序压缩到约 1 秒
```

几个关键点：

1. **`/v2/status` 必须带 `terminalid` 头**，值填 `31452963`（`--terminal-id` 可改）。集合里已预置，想关掉校验用 `--no-terminal-check`。
2. **状态按真实时间推进**：发完 `payment` 等约 6 秒（加 `--fast` 约 1 秒）再点 `status`，或连点几次看 `Initialize` → `WaitUser` → `WaitForQrConfirmation` → `QrTransactionSuccess` 的变化。
3. **退款前必须先有一笔成功支付**，且方式一致。先跑 01 再跑 02，否则 `transactionId` 是空的。
4. **同一时刻只允许一笔未完成交易**，遇到 `107` 就点 `reset`。
5. 判断结果只看 body 的 `statusCode`：HTTP 400 是参数问题、401 鉴权、403 令牌过期、404 路径写错。
6. 若坚持用 HTTPS，二选一：**关掉** Settings → General 的 **SSL certificate verification**；
   或更稳妥地，Settings → Certificates → **CA Certificates** 把 `.certs/cert.pem` 加进去，
   这样校验仍然生效（详见下方「HTTPS 验证」）。

## HTTPS 验证

模拟器默认 HTTPS（手册 2.5），首次启动会用 openssl 在 `<脚本目录>/.certs` 下生成自签证书。

**一键验证**（启动临时实例，跑完自动退出）：

```bash
python verify_https.py          # 默认端口 8443
python verify_https.py 8444     # 指定端口
```

覆盖 11 项：证书可读性与 SAN、明文 HTTP 被拒、跳过校验可访问、默认校验下被拒、
信任 cert.pem 后通过、`curl -k` 与 `curl --cacert`、HTTPS 上跑通支付链路、TLS 版本。

### 手工验证

```bash
# 1. 启动（默认就是 HTTPS）
python kaspi_pos_simulator.py --host 127.0.0.1 --port 8443

# 2. 看证书
openssl x509 -in .certs/cert.pem -noout -subject -issuer -dates
openssl x509 -in .certs/cert.pem -noout -text | findstr "Alternative"

# 3. 跳过校验（最省事）
curl -k https://127.0.0.1:8443/v2/deviceinfo

# 4. 信任自签证书后校验（更贴近真机）
curl --cacert .certs/cert.pem https://127.0.0.1:8443/v2/deviceinfo

# 5. 反向验证：不带 -k 必须失败
curl https://127.0.0.1:8443/v2/deviceinfo
# → curl: (60) schannel: SEC_E_UNTRUSTED_ROOT
```

### 客户端接入

| 客户端 | 做法 |
| --- | --- |
| Postman | Settings → General 关 **SSL certificate verification**；或 Settings → Certificates → CA Certificates 添加 `cert.pem` |
| 浏览器 | 打开 `https://127.0.0.1:8443/ui`，警告页点「高级 → 继续访问」 |
| curl | `-k` 或 `--cacert .certs/cert.pem` |
| Python | `ssl._create_unverified_context()`，或 `create_default_context(cafile=".certs/cert.pem")` |

Flutter / Dart 收银机接入自签服务端，二选一：

```dart
// 调试用：接受任意证书（上线前务必删掉）
final client = HttpClient()
  ..badCertificateCallback = (cert, host, port) => true;

// 或：把自签证书作为信任锚（推荐，保留校验逻辑）
final ctx = SecurityContext(withTrustedRoots: false)
  ..setTrustedCertificates('assets/cert.pem');
final client = HttpClient(context: ctx);
```

### 两个注意点

1. **证书绑了局域网 IP**。自签证书的 SAN 包含 `localhost`、`kaspipos.local`、`127.0.0.1`
   和**生成时的局域网 IP**（例如 `10.1.85.50`）。换网络后 IP 会变，此时即使用 IP 访问
   也会校验失败 —— 要么删掉 `.certs` 重新生成，要么改用主机名访问，要么跳过校验。
2. **不能只监听 127.0.0.1 又要真机访问**。收银机是真机的话得 `--host 0.0.0.0`，
   并用上面那个局域网 IP 访问，同时注意 `/v2/reset` 会暴露到局域网。

## API

所有端点均为 `GET`。

| 端点 | 参数 | 说明 |
| --- | --- | --- |
| `/v2/deviceinfo` | — | 终端号、序列号 |
| `/v2/payment` | `amount`（必填）、`method`、`outcome`、`owncheque` | 发起支付 |
| `/v2/refund` | `amount`、`method`、`transactionId`（均必填） | 发起退款，**会校验方式/金额/原交易** |
| `/v2/remains` | `transactionId`（必填），`amount`、`method` 可选 | 退余款：不给 `amount` 时自动退该笔剩余可退金额 |
| `/v2/status` | `processId`（必填）；**请求头需 `terminalId`** | 查询状态 |
| `/v2/actualize` | `processId`（必填）、`outcome` | 对 `unknown` 交易补确认；10 秒内只能一次 |
| `/v2/register` | `name`（必填） | 换取 accessToken / refreshToken，并返回 `deviceName` / `deviceId`（=终端号） |
| `/v2/payment`、`/v2/refund` | 另可带 `hold=1\|0` | 挂起这一笔，等人点按钮（见「手动完成」） |
| `/v2/revoke` | `name`、`refreshToken`（必填） | 轮换 token；`--secure` 下需带 `accesstoken` |
| `/v2/reset` | — | 清空全部交易与可退款台账 |

> `/v2/remains` 在官方手册里没有正式定义，只在错误码 107 / 108 的出现位置和 3.19 里被提到。
> 这里按「退余款」实现， semantics 建议向 Kaspi 确认。

### v2 新增参数

- `method=qr|card|alaqan`：指定支付方式（手册 3.17 新增刷掌 `alaqan`）。不传则随机，**一旦确定就不再变化**。
- `outcome=success|fail|unknown`：指定交易最终状态，用于测试失败与「状态未知」分支。默认 `success`。
- `actualize` 的 `outcome=success|fail|unknown|nointernet`：补确认的结果。
  `unknown` 表示「仍未知，可稍后再试」，`nointernet` 按手册 3.22 返回 HTTP 200 + `statusCode 100`。

```bash
# 模拟一笔刷卡的失败交易
curl "http://localhost:8080/v2/payment?amount=1000&method=card&outcome=fail"

# 模拟状态未知，再用 actualize 补确认
curl "http://localhost:8080/v2/payment?amount=1000&outcome=unknown"
curl "http://localhost:8080/v2/actualize?processId=<上一步返回的 processId>"
```

### 退款校验（手册 2.8 / 3.3 / 3.20）

退款必须与原支付**方式一致**，金额不得超过**可用退款金额**（原金额 − 已退 − 在途），
`transactionId` 必须是此前一笔**成功支付**的标识（QR 取 `orderNumber`，银行卡取 `rrn`）。

```bash
# 先做一笔支付，拿到 transactionId
curl "http://localhost:8080/v2/payment?amount=5000&method=card"
curl "http://localhost:8080/v2/status?processId=<processId>"   # transactionId = rrn

# 用同样的方式退款 —— 通过
curl "http://localhost:8080/v2/refund?amount=5000&method=card&transactionId=<rrn>"

# 用错方式 —— 被拒绝
curl "http://localhost:8080/v2/refund?amount=1000&method=qr&transactionId=<rrn>"
# {"errorText": "Param unvalidated: method. Refund method must match the original payment method (card)",
#  "statusCode": 999}   HTTP 400
```

`/v2/reset` 会同时清空可退款台账；可退款交易仍可通过 `/ui/ledger` 查看。

### 设备注册（收银机侧）

注册这一步**由收银机（点餐机）发起**：

```bash
curl -k "https://127.0.0.1:8080/v2/register?name=Kashier1"
```

```json
{"data": {"accessToken":"…","refreshToken":"…","deviceName":"Kashier1",
          "deviceId":"31452963","expirationDate":"2026-10-11 18:20:00"}}
```

`deviceId` 就是**终端号**：与 `/v2/deviceinfo` 的 `terminalId` 同一个值，
也是 `/v2/status` 请求头要带的值（`--terminal-id` 可改）—— 控制台顶部的
`terminal:` 显示的就是它。

#### 注册要先在控制台点「允许」

默认**不会**立刻签发 token：请求先挂起，控制台 `/ui` 弹出确认框
（收银软件名称 + 请求时间 + 剩余秒数）：

| 操作 | 结果 |
| --- | --- |
| 点 **允许注册** | 才真正签发 token，收银机拿到上面的成功响应 |
| 点 **拒绝** | 收银机收到 HTTP 400 / `statusCode 999` `Registration rejected by the operator in /ui` |
| 一直没人点 | `--register-timeout` 秒（默认 120）后自动失败：`statusCode 105` `Registration timed out` |
| 已有请求在等时又来一个 | 直接 `statusCode 107`，不会排队 |

不想要这一步（比如跑自动化脚本）就加 `--no-register-approval`。

对应接口（模拟器扩展，非 Kaspi 官方）：

```bash
curl -k "https://127.0.0.1:8080/ui/registration"          # data=最近一次注册, pending=待审批请求
curl -k "https://127.0.0.1:8080/ui/registration/approve"  # 允许
curl -k "https://127.0.0.1:8080/ui/registration/reject"   # 拒绝
```

模拟器记住**最近一次注册**，控制台「设备注册信息」卡片每 3 秒刷新并展示：
设备名称、设备ID（终端号）、accesstoken、refreshToken、有效期、注册时间；
页面自己调 `/v2/*` 时会**自动使用**这个 token（不需要在页面上粘贴）。
收银机 `revoke` 之后，该卡片会显示 token 已失效。

### 交易配置（控制台只配置，不发起交易）

交易**一律由收银机（点餐机）发起**，控制台 `/ui` 只配置「模拟器怎么跑」。
打开 `/ui` 的「交易配置」卡片，选好保存即可，对**下一次**交易生效：

| 配置项 | 作用 |
| --- | --- |
| 支付方式 | 收银机没传 `method` 时用什么方式（`自动随机` / `qr` / `card` / `alaqan`） |
| 交易结果 | 收银机没传 `outcome` 时给什么结果（`success` / `fail` / `unknown`） |
| 手动完成 | 勾上后交易停在 `wait`，需在「当前交易」卡片点按钮或调 `/ui/complete` 落定 |

**请求里显式带了参数就以请求为准**：`?outcome=fail` 会盖掉配置里的 `success`。

```bash
# 读配置
curl -k "https://127.0.0.1:8080/ui/config"
# 写配置（只更新传了的键）
curl -k "https://127.0.0.1:8080/ui/config?method=card&outcome=fail&hold=1"

# 收银机照常只带金额，模拟器按配置跑
curl -k -H "accesstoken: <token>" "https://127.0.0.1:8080/v2/payment?amount=2000"
```

`method` 非法值、`outcome` 不在枚举内都返回 HTTP 400 / `999`。
`/ui/config` 是**模拟器扩展**，不属于 Kaspi 官方接口。

### 手动完成（hold / `--manual-completion`）

默认交易按上面的时间线自动落定。想停下来逐步观察中间状态，有两个办法：

| 办法 | 适用 | 说明 |
| --- | --- | --- |
| **服务端开关 `--manual-completion`** | **点餐机 / 收银机联调**（对方不会带额外参数） | 所有新交易一律挂起，与该笔请求是谁发起的无关 |
| 单笔参数 `hold=1` | 只想挂起某一笔 | 只对这一笔生效；`hold=0` 可以覆盖服务端开关改回自动 |

**跟点餐机对接时应该用服务端开关**：点餐机发的是标准请求
（`GET /v2/payment?amount=...`），不可能带 `hold`，只有服务端默认挂起才能拦住自动完成。

```bash
python kaspi_pos_simulator.py --manual-completion          # 或双击 start_https.bat（默认 MANUAL=1）
```

挂起后：点餐机轮询 `/v2/status` 会一直拿到 `wait / WaitUser`（`--fast` 下也一样），
你在控制台点「完成支付」，它下一次轮询才拿到 `success` 和小票。

若确实想让某笔自动完成（比如点餐机自己带重试逻辑），请求加 `hold=0` 即可。

```bash
# 1. 挂起一笔支付
curl "http://localhost:8080/v2/payment?amount=1000&method=card&hold=1"

# 2. 随便什么时候查，都是 wait / WaitUser
curl "http://localhost:8080/v2/status?processId=<processId>" -H "terminalid: 31452963"

# 3. 落定（success / fail / unknown）
curl "http://localhost:8080/ui/complete?processId=<processId>&outcome=success"
```

挂起的交易**不会**被扫码超时判失败，也不会被 300 秒兜底清理，只能靠按钮落定。

`/ui/complete` 是**模拟器扩展**，不属于 Kaspi 官方接口：

| 参数 | 说明 |
| --- | --- |
| `processId` | 必填，要落定的交易 |
| `outcome` | `success` / `fail` / `unknown`，默认 `success` |

已落定的交易再调会返回 `105` + `data.message`；`processId` 不存在返回 `101`；
`outcome` 非法返回 HTTP 400 / `999`。

控制台用法：「交易配置」卡片勾上**手动完成**（或启动时带 `--manual-completion`）→
等收银机发起交易 → 在「当前交易」卡片点 **完成支付** 或 **完成退款**，
落定结果取旁边「手动完成的结果」下拉。两个按钮**始终可点**：优先作用于当前选中的
交易，否则自动挑最近一笔同类挂起交易；一笔都没有时给出「没有挂起的支付/退款交易」提示。

### 时间线

交易在 `createTime` 之后按真实时间推进，**与轮询时机无关**：

| 时间 | status | subStatus |
| --- | --- | --- |
| 0 – 1s | `wait` | `Initialize` |
| 1 – 2s | `wait` | `WaitUser` |
| 2 – 4s | `wait` | `ProcessingCard`（银行卡）/ `WaitForQrConfirmation`（QR、alaqan） |
| 4 – 6s | `wait` | `WaitForPinCode`（银行卡）/ `WaitForQrConfirmation`（QR、alaqan）/ `ProcessRefund`（退款） |
| ≥ 6s | `success` / `fail` / `unknown` | `CardTransactionSuccess` / `QrTransactionFailure` / `ProcessCancelled` 等 |

超过手册 3.18 的超时（扫码 **180 秒**、支付确认 **60 秒**，可用 `--scan-timeout` / `--confirm-timeout`
调整）仍未完成的交易会被判为 `fail` 并给出 `Отмена покупки; Покупатель не подтвердил покупку`；
终态交易按手册要求保留 **24 小时**。

`subStatus` 只取手册 3.18 定义的 11 个值：
`Initialize`、`WaitUser`、`WaitForQrConfirmation`、`ProcessingCard`、`WaitForPinCode`、
`ProcessRefund`、`QrTransactionSuccess`、`QrTransactionFailure`、`CardTransactionSuccess`、
`CardTransactionFailure`、`ProcessCancelled`。

超过 300s 仍处于 `wait` 的交易会被兜底清理。

## v2 修复清单

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | 业务码被写进 HTTP 状态行，返回 `HTTP/1.0 101 Switching Protocols`、`107`、`999` 等非法响应，urllib / dio 客户端直接崩溃 | 业务码统一走 HTTP 200 + body `statusCode`，仅 401 / 404 保留真实 HTTP 状态 |
| 2 | 状态机依赖轮询时机，6s 后首次轮询会永久卡在 `wait` | 改为按 `createTime` 真实时间推进，任意时刻轮询都返回正确终态 |
| 3 | `/v2/actualize` 不可测（代码里没有任何路径产生 `unknown`） | 新增 `outcome=unknown`，可完整测试 `unknown → actualize → success` |
| 4 | 支付方式在 2–4s 窗口内每次轮询重新随机，同一笔交易在 qr / card 之间跳变 | 创建时确定一次，之后不再变化 |
| 5 | 每次 `status` 轮询都刷新 `completedTime`，导致 30 分钟清理永不触发（内存泄漏） | `completedTime` 只在进入终态时写入一次 |
| 6 | `processId` 用毫秒时间戳，同毫秒内两笔交易互相覆盖 | 改为「毫秒时间戳 + 原子递增序号」，保证唯一 |

同时做了以下加固：

- 改用 `ThreadingHTTPServer` + 全局锁，支持并发请求
- 日志中的 token 脱敏（只显示前后各几位），不再打印完整请求头
- `--secure` 下 `/v2/reset` 也需鉴权（`start_https.bat` 默认已带 `--allow-open-reset`，控制台 Reset 按钮仍可用）
- 监听 `0.0.0.0` 时打印局域网暴露警告
- 响应补全 `Content-Length` / `Content-Type` 字符集，启用 HTTP/1.1 keep-alive
- 可选的 `--cors` 与 `do_OPTIONS` 预检支持

## v3 修复清单（对照官方手册）

依据：《Kaspi Smart POS 集成文档（中文版）》。下表为第一批（P0）已修复项。

| # | 手册条款 | 问题 | 修复 |
| --- | --- | --- | --- |
| 1 | 2.8 / 3.3 / 3.20 | 用 `qr` 退一笔 `card` 支付照样返回 0，收银机记错支付方式联调发现不了 | 新增可退款台账，退款时校验**方式必须与原支付一致** |
| 2 | 3.20 | 退款金额不受约束，超额 / 重复退款测不出来 | 校验**金额 ≤ 可用退款金额**（原金额 − 已退 − 在途预留），支持部分退款 |
| 3 | 3.20 | 任意伪造 `transactionId` 都被接受 | `transactionId` 必须对应一笔**已成功**的支付 |
| 4 | 3.18 | `subStatus` 出现枚举外的值 `OperationDeclined` / `UnknownStatus` | 失败按方式返回 `QrTransactionFailure` / `CardTransactionFailure`，`unknown` 返回 `ProcessCancelled`；退款过程返回 `ProcessRefund`，银行卡补 `WaitForPinCode` |
| 5 | 3.14 / 3.15 | `expirationDate` 返回 `Sep 19, 2026 16:36:12` | 改为 `2026-09-19 16:36:12` |
| 6 | 3.16 | 非 `--secure` 模式下 `/v2/revoke` 恒返回 401，令牌续期流程测不了 | 非 secure 模式按 `refreshToken` 定位记录；启用 `--secure` 时才要求 `accesstoken` |
| 7 | 3.15 | 令牌过期返回 401 + `Authorization failed` | 改为 **HTTP 403** + `Session expires and need to be updated`（手册 3.15 正文；注意手册错误码表此处写的是 401，存在矛盾） |
| 8 | 3.22 | body 的 `statusCode` 混入 HTTP 码（401 / 404） | 业务码与 HTTP 码分层：参数错误 400、鉴权 401、过期 403、无效 URL 404，body 内始终为 999 / 101 / 103 / … |
| 9 | 3 工作原理 | 终态交易只保留 30 分钟 | 改为保留 **24 小时** |

顺带调整：`/v2/reset` 同时清空台账；新增 `/ui/ledger` 供控制台下拉选择可退交易；
`105` 错误按手册返回 `data.message`；新增 `--no-refund-validation` 开关。

## v4 修复清单（第二批）

| # | 手册条款 | 问题 | 修复 |
| --- | --- | --- | --- |
| 9 | 3.18 | `/v2/status` 未校验必填的 `terminalId` 请求头 | 缺失 → 400 / 999 `Missing header: terminalId`；不匹配 → 400 / 999 `Unknown terminal`；`--no-terminal-check` 可关闭 |
| 10 | 3.19 | actualize 无频率限制，且一次调用必定转 success，无法复现「仍 unknown」 | 10 秒节流（重复调用 → `105` + `data.message`）；新增 `outcome=fail\|unknown\|nointernet` |
| 11 | 3.17 / 3.20 | 不支持刷掌支付 `alaqan` | `method=alaqan` 可用，与 QR 同用 `orderNumber` 作交易标识，成功/失败走 `QrTransactionSuccess` / `QrTransactionFailure` |
| 13 | 错误码 107 / 108 | `/v2/remains` 未实现 | 按「退余款」实现：不给 `amount` 时退该笔剩余可退金额（手册无正式定义，建议向 Kaspi 确认） |
| 16 | 3.18 | 超时统一 120 秒且不可配 | 拆分为扫码 180 秒 / 支付确认 60 秒，可用 `--scan-timeout` / `--confirm-timeout` / `--fast` 调整；超时判 `fail` |
| 17 | 3.18 | `Initialize` 子状态实际查不到 | 发起后 1 秒内查询返回 `Initialize` |
| 18 | 2.5 / 3.1 | 默认明文 HTTP，手册要求 HTTPS | 默认 HTTPS：启动时用 `openssl` 生成自签证书（缓存到 `.certs/`）；`--http` 可退回明文 |

控制台同步更新：请求自动带 `terminalid` 头、支付方式新增 `alaqan`、类型新增「退余款 remains」、
补确认增加结果下拉（成功 / 失败 / 仍未知 / 网络错误）。

## 测试

```bash
python test_simulator.py
```

92 条回归测试，覆盖 v2 的 6 个缺陷、v3 的退款校验 / subStatus 枚举 / 错误码分层 / 令牌处理、
v4 的 terminalId 头 / actualize 节流与四种结果 / alaqan / remains / 超时 / HTTPS 证书生成、
安全模式、网页控制台，设备注册（`deviceId` 与 `terminalId` 一致 / `/ui/registration` /
revoke 后失效 / 控制台审批：挂起、允许、拒绝、超时、重复请求被拒），
交易配置（默认值 / 配置驱动 method 与 outcome / 配置挂起 / 请求参数优先 / 非法值被拒），
以及手动完成（`hold` 挂起与 `--manual-completion` 服务端模式 /
`/ui/complete` 落定与三种结果 / 重复完成被拒）等，约 100 秒跑完。

## 错误处理

HTTP 状态码与 body 内的业务码是**两层**，都要判断：

```json
{"errorText": "Could not register transaction. Last operation was not completed.", "statusCode": 107}
```

| HTTP | 场景 | body 业务码 |
| --- | --- | --- |
| 400 | 参数缺失 / 校验失败 | 999 |
| 401 | 缺少或无效 `accesstoken` | 999 |
| 403 | 令牌已过期，需 `revoke` 换新 | 999 |
| 404 | URL 不存在 | 999 |
| 200 | 其余业务错误 | 101 / 103 / 105 / 107 |

| 业务码 | 含义 | 处理建议 |
| --- | --- | --- |
| 100 | No internet（actualize 过程中网络错误） | 交易仍是 `unknown`，稍后重试 |
| 101 | Process not found | 交易已过期或被清理，重新开始 |
| 103 | 该交易无法 actualize | 只有 `unknown` 状态才能补确认 |
| 105 | revoke 名称或 refreshToken 不匹配 | 检查参数，详见 `data.message` |
| 107 | 上一笔交易未完成 | 查询其状态，或调用 `/v2/reset` |
| 999 | 参数缺失 / 不合法 / 鉴权失败 / 退款校验未通过 | 看 `errorText` |

## Flutter / 客户端集成要点

- **按 HTTP 码分流**：400 是参数问题（不要重试），401 去 `register`，**403 去 `revoke` 换令牌**，404 是路径写错
- 业务结果一律看 body 的 `statusCode`，为 0 才是成功
- **`/v2/status` 必须带 `terminalId` 请求头**（手册 3.18），值取自 `/v2/deviceinfo`
- **默认走 HTTPS 且证书是自签的**，收银机需关闭证书校验；真机用的是 `*.kaspipos.kz` 的 CA 证书
- 收银机侧超时应与 Smart POS 一致：扫码 180 秒、支付确认 60 秒（手册 3.18 明确要求）
- 补确认（actualize）最小间隔 10 秒，且**可能仍返回 `unknown`**，需按手册 3.19 稍后重试
- 退款前必须保存 `method` 与 `transactionId`（QR 用 `orderNumber`，银行卡用 `rrn`）——
  手册 2.8 要求退款方式与原支付一致，本模拟器会强制校验
- `subStatus` 只需处理手册 3.18 的 11 个枚举值
- 建议轮询间隔 1 秒；状态更新（actualize）最小间隔 10 秒
- 更新 token（`revoke`）时，`--secure` 下必须带**当前** token
- 遇到 107 表示上一笔未结束，先查 `/v2/status` 确认，仍卡住则 `/v2/reset`

## 注意

- 本模拟器为测试用途，交易数据只存在内存中，重启即清空。
- `--host 0.0.0.0` 会让局域网内任何人都能访问，请勿在不可信网络中使用。
- 最后一次在真实设备上对照验证的时间：12.04.2025。

## Лицензия / 许可证

Unlicense — public domain.
