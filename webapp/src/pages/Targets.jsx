import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Field,
  Input,
  Modal,
  SearchInput,
  Textarea,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi } from '../hooks.js'
import { targetsApi } from '../api.js'
import { dtShort, truncate } from '../format.js'

export default function Targets() {
  const navigate = useNavigate()
  const toast = useToast()
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(null)

  const fetcher = useCallback(() => targetsApi.list(), [])
  const { data: targets, loading, reload } = useApi(fetcher, [])
  const [run, pending] = useAction()

  const rows = useMemo(() => {
    const kw = search.trim().toLowerCase()
    return (targets ?? []).filter(
      (t) =>
        !kw ||
        t.name.toLowerCase().includes(kw) ||
        t.url.toLowerCase().includes(kw) ||
        (t.description || '').toLowerCase().includes(kw),
    )
  }, [targets, search])

  const onToggle = (t) =>
    run(async () => {
      try {
        await targetsApi.update(t.id, { enabled: !t.enabled })
        toast(t.enabled ? '目标已停用' : '目标已启用', 'success')
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onDelete = () =>
    run(async () => {
      try {
        await targetsApi.remove(pendingDelete.id)
        toast('授权目标已删除', 'success')
        setPendingDelete(null)
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const columns = [
    {
      key: 'name',
      title: '目标',
      render: (t) => (
        <div className="task-name">
          <span className="task-name__main">{t.name}</span>
          <span className="task-name__sub">{truncate(t.description || '—', 64)}</span>
        </div>
      ),
    },
    {
      key: 'url',
      title: 'URL',
      render: (t) => (
        <span className="mono" style={{ fontSize: 12, color: 'var(--accent)' }}>
          {truncate(t.url, 52)}
        </span>
      ),
    },
    {
      key: 'scope',
      title: '授权范围',
      width: 150,
      render: (t) => (
        <span className="row" style={{ gap: 6 }}>
          <Badge tone="accent">in {t.in_scope?.length ?? 0}</Badge>
          <Badge tone="danger">out {t.out_of_scope?.length ?? 0}</Badge>
        </span>
      ),
    },
    {
      key: 'is_range',
      title: '靶场',
      width: 84,
      render: (t) => (t.flag_regex ? <Badge tone="warning">是</Badge> : <Badge tone="neutral">否</Badge>),
    },
    {
      key: 'enabled',
      title: '状态',
      width: 100,
      render: (t) => (
        <Badge tone={t.enabled ? 'accent' : 'neutral'}>{t.enabled ? '已启用' : '已停用'}</Badge>
      ),
    },
    { key: 'created_at', title: '登记时间', width: 128, render: (t) => <span className="mono" style={{ fontSize: 12 }}>{dtShort(t.created_at)}</span> },
    {
      key: 'actions',
      title: '操作',
      width: 168,
      render: (t) => (
        <div className="row" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="ghost" icon="external" onClick={() => navigate(`/targets/${t.id}`)}>
            详情
          </Button>
          <Button size="sm" variant="ghost" icon="filter" onClick={() => setEditing(t)} title="编辑" />
          <Button
            size="sm"
            variant="ghost"
            icon={t.enabled ? 'ban' : 'check'}
            onClick={() => onToggle(t)}
            title={t.enabled ? '停用' : '启用'}
          />
          <Button size="sm" variant="ghost" icon="trash" onClick={() => setPendingDelete(t)} title="删除" />
        </div>
      ),
    },
  ]

  return (
    <>
      <PageHead
        title="授权目标"
        icon="target"
        desc="登记已获书面授权的测试目标与 scope 边界。所有任务执行前都会经过授权闸门校验。"
        actions={
          <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
            登记目标
          </Button>
        }
      />

      <Card
        flush
        title="目标清单"
        actions={
          <div className="filter-bar" style={{ margin: 0 }}>
            <SearchInput value={search} onChange={setSearch} placeholder="搜索名称 / URL / 描述" />
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
          onRowClick={(t) => navigate(`/targets/${t.id}`)}
          empty={
            <EmptyState
              icon="target"
              title="还没有授权目标"
              desc="登记目标及其 scope 后，才能下发渗透测试任务。"
              action={
                <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
                  登记目标
                </Button>
              }
            />
          }
        />
      </Card>

      <TargetModal
        open={createOpen || !!editing}
        target={editing}
        onClose={() => {
          setCreateOpen(false)
          setEditing(null)
        }}
        onSaved={() => {
          setCreateOpen(false)
          setEditing(null)
          toast(editing ? '目标已更新' : '授权目标已登记', 'success')
          reload()
        }}
      />

      <ConfirmDialog
        open={!!pendingDelete}
        title="删除授权目标"
        message={`确认删除「${pendingDelete?.name || ''}」？其历史侦察数据与发现将不再可查询。`}
        confirmText="删除"
        danger
        loading={pending}
        onConfirm={onDelete}
        onClose={() => setPendingDelete(null)}
      />
    </>
  )
}

function TargetModal({ open, target, onClose, onSaved }) {
  const toast = useToast()
  const [form, setForm] = useState(blank())
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) setForm(target ? fromTarget(target) : blank())
  }

  const submit = async () => {
    if (!form.name.trim()) return toast('请填写目标名称', 'warning')
    if (!form.url.trim()) return toast('请填写目标 URL', 'warning')
    setBusy(true)
    try {
      const payload = {
        name: form.name.trim(),
        url: form.url.trim(),
        description: form.description.trim() || null,
        in_scope: splitLines(form.in_scope),
        out_of_scope: splitLines(form.out_of_scope),
        flag_regex: form.flag_regex.trim() || null,
        validation_format: form.validation_format.trim() || null,
        confidence_threshold: Number(form.confidence_threshold),
        max_tree_depth: Number(form.max_tree_depth),
        enabled: form.enabled,
      }
      if (target) await targetsApi.update(target.id, payload)
      else await targetsApi.create(payload)
      onSaved?.()
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={target ? '编辑授权目标' : '登记授权目标'}
      wide
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            {target ? '保存修改' : '登记目标'}
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <div className="form-grid form-grid--2">
          <Field label="目标名称" required>
            <Input value={form.name} onChange={set('name')} placeholder="例如：电商主站" />
          </Field>
          <Field label="目标 URL" required>
            <Input className="mono" value={form.url} onChange={set('url')} placeholder="https://example.com" />
          </Field>
        </div>

        <Field label="描述">
          <Input value={form.description} onChange={set('description')} placeholder="业务背景、授权编号等" />
        </Field>

        <div className="form-grid form-grid--2">
          <Field label="授权范围内（in_scope）" hint="每行一条，支持 URL 前缀或域名；留空表示整站授权。">
            <Textarea
              mono
              rows={4}
              value={form.in_scope}
              onChange={set('in_scope')}
              placeholder={'https://example.com\nhttps://api.example.com'}
            />
          </Field>
          <Field label="授权范围外（out_of_scope）" hint="每行一条，命中即拒绝执行。">
            <Textarea
              mono
              rows={4}
              value={form.out_of_scope}
              onChange={set('out_of_scope')}
              placeholder={'https://example.com/admin\nhttps://example.com/payment'}
            />
          </Field>
        </div>

        <div className="form-grid form-grid--2">
          <Field label="Flag 正则（靶场）" hint="靶场目标用于验收，留空表示非靶场。">
            <Input className="mono" value={form.flag_regex} onChange={set('flag_regex')} placeholder="flag\{[a-zA-Z0-9_-]+\}" />
          </Field>
          <Field label="验证格式" hint="例如 flag / custom，留空使用默认。">
            <Input className="mono" value={form.validation_format} onChange={set('validation_format')} />
          </Field>
        </div>

        <div className="form-grid form-grid--2">
          <Field label="置信度阈值" hint="0 ~ 1，低于阈值的发现不入库。">
            <Input type="number" step="0.05" min="0" max="1" value={form.confidence_threshold} onChange={set('confidence_threshold')} />
          </Field>
          <Field label="最大树深" hint="1 ~ 16，限制任务拆解深度。">
            <Input type="number" min="1" max="16" value={form.max_tree_depth} onChange={set('max_tree_depth')} />
          </Field>
        </div>

        <Checkbox
          label="启用该目标（停用后不可下发新任务）"
          checked={form.enabled}
          onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
        />
      </div>
    </Modal>
  )
}

function blank() {
  return {
    name: '',
    url: '',
    description: '',
    in_scope: '',
    out_of_scope: '',
    flag_regex: '',
    validation_format: '',
    confidence_threshold: '0.6',
    max_tree_depth: '4',
    enabled: true,
  }
}

function fromTarget(t) {
  return {
    name: t.name || '',
    url: t.url || '',
    description: t.description || '',
    in_scope: (t.in_scope || []).join('\n'),
    out_of_scope: (t.out_of_scope || []).join('\n'),
    flag_regex: t.flag_regex || '',
    validation_format: t.validation_format || '',
    confidence_threshold: String(t.confidence_threshold ?? 0.6),
    max_tree_depth: String(t.max_tree_depth ?? 4),
    enabled: !!t.enabled,
  }
}

function splitLines(v) {
  return String(v || '')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}
