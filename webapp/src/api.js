/**
 * 统一 API 客户端。
 *
 * 后端为 FastAPI，所有业务端点前缀 /api/v1；错误体形如 { detail: string | Array }。
 * 401 时清空本地令牌并跳转登录，避免各页面重复处理。
 */

const BASE = '/api/v1'
const TOKEN_KEY = 'pobi_token'

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY) || '',
  set: (t) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** 把 FastAPI 的 detail（字符串或校验错误数组）压平成一句话。 */
function flattenDetail(detail) {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === 'string' ? d : `${(d.loc || []).join('.')} ${d.msg || ''}`.trim()))
      .join('；')
  }
  return String(detail)
}

let onUnauthorized = null
/** 由 Auth 层注入，401 时触发登出。 */
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

function buildUrl(path, query) {
  const qs = new URLSearchParams()
  Object.entries(query || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  const s = qs.toString()
  return `${BASE}${path}${s ? `?${s}` : ''}`
}

async function request(path, { method = 'GET', body, query, signal } = {}) {
  const headers = { Accept: 'application/json' }
  const token = tokenStore.get()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const res = await fetch(buildUrl(path, query), {
    method,
    headers,
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (res.status === 401) {
    tokenStore.clear()
    onUnauthorized?.()
    throw new ApiError('登录状态已失效，请重新登录', { status: 401 })
  }

  if (res.status === 204) return null

  const text = await res.text()
  let payload = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!res.ok) {
    const detail = payload && typeof payload === 'object' ? payload.detail : payload
    throw new ApiError(flattenDetail(detail) || `请求失败（HTTP ${res.status}）`, {
      status: res.status,
      detail,
    })
  }
  return payload
}

// ---------------------------------------------------------------- 鉴权

export const authApi = {
  login: (email, password) => request('/auth/login', { method: 'POST', body: { email, password } }),
  me: () => request('/auth/me'),
}

// ---------------------------------------------------------------- 授权目标

export const targetsApi = {
  list: (signal) => request('/targets', { signal }),
  get: (id, signal) => request(`/targets/${id}`, { signal }),
  create: (data) => request('/targets', { method: 'POST', body: data }),
  update: (id, data) => request(`/targets/${id}`, { method: 'PATCH', body: data }),
  remove: (id) => request(`/targets/${id}`, { method: 'DELETE' }),
  overview: (id, signal) => request(`/targets/${id}/overview`, { signal }),
  tree: (id, signal) => request(`/targets/${id}/tree`, { signal }),
  facts: (id, signal) => request(`/targets/${id}/facts`, { signal }),
  threats: (id, signal) => request(`/targets/${id}/threats`, { signal }),
  findings: (id, signal) => request(`/targets/${id}/findings`, { signal }),
  artifacts: (id, signal) => request(`/targets/${id}/artifacts`, { signal }),
  assets: (id, signal) => request(`/targets/${id}/assets`, { signal }),
}

// ---------------------------------------------------------------- 任务

export const tasksApi = {
  list: (signal) => request('/tasks', { signal }),
  create: (data) => request('/tasks', { method: 'POST', body: data }),
  verifyAuth: (data) => request('/tasks/verify-auth', { method: 'POST', body: data }),
  get: (id, signal) => request(`/tasks/${id}`, { signal }),
  update: (id, data) => request(`/tasks/${id}`, { method: 'PATCH', body: data }),
  remove: (id) => request(`/tasks/${id}`, { method: 'DELETE' }),
  enqueue: (id) => request(`/tasks/${id}/enqueue`, { method: 'POST' }),
  cancel: (id) => request(`/tasks/${id}/cancel`, { method: 'POST' }),
  live: (id, signal) => request(`/tasks/${id}/live`, { signal }),
  plan: (id, signal) => request(`/tasks/${id}/plan`, { signal }),
  events: (id, query, signal) => request(`/tasks/${id}/events`, { query, signal }),
  usage: (id, signal) => request(`/tasks/${id}/usage`, { signal }),
  usageSummary: (signal) => request('/tasks/usage/summary', { signal }),
  findings: (id, signal) => request(`/tasks/${id}/findings`, { signal }),
  artifacts: (id, signal) => request(`/tasks/${id}/artifacts`, { signal }),
  report: (id, signal) => request(`/tasks/${id}/report`, { signal }),
  reportMarkdown: (id, signal) => request(`/tasks/${id}/report/markdown`, { signal }),
  instruct: (id, instruction) =>
    request(`/tasks/${id}/instructions`, { method: 'POST', body: { instruction } }),
  reconThreats: (id, signal) => request(`/tasks/${id}/recon/threats`, { signal }),
}

// ---------------------------------------------------------------- 认证前置（PreAuth）

export const taskAuthApi = {
  status: (id, signal) => request(`/tasks/${id}/auth/status`, { signal }),
  auto: (id, body) => request(`/tasks/${id}/auth/auto`, { method: 'POST', body: body || {} }),
  // [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
  // manualStart: (id) => request(`/tasks/${id}/auth/manual/start`, { method: 'POST' }),
  // manualSnapshot: (id, signal) => request(`/tasks/${id}/auth/manual/snapshot`, { signal }),
  // manualAction: (id, action) => request(`/tasks/${id}/auth/manual/action`, { method: 'POST', body: action }),
  // manualCapture: (id) => request(`/tasks/${id}/auth/manual/capture`, { method: 'POST' }),
  // manualAbort: (id) => request(`/tasks/${id}/auth/manual/abort`, { method: 'POST' }),
}

// ---------------------------------------------------------------- 审批 / 审计

export const approvalsApi = {
  list: (query, signal) => request('/approvals', { query, signal }),
  decide: (id, decision, reason) =>
    request(`/approvals/${id}/decision`, { method: 'POST', body: { decision, reason } }),
}

export const auditApi = {
  list: (query, signal) => request('/audit', { query, signal }),
}

// ---------------------------------------------------------------- 用量 / 计费

export const pricingApi = {
  get: (signal) => request('/pricing', { signal }),
  update: (data) => request('/pricing', { method: 'PUT', body: data }),
}

// ---------------------------------------------------------------- API 令牌

export const tokensApi = {
  list: (signal) => request('/tokens', { signal }),
  create: (data) => request('/tokens', { method: 'POST', body: data }),
  reveal: (id) => request(`/tokens/${id}/reveal`, { method: 'POST' }),
  revoke: (id) => request(`/tokens/${id}`, { method: 'DELETE' }),
}

// ---------------------------------------------------------------- 系统

export const systemApi = {
  workerStatus: (signal) => request('/system/worker-status', { signal }),
  kaliStatus: (signal) => request('/system/kali-status', { signal }),
  llmStatus: (signal) => request('/system/llm-status', { signal }),
  probe: (data) => request('/system/probe', { method: 'POST', body: data }),
  reconcile: () => request('/system/task-reconcile', { method: 'POST' }),
}

// ---------------------------------------------------------------- SSE

/**
 * 订阅任务事件流。
 *
 * 后端按事件类型发送具名 SSE（event: <type>），类型集合动态且未在前端枚举，
 * 故不用 EventSource（无法通配监听具名事件）而改为解析原始流，保证任何新事件
 * 类型都能被接收。返回的 close() 用于组件卸载时中断。
 */
export function openTaskStream({ taskId, onEvent, onOpen, onError, signal }) {
  const controller = new AbortController()
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  const url = `${BASE}/tasks/${taskId}/stream?token=${encodeURIComponent(tokenStore.get())}`

  ;(async () => {
    try {
      const res = await fetch(url, {
        signal: controller.signal,
        headers: { Accept: 'text/event-stream' },
      })
      if (!res.ok) throw new Error(`SSE 连接失败（HTTP ${res.status}）`)
      onOpen?.()

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let sep
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          const { event, data } = parseSseBlock(block)
          if (!data) continue
          try {
            onEvent?.(JSON.parse(data), event)
          } catch {
            /* 非 JSON 载荷（如心跳 {}）忽略 */
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') onError?.(err)
    }
  })()

  return {
    close() {
      signal?.removeEventListener('abort', onAbort)
      controller.abort()
    },
  }
}

function parseSseBlock(block) {
  let event = 'message'
  const dataLines = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  return { event, data: dataLines.join('\n') }
}
