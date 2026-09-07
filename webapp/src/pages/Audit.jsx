import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Modal,
  SearchInput,
  Select,
} from '../components/ui.jsx'
import { useApi } from '../hooks.js'
import { auditApi } from '../api.js'
import { OUTCOME, dt, dtShort, shortId, statusOf, truncate } from '../format.js'

const LIMITS = [
  { value: '100', label: '最近 100 条' },
  { value: '300', label: '最近 300 条' },
  { value: '500', label: '最近 500 条' },
]

export default function Audit() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [outcome, setOutcome] = useState('')
  const [limit, setLimit] = useState('100')
  const [detail, setDetail] = useState(null)

  const fetcher = useCallback(() => auditApi.list({ limit: Number(limit) }), [limit])
  const { data, loading, reload } = useApi(fetcher, [limit])

  const rows = useMemo(() => {
    const kw = search.trim().toLowerCase()
    return (data ?? []).filter((e) => {
      if (outcome && e.outcome !== outcome) return false
      if (!kw) return true
      return (
        (e.action || '').toLowerCase().includes(kw) ||
        (e.actor || '').toLowerCase().includes(kw) ||
        (e.detail || '').toLowerCase().includes(kw)
      )
    })
  }, [data, search, outcome])

  // 结果枚举随业务演进，从现有数据中提取可用取值而非硬编码。
  const outcomeOptions = useMemo(() => {
    const set = new Set((data ?? []).map((e) => e.outcome).filter(Boolean))
    return [{ value: '', label: '全部结果' }, ...[...set].map((v) => ({ value: v, label: v }))]
  }, [data])

  const columns = [
    { key: 'created_at', title: '时间', width: 132, render: (e) => <span className="mono" style={{ fontSize: 12 }}>{dt(e.created_at)}</span> },
    {
      key: 'actor',
      title: '操作者',
      width: 180,
      render: (e) => <span className="mono" style={{ fontSize: 12 }}>{truncate(e.actor, 28)}</span>,
    },
    {
      key: 'action',
      title: '行为',
      render: (e) => <span className="mono">{e.action}</span>,
    },
    {
      key: 'outcome',
      title: '结果',
      width: 96,
      render: (e) => {
        const s = statusOf(OUTCOME, e.outcome)
        return <Badge tone={s.tone}>{s.label}</Badge>
      },
    },
    {
      key: 'detail',
      title: '详情',
      render: (e) => <span className="audit-detail">{e.detail || '—'}</span>,
    },
    {
      key: 'refs',
      title: '关联',
      width: 150,
      render: (e) => (
        <span className="audit-meta">
          {e.task_id ? `任务 ${shortId(e.task_id)}` : ''}
          {e.task_id && e.target_id ? ' · ' : ''}
          {e.target_id ? `目标 ${shortId(e.target_id)}` : ''}
          {!e.task_id && !e.target_id ? '—' : ''}
        </span>
      ),
    },
    {
      key: 'actions',
      title: '',
      width: 132,
      render: (e) => (
        <div className="row" onClick={(ev) => ev.stopPropagation()}>
          <Button size="sm" variant="ghost" onClick={() => setDetail(e)}>
            详情
          </Button>
          {e.task_id && (
            <Button size="sm" variant="ghost" onClick={() => navigate(`/tasks/${e.task_id}`)}>
              任务
            </Button>
          )}
        </div>
      ),
    },
  ]

  return (
    <>
      <PageHead
        title="审计日志"
        icon="scroll"
        desc="平台操作的追加式记录（哈希链防篡改）：登录、目标变更、任务调度、审批决策、护栏拦截与 Agent 高危动作。"
        actions={
          <Button icon="refresh" onClick={reload} disabled={loading}>
            刷新
          </Button>
        }
      />

      <Card
        flush
        title="操作记录"
        actions={
          <div className="filter-bar" style={{ margin: 0 }}>
            <SearchInput value={search} onChange={setSearch} placeholder="搜索行为 / 操作者 / 详情" />
            <Select options={outcomeOptions} value={outcome} onChange={(e) => setOutcome(e.target.value)} style={{ width: 130 }} />
            <Select options={LIMITS} value={limit} onChange={(e) => setLimit(e.target.value)} style={{ width: 140 }} />
          </div>
        }
      >
        <DataTable
          columns={columns}
          rows={rows}
          loading={loading}
          empty="暂无审计记录"
        />
      </Card>

      <Modal open={!!detail} title="审计事件详情" wide onClose={() => setDetail(null)}>
        {detail && (
          <div className="stack">
            <div className="row row--wrap" style={{ gap: 8 }}>
              <Badge tone={statusOf(OUTCOME, detail.outcome).tone}>{detail.outcome}</Badge>
              <span className="mono">{detail.action}</span>
            </div>
            <div className="kv">
              <span className="kv__k">时间</span>
              <span className="kv__v kv__v--mono">{dt(detail.created_at)}</span>
              <span className="kv__k">操作者</span>
              <span className="kv__v kv__v--mono">{detail.actor}</span>
              {detail.actor_id && (
                <>
                  <span className="kv__k">操作者 ID</span>
                  <span className="kv__v kv__v--mono">{detail.actor_id}</span>
                </>
              )}
              {detail.trace_id && (
                <>
                  <span className="kv__k">追溯 ID</span>
                  <span className="kv__v kv__v--mono">
                    {detail.trace_id}
                    {detail.span_id ? ` / ${detail.span_id}` : ''}
                  </span>
                </>
              )}
              <span className="kv__k">事件 ID</span>
              <span className="kv__v kv__v--mono">{detail.id}</span>
              {detail.task_id && (
                <>
                  <span className="kv__k">任务</span>
                  <span className="kv__v kv__v--mono">{detail.task_id}</span>
                </>
              )}
              {detail.target_id && (
                <>
                  <span className="kv__k">目标</span>
                  <span className="kv__v kv__v--mono">{detail.target_id}</span>
                </>
              )}
            </div>
            <div className="stack">
              <span className="stat__label">详情</span>
              <CodeBlock>{detail.detail || '—'}</CodeBlock>
            </div>
            {detail.meta && Object.keys(detail.meta).length > 0 && (
              <div className="stack">
                <span className="stat__label">元数据</span>
                <CodeBlock>{JSON.stringify(detail.meta, null, 2)}</CodeBlock>
              </div>
            )}
          </div>
        )}
      </Modal>
    </>
  )
}
