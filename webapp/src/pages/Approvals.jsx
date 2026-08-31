import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  Input,
  LoadingBlock,
  Select,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi, usePolling } from '../hooks.js'
import { approvalsApi } from '../api.js'
import { APPROVAL_STATUS, dt, dtShort, shortId, statusOf, truncate } from '../format.js'

const FILTERS = [
  { value: 'pending', label: '待审批' },
  { value: 'approved', label: '已批准' },
  { value: 'rejected', label: '已拒绝' },
  { value: '', label: '全部' },
]

export default function Approvals() {
  const navigate = useNavigate()
  const toast = useToast()
  const [status, setStatus] = useState('pending')
  const [reasonFor, setReasonFor] = useState(null)
  const [reason, setReason] = useState('')

  const fetcher = useCallback(() => approvalsApi.list({ status: status || undefined, limit: 200 }), [status])
  const { data, loading, reload } = useApi(fetcher, [status])
  usePolling(reload, 10000, status === 'pending')

  const [run, pending] = useAction()

  const list = data ?? []
  const pendingItems = useMemo(() => list.filter((a) => a.status === 'pending'), [list])
  const decidedItems = useMemo(() => list.filter((a) => a.status !== 'pending'), [list])

  const decide = (item, decision, withReason) =>
    run(async () => {
      try {
        await approvalsApi.decide(item.id, decision, withReason?.trim() || null)
        toast(decision === 'approve' ? '已批准，任务继续执行' : '已拒绝，该操作被中止', 'success')
        setReasonFor(null)
        setReason('')
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  return (
    <>
      <PageHead
        title="高危审批"
        icon="shieldCheck"
        desc="hacker 模式下，智能体命中的高危操作会在此暂停等待人工决策。"
        actions={
          <>
            <Select options={FILTERS} value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 120 }} />
            <Button icon="refresh" onClick={reload} disabled={loading}>
              刷新
            </Button>
          </>
        }
      />

      {loading ? (
        <LoadingBlock />
      ) : pendingItems.length ? (
        <div className="stack">
          {pendingItems.map((a) => (
            <div className="approval-card" key={a.id}>
              <div className="approval-card__head">
                <Icon name="alert" size={16} style={{ color: 'var(--warning)' }} />
                <span className="approval-card__tool">{a.tool_name}</span>
                {a.agent_name && <Badge tone="outline">{a.agent_name}</Badge>}
                <div style={{ flex: 1 }} />
                <span className="mono muted" style={{ fontSize: 12 }}>
                  {dt(a.created_at)}
                </span>
              </div>

              <div className="approval-card__grid">
                <div className="kv">
                  <span className="kv__k">审批 ID</span>
                  <span className="kv__v kv__v--mono">{shortId(a.id)}</span>
                  <span className="kv__k">关联任务</span>
                  <span className="kv__v kv__v--mono">{shortId(a.task_id)}</span>
                </div>
              </div>

              <div className="approval-card__args">
                <span className="stat__label">调用参数</span>
                <CodeBlock>{safeJson(a.tool_args)}</CodeBlock>
              </div>

              {reasonFor === a.id ? (
                <div className="reason-input">
                  <Input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="决策理由（可选）"
                    autoFocus
                  />
                  <Button variant="ghost" onClick={() => { setReasonFor(null); setReason(''); }}>
                    取消
                  </Button>
                  <Button variant="danger" loading={pending} onClick={() => decide(a, 'reject', reason)}>
                    确认拒绝
                  </Button>
                  <Button variant="primary" loading={pending} onClick={() => decide(a, 'approve', reason)}>
                    确认批准
                  </Button>
                </div>
              ) : (
                <div className="approval-card__actions">
                  <Button variant="primary" icon="check" loading={pending} onClick={() => decide(a, 'approve')}>
                    批准执行
                  </Button>
                  <Button variant="danger" icon="ban" loading={pending} onClick={() => setReasonFor(a.id)}>
                    拒绝
                  </Button>
                  <Button variant="ghost" icon="terminal" onClick={() => navigate(`/tasks/${a.task_id}`)}>
                    查看任务
                  </Button>
                  <div style={{ flex: 1 }} />
                  <Badge tone="warning">任务已暂停</Badge>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : status === 'pending' ? (
        <Card flush>
          <EmptyState
            icon="shieldCheck"
            title="没有待审批的操作"
            desc="智能体在 hacker 模式下遇到高危工具时会在此排队，等待你批准或拒绝。"
          />
        </Card>
      ) : null}

      {decidedItems.length > 0 && (
        <Card flush title="历史决策" icon="scroll" style={{ marginTop: 24 }}>
          <DataTable
            columns={[
              {
                key: 'tool_name',
                title: '工具',
                render: (a) => <span className="mono">{a.tool_name}</span>,
              },
              { key: 'agent_name', title: '智能体', render: (a) => a.agent_name || '—' },
              {
                key: 'status',
                title: '决策',
                width: 100,
                render: (a) => {
                  const s = statusOf(APPROVAL_STATUS, a.status)
                  return <Badge tone={s.tone}>{s.label}</Badge>
                },
              },
              {
                key: 'decision_reason',
                title: '理由',
                render: (a) => <span className="muted">{truncate(a.decision_reason, 60) || '—'}</span>,
              },
              { key: 'created_at', title: '发起时间', width: 128, render: (a) => <span className="mono" style={{ fontSize: 12 }}>{dtShort(a.created_at)}</span> },
              {
                key: 'actions',
                title: '',
                width: 96,
                render: (a) => (
                  <Button size="sm" variant="ghost" onClick={() => navigate(`/tasks/${a.task_id}`)}>
                    任务
                  </Button>
                ),
              },
            ]}
            rows={decidedItems}
          />
        </Card>
      )}
    </>
  )
}

function safeJson(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}
