import { memo, useMemo, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import dagre from 'dagre'
import '@xyflow/react/dist/style.css'
import { EmptyState } from '../ui.jsx'
import Icon from '../Icons.jsx'
import { THREAT_STATUS, pct, shortId, statusOf } from '../../format.js'

/**
 * 情报关系图谱：把"威胁清单"还原成"攻击路径为什么成立"。
 *
 * 边全部来自现成关联：recon_threat_evidence_link（实线=显式证据）与
 * threat→endpoint 路径匹配 / finding→threat 同源命中（虚线=推导）。
 * 默认只渲染威胁连通子图，端点全量展开由后端 full_endpoints 开关控制。
 */

const NODE_W = 220
const NODE_H = 54

const TYPE_LABEL = {
  threat: '威胁',
  fact: '证据事实',
  endpoint: '端点',
  finding: '漏洞发现',
}

const SEV_VAR = {
  critical: 'var(--sev-critical)',
  high: 'var(--sev-high)',
  medium: 'var(--sev-medium)',
  low: 'var(--sev-low)',
  info: 'var(--sev-info)',
}

const SEV_MIN_OPTIONS = [
  { value: 'info', label: '全部严重度' },
  { value: 'low', label: 'low 及以上' },
  { value: 'medium', label: 'medium 及以上' },
  { value: 'high', label: 'high 及以上' },
  { value: 'critical', label: '仅 critical' },
]

const SEV_RANK = { info: 0, low: 1, medium: 2, high: 3, critical: 4 }
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  ...Object.keys(THREAT_STATUS).map((v) => ({ value: v, label: THREAT_STATUS[v].label })),
]

const FlowNode = memo(({ data, selected }) => (
  <div
    className={`fnode fnode--${data.type}`}
    style={{ '--sev': SEV_VAR[data.severity] || SEV_VAR.info }}
    data-selected={selected ? 'yes' : undefined}
  >
    <Handle type="target" position={Position.Left} className="fnode__handle" />
    <span className="fnode__type">{TYPE_LABEL[data.type] || data.type}</span>
    <span className="fnode__label" title={data.label}>
      {data.label}
    </span>
    <Handle type="source" position={Position.Right} className="fnode__handle" />
  </div>
))
FlowNode.displayName = 'FlowNode'

const NODE_TYPES = { flow: FlowNode }

function layoutGraph(nodes, edges) {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'LR', nodesep: 22, ranksep: 100, marginx: 24, marginy: 24 })
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }))
  const ids = new Set(nodes.map((n) => n.id))
  edges.forEach((e) => {
    if (ids.has(e.source) && ids.has(e.target)) g.setEdge(e.source, e.target)
  })
  dagre.layout(g)
  return nodes.map((n) => {
    const p = g.node(n.id)
    return {
      ...n,
      position: {
        x: (p?.x ?? 0) - NODE_W / 2,
        y: (p?.y ?? 0) - NODE_H / 2,
      },
    }
  })
}

function GraphCanvas({ graph, truncated }) {
  const [types, setTypes] = useState(() => new Set(Object.keys(TYPE_LABEL)))
  const [sevMin, setSevMin] = useState('info')
  const [status, setStatus] = useState('')
  const [selected, setSelected] = useState(null)

  const toggleType = (t) =>
    setTypes((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })

  const filtered = useMemo(() => {
    const nodes = (graph?.nodes || []).filter((n) => {
      if (!types.has(n.type)) return false
      if ((SEV_RANK[n.severity] ?? 0) < (SEV_RANK[sevMin] ?? 0)) return false
      if (status && n.type === 'threat' && n.status !== status) return false
      return true
    })
    const ids = new Set(nodes.map((n) => n.id))
    const edges = (graph?.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target))
    return { nodes, edges }
  }, [graph, types, sevMin, status])

  const rfNodes = useMemo(
    () =>
      layoutGraph(
        filtered.nodes.map((n) => ({
          id: n.id,
          type: 'flow',
          data: n,
        })),
        filtered.edges,
      ),
    [filtered],
  )

  const rfEdges = useMemo(
    () =>
      filtered.edges.map((e, i) => ({
        id: `e-${i}`,
        source: e.source,
        target: e.target,
        className: `fedge fedge--${e.relation} fedge--${e.kind}`,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      })),
    [filtered],
  )

  const neighbors = useMemo(() => {
    if (!selected) return []
    const out = []
    for (const e of graph?.edges || []) {
      if (e.source === selected) out.push({ dir: '→', id: e.target, relation: e.relation })
      else if (e.target === selected) out.push({ dir: '←', id: e.source, relation: e.relation })
    }
    return out
  }, [graph, selected])

  const nodeById = useMemo(() => {
    const m = new Map()
    for (const n of graph?.nodes || []) m.set(n.id, n)
    return m
  }, [graph])

  const detail = selected ? nodeById.get(selected) : null

  if (!graph?.nodes?.length) {
    return (
      <EmptyState
        icon="layers"
        title="暂无可关联的情报"
        desc="侦察产出威胁后，事实 → 威胁 → 端点的攻击路径会在此呈现。"
      />
    )
  }

  return (
    <div className="graph">
      <div className="graph__toolbar">
        {Object.keys(TYPE_LABEL).map((t) => (
          <button
            key={t}
            type="button"
            className={`event-chip ${types.has(t) ? 'event-chip--active' : ''}`}
            onClick={() => toggleType(t)}
          >
            {TYPE_LABEL[t]}
          </button>
        ))}
        <span className="graph__sep" />
        <select
          className="select select--sm"
          value={sevMin}
          onChange={(e) => setSevMin(e.target.value)}
          aria-label="严重度下限"
        >
          {SEV_MIN_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="select select--sm"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="威胁状态"
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <span className="tl__spacer" />
        <span className="graph__stat muted">
          {filtered.nodes.length} 节点 / {filtered.edges.length} 边
          {truncated ? '（已达上限，已截断）' : ''}
        </span>
      </div>

      <div className="graph__canvas">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={NODE_TYPES}
          onNodeClick={(_, n) => setSelected(n.id)}
          onPaneClick={() => setSelected(null)}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.15}
          proOptions={{ hideAttribution: true }}
          nodesConnectable={false}
          elementsSelectable
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#1f2a3f" />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable className="graph__minimap" />
        </ReactFlow>

        {detail && (
          <aside className="graph__drawer">
            <header className="graph__drawer-head">
              <span className="graph__drawer-type">{TYPE_LABEL[detail.type]}</span>
              <button
                type="button"
                className="btn btn--ghost btn--sm btn--icon"
                onClick={() => setSelected(null)}
                aria-label="关闭详情"
              >
                <Icon name="x" size={14} />
              </button>
            </header>
            <h4 className="graph__drawer-title">{detail.label}</h4>
            <div className="graph__drawer-tags">
              {detail.type === 'threat' && (
                <>
                  <span className={`sev sev--${detail.severity}`}>{detail.severity}</span>
                  <span className={`badge badge--${statusOf(THREAT_STATUS, detail.status).tone}`}>
                    {statusOf(THREAT_STATUS, detail.status).label}
                  </span>
                </>
              )}
              <span className="badge badge--outline">置信度 {pct(detail.confidence)}</span>
            </div>

            <dl className="graph__kv">
              {Object.entries(detail.detail || {})
                .filter(([, v]) => v !== null && v !== undefined && v !== '' && v !== false)
                .map(([k, v]) => (
                  <div key={k}>
                    <dt>{DETAIL_LABEL[k] || k}</dt>
                    <dd className="mono">
                      {Array.isArray(v) ? v.join(', ') : v === true ? '是' : String(v)}
                    </dd>
                  </div>
                ))}
            </dl>

            {!!detail.source_tasks.length && (
              <div className="graph__tasks">
                <span className="graph__section">来源任务</span>
                <div className="graph__task-list">
                  {detail.source_tasks.slice(0, 6).map((t) => (
                    <a key={t} href={`/tasks/${t}`} className="graph__task mono">
                      {shortId(t)}
                    </a>
                  ))}
                </div>
              </div>
            )}

            {!!neighbors.length && (
              <div className="graph__tasks">
                <span className="graph__section">关联节点 {neighbors.length}</span>
                <div className="graph__task-list">
                  {neighbors.slice(0, 12).map((n) => (
                    <button
                      key={`${n.dir}-${n.id}`}
                      type="button"
                      className="graph__task graph__task--btn"
                      onClick={() => setSelected(n.id)}
                      title={nodeById.get(n.id)?.label || n.id}
                    >
                      <span className="graph__task-dir">{n.dir}</span>
                      {nodeById.get(n.id)?.label || n.id}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
  )
}

const DETAIL_LABEL = {
  cve_id: 'CVE',
  category: '类别',
  cvss_score: 'CVSS',
  target_endpoint: '目标端点',
  evidence_summary: '证据摘要',
  value: '值',
  key: '键',
  host: '主机',
  path: '路径',
  method: '方法',
  status_code: '状态码',
  auth_required: '需认证',
  tech_stack: '技术栈',
  cwe: 'CWE',
  description: '描述',
  task_id: '任务',
}

export default function GraphView(props) {
  return (
    <ReactFlowProvider>
      <GraphCanvas {...props} />
    </ReactFlowProvider>
  )
}
