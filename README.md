# 告警值班控制台（FastAPI + SQLite + Docker Compose）

值班工程师维护服务时暂停新故障通知，同时保证通知端不会留下"已恢复但没通知"的故障。
系统由两个容器组成：

| 服务 | 端口 | 职责 |
| --- | --- | --- |
| `alert-app` | 8000 | 告警控制台：事件接入、静默管理、告警/通知状态查询、发件箱 worker |
| `notifier` | 8001 | 本地通知端：接收通知、模拟丢回执、按通知号去重、展示最终状态 |

## 快速启动

```bash
docker compose up --build
# 控制台:   http://localhost:8000/
# 通知端:   http://localhost:8001/
```

`LOSE_FIRST_N=1` 表示每个**新通知号**的首次投递返回 HTTP 500（回执丢失，消息已落库），
应用随后用**同一个通知号**重试，通知端去重后返回成功。设为 0 可关闭模拟。

无 Docker/PyPI 的环境可用标准库 runner 启动（业务逻辑与 FastAPI 版本完全共用 `common/`）：

```bash
ALERT_PORT=8000 NOTIFIER_PORT=8001 \
NOTIFIER_URL=http://127.0.0.1:8001 \
ALERT_DB_PATH=./data/alerts.db NOTIFIER_DB_PATH=./data/notifier.db \
python3 -I runner/__main__.py
```

## 事件接入 API

`POST /api/events`

```json
{ "fingerprint": "hostA/disk", "seq": 1, "kind": "firing", "summary": "disk 92%" }
```

- `kind`：`firing`（触发）或 `recovery`（恢复）
- `(fingerprint, seq)` 唯一；`seq` 在同指纹内严格递增

### 幂等与顺序规则

| 场景 | 行为 |
| --- | --- |
| 同指纹同序同文重试 | 返回 `duplicate_replayed`，沿用原结果，不产生新通知 |
| 同指纹同序异文 | HTTP 409 `conflicting_event`，拒绝 |
| 旧序号（`seq <= current_seq`） | HTTP 409 `stale_event`，当前状态不被改写 |
| 无告警时的恢复 | HTTP 409 `stale_event` |
| 恢复后再次触发 | 轮次（round）+1，视为新一轮，重新走触发/恢复通知 |

## 静默规则

- `POST /api/silence`，body：`{"duration_seconds": 60, "reason": "维护"}`
  （也支持绝对时间 `until_ts`）；`DELETE /api/silence` 立即解除；`GET /api/silence` 查询。
- 静默期内的新触发**只更新告警**（序号/摘要），不产生通知。
- **此前已通知故障的恢复不受静默影响**，恢复通知照常入发件箱。
- 静默到期（或手动解除）后，worker 只补发**仍在触发且本轮从未通知**的故障；
  静默期内已经恢复的轮次保持静默，不会补发。

## 发布门禁（放行票据）

发布工程师上线前在控制台「发布门禁」卡片勾选本次涉及的告警指纹并签发票据；
部署作业携带票据号与作业 ID 调用确认 API 获取放行结论。

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/gate/tickets` | 签发票据，body：`{"fingerprints": ["svc/a", "svc/b"]}`，成功返回 201 |
| `GET /api/gate/tickets` | 票据列表（open 票据附带当前有效性预览 `valid_now`/`current_reasons`） |
| `GET /api/gate/tickets/{ticket_no}` | 单票据状态（stdlib runner 用 `?ticket_no=` 查询参数） |
| `POST /api/gate/confirm` | 部署确认，body：`{"ticket_no": "GT-000001", "job_id": "deploy-001"}` |

规则：

- **签发条件**：所选指纹全部存在且已恢复（`recovered`），且当前未处于全局静默；
  否则 409 并返回逐条原因。签发时快照每个指纹的 `(序号, 轮次)` 基线与当前静默纪元。
- **确认时失效**：任一指纹的序号或轮次相对基线发生变化，或签发后曾设置、解除过
  静默（以静默纪元计数，与静默写操作同一事务内递增），票据即失效，结论为 `rejected`。
- **结论固定**：首次确认把放行/拒绝结论与作业 ID 一起持久化；同作业重试原样返回
  （`replayed: true`），异作业确认返回 409 `job_mismatch`，不得复用。
- **一致性**：确认的「读状态 + 写结论」在单个写事务内完成，与事件接入、静默变更
  互为原子；确认后的新故障不改写已固定的结论。
- **持久化**：票据与结论存于 SQLite（`gate_tickets`/`gate_state` 表），重启后可查可续用。
- 确认响应：`{"decision": "released"|"rejected", "reasons": [...], "replayed": bool, ...}`；
  未知票据 404，载荷非法 422。

## 通知投递与去重

- 通知号格式：`{fingerprint}#r{round}:{kind}`，同一轮的 firing/recovery 各一个稳定号码，
  重试永远复用该号码。
- 发件箱表（`notifications`）状态机：`pending → in_transit → delivered`，
  失败按 `attempts` 退避重试（1s 起，上限 30s）。
- **同指纹严格按状态顺序送达**：前一条未 delivered，后一条不会发出。
- 通知端以通知号为主键去重：首次见到的号码落库；同号重试返回 `duplicate: true`，
  只累加 `attempts_seen`；同号异文返回 409。
- 两个 SQLite 库都挂载在 Docker volume，重启后发件箱继续投递、通知端记录保留，
  不漏发、不重复；迟到的旧序号事件无法覆盖恢复后的状态。

## 其他 API

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/alerts` | 告警列表（含当前轮 firing/recovery 通知状态）+ 当前静默状态 |
| `GET /api/alerts/{fingerprint}/events` | 单指纹事件历史 |
| `GET /api/notifications` | 发件箱明细（状态、尝试次数、错误、入队/送达时间） |
| `GET /api/notifications`（通知端 8001） | 通知端收到的最终状态 |
| `GET /health` | 健康检查 |

## 目录结构

```
common/        # 框架无关的核心逻辑（事件、静默、发件箱、发布门禁、通知端去重）+ SQLite schema
app/           # FastAPI 告警控制台（后台 worker 线程）
notifier/      # FastAPI 本地通知端（丢回执模拟）
runner/        # 纯标准库 HTTP runner，复用 common，供无 PyPI 环境运行与自测
web/           # 两个网页（原生 HTML/CSS/JS，2s 自动刷新；控制台含发布门禁卡片）
tests/e2e.py   # 端到端关键链路自测（真实 HTTP + SQLite，无第三方依赖）
Dockerfile / Dockerfile.notifier / docker-compose.yml
```

## 自测

```bash
python3 -I tests/e2e.py
```

覆盖：健康检查与页面/静态资源、幂等/冲突/旧序拒绝、触发恢复全链路、丢回执同号重试、
静默期抑制触发但恢复照发、静默到期仅补发仍触发告警、恢复后再触发新一轮、
同指纹顺序投递、进程重启持久化不重不漏、非法载荷校验；
发布门禁：签发前置条件（未恢复/未知指纹/静默中/空选择）、确认放行与作业绑定、
序号轮次漂移与静默操作致票据失效、并发首确认原子唯一、同作业重试与异作业拒绝、
重启后票据与结论可查可续用。
