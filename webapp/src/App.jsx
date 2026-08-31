import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import { LoadingBlock } from './components/ui.jsx'
import { AuthProvider, useAuth } from './auth.jsx'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Tasks from './pages/Tasks.jsx'
import TaskConsole from './pages/TaskConsole.jsx'
import Targets from './pages/Targets.jsx'
import TargetDetail from './pages/TargetDetail.jsx'
import Approvals from './pages/Approvals.jsx'
import Audit from './pages/Audit.jsx'
import Usage from './pages/Usage.jsx'
import Tokens from './pages/Tokens.jsx'
import Health from './pages/Health.jsx'

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<GuestOnly />} />
        <Route element={<RequireAuth />}>
          <Route path="/" element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="tasks/:taskId" element={<TaskConsole />} />
            <Route path="targets" element={<Targets />} />
            <Route path="targets/:targetId" element={<TargetDetail />} />
            <Route path="approvals" element={<Approvals />} />
            <Route path="audit" element={<Audit />} />
            <Route path="usage" element={<Usage />} />
            <Route path="tokens" element={<Tokens />} />
            <Route path="health" element={<Health />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  )
}

function RequireAuth() {
  const { user, booting } = useAuth()
  if (booting) return <LoadingBlock text="正在验证登录状态…" />
  if (!user) return <Navigate to="/login" replace />
  return <Outlet />
}

/** 已登录用户访问 /login 时直接回到首页。 */
function GuestOnly() {
  const { user, booting } = useAuth()
  if (booting) return <LoadingBlock text="正在验证登录状态…" />
  if (user) return <Navigate to="/" replace />
  return <Login />
}
