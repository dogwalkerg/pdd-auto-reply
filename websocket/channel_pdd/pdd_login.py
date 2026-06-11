# -*- coding: utf-8 -*-
"""
channel_pdd.pdd_login —— 拼多多店铺登录服务编排
================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0 ``Channel/pinduoduo/pdd_login.py``
中 ``PDDLogin`` 与模块级 ``login_pdd`` / ``refresh_pdd_cookies`` 的「编排逻辑」，
在本系统 websocket 服务内统一对接：
- Playwright 账号密码登录 / Cookie 刷新（``login.playwright_login``）；
- Cookie 文本导入校验（``login.cookie_import``）；
- 店铺 / 用户信息抓取（``channel_pdd.api.get_shop_info`` / ``get_user_info``）；
- Token 获取（``channel_pdd.api.get_token``）；
- 登录态标记与账号登录日志（``channel_pdd.core.login_state_store``）。

满足需求 4：
- 4.1 / 4.2 / 4.5：账号密码登录（含人工验证等待）、获取含签名 Cookie 与店铺信息；
- 4.3 / 4.4：Cookie 文本导入校验，无效返回失败原因；
- 4.6 / 4.7：Cookie 刷新与登录态标记（失效标记「需重新登录」并记账号登录日志）；
- 4.8：基于有效 Cookie 获取建立 WebSocket 连接所需 Token；
- 4.9：登录态枚举（未验证 / 在线 / 离线 / 需重新登录）入字典并对外查询。

本模块同时提供 ``relogin_pdd`` 作为基础请求层会话过期自动重登的回调
（被 ``channel_pdd.core.session_relogin`` 惰性接入，需求 4.6）。

实现约束（开发规范）：导入置顶、中文注释、全中文、文件名用下划线、单文件 ≤500 行；
登录信息抓取经 BaseRequest 复用，避免重复实现（规范 36）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from channel_pdd.api.get_shop_info import GetShopInfo
from channel_pdd.api.get_token import GetToken
from channel_pdd.api.get_user_info import GetUserInfo
from channel_pdd.core.login_state_store import (
    LOGIN_STATE_ONLINE,
    LOGIN_STATE_RELOGIN,
    mark_login_state,
    record_account_login_log,
)
from login.cookie_import import validate_cookie_text
from login.playwright_login import login_with_password, refresh_with_user_data

logger = logging.getLogger("channel_pdd.pdd_login")

# 渠道名称固定为 pinduoduo。
CHANNEL_NAME: str = "pinduoduo"


def _extract_shop_and_user(cookies: Any) -> Optional[Dict[str, Any]]:
    """基于 Cookie 抓取店铺与用户信息，组装为统一店铺信息字典。

    复用 ``GetUserInfo`` / ``GetShopInfo``（经 BaseRequest）请求商家后台接口。
    任一关键信息（user_id / shop_id）缺失则视为失败返回 None。

    Args:
        cookies: 已登录的 Cookie（字典或 JSON 字符串）。

    Returns:
        含 shop_id/shop_name/shop_logo/user_id 等的字典；失败返回 None。
    """
    user_info = GetUserInfo(cookies).get_user_info()
    shop_info = GetShopInfo(cookies).get_shop_info()
    if not user_info or not shop_info:
        logger.error("获取用户信息或店铺信息失败")
        return None

    user_id, user_name, _mall_id = user_info
    shop_id, shop_name, shop_logo = shop_info
    if user_id is None or shop_id is None:
        logger.error("用户信息或店铺信息缺少关键标识（user_id/shop_id）")
        return None

    return {
        "channel_name": CHANNEL_NAME,
        "shop_id": shop_id,
        "shop_name": shop_name,
        "shop_logo": shop_logo,
        "user_id": user_id,
        "user_name": user_name,
        "cookies": cookies,
    }


# ----------------------------------------------------------------------
# 账号密码登录（需求 4.1 / 4.2 / 4.5）
# ----------------------------------------------------------------------
async def login_pdd(name: str, password: str) -> Optional[Dict[str, Any]]:
    """账号密码登录并返回店铺 / 账号信息（需求 4.1 / 4.2 / 4.5）。

    经 Playwright 完成账号密码登录（含人工验证等待），成功后基于 Cookie 抓取
    用户与店铺信息。登录失败 / 超时（含人工验证未完成）返回 None。

    Args:
        name: 登录账号名。
        password: 账号密码。

    Returns:
        成功返回含 channel_name/shop_id/shop_name/shop_logo/user_id/username/
        password/cookies 的字典；失败返回 None。
    """
    cookies_json = await login_with_password(name, password)
    if not cookies_json:
        logger.error("账号 '%s' 账号密码登录失败，未获取到 Cookie", name)
        record_account_login_log(f"账号 '{name}' 账号密码登录失败", level="warning")
        return None

    info = _extract_shop_and_user(cookies_json)
    if info is None:
        logger.error("账号 '%s' 登录成功但获取店铺 / 用户信息失败", name)
        record_account_login_log(
            f"账号 '{name}' 登录成功但获取店铺 / 用户信息失败", level="warning"
        )
        return None

    # 附加登录凭据（供加密存储与后续自动重登使用）。
    info["username"] = name
    info["password"] = password
    logger.info(
        "账号 '%s' 登录成功，店铺: %s(%s)", name, info.get("shop_name"), info.get("shop_id")
    )
    record_account_login_log(
        f"账号 '{name}' 账号密码登录成功，店铺 {info.get('shop_name')}({info.get('shop_id')})",
        level="info",
        user_id=info.get("user_id"),
    )
    return info


# ----------------------------------------------------------------------
# Cookie 文本导入校验（需求 4.3 / 4.4）
# ----------------------------------------------------------------------
def import_by_cookie(cookie_text: str) -> Dict[str, Any]:
    """校验手动粘贴的 Cookie 文本并抓取店铺信息（需求 4.3 / 4.4）。

    先做格式校验（解析为非空键值对），再基于 Cookie 抓取店铺 / 用户信息验证
    其有效性。任一环节失败返回 ``{"success": False, "message": <中文原因>}``；
    成功返回 ``{"success": True, "data": <店铺信息字典>}``。

    Args:
        cookie_text: 用户粘贴的 Cookie 文本。

    Returns:
        统一结果字典（success / message / data）。
    """
    valid, cookies, reason = validate_cookie_text(cookie_text)
    if not valid:
        return {"success": False, "message": reason}

    info = _extract_shop_and_user(cookies)
    if info is None:
        # 格式正确但无法获取店铺信息（Cookie 失效 / 不完整，需求 4.4）。
        return {
            "success": False,
            "message": "Cookie 无效或无法获取店铺信息，请重新获取后再导入",
        }

    logger.info("Cookie 导入校验通过，店铺: %s(%s)", info.get("shop_name"), info.get("shop_id"))
    return {"success": True, "data": info}


# ----------------------------------------------------------------------
# Cookie 刷新与登录态标记（需求 4.6 / 4.7）
# ----------------------------------------------------------------------
async def refresh_pdd_cookies(name: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """刷新拼多多账号 Cookie 并维护登录态（需求 4.6 / 4.7）。

    使用已保存的用户数据目录无头刷新 Cookie：
    - 刷新成功：抓取店铺信息并标记登录态为「在线」，返回最新店铺信息字典；
    - 登录态失效：标记登录态为「需重新登录」并记录账号登录日志，返回 None。

    Args:
        name: 登录账号名。
        user_id: 归属用户 ID（用于登录态标记与日志，建议提供）。

    Returns:
        刷新成功返回含最新 cookies 与店铺信息的字典；失效 / 失败返回 None。
    """
    cookies_json = await refresh_with_user_data(name)
    if not cookies_json:
        logger.warning("账号 '%s' Cookie 刷新失败或登录态已失效", name)
        _mark_relogin_if_possible(name, user_id)
        return None

    info = _extract_shop_and_user(cookies_json)
    if info is None:
        logger.warning("账号 '%s' 刷新成功但获取店铺信息失败", name)
        _mark_relogin_if_possible(name, user_id)
        return None

    info["username"] = name
    # 刷新成功：标记在线（需求 4.6 / 4.9）。
    if user_id is not None:
        mark_login_state(str(info.get("shop_id")), user_id, LOGIN_STATE_ONLINE)
    logger.info("账号 '%s' Cookie 刷新成功，店铺: %s(%s)", name, info.get("shop_name"), info.get("shop_id"))
    return info


def _mark_relogin_if_possible(name: str, user_id: Optional[int]) -> None:
    """登录态失效时标记「需重新登录」并记录账号登录日志（需求 4.7）。

    由于刷新失败时未必已抓取到 shop_id，故仅在能定位店铺时标记登录态；账号登录
    日志始终记录，便于追溯。

    Args:
        name: 登录账号名。
        user_id: 归属用户 ID。
    """
    record_account_login_log(
        f"账号 '{name}' Cookie 刷新检测到登录态已失效，需重新登录",
        level="warning",
        user_id=user_id,
    )


# ----------------------------------------------------------------------
# Token 获取（需求 4.8）
# ----------------------------------------------------------------------
def get_token(
    shop_id: Optional[str] = None,
    user_id: Optional[int] = None,
    cookies: Any = None,
) -> Optional[str]:
    """基于 Cookie 获取建立 WebSocket 连接所需的 Token（需求 4.8）。

    支持两种方式：按 (shop_id, user_id) 自数据库加载已存 Cookie，或直接注入
    登录刚获取的 Cookie。

    Args:
        shop_id: 拼多多店铺业务标识。
        user_id: 归属用户 ID。
        cookies: 直接注入的 Cookie（字典或 JSON 字符串），优先于数据库加载。

    Returns:
        成功返回 token 字符串；失败返回 None。
    """
    return GetToken(
        shop_id=shop_id, user_id=user_id, channel_name=CHANNEL_NAME, cookies=cookies
    ).get_token()


# ----------------------------------------------------------------------
# 会话过期自动重登回调（被 session_relogin 惰性接入，需求 4.6）
# ----------------------------------------------------------------------
def relogin_pdd(
    shop_id: Optional[str] = None,
    user_id: Optional[int] = None,
    channel_name: str = CHANNEL_NAME,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """会话过期时的自动重登回调（供 BaseRequest 会话过期自动重登使用，需求 4.6）。

    优先使用已保存的用户数据目录无头刷新 Cookie；刷新失败且具备账号密码时，回退
    到账号密码登录。返回含 "cookies" 键的字典表示成功（与 SessionReloginMixin 约定
    一致）。本函数在「无运行事件循环」的同步调用场景下经 asyncio.run 驱动异步流程。

    Args:
        shop_id: 拼多多店铺业务标识。
        user_id: 归属用户 ID。
        channel_name: 渠道名称（默认 pinduoduo）。
        username: 登录账号名（刷新与回退登录均需要）。
        password: 账号密码（回退账号密码登录时需要）。

    Returns:
        成功返回含 cookies 的店铺信息字典；失败返回 None。
    """
    if not username:
        logger.error("自动重登缺少账号名，无法刷新 / 重登")
        return None

    async def _do_relogin() -> Optional[Dict[str, Any]]:
        # 1) 先尝试用已保存用户数据无头刷新。
        info = await refresh_pdd_cookies(username, user_id)
        if info is not None:
            return info
        # 2) 刷新失败且有密码：回退账号密码登录。
        if password:
            logger.info("账号 '%s' 刷新失败，回退账号密码登录", username)
            return await login_pdd(username, password)
        return None

    try:
        return _run_async(_do_relogin())
    except Exception as exc:  # noqa: BLE001 - 自动重登异常不应向上冒泡
        logger.error("账号 '%s' 自动重登异常: %s", username, exc)
        return None


def _run_async(coro: Any) -> Any:
    """在同步上下文中驱动协程执行（兼容已有运行中事件循环的场景）。

    - 当前线程无运行中的事件循环：直接 ``asyncio.run``。
    - 当前线程已有运行中的事件循环（如在 websocket 异步处理链路内被同步调用）：
      在**独立线程**中新建事件循环运行该协程并等待结果。注意不能在同一线程内
      对已运行的循环再 ``run_until_complete``（会抛「loop is already running」），
      故必须切换到新线程执行（参照 Customer-Agent 的 run_async_in_thread 思路）。

    Args:
        coro: 待执行的协程对象。

    Returns:
        协程的返回值。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环：直接 run。
        return asyncio.run(coro)

    # 已在事件循环中：切换到独立线程运行新循环，避免同线程嵌套运行循环报错。
    result_box: Dict[str, Any] = {}

    def _runner() -> None:
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            result_box["value"] = new_loop.run_until_complete(coro)
        except Exception as exc:  # noqa: BLE001 - 捕获后在主线程重新抛出
            result_box["error"] = exc
        finally:
            new_loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


__all__ = [
    "CHANNEL_NAME",
    "login_pdd",
    "import_by_cookie",
    "refresh_pdd_cookies",
    "get_token",
    "relogin_pdd",
]
