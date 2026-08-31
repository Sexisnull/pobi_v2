import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  Field,
  Input,
  Modal,
  Select,
  StatusDot,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi } from '../hooks.js'
import { systemApi, targetsApi } from '../api.js'
import { num } from '../format.js'

export default function Health() {
  const toast = useToast()
  const navigate = useNavigate()
  const [probeOpen, setProbeOpen] = useState(false)

  const fetchWorker = useCallback(() => systemApi.workerStatus(), [])
  const { data: worker, loading: lw, reload: rw } = useApi(fetchWorker, [])
  const fetchKali = useCallback(() => systemApi.kaliStatus(), [])
  const { data: kali, loading: lk, reload: rk } = useApi(fetchKali, [])
  const fetchLlm = useCallback(() => systemApi.llmStatus(), [])
  const { data: llm, loading: ll, reload: rl } = useApi(fetchLlm, [])
  const fetchTargets = useCallback(() => targetsApi.list(), [])
  const { data: targets } = useApi(fetchTargets, [])

  const [run, pending] = useAction()

  const reloadAll = () =>
    run(async () => {
      await Promise.allSettled([rw(), rk(), rl()])
    })

  const onReconcile = () =>
    run(async () => {
      try {
        const res = await systemApi.reconcile()
        toast(`对账完成：${JSON.stringify(res)}`, 'success')
        rw()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  return (
    <>
      <PageHead
        title="系统健康"
        icon="activity"
        desc="检测任务消费、沙箱与模型链路的连通性，定位任务卡住的根因。"
        actions={
          <>
            <Button icon="refresh" onClick={reloadAll} disabled={pending}>
              重新检测
            </Button>
            <Button icon="refresh" onClick={onReconcile} disabled={pending}>
              任务对账
            </Button>
            <Button variant="primary" icon="zap" onClick={() => setProbeOpen(true)}>
              链路探针
            </Button>
          </>
        }
      />

      <div className="grid grid--3">
        <HealthCard
          icon="cpu"
          title="任务 Worker"
          subtitle="ARQ 消费进程与队列积压"
          loading={lw}
          ok={!!(worker?.available && worker?.online)}
          unknown={!worker?.available}
          footer={
            worker?.available ? (
              <>
                <Badge tone={worker.online ? 'accent' : 'danger'}>{worker.online ? '在线' : '离线'}</Badge>
                <span className="muted" style={{ fontSize: 12 }}>
                  队列深度 {num(worker.queue_depth)} · 心跳 TTL {worker.health_ttl}s
                </span>
              </>
            ) : null
          }
        >
          {worker?.available ? (
            <>
              <div className="kv" style={{ marginBottom: 12 }}>
                {Object.entries(worker.stats || {}).map(([k, v]) => (
                  <div key={k} style={{ display: 'contents' }}>
                    <span className="kv__k">{k}</span>
                    <span className="kv__v kv__v--mono">{String(v)}</span>
                  </div>
                ))}
              </div>
              {worker.detail && <CodeBlock>{worker.detail}</CodeBlock>}
            </>
          ) : (
            <span style={{ color: 'var(--danger)' }}>{worker?.error || '不可用'}</span>
          )}
        </HealthCard>

        <HealthCard
          icon="terminal"
          title="Kali 沙箱"
          subtitle="共享容器与基础工具链"
          loading={lk}
          ok={!!kali?.healthy}
          unknown={!kali?.available}
          footer={
            kali?.available ? (
              <>
                <Badge tone={kali.healthy ? 'accent' : 'danger'}>{kali.healthy ? '健康' : '异常'}</Badge>
                {kali.container_id && (
                  <span className="mono muted" style={{ fontSize: 12 }}>
                    {kali.container_id}
                  </span>
                )}
              </>
            ) : null
          }
        >
          {kali?.available ? (
            <>
              <div className="kv" style={{ marginBottom: 12 }}>
                <span className="kv__k">退出码</span>
                <span className="kv__v kv__v--mono">{String(kali.exit_code)}</span>
              </div>
              {kali.stdout && <CodeBlock>{kali.stdout}</CodeBlock>}
              {kali.stderr && (
                <div style={{ marginTop: 8 }}>
                  <CodeBlock>{kali.stderr}</CodeBlock>
                </div>
              )}
              {kali.error && <span style={{ color: 'var(--danger)' }}>{kali.error}</span>}
            </>
          ) : (
            <span style={{ color: 'var(--danger)' }}>{kali?.error || '不可用'}</span>
          )}
        </HealthCard>

        <HealthCard
          icon="zap"
          title="模型服务"
          subtitle="LLM 连通性与响应延迟"
          loading={ll}
          ok={!!llm?.healthy}
          unknown={!llm?.available}
          footer={
            llm?.available ? (
              <>
                <Badge tone={llm.healthy ? 'accent' : 'danger'}>{llm.healthy ? '可用' : '异常'}</Badge>
                {llm.latency_ms != null && (
                  <span className="muted" style={{ fontSize: 12 }}>
                    延迟 {llm.latency_ms} ms
                  </span>
                )}
              </>
            ) : null
          }
        >
          {llm?.available ? (
            <>
              <div className="kv" style={{ marginBottom: 12 }}>
                <span className="kv__k">模型</span>
                <span className="kv__v kv__v--mono">{llm.model || '—'}</span>
                {llm.usage && (
                  <>
                    <span className="kv__k">用量</span>
                    <span className="kv__v kv__v--mono">
                      in {num(llm.usage.prompt_tokens)} / out {num(llm.usage.completion_tokens)}
                    </span>
                  </>
                )}
              </div>
              {llm.reply && <CodeBlock>{llm.reply}</CodeBlock>}
            </>
          ) : (
            <span style={{ color: 'var(--danger)' }}>{llm?.error || '不可用'}</span>
          )}
        </HealthCard>
      </div>

      <Card title="说明" icon="info" style={{ marginTop: 24 }}>
        <div className="stack" style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>
          <span>
            <strong style={{ color: 'var(--text-secondary)' }}>任务对账</strong>
            ：校正数据库任务状态与队列真实状态的不一致，例如 Worker 重启导致任务卡在 running。
          </span>
          <span>
            <strong style={{ color: 'var(--text-secondary)' }}>链路探针</strong>
            ：派发一个轻量探针任务，在共享 Kali 沙箱中对已授权目标做连通性验证，用于判断「任务无进展」是网络不通还是 Agent 问题。
          </span>
        </div>
      </Card>

      <ProbeModal
        open={probeOpen}
        targets={(targets ?? []).filter((t) => t.enabled)}
        onClose={() => setProbeOpen(false)}
        onDispatched={(taskId) => {
          setProbeOpen(false)
          toast('探针任务已派发', 'success')
          navigate(`/tasks/${taskId}`)
        }}
      />
    </>
  )
}

function HealthCard({ icon, title, subtitle, loading, ok, unknown, footer, children }) {
  const state = loading ? 'loading' : unknown ? 'unknown' : ok ? 'ok' : 'bad'
  return (
    <div className={`health-card health-card--${state}`}>
      <div className="health-card__head">
        <span className="health-card__icon">
          <Icon name={icon} size={17} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <div className="health-card__title">{title}</div>
          <div className="health-card__subtitle">{subtitle}</div>
        </span>
        {!loading && <StatusDot status={unknown ? 'offline' : ok ? 'online' : 'failed'} />}
      </div>
      <div className="health-card__body">
        {loading ? (
          <span className="row" style={{ gap: 8 }}>
            <span className="spinner" /> 检测中…
          </span>
        ) : (
          children
        )}
      </div>
      {footer && <div className="health-card__foot">{footer}</div>}
    </div>
  )
}

function ProbeModal({ open, targets, onClose, onDispatched }) {
  const toast = useToast()
  const [form, setForm] = useState({ target_id: '', prompt: '', max_turns: 8 })
  const [busy, setBusy] = useState(false)

  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) setForm({ target_id: targets[0]?.id || '', prompt: '', max_turns: 8 })
  }

  const submit = async () => {
    if (!form.target_id) return toast('请选择已启用的授权目标', 'warning')
    setBusy(true)
    try {
      const res = await systemApi.probe({
        target_id: form.target_id,
        prompt: form.prompt.trim() || null,
        max_turns: Number(form.max_turns) || 8,
      })
      onDispatched?.(res.task_id)
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="派发链路探针"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            派发探针
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field label="授权目标" required hint="探针仅在目标授权范围内执行连通性验证。">
          <Select
            value={form.target_id}
            onChange={(e) => setForm((f) => ({ ...f, target_id: e.target.value }))}
            options={targets.map((t) => ({ value: t.id, label: `${t.name} · ${t.url}` }))}
          />
        </Field>
        <Field label="自定义指令（可选）" hint="留空则使用默认连通性探测流程。">
          <Input
            value={form.prompt}
            onChange={(e) => setForm((f) => ({ ...f, prompt: e.target.value }))}
            placeholder="curl 目标首页并报告 HTTP 状态码"
          />
        </Field>
        <Field label="最大轮次">
          <Input
            type="number"
            min={1}
            max={50}
            value={form.max_turns}
            onChange={(e) => setForm((f) => ({ ...f, max_turns: e.target.value }))}
          />
        </Field>
      </div>
    </Modal>
  )
}
