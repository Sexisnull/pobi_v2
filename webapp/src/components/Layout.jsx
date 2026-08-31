import { useCallback, useEffect, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import Icon from './Icons.jsx'
import { useAuth } from '../auth.jsx'
import { approvalsApi } from '../api.js'

/**
 * 导航配置。
 * soon=true 的条目为已规划但后端尚未实现的页面，占位渲染为禁用态，
 * 便于后续新增页面时只需在此追加配置并补一条路由。
 */
const NAV_GROUPS = [
  {
    label: '运营',
    items: [
      { to: '/', label: '总览', icon: 'dashboard', exact: true },
      { to: '/tasks', label: '任务', icon: 'tasks' },
      { to: '/targets', label: '授权目标', icon: 'target' },
    ],
  },
  {
    label: '治理',
    items: [
      { to: '/approvals', label: '高危审批', icon: 'shieldCheck', badgeKey: 'approvals' },
      { to: '/audit', label: '审计日志', icon: 'scroll' },
    ],
  },
  {
    label: '系统',
    items: [
      { to: '/usage', label: 'Token 用量', icon: 'gauge' },
      { to: '/tokens', label: 'API 令牌', icon: 'key' },
      { to: '/health', label: '系统健康', icon: 'activity' },
    ],
  },
  {
    label: '规划中',
    items: [
      { to: '/reports', label: '报告中心', icon: 'file', soon: true },
      { to: '/rules', label: '检测规则库', icon: 'layers', soon: true },
      { to: '/team', label: '团队协作', icon: 'user', soon: true },
    ],
  },
]

function findSection(pathname) {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (item.exact ? pathname === item.to : pathname.startsWith(item.to)) return item
    }
  }
  return null
}

export default function Layout() {
  const location = useLocation()
  const { user, logout } = useAuth()
  const [mobileOpen, setMobileOpen] = useState(false)

  const section = findSection(location.pathname)
  const isConsole = /^\/tasks\/[^/]+$/.test(location.pathname)

  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  return (
    <div className="shell">
      <Sidebar open={mobileOpen} user={user} onLogout={logout} />
      {mobileOpen && (
        <div
          onClick={() => setMobileOpen(false)}
          style={{ position: 'fixed', inset: 0, zIndex: 39, background: 'rgba(0,0,0,.5)' }}
        />
      )}
      <div className="main">
        <Topbar section={section} user={user} onLogout={logout} onMenu={() => setMobileOpen((v) => !v)} />
        {/* 任务控制台为全屏三栏布局，需取消页面内边距与滚动 */}
        <main className={`page ${isConsole ? 'page--flush' : ''}`}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function Sidebar({ open, user, onLogout }) {
  const location = useLocation()
  const pending = usePendingApprovals()

  return (
    <aside className={`sidebar ${open ? 'sidebar--open' : ''}`}>
      <div className="sidebar__brand">
        <span className="sidebar__logo">
          <Icon name="shield" size={16} strokeWidth={2} />
        </span>
        <span style={{ minWidth: 0 }}>
          <div className="sidebar__title">Pobi v2</div>
          <div className="sidebar__subtitle">Autonomous Pentest</div>
        </span>
      </div>

      <nav className="sidebar__nav" aria-label="主导航">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.label}>
            <div className="nav-group__label">{group.label}</div>
            <div className="nav-group__items">
              {group.items.map((item) => {
                const active =
                  item.exact || item.to === '/'
                    ? location.pathname === item.to
                    : location.pathname.startsWith(item.to)
                if (item.soon) {
                  return (
                    <span key={item.to} className="nav-item nav-item--disabled" aria-disabled="true">
                      <span className="nav-item__icon">
                        <Icon name={item.icon} size={16} />
                      </span>
                      <span className="nav-item__label">{item.label}</span>
                      <span className="nav-soon">规划中</span>
                    </span>
                  )
                }
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    className={`nav-item ${active ? 'nav-item--active' : ''}`}
                    aria-current={active ? 'page' : undefined}
                  >
                    <span className="nav-item__icon">
                      <Icon name={item.icon} size={16} />
                    </span>
                    <span className="nav-item__label">{item.label}</span>
                    {item.badgeKey === 'approvals' && pending > 0 && (
                      <span className="nav-badge" aria-label={`${pending} 条待审批`}>
                        {pending > 99 ? '99+' : pending}
                      </span>
                    )}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="sidebar__footer">
        <div className="sidebar__user">
          <span className="sidebar__avatar">
            {(user?.email || '?').slice(0, 1).toUpperCase()}
          </span>
          <span className="sidebar__user-meta">
            <div className="sidebar__user-name truncate">{user?.full_name || user?.email || '未登录'}</div>
            <div className="sidebar__user-role">{user?.is_admin ? '管理员' : '操作员'}</div>
          </span>
          <button className="btn btn--ghost btn--icon btn--sm" onClick={onLogout} title="退出登录" aria-label="退出登录">
            <Icon name="logout" size={15} />
          </button>
        </div>
      </div>
    </aside>
  )
}

function Topbar({ section, user, onLogout, onMenu }) {
  return (
    <header className="topbar">
      <button className="btn btn--ghost btn--icon topbar__menu" onClick={onMenu} aria-label="切换导航">
        <Icon name="menu" size={18} />
      </button>
      <div className="topbar__crumb">
        <span>控制台</span>
        {section && (
          <>
            <Icon name="chevronRight" size={13} />
            <span className="topbar__crumb-current">{section.label}</span>
          </>
        )}
      </div>
      <div className="topbar__spacer" />
      <a className="link-pill" href="/docs" target="_blank" rel="noreferrer">
        <Icon name="external" size={13} />
        API 文档
      </a>
      <span className="link-pill" title={user?.email}>
        <Icon name="user" size={13} />
        <span className="truncate" style={{ maxWidth: 180 }}>
          {user?.email || '—'}
        </span>
      </span>
      <button className="btn btn--ghost btn--icon" onClick={onLogout} title="退出登录" aria-label="退出登录">
        <Icon name="logout" size={16} />
      </button>
    </header>
  )
}

/** 侧边栏待审批计数：立即拉取一次，之后每 30s 轮询。 */
function usePendingApprovals() {
  const [count, setCount] = useState(0)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await approvalsApi.list({ status: 'pending', limit: 100 })
        if (alive) setCount(res.length)
      } catch {
        /* 轮询失败静默，保留上一次计数 */
      }
    }
    load()
    const id = setInterval(load, 30000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  return count
}

export function PageHead({ title, icon, desc, actions }) {
  return (
    <div className="page-head">
      <div className="page-head__main">
        <h1 className="page-head__title">
          {icon && <Icon name={icon} size={20} />}
          {title}
        </h1>
        {desc && <p className="page-head__desc">{desc}</p>}
      </div>
      {actions && <div className="page-head__actions">{actions}</div>}
    </div>
  )
}
