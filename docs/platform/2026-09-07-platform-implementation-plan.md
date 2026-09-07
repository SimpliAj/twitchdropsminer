# 基于 TwitchDropsMiner 的多用户挂机平台 —— 可行性分析与实施方案

> 版本：v0.2（草案） | 日期：2026-09-07 | 状态：待评审
> v0.2 变更：纳入部署约束（单机、无代理池、并发上限 3），编排器简化为预置槽位模型
> 评估基线：twitchdropsminer v1.4.3（`6559ddf`，MIT License）

---

## 1. 背景与目标

### 1.1 业务目标

基于开源项目 [TwitchDropsMiner](https://github.com/SimpliAj/twitchdropsminer)（MIT 许可，商用无障碍），构建一个多用户自助挂机平台：

1. 用户注册平台账号；
2. 自助绑定自己的 Twitch 账号（OAuth 设备码流程）；
3. 选择要挂机的游戏；
4. 后台自动为其创建/管理挖矿实例（下称 **worker**）；
5. 用户随时查看实例状态、掉落领取进度与历史。

### 1.2 部署约束（当前阶段）

- 单台电脑部署（控制平面与全部 worker 同机运行）；
- 无代理池，全部账号共享同一出口 IP；
- 并发硬上限：最多 3 个账号同时挂机。

### 1.3 MVP 定义（最小闭环）

- 用户注册/登录（邮箱 + 密码）；
- 绑定 1 个 Twitch 账号（MVP 阶段采用"引导式绑定"，见 §5.4）；
- 为每个绑定账号分配 1 个预置 worker 槽位（无代理，见 §6.3）；
- 用户可选游戏、查看实例状态与掉落历史；
- 平台后台可启停/删除实例、查看全局状态。

### 1.4 非目标（MVP 不含）

- 计费/支付（Phase 3，可选）；
- 代理池与动态扩容（超出 3 账号后再评估）；
- 多机调度/K8s；
- 移动端 App。

---

## 2. 现状评估（代码级）

### 2.1 可直接复用的部分

| 模块 | 位置 | 复用方式 |
|---|---|---|
| 挖矿核心（状态机 / 看播心跳 / 掉落认领） | `src/core/client.py`、`src/services/`（watch/inventory/channel/maintenance） | 原样保留，作为 worker 主体 |
| Twitch 内部 GQL 操作定义 | `src/config/operations.py` | 原样保留（需跟随上游维护） |
| WebSocket 池（User.Drops 进度推送、分片重连） | `src/websocket/pool.py` | 原样保留 |
| OAuth 设备码登录 | `src/auth/auth_state.py:68`（`POST id.twitch.tv/oauth2/device` + 轮询） | 保留；Phase 2 增加 REST 包装 |
| 游戏选择 / 优先级 / 黑名单 / 自动发现 | `src/config/settings.py` + `SettingsUpdate` API（`src/web/app.py:501`） | 原样保留 |
| 每账号代理支持（含连通性验证） | `settings.proxy`、`/api/settings/verify-proxy`（`app.py:1030`） | 原样保留，接入平台代理池 |
| 掉落历史 / 统计 | `drops_history.json`、`/api/stats`（`app.py:907`）、`/api/drops-history`（`app.py:896`） | 原样保留，控制平面定时聚合 |
| Web UI（7-tab 控制面板） | `web/`（index.html + app.js） | 作为"用户高级面板"直接复用 |
| i18n（19 语言） | `lang/`、`src/i18n/` | 原样保留 |
| 健康检查 / 启停端点 | `/healthz`（`app.py:650`）、`/api/pause`、`/api/resume`、`/api/reload` | 原样保留，编排器直接调用 |

### 2.2 必须改造的部分

| 项 | 位置 | 改造内容 |
|---|---|---|
| 弱鉴权（session cookie 即密码明文、CORS 全开、`/api/pair/claim` 无鉴权） | `app.py:394`、`app.py:415`、`app.py:195` | fork 后新增 API Token 中间件、收紧 CORS、删除配对端点 |
| 实例编排硬编码作者 VPS（pm2+nginx+固定域名） | `app.py:1719`、`scripts/manage_instance.sh` | 全部剥离，由控制平面接管 |
| 账户槽位 / 舰队 API（单用户多账号的产物） | `app.py:1566-2139` | 剥离（平台的多用户模型与它无关） |
| 自助更新（`git pull` 上游 + pm2 重启） | `app.py:1258`、`app.py:1235` | 剥离，改用平台自己的镜像发布 |
| 设备码登录缺 REST 端点（现为 GUI 事件驱动） | `app.py:1184`、`app.py:1194` | Phase 2 新增 `/api/oauth/start`、`/api/oauth/status` |
| 单进程单账号的全局状态（路径 import 时定死、全局单例） | `src/config/paths.py:68`、`app.py:433`、`src/i18n/`、`src/services/drop_minutes_cache.py` | **不改**——采用"1 账号 = 1 进程"模型绕开；规模固定 3 个，无需进程内多账号 |

### 2.3 必须新建的部分

用户系统、控制平面 API、槽位编排器、数据库、通知、监控告警、公共站点。详见 §6。

---

## 3. 总体架构

```
                    ┌──────────────────────────── 控制平面（新建） ────────────────────────────┐
                    │                                                                            │
用户浏览器 ──HTTPS──▶│  公共站/用户面板     用户服务        绑定服务         编排器     状态聚合    │
                    │  (Next.js/Vue)     (FastAPI)   (设备码, Phase2)   (asyncio)  (定时拉取)   │
                    │       │                │             │              │          │          │
                    │       └────────────────┴────────────── PostgreSQL ──────────────┘          │
                    └──────────────┬───────────────────────────┬───────────────────────────────┘
                                   │ X-TDM-Token（仅内网）        │
                     ┌─────────────▼────────────┐    ┌───────────▼──────────┐
                     │  worker #1（Docker 容器）  │    │  worker #2（Docker 容器）│  … 每绑定账号 1 个
                     │  挖矿核心 + Web UI        │    │  独立数据卷、可选代理   │
                     │  /api/status /api/stats  │    │  仅内网可达            │
                     └──────────────────────────┘    └──────────────────────┘
```

### 关键设计决策

1. **1 账号 = 1 worker，预置 3 个槽位**
   现状即此模型（`src/__main__.py:77` 单 `Twitch` 客户端）；进程级隔离最干净、故障域最小。worker 无视频流、纯 asyncio，单容器内存估计 100–300MB，3 个 worker 总开销 <1GB，单台电脑轻松承载。规模固定为 3，**不需要动态容器编排**——docker-compose 预置 3 个 worker 服务（端口 8080/8082/8084），控制平面只做"槽位分配与回收"（§6.3）。进程内多账号密度优化不再需要。

2. **并发硬上限 = 3（同 IP 风控约束）**
   无代理池，全部账号共享同一出口 IP。3 个并发账号恰好处于本项目自身的风控提示阈值（worker UI 对"3+ 实例同 IP"会警告，`app.py:1741`）。平台层做硬配额（全平台最多 3 个运行中实例），**该假设以 Phase 0 灰度实测验证（附录 B）**；若触发风控，降为 2 或引入代理再开放。

3. **控制平面与 worker 解耦**
   挖矿循环完全在 worker 内部，控制平面挂掉不影响已运行实例；控制平面只负责编排、配置与展示。这使平台可渐进式开发，且控制平面可随时重启。

4. **两步走绑定体验**（§5.4）
   MVP：用户被引导到其 worker 自带 Web UI 完成 Twitch 登录与选游戏（零改造、零风险）；Phase 2：设备码流程 REST 化，实现平台内无缝绑定。

### 目标态请求时序（Phase 2 达成）

```
用户点"绑定 Twitch"
  → 控制平面创建 twitch_accounts + miners 记录
  → 编排器分配空闲槽位（轮换 WEB_PASSWORD / TDM_API_TOKEN 并重启槽位容器）
  → 控制平面 POST worker /api/oauth/start → 返回 {user_code, verification_uri}
  → 平台页面展示激活码 + "打开 twitch.tv/activate"按钮
  → 控制平面轮询 worker GET /api/oauth/status（间隔 5s，与 Twitch 要求一致）
  → 完成 → 用户选游戏（PUT /api/v1/miners/{id}/games → 转发 worker POST /api/settings）
  → worker 开始挖矿；控制平面每 30s 聚合 /api/status 与 /api/stats，掉落写入 drop_events
```

---

## 4. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 控制平面后端 | Python 3.12 + FastAPI + SQLAlchemy/Alembic | 与 worker 同栈同版本，复用团队知识；asyncio 与 docker 交互自然 |
| 数据库 | SQLite 起步（3 用户规模完全够用），SQLAlchemy + Alembic 预留迁移 PG 的路径 | 单机、低并发，SQLite 零运维 |
| 任务调度 | 控制平面内置 asyncio 循环 + APScheduler | 规模（≤3 实例）无需 Redis/消息队列 |
| Worker 运行 | Docker Compose 预置 3 个 worker 服务（固定端口 8080/8082/8084）；无 Docker 环境也可用原生进程 + `TDM_PORT`/`TDM_DATA_DIR` 环境变量 | 项目本身 Docker-first（`docker-compose.yml` 现成） |
| 前端 | 用户面板/公共站：Next.js 或 Vue 3；worker 7-tab UI 原样内嵌 | worker UI 已是成熟单页面板 |

---

## 5. Worker Fork 改造清单

### 5.1 Fork 策略

- Fork 本项目为私有仓库 `miner-worker`，`main` 跟随上游 release，定期 cherry-pick 上游 bugfix（上游路线独立演进，需持续跟踪其 release）。
- 改造只发生在 `src/web/` 与入口文件；**不动 `src/core`、`src/services`、`src/api`、`src/websocket`**，保证上游合并成本最低。

### 5.2 剥离清单（Phase 1）

| 端点/文件 | 处理 |
|---|---|
| `/api/instances*`（`app.py:1738-1822`） | 删除；实例生命周期由控制平面管理 |
| `/api/accounts*` 槽位与舰队 fan-out（`app.py:1566-2139`） | 删除 |
| `/api/self-update`（`app.py:1258`）、`/api/restart` 的 pm2 分支（`app.py:1235`） | 删除；平台统一发布镜像 |
| Discord bot 配对 `/api/pair/*`（`app.py:1385-1424`） | 删除（Discord webhook 保留，走实例 settings） |
| `scripts/manage_instance.sh` | 不打包 |

### 5.3 新增清单

1. **`TDM_API_TOKEN` 中间件（Phase 1，必做）**
   控制平面对 worker 的所有管理调用带 `X-TDM-Token` 头，worker 在现有 `PasswordAuthMiddleware`（`app.py:372`）中追加校验。worker 端口只绑 Docker 内网，公网不可达。

2. **随机化启动配置（Phase 1）**
   `WEB_PASSWORD` 由编排器生成随机值注入并加密存库；用户访问面板时控制平面反代并代填 cookie（用户无感知）。

3. **控制端点确认可用（无需改，Phase 1 直接用）**
   `/healthz`、`/api/status`、`/api/stats`、`/api/drops-history`、`/api/channels`、`/api/pause`、`/api/resume`、`/api/reload`、`/api/settings`(GET/POST)。

4. **REST 化设备码登录（Phase 2）**
   - `POST /api/oauth/start`：调用 `_AuthState._oauth_login`（`auth_state.py:68`）的设备码申请部分，返回 `{user_code, verification_uri, expires_in}`；
   - `GET /api/oauth/status`：返回 `pending / done(username) / captcha / failed`；
   - 实现要点：把 `auth_state.py` 里的阻塞循环拆为可挂起的增量状态机（申请设备码 → 轮询 token 端点），登录成功后 cookies 仍写入该账号数据卷 `cookies.jar`，与现有一致。

5. **凭证落盘加密（Phase 2）**
   `cookies.jar` 写盘前用 Fernet 加密；密钥由控制平面按账号生成、经 env 注入。

### 5.4 两步走绑定策略（为什么 MVP 先不做无缝绑定）

现状登录流是 WebSocket/事件驱动的 GUI 交互（`gui_manager.login` + `/api/oauth/confirm`，见 `app.py:1184/1194`），REST 化需要拆解 `_auth_state` 的阻塞循环，改造量中等且有回归风险。

**MVP 方案（零改造）**：控制平面在用户点击"绑定"时创建 worker，把用户引导到该 worker 面板（平台反代），用户在其原生 UI 上完成设备码登录与选游戏。体验略折衷，但风险最低、上线最快。Phase 2 再做无缝化。

---

## 6. 控制平面详细设计

### 6.1 数据库 Schema（核心表草案）

```sql
users(id PK, email UNIQUE, password_hash, status, created_at)

twitch_accounts(id PK, user_id FK, twitch_user_id, twitch_login,
                cookies_encrypted BYTEA,           -- Phase 2
                status, created_at, last_login_at)

miners(id PK, account_id FK, slot_no, port, status,    -- 槽位 1..3；编排器状态机（§6.3）
       settings_jsonb,                             -- 镜像 worker settings.json（游戏/黑名单/webhook）
       web_password_encrypted,
       created_at, updated_at)

miner_events(id PK, miner_id FK, type, detail, created_at)  -- 启停/异常/风控嫌疑/自动恢复

drop_events(id PK, miner_id FK, game, drop_name, reward, claimed_at)  -- 聚合自 /api/drops-history

webhook_settings(user_id, discord_drops_url, discord_points_url)      -- 转发写入 worker settings

plans / subscriptions                                                   -- Phase 3
```

### 6.2 核心 API 草案

```
# 认证
POST /api/v1/auth/register                 # 邮箱+密码 → JWT（httpOnly cookie）
POST /api/v1/auth/login  /logout

# 账号绑定
POST   /api/v1/accounts                    # 创建绑定 → 编排器起 worker → 返回 oauth/start 转发结果（Phase 2）
GET    /api/v1/accounts/{id}/oauth/status  # 转发 worker GET /api/oauth/status
DELETE /api/v1/accounts/{id}               # 解绑 = 停 worker + 删数据卷（二次确认 + 24h 宽限期）

# 实例控制（编排器代理到对应 worker，带 X-TDM-Token）
GET    /api/v1/miners/{id}/status          # 聚合 /api/status + /api/stats + 平台侧记录
POST   /api/v1/miners/{id}/pause | resume | reload
PUT    /api/v1/miners/{id}/games           # 写 games_to_watch（转发 POST /api/settings 部分更新）
GET    /api/v1/miners/{id}/drops           # 转发 /api/drops-history（或读平台 drop_events 缓存）

# 平台管理（admin）
GET    /api/v1/admin/miners                # 全局实例列表/健康
GET    /api/v1/admin/metrics               # 容量、成本、封号率等
```

### 6.3 编排器：槽位分配（简化版）

规模固定为 3，无需动态容器管理。预置 3 个槽位（docker-compose 静态服务 worker-1/2/3，端口 8080/8082/8084，数据卷 data1/data2/data3，`restart: unless-stopped`）。

```
slot(1..3): free → assigned(miner_id) → released
miner 状态: pending → provisioning → running ⇄ paused → stopping → stopped → error
```

- **分配**：用户创建绑定 → 取一个 free 槽位 → 轮换该槽位的 `TDM_API_TOKEN` / `WEB_PASSWORD`（控制平面生成、加密入库）并重启槽位容器 → 槽位置为 assigned。
- **释放**：用户解绑 → `pause` 保留 24h 宽限期 → 销毁该槽位数据卷 → 槽位回归 free。
- **健康检查**：每 30s `GET /healthz` + `/api/status`（带 token）；连续 3 次失败 → `docker restart` 该槽位；3 轮仍失败 → `error` + 告警。
- **风控嫌疑检测（Phase 2）**：worker 状态或日志出现 `captcha` / 登录失效关键字 → 自动暂停并通知用户重新绑定。
- **占满策略**：3 个槽位全占用时，新绑定请求进入排队（有槽位释放后自动分配并通知用户）。

### 6.4 部署模型

- **单机部署**：一台电脑（Windows/Linux 均可）跑控制平面（docker 或原生进程）+ 3 个 worker；worker 端口只绑 `127.0.0.1`，不暴露公网。
- **用户面板访问**：控制平面反代 `https://panel.<domain>/m/{miner_id}/ → http://127.0.0.1:{port}/`，代填 session cookie（worker 的密码中间件无需改动）。
- **家用环境注意**：若部署在家用宽带，需考虑动态 IP / 断电断网——建议 DDNS + 控制平面 `restart=always` + 断电自启；Phase 0 验证期间一并观察连续运行稳定性。

### 6.5 同 IP 风控管理（替代代理池）

当前阶段无代理池，3 个账号共享同一出口 IP：

- **硬配额**：全平台并发实例数 ≤ 3，超出的绑定请求排队（§6.3）。该阈值取自已方假设（3 账号同 IP 不易触发风控），**必须经 Phase 0 灰度实测验证**——若 72h 内出现验证码/强制登出，则降为 2 或引入代理。
- **监测**：编排器轮询 worker 状态中的 `captcha` / 登录失效信号（Phase 2 自动化，Phase 1 人工看管理后台）；任一账号出现风控信号时**暂停全部实例**并评估（同 IP 风控可能波及同机其他账号）。
- **未来扩展**：如需超过 3 账号，再引入住宅代理池（每 miner 独占代理；worker 已有 `/api/settings/verify-proxy` 可复用做健康检查，`app.py:1030`）。

### 6.6 配额与计费

- 配额：每用户默认 1 个绑定账号；全平台并发硬上限 3（§6.5）。
- 计费：暂不实施；3 人规模无成本压力，未来扩大再评估套餐（支付渠道另行决定）。

### 6.7 通知

- 复用 worker 自带 Discord webhook（drops/points 两类，`SettingsUpdate` 已含字段，`app.py:519-520`），控制平面把用户填写的 webhook 写入 worker settings。
- 平台侧邮件（Phase 2）：绑定成功、掉落领取、实例异常、代理失效。

---

## 7. 前端页面清单

| 页面 | 说明 | MVP |
|---|---|---|
| 首页 / 注册 / 登录 | 公共站 | ✓ |
| 用户面板 - 账号 | 绑定状态、游戏选择（转发 worker settings API）、实例开关 | ✓ |
| 用户面板 - 掉落 | 平台 `drop_events` + worker `/api/drops-history` | ✓ |
| 用户面板 - 高级 | 嵌入/链接进入 worker 原生 7-tab UI | ✓ |
| 管理后台 | 全局实例列表、代理池、告警、指标 | ✓（最简版） |
| 计费 / 套餐 | — | ✗（Phase 3） |

---

## 8. 安全设计

- 用户口令：argon2；会话 JWT（httpOnly + SameSite）。
- 对 worker 的全部调用走内网 + `X-TDM-Token`；fork 后收紧 CORS（现状 `app.py:415` 全开且允许 credentials）。
- Twitch 凭证：数据卷仅平台 root 可读；Phase 2 加 Fernet 落盘加密；解绑即销毁卷。
- 控制平面 API：注册/登录限流防爆破、输入校验、日志脱敏（借鉴 worker 已有 `_SECRET_LINE_RE`，`app.py:1145`）。
- 密钥管理：`.env` 起步，Phase 3 迁移 Secret Manager。

---

## 9. 分阶段实施计划

### Phase 0：预研与验证（1 周）

- [ ] 法务/风控评估：确认 Twitch ToS 立场、起草用户免责条款（**上线前置条件**，见 §11）
- [ ] 灰度验证：同机启动 **3 个 worker + 3 个测试 Twitch 账号、同一 IP（无代理）**，连续 72h，观察：掉落领取成功率、风控反应（验证码/登出）、单 worker 内存/流量实测（附录 B）
- [ ] 产出：**"3 账号同 IP 不触发风控"假设的验证结论**（不成立则降为 2 或引入代理）、单机资源占用数据

### Phase 1：MVP（4–6 周）

- W1–2：worker fork 改造（§5.2 剥离 + §5.3 token 中间件）；控制平面骨架（用户注册/登录、DB schema、Alembic）
- W3–4：槽位编排器（分配/释放/健康检查/自愈）；状态聚合与掉落入库
- W5：用户面板（引导式绑定 + 游戏选择 + 状态页）；管理后台最简版
- W6：联调 + 灰度（3 账号 72h）+ 修复
- **验收标准**：新用户 10 分钟内完成"注册 → 绑定 → 选游戏 → 开始挂机"；实例可用率 >99%；掉落自动领取且可追溯

### Phase 2：无缝体验与运营能力（3–4 周）

- [ ] 设备码登录 REST 化（§5.3.4），平台内一键绑定
- [ ] 凭证加密；风控/验证码嫌疑检测与自动暂停（同 IP 场景下发现风控信号即暂停全体并通知）
- [ ] 排队分配；邮件通知；管理后台完善
- **验收标准**：绑定全程停留平台内；任一账号触发风控信号时全平台自动暂停

### Phase 3：可选扩展（仅当用户规模超过 3）

- [ ] 代理池（每 miner 独占住宅代理）与动态扩容；多机调度
- [ ] 计费/支付；Prometheus/Grafana 监控告警；安全审计
- [ ] 上游 cherry-pick 例行化

---

## 10. 人力与工时估算

| 阶段 | 后端 | 前端 | 合计 |
|---|---|---|---|
| Phase 0 | 0.5–1 人周 | — | 0.5–1 人周 |
| Phase 1 | 3–4 人周 | 1.5–2 人周 | 4.5–6 人周 |
| Phase 2 | 2–3 人周 | 1 人周 | 3–4 人周 |
| **建议配置** | **1 名 Python 后端** | **1 名前端（可兼职）** | **MVP 约 1–1.5 个月（1–2 人）** |

> 注：槽位模型（§6.3）+ SQLite + 无代理池相比原方案显著降低了工作量（无 docker SDK 动态编排、无代理池服务）。

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **Twitch ToS / 风控**：多账号挂机 + 伪装 Android 客户端（`src/config/client_info.py`）；当前 3 账号共享同一 IP | 高（账号批量封禁、平台声誉受损） | Phase 0 法务评估 + "3 账号同 IP"72h 灰度实测；并发硬上限 3（不成立则降为 2 或引入代理）；节奏温和（默认限速参数）；用户协议免责 + 封号补偿政策；风控信号自动暂停全体；持续跟踪上游社区的风控经验 |
| Twitch 端 API 变动（内部 GQL 失效） | 高（全平台停摆） | fork 自维护 + 上游 cherry-pick；监控"掉落领取失败率"快速感知；保持与上游社区联系 |
| 用户 Twitch 凭证泄露 | 高 | 卷隔离 + 落盘加密（Phase 2）、解绑即销毁、访问审计日志 |
| 控制平面单点 | 中（编排停摆，挖矿不停） | worker 不依赖控制平面存活（设计保证）；控制平面自身 `restart=always` + 备份恢复演练 |
| 家用宽带断电/断网/动态 IP | 中（全平台不可用） | 断电自启、`restart=unless-stopped`、DDNS、离线告警 |
| 上游项目方向变化（本项目路线独立） | 低 | 平台只依赖其核心层，价值在编排与体验；必要时可切换 fork 基线 |

---

## 12. 关键指标（KPI）

- 实例可用率 ≥ 99%（healthz 计算）
- 掉落领取成功率 ≥ 95%（应领 / 实领）
- 绑定流程完成率 ≥ 80%
- 月封号率 < 2%（以 Phase 0 灰度实测校准）

---

## 附录 A：文件级复用对照表

| 现有文件 | 平台角色 | 改动 |
|---|---|---|
| `src/core/client.py`、`src/services/*` | worker 挖矿核心 | 不动 |
| `src/api/*`、`src/websocket/*`、`src/models/*` | Twitch 通信层 | 不动 |
| `src/auth/auth_state.py` | Twitch 登录 | Phase 2 拆分为可挂起状态机 |
| `src/config/settings.py`、`operations.py`、`client_info.py` | 设置 / GQL / 客户端伪装 | 不动（client_info 是风控风险点，见 §11） |
| `src/web/app.py` | worker 控制 API | 剥离 §5.2 端点；新增 token 中间件与 oauth REST 端点 |
| `src/web/gui_manager.py`、`web/*` | 用户高级面板 | 原样保留（内嵌复用） |
| `src/__main__.py` | worker 入口 | env 化（已有 TDM_PORT/TDM_DATA_DIR） |
| `scripts/manage_instance.sh` | — | 不用；`docker-compose.yml` 可参考改造为 3 槽位编排 |
| `lang/*`、`src/i18n/*` | UI 多语言 | 不动 |

## 附录 B：Phase 0 验证脚本要点（3 账号同 IP）

```bash
# worker 1（测试账号 A）—— 同机、同一出口 IP、无代理
docker run -d --name miner-1 --restart unless-stopped \
  -v miner-data-1:/app/data \
  -e TDM_PORT=8080 -e TDM_DATA_DIR=data -e TDM_LABEL=miner-1 \
  -e WEB_PASSWORD=$(openssl rand -hex 16) \
  -p 127.0.0.1:18081:8080 \
  ghcr.io/simpliaj/twitch-drops-miner:latest

# worker 2（测试账号 B，端口 18082 / 卷 miner-data-2）…… worker 3（测试账号 C，18083 / miner-data-3）
# Windows 主机可用 PowerShell 替代 openssl 生成随机密码
```

72 小时观察清单（核心目的：验证"3 账号同 IP 不触发风控"假设）：

- [ ] 掉落领取成功率（对照 Twitch 官方进度页）
- [ ] 是否出现验证码 / 强制登出 / 设备锁（**任一出现即假设不成立**）
- [ ] 单 worker 内存（`docker stats`）与出网流量（路由器/系统级统计）
- [ ] 3 个账号同时挖矿 vs 逐个启动的风控差异
- [ ] worker 进程崩溃后 `restart=unless-stopped` 自恢复效果
- [ ] 单机资源占用（3 worker + 未来控制平面的余量）
