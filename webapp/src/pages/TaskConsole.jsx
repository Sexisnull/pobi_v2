import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
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
import ReplayBar from '../components/attackflow/ReplayBar.jsx'
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

  // 回放：先取 seq 区间元信息（range），再按 seq 游标分窗口取明细，
  // 长任务不一次加载全量事件。
  const [replayEvents, setReplayEvents] = useState([])
  const [replayRange, setReplayRange] = useState(null)
  const [replayLoading, setReplayLoading] = useState(false)
  const [replayDone, setReplayDone] = useState(false)
  const [cursor, setCursor] = useState(0)
  const [cursorActive, setCursorActive] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)

  const loadReplay = useCallback(async () => {
    setReplayLoading(true)
    try {
      const [range, page] = await Promise.all([
        tasksApi.eventsRange(taskId),
        tasksApi.events(taskId, { limit: 500 }),
      ])
      setReplayRange(range)
      const items = page?.events || []
      setReplayEvents(items)
      setReplayDone(items.length < 500)
      setCursor(items.length ? items.length - 1 : 0)
    } finally {
      setReplayLoading(false)
    }
  }, [taskId])

  const loadMoreReplay = useCallback(async () => {
    if (replayLoading || replayDone || !replayEvents.length) return
    setReplayLoading(true)
    try {
      const page = await tasksApi.events(taskId, {
        after_seq: replayEvents[replayEvents.length - 1].seq,
        limit: 500,
      })
      const items = page?.events || []
      setReplayEvents((prev) => [...prev, ...items])
      if (items.length < 500) setReplayDone(true)
    } finally {
      setReplayLoading(false)
    }
  }, [taskId, replayLoading, replayDone, replayEvents])

  useEffect(() => {
    setReplayEvents([])
    setReplayRange(null)
    setReplayDone(false)
    setCursor(0)
    setCursorActive(false)
    setPlaying(false)
  }, [taskId])

  useEffect(() => {
    if (tab !== 'replay' || replayEvents.length || replayLoading) return
    loadReplay().catch(() => {})
  }, [tab, replayEvents.length, replayLoading, loadReplay])

  // 播放推进：走到已加载末尾时自动续页，全部加载完则停止
  useEffect(() => {
    if (tab !== 'replay' || !playing || !replayEvents.length) return undefined
    if (cursor >= replayEvents.length - 1) {
      if (replayDone) {
        setPlaying(false)
        return undefined
      }
      loadMoreReplay().catch(() => {})
      return undefined
    }
    const timer = setTimeout(() => setCursor((c) => c + 1), Math.max(60, 420 / speed))
    return () => clearTimeout(timer)
  }, [tab, playing, cursor, speed, replayEvents.length, replayDone, loadMoreReplay])

  // 从目标攻击流时间轴跳入（?at=<iso>）：切到回放并定位到该时刻前最后一条事件
  const [params, setParams] = useSearchParams()
  const atParam = params.get('at')
  useEffect(() => {
    if (!atParam || !replayEvents.length) return
    const ts = new Date(atParam).getTime()
    setTab('replay')
    setCursorActive(true)
    let idx = 0
    for (let i = 0; i < replayEvents.length; i += 1) {
      if (new Date(replayEvents[i].created_at).getTime() <= ts) idx = i
      else break
    }
    setCursor(idx)
    params.delete('at')
    setParams(params, { replace: true })
  }, [atParam, replayEvents, params, setParams])

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
      const src = cursorActive ? replayEvents.slice(0, cursor + 1) : replayEvents
      return src.map((e) => ({
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
  }, [tab, replayEvents, cursor, cursorActive, streamEvents, live])

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
            {tab === 'replay' && replayLoading && !replayEvents.length ? (
              <LoadingBlock />
            ) : events.length ? (
              <div className="timeline">
                {events.map((e) => (
                  <EventRow key={e.key} event={e} />
                ))}
              </div>
            ) : (
              <EmptyState
                icon="activity"
                title="暂无事件"
                desc={active ? '等待智能体产生第一条事件…' : '该任务当前没有可展示的事件记录。'}
              />
            )}
          </div>
          {tab === 'replay' && (
            <ReplayBar
              index={cursor}
              total={replayRange?.total ?? replayEvents.length}
              loaded={replayEvents.length}
              playing={playing}
              speed={speed}
              cursorActive={cursorActive}
              currentType={replayEvents[cursor]?.type}
              currentAt={replayEvents[cursor]?.created_at ? dtShort(replayEvents[cursor].created_at) : ''}
              loading={replayLoading}
              onToggle={() => {
                setCursorActive(true)
                setPlaying((p) => !p)
              }}
              onStep={(d) => {
                setCursorActive(true)
                setPlaying(false)
                setCursor((c) => Math.min(Math.max(0, c + d), Math.max(0, replayEvents.length - 1)))
              }}
              onSeek={(v) => {
                setCursorActive(true)
                setPlaying(false)
                setCursor(Math.max(0, Math.min(v, replayEvents.length - 1)))
                if (v >= replayEvents.length - 1 && !replayDone) loadMoreReplay().catch(() => {})
              }}
              onSpeed={setSpeed}
              onShowAll={() => {
                setCursorActive(false)
                setPlaying(false)
              }}
              onLoadMore={() => loadMoreReplay().catch(() => {})}
            />
          )}
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

function EventRow({ event }) {
  const { type, payload, ts } = event
  const tone = eventTone(type, payload)
  const [open, setOpen] = useState(false)
  const summary = describeEvent(type, payload)

  return (
    <div className={`timeline__item ${open ? 'timeline__item--open' : ''}`}>
      <button
        type="button"
        className="timeline__marker"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? '收起事件详情' : '展开事件详情'}
      >
        <Icon
          name={open ? 'chevronDown' : tone === 'danger' ? 'alert' : 'chevronRight'}
          size={11}
          style={{ color: tone === 'danger' ? 'var(--danger)' : open ? 'var(--accent)' : 'var(--text-disabled)' }}
        />
      </button>
      <div className="timeline__body">
        <div className="timeline__head">
          <button
            type="button"
            className="timeline__type"
            onClick={() => setOpen((v) => !v)}
            style={
              tone === 'danger'
                ? { color: 'var(--danger)' }
                : tone === 'accent'
                  ? { color: 'var(--accent)' }
                  : undefined
            }
          >
            {typeLabel(type)}
          </button>
          {payload?.agent_name && <span className="timeline__time">{payload.agent_name}</span>}
          <span className="timeline__time">{ts ? ago(ts) : ''}</span>
        </div>
        <div className="timeline__text">{summary}</div>
        {open && (
          <CodeBlock maxHeight={320}>{JSON.stringify(payload, null, 2)}</CodeBlock>
        )}
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
