import { useCallback, useEffect, useState } from 'react'
import { taskAuthApi } from '../api.js'
import { useToast } from './ui.jsx'

const STATUS_LABEL = {
  none: '未配置',
  pending: '待处理',
  running: '认证中',
  success: '已认证',
  failed: '失败',
  mfa: '需人工认证',
}
const STATUS_TONE = {
  success: 'success',
  failed: 'danger',
  mfa: 'warning',
  running: 'accent',
}

/**
 * 认证前置面板：展示任务认证状态（仅账号密码自动认证分支）。
 * [DISABLED 2026-09-01] 手动登录分支（MFA 人工流程）已搁置，见 .ai/roadmap.md MFA 演进计划。
 */
export default function AuthPanel({ taskId, onStatusChange }) {
  const toast = useToast()
  const [status, setStatus] = useState(null)
  // [DISABLED 2026-09-01] 手动登录分支搁置，busy 仅手动交互使用
  // const [busy, setBusy] = useState(false)
  // [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
  // const [started, setStarted] = useState(false)
  // const [snap, setSnap] = useState(null)
  // const [navUrl, setNavUrl] = useState('')
  // const [sel, setSel] = useState('')
  // const [text, setText] = useState('')
  // const [keySel, setKeySel] = useState('input')

  const refresh = useCallback(async () => {
    try {
      const s = await taskAuthApi.status(taskId)
      setStatus(s)
    } catch (e) {
      /* 状态查询失败静默 */
    }
  }, [taskId])

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  // [DISABLED 2026-09-01] 手动浏览器运行期间轮询截图（手动分支搁置）
  // useEffect(() => {
  //   if (!started) return
  //   const timer = setInterval(async () => {
  //     try {
  //       const s = await taskAuthApi.manualSnapshot(taskId)
  //       setSnap(s)
  //     } catch {
  //       setStarted(false)
  //       clearInterval(timer)
  //       refresh()
  //     }
  //   }, 2000)
  //   return () => clearInterval(timer)
  // }, [started, taskId, refresh])

  // [DISABLED 2026-09-01] 手动登录：启动 / 指令 / 捕获 / 销毁（手动分支搁置）
  // const start = async () => {
  //   setBusy(true)
  //   try {
  //     const s = await taskAuthApi.manualStart(taskId)
  //     setStarted(true)
  //     setSnap(s)
  //     setStatus((st) => ({ ...(st || {}), auth_status: 'running', manual_session_active: true }))
  //     toast('手动登录浏览器已启动，请完成登录后点击「完成捕获」', 'success')
  //   } catch (e) {
  //     toast(e.message, 'error')
  //   } finally {
  //     setBusy(false)
  //   }
  // }
  //
  // const send = async (action) => {
  //   setBusy(true)
  //   try {
  //     await taskAuthApi.manualAction(taskId, action)
  //     const s = await taskAuthApi.manualSnapshot(taskId)
  //     setSnap(s)
  //   } catch (e) {
  //     toast(e.message, 'error')
  //   } finally {
  //     setBusy(false)
  //   }
  // }
  //
  // const capture = async () => {
  //   setBusy(true)
  //   try {
  //     const r = await taskAuthApi.manualCapture(taskId)
  //     setStarted(false)
  //     setSnap(null)
  //     toast(`会话已保存（profile=${r.profile}）`, 'success')
  //     refresh()
  //     onStatusChange?.()
  //   } catch (e) {
  //     toast(e.message, 'error')
  //   } finally {
  //     setBusy(false)
  //   }
  // }
  //
  // const abort = async () => {
  //   setBusy(true)
  //   try {
  //     await taskAuthApi.manualAbort(taskId)
  //     setStarted(false)
  //     setSnap(null)
  //     toast('已销毁手动浏览器', 'info')
  //     refresh()
  //   } catch (e) {
  //     toast(e.message, 'error')
  //   } finally {
  //     setBusy(false)
  //   }
  // }

  const tag = (status || {}).auth_status || 'none'
  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div className="panel__head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>
          认证前置{' '}
          <span className={`badge badge--${STATUS_TONE[tag] || 'neutral'}`}>{STATUS_LABEL[tag] || tag}</span>
          {status?.auth_profile ? <span className="muted"> · profile={status.auth_profile}</span> : null}
        </span>
        <span className="muted" style={{ fontSize: 12 }}>
          {status?.auth_mode === 'auto' && status?.auth_username ? `账号：${status.auth_username}` : ''}
          {status?.auth_error ? ` · ${status.auth_error}` : ''}
        </span>
      </div>

      {/*
      [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
      mode === 'manual' && !started && (
        <div style={{ padding: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className="muted" style={{ flex: 1 }}>
            适用于 MFA / 短信 / 滑块 / SSO 等无法自动化的登录场景：点击启动后，在后端浏览器中手动完成登录，再捕获会话。
          </span>
          <Button variant="primary" size="sm" loading={busy} onClick={start}>
            启动手动登录
          </Button>
        </div>
      )

      started && (
        <div style={{ padding: 12 }}>
          {snap?.screenshot_base64 ? (
            <div>
              <div className="muted" style={{ marginBottom: 6 }}>
                {snap.title || ''} — {snap.url || ''}
              </div>
              <img
                src={`data:image/png;base64,${snap.screenshot_base64}`}
                alt="浏览器画面"
                style={{ width: '100%', maxHeight: 420, objectFit: 'contain', background: '#fff', borderRadius: 6, border: '1px solid var(--border-subtle)' }}
              />
            </div>
          ) : (
            <div className="muted" style={{ padding: '16px 0' }}>正在加载浏览器画面…</div>
          )}

          <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            <Input
              style={{ flex: '2 1 260px' }}
              value={navUrl}
              onChange={(e) => setNavUrl(e.target.value)}
              placeholder="跳转地址 https://…"
            />
            <Button variant="secondary" size="sm" loading={busy} onClick={() => send({ action_type: 'navigate', url: navUrl })}>
              跳转
            </Button>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <Input
              style={{ flex: '1 1 160px' }}
              className="mono"
              value={sel}
              onChange={(e) => setSel(e.target.value)}
              placeholder="选择器 input[name='user']"
            />
            <Input style={{ flex: '1 1 160px' }} value={text} onChange={(e) => setText(e.target.value)} placeholder="填充文本" />
            <Button variant="secondary" size="sm" loading={busy} onClick={() => send({ action_type: 'fill', selector: sel, text })}>
              填充
            </Button>
            <Button variant="secondary" size="sm" loading={busy} onClick={() => send({ action_type: 'click', selector: sel })}>
              点击
            </Button>
            <Button variant="secondary" size="sm" loading={busy} onClick={() => send({ action_type: 'press', selector: sel || keySel, key: 'Enter' })}>
              回车
            </Button>
            <Button variant="secondary" size="sm" loading={busy} onClick={() => send({ action_type: 'wait', ms: 1500 })}>
              等待1.5s
            </Button>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <Button variant="primary" loading={busy} onClick={capture}>
              完成捕获并保存会话
            </Button>
            <Button variant="ghost" loading={busy} onClick={abort}>
              销毁
            </Button>
          </div>
        </div>
      )
      */}
    </div>
  )
}
