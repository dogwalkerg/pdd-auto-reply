<!--
  在线聊天页面（拼多多自动回复系统前端）
  参照 xianyu-auto-reply-wangpan 的「在线聊天」三栏布局，按需求与开发规范改造为
  拼多多版：
    - 会话列表 / 聊天记录均为接口实时返回（方案 A），不展示数据库数据；后台仍落库留存。
    - 多店铺「主动连接」：用户点「连接」才连该店铺，不一次性全连。
    - 实时显示（方案 2）：经 backend WebSocket 实时推送，新消息自动出现在界面。
    - 连接为常驻（与自动回复共用），聊天页只显示连接状态、不在此真断开（选 A）。

  三栏布局：
    左：店铺列表（显示连接状态；未连接可点「连接」）；
    中：会话列表（实时调拼多多 latest_conversations 返回，需求 14.1）；
    右：聊天记录（实时调拼多多 chat/list 返回，需求 14.2）+ 手动发送（需求 14.3）。

  规范遵循：全中文（27）；showToast 统一提示（6），失败不二次包装（2）；列表固定高度内部
  滚动（29）；加载遮罩 + 转圈（23）；SafeHtml 转义防 XSS（22）；时间为后端北京时间字符串（17）。
-->
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { Loading, Pagination, SafeHtml } from '@/components/common'
import { formatDateTime } from '@/utils/format'
import { showToast } from '@/utils/toast'
import { createChatWsManager } from '@/utils/chat_ws'
import { chatApi } from '@/api'

// ====================== 店铺列表（左栏，支持多店铺主动连接）======================
const shops = ref([])
const shopTotal = ref(0)
const shopPage = ref(1)
const shopPageSize = ref(20)
const shopLoading = ref(false)
// 正在连接操作中的店铺主键（用于按钮 loading 态）
const operatingShopPk = ref(null)
// 当前选中的店铺（用于加载其会话列表）
const activeShop = ref(null)

// 加载在线聊天店铺列表（含实时连接状态，后端分页，需求 14.1）
async function loadShops() {
  shopLoading.value = true
  try {
    const data = await chatApi.listChatShops({
      page: shopPage.value,
      page_size: shopPageSize.value,
    })
    shops.value = (data && data.list) || []
    shopTotal.value = (data && data.total) || 0
  } catch (error) {
    // 已由 request 统一提示
  } finally {
    shopLoading.value = false
  }
}

function onShopPageChange(p) {
  shopPage.value = p
  loadShops()
}

function onShopPageSizeChange(size) {
  shopPageSize.value = size
  shopPage.value = 1
  loadShops()
}

// 连接指定店铺（用户主动连接，参照闲鱼版「连接账号」，需求 5.1）
async function connectShop(shop) {
  operatingShopPk.value = shop.shop_pk
  try {
    await chatApi.connectShop(shop.shop_pk)
    showToast('连接成功', 'success')
    await loadShops()
    // 连接成功后自动选中该店铺并拉取会话
    const fresh = shops.value.find((s) => s.shop_pk === shop.shop_pk) || shop
    selectShop(fresh)
  } catch (error) {
    // 已由 request 统一提示
  } finally {
    operatingShopPk.value = null
  }
}

// 点击店铺：已连接则选中并拉取实时会话 + 建立实时推送；未连接则提示先连接
function selectShop(shop) {
  if (!shop.connected) {
    showToast('请先连接该店铺', 'warning')
    return
  }
  // 切换店铺前断开上一个店铺的实时推送 WS：handleRealtimeMessage 仅处理当前选中
  // 店铺，旧连接收到的推送会被丢弃，保留只会造成连接 / 内存泄漏（来回切换累积）。
  const previous = activeShop.value
  if (previous && previous.shop_pk !== shop.shop_pk) {
    wsManager.disconnect(previous.shop_pk)
  }
  activeShop.value = shop
  activeConv.value = null
  messages.value = []
  // 选中已连接店铺时建立前端实时推送 WS（仅主动连接 / 选中的店铺，不一次性全连）
  wsManager.connect(shop.shop_pk)
  loadConversations()
}

// ====================== 会话列表（中栏，实时返回）======================
const conversations = ref([])
const convLoading = ref(false)
const activeConv = ref(null)

// 实时拉取当前选中店铺的会话列表（方案 A：实时调拼多多接口，不读本地库，需求 14.1）
async function loadConversations() {
  if (!activeShop.value) {
    return
  }
  convLoading.value = true
  try {
    const data = await chatApi.syncConversations(activeShop.value.shop_pk, {
      fetch_all: true,
    })
    conversations.value = (data && data.conversations) || []
  } catch (error) {
    // 已由 request 统一提示
  } finally {
    convLoading.value = false
  }
}

// ====================== 聊天记录（右栏，实时返回）======================
const messages = ref([])
const msgLoading = ref(false)
// 消息列表容器引用：用于加载 / 追加消息后自动滚动到底部（展示最新记录）
const msgContainerRef = ref(null)

// 滚动消息区到底部（聊天记录按时间正序，最新在底部，需在渲染后滚动）
async function scrollMessagesToBottom() {
  await nextTick()
  const el = msgContainerRef.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

// 选中某会话：实时拉取该客户会话的全部历史聊天记录（方案 A，需求 14.2；后台同时落库）
async function selectConversation(conv) {
  activeConv.value = conv
  msgLoading.value = true
  messages.value = []
  // 选中即清零该会话未读
  const idx = conversations.value.findIndex((c) => c.customer_uid === conv.customer_uid)
  if (idx >= 0) {
    conversations.value[idx] = { ...conversations.value[idx], unread: 0 }
  }
  try {
    const data = await chatApi.syncHistory(
      activeShop.value.shop_pk,
      conv.customer_uid
    )
    messages.value = (data && data.messages) || []
    // 加载完成后滚动到底部，展示最新记录
    await scrollMessagesToBottom()
  } catch (error) {
    // 已由 request 统一提示
  } finally {
    msgLoading.value = false
  }
}

// ====================== 手动发送消息（需求 14.3）======================
const draft = ref('')
const sending = ref(false)

const canSend = computed(
  () =>
    Boolean(activeShop.value) &&
    Boolean(activeConv.value) &&
    draft.value.trim().length > 0 &&
    !sending.value
)

// 按 (店铺, 客户 uid) 实时发送消息，成功后刷新该会话聊天记录
async function sendMessage() {
  if (!canSend.value) {
    return
  }
  const text = draft.value.trim()
  const conv = activeConv.value
  sending.value = true
  try {
    await chatApi.sendMessageByUid(
      activeShop.value.shop_pk,
      conv.customer_uid,
      text
    )
    draft.value = ''
    // 乐观渲染：发送成功后直接在前端追加这条「客服」消息并滚到底部，无需整体重拉。
    // 仅当用户仍停留在该会话时追加（避免发送期间切换会话导致错位）。
    if (activeConv.value && activeConv.value.customer_uid === conv.customer_uid) {
      messages.value.push({
        // 本端发送暂无拼多多 msg_id；用本地临时标记避免与回推消息的 msg_id 去重冲突
        msg_id: null,
        direction: 'out',
        msg_type: 'text',
        content: text,
        msg_at: nowBeijingIso(),
        ts: null,
        _local: true,
      })
      scrollMessagesToBottom()
      // 同步刷新左侧会话列表该会话的摘要与时间（置顶）
      updateConversationSummary(conv.customer_uid, text)
    }
  } catch (error) {
    // 已由 request 统一提示
  } finally {
    sending.value = false
  }
}

// 更新左侧会话列表某会话的最近消息摘要 / 时间并置顶（本端发送 / 实时推送复用）
function updateConversationSummary(customerUid, content) {
  const idx = conversations.value.findIndex((c) => c.customer_uid === customerUid)
  if (idx < 0) {
    return
  }
  const conv = { ...conversations.value[idx], last_content: content, last_msg_at: nowBeijingIso() }
  conversations.value.splice(idx, 1)
  conversations.value.unshift(conv)
}

// 消息方向中文文案（in=客户，out=客服）
function directionLabel(direction) {
  return direction === 'out' ? '客服' : '客户'
}

// 生成当前北京时间的「无时区」ISO 字符串（与后端北京时间字段口径一致，规范 17）
// 例：2026-06-10T13:30:00。前端 formatDateTime 按本地解析展示，跨时区浏览器一致。
function nowBeijingIso() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 19)
}

// ====================== 实时推送（方案 2：WebSocket）======================
// 多店铺实时推送管理器：仅对用户主动连接 / 选中的店铺建立 WS（不一次性全连）
const wsManager = createChatWsManager({ onNewMessage: handleRealtimeMessage })

// 收到某店铺某客户的实时新消息：更新会话列表摘要/未读，若正在查看该会话则追加消息
function handleRealtimeMessage(shopPk, customerUid, message) {
  // 仅处理当前选中店铺的推送（其它店铺的消息在切换过去时重新拉取）
  if (!activeShop.value || activeShop.value.shop_pk !== shopPk) {
    return
  }
  const isViewing =
    activeConv.value && activeConv.value.customer_uid === customerUid

  // 1) 更新会话列表：命中则刷新摘要/时间/未读并置顶；未命中则插到最前
  const idx = conversations.value.findIndex((c) => c.customer_uid === customerUid)
  if (idx >= 0) {
    const conv = { ...conversations.value[idx] }
    conv.last_content = message.content || conv.last_content
    conv.last_msg_at = message.msg_at || conv.last_msg_at
    conv.unread = isViewing ? 0 : (conv.unread || 0) + 1
    conversations.value.splice(idx, 1)
    conversations.value.unshift(conv)
  } else {
    conversations.value.unshift({
      customer_uid: customerUid,
      nickname: message.nickname || customerUid,
      avatar: '',
      unread: isViewing ? 0 : 1,
      last_content: message.content || '',
      last_msg_type: message.msg_type || null,
      last_msg_id: message.msg_id || null,
      last_msg_at: message.msg_at || null,
    })
  }

  // 2) 若正在查看该会话，实时追加消息到聊天记录并滚动到底部。去重双保险：
  //    a) 按 msg_id 去重（避免同一条消息重复推送）；
  //    b) 客服(out)消息：匹配本端「乐观渲染」的临时消息（_local，无 msg_id，内容相同）
  //       —— 命中则用回推消息补全其 msg_id，而非新增一条，避免重复气泡。
  if (isViewing) {
    const byId =
      message.msg_id &&
      messages.value.some((m) => m.msg_id && m.msg_id === message.msg_id)
    if (byId) {
      return
    }
    if (message.direction === 'out') {
      // 匹配最近一条「内容相同且尚无 msg_id」的本端 out 消息（本端乐观渲染 / 自动回复
      // 推送），命中则补全其 msg_id 完成对账，而非新增一条，避免重复气泡。
      const local = messages.value.find(
        (m) => m.direction === 'out' && !m.msg_id && (m.content || '') === (message.content || '')
      )
      if (local) {
        local.msg_id = message.msg_id || local.msg_id
        local._local = false
        return
      }
    }
    messages.value.push({
      msg_id: message.msg_id,
      direction: message.direction || 'in',
      msg_type: message.msg_type,
      content: message.content,
      msg_at: message.msg_at,
      ts: message.ts,
    })
    scrollMessagesToBottom()
  }
}

// ====================== 生命周期 ======================
onMounted(() => {
  loadShops()
})

onUnmounted(() => {
  // 组件卸载时清理全部实时推送连接（前端 WS；不影响 backend↔拼多多 的常驻连接）
  wsManager.dispose()
})
</script>

<template>
  <div class="chat-page">
    <h2 class="chat-page__title">在线聊天</h2>

    <div class="chat-layout">
      <!-- 左侧：店铺列表（多店铺主动连接） -->
      <section class="chat-shops">
        <div class="chat-shops__head">
          <span>店铺列表</span>
          <button type="button" class="chat-shops__refresh" @click="loadShops">
            刷新
          </button>
        </div>

        <div class="chat-shops__list">
          <Loading :visible="shopLoading" text="加载中..." />
          <button
            v-for="shop in shops"
            :key="shop.shop_pk"
            type="button"
            class="shop-item"
            :class="{ 'shop-item--active': activeShop && activeShop.shop_pk === shop.shop_pk }"
            @click="selectShop(shop)"
          >
            <div class="shop-item__main">
              <span class="shop-item__name">
                <SafeHtml :content="shop.shop_name || shop.shop_id" />
              </span>
              <span
                v-if="!shop.connected"
                class="shop-item__op shop-item__op--on"
                @click.stop="connectShop(shop)"
              >
                <template v-if="operatingShopPk === shop.shop_pk">处理中</template>
                <template v-else>连接</template>
              </span>
            </div>
            <div class="shop-item__status">
              <span
                class="shop-item__dot"
                :class="shop.connected ? 'shop-item__dot--on' : 'shop-item__dot--off'"
              />
              {{ shop.connected ? '已连接' : '未连接' }}
            </div>
          </button>

          <p v-if="!shopLoading && shops.length === 0" class="chat-empty">
            暂无可用店铺
          </p>
        </div>

        <Pagination
          :page="shopPage"
          :page-size="shopPageSize"
          :total="shopTotal"
          @update:page="onShopPageChange"
          @update:page-size="onShopPageSizeChange"
        />
      </section>

      <!-- 中间：会话列表（实时返回） -->
      <section class="chat-conv">
        <div class="chat-conv__head">
          <span>会话列表</span>
          <button
            v-if="activeShop"
            type="button"
            class="chat-conv__refresh"
            @click="loadConversations"
          >
            刷新会话
          </button>
        </div>

        <div class="chat-conv__list">
          <Loading :visible="convLoading" text="加载中..." />
          <template v-if="!activeShop">
            <p class="chat-empty">请先选择已连接的店铺</p>
          </template>
          <template v-else>
            <button
              v-for="conv in conversations"
              :key="conv.customer_uid"
              type="button"
              class="conv-item"
              :class="{ 'conv-item--active': activeConv && activeConv.customer_uid === conv.customer_uid }"
              @click="selectConversation(conv)"
            >
              <img
                v-if="conv.avatar"
                class="conv-item__avatar"
                :src="conv.avatar"
                alt="头像"
              />
              <div v-else class="conv-item__avatar conv-item__avatar--ph"></div>
              <div class="conv-item__body">
                <div class="conv-item__head">
                  <span class="conv-item__name">
                    <SafeHtml :content="conv.nickname || conv.customer_uid" />
                  </span>
                  <span v-if="conv.unread > 0" class="conv-item__badge">
                    {{ conv.unread > 99 ? '99+' : conv.unread }}
                  </span>
                </div>
                <div class="conv-item__summary">
                  <SafeHtml :content="conv.last_content || '暂无消息'" />
                </div>
                <div class="conv-item__time">{{ formatDateTime(conv.last_msg_at) }}</div>
              </div>
            </button>

            <p v-if="!convLoading && conversations.length === 0" class="chat-empty">
              暂无会话
            </p>
          </template>
        </div>
      </section>

      <!-- 右侧：聊天记录 + 发送 -->
      <section class="chat-main">
        <div v-if="!activeConv" class="chat-main__placeholder">
          请选择左侧会话查看聊天记录
        </div>

        <template v-else>
          <div class="chat-main__header">
            <SafeHtml :content="activeConv.nickname || activeConv.customer_uid" />
          </div>

          <div ref="msgContainerRef" class="chat-main__messages">
            <Loading :visible="msgLoading" text="加载中..." />
            <div
              v-for="(msg, idx) in messages"
              :key="msg.msg_id || `msg-${idx}`"
              class="msg-row"
              :class="{ 'msg-row--out': msg.direction === 'out' }"
            >
              <div class="msg-bubble">
                <div class="msg-bubble__meta">
                  <span class="msg-bubble__dir">{{ directionLabel(msg.direction) }}</span>
                  <span class="msg-bubble__time">{{ formatDateTime(msg.msg_at) }}</span>
                </div>
                <SafeHtml class="msg-bubble__content" :content="msg.content || ''" />
              </div>
            </div>

            <p v-if="!msgLoading && messages.length === 0" class="chat-empty">
              暂无聊天记录
            </p>
          </div>

          <!-- 手动发送区（需求 14.3） -->
          <div class="chat-main__compose">
            <textarea
              v-model="draft"
              class="compose-input"
              rows="2"
              placeholder="输入消息内容，手动发送给客户"
              @keydown.enter.exact.prevent="sendMessage"
            ></textarea>
            <button
              type="button"
              class="compose-send"
              :disabled="!canSend"
              @click="sendMessage"
            >
              {{ sending ? '发送中...' : '发送' }}
            </button>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.chat-page__title {
  margin-bottom: 12px;
  color: var(--color-text);
  font-size: 18px;
}

/* 三栏布局：店铺列表 + 会话列表 + 聊天记录 */
.chat-layout {
  flex: 1;
  display: grid;
  grid-template-columns: 240px 300px 1fr;
  gap: 12px;
  min-height: 0;
}

/* ===== 通用面板 ===== */
.chat-shops,
.chat-conv,
.chat-main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg-elevated);
}

.chat-shops__head,
.chat-conv__head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);
  font-weight: 600;
  font-size: 14px;
  color: var(--color-text);
}

.chat-shops__refresh,
.chat-conv__refresh {
  padding: 4px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}

/* ===== 店铺列表 ===== */
.chat-shops__list {
  position: relative;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 8px;
}

.shop-item {
  width: 100%;
  text-align: left;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  padding: 10px;
  cursor: pointer;
  color: var(--color-text);
  margin-bottom: 4px;
}

.shop-item:hover {
  background: var(--color-hover-bg);
}

.shop-item--active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.shop-item__main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.shop-item__name {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.shop-item__op {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.shop-item__op--on {
  color: #16a34a;
  border: 1px solid #16a34a;
}

.shop-item__status {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.shop-item__dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.shop-item__dot--on {
  background: #16a34a;
}

.shop-item__dot--off {
  background: #c0c4cc;
}

/* ===== 会话列表 ===== */
.chat-conv__list {
  position: relative;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.conv-item {
  width: 100%;
  display: flex;
  gap: 8px;
  text-align: left;
  border: none;
  border-bottom: 1px solid var(--color-border);
  background: transparent;
  padding: 10px 12px;
  cursor: pointer;
  color: var(--color-text);
}

.conv-item:hover {
  background: var(--color-hover-bg);
}

.conv-item--active {
  background: var(--color-primary-light);
}

.conv-item__avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  object-fit: cover;
}

.conv-item__avatar--ph {
  background: var(--color-hover-bg);
}

.conv-item__body {
  flex: 1;
  min-width: 0;
}

.conv-item__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.conv-item__name {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-item__badge {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: #ff4d4f;
  color: #ffffff;
  font-size: 12px;
  line-height: 18px;
  text-align: center;
}

.conv-item__summary {
  margin-top: 2px;
  font-size: 12px;
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-item__time {
  margin-top: 2px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ===== 聊天记录区 ===== */
.chat-main__placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
}

.chat-main__header {
  flex-shrink: 0;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
  font-weight: 600;
  color: var(--color-text);
}

.chat-main__messages {
  position: relative;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 12px 16px;
}

.msg-row {
  display: flex;
  margin-bottom: 10px;
}

.msg-row--out {
  justify-content: flex-end;
}

.msg-bubble {
  max-width: 75%;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--color-hover-bg);
  color: var(--color-text);
}

.msg-row--out .msg-bubble {
  background: var(--color-primary-light);
}

.msg-bubble__meta {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.msg-bubble__content {
  font-size: 14px;
  word-break: break-all;
}

.chat-main__compose {
  flex-shrink: 0;
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--color-border);
}

.compose-input {
  flex: 1;
  resize: none;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 14px;
  font-family: inherit;
}

.compose-send {
  align-self: flex-end;
  padding: 9px 20px;
  border: none;
  border-radius: 6px;
  background: var(--color-primary);
  color: var(--color-on-primary);
  font-size: 14px;
  cursor: pointer;
}

.compose-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.chat-empty {
  padding: 16px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 13px;
}

/* ===== 响应式：移动端纵向堆叠（规范 20） ===== */
@media (max-width: 991px) {
  .chat-layout {
    grid-template-columns: 1fr;
    grid-auto-rows: minmax(260px, auto);
    overflow-y: auto;
  }
}
</style>
