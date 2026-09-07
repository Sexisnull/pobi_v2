import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { PageHead } from '../components/Layout.jsx'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  LoadingBlock,
  SeverityTag,
  Stat,
  Tabs,
} from '../components/ui.jsx'
import Icon from '../components/Icons.jsx'
import { useApi } from '../hooks.js'
import { targetsApi } from '../api.js'
import { TaskCreateModal } from './Tasks.jsx'
import { THREAT_STATUS, ago, bytes, dt, dtShort, num, pct, shortId, statusOf, truncate } from '../format.js'

const TABS = [
  { value: 'tree', label: '页面树' },
  { value: 'assets', label: '资产端点' },
  { value: 'facts', label: '侦察事实' },
  { value: 'threats', label: '威胁' },
  { value: 'findings', label: '漏洞发现' },
  { value: 'artifacts', label: '产物' },
]

const CVE_ID_RE = /^CVE-\d{4}-\d{4,7}$/i

function isCveId(value) {
  return !!value && CVE_ID_RE.test(String(value).trim())
}

export default function TargetDetail() {
  const { targetId } = useParams()
  const navigate = useNavigate()
  const [tab, setTab] = useState('tree')
  const [createOpen, setCreateOpen] = useState(false)


  const fetchTarget = useCallback(() => targetsApi.get(targetId), [targetId])
  const { data: target, loading, error } = useApi(fetchTarget, [targetId])

  const fetchOverview = useCallback(() => targetsApi.overview(targetId), [targetId])
  const { data: overview } = useApi(fetchOverview, [targetId])

  if (loading) return <LoadingBlock text="加载目标…" />
  if (error || !target)
    return (
      <EmptyState
        icon="alert"
        title="目标不存在或无权访问"
        desc="请返回授权目标列表重新选择。"
        action={
          <Link to="/targets">
            <Button variant="primary">返回列表</Button>
          </Link>
        }
      />
    )

  return (
    <>
      <PageHead
        title={target.name}
        icon="target"
        desc={
          <>
            <span className="mono">{target.url}</span>
            {target.description ? ` · ${target.description}` : ''}
          </>
        }
        actions={
          <>
            <Button icon="arrowLeft" onClick={() => navigate('/targets')}>
              返回
            </Button>
            <Button variant="primary" icon="zap" onClick={() => setCreateOpen(true)} disabled={!target.enabled}>
              下发任务
            </Button>
          </>
        }
      />

      <div className="stack">
        <div className="row row--wrap" style={{ gap: 8 }}>
          <Badge tone={target.enabled ? 'accent' : 'neutral'}>{target.enabled ? '已启用' : '已停用'}</Badge>
          {target.flag_regex && <Badge tone="warning">靶场目标</Badge>}
          <Badge tone="outline">置信度阈值 {pct(target.confidence_threshold)}</Badge>
          <Badge tone="outline">最大树深 {target.max_tree_depth}</Badge>
          {target.flag_regex && (
            <span className="mono muted" style={{ fontSize: 12 }}>
              flag: {target.flag_regex}
            </span>
          )}
        </div>

        <div className="grid grid--stats">
          <Stat label="侦察事实" icon="search" value={num(overview?.facts_count)} />
          <Stat label="资产端点" icon="globe" value={num(overview?.endpoints_count)} />
          <Stat label="威胁" icon="bug" value={num(overview?.threats_count)} tone="warning" />
          <Stat label="漏洞发现" icon="alert" value={num(overview?.findings_count)} tone="danger" />
          <Stat label="任务数" icon="tasks" value={num(overview?.tasks_count)} />
          <Stat
            label="峰值严重度"
            icon="flame"
            value={<SeverityTag value={overview?.severity_max || 'info'} />}
            hint={overview?.last_seen ? `最近活动 ${ago(overview.last_seen)}` : '暂无活动'}
          />
        </div>

        <Card flush title="授权范围" icon="lock">
          <div style={{ padding: '16px 20px' }}>
            <div className="scope-box">
              {(target.in_scope || []).length ? (
                target.in_scope.map((s, i) => (
                  <div className="scope-line scope-line--in" key={`in-${i}`}>
                    <span className="scope-line__mark">+</span>
                    <span>{s}</span>
                  </div>
                ))
              ) : (
                <div className="scope-line scope-line--in">
                  <span className="scope-line__mark">+</span>
                  <span className="muted">未限定，整站 {target.url} 均在授权范围内</span>
                </div>
              )}
              {(target.out_of_scope || []).map((s, i) => (
                <div className="scope-line scope-line--out" key={`out-${i}`}>
                  <span className="scope-line__mark">−</span>
                  <span>{s}</span>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <Card flush>
          <Tabs value={tab} onChange={setTab} tabs={TABS} />
          <TabPanel targetId={targetId} tab={tab} />
        </Card>
      </div>

      <TaskCreateModal
        open={createOpen}
        targets={[target]}
        presetTargetId={target.id}
        onClose={() => setCreateOpen(false)}
        onCreated={(task) => {
          setCreateOpen(false)
          navigate(`/tasks/${task.id}`)
        }}
      />
    </>
  )
}

function TabPanel({ targetId, tab }) {
  if (tab === 'tree') return <TreePanel targetId={targetId} />
  if (tab === 'assets') return <AssetsPanel targetId={targetId} />
  if (tab === 'facts') return <FactsPanel targetId={targetId} />
  if (tab === 'threats') return <ThreatsPanel targetId={targetId} />
  if (tab === 'findings') return <FindingsPanel targetId={targetId} />
  return <ArtifactsPanel targetId={targetId} />
}

function TreePanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.tree(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const [open, setOpen] = useState(() => new Set())
  const navigate = useNavigate()

  const hosts = data?.hosts || []
  const toggle = (h) =>
    setOpen((prev) => {
      const next = new Set(prev)
      next.has(h) ? next.delete(h) : next.add(h)
      return next
    })

  if (loading) return <LoadingBlock />
  if (!hosts.length)
    return <EmptyState icon="tree" title="尚未发现页面" desc="对目标执行一次侦察任务后，页面树会在此呈现。" />

  return (
    <div>
      {hosts.map((h) => {
        const expanded = open.has(h.host)
        return (
          <div className="tree-host" key={h.host}>
            <button className="tree-host__head" onClick={() => toggle(h.host)}>
              <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={13} />
              <Icon name="globe" size={13} />
              <span style={{ flex: 1 }}>{h.host || '(默认主机)'}</span>
              <span className="muted" style={{ fontSize: 11 }}>
                {h.paths.length} 个路径
              </span>
            </button>
            {expanded && (
              <div className="tree-host__paths">
                {h.paths.map((p, i) => (
                  <div className="tree-path" key={`${p.path}-${i}`}>
                    {p.method && <span className="tree-path__code">{p.method}</span>}
                    <span className="tree-path__p mono">{p.path}</span>
                    {p.status_code ? <span className="tree-path__code">{p.status_code}</span> : null}
                    {p.auth_required && <Badge tone="warning">需认证</Badge>}
                    <SeverityTag value={p.threat_severity_max} />
                    {(p.tech_stack || []).slice(0, 2).map((t) => (
                      <span className="tree-path__tech" key={t}>
                        {t}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
      <div className="card__foot">
        <span className="muted">共 {data.total} 个端点</span>
        <div style={{ flex: 1 }} />
        <Button size="sm" variant="ghost" icon="zap" onClick={() => navigate(`/targets/${targetId}`)}>
          下发新任务以扩展 coverage
        </Button>
      </div>
    </div>
  )
}

function AssetsPanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.assets(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const rows = useMemo(() => data?.assets || [], [data])

  if (loading) return <LoadingBlock />
  if (!rows.length)
    return <EmptyState icon="globe" title="暂无资产端点" desc="侦察任务完成后，发现的主机与路径会收敛到此清单。" />

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>主机</th>
            <th>路径</th>
            <th>方法</th>
            <th className="num">状态码</th>
            <th>技术栈</th>
            <th>认证</th>
            <th>参数</th>
            <th>最近发现</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="mono" style={{ fontSize: 12 }}>{a.host}</td>
              <td className="mono" style={{ fontSize: 12 }}>{truncate(a.path, 60)}</td>
              <td className="mono" style={{ fontSize: 12 }}>{a.method || '—'}</td>
              <td className="num">{a.status_code ?? '—'}</td>
              <td>{(a.tech_stack || []).join(', ') || '—'}</td>
              <td>{a.auth_required ? <Badge tone="warning">需认证</Badge> : '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{(a.parameters || []).join(', ') || '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{dtShort(a.last_seen)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FactsPanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.facts(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const rows = data?.items || []

  if (loading) return <LoadingBlock />
  if (!rows.length)
    return <EmptyState icon="search" title="暂无侦察事实" desc="侦察阶段沉淀的技术指纹、组件版本等事实会在此汇总。" />

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>类别</th>
            <th>键</th>
            <th>值</th>
            <th className="num">置信度</th>
            <th>来源任务</th>
            <th>最近更新</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.id}>
              <td>
                <Badge tone="outline">{f.category || '—'}</Badge>
              </td>
              <td className="mono" style={{ fontSize: 12 }}>{f.key}</td>
              <td className="mono" style={{ fontSize: 12 }}>{truncate(f.value, 80)}</td>
              <td className="num">{pct(f.confidence)}</td>
              <td className="mono" style={{ fontSize: 12 }}>{(f.source_tasks || []).map(shortId).join(', ') || '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{dtShort(f.last_seen)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ThreatsPanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.threats(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const rows = data?.items || []

  if (loading) return <LoadingBlock />
  if (!rows.length)
    return <EmptyState icon="bug" title="暂无威胁" desc="侦察阶段识别的 CVE 与可疑风险点会在此列出。" />

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>严重度</th>
            <th>标题</th>
            <th>CVE</th>
            <th className="num">CVSS</th>
            <th>状态</th>
            <th>端点</th>
            <th className="num">置信度</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const st = statusOf(THREAT_STATUS, t.status)
            return (
              <tr key={t.id}>
                <td>
                  <SeverityTag value={t.severity} />
                </td>
                <td>
                  <div className="task-name">
                    <span className="task-name__main">{t.title}</span>
                    <span className="task-name__sub">{truncate(t.evidence_summary, 70)}</span>
                  </div>
                </td>
                {/* 自研漏洞（无 CVE 编号）时 cve_id 存的是漏洞名，仅 CVE 编号才展示 */}
                <td className="mono" style={{ fontSize: 12 }}>{isCveId(t.cve_id) ? t.cve_id : '—'}</td>
                <td className="num">{t.cvss_score ?? '—'}</td>
                <td>
                  <Badge tone={st.tone}>{st.label}</Badge>
                </td>
                <td className="mono" style={{ fontSize: 12 }}>{truncate(t.target_endpoint, 40) || '—'}</td>
                <td className="num">{pct(t.confidence)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function FindingsPanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.findings(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const navigate = useNavigate()
  const rows = data?.items || []

  if (loading) return <LoadingBlock />
  if (!rows.length)
    return <EmptyState icon="alert" title="暂无已验证漏洞" desc="任务完成并通过验证的漏洞会归档到此处。" />

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>严重度</th>
            <th>标题</th>
            <th>CWE</th>
            <th className="num">置信度</th>
            <th>描述</th>
            <th>发现时间</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.id}>
              <td>
                <SeverityTag value={f.severity} />
              </td>
              <td style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{f.title}</td>
              <td className="mono" style={{ fontSize: 12 }}>{f.cwe || '—'}</td>
              <td className="num">{f.confidence != null ? pct(f.confidence) : '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{truncate(f.description, 70) || '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{dtShort(f.created_at)}</td>
              <td>
                <Button size="sm" variant="ghost" onClick={() => navigate(`/tasks/${f.task_id}`)}>
                  查看任务
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ArtifactsPanel({ targetId }) {
  const fetcher = useCallback(() => targetsApi.artifacts(targetId), [targetId])
  const { data, loading } = useApi(fetcher, [targetId])
  const rows = data?.items || []

  if (loading) return <LoadingBlock />
  if (!rows.length)
    return <EmptyState icon="box" title="暂无产物" desc="PoC、截图、日志等任务产物会在此登记。" />

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>内容类型</th>
            <th className="num">大小</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="mono" style={{ fontSize: 12 }}>{a.name}</td>
              <td>
                <Badge tone="outline">{a.kind}</Badge>
              </td>
              <td className="mono" style={{ fontSize: 12 }}>{a.content_type || '—'}</td>
              <td className="num">{a.size_bytes != null ? bytes(a.size_bytes) : '—'}</td>
              <td className="mono" style={{ fontSize: 12 }}>{dt(a.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
