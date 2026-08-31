import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import Icon from './Icons.jsx'

/* ------------------------------------------------------------------ 徽标 */

export function Badge({ tone = 'neutral', children, title }) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  )
}

export function SeverityTag({ value }) {
  const v = String(value || 'info').toLowerCase()
  return <span className={`sev sev--${v}`}>{v}</span>
}

export function StatusDot({ status, title }) {
  return <span className={`status-dot status-dot--${status}`} title={title || status} />
}

/* ------------------------------------------------------------------ 按钮 */

export function Button({
  variant = 'default',
  size,
  icon,
  iconRight,
  loading,
  block,
  className = '',
  children,
  ...rest
}) {
  const cls = [
    'btn',
    variant !== 'default' && `btn--${variant}`,
    size && `btn--${size}`,
    block && 'btn--block',
    className,
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <button className={cls} disabled={loading || rest.disabled} {...rest}>
      {loading ? <span className="spinner" /> : icon ? <Icon name={icon} size={15} /> : null}
      {children}
      {iconRight && !loading ? <Icon name={iconRight} size={15} /> : null}
    </button>
  )
}

export function IconButton({ icon, label, size = 'sm', ...rest }) {
  return (
    <button className="btn btn--ghost btn--icon" title={label} aria-label={label} {...rest}>
      <Icon name={icon} size={size === 'sm' ? 15 : 17} />
    </button>
  )
}

/* ------------------------------------------------------------------ 容器 */

export function Card({ title, icon, actions, foot, flush, elevated, className = '', style, children }) {
  return (
    <section className={`card ${elevated ? 'card--elevated' : ''} ${className}`} style={style}>
      {(title || actions) && (
        <header className="card__head">
          {title && (
            <h3 className="card__title">
              {icon && <Icon name={icon} size={15} />}
              {title}
            </h3>
          )}
          {actions && <div className="card__actions">{actions}</div>}
        </header>
      )}
      <div className={`card__body ${flush ? 'card__body--flush' : ''}`}>{children}</div>
      {foot && <footer className="card__foot">{foot}</footer>}
    </section>
  )
}

export function Stat({ label, icon, value, hint, tone, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag
      className={`stat ${tone ? `stat--${tone}` : ''} ${onClick ? 'stat--link' : ''}`}
      onClick={onClick}
      type={onClick ? 'button' : undefined}
    >
      <span className="stat__label">
        {icon && <Icon name={icon} size={13} />}
        {label}
      </span>
      <span className="stat__value">{value}</span>
      {hint && <span className="stat__hint">{hint}</span>}
    </Tag>
  )
}

export function EmptyState({ icon = 'box', title, desc, action }) {
  return (
    <div className="empty">
      <span className="empty__icon">
        <Icon name={icon} size={20} />
      </span>
      <span className="empty__title">{title}</span>
      {desc && <span className="empty__desc">{desc}</span>}
      {action}
    </div>
  )
}

export function LoadingBlock({ text = '加载中…' }) {
  return (
    <div className="loading-block">
      <span className="spinner spinner--lg" />
      {text}
    </div>
  )
}

export function Spinner() {
  return <span className="spinner" />
}

export function ProgressBar({ value, total, tone }) {
  const pctVal = total > 0 ? Math.min(100, Math.round((value / total) * 100)) : 0
  return (
    <div className="progress">
      <div className={`progress__bar ${tone ? `progress__bar--${tone}` : ''}`} style={{ width: `${pctVal}%` }} />
    </div>
  )
}

/* ------------------------------------------------------------------ 表单 */

export function Field({ label, required, hint, error, children }) {
  return (
    <label className="field">
      {label && (
        <span className="field__label">
          {label}
          {required && <span className="field__req">*</span>}
        </span>
      )}
      {children}
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  )
}

export function Input({ className = '', ...rest }) {
  return <input className={`input ${className}`} {...rest} />
}

export function Textarea({ className = '', mono, ...rest }) {
  return <textarea className={`textarea ${mono ? 'textarea--mono' : ''} ${className}`} {...rest} />
}

export function Select({ options, className = '', ...rest }) {
  return (
    <select className={`select ${className}`} {...rest}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

export function Checkbox({ label, className = '', ...rest }) {
  return (
    <label className={`checkbox ${className}`}>
      <input type="checkbox" {...rest} />
      <span className="checkbox__label">{label}</span>
    </label>
  )
}

export function SearchInput({ value, onChange, placeholder = '搜索…' }) {
  return (
    <div className="search">
      <span className="search__icon">
        <Icon name="search" size={14} />
      </span>
      <Input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </div>
  )
}

/* ------------------------------------------------------------------ 表格 */

export function DataTable({ columns, rows, empty, onRowClick, loading }) {
  if (loading) return <LoadingBlock />
  if (!rows.length) {
    return empty ?? <div className="table-empty">暂无数据</div>
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.num ? 'num' : ''} style={c.width ? { width: c.width } : undefined}>
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={row.id ?? i}
              className={onRowClick ? 'is-clickable' : ''}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((c) => (
                <td key={c.key} className={c.num ? 'num' : ''}>
                  {c.render ? c.render(row) : row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ------------------------------------------------------------------ 标签页 */

export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={t.value === value}
          className={`tab ${t.value === value ? 'tab--active' : ''}`}
          onClick={() => onChange(t.value)}
        >
          {t.label}
          {t.count !== undefined && <span className="tab__count">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ 弹层 */

export function Modal({ open, title, onClose, footer, wide, children }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => e.key === 'Escape' && onClose?.()
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className={`modal ${wide ? 'modal--wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className="modal__head">
          <h2 className="modal__title">{title}</h2>
          <IconButton icon="x" label="关闭" onClick={onClose} />
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__foot">{footer}</footer>}
      </div>
    </div>,
    document.body,
  )
}

export function ConfirmDialog({ open, title, message, confirmText = '确认', danger, loading, onConfirm, onClose }) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} loading={loading} onClick={onConfirm}>
            {confirmText}
          </Button>
        </>
      }
    >
      <p style={{ color: 'var(--text-secondary)' }}>{message}</p>
    </Modal>
  )
}

/* ------------------------------------------------------------------ Toast */

const ToastCtx = createContext(() => {})

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])
  const timers = useRef(new Map())

  const remove = useCallback((id) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
  }, [])

  const push = useCallback(
    (message, type = 'info') => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      setItems((prev) => [...prev, { id, message, type }])
      timers.current.set(
        id,
        setTimeout(() => remove(id), type === 'error' ? 6000 : 3600),
      )
    },
    [remove],
  )

  useEffect(() => {
    const map = timers.current
    return () => map.forEach(clearTimeout)
  }, [])

  const value = useMemo(() => push, [push])

  return (
    <ToastCtx.Provider value={value}>
      {children}
      {createPortal(
        <div className="toast-host" role="status" aria-live="polite">
          {items.map((t) => (
            <div key={t.id} className={`toast toast--${t.type}`}>
              <span className="toast__icon">
                <Icon
                  name={t.type === 'success' ? 'check' : t.type === 'error' ? 'alert' : t.type === 'warning' ? 'alert' : 'info'}
                  size={15}
                />
              </span>
              <span className="toast__msg">{t.message}</span>
              <IconButton icon="x" label="关闭" onClick={() => remove(t.id)} />
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastCtx.Provider>
  )
}

export const useToast = () => useContext(ToastCtx)

/* ------------------------------------------------------------------ 杂项 */

export function KV({ pairs }) {
  return (
    <div className="kv">
      {pairs
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([k, v, mono]) => (
          <div key={k} style={{ display: 'contents' }}>
            <span className="kv__k">{k}</span>
            <span className={`kv__v ${mono ? 'kv__v--mono' : ''}`}>{v}</span>
          </div>
        ))}
    </div>
  )
}

export function CodeBlock({ children, maxHeight }) {
  return (
    <pre className="code-block" style={maxHeight ? { maxHeight } : undefined}>
      {children}
    </pre>
  )
}

export function CopyButton({ text, label = '复制' }) {
  const toast = useToast()
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      toast('已复制到剪贴板', 'success')
    } catch {
      toast('复制失败，请手动选择文本', 'error')
    }
  }
  return (
    <Button size="sm" variant="ghost" icon="copy" onClick={copy}>
      {label}
    </Button>
  )
}
