import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  LoadingBlock,
  SeverityTag,
  Stat,
  StatusDot,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useApi, usePolling } from '../hooks.js'
import { approvalsApi, systemApi, targetsApi, tasksApi } from '../api.js'
import { TASK_STATUS, ago, dtShort, num, shortId, statusOf, truncate } from '../format.js'

export default function Dashboard() {
  const navigate = useNavigate()

  const fetchTasks = useCallback(() => tasksApi.list(), [])
  const { data: tasks, loading: loadingTasks, reload: reloadTasks } = useApi(fetchTasks, [])
  const fetchTargets = useCallback(() => targetsApi.list(), [])
  const { data: targets } = useApi(fetchTargets, [])
  const fetchApprovals = useCallback(() => approvalsApi.list({ status: 'pending', limit: 100 }), [])
  const { data: approvals, reload: reloadApprovals } = useApi(fetchApprovals, [])
  const fetchWorker = useCallback(() => systemApi.workerStatus(), [])
  const { data: worker } = useApi(fetchWorker, [])

  const list = tasks ?? []
  const hasActive = list.some((t) => t.status === 'running' || t.status === 'queued')
  usePolling(reloadTasks, 8000, hasActive)

  const stats = useMemo(() => {
    const real = list.filter((t) => t.kind !== 'probe')
    return {
      total: real.length,
      running: real.filter((t) => t.status === 'running').length,
      queued: real.filter((t) => t.status === 'queued').length,
      completed: real.filter((t) => t.status === 'completed').length,
      failed: real.filter((t) => t.status === 'failed').length,
      tokens: real.reduce((s, t) => s + (t.total_tokens || 0), 0),
    }
  }, [list])

  const active = list.filter((t) => t.status === 'running' || t.status === 'queued')
  const recent = list.slice(0, 8)
  const pendingApprovals = approvals ?? []

  const findings = useRecentFindings(list)

  const targetMap = useMemo(() => new Map((targets ?? []).map((t) => [t.id, t])), [targets])
  const workerOnline = worker?.available && worker?.online

  return (
    <>
      <PageHead
        title="总览"
        icon="dashboard"
        desc="平台运行态势一屏可见：任务执行、待审批高危操作与最新漏洞发现。"
        actions={
          <Button icon="refresh" onClick={() => { reloadTasks(); reloadApprovals(); }} disabled={loadingTasks}>
            刷新
          </Button>
        }
      />

      <div className="grid grid--stats" style={{ marginBottom: 24 }}>
        <Stat label="任务总数" icon="tasks" value={num(stats.total)} hint={`已完成 ${stats.completed} · 失败 ${stats.failed}`} />
        <Stat
          label="运行中"
          icon="activity"
          tone="accent"
          value={num(stats.running)}
          hint={stats.queued ? `另有 ${stats.queued} 个排队中` : '无排队任务'}
        />
        <Stat
          label="待审批高危"
          icon="shieldCheck"
          tone={pendingApprovals.length ? 'warning' : undefined}
          value={num(pendingApprovals.length)}
          hint={pendingApprovals.length ? '需要操作员决策' : '暂无待处理'}
          onClick={pendingApprovals.length ? () => navigate('/approvals') : undefined}
        />
        <Stat
          label="Worker"
          icon="cpu"
          tone={workerOnline ? 'accent' : 'danger'}
          value={workerOnline ? '在线' : '离线'}
          hint={workerOnline ? `队列深度 ${worker.queue_depth ?? 0}` : '任务将无法被消费'}
          onClick={() => navigate('/health')}
        />
      </div>

      {pendingApprovals.length > 0 && (
        <div className="overview-alert overview-alert--warning" style={{ marginBottom: 24 }}>
          <span className="overview-alert__icon">
            <Icon name="alert" size={16} />
          </span>
          <div className="overview-alert__body">
            <div className="overview-alert__title">
              有 {pendingApprovals.length} 项高危操作等待审批，相关任务已暂停
            </div>
            <div className="overview-alert__desc">
              涉及：{pendingApprovals.slice(0, 3).map((a) => a.tool_name).join('、')}
              {pendingApprovals.length > 3 ? ' 等' : ''}
            </div>
          </div>
          <Button variant="primary" size="sm" onClick={() => navigate('/approvals')}>
            前往审批
          </Button>
        </div>
      )}

      <div className="grid grid--2" style={{ marginBottom: 24 }}>
        <Card
          title="活跃任务"
          icon="activity"
          flush
          actions={
            <Link className="link-pill" to="/tasks">
              全部任务
              <Icon name="chevronRight" size={13} />
            </Link>
          }
        >
          {loadingTasks ? (
            <LoadingBlock />
          ) : active.length ? (
            <div className="mini-list">
              {active.map((t) => (
                <button
                  key={t.id}
                  className="mini-item"
                  style={{ width: '100%', textAlign: 'left' }}
                  onClick={() => navigate(`/tasks/${t.id}`)}
                >
                  <StatusDot status={t.status} />
                  <span className="mini-item__main">
                    <span className="mini-item__title">{t.name}</span>
                    <span className="mini-item__meta">
                      <span>{targetMap.get(t.target_id)?.name || shortId(t.target_id)}</span>
                      <span>·</span>
                      <span>{ago(t.created_at)}启动</span>
                      <span>·</span>
                      <span className="mono">{num(t.total_tokens)} tok</span>
                    </span>
                  </span>
                  <Icon name="chevronRight" size={14} />
                </button>
              ))}
            </div>
          ) : (
            <EmptyState icon="activity" title="当前没有运行中的任务" desc="创建任务后，智能体将在授权范围内自主执行测试。" />
          )}
        </Card>

        <Card title="最新漏洞发现" icon="bug" flush>
          {findings.length ? (
            <div className="mini-list">
              {findings.slice(0, 8).map((f) => (
                <button
                  key={f.id}
                  className="mini-item"
                  style={{ width: '100%', textAlign: 'left' }}
                  onClick={() => navigate(`/tasks/${f.task_id}`)}
                >
                  <SeverityTag value={f.severity} />
                  <span className="mini-item__main">
                    <span className="mini-item__title">{f.title}</span>
                    <span className="mini-item__meta">
                      <span>{f.task_name}</span>
                      {f.cwe && (
                        <>
                          <span>·</span>
                          <span className="mono">{f.cwe}</span>
                        </>
                      )}
                      <span>·</span>
                      <span>{dtShort(f.created_at)}</span>
                    </span>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState icon="bug" title="暂无漏洞发现" desc="任务完成并通过验证后，确认的漏洞会出现在这里。" />
          )}
        </Card>
      </div>

      <Card title="最近任务" icon="tasks" flush>
        {loadingTasks ? (
          <LoadingBlock />
        ) : recent.length ? (
          <div className="mini-list">
            {recent.map((t) => {
              const s = statusOf(TASK_STATUS, t.status)
              return (
                <button
                  key={t.id}
                  className="mini-item"
                  style={{ width: '100%', textAlign: 'left' }}
                  onClick={() => navigate(`/tasks/${t.id}`)}
                >
                  <StatusDot status={t.status} />
                  <span className="mini-item__main">
                    <span className="mini-item__title">{t.name}</span>
                    <span className="mini-item__meta">
                      <span>{truncate(t.objective, 60)}</span>
                    </span>
                  </span>
                  <Badge tone={s.tone}>{s.label}</Badge>
                  <span className="mono muted" style={{ fontSize: 12 }}>
                    {dtShort(t.created_at)}
                  </span>
                </button>
              )
            })}
          </div>
        ) : (
          <EmptyState
            icon="tasks"
            title="还没有任务"
            desc="先登记授权目标，再下发第一个渗透测试任务。"
            action={
              <Link to="/targets">
                <Button variant="primary" icon="plus">
                  登记授权目标
                </Button>
              </Link>
            }
          />
        )}
      </Card>
    </>
  )
}

/**
 * 汇总最近完成任务中的漏洞发现。
 * 后端未提供跨任务的 findings 聚合端点，故对最近若干个已完成任务并发查询，
 * 数量有上限且仅在任务列表变化时执行一次，避免 N+1 扩散。
 */
function useRecentFindings(tasks) {
  const [findings, setFindings] = useState([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!tasks?.length) {
      setFindings([])
      return undefined
    }
    const done = tasks.filter((t) => t.status === 'completed').slice(0, 6)
    if (!done.length) {
      setFindings([])
      return undefined
    }
    let alive = true
    setLoading(true)
    Promise.all(done.map((t) => tasksApi.findings(t.id).catch(() => [])))
      .then((groups) => {
        if (!alive) return
        const merged = groups
          .flatMap((items, i) => (items || []).map((f) => ({ ...f, task_name: done[i].name })))
          .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
        setFindings(merged)
      })
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [tasks])

  return findings
}
