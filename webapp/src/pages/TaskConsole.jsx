import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Badge,
  Button,
  CodeBlock,
  EmptyState,
  Input,
  KV,
  LoadingBlock,
  ProgressBar,
  StatusDot,
  Tabs,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi, usePolling } from '../hooks.js'
import { approvalsApi, openTaskStream, tasksApi } from '../api.js'
import AuthPanel from '../components/AuthPanel.jsx'
import { categoryOf, describeEvent, eventTone, EVENT_CATEGORY, typeLabel } from '../events.js'
import { TASK_STATUS, ago, duration, dt, num, statusOf, truncate } from '../format.js'

const FILTERS = [{ value: 'all', label: '全部' }].concat(
  Object.entries(EVENT_CATEGORY).map(([value, def]) => ({ value, label: def.label })),
)

export default function TaskConsole() {
  const { taskId } = useParams()
  const navigate = useNavigate()
  const toast = useToast()

  const fetchTask = useCallback(() => tasksApi.get(taskId), [taskId])
  const { data: task, loading, reload: reloadTask } = useApi(fetchTask, [taskId])

  const [live, setLive] = useState(null)
  const [streamEvents, setStreamEvents] = useState([])
  const [connected, setConnected] = useState(false)
  const [filter, setFilter] = useState('all')
  const [tab, setTab] = useState('live')
  const seqRef = useRef(0)

  // 实时状态聚合（计划 / 智能体 / 阶段）走轮询，保证断连后仍可恢复。
  const pollLive = useCallback(async () => {
    try {
      setLive(await tasksApi.live(taskId))
    } catch {
      /* 轮询失败保留上一次快照 */
    }
  }, [taskId])

  useEffect(() => {
    pollLive()
  }, [pollLive])

  const active = task?.status === 'running' || task?.status === 'queued'
  usePolling(pollLive, 3000, active)

  // SSE 实时事件流
  useEffect(() => {
    if (!taskId) return undefined
    setStreamEvents([])
    const stream = openTaskStream({
      taskId,
      onOpen: () => setConnected(true),
      onEvent: (evt) => {
        seqRef.current += 1
        const item = {
          key: `${taskId}-${seqRef.current}`,
          type: evt.type,
          payload: evt.payload || {},
          ts: new Date().toISOString(),
        }
        setStreamEvents((prev) => [...prev.slice(-399), item])
      },
      onError: () => setConnected(false),
    })
    return () => {
      stream.close()
      setConnected(false)
    }
  }, [taskId])

  const fetchReplay = useCallback(() => tasksApi.events(taskId, { limit: 500 }), [taskId])
  const { data: replay, loading: replayLoading, reload: reloadReplay } = useApi(
    fetchReplay,
    [taskId],
    { enabled: false },
  )

  useEffect(() => {
    if (tab === 'replay') reloadReplay().catch(() => {})
  }, [tab, reloadReplay])

  const fetchApprovals = useCallback(() => approvalsApi.list({ task_id: taskId, status: 'pending' }), [taskId])
  const { data: approvals, reload: reloadApprovals } = useApi(fetchApprovals, [taskId])
  usePolling(reloadApprovals, 5000, active)

  const [run, pending] = useAction()

  const onCancel = () =>
    run(async () => {
      try {
        await tasksApi.cancel(taskId)
        toast('已发送取消请求', 'success')
        reloadTask()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onRerun = () =>
    run(async () => {
      try {
        await tasksApi.enqueue(taskId)
        toast('任务已重新入队', 'success')
        reloadTask()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onDownload = () =>
    run(async () => {
      try {
        const md = await tasksApi.reportMarkdown(taskId)
        const blob = new Blob([typeof md === 'string' ? md : JSON.stringify(md, null, 2)], {
          type: 'text/markdown;charset=utf-8',
        })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `${task?.name || 'report'}.md`
        a.click()
        URL.revokeObjectURL(url)
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const rawEvents = useMemo(() => {
    if (tab === 'replay') {
      return (replay?.events || []).map((e) => ({
        key: `r-${e.seq}`,
        type: e.type,
        payload: e.payload || {},
        ts: e.created_at,
      }))
    }
    if (streamEvents.length) return streamEvents
    return (live?.recent_events || []).map((e) => ({
      key: `p-${e.seq}`,
      type: e.type,
      payload: e.payload || {},
      ts: e.created_at,
    }))
  }, [tab, replay, streamEvents, live])

  const events = useMemo(
    () => (filter === 'all' ? rawEvents : rawEvents.filter((e) => categoryOf(e.type) === filter)),
    [rawEvents, filter],
  )

  if (loading && !task) return <LoadingBlock text="加载任务…" />
  if (!task)
    return (
      <EmptyState
        icon="alert"
        title="任务不存在或无权访问"
        desc="请返回任务列表重新选择。"
        action={
          <Link to="/tasks">
            <Button variant="primary">返回任务列表</Button>
          </Link>
        }
      />
    )

  const status = statusOf(TASK_STATUS, task.status)
  const plan = live?.plan
  const agents = live?.agents || []
  const agentWork = live?.agent_work || {}

  return (
    <div className="stack" style={{ height: '100%', minHeight: 0 }}>
      <div className="console-head">
        <button className="btn btn--ghost btn--icon" onClick={() => navigate('/tasks')} aria-label="返回">
          <Icon name="arrowLeft" size={17} />
        </button>
        <div style={{ minWidth: 0 }}>
          <div className="console-head__title">
            <StatusDot status={task.status} />
            {task.name}
            <Badge tone={status.tone}>{status.label}</Badge>
            {task.kind === 'probe' && <Badge tone="info">链路探针</Badge>}
          </div>
          <div className="console-head__meta">
            <span className="mono">{task.id.slice(0, 8)}</span>
            {live?.target_url && <span className="mono">{truncate(live.target_url, 48)}</span>}
            {live?.current_phase && <span>阶段：{live.current_phase}</span>}
            {live?.current_agent && <span>当前：{live.current_agent}</span>}
            <span>耗时 {duration(task.started_at, task.finished_at)}</span>
            <span className="mono">{num(task.total_tokens)} tok</span>
            {live?.last_event_at && <span>最后活跃 {ago(live.last_event_at)}</span>}
          </div>
        </div>
        <div className="console-head__actions">
          <span className={`stream-state ${connected ? 'stream-state--live' : ''}`}>
            <StatusDot status={connected ? 'online' : 'offline'} />
            {connected ? '实时流已连接' : '实时流未连接'}
          </span>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => { reloadTask(); pollLive(); }}>
            刷新
          </Button>
          <Button size="sm" variant="ghost" icon="download" onClick={onDownload} disabled={pending}>
            报告
          </Button>
          {active && (
            <Button size="sm" variant="danger" icon="stop" onClick={onCancel} disabled={pending}>
              取消任务
            </Button>
          )}
          {!active && (task.status === 'failed' || task.status === 'cancelled') && (
            <Button size="sm" variant="primary" icon="play" onClick={onRerun} disabled={pending}>
              继续任务
            </Button>
          )}
        </div>
      </div>

      {task?.auth_mode && task.auth_mode !== 'none' && (
        <AuthPanel taskId={taskId} />
      )}

      <div className="console" style={{ flex: 1, minHeight: 0 }}>
        {/* 左栏：执行计划 */}
        <div className="console__col console__col--left">
          <div className="console__col-head">
            <Icon name="list" size={13} />
            执行计划
            {plan?.total ? <span className="tab__count">{plan.completed}/{plan.total}</span> : null}
          </div>
          <div className="console__col-body">
            <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border-subtle)' }}>
              <KV
                pairs={[
                  ['目标', truncate(live?.objective || task.objective, 60)],
                  ['模式', task.agent_mode, true],
                  ['模型', task.model || '默认', true],
                  ['最大轮次', String(task.max_turns), true],
                  ['发送 Tokens', num(task.prompt_tokens), true],
                  ['接收 Tokens', num(task.completion_tokens), true],
                  ['创建', dt(task.created_at), true],
                  ['开始', dt(task.started_at), true],
                  ['结束', dt(task.finished_at), true],
                ]}
              />
            </div>
            {plan?.total ? (
              <>
                <div style={{ padding: '12px 16px' }}>
                  <ProgressBar value={plan.completed} total={plan.total} tone={plan.failed ? 'danger' : undefined} />
                </div>
                {plan.steps.map((s) => (
                  <div key={s.step_id} className={`plan-step plan-step--${s.status}`}>
                    <span className="plan-step__icon">
                      {s.status === 'completed' ? (
                        <Icon name="check" size={13} style={{ color: 'var(--accent)' }} />
                      ) : s.status === 'running' ? (
                        <span className="spinner" style={{ width: 12, height: 12 }} />
                      ) : s.status === 'failed' ? (
                        <Icon name="x" size={13} style={{ color: 'var(--danger)' }} />
                      ) : (
                        <Icon name="clock" size={13} style={{ color: 'var(--text-disabled)' }} />
                      )}
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <div className="plan-step__title">{s.title}</div>
                      {s.detail && <div className="plan-step__meta">{truncate(s.detail, 80)}</div>}
                    </div>
                  </div>
                ))}
              </>
            ) : (
              <EmptyState icon="list" title="暂无执行计划" desc="智能体开始规划后，结构化步骤会实时出现在这里。" />
            )}
          </div>
        </div>

        {/* 中栏：事件流 */}
        <div className="console__col">
          <div className="console__col-head" style={{ padding: 0, borderBottom: 'none' }}>
            <Tabs
              value={tab}
              onChange={setTab}
              tabs={[
                { value: 'live', label: '实时事件流' },
                { value: 'replay', label: '全量回放' },
              ]}
            />
          </div>
          <div className="event-filters">
            {FILTERS.map((f) => (
              <button
                key={f.value}
                className={`event-chip ${filter === f.value ? 'event-chip--active' : ''}`}
                onClick={() => setFilter(f.value)}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="console__col-body" style={{ padding: '0 16px' }}>
            {tab === 'replay' && replayLoading ? (
              <LoadingBlock />
            ) : events.length ? (
              <div className="timeline">
                {events.map((e) => {
                  const tone = eventTone(e.type, e.payload)
                  return (
                    <div className="timeline__item" key={e.key}>
                      <span className="timeline__marker">
                        <Icon
                          name={tone === 'danger' ? 'alert' : 'chevronRight'}
                          size={11}
                          style={{ color: tone === 'danger' ? 'var(--danger)' : 'var(--text-disabled)' }}
                        />
                      </span>
                      <div className="timeline__body">
                        <div className="timeline__head">
                          <span
                            className="timeline__type"
                            style={tone === 'danger' ? { color: 'var(--danger)' } : tone === 'accent' ? { color: 'var(--accent)' } : undefined}
                          >
                            {typeLabel(e.type)}
                          </span>
                          {e.payload?.agent_name && (
                            <span className="timeline__time">{e.payload.agent_name}</span>
                          )}
                          <span className="timeline__time">{e.ts ? ago(e.ts) : ''}</span>
                        </div>
                        <div className="timeline__text">{describeEvent(e.type, e.payload)}</div>
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <EmptyState
                icon="activity"
                title="暂无事件"
                desc={active ? '等待智能体产生第一条事件…' : '该任务当前没有可展示的事件记录。'}
              />
            )}
          </div>
          <InstructionBar taskId={taskId} disabled={!active} onSent={reloadTask} />
        </div>

        {/* 右栏：智能体与审批 */}
        <div className="console__col console__col--right">
          <div className="console__col-head">
            <Icon name="cpu" size={13} />
            智能体
            {live?.pending_instructions ? (
              <span className="tab__count">{live.pending_instructions} 条指令待消费</span>
            ) : null}
          </div>
          <div className="console__col-body">
            {(approvals || []).length > 0 && (
              <div style={{ padding: 16, borderBottom: '1px solid var(--border-subtle)' }}>
                <div className="overview-alert overview-alert--warning" style={{ marginBottom: 12 }}>
                  <span className="overview-alert__icon">
                    <Icon name="alert" size={15} />
                  </span>
                  <div className="overview-alert__body">
                    <div className="overview-alert__title">{approvals.length} 项高危操作待审批</div>
                    <div className="overview-alert__desc">任务已暂停，等待操作员决策。</div>
                  </div>
                </div>
                <Link to="/approvals">
                  <Button variant="primary" size="sm" block>
                    前往审批
                  </Button>
                </Link>
              </div>
            )}

            {agents.length ? (
              agents.map((a) => (
                <div className="agent-card" key={a.name}>
                  <div className="agent-card__head">
                    <StatusDot status={a.status === 'running' ? 'running' : a.status === 'done' ? 'completed' : 'failed'} />
                    <span className="agent-card__name">{a.name}</span>
                    <Badge tone={a.status === 'running' ? 'accent' : a.status === 'done' ? 'neutral' : 'danger'}>
                      {a.status}
                    </Badge>
                    <span className="agent-card__role">{a.role}</span>
                  </div>
                  <div className="agent-card__work">
                    {(agentWork[a.name] || []).slice(-4).reverse().map((w, i) => (
                      <div className={`work-line ${w.success === false || w.error ? 'work-line--error' : ''}`} key={i}>
                        <span className="work-line__tool">{w.tool}</span>
                        <span className="work-line__text">{truncate(w.error || w.result || w.args || '', 120)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <EmptyState icon="cpu" title="尚无智能体运行" desc="任务启动后，参与的智能体及其工作会在此展示。" />
            )}
          </div>

          {(task.result || task.error) && (
            <div style={{ borderTop: '1px solid var(--border-subtle)', padding: 16, maxHeight: '34%', overflow: 'auto' }}>
              <div className="stat__label" style={{ marginBottom: 8 }}>
                {task.error ? '错误信息' : '最终结论'}
              </div>
              {task.error ? (
                <CodeBlock>{task.error}</CodeBlock>
              ) : (
                <div style={{ fontSize: 13, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>
                  {task.result}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function InstructionBar({ taskId, disabled, onSent }) {
  const toast = useToast()
  const [text, setText] = useState('')
  const [run, pending] = useAction()

  const send = () =>
    run(async () => {
      const value = text.trim()
      if (!value) return
      try {
        await tasksApi.instruct(taskId, value)
        setText('')
        toast('指令已提交，将在下一个检查点生效', 'success')
        onSent?.()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  return (
    <div className="instr-bar">
      <Input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && send()}
        placeholder={disabled ? '仅运行中的任务可追加指令' : '向主控 Agent 追加指令，回车发送…'}
        disabled={disabled || pending}
      />
      <Button variant="primary" icon="send" onClick={send} disabled={disabled || pending || !text.trim()}>
        发送
      </Button>
    </div>
  )
}
