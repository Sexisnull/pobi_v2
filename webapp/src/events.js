/**
 * 任务事件的可读化。
 *
 * 事件总线信封为 { type, session_id, payload }，类型由 pobi_agent 的 EventHooks
 * 决定；此处只做展示映射，新增事件类型时在此补一条即可，未知类型有兜底渲染。
 */

export const EVENT_CATEGORY = {
  thought: { label: '思考', types: ['agent_thought'] },
  tool: { label: '工具', types: ['tool_call_start', 'tool_call_end'] },
  llm: { label: '模型', types: ['llm_iteration', 'llm_input', 'llm_response'] },
  plan: { label: '计划', types: ['plan_step', 'phase_changed'] },
  status: {
    label: '状态',
    types: [
      'agent_start',
      'agent_end',
      'agent_error',
      'agent_routed',
      'task_created',
      'task_expanded',
      'task_status_changed',
      'confidence_update',
      'validation_result',
    ],
  },
  report: { label: '报告', types: ['report_task_event'] },
  log: { label: '日志', types: ['log'] },
}

const TYPE_TO_CATEGORY = new Map(
  Object.entries(EVENT_CATEGORY).flatMap(([cat, def]) => def.types.map((t) => [t, cat])),
)

export function categoryOf(type) {
  return TYPE_TO_CATEGORY.get(type) || 'log'
}

/** 事件类型的展示名（中文）。 */
const TYPE_LABEL = {
  agent_start: '智能体启动',
  agent_end: '智能体结束',
  agent_error: '智能体异常',
  agent_thought: '思考',
  agent_routed: '路由决策',
  tool_call_start: '工具调用',
  tool_call_end: '工具返回',
  task_created: '任务创建',
  task_expanded: '任务拆解',
  task_status_changed: '状态流转',
  confidence_update: '置信度',
  validation_result: '验证结果',
  plan_step: '执行计划',
  phase_changed: '阶段切换',
  llm_iteration: '模型迭代',
  llm_input: '模型输入',
  llm_response: '模型输出',
  log: '日志',
  report_task_event: '评估报告',
}

export function typeLabel(type) {
  return TYPE_LABEL[type] || type
}

const STATUS_ZH = {
  pending: '待处理',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

function clip(v, n = 600) {
  const s = String(v ?? '').trim()
  return s.length > n ? `${s.slice(0, n)}…` : s
}

function conf(v) {
  const n = Number(v)
  return Number.isFinite(n) ? (n <= 1 ? `${(n * 100).toFixed(0)}%` : n.toFixed(2)) : '—'
}

/** 把事件渲染为一行可读文本。 */
export function describeEvent(type, p = {}) {
  switch (type) {
    case 'agent_start':
      return `启动智能体 ${p.agent_name || '?'}（${p.role || 'agent'}）→ ${clip(p.task, 120)}`
    case 'agent_end':
      return `智能体 ${p.agent_name || '?'} 完成，置信度 ${conf(p.confidence_score)}${p.notes ? ` · ${clip(p.notes, 160)}` : ''}`
    case 'agent_error':
      return `[${p.error_type || 'error'}] ${p.agent_name || '?'}：${clip(p.error_message, 400)}`
    case 'agent_thought':
      return clip(p.summary || p.thought, 400)
    case 'agent_routed':
      return `路由到 ${p.selected_agent || '?'} · ${clip(p.reasoning, 200)}`
    case 'tool_call_start':
      return `${p.tool_name || '?'}(${clip(p.args, 300)})`
    case 'tool_call_end': {
      const ok = p.success ? '成功' : '失败'
      return `${p.tool_name || '?'} ${ok}${p.duration_ms ? ` · ${p.duration_ms}ms` : ''} · ${clip(p.error || p.result, 300)}`
    }
    case 'task_created':
      return `创建子任务 ${clip(p.task, 120)}（depth ${p.depth ?? 0}）`
    case 'task_expanded':
      return `拆解为 ${(p.subtasks || []).length} 个子任务：${clip(p.parent_task, 100)}`
    case 'task_status_changed':
      return `${clip(p.task, 100)}：${STATUS_ZH[p.old_status] || p.old_status} → ${STATUS_ZH[p.new_status] || p.new_status}`
    case 'confidence_update':
      return `${conf(p.old_confidence)} → ${conf(p.new_confidence)} · ${p.decision || ''}`
    case 'validation_result':
      return `${p.valid ? '通过' : '未通过'} · 置信度 ${conf(p.confidence_score)}${p.critique ? ` · ${clip(p.critique, 200)}` : ''}`
    case 'plan_step':
      return `${p.title || '步骤'} → ${STATUS_ZH[p.status] || p.status}${p.detail ? ` · ${clip(p.detail, 160)}` : ''}`
    case 'phase_changed':
      return `进入阶段 ${p.new_phase || '?'}${p.detail ? ` · ${clip(p.detail, 160)}` : ''}`
    case 'llm_iteration':
      return `${p.agent_name || '?'} 第 ${p.iteration ?? '?'} 轮推理（上下文 ${p.message_count ?? '?'} 条）`
    case 'llm_input':
      return `[${p.role || 'user'}]${p.tool_name ? ` ${p.tool_name}` : ''} ${clip(p.content, 400)}`
    case 'llm_response': {
      const body = p.response_text || p.thinking_text || ''
      return clip(body, 500)
    }
    case 'log':
      return clip(p.message, 300)
    case 'report_task_event':
      return clip(p.summary || p.content, 600)
    default:
      return clip(JSON.stringify(p), 300)
  }
}

/** 工具调用结果的成功/失败，用于着色。 */
export function eventTone(type, p = {}) {
  if (type === 'agent_error') return 'danger'
  if (type === 'tool_call_end' && p.success === false) return 'danger'
  if (type === 'validation_result' && p.valid === false) return 'danger'
  if (type === 'task_status_changed' && p.new_status === 'failed') return 'danger'
  if (type === 'report_task_event') return 'accent'
  return null
}
