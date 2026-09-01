import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  DataTable,
  Field,
  Input,
  Modal,
  SearchInput,
  Select,
  StatusDot,
  Textarea,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi, usePolling } from '../hooks.js'
import { targetsApi, tasksApi } from '../api.js'
import { TASK_STATUS, dtShort, num, shortId, statusOf, truncate } from '../format.js'

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '运行中' },
  { value: 'queued', label: '排队中' },
  { value: 'pending', label: '待派发' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

export default function Tasks() {
  const navigate = useNavigate()
  const toast = useToast()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [showProbe, setShowProbe] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(null)

  const fetchTasks = useCallback(() => tasksApi.list(), [])
  const { data: tasks, loading, reload } = useApi(fetchTasks, [])
  const fetchTargets = useCallback(() => targetsApi.list(), [])
  const { data: targets } = useApi(fetchTargets, [])

  const list = tasks ?? []
  const hasActive = list.some((t) => t.status === 'running' || t.status === 'queued')
  usePolling(reload, 6000, hasActive)

  const targetMap = useMemo(() => {
    const m = new Map()
    ;(targets ?? []).forEach((t) => m.set(t.id, t))
    return m
  }, [targets])

  const rows = useMemo(() => {
    const kw = search.trim().toLowerCase()
    return list
      .filter((t) => (showProbe ? true : t.kind !== 'probe'))
      .filter((t) => (status ? t.status === status : true))
      .filter((t) =>
        kw
          ? t.name.toLowerCase().includes(kw) ||
            (t.objective || '').toLowerCase().includes(kw) ||
            t.id.toLowerCase().includes(kw)
          : true,
      )
  }, [list, search, status, showProbe])

  const counts = useMemo(() => {
    const c = { running: 0, queued: 0, completed: 0, failed: 0 }
    list.forEach((t) => {
      if (t.kind === 'probe') return
      if (c[t.status] !== undefined) c[t.status] += 1
    })
    return c
  }, [list])

  const [run, pending] = useAction()

  const onCancel = (task) =>
    run(async () => {
      try {
        await tasksApi.cancel(task.id)
        toast('已发送取消请求', 'success')
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onRerun = (task) =>
    run(async () => {
      try {
        await tasksApi.enqueue(task.id)
        toast('任务已重新入队', 'success')
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onDelete = () =>
    run(async () => {
      try {
        await tasksApi.remove(pendingDelete.id)
        toast('任务已删除', 'success')
        setPendingDelete(null)
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const columns = [
    {
      key: 'name',
      title: '任务',
      render: (t) => (
        <div className="task-name">
          <span className="task-name__main">{t.name}</span>
          <span className="task-name__sub">{truncate(t.objective, 72)}</span>
        </div>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 120,
      render: (t) => {
        const s = statusOf(TASK_STATUS, t.status)
        return (
          <Badge tone={s.tone}>
            <StatusDot status={t.status} />
            {s.label}
          </Badge>
        )
      },
    },
    {
      key: 'target',
      title: '授权目标',
      width: 180,
      render: (t) => {
        const tg = targetMap.get(t.target_id)
        return (
          <span className="truncate mono" style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
            {tg ? tg.name : shortId(t.target_id)}
          </span>
        )
      },
    },
    {
      key: 'kind',
      title: '类型',
      width: 92,
      render: (t) =>
        t.kind === 'probe' ? (
          <Badge tone="info">探针</Badge>
        ) : t.is_range ? (
          <Badge tone="warning">靶场</Badge>
        ) : (
          <Badge tone="neutral">常规</Badge>
        ),
    },
    {
      key: 'agent_mode',
      title: '模式',
      width: 88,
      render: (t) => (
        <span className="mono" style={{ fontSize: 12 }}>
          {t.agent_mode}
        </span>
      ),
    },
    { key: 'total_tokens', title: 'Tokens', num: true, width: 100, render: (t) => num(t.total_tokens) },
    { key: 'created_at', title: '创建时间', width: 128, render: (t) => <span className="mono" style={{ fontSize: 12 }}>{dtShort(t.created_at)}</span> },
    {
      key: 'actions',
      title: '操作',
      width: 200,
      render: (t) => (
        <div className="row" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="ghost" icon="terminal" onClick={() => navigate(`/tasks/${t.id}`)}>
            控制台
          </Button>
          {(t.status === 'running' || t.status === 'queued') && (
            <Button size="sm" variant="ghost" icon="stop" onClick={() => onCancel(t)} title="取消任务" />
          )}
          {(t.status === 'failed' || t.status === 'cancelled') && (
            <Button size="sm" variant="ghost" icon="play" onClick={() => onRerun(t)} title="重新入队（继续）">
              继续
            </Button>
          )}
          <Button size="sm" variant="ghost" icon="trash" onClick={() => setPendingDelete(t)} title="删除任务" />
        </div>
      ),
    },
  ]

  return (
    <>
      <PageHead
        title="任务"
        icon="tasks"
        desc="下发渗透测试目标，跟踪多智能体的执行过程、产物与漏洞发现。"
        actions={
          <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)} disabled={!targets?.length}>
            新建任务
          </Button>
        }
      />

      {!targets?.length && (
        <div className="overview-alert overview-alert--warning" style={{ marginBottom: 16 }}>
          <span className="overview-alert__icon">
            <Icon name="alert" size={16} />
          </span>
          <div className="overview-alert__body">
            <div className="overview-alert__title">尚无可用的授权目标</div>
            <div className="overview-alert__desc">
              任务必须绑定到已授权目标，请先前往 <Link to="/targets">授权目标</Link> 登记 scope 后再创建。
            </div>
          </div>
        </div>
      )}

      <div className="grid grid--stats" style={{ marginBottom: 24 }}>
        <MiniStat label="运行中" value={counts.running} tone="accent" icon="activity" />
        <MiniStat label="排队中" value={counts.queued} tone="info" icon="clock" />
        <MiniStat label="已完成" value={counts.completed} icon="check" />
        <MiniStat label="失败" value={counts.failed} tone="danger" icon="alert" />
      </div>

      <Card
        flush
        title="任务列表"
        actions={
          <div className="filter-bar" style={{ margin: 0 }}>
            <SearchInput value={search} onChange={setSearch} placeholder="搜索任务名 / 目标 / ID" />
            <Select options={STATUS_OPTIONS} value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 130 }} />
            <Checkbox label="含链路探针" checked={showProbe} onChange={(e) => setShowProbe(e.target.checked)} />
            <Button size="sm" variant="ghost" icon="refresh" onClick={reload} disabled={loading}>
              刷新
            </Button>
          </div>
        }
      >
        <DataTable
          columns={columns}
          rows={rows}
          loading={loading}
          onRowClick={(t) => navigate(`/tasks/${t.id}`)}
          empty={
            <div className="empty">
              <span className="empty__icon">
                <Icon name="tasks" size={20} />
              </span>
              <span className="empty__title">还没有任务</span>
              <span className="empty__desc">创建第一个任务，让智能体在授权范围内自主完成侦察与利用验证。</span>
            </div>
          }
        />
      </Card>

      <TaskCreateModal
        open={createOpen}
        targets={targets ?? []}
        onClose={() => setCreateOpen(false)}
        onCreated={(task) => {
          setCreateOpen(false)
          toast(`任务「${task.name}」已创建并投递`, 'success')
          reload()
        }}
      />

      <ConfirmDialog
        open={!!pendingDelete}
        title="删除任务"
        message={`确认删除任务「${pendingDelete?.name || ''}」？该操作会同时移除其关联的发现与产物记录。`}
        confirmText="删除"
        danger
        loading={pending}
        onConfirm={onDelete}
        onClose={() => setPendingDelete(null)}
      />
    </>
  )
}

function MiniStat({ label, value, tone, icon }) {
  return (
    <div className={`stat ${tone ? `stat--${tone}` : ''}`}>
      <span className="stat__label">
        {icon && <Icon name={icon} size={13} />}
        {label}
      </span>
      <span className="stat__value">{value}</span>
    </div>
  )
}

/* ------------------------------------------------------------------ 创建任务 */

export function TaskCreateModal({ open, targets, onClose, onCreated, presetTargetId }) {
  const toast = useToast()
  const [form, setForm] = useState({
    target_id: '',
    name: '',
    objective: '',
    model: '',
    max_turns: 50,
    agent_mode: 'hacker',
    is_range: false,
    flag_regex: '',
    auth_mode: 'none',
    auth_username: '',
    auth_password: '',
    auth_login_url: '',
  })
  const [busy, setBusy] = useState(false)
  const [verifyState, setVerifyState] = useState({ status: 'idle', message: '' }) // idle/verifying/success/failed/mfa/aborted/error
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))
  const resetVerify = () => setVerifyState({ status: 'idle', message: '' })

  // 凭据/登录地址变化时清除验证状态（避免旧验证结果误导）
  const setAuthField = (k) => (e) => {
    resetVerify()
    setForm((f) => ({ ...f, [k]: e.target.value }))
  }

  const verify = async () => {
    if (!form.target_id) return toast('请选择授权目标', 'warning')
    if (!form.auth_username.trim() || !form.auth_password) {
      return toast('请先填写账号与密码', 'warning')
    }
    setVerifyState({ status: 'verifying', message: '' })
    try {
      const payload = {
        target_id: form.target_id,
        username: form.auth_username.trim(),
        password: form.auth_password,
      }
      if (form.auth_login_url.trim()) payload.login_url = form.auth_login_url.trim()
      const res = await tasksApi.verifyAuth(payload)
      setVerifyState({ status: res.status || 'error', message: res.message || '验证完成' })
    } catch (e) {
      setVerifyState({ status: 'error', message: e.message || '验证失败' })
    }
  }

  // 每次打开时重置表单并预填目标
  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setForm({
        target_id: presetTargetId || targets[0]?.id || '',
        name: '',
        objective: '',
        model: '',
        max_turns: 50,
        agent_mode: 'hacker',
        is_range: false,
        flag_regex: '',
        auth_mode: 'none',
        auth_username: '',
        auth_password: '',
        auth_login_url: '',
      })
    }
  }

  const submit = async () => {
    if (!form.target_id) return toast('请选择授权目标', 'warning')
    if (!form.name.trim()) return toast('请填写任务名称', 'warning')
    if (!form.objective.trim()) return toast('请填写测试目标描述', 'warning')
    if (form.is_range && !form.flag_regex.trim()) return toast('靶场任务必须配置 Flag 正则', 'warning')
    if (form.auth_mode === 'auto' && (!form.auth_username.trim() || !form.auth_password)) {
      return toast('账号密码自动认证需填写用户名与密码', 'warning')
    }
    // 凭据验证门禁：auto 模式必须先验证，凭据错误不允许发放任务
    if (form.auth_mode === 'auto' && form.auth_password) {
      if (verifyState.status === 'idle') {
        return toast('请先点击「验证凭据」，验证通过后才允许创建任务', 'warning')
      }
      if (verifyState.status === 'verifying') {
        return toast('凭据正在验证中，请稍候', 'warning')
      }
      if (verifyState.status === 'failed') {
        return toast('凭据验证未通过（账号或密码错误），请修正后重新验证', 'warning')
      }
    }

    setBusy(true)
    try {
      const payload = {
        target_id: form.target_id,
        name: form.name.trim(),
        objective: form.objective.trim(),
        max_turns: Number(form.max_turns) || 50,
        agent_mode: form.agent_mode,
        is_range: form.is_range,
        auth_mode: form.auth_mode,
      }
      if (form.model.trim()) payload.model = form.model.trim()
      if (form.is_range) payload.flag_regex = form.flag_regex.trim()
      if (form.auth_mode === 'auto') {
        payload.auth_username = form.auth_username.trim()
        payload.auth_password = form.auth_password
        if (form.auth_login_url.trim()) payload.auth_login_url = form.auth_login_url.trim()
      }
      const task = await tasksApi.create(payload)
      onCreated?.(task)
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="新建任务"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            创建并投递
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field label="授权目标" required>
          <Select
            value={form.target_id}
            onChange={set('target_id')}
            options={targets.map((t) => ({ value: t.id, label: `${t.name} · ${t.url}` }))}
          />
        </Field>

        <Field label="任务名称" required>
          <Input value={form.name} onChange={set('name')} placeholder="例如：电商站点评测-第一轮" />
        </Field>

        <Field label="测试目标描述" required hint="描述本次要达成的测试目标，智能体据此自主规划执行路径。">
          <Textarea
            value={form.objective}
            onChange={set('objective')}
            rows={4}
            placeholder="例如：对目标主站做完整安全评估，重点验证认证与订单链路，尝试发现可利用的越权漏洞并给出复现步骤。"
          />
        </Field>

        <div className="form-grid form-grid--2">
          <Field label="执行模式" required hint="hacker：高危操作需人工审批；yolo：自动批准。">
            <Select
              value={form.agent_mode}
              onChange={set('agent_mode')}
              options={[
                { value: 'hacker', label: 'hacker（人工审批高危）' },
                { value: 'yolo', label: 'yolo（自动批准）' },
              ]}
            />
          </Field>
          <Field label="最大轮次" hint="Agent 自主推理的轮次上限。">
            <Input type="number" min={1} max={500} value={form.max_turns} onChange={set('max_turns')} />
          </Field>
        </div>

        <Field label="模型（可选）" hint="留空则使用平台默认模型。">
          <Input value={form.model} onChange={set('model')} placeholder="默认模型" />
        </Field>

        <Field>
          <Checkbox
            label="靶场模式（需要 Flag 正则做验收）"
            checked={form.is_range}
            onChange={(e) => setForm((f) => ({ ...f, is_range: e.target.checked }))}
          />
        </Field>

        {form.is_range && (
          <Field label="Flag 正则" required hint="用于验证 Agent 是否成功拿下靶标。">
            <Input className="mono" value={form.flag_regex} onChange={set('flag_regex')} placeholder="flag\{[a-zA-Z0-9_-]+\}" />
          </Field>
        )}

        <Field
          label="登录认证（可选）"
          hint="auto：后端自动登录并缓存会话，任务创建时先验证凭据。MFA/短信等需人工登录的场景暂缓支持（见演进计划）。"
        >
          <Select
            value={form.auth_mode}
            onChange={set('auth_mode')}
            options={[
              { value: 'none', label: '无需认证' },
              { value: 'auto', label: '账号密码自动认证' },
              // [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
              // { value: 'manual', label: '手动登录（MFA/验证码）' },
            ]}
          />
        </Field>

        {form.auth_mode === 'auto' && (
          <>
            <div className="form-grid form-grid--2">
              <Field label="登录地址（可选）" hint="留空则使用目标 URL。">
                <Input value={form.auth_login_url} onChange={setAuthField('auth_login_url')} placeholder="https://target/login" />
              </Field>
              <Field label="账号" required>
                <Input value={form.auth_username} onChange={setAuthField('auth_username')} placeholder="登录用户名" />
              </Field>
              <Field label="密码" required>
                <Input type="password" value={form.auth_password} onChange={setAuthField('auth_password')} placeholder="登录密码" />
              </Field>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 4 }}>
              <Button
                variant="outline"
                loading={verifyState.status === 'verifying'}
                disabled={!form.auth_username.trim() || !form.auth_password}
                onClick={verify}
              >
                验证凭据
              </Button>
              {verifyState.status !== 'idle' && verifyState.status !== 'verifying' && (
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: 500,
                    color:
                      verifyState.status === 'success'
                        ? 'var(--green-400)'
                        : verifyState.status === 'mfa' || verifyState.status === 'aborted'
                          ? 'var(--amber-400)'
                          : 'var(--red-400)',
                  }}
                >
                  {verifyState.status === 'success' ? '✓ ' : '✗ '}
                  {verifyState.message}
                </span>
              )}
            </div>
          </>
        )}

        {/* [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
        {form.auth_mode === 'manual' && (
          <div className="form-grid">
            <Field label="登录地址（可选）" hint="留空则使用目标 URL。创建任务后请在任务页点击「手动登录」完成 MFA/验证码流程。">
              <Input value={form.auth_login_url} onChange={set('auth_login_url')} placeholder="https://target/login" />
            </Field>
          </div>
        )}
        */}
      </div>
    </Modal>
  )
}
