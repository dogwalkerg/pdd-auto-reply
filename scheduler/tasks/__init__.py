# -*- coding: utf-8 -*-
"""
文件用途：scheduler 服务「定时任务」包。

本包承载各类定时任务的调度与执行逻辑（任务 15.1 实现），包含：
  - Cookie 刷新任务：遍历启用店铺，跨服务经 HTTP 调用 websocket 服务刷新登录态
    Cookie，维持连接可用（需求 4.6 / 21.2）；
  - 商品同步任务：遍历启用店铺，跨服务经 HTTP 调用 backend 服务触发商品同步
    （需求 15.2 / 21.2）；
  - 文件日志清理任务：按保留天数仅清理磁盘日志文件；数据库业务日志表禁止物理
    删除（规范 11 / 需求 19.5 / 21.4）。

模块组织（单文件 ≤500 行，规范 35）：
  - constants.py         任务键 / 调度方式 / 执行结果常量；
  - task_run_log.py      执行日志写入（task_run_log，禁止物理删除）；
  - service_client.py    跨服务 HTTP 调用（地址经环境变量，禁止写死 localhost）；
  - log_cleanup.py       磁盘日志文件清理（仅磁盘，绝不删库）；
  - task_runners.py      三类任务执行体；
  - scheduler_service.py 核心调度器 SchedulerService（按 scheduled_task 配置调度）。
"""
from __future__ import annotations

from tasks.scheduler_service import SchedulerService

__all__ = [
    "SchedulerService",
]
