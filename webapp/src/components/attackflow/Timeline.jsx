import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { EmptyState } from '../ui.jsx'

/**
 * 攻击时间轴（零依赖）：每任务一条泳道，X 轴为时间，桶内按事件大类分段着色。
 *
 * 后端只回传时间桶计数（无明细），故渲染量固定：桶数 ≤300、泳道 ≤20，
 * 与目标事件总量无关。空档（无桶）即"空转区间"，靠留白自然弱化。
 */

const LANE_H = 34
const AXIS_H = 22
const MIN_ZOOM = 0.01

export const EVENT_CLASS_META = {
  recon: { label: '侦察' },
  tool: { label: '工具' },
  finding: { label: '发现' },
  error: { label: '错误' },
  other: { label: '其他' },
}

const CLASS_ORDER = ['recon', 'tool', 'finding', 'error', 'other']

function sumCounts(counts) {
  let n = 0
  for (const k of Object.keys(counts || {})) n += counts[k] || 0
  return n
}

function tickTimes(t0, t1, win) {
  const a = t0 + win[0] * (t1 - t0)
  const b = t0 + win[1] * (t1 - t0)
  const span = Math.max(1, b - a)
  const steps = [
    1e3, 5e3, 1e4, 3e4, 6e4, 3e5, 9e5, 18e5, 36e5,
    108e5, 216e5, 432e5, 864e5,
  ]
  const step = steps.find((s) => span / s <= 6) || 864e5
  const out = []
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) out.push(t)
  return { a, b, ticks: out }
}

function fmtTick(t, spanMs) {
  const d = new Date(t)
  const p = (n) => String(n).padStart(2, '0')
  if (spanMs < 6e5) return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  if (spanMs < 864e5) return `${p(d.getHours())}:${p(d.getMinutes())}`
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export default function Timeline({ timeline, hidden, onPick }) {
  const { start, end, bucket_ms: bucketMs = 0, lanes = [] } = timeline || {}
  const t0 = start ? new Date(start).getTime() : 0
  const t1 = end ? new Date(end).getTime() : 0
  const span = Math.max(1, t1 - t0)

  const [win, setWin] = useState([0, 1])
  const [drag, setDrag] = useState(null)
  const [hover, setHover] = useState(null)
  const tracksRef = useRef(null)

  useEffect(() => {
    setWin([0, 1])
  }, [start, end])

  const visible = useMemo(
    () => lanes.filter((l) => !hidden || !hidden.has(l.task_id)),
    [lanes, hidden],
  )

  const maxCount = useMemo(() => {
    let m = 1
    for (const lane of visible) {
      for (const b of lane.buckets || []) m = Math.max(m, sumCounts(b.counts))
    }
    return m
  }, [visible])

  const posOf = useCallback(
    (iso) => (new Date(iso).getTime() - t0) / span,
    [t0, span],
  )

  /** 屏幕 x → 全量区间的归一化位置（0~1） */
  const fracAt = useCallback(
    (clientX) => {
      const el = tracksRef.current
      if (!el) return 0
      const rect = el.getBoundingClientRect()
      if (!rect.width) return 0
      const f = (clientX - rect.left) / rect.width
      return Math.min(1, Math.max(0, win[0] + f * (win[1] - win[0])))
    },
    [win],
  )

  // 滚轮缩放（需 passive:false 才能 preventDefault）
  useEffect(() => {
    const el = tracksRef.current
    if (!el) return undefined
    const onWheel = (e) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const f = rect.width ? (e.clientX - rect.left) / rect.width : 0.5
      const pos = win[0] + f * (win[1] - win[0])
      const k = e.deltaY > 0 ? 1.3 : 1 / 1.3
      let a = pos - (pos - win[0]) * k
      let b = pos + (win[1] - pos) * k
      if (b - a > 1) {
        a = 0
        b = 1
      }
      if (b - a < MIN_ZOOM) {
        const mid = (a + b) / 2
        a = Math.max(0, mid - MIN_ZOOM / 2)
        b = Math.min(1, a + MIN_ZOOM)
      }
      setWin([Math.max(0, a), Math.min(1, b)])
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [win])

  const onMouseDown = (e) => {
    if (e.button !== 0) return
    const pos = fracAt(e.clientX)
    setDrag({ a: pos, b: pos })
  }

  const onMouseMove = (e) => {
    if (!drag) return
    setDrag({ ...drag, b: fracAt(e.clientX) })
  }

  const onMouseUp = () => {
    if (!drag) return
    const a = Math.min(drag.a, drag.b)
    const b = Math.max(drag.a, drag.b)
    setDrag(null)
    if (b - a > 0.02) setWin([a, b])
  }

  if (!start || !visible.length) {
    return (
      <EmptyState
        icon="activity"
        title="暂无事件时间轴"
        desc="该目标下的任务还没有落库事件，执行侦察任务后会在此呈现执行节奏。"
      />
    )
  }

  const view = tickTimes(t0, t1, win)
  const winSpan = Math.max(1, win[1] - win[0])
  const bucketFrac = bucketMs / span
  const toLeft = (pos) => ((pos - win[0]) / winSpan) * 100

  return (
    <div className="tl">
      <div className="tl__legend">
        {CLASS_ORDER.map((k) => (
          <span className="tl__legend-item" key={k}>
            <i className={`tl__swatch tl__swatch--${k}`} />
            {EVENT_CLASS_META[k].label}
          </span>
        ))}
        <span className="tl__legend-hint">滚轮缩放 · 拖拽框选 · 双击重置</span>
        <span className="tl__spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => setWin([0, 1])}
          disabled={win[0] === 0 && win[1] === 1}
        >
          重置缩放
        </button>
      </div>

      <div className="tl__body">
        <div className="tl__labels">
          <div className="tl__labels-head" style={{ height: AXIS_H }} />
          {visible.map((lane) => (
            <div className="tl__label" key={lane.task_id} style={{ height: LANE_H }}>
              <span className={`status-dot status-dot--${lane.status}`} />
              <span className="tl__label-name" title={lane.name}>
                {lane.name}
              </span>
              <span className="tl__label-count">{lane.total}</span>
            </div>
          ))}
        </div>

        <div
          className="tl__tracks"
          ref={tracksRef}
          style={{ '--lane-h': `${LANE_H}px` }}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={onMouseUp}
          onMouseLeave={() => {
            setDrag(null)
            setHover(null)
          }}
          onDoubleClick={() => setWin([0, 1])}
        >
          <div className="tl__axis" style={{ height: AXIS_H }}>
            {view.ticks.map((t) => (
              <span
                className="tl__tick"
                key={t}
                style={{ left: `${((t - t0) / span - win[0]) / winSpan * 100}%` }}
              >
                {fmtTick(t, view.b - view.a)}
              </span>
            ))}
          </div>

          {visible.map((lane) => (
            <div className="tl__track" key={lane.task_id} style={{ height: LANE_H }}>
              {(lane.buckets || []).map((b) => {
                const left = toLeft(posOf(b.start))
                const width = (bucketFrac / winSpan) * 100
                if (left > 100 || left + width < 0) return null
                const total = sumCounts(b.counts)
                const h = Math.max(3, Math.round((total / maxCount) * (LANE_H - 10)))
                return (
                  <button
                    type="button"
                    className="tl__bar"
                    key={b.index}
                    style={{ left: `${left}%`, width: `max(2px, ${width}%)`, height: h }}
                    onMouseEnter={(e) =>
                      setHover({ x: e.clientX, y: e.clientY, lane, bucket: b, total })
                    }
                    onMouseMove={(e) =>
                      setHover({ x: e.clientX, y: e.clientY, lane, bucket: b, total })
                    }
                    onMouseLeave={() => setHover(null)}
                    onClick={() => onPick?.(lane.task_id, b.start)}
                    aria-label={`${lane.name} ${b.start} 起 ${total} 个事件`}
                  >
                    {CLASS_ORDER.filter((k) => (b.counts || {})[k]).map((k) => (
                      <i
                        className={`tl__seg tl__seg--${k}`}
                        key={k}
                        style={{ flexGrow: b.counts[k] }}
                      />
                    ))}
                  </button>
                )
              })}
            </div>
          ))}

          {drag && (
            <div
              className="tl__brush"
              style={{
                left: `${toLeft(Math.min(drag.a, drag.b))}%`,
                width: `${(Math.abs(drag.b - drag.a) / winSpan) * 100}%`,
                top: AXIS_H,
              }}
            />
          )}
        </div>
      </div>

      {hover && (
        <div className="tl__tip" style={{ left: hover.x + 14, top: hover.y + 14 }}>
          <div className="tl__tip-head">{hover.lane.name}</div>
          <div className="tl__tip-time mono">{hover.bucket.start.slice(0, 19).replace('T', ' ')}</div>
          <div className="tl__tip-rows">
            {CLASS_ORDER.filter((k) => (hover.bucket.counts || {})[k]).map((k) => (
              <div className="tl__tip-row" key={k}>
                <i className={`tl__swatch tl__swatch--${k}`} />
                <span>{EVENT_CLASS_META[k].label}</span>
                <b>{hover.bucket.counts[k]}</b>
              </div>
            ))}
          </div>
          <div className="tl__tip-foot">点击跳到该时刻的任务回放</div>
        </div>
      )}
    </div>
  )
}
