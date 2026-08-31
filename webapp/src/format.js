/** 展示层格式化与枚举映射。 */

export const TASK_STATUS = {
  pending: { label: '待派发', tone: 'neutral' },
  queued: { label: '排队中', tone: 'info' },
  running: { label: '运行中', tone: 'accent' },
  completed: { label: '已完成', tone: 'accent' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'neutral' },
}

export const APPROVAL_STATUS = {
  pending: { label: '待审批', tone: 'warning' },
  approved: { label: '已批准', tone: 'accent' },
  rejected: { label: '已拒绝', tone: 'danger' },
  expired: { label: '已过期', tone: 'neutral' },
}

export const THREAT_STATUS = {
  suspected: { label: '疑似', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'danger' },
  exploited: { label: '已利用', tone: 'accent' },
  remediated: { label: '已修复', tone: 'info' },
}

export const OUTCOME = {
  allowed: { label: '允许', tone: 'accent' },
  success: { label: '成功', tone: 'accent' },
  ok: { label: '成功', tone: 'accent' },
  denied: { label: '拒绝', tone: 'danger' },
  blocked: { label: '拦截', tone: 'danger' },
  failed: { label: '失败', tone: 'danger' },
  error: { label: '错误', tone: 'danger' },
}

export function statusOf(map, v) {
  return map[String(v || '').toLowerCase()] || { label: String(v || '—'), tone: 'neutral' }
}

// ---------------------------------------------------------------- 数字 / 时间

export function num(v) {
  return (v ?? 0).toLocaleString('zh-CN')
}

export function bytes(v) {
  if (v === null || v === undefined) return '—'
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let i = -1
  let n = Number(v)
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n.toFixed(n >= 10 || i === -1 ? 0 : 1)} ${units[i]}`
}

export function money(v, currency = 'USD') {
  const symbol = currency === 'CNY' ? '¥' : currency === 'USD' ? '$' : ''
  const n = Number(v || 0)
  if (n !== 0 && Math.abs(n) < 0.01) return `< ${symbol}0.01`
  return `${symbol}${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 后端返回 ISO8601（带 tz），统一按本地时区渲染。 */
export function dt(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function dtShort(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function ago(v) {
  if (!v) return '—'
  const t = new Date(v).getTime()
  if (Number.isNaN(t)) return String(v)
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 5) return '刚刚'
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

export function duration(start, end) {
  if (!start) return '—'
  const a = new Date(start).getTime()
  const b = end ? new Date(end).getTime() : Date.now()
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return '—'
  const s = Math.floor((b - a) / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

export function shortId(v) {
  const s = String(v || '')
  return s.length > 8 ? s.slice(0, 8) : s
}

export function truncate(s, n = 120) {
  const str = String(s ?? '')
  return str.length > n ? `${str.slice(0, n)}…` : str
}

export function pct(v) {
  const n = Number(v || 0)
  return `${Math.round(n * 100)}%`
}
