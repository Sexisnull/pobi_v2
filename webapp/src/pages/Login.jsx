import { useState } from 'react'
import Icon from '../components/Icons.jsx'
import { Field, Input, Button } from '../components/ui.jsx'
import { useAuth } from '../auth.jsx'

export default function Login() {
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(email.trim(), password)
    } catch (err) {
      setError(err.message || '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login">
      <div className="login__glow" aria-hidden="true" />
      <div className="login__panel">
        <div className="login__brand">
          <span className="login__logo">
            <Icon name="shield" size={22} strokeWidth={2} />
          </span>
          <div>
            <h1 className="login__title">Pobi v2</h1>
            <p className="login__subtitle">Autonomous Penetration Testing Platform</p>
          </div>
        </div>

        <form className="login__form" onSubmit={submit}>
          <Field label="邮箱" required>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="username"
              autoFocus
              required
            />
          </Field>
          <Field label="密码" required>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              required
            />
          </Field>

          {error && (
            <div className="login__error" role="alert">
              <Icon name="alert" size={14} />
              {error}
            </div>
          )}

          <Button type="submit" variant="primary" size="lg" block loading={loading}>
            登录控制台
          </Button>
        </form>

        <p className="login__note">
          <Icon name="lock" size={12} />
          所有操作均记录审计日志；仅可在已授权目标范围内执行测试。
        </p>
      </div>
    </div>
  )
}
