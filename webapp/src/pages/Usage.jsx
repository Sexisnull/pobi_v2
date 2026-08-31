import { useCallback, useMemo, useState } from 'react'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Field,
  Input,
  LoadingBlock,
  Select,
  Stat,
  useToast,
} from '../components/ui.jsx'
import { useAction, useApi } from '../hooks.js'
import { pricingApi, tasksApi } from '../api.js'
import { TASK_STATUS, dtShort, money, num, statusOf } from '../format.js'

const CURRENCIES = [
  { value: 'USD', label: 'USD（美元）' },
  { value: 'CNY', label: 'CNY（人民币）' },
]

export default function Usage() {
  const toast = useToast()
  const [editing, setEditing] = useState(false)

  const fetchSummary = useCallback(() => tasksApi.usageSummary(), [])
  const { data: summary, loading: loadingSummary, reload: reloadSummary } = useApi(fetchSummary, [])
  const fetchTasks = useCallback(() => tasksApi.list(), [])
  const { data: tasks, loading: loadingTasks, reload: reloadTasks } = useApi(fetchTasks, [])
  const fetchPricing = useCallback(() => pricingApi.get(), [])
  const { data: pricing, reload: reloadPricing } = useApi(fetchPricing, [])

  const priceInput = Number(pricing?.price_input || 0)
  const priceOutput = Number(pricing?.price_output || 0)
  const currency = pricing?.currency || 'USD'

  const cost = (prompt, completion) =>
    (prompt / 1_000_000) * priceInput + (completion / 1_000_000) * priceOutput

  const totalCost = summary ? cost(summary.total_prompt_tokens, summary.total_completion_tokens) : 0
  const completedCost = summary
    ? cost(summary.completed_prompt_tokens, summary.completed_completion_tokens)
    : 0

  const rows = useMemo(
    () => (tasks ?? []).filter((t) => (t.total_tokens || 0) > 0),
    [tasks],
  )

  const [run, pending] = useAction()
  const savePricing = (data) =>
    run(async () => {
      try {
        await pricingApi.update(data)
        toast('单价配置已保存', 'success')
        setEditing(false)
        reloadPricing()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  if (loadingSummary || loadingTasks) return <LoadingBlock />

  const prompt = summary?.total_prompt_tokens || 0
  const completion = summary?.total_completion_tokens || 0
  const total = prompt + completion || 1

  return (
    <>
      <PageHead
        title="Token 用量"
        icon="gauge"
        desc="统计全部任务的模型调用消耗，并可按自定义单价估算成本。"
        actions={
          <Button
            icon="refresh"
            onClick={() => {
              reloadSummary()
              reloadTasks()
            }}
          >
            刷新
          </Button>
        }
      />

      <div className="grid grid--stats" style={{ marginBottom: 24 }}>
        <Stat label="总消耗" icon="gauge" value={num(summary?.total_tokens || 0)} hint="发送 + 接收" />
        <Stat label="发送 Tokens" icon="send" value={num(prompt)} />
        <Stat label="接收 Tokens" icon="download" value={num(completion)} />
        <Stat
          label="估算总成本"
          icon="zap"
          tone="accent"
          value={money(totalCost, currency)}
          hint={`已完成任务 ${money(completedCost, currency)}`}
        />
      </div>

      <div className="grid grid--2" style={{ marginBottom: 24 }}>
        <Card title="收发占比" icon="layers">
          <div className="usage-bar">
            <div className="usage-bar__seg usage-bar__seg--prompt" style={{ width: `${(prompt / total) * 100}%` }}>
              {prompt / total > 0.12 ? `${Math.round((prompt / total) * 100)}%` : ''}
            </div>
            <div className="usage-bar__seg usage-bar__seg--completion" style={{ width: `${(completion / total) * 100}%` }}>
              {completion / total > 0.12 ? `${Math.round((completion / total) * 100)}%` : ''}
            </div>
          </div>
          <div className="legend">
            <span className="legend__item">
              <span className="legend__swatch" style={{ background: 'var(--blue-600)' }} />
              发送 {num(prompt)}
            </span>
            <span className="legend__item">
              <span className="legend__swatch" style={{ background: 'var(--green-600)' }} />
              接收 {num(completion)}
            </span>
          </div>
        </Card>

        <Card
          title="单价配置"
          icon="box"
          actions={
            !editing && (
              <Button size="sm" variant="ghost" icon="filter" onClick={() => setEditing(true)}>
                编辑
              </Button>
            )
          }
        >
          {editing ? (
            <PricingForm
              initial={{ price_input: priceInput, price_output: priceOutput, currency }}
              pending={pending}
              onCancel={() => setEditing(false)}
              onSubmit={savePricing}
            />
          ) : (
            <div className="kv">
              <span className="kv__k">输入单价</span>
              <span className="kv__v kv__v--mono">{priceInput} / 1M tokens</span>
              <span className="kv__k">输出单价</span>
              <span className="kv__v kv__v--mono">{priceOutput} / 1M tokens</span>
              <span className="kv__k">币种</span>
              <span className="kv__v kv__v--mono">{currency}</span>
            </div>
          )}
        </Card>
      </div>

      <Card flush title="按任务明细" icon="tasks">
        <DataTable
          columns={[
            { key: 'name', title: '任务', render: (t) => <span style={{ color: 'var(--text-primary)' }}>{t.name}</span> },
            {
              key: 'status',
              title: '状态',
              width: 100,
              render: (t) => {
                const s = statusOf(TASK_STATUS, t.status)
                return <Badge tone={s.tone}>{s.label}</Badge>
              },
            },
            { key: 'model', title: '模型', render: (t) => <span className="mono" style={{ fontSize: 12 }}>{t.model || '默认'}</span> },
            { key: 'prompt_tokens', title: '发送', num: true, render: (t) => num(t.prompt_tokens) },
            { key: 'completion_tokens', title: '接收', num: true, render: (t) => num(t.completion_tokens) },
            { key: 'total_tokens', title: '合计', num: true, render: (t) => num(t.total_tokens) },
            {
              key: 'cost',
              title: '成本',
              num: true,
              render: (t) => money(cost(t.prompt_tokens || 0, t.completion_tokens || 0), currency),
            },
            { key: 'created_at', title: '创建时间', width: 128, render: (t) => <span className="mono" style={{ fontSize: 12 }}>{dtShort(t.created_at)}</span> },
          ]}
          rows={rows}
          empty="暂无消耗记录"
        />
      </Card>
    </>
  )
}

function PricingForm({ initial, pending, onCancel, onSubmit }) {
  const [form, setForm] = useState({
    price_input: String(initial.price_input ?? 0),
    price_output: String(initial.price_output ?? 0),
    currency: initial.currency || 'USD',
  })
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  return (
    <form
      className="form-grid"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit({
          price_input: Number(form.price_input) || 0,
          price_output: Number(form.price_output) || 0,
          currency: form.currency,
        })
      }}
    >
      <div className="form-grid form-grid--2">
        <Field label="输入单价" hint="每 100 万 tokens">
          <Input type="number" step="0.01" min="0" value={form.price_input} onChange={set('price_input')} />
        </Field>
        <Field label="输出单价" hint="每 100 万 tokens">
          <Input type="number" step="0.01" min="0" value={form.price_output} onChange={set('price_output')} />
        </Field>
      </div>
      <Field label="币种">
        <Select options={CURRENCIES} value={form.currency} onChange={set('currency')} />
      </Field>
      <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
        <Button type="button" variant="ghost" onClick={onCancel}>
          取消
        </Button>
        <Button type="submit" variant="primary" loading={pending}>
          保存
        </Button>
      </div>
    </form>
  )
}
