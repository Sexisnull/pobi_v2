import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 通用数据请求。
 *
 * fetcher 每次渲染都会被重新读取（存于 ref），因此无需是稳定引用；
 * deps 变化或调用 reload 时重新拉取。
 */
export function useApi(fetcher, deps = [], { enabled = true } = {}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState(null)
  const alive = useRef(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetcherRef.current()
      if (alive.current) setData(res)
      return res
    } catch (e) {
      if (alive.current) setError(e)
      throw e
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    run().catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, run, ...deps])

  return { data, loading, error, reload: run, setData }
}

/** 轮询刷新。fn 需为稳定引用（用 useCallback 包裹）以避免重置定时器。 */
export function usePolling(fn, intervalMs, enabled = true) {
  useEffect(() => {
    if (!enabled || !intervalMs) return undefined
    const id = setInterval(fn, intervalMs)
    return () => clearInterval(id)
  }, [fn, intervalMs, enabled])
}

/** 变更操作：返回包装函数与进行中标志。 */
export function useAction() {
  const [pending, setPending] = useState(false)
  const run = useCallback(async (fn) => {
    setPending(true)
    try {
      return await fn()
    } finally {
      setPending(false)
    }
  }, [])
  return [run, pending]
}

/** 复制到剪贴板，返回 [是否刚复制成功, 复制函数]。 */
export function useCopy(timeout = 1600) {
  const [copied, setCopied] = useState(false)
  const copy = useCallback(
    async (text) => {
      try {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), timeout)
        return true
      } catch {
        return false
      }
    },
    [timeout],
  )
  return [copied, copy]
}
