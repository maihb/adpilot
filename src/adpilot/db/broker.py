"""RabbitMQ 接线：Celery 应用的构造，以及队列与死信队列的声明。

**为什么 Celery 的接线在 `db/` 而不是 `tasks/`。** 消息总线跟 PostgreSQL、Mongo、
Redis 是同一类东西 —— 一个外部系统的连接。摆在这一层，`resources.py` 才能像持有
另外三个客户端那样持有它，接口进程也就能在**不 import 任何任务代码**的前提下投递
任务（见下面「按名字投递」）。`tasks/` 那一层放的是任务体，它 import 这里，反过来
不行。

## 为什么选 RabbitMQ 而不是 Redis 做 broker

[设计文档第四节](../../../docs/design/2026-08-19-mvp-design.md)写的理由是「需要真正
的 ack 和死信队列」。落到这个文件里就是两件事：

* **`acks_late`**：worker 是**跑完**才 ack，不是取到就 ack。进程被 OOM kill 掉时
  消息回到队列，换个 worker 重来 —— 拉数是长任务，「任务丢了等于数据缺一天」。
* **死信队列**：判定为「重试也没用」的消息不是丢掉，而是被 reject 进 `adpilot.dead`
  躺着，等人来看。Redis broker 这两件事都做不了。

## 🔴 队列参数一旦声明就改不动了

`x-dead-letter-exchange` 这类参数是**建队列时**烧进去的。改了这里的值再去连同一台
RabbitMQ，声明会以 `PRECONDITION_FAILED - inequivalent arg` 失败，而 worker 只会
反复重连、看起来像「连不上」。真要改：先把旧队列删掉（确认里面没有消息），或者换
一个队列名。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from adpilot.config import Settings

# 任务名。**接口进程按名字投递，不 import 任务函数**（`Celery.send_task`），所以
# 这个字符串是生产者与消费者之间唯一的契约 —— 改了它，队列里在途的老消息就再也
# 没人认领。名字定在这里而不是任务模块里，正是为了让生产者够得着它。
TASK_NORMALIZE_ACCOUNT: Final = "adpilot.normalize_account"
TASK_SWEEP_ALERTS: Final = "adpilot.sweep_alerts"
TASK_GENERATE_DUE_REPORTS: Final = "adpilot.generate_due_reports"
TASK_FETCH_DUE_ACCOUNTS: Final = "adpilot.fetch_due_accounts"

# 巡检的排期。**每小时一次，不是每天一次。**
#
# 余额可撑天数低于 1 天的账户是存在的（大促期间日耗翻几倍），一天扫一次可能整个
# 错过它归零的那个窗口 —— 而错过的代价是学习期重置，远大于多跑 23 次巡检。
#
# 敢这么密是因为两件事：巡检本身很轻（几个账户、两条 SQL），以及告警是**状态机**
# —— 同一件事只有一条 open，只在新开时推送，所以密集巡检不会变成密集打扰。
SWEEP_SCHEDULE_MINUTE: Final = 0

#: 定时日报的排期。**同样每小时一次，而且理由和上面不一样。**
#:
#: 账户时区各不相同，日切点散布在一天里的各个整点上（本项目真实案例里就有
#: Los_Angeles、Anchorage、Shanghai、Berlin 四个）。一天扫一次的话，某些时区的
#: 账户要多等二十几个小时才拿到日报 —— 而日报的价值几乎全在及时性上。
#:
#: 敢这么密的理由和巡检不同：巡检是「跑一遍规则」，这个是「**可能**生成几份日报，
#: 而每份都要花一次 LLM 调用」。密而不贵靠的是判定本身 —— 那天已经有日报就跳过，
#: 所以绝大多数轮次什么都不做（`services/report.py` 的 `due_reports`）。
#:
#: 错开 20 分是为了不和巡检抢同一分钟：两个任务都会在整点被投出来，而巡检的结果
#: （新开的告警）正是日报要引用的东西 —— 让它先跑完。
REPORTS_SCHEDULE_MINUTE: Final = 20

#: 自动拉取的排期。**第三个整点任务，而这个数字不是随手挑的。**
#:
#: 三个任务在一小时里是这样接起来的：
#:
#:     :40 拉取 ──20分钟──▶ :00 巡检（用刚拉到的数判告警）──20分钟──▶ :20 日报
#:
#: 20 分钟是留给拉取跑完的余量 —— 多账户 × 多层级 × 退避重试，比两条 SQL 慢得
#: 多。放在 :50 会让数据更新鲜，但拉取一慢就会和巡检抢在同一时刻，而那时谁先跑
#: 完取决于 worker 取消息的顺序，正是上面那条排期规则要避免的情形。
#:
#: 密而不贵和日报同理，靠的是判定本身：**那天已经成功拉过就跳过**
#: （`services/fetch.py` 的 `due_accounts`），所以绝大多数轮次什么都不做。
#: 差别是拉取**不花钱**，所以它没有「撞额度就整轮中断」那条 —— 一个账户的
#: token 失效不该让其它账户跟着停更。
FETCH_SCHEDULE_MINUTE: Final = 40

MAIN_QUEUE: Final = "adpilot"
DEAD_LETTER_QUEUE: Final = "adpilot.dead"
DEAD_LETTER_EXCHANGE: Final = "adpilot.dlx"

# 连不上 broker 时等多久放弃。压短的理由和 Mongo 那个
# `serverSelectionTimeoutMS` 一样：就绪探针要快速报出「连不上」，而不是干等
# 默认的 4 秒 × 若干次重试。
BROKER_CONNECT_TIMEOUT_SECONDS: Final = 2


def build_queues() -> tuple[Queue, ...]:
    """业务队列 + 死信队列。

    死信队列**不能靠 RabbitMQ 自己长出来** —— `x-dead-letter-exchange` 只是说「被
    拒的消息投到这个 exchange」，那个 exchange 后面没有队列绑着的话，消息照样消失。
    所以两个队列必须一起声明。
    """
    dead_letter = Exchange(DEAD_LETTER_EXCHANGE, type="direct", durable=True)
    return (
        Queue(
            MAIN_QUEUE,
            Exchange(MAIN_QUEUE, type="direct", durable=True),
            routing_key=MAIN_QUEUE,
            queue_arguments={
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
                "x-dead-letter-routing-key": DEAD_LETTER_QUEUE,
            },
        ),
        Queue(DEAD_LETTER_QUEUE, dead_letter, routing_key=DEAD_LETTER_QUEUE),
    )


def create_celery_app(settings: Settings, *, include: Sequence[str] = ()) -> Celery:
    """构造 Celery 应用。

    `include` 是**任务模块的名字**（字符串），worker 启动时按名 import 它们来注册
    任务。这里收字符串而不是模块对象，是为了让这一层不依赖 `tasks/` —— 分层契约要
    的是「低层不认识高层」，而字符串不是 import。

    和另外三个客户端一样，构造不等于连上去：Celery 的 broker 连接是懒的，第一次
    投递或第一次消费才建。
    """
    app = Celery(settings.app_name, broker=settings.celery_broker_url, include=list(include))
    app.conf.update(
        result_backend=settings.celery_result_backend,
        # 结果留一天。**任务结果不是审计留痕**（那是 Mongo 里的快照和 PG 里的事实），
        # 它只回答「刚才那个任务成没成」，过了一天没人再问。不设过期的话，Redis 会
        # 被跑过的每一个任务的元数据慢慢撑满。
        result_expires=24 * 60 * 60,
        task_queues=build_queues(),
        task_default_queue=MAIN_QUEUE,
        task_default_exchange=MAIN_QUEUE,
        task_default_routing_key=MAIN_QUEUE,
        # 只认 JSON。pickle 能让 broker 里的一条消息在 worker 里执行任意代码，
        # 而 broker 是整个系统里最容易被别的服务共用的那个组件。
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # 时间一律 UTC。任务的 eta / countdown 要是跟着机器本地时区走，worker 和
        # 接口部署在不同时区的机器上时，定时任务会差出好几个小时。
        # 注意这跟 `stat_date` 的口径是两回事：那个是账户时区下的自然日。
        timezone="UTC",
        enable_utc=True,
        # 跑完才 ack，配合下面一条：worker 被 kill 掉时消息回队列而不是消失。
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # 一次只预取一条。默认是 4，对「几秒钟一条」的短任务是对的，对归一化这种
        # 几十秒的长任务是错的 —— 一个 worker 会把四条消息扣在手里，另一个空转。
        worker_prefetch_multiplier=1,
        broker_connection_timeout=BROKER_CONNECT_TIMEOUT_SECONDS,
        # 投递失败时重试。接口进程投递的那一下要是碰上 RabbitMQ 正在重启，重试
        # 几次比直接让导入接口报错强得多。
        broker_connection_retry_on_startup=True,
        task_publish_retry=True,
        # 🔴 关掉远程控制 —— **不是嫌它多余，是 RabbitMQ 4 已经不让它跑了。**
        #
        # Celery 的远程控制（`celery inspect` / `celery control`）靠 kombu 的
        # pidbox，而 pidbox 的应答队列是「非持久 + 非独占」的。RabbitMQ 4 把这类
        # 队列列成了废弃特性（`transient_nonexcl_queues`）并默认拒绝声明，于是
        # worker 一启动就 `INTERNAL_ERROR (541)`、疯狂重连，最后死在
        # `RestartFreqExceeded` 上。报错里只字不提「远程控制」。
        #
        # 代价说清楚：**`celery inspect` / `celery control` / `celery events` 都用
        # 不了了**，看 worker 在干什么只能看日志。让 RabbitMQ 重新放行那个特性也能
        # 修（`deprecated_features.permit.transient_nonexcl_queues`），但那是把问题
        # 推给下一个大版本 —— 它迟早会被彻底删掉。
        #
        # ⚠️ 这一条挡不住 `mingle` 和 `gossip`：那两个不看配置，只认命令行的
        # `--without-mingle` / `--without-gossip`，而它们同样会去碰 pidbox。所以
        # **启动命令上那两个参数一个都不能少**，`tests/test_tasks.py` 里有门禁盯着。
        worker_enable_remote_control=False,
        # 定时排期。**要有一个 beat 进程在跑它才会发生**（`make beat`，或 compose
        # 里的 beat 服务）—— 忘了起 beat 的症状是「告警一条都不来」，而那看起来
        # 跟「一切正常」一模一样。
        beat_schedule={
            "sweep-alerts-hourly": {
                "task": TASK_SWEEP_ALERTS,
                "schedule": crontab(minute=SWEEP_SCHEDULE_MINUTE),
                # 排期投出来的消息也要落在业务队列上，否则它会去默认的 celery
                # 队列，而 worker 只消费 adpilot —— 消息进去了，没人取。
                "options": {"queue": MAIN_QUEUE},
            },
            "generate-due-reports-hourly": {
                "task": TASK_GENERATE_DUE_REPORTS,
                "schedule": crontab(minute=REPORTS_SCHEDULE_MINUTE),
                "options": {"queue": MAIN_QUEUE},
            },
            "fetch-due-accounts-hourly": {
                "task": TASK_FETCH_DUE_ACCOUNTS,
                "schedule": crontab(minute=FETCH_SCHEDULE_MINUTE),
                "options": {"queue": MAIN_QUEUE},
            },
        },
    )
    return app


def declare_queues(app: Celery) -> None:
    """把两个队列（和死信 exchange）声明出来。

    🔴 **没有这一步，被拒的消息会无声无息地消失。** 直觉上「配置里写了 `task_queues`
    就都会建出来」是错的：worker 带 `-Q adpilot` 启动时，Celery 只声明它**要消费**
    的那个队列；生产者投递时也只声明目标队列。于是 `adpilot.dead` 从来没人建，
    `x-dead-letter-exchange` 指向的 exchange 后面空无一物，reject 掉的消息就地蒸发
    —— 而两端的日志都显示一切正常（reject 本身是成功的）。

    这个坑是跑真链路才发现的：任务确实被 reject 了，死信队列的深度却始终是 0。

    ⚠️ 同步阻塞调用，只在 worker 启动时调一次。
    """
    with app.connection_for_write() as connection:
        channel = connection.channel()
        for queue in build_queues():
            # declare() 会把 exchange、queue、binding 三样一起建出来。
            queue(channel).declare()


def check_connection(app: Celery) -> None:
    """探一次 broker 是否可达；连不上就抛异常。

    ⚠️ **这是同步阻塞调用**（kombu 没有 async 接口），在事件循环里调必须走
    `asyncio.to_thread` —— 就绪探针就是这么用的。`max_retries=0` 是关键：默认会
    重试若干次并指数退避，探针要的是「现在这一下通不通」。
    """
    with app.connection_for_write() as connection:
        connection.ensure_connection(max_retries=0, timeout=BROKER_CONNECT_TIMEOUT_SECONDS)
