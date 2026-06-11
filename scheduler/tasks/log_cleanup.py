# -*- coding: utf-8 -*-
"""
scheduler.tasks.log_cleanup —— 文件日志清理（按保留天数仅清理磁盘日志文件）
==========================================================================
本文件用途：实现 scheduler 服务的「文件日志清理」定时任务（需求 21.4）。

**核心安全约束（规范 11 / 需求 19.5 / 24.6）**：
- 本任务**仅清理磁盘上的日志文件**（如 *.log、轮转后的 *.log.1 等），按
  「日志保留天数」删除超过保留期的磁盘日志文件；
- **绝不**对数据库业务日志表（message_log / risk_log / system_log /
  task_run_log 等）执行任何物理删除——数据库业务数据一律保留（如需「清理」也
  只能逻辑标记，本任务不涉及任何数据库删除操作）。

保留天数来源：
- 优先读取系统设置 ``basic.log_retention_days``（与 backend setting_service 的
  存储约定一致，需求 21.6，取值 1~365）；
- 读取失败或越界时回退到默认 ``DEFAULT_RETENTION_DAYS``，保证任务可执行。

日志目录来源：
- 经环境变量 ``LOG_FILE_DIR`` 配置磁盘日志目录（禁止写死路径，规范 21 精神）；
  未配置时回退到默认目录 ``logs``（相对当前工作目录）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import List

from common.db.repository import Repository, run_with_retry
from common.models.setting_models import SysSetting
from common.utils.time_utils import now_beijing

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.log_cleanup")

# 系统设置中「基础设置」分组键与日志保留天数字段（与 backend setting_store 一致）。
_SETTING_KEY_BASIC: str = "basic"
_FIELD_RETENTION_DAYS: str = "log_retention_days"
_SETTING_SCOPE_GLOBAL: str = "global"

# 日志保留天数允许区间与默认值（需求 21.6：1~365）。
RETENTION_DAYS_MIN: int = 1
RETENTION_DAYS_MAX: int = 365
DEFAULT_RETENTION_DAYS: int = 30

# 磁盘日志目录环境变量名与默认目录（禁止写死绝对路径）。
_ENV_LOG_DIR: str = "LOG_FILE_DIR"
_DEFAULT_LOG_DIR: str = "logs"

# 视为「日志文件」的后缀名（轮转日志如 app.log.1 亦通过 ".log" 包含判定覆盖）。
_LOG_SUFFIXES: tuple[str, ...] = (".log",)


def resolve_retention_days() -> int:
    """读取并规整「日志保留天数」，越界 / 失败时回退默认值。

    经「带连接失败重试」的会话读取系统设置 basic.log_retention_days（规范 13）；
    任何异常都不向上抛出，回退默认天数，保证清理任务始终可执行。

    Returns:
        合法的保留天数（落在 [RETENTION_DAYS_MIN, RETENTION_DAYS_MAX] 内）。
    """
    try:
        raw = run_with_retry(_read_retention_setting)
    except Exception as exc:  # noqa: BLE001 —— 读取失败回退默认，不中断任务
        logger.warning("读取日志保留天数失败，回退默认 %d 天：%s", DEFAULT_RETENTION_DAYS, exc)
        return DEFAULT_RETENTION_DAYS
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    # 规整：非整数 / 布尔 / 越界一律回退默认值。
    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_DAYS
    if raw < RETENTION_DAYS_MIN or raw > RETENTION_DAYS_MAX:
        return DEFAULT_RETENTION_DAYS
    return raw


def _read_retention_setting(session) -> object:
    """从 sys_setting 读取基础设置中的日志保留天数（参数化查询）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        日志保留天数原始值；记录不存在或字段缺失时返回 None。
    """
    record = Repository(SysSetting, session).get_by(
        setting_key=_SETTING_KEY_BASIC,
        scope=_SETTING_SCOPE_GLOBAL,
        owner_user_id=None,
    )
    if record is None or not record.setting_value:
        return None
    try:
        value = json.loads(record.setting_value)
    except (ValueError, TypeError):
        return None
    if isinstance(value, dict):
        return value.get(_FIELD_RETENTION_DAYS)
    return None


def resolve_log_dir() -> Path:
    """解析磁盘日志目录（经环境变量配置，默认 ``logs``）。

    Returns:
        日志目录的 Path 对象（可能不存在，由调用方判定）。
    """
    configured = os.getenv(_ENV_LOG_DIR, _DEFAULT_LOG_DIR)
    return Path(configured)


def _is_log_file(path: Path) -> bool:
    """判断给定路径是否为「日志文件」（按后缀或文件名含 .log 判定）。

    覆盖普通日志 app.log 与轮转日志 app.log.1 / app.2024-01-01.log 等命名。

    Args:
        path: 待判定的文件路径。

    Returns:
        True 表示为日志文件。
    """
    if not path.is_file():
        return False
    name = path.name.lower()
    # 后缀匹配（app.log）或文件名包含 ".log"（轮转 app.log.1 / app.log.2024-01-01）。
    if path.suffix.lower() in _LOG_SUFFIXES:
        return True
    return ".log" in name


def cleanup_log_files(retention_days: int) -> List[str]:
    """按保留天数删除磁盘日志目录下的过期日志文件（仅磁盘文件）。

    判定规则：文件的最近修改时间（mtime）早于「当前北京时间 - 保留天数」的，
    视为过期并删除。**仅删除磁盘日志文件，绝不触碰数据库业务日志表**。

    Args:
        retention_days: 日志保留天数（调用方应先经 resolve_retention_days 规整）。

    Returns:
        已删除的日志文件路径字符串列表。
    """
    log_dir = resolve_log_dir()
    if not log_dir.exists() or not log_dir.is_dir():
        logger.info("日志目录不存在，跳过文件日志清理：%s", log_dir)
        return []

    # 过期时间阈值：早于该时刻修改的日志文件视为过期（北京时间口径，规范 17）。
    cutoff = now_beijing() - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()

    removed: List[str] = []
    # 递归遍历日志目录下的所有文件，仅处理日志文件。
    for entry in log_dir.rglob("*"):
        if not _is_log_file(entry):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as exc:
            logger.warning("读取日志文件信息失败，跳过：%s err=%s", entry, exc)
            continue
        if mtime >= cutoff_ts:
            # 未超过保留期，保留该文件。
            continue
        try:
            entry.unlink()
            removed.append(str(entry))
        except OSError as exc:
            logger.warning("删除过期日志文件失败，跳过：%s err=%s", entry, exc)
            continue

    logger.info(
        "文件日志清理完成：目录=%s 保留天数=%d 删除文件数=%d",
        log_dir,
        retention_days,
        len(removed),
    )
    return removed


__all__ = [
    "RETENTION_DAYS_MIN",
    "RETENTION_DAYS_MAX",
    "DEFAULT_RETENTION_DAYS",
    "resolve_retention_days",
    "resolve_log_dir",
    "cleanup_log_files",
]
