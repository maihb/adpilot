"""报表文件导入的路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from adpilot.api.deps import MongoDep, SessionDep
from adpilot.api.errors import responses
from adpilot.models.daily_metric import MetricLevel
from adpilot.providers import registry
from adpilot.schemas.imports import ImportResponse
from adpilot.services import imports as imports_service

router = APIRouter(tags=["imports"])

# UploadFile 会把整个文件读进内存，所以必须有上限，否则一个几百 MB 的文件就能
# 把进程撑爆。10 MiB 对「后台导出的日报表」绰绰有余（几万行 CSV 也就几 MB）。
# D6 把这条链路挪进 Celery 之后，大文件该走对象存储 + 任务队列，而不是放宽这里。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post(
    "/imports",
    response_model=ImportResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="importReportFile",
    responses=responses(
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def import_report_file(
    session: SessionDep,
    mongo: MongoDep,
    account_id: Annotated[int, Form(description="快照归属的广告账户")],
    file: Annotated[UploadFile, File(description="平台后台导出的报表文件")],
    level: Annotated[
        MetricLevel,
        Form(description="这份报表是哪个投放层级的。导出时选的是什么就填什么"),
    ],
    provider: Annotated[
        str,
        Form(description="数据源适配器；目前只有 file_csv"),
    ] = "file_csv",
    date_column: Annotated[
        str | None,
        Form(description="日期列名。不给就自动探测，探测不到会报错并列出表头"),
    ] = None,
) -> ImportResponse:
    """把一份报表文件解析成按天分组的原始快照，落进 Mongo。

    **这一步不做字段映射**，落进去的是未经解释的原始行 —— 归一化是下一步。同一个
    (账户, 日期) 导两次会得到两条快照，这是刻意的：`raw_reports` append-only，
    去重是归一化按唯一键 upsert 时的事。

    账户不存在或文件解析不了都返回 422，`detail` 里带得上定位信息（第几行、
    哪个字段、期望什么）。
    """
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MiB 上限",
        )

    summary = await imports_service.import_report_file(
        session,
        mongo,
        account_id=account_id,
        provider_name=provider,
        content=await file.read(),
        level=level,
        date_column=date_column,
    )
    return ImportResponse.model_validate(summary)


@router.get(
    "/imports/providers",
    response_model=list[str],
    operation_id="listImportProviders",
)
async def list_import_providers() -> list[str]:
    """列出可用的数据源适配器。

    给内部后台的下拉框用 —— 前端硬编码一份清单的话，接入新平台那天要改两处，
    而漏改的那处不会报错，只会让新平台在界面上「不存在」。
    """
    return registry.available()
