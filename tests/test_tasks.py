"""异步任务：接线、重试策略、投递与状态查询。

**分工说明**（不写清楚的话，下一个人会以为覆盖有漏）：

* **接线和策略**（队列参数、`acks_late`、重试与死信的分流）在这里用单元测试盯着
  —— 它们是配置，配置错了不会有人发现，直到某天一条消息悄悄消失。
* **投递与状态查询**用 kombu 的 `memory://` 传输和 `cache+memory://` 结果后端，
  纯内存、不碰任何外部服务，但走的是真实的序列化与路由代码。
* **任务体的编排**（异常怎么分流、返回值怎么整形）把 `runtime.run` 换掉来测 ——
  它背后是连接池和事件循环，那两样在测试进程里没有意义。
* **归一化本身的业务逻辑**不在这里，它由 `test_metrics_api.py` 的集成用例覆盖
  —— 任务和接口调的是同一个服务函数，验一遍就够了。
* **真 RabbitMQ 那一段**只有一条集成用例：发一条、自己收回来。它验的是队列声明
  （含死信参数）在真 broker 上通得过 —— 这件事只有真连上去才知道。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from amqp.exceptions import NotFound
from celery import Celery
from celery.exceptions import Reject
from kombu import Connection

from adpilot.config import Settings
from adpilot.db import broker
from adpilot.db.broker import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_QUEUE,
    MAIN_QUEUE,
    TASK_NORMALIZE_ACCOUNT,
    TASK_SWEEP_ALERTS,
)
from adpilot.services import task as task_service
from adpilot.services.exceptions import InvalidDataError
from adpilot.tasks import normalize as normalize_task
from adpilot.tasks import runtime
from adpilot.tasks.app import AdpilotTask, celery_app

# 锚在文件位置上而不是当前目录 —— 从别的目录跑 pytest 时，相对路径会读不到文件，
# 而报错是「文件不存在」，跟这条用例要验的事情毫无关系。约定同 test_business_docs.py。
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def in_memory_celery(offline_settings: Settings) -> Celery:
    """完全跑在进程内存里的 Celery app：不连 RabbitMQ，也不连 Redis。"""
    app = broker.create_celery_app(offline_settings)
    app.conf.update(broker_url="memory://", result_backend="cache+memory://")
    return app


# --- 接线与策略 -------------------------------------------------------------


def test_worker_is_told_to_ack_only_after_the_task_finishes() -> None:
    """🔴 `acks_late` 是「任务丢了等于数据缺一天」的那道保险。

    默认是取到就 ack，于是 worker 被 OOM kill 掉时那条消息已经不在队列里了 ——
    而这正是最需要它还在的时候。配套的 `reject_on_worker_lost` 决定被 kill 之后
    消息是回队列还是算失败。
    """
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_only_json_crosses_the_broker() -> None:
    """pickle 能让 broker 里的一条消息在 worker 里执行任意代码。"""
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_long_tasks_are_not_hoarded_by_one_worker() -> None:
    """默认预取 4 条，对几十秒的归一化任务来说是让一个 worker 扣着活、另一个空转。"""
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_main_queue_dead_letters_into_a_queue_that_actually_exists() -> None:
    """🔴 `x-dead-letter-exchange` 后面没有队列绑着的话，被拒的消息照样消失。

    所以两个队列必须**一起**声明。只声明主队列的话，一切看起来都对：参数在、
    消息也确实被 reject 了，然后无声无息地没了。
    """
    queues = {queue.name: queue for queue in broker.build_queues()}

    assert set(queues) == {MAIN_QUEUE, DEAD_LETTER_QUEUE}
    assert queues[MAIN_QUEUE].queue_arguments == {
        "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
        "x-dead-letter-routing-key": DEAD_LETTER_QUEUE,
    }
    assert queues[DEAD_LETTER_QUEUE].exchange.name == DEAD_LETTER_EXCHANGE


def test_reject_is_excluded_from_automatic_retry() -> None:
    """🔴 `Reject` 也是 `Exception` 的子类。

    不把它排除掉的话，`autoretry_for=(Exception,)` 会把「判定为不该重试」的那条
    路径整个捞回去重试 —— 死信队列于是永远收不到东西，而配置看起来完全正确。
    """
    assert Exception in AdpilotTask.autoretry_for
    assert Reject in AdpilotTask.dont_autoretry_for


def test_retries_back_off_with_jitter() -> None:
    """没有抖动的话，一次数据库重启会让所有在途任务踩着同一个节拍一起回来。"""
    assert AdpilotTask.retry_backoff == 2
    assert AdpilotTask.retry_jitter is True
    assert AdpilotTask.max_retries == 5


def test_remote_control_is_off() -> None:
    """🔴 不是嫌它多余 —— RabbitMQ 4 已经不让它跑了。

    pidbox 的应答队列是「非持久 + 非独占」的，而 RabbitMQ 4 把这类队列列成废弃
    特性并默认拒绝声明。开着的话 worker 一启动就 `INTERNAL_ERROR (541)`、疯狂重连，
    最后死在 `RestartFreqExceeded` 上，而报错里只字不提「远程控制」。
    """
    assert celery_app.conf.worker_enable_remote_control is False


@pytest.mark.parametrize("path", ["Makefile", "docker-compose.yml"])
def test_worker_command_carries_the_flags_that_config_cannot(path: str) -> None:
    """🔴 `mingle` 和 `gossip` 不看配置，只认命令行开关。

    它们和远程控制踩的是同一个坑（非持久非独占队列 → RabbitMQ 4 拒绝声明），但
    `worker_enable_remote_control=False` 管不到它们，Celery 也没给配置项。于是这
    两个参数只能写在每一条启动命令上 —— 而「命令行参数被谁顺手删了」正是 review
    看不出来的那类改动：本地起得来（旧版 RabbitMQ 还放行），别人的机器上起不来。

    `-Q adpilot` 一起盯着：漏了它 worker 会去消费默认的 `celery` 队列，然后安静
    地跑着、一条消息都不处理 —— 连报错都没有。
    """
    command = (REPO_ROOT / path).read_text(encoding="utf-8")

    assert "--without-mingle" in command
    assert "--without-gossip" in command
    assert MAIN_QUEUE in command


@pytest.mark.parametrize("service", ["worker", "beat"])
def test_processes_without_a_port_disable_the_inherited_healthcheck(service: str) -> None:
    """🔴 不监听端口的服务必须**显式**关掉镜像自带的 healthcheck。

    `HEALTHCHECK` 写在 Dockerfile 里，是**镜像级**的，而 api / worker / beat 共用
    同一个镜像（那是刻意的：镜像分家只会让「代码更新了但 worker 还是旧的」变成
    一类新故障）。于是不禁用的话，worker 会去探自己根本没监听的 8000 端口，
    `docker compose ps` 里永远挂着 unhealthy。

    这不只是难看：**一个恒为红的健康灯会让人对整列输出脱敏**，于是 api 真的红了
    那一次也不会有人注意到。compose 里「刻意没有 healthcheck」那句注释在这条
    测试出现之前，是一句不成立的话 —— 光是「不写」不够，继承会补上。
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"][service]["healthcheck"] == {"disable": True}


@pytest.mark.parametrize("path", ["Makefile", "docker-compose.yml"])
def test_beat_has_a_way_to_be_started(path: str) -> None:
    """🔴 排期任务要有一个 beat 进程在跑才会发生。

    只起 worker 的症状是「告警一条都不来」—— 而那和「一切正常、最近没什么问题」
    长得一模一样，没有任何报错提示你少起了一个进程。所以「有没有 beat 的启动方式」
    这件事得有机器盯着：删掉那个 target / 那个服务，这条会红。
    """
    content = (REPO_ROOT / path).read_text(encoding="utf-8")

    assert "beat" in content


def test_the_sweep_is_scheduled() -> None:
    """排期本身也钉一条：巡检任务必须真的在 `beat_schedule` 里，且投到业务队列。

    投错队列的话消息会进默认的 `celery` 队列，而 worker 只消费 `adpilot` ——
    消息进去了，没人取，同样是「一条告警都不来」。
    """
    schedule = celery_app.conf.beat_schedule
    entries = [entry for entry in schedule.values() if entry["task"] == TASK_SWEEP_ALERTS]

    assert len(entries) == 1
    assert entries[0]["options"]["queue"] == MAIN_QUEUE


def test_every_task_name_is_registered_by_the_modules_the_worker_loads() -> None:
    """🔴 worker 只 import `TASK_MODULES` 里列的那几个模块。

    漏一行的症状很安静：任务代码本身完全正常、测试也能直接调它，只有队列里那条
    消息会被 worker 以「Received unregistered task」拒掉 —— 而那发生在另一个进程
    的日志里。

    所以这里**按 worker 的方式加载**（`import_default_modules`），再核对每个任务名
    常量都真的注册上了。直接 `import adpilot.tasks.alerts` 来测就没有意义了：那验的
    是「这个模块能 import」，不是「worker 会 import 它」。
    """
    celery_app.loader.import_default_modules()

    assert TASK_NORMALIZE_ACCOUNT in celery_app.tasks
    assert TASK_SWEEP_ALERTS in celery_app.tasks
    assert normalize_task.normalize_account.name == TASK_NORMALIZE_ACCOUNT


# --- 投递 -------------------------------------------------------------------


async def test_enqueue_publishes_the_task_name_and_arguments(in_memory_celery: Celery) -> None:
    """投出去的消息要能被 worker 认出来：任务名对、参数名对。

    按名字投递换来的是生产者与消费者解耦，代价是**参数名写错没有编译期报错**。
    所以这条用例是在替编译器干活。
    """
    task_id = await task_service.enqueue_normalize(in_memory_celery, account_id=7)

    assert task_id is not None
    body = _drain_one(in_memory_celery)
    assert body["task"] == TASK_NORMALIZE_ACCOUNT
    assert body["kwargs"] == {"account_id": 7, "stat_date": None}


async def test_dates_cross_the_broker_as_iso_strings(in_memory_celery: Celery) -> None:
    """JSON 序列化器不认识 `date`。

    传对象进去的话，失败发生在**接口进程**投递的那一刻，报错看起来跟归一化任务
    毫无关系。所以日期在这一层就转成字符串。
    """
    from datetime import date

    await task_service.enqueue_normalize(
        in_memory_celery, account_id=7, stat_date=date(2026, 8, 18)
    )

    assert _drain_one(in_memory_celery)["kwargs"]["stat_date"] == "2026-08-18"


async def test_enqueue_failure_does_not_blow_up_the_caller(offline_settings: Settings) -> None:
    """🔴 投不进去只能返回 `None`，不能抛，而且要**快**。

    不能抛：调用它的是导入接口，而那时快照**已经落进 Mongo 了**。让整个请求以 500
    收场，人会以为导入没成功、再导一次，于是白白多一条快照。

    要快：Celery 默认的连接池会一路重试到 100 次，实测这一下卡 19 秒 —— 导入接口
    就成了「转圈等 19 秒，然后告诉你其实成功了」。上限卡在 `services/task.py` 的
    `_CONNECT_POLICY`，这里的 5 秒是给慢机器留的余量，不是目标值（实测 0.3 秒）。
    """
    unreachable = broker.create_celery_app(offline_settings)
    started = time.monotonic()
    try:
        assert await task_service.enqueue_normalize(unreachable, account_id=7) is None
    finally:
        unreachable.close()

    assert time.monotonic() - started < 5


# --- 状态查询 ---------------------------------------------------------------


async def test_unknown_task_id_reports_pending_rather_than_an_error(
    in_memory_celery: Celery,
) -> None:
    """⚠️ result backend 只在任务**结束**时写记录。

    所以「排队中」和「查无此 ID」对它是同一种状态。返回 404 会让一个刚投出去、
    还没被取走的任务显示成「不存在」—— 那是在替 Celery 撒谎。
    """
    status = await task_service.get_status(in_memory_celery, "没有这个-id")

    assert status.state == "PENDING"
    assert status.ready is False
    assert status.result is None
    assert status.error is None


async def test_failed_task_reports_only_the_exception_class(in_memory_celery: Celery) -> None:
    """驱动的报错信息里可能带着 DSN，所以只回类名 —— 和就绪探针同一条规矩。"""
    in_memory_celery.backend.mark_as_failure("task-42", ValueError("password=hunter2"))

    status = await task_service.get_status(in_memory_celery, "task-42")

    assert status.state == "FAILURE"
    assert status.ready is True
    assert status.error == "ValueError"
    assert "hunter2" not in json.dumps(status.error)


async def test_successful_task_hands_back_its_payload(in_memory_celery: Celery) -> None:
    in_memory_celery.backend.mark_as_done("task-7", {"rows": 12})

    status = await task_service.get_status(in_memory_celery, "task-7")

    assert status.ready is True
    assert status.result == {"rows": 12}


# --- 任务体的编排 -----------------------------------------------------------


def test_bad_data_goes_to_the_dead_letter_queue_instead_of_being_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 领域异常重试一万次还是同一个错。

    退避重试只对瞬时故障有意义；数据本身不对时它只会把真正的原因埋进五条一模一样
    的报错里。这类消息要被 reject 到死信队列，等人来看。
    """

    def boom(job: object) -> None:
        raise InvalidDataError("快照里找不到对象 ID 列")

    monkeypatch.setattr(runtime, "run", boom)

    with pytest.raises(Reject):
        normalize_task.normalize_account(account_id=7)


def test_a_malformed_date_is_rejected_without_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投递方给了个不是日期的字符串 —— 重试一万次还是同一个字符串。"""

    def never_called(job: object) -> None:
        raise AssertionError("参数都没解出来，不该碰数据库")

    monkeypatch.setattr(runtime, "run", never_called)

    with pytest.raises(Reject):
        normalize_task.normalize_account(account_id=7, stat_date="八月十八")


def test_result_is_json_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回值要进 result backend，`date` 对象到那一步才会炸 —— 离现场很远。"""
    from datetime import date

    from adpilot.services.normalize import NormalizeSummary

    summary = NormalizeSummary(
        account_id=7,
        days=[date(2026, 8, 18)],
        rows=12,
        snapshots=1,
        skipped_rows=0,
    )
    monkeypatch.setattr(runtime, "run", lambda job: summary)

    result = normalize_task.normalize_account(account_id=7)

    assert result == {
        "account_id": 7,
        "days": ["2026-08-18"],
        "rows": 12,
        "snapshots": 1,
        "skipped_rows": 0,
    }
    json.dumps(result)  # 真序列化一次，别只是看着像


# --- 真 broker --------------------------------------------------------------


@pytest.mark.integration
def test_a_real_broker_gets_both_queues_including_the_dead_letter_one(
    live_settings: Settings,
) -> None:
    """🔴 `adpilot.dead` 必须真的被建出来，不能只存在于配置里。

    这条是跑真链路才发现的：任务确实被 reject 了，死信队列的深度却一直是 0。原因是
    worker 带 `-Q adpilot` 启动时 Celery 只声明它**要消费**的那个队列，生产者投递
    时也只声明目标队列 —— 于是 `x-dead-letter-exchange` 指向的 exchange 后面空无
    一物，被拒的消息就地蒸发，而两端日志都显示一切正常。

    所以 `declare_queues()` 存在，所以它在 `celeryd_init` 上挂着，所以这里验它。
    """
    app = broker.create_celery_app(live_settings)
    try:
        broker.declare_queues(app)

        with Connection(live_settings.celery_broker_url) as connection:
            channel = connection.channel()
            for queue in broker.build_queues():
                # passive=True：只问「这个队列在不在」，不在就抛 NotFound
                queue(channel).queue_declare(passive=True)
    finally:
        app.close()


@pytest.mark.integration
async def test_a_real_broker_accepts_the_queue_declaration_and_the_message(
    live_settings: Settings,
) -> None:
    """发一条、自己收回来。

    验的是**只有真连上 RabbitMQ 才知道**的事：带死信参数的队列声明得下去（参数
    对不上时 broker 会以 `PRECONDITION_FAILED` 拒掉，而 worker 那边只表现为反复
    重连），消息也确实落进了 `adpilot` 队列。

    收回来还有一个副作用是必要的：**别在共享队列里留垃圾**。

    ⚠️ **要求没有别的消费者挂在队列上**，否则先跳过（下面那个函数解释了为什么不能
    绕开这个前提）。
    """
    app = broker.create_celery_app(live_settings)
    try:
        _skip_if_a_consumer_is_attached(app)
        # 走生产代码那条投递路径，不是手拼一条 send_task —— 那样测的就只是 Celery
        # 自己了，参数名写错照样绿。
        task_id = await task_service.enqueue_normalize(app, account_id=-1)
        assert task_id is not None

        assert _drain_one(app, task_id=task_id) == {
            "task": TASK_NORMALIZE_ACCOUNT,
            "kwargs": {"account_id": -1, "stat_date": None},
        }
    finally:
        app.close()


def _skip_if_a_consumer_is_attached(app: Celery) -> None:
    """业务队列上挂着别的消费者就跳过这条用例。

    🔴 **这不是「测试不稳定」，是前提不成立。** 上面那条用例要自己把发出去的消息收
    回来，而 `docker compose up` 起的 worker 正订阅着同一个 `adpilot` 队列 —— 它会
    抢先消费掉，于是断言等在一个永远不会到货的消息上，最后报「队列里没有等到那条
    消息」。那个报错指向的方向是错的：看起来像投递坏了，实际是有人先拿走了。

    **为什么不改成投递到一条专属的测试队列**（那样就不会撞车了）：这条用例的价值
    在于它走 `task_service.enqueue_normalize` 那条**生产投递路径**，而投到哪个队列
    正是那条路径上的配置。换个队列名就得绕开路由配置，等于把最值钱的部分测没了。

    所以选择 skip 而不是回避：「worker 正在跑」是合法的开发状态，不是代码缺陷。
    CI 的集成 job 只起 service container、不起 worker，所以那边始终真跑 —— 覆盖不丢，
    只是本地开着全套环境时诚实地跳过。
    """
    main_queue = broker.build_queues()[0]
    with Connection(app.conf.broker_url) as connection:
        try:
            # passive=True：只问状态、不碰声明。队列还没建出来时抛 NotFound，
            # 那种情况下自然也不会有消费者。
            declared = main_queue(connection.channel()).queue_declare(passive=True)
        except NotFound:
            return

    # kombu 把 queue_declare 的返回值标成了 str | None，实际给回来的是 amqp 的
    # queue_declare_ok_t(queue, message_count, consumer_count)。上游标注不准，
    # 取一次存下来，省得在两处各 ignore 一遍。
    consumers: int = declared.consumer_count  # type: ignore[union-attr]

    if consumers:
        pytest.skip(
            f"{MAIN_QUEUE} 上挂着 {consumers} 个消费者"
            "（多半是 docker compose 起的 worker），它会抢先消费掉这条消息。"
            "先 `docker compose stop worker` 再跑。"
        )


def _drain_one(app: Celery, *, task_id: str | None = None) -> dict[str, Any]:
    """取一条消息，返回 `{"task": ..., "kwargs": ...}`；取不到就让用例失败。

    `task_id` 给了就只认那一条，别的照样 ack 掉扔掉 —— 开发机上那个队列里可能
    躺着上一次跑剩下的消息，不指名道姓的话，断言比的是别人那条。

    两个坑：

    * Celery 的 protocol 2 把任务名和参数放在**两个地方** —— 名字在 headers 里，
      参数在 body 里（`[args, kwargs, embed]` 三元组）。只看其中一处是找不全的。
    * **不能用 `connection.SimpleQueue(name)`**。它会按默认参数重新声明一遍队列，
      而我们的 `adpilot` 队列带着 `x-dead-letter-exchange`，于是 RabbitMQ 报
      `PRECONDITION_FAILED - inequivalent arg`。要拿就拿 `build_queues()` 里那份
      定义 —— 声明必须处处一致，这正是那条参数改不动的规矩的另一面。
    """
    main_queue = broker.build_queues()[0]
    with Connection(app.conf.broker_url) as connection:
        bound = main_queue(connection.channel())
        bound.declare()
        # basic_get 不阻塞，而 publish 是异步的 —— 轮询几次，别指望第一下就在。
        for _ in range(50):
            message = bound.get(accept=["json"])
            if message is None:
                time.sleep(0.1)
                continue
            message.ack()
            if task_id is None or message.headers["id"] == task_id:
                return {"task": message.headers["task"], "kwargs": message.payload[1]}
    raise AssertionError("队列里没有等到那条消息")
