# -*- coding: utf-8 -*-
"""
scheduler.tasks.task_run_log —— 定时任务执行日志写入
====================================================
本文件用途：为 scheduler 服务的各定时任务提供「执行日志落库」能力，将每次任务
执行结果写入 ``task_run_log`` 表（需求 21.2），并同步更新对应 ``scheduled_task``
记录的「上次执行时间 last_run_at」（北京时间）。

关键约束（开发规范）：
- 规范 12 / 16：所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL。
- 规范 11 / 需求 19.5：task_run_log 属业务日志，**禁止物理删除**，仅新增写入。
- 规范 13：写库经 ``run_with_retry`` 在连接失败时自动重试。
- 规范 17：执行时间字段 log_time / last_run_at 统一北京时间。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from common.db.repository import Repository, run_with_retry
from common.models.task_models import ScheduledTask, TaskRunLog
from common.utils.time_utils import now_beijing_naive

from tasks.constants import RESULT_FAILED, RESULT_SUCCESS

# 模块级日志记录器（禁用 debug 级别，统一 info/warning/error —— 规范 38）。
logger = logging.getLogger("scheduler.task_run_log")


def write_run_log(
    task_key: str,
    run_result: str,
    message: Optional[str] = None,
) -> None:
    """写入一条定时任务执行日志并刷新对应任务的上次执行时间。

    在「带连接失败重试」的事务性会话中执行（规范 13）：
    1. 向 task_run_log 追加一条执行结果记录（禁止物理删除，仅新增）；
    2. 若存在同 task_key 的 scheduled_task 记录，则更新其 last_run_at 为当前
       北京时间，便于前端展示与下次调度参考。

    本函数自身不抛出异常：写库失败仅记录错误日志，避免因日志写入失败反过来
    中断定时任务的主流程。

    Args:
        task_key: 任务键（cookie_refresh / product_sync / log_file_cleanup）。
        run_result: 执行结果（success / failed）。
        message: 执行信息（中文），成功概述或失败原因。
    """
    try:
        run_with_retry(lambda s: _persist(s, task_key, run_result, message))
    except Exception as exc:  # noqa: BLE001 —— 兜底：日志写入失败不得中断任务
        logger.error(
            "写入定时任务执行日志失败：task_key=%s result=%s err=%s",
            task_key,
            run_result,
            exc,
        )


def _persist(
    session: Session,
    task_key: str,
    run_result: str,
    message: Optional[str],
) -> None:
    """在单个会话中持久化执行日志并更新任务上次执行时间（参数化查询）。

    Args:
        session: 事务性会话（由 run_with_retry 管理生命周期）。
        task_key: 任务键。
        run_result: 执行结果（success / failed）。
        message: 执行信息（中文）。
    """
    now = now_beijing_naive()
    # 1) 追加执行日志记录（仅新增，禁止物理删除 —— 规范 11 / 需求 19.5）。
    Repository(TaskRunLog, session).create(
        task_key=task_key,
        run_result=run_result,
        message=message,
        log_time=now,
    )
    # 2) 更新对应任务的上次执行时间（若任务配置存在）。
    task_repo = Repository(ScheduledTask, session)
    task = task_repo.get_by(task_key=task_key)
    if task is not None:
        task_repo.update(task.id, last_run_at=now)


def write_success(task_key: str, message: Optional[str] = None) -> None:
    """写入一条「执行成功」日志（便捷封装）。

    Args:
        task_key: 任务键。
        message: 成功概述（中文）。
    """
    write_run_log(task_key, RESULT_SUCCESS, message)


def write_failed(task_key: str, message: Optional[str] = None) -> None:
    """写入一条「执行失败」日志（便捷封装）。

    Args:
        task_key: 任务键。
        message: 失败原因（中文）。
    """
    write_run_log(task_key, RESULT_FAILED, message)


__all__ = [
    "write_run_log",
    "write_success",
    "write_failed",
]
