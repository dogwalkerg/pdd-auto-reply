// 在线聊天实时推送 WebSocket 工具（拼多多自动回复系统前端）
// 职责（需求 14，方案 2 实时推送，参照 xianyu-auto-reply-wangpan）：
//   管理与 backend 的多店铺 WebSocket 连接，接收拼多多客户消息实时推送并回调上层。
//   每个「已连接并选中」的店铺维护一条独立 WebSocket，支持心跳保活与自动重连。
// 说明：
//   - 浏览器原生 WebSocket 不支持自定义请求头，令牌经查询参数 token 传入（与后端约定）；
//   - WS 地址经同源 /api 前缀，由 vite 代理（ws:true）/ nginx 反代转发到 backend，禁止写死 localhost（规范 21）；
//   - 仅在用户主动连接 / 选中店铺时建连（不一次性全连）。
import { TOKEN_STORAGE_KEY } from '@/api/auth_api'

// 心跳间隔（毫秒）
const HEARTBEAT_INTERVAL = 20000
// 重连延迟（毫秒）
const RECONNECT_DELAY = 3000
// 最大连续重连次数（达上限后停止，防御异常情况下的无限重连）
const MAX_RECONNECT = 10
// 后端鉴权失败 / 越权的 WebSocket 关闭码（1008=策略违规）：收到则不再重连
const WS_CLOSE_POLICY = 1008

// 构造在线聊天 WebSocket 基础地址（同源，经代理转发到 backend）
function getWsBaseUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}`
}

// 创建某店铺的实时推送 WebSocket（令牌经查询参数传入）
function createShopWs(shopPk) {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY) || ''
  const base = getWsBaseUrl()
  // 与 backend 路由约定：/api/v1/chat/ws/{shop_pk}?token=<JWT>
  return new WebSocket(`${base}/api/v1/chat/ws/${shopPk}?token=${encodeURIComponent(token)}`)
}

/**
 * 多店铺在线聊天 WebSocket 管理器
 *
 * 用法：
 *   const mgr = createChatWsManager({ onNewMessage })
 *   mgr.connect(shopPk)      // 主动连接某店铺的实时推送
 *   mgr.disconnect(shopPk)   // 断开某店铺
 *   mgr.dispose()            // 组件卸载时清理全部连接
 */
export function createChatWsManager({ onNewMessage }) {
  // shopPk -> { ws, heartbeat, reconnect, closed }
  const connections = new Map()

  function cleanup(shopPk) {
    const conn = connections.get(shopPk)
    if (!conn) {
      return
    }
    conn.closed = true
    if (conn.heartbeat) {
      clearInterval(conn.heartbeat)
    }
    if (conn.reconnect) {
      clearTimeout(conn.reconnect)
    }
    if (conn.ws) {
      conn.ws.onopen = null
      conn.ws.onmessage = null
      conn.ws.onerror = null
      conn.ws.onclose = null
      if (conn.ws.readyState === WebSocket.OPEN || conn.ws.readyState === WebSocket.CONNECTING) {
        conn.ws.close()
      }
    }
    connections.delete(shopPk)
  }

  function connect(shopPk, retries = 0) {
    // 已有连接则不重复建连
    if (connections.has(shopPk)) {
      return
    }
    const ws = createShopWs(shopPk)
    const conn = { ws, heartbeat: null, reconnect: null, closed: false, retries }
    connections.set(shopPk, conn)

    ws.onopen = () => {
      // 连接成功：重置重连计数
      conn.retries = 0
      conn.heartbeat = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, HEARTBEAT_INTERVAL)
    }

    ws.onmessage = (event) => {
      let data
      try {
        data = JSON.parse(event.data)
      } catch (error) {
        return
      }
      if (data.event === 'new_message' && data.customer_uid && data.message) {
        onNewMessage(shopPk, data.customer_uid, data.message)
      }
    }

    ws.onerror = () => {}

    ws.onclose = (event) => {
      if (conn.heartbeat) {
        clearInterval(conn.heartbeat)
        conn.heartbeat = null
      }
      if (conn.closed) {
        return
      }
      // 鉴权失败 / 越权（后端 close code 1008）：不重连，避免 token 失效时的重连风暴
      if (event && event.code === WS_CLOSE_POLICY) {
        connections.delete(shopPk)
        return
      }
      // 达到最大重连次数：停止重连（防御异常情况下的无限重连）
      const nextRetries = (conn.retries || 0) + 1
      if (nextRetries > MAX_RECONNECT) {
        connections.delete(shopPk)
        return
      }
      // 其它原因（网络抖动 / 服务重启）：延迟自动重连，累计重连次数
      conn.reconnect = setTimeout(() => {
        if (!conn.closed) {
          connections.delete(shopPk)
          connect(shopPk, nextRetries)
        }
      }, RECONNECT_DELAY)
    }
  }

  function disconnect(shopPk) {
    cleanup(shopPk)
  }

  function dispose() {
    for (const shopPk of Array.from(connections.keys())) {
      cleanup(shopPk)
    }
  }

  return { connect, disconnect, dispose }
}

export default createChatWsManager
