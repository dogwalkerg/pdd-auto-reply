# -*- coding: utf-8 -*-
"""
backend.app.services.product_spec_backfill —— 商品规格后台异步补拉
================================================================
本文件用途：在「商品同步」主流程返回后，**用独立后台线程**逐个商品补拉拼多多
商品详情中的规格信息并落库（需求 15），不阻塞、不影响同步接口的主流程返回。

为何独立线程（用户诉求）：
- 商品列表接口（recommendGoods）不返回规格，规格只能逐个商品调详情接口获取；
- 若在同步主流程内串行补拉，会显著拉长接口响应时间。故同步主流程仅负责
  「拉列表 + upsert 基础信息」并立即返回，规格补拉交由后台线程异步完成。

设计要点（开发规范）：
- 后台线程使用 **独立数据库会话**（``session_scope``），绝不复用请求线程的
  会话（Session 非线程安全）；线程为 daemon，不阻塞进程退出。
- 经统一服务间客户端 ``product_sync_client.fetch_product_detail`` 调用 websocket
  查询详情（地址经环境变量配置，禁止写死 localhost，规范 21）。
- 规格以 JSON 文本逐个 upsert 落库（按 product.id），失败 / 签名缺失 / 外部依赖
  不可用均仅记日志跳过，不抛出、不中断其它商品（健壮性兜底，需求 26）。
- 每个商品之间留有可配置的请求间隔，降低对拼多多接口的压力（参照 Customer-Agent
  ``product_sync`` 的 request_delay 思路）。
- 并发保护：同一店铺同时只允许一个补拉任务在跑，重复触发直接跳过（避免叠加请求）。
- 导入置顶（规范 51）、中文注释（规范 37）、日志禁用 debug（规范 38）、
  文件名用下划线（规范 40）、单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Set

from app.services import product_spec_codec, product_sync_client
from common.db.session import session_scope
from common.models.knowledge_models import Product
from common.db.repository import Repository

logger = logging.getLogger(__name__)

# 每个商品详情请求之间的间隔（秒），降低对拼多多接口的压力。
_REQUEST_DELAY_SECONDS: float = 0.5

# 单次后台补拉处理的最大商品数保护（避免异常数据导致超长任务）。
_MAX_BACKFILL_ITEMS: int = 500

# 正在补拉中的店铺主键集合 + 保护锁（同店铺同时仅允许一个补拉任务，避免叠加请求）。
_running_shops: Set[int] = set()
_running_lock = threading.Lock()


def schedule_spec_backfill(
    shop_pk: int, shop_id: str, owner_user_id: Optional[int]
) -> bool:
    """调度一个后台线程，异步补拉指定店铺全部商品的规格并落库（需求 15）。

    立即返回、不阻塞调用方（商品同步主流程）。同一店铺已有补拉任务在跑时直接跳过
    （返回 False），避免重复叠加请求。

    Args:
        shop_pk: 店铺主键 shop.id。
        shop_id: 拼多多店铺业务标识（websocket 侧定位凭据用）。
        owner_user_id: 店铺归属用户 ID（websocket 侧定位凭据用）。

    Returns:
        成功启动后台线程返回 True；已有同店铺任务在跑而跳过返回 False。
    """
    with _running_lock:
        if shop_pk in _running_shops:
            logger.info("店铺[shop_pk=%s]规格补拉任务已在进行中，跳过本次调度", shop_pk)
            return False
        _running_shops.add(shop_pk)

    thread = threading.Thread(
        target=_run_backfill,
        args=(shop_pk, shop_id, owner_user_id),
        name=f"spec-backfill-{shop_pk}",
        daemon=True,
    )
    thread.start()
    logger.info("已启动店铺[shop_pk=%s]商品规格后台补拉线程", shop_pk)
    return True


def _run_backfill(shop_pk: int, shop_id: str, owner_user_id: Optional[int]) -> None:
    """后台线程主体：逐个商品补拉规格并落库（独立会话，失败跳过不中断）。

    Args:
        shop_pk: 店铺主键 shop.id。
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID。
    """
    try:
        targets = _load_target_products(shop_pk)
        if not targets:
            logger.info("店铺[shop_pk=%s]无待补拉规格的商品", shop_pk)
            return

        logger.info(
            "店铺[shop_pk=%s]开始补拉商品规格，共 %d 个商品", shop_pk, len(targets)
        )
        updated = 0
        for index, (product_id, goods_id) in enumerate(targets):
            if index > 0:
                # 商品之间留间隔，降低接口压力。
                time.sleep(_REQUEST_DELAY_SECONDS)
            if _backfill_one(shop_id, owner_user_id, product_id, goods_id):
                updated += 1

        logger.info(
            "店铺[shop_pk=%s]商品规格补拉完成，更新 %d 个商品规格", shop_pk, updated
        )
    except Exception as exc:  # noqa: BLE001 - 后台线程兜底，绝不向外抛出
        logger.error("店铺[shop_pk=%s]商品规格后台补拉异常: %s", shop_pk, exc)
    finally:
        # 无论成败都要释放「运行中」标记，允许后续再次调度。
        with _running_lock:
            _running_shops.discard(shop_pk)


def _load_target_products(shop_pk: int) -> List[tuple[int, str]]:
    """加载指定店铺待补拉规格的商品 (product_id, goods_id) 列表（独立会话）。

    Args:
        shop_pk: 店铺主键 shop.id。

    Returns:
        商品 (id, goods_id) 元组列表（最多 ``_MAX_BACKFILL_ITEMS`` 个）。
    """
    targets: List[tuple[int, str]] = []
    with session_scope() as session:
        products = Repository(Product, session).list(
            filters={"shop_pk": shop_pk}, order_by=False
        )
        for product in products:
            goods_id = product.goods_id
            if goods_id is None or not str(goods_id).strip():
                continue
            targets.append((product.id, str(goods_id).strip()))
            if len(targets) >= _MAX_BACKFILL_ITEMS:
                break
    return targets


def _backfill_one(
    shop_id: str,
    owner_user_id: Optional[int],
    product_id: int,
    goods_id: str,
) -> bool:
    """补拉单个商品的规格并落库（独立会话，失败仅记日志返回 False）。

    Args:
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID。
        product_id: 商品记录主键 product.id。
        goods_id: 拼多多商品业务标识。

    Returns:
        成功拉取到非空规格并落库返回 True；否则 False。
    """
    result = product_sync_client.fetch_product_detail(
        shop_id=shop_id, owner_user_id=owner_user_id, goods_id=goods_id
    )
    if not result.ok:
        if result.signature_missing:
            # 签名缺失对全店一致：记一次告警即可（逐个会刷屏，但仍跳过不中断）。
            logger.warning(
                "店铺商品[goods_id=%s]规格补拉因签名缺失跳过", goods_id
            )
        else:
            logger.warning(
                "店铺商品[goods_id=%s]规格补拉失败：%s", goods_id, result.message
            )
        return False

    specifications = (result.detail or {}).get("specifications")
    encoded = product_spec_codec.encode_specifications(specifications)
    if encoded is None:
        # 实时无规格：不覆盖库内已有规格，跳过。
        return False

    with session_scope() as session:
        Repository(Product, session).update(product_id, specifications=encoded)
    return True


__all__ = ["schedule_spec_backfill"]
