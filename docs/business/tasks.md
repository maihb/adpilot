# 异步任务

**代码**：`db/broker.py`（Celery 接线、队列与死信队列、任务名常量）·
`tasks/app.py`（worker 入口与重试策略）· `tasks/runtime.py`（同步↔async 的桥）·
`tasks/normalize.py`（任务体）· `services/task.py`（投递与状态查询）·
`schemas/task.py` · `api/task.py`（路由）· `tests/test_tasks.py`

**范围出处**：[设计文档 §3–4](../design/2026-08-19-mvp-design.md) · 里程碑 D6–D8

**OpenAPI tag**：`tasks`

## 一句话职责

把「重、且没人需要盯着看」的活从请求里挪走，并且在它失败时**留下痕迹**。

**不负责**：业务逻辑（全在 `services/`，任务体只做编排）、定时调度（Celery beat
还没接，见「已知残余」）、把解析这类**要当场报错**的活挪走 —— 那种失败得趁上传的人
还盯着屏幕说。

## 接口

| operationId | 路径 | 说明 |
|---|---|---|
| `getTaskStatus` | `GET /api/tasks/{task_id}` | 查一个任务跑到哪了 |

任务**没有投递接口**：它是别的动作的副产物（导入完自动排一个归一化），不是一种能
被单独下单的资源。要手动重跑归一化，用 `POST /api/ad-accounts/{id}/normalize`（同步，
当场返回行数）。

## 能读文档就够的部分

| 规则 | 一句话 |
|---|---|
| 现在只有一个任务 | `adpilot.normalize_account`：把某账户的快照归一化进 `daily_metrics` |
| 谁投递它 | 导入接口（`POST /api/imports`），落完快照就排队，响应里回 `task_id` |
| `task_id` 可能是 `null` | 队列连不上。**快照已经落好了** —— 重新触发一次归一化即可，别重新导文件（那只会多一条快照） |
| 任务参数必须能 JSON 化 | 序列化器只认 json（不用 pickle：broker 里的一条消息不该能执行任意代码）。日期进出都是 ISO 字符串 |
| 任务可以反复跑 | 归一化按唯一键 upsert，重跑是覆盖。这是敢开 `acks_late` 的前提 |
| 重试 5 次，指数退避 | 2 秒起翻倍、封顶 5 分钟、带抖动。**抖动不是装饰**：没有它，一次数据库重启会让所有在途任务踩着同一个节拍回来，把刚起来的库再打趴一次 |
| 数据不对 → 不重试 | 领域异常（账户不存在、快照缺必需列）直接进死信队列 `adpilot.dead`。重试一万次它还是不对 |
| 重试用尽 → **不进**死信队列 | 状态和 traceback 留在 result backend 里，查 `GET /api/tasks/{id}` 或看 worker 日志。别只盯着死信队列 |
| `PENDING` 有两个意思 | 「排队中」和「查无此 ID」。result backend 只在任务**结束**时写记录，两者它分不出来 —— 所以这个接口不返回 404，那会是撒谎 |
| 前端轮询看 `ready` | 别比对 `state` 字面值，那是 Celery 的枚举，会随版本增减 |
| 失败只给异常**类名** | 不给原始报错：驱动的异常消息里可能带着 DSN。完整原因在 worker 日志里 |
| 任务结果留一天 | 它只回答「刚才那个任务成没成」，不是审计留痕（那是 Mongo 的快照和 PG 的事实） |

## ⛔ 必须读源码的部分

- **worker 里的连接池必须在 fork 之后建、事件循环必须复用同一个**
  （`tasks/runtime.py` 的模块 docstring）。两个坑都不会当场报错：前者让多个子进程
  共用同一条 TCP 连接、协议流错乱，后者让 asyncpg 报「attached to a different
  loop」，而且都**只在压力上来之后间歇性出现**。这个模块存在的全部理由就是这两件事。

- **不要写 `async for session in session_scope(...): return ...`**
  （`db/postgres.py` 的 `transaction`）。在 `async for` 里 return 会让生成器收到
  `GeneratorExit`，而它继承的是 `BaseException` —— `except Exception` 接不住，
  `commit()` 永远跑不到。症状是任务写的数据一行都没落地，而接口测试全绿（那条路径
  由 FastAPI 把生成器驱动到底）。任务里开事务一律用 `transaction`。

- **队列参数一旦声明就改不动**（`db/broker.py` 的模块 docstring）。
  `x-dead-letter-exchange` 是建队列时烧进去的，改了值再连同一台 RabbitMQ 会以
  `PRECONDITION_FAILED` 失败，而 worker 只会反复重连、看起来像「连不上」。

- **投递失败为什么不抛异常**（`services/task.py` 的 `enqueue_normalize`）。调用它的
  是导入接口，那时快照已经落进 Mongo 了 —— 报 500 会让人以为导入没成功，于是再导
  一次，于是多一条快照。

- **接口进程为什么按名字投递、不 import 任务函数**（`services/task.py` 的模块
  docstring）。代价是参数名写错没有编译期报错，所以任务名是常量、参数在那几个函数里
  收口，调用方不自己拼 kwargs。

## 已知残余

- **没有定时任务。** Celery beat 还没接，所以「每天自动拉数」「定时巡检告警」都还
  不存在。D7–D8 接规则引擎时一起做，入口会是 `tasks/` 下新的一个模块 +
  `db/broker.py` 里的 `beat_schedule`。
- **导入本身仍是同步的。** 解析走请求线程（丢进了线程池），只有归一化异步化了。
  理由写在 `services/imports.py` 里那段注释：文件内容得跟着消息走一遍 broker，而且
  解析失败恰恰是要当场说的那类错误。真要处理大文件，正确做法是先落对象存储、消息里
  只带一个键，不是放宽上传上限。
- **死信队列没有消费者，也没有告警。** 消息进去就躺着，得有人用 RabbitMQ 管理界面
  （`http://localhost:15672`）去看。等 D8 的告警链路做出来之后，「死信队列里有东西」
  本身应该成为一条告警。
- **没有任务列表接口。** 只能按 ID 查。要看「最近跑了些什么」得看 worker 日志 ——
  真需要的时候，那意味着要建一张任务记录表，而不是去翻 result backend。
