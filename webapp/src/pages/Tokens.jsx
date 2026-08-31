import { useCallback, useState } from 'react'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  Field,
  Input,
  Modal,
  Select,
  useToast,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useAction, useApi } from '../hooks.js'
import { tokensApi } from '../api.js'
import { ago, dt } from '../format.js'

const EXPIRES = [
  { value: '', label: '长期有效' },
  { value: '7', label: '7 天' },
  { value: '30', label: '30 天' },
  { value: '90', label: '90 天' },
  { value: '365', label: '365 天' },
]

export default function Tokens() {
  const toast = useToast()
  const [createOpen, setCreateOpen] = useState(false)
  const [created, setCreated] = useState(null)
  const [revealed, setRevealed] = useState(null)
  const [pendingRevoke, setPendingRevoke] = useState(null)

  const fetcher = useCallback(() => tokensApi.list(), [])
  const { data: tokens, loading, reload } = useApi(fetcher, [])

  const [run, pending] = useAction()

  const onReveal = (t) =>
    run(async () => {
      try {
        const res = await tokensApi.reveal(t.id)
        setRevealed(res)
        toast('已解密明文令牌', 'success')
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  const onRevoke = () =>
    run(async () => {
      try {
        await tokensApi.revoke(pendingRevoke.id)
        toast('令牌已吊销', 'success')
        setPendingRevoke(null)
        reload()
      } catch (e) {
        toast(e.message, 'error')
      }
    })

  return (
    <>
      <PageHead
        title="API 令牌"
        icon="key"
        desc="个人访问令牌（PAT）用于脚本与 CI 直接调用平台 API，与登录态解耦、可独立吊销。"
        actions={
          <>
            <Button icon="refresh" onClick={reload} disabled={loading}>
              刷新
            </Button>
            <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
              创建令牌
            </Button>
          </>
        }
      />

      {(created || revealed) && (
        <Card title="请立即复制令牌明文" icon="alert" style={{ marginBottom: 24 }}>
          <div className="stack">
            <div className="token-secret">
              <Icon name="key" size={15} />
              <span className="token-secret__value">{created?.plaintext_token || revealed?.plaintext_token}</span>
              <Button
                size="sm"
                icon="copy"
                onClick={() => {
                  navigator.clipboard.writeText(created?.plaintext_token || revealed?.plaintext_token)
                  toast('已复制到剪贴板', 'success')
                }}
              >
                复制
              </Button>
            </div>
            <span className="muted" style={{ fontSize: 12 }}>
              关闭后该明文将不再展示。若后端启用了加密存储，可随时通过列表中的「查看」再次获取。
            </span>
            <div className="row">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setCreated(null)
                  setRevealed(null)
                }}
              >
                关闭
              </Button>
            </div>
          </div>
        </Card>
      )}

      <Card flush title="令牌列表">
        <DataTable
          columns={[
            { key: 'name', title: '名称', render: (t) => <span style={{ color: 'var(--text-primary)' }}>{t.name}</span> },
            {
              key: 'prefix',
              title: '前缀',
              width: 130,
              render: (t) => <span className="mono" style={{ fontSize: 12 }}>{t.prefix}</span>,
            },
            {
              key: 'scopes',
              title: '权限范围',
              width: 140,
              render: (t) => <span className="mono" style={{ fontSize: 12 }}>{(t.scopes || []).join(', ') || '*'}</span>,
            },
            {
              key: 'revoked',
              title: '状态',
              width: 100,
              render: (t) => {
                const expired = t.expires_at && new Date(t.expires_at).getTime() < Date.now()
                return t.revoked ? (
                  <Badge tone="neutral">已吊销</Badge>
                ) : expired ? (
                  <Badge tone="danger">已过期</Badge>
                ) : (
                  <Badge tone="accent">有效</Badge>
                )
              },
            },
            { key: 'last_used_at', title: '最近使用', width: 120, render: (t) => ago(t.last_used_at) },
            { key: 'expires_at', title: '过期时间', width: 132, render: (t) => <span className="mono" style={{ fontSize: 12 }}>{t.expires_at ? dt(t.expires_at) : '长期'}</span> },
            { key: 'created_at', title: '创建时间', width: 132, render: (t) => <span className="mono" style={{ fontSize: 12 }}>{dt(t.created_at)}</span> },
            {
              key: 'actions',
              title: '操作',
              width: 168,
              render: (t) => (
                <div className="row" onClick={(e) => e.stopPropagation()}>
                  <Button size="sm" variant="ghost" icon="eye" onClick={() => onReveal(t)} disabled={t.revoked}>
                    查看
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="ban"
                    onClick={() => setPendingRevoke(t)}
                    disabled={t.revoked}
                  >
                    吊销
                  </Button>
                </div>
              ),
            },
          ]}
          rows={tokens ?? []}
          loading={loading}
          empty="还没有创建任何令牌"
        />
      </Card>

      <CreateTokenModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(t) => {
          setCreated(t)
          setRevealed(null)
          setCreateOpen(false)
          reload()
        }}
      />

      <ConfirmDialog
        open={!!pendingRevoke}
        title="吊销令牌"
        message={`确认吊销「${pendingRevoke?.name || ''}」？使用该令牌的所有脚本将立即失去访问权限，且不可恢复。`}
        confirmText="吊销"
        danger
        loading={pending}
        onConfirm={onRevoke}
        onClose={() => setPendingRevoke(null)}
      />
    </>
  )
}

function CreateTokenModal({ open, onClose, onCreated }) {
  const toast = useToast()
  const [form, setForm] = useState({ name: '', expires_in_days: '', scopes: '*' })
  const [busy, setBusy] = useState(false)

  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) setForm({ name: '', expires_in_days: '', scopes: '*' })
  }

  const submit = async () => {
    if (!form.name.trim()) return toast('请填写令牌名称', 'warning')
    setBusy(true)
    try {
      const payload = { name: form.name.trim() }
      if (form.expires_in_days) payload.expires_in_days = Number(form.expires_in_days)
      const scopes = form.scopes
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      if (scopes.length && !(scopes.length === 1 && scopes[0] === '*')) payload.scopes = scopes
      const created = await tokensApi.create(payload)
      toast('令牌已创建', 'success')
      onCreated?.(created)
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="创建 API 令牌"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            创建
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field label="令牌名称" required hint="用于区分用途，例如 ci-runner / 本地脚本。">
          <Input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="ci-runner" />
        </Field>
        <Field label="有效期">
          <Select
            options={EXPIRES}
            value={form.expires_in_days}
            onChange={(e) => setForm((f) => ({ ...f, expires_in_days: e.target.value }))}
          />
        </Field>
        <Field label="权限范围" hint="逗号分隔；填 * 表示继承当前用户全部权限。">
          <Input
            className="mono"
            value={form.scopes}
            onChange={(e) => setForm((f) => ({ ...f, scopes: e.target.value }))}
            placeholder="tasks:read, targets:read"
          />
        </Field>
      </div>
    </Modal>
  )
}
