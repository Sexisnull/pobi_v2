import Icon from '../Icons.jsx'
import { typeLabel } from '../../events.js'

/**
 * 执行回放控制条：按 seq 游标重建执行过程。
 *
 * 只负责控件与游标，事件明细由调用方按窗口分页供给（长任务不一次加载全量）。
 */

const SPEEDS = [0.5, 1, 2, 4]

export default function ReplayBar({
  index,
  total,
  loaded,
  playing,
  speed,
  currentType,
  currentAt,
  loading,
  cursorActive,
  onToggle,
  onStep,
  onSeek,
  onSpeed,
  onShowAll,
  onLoadMore,
}) {
  const max = Math.max(0, (total || loaded || 1) - 1)
  const pos = Math.min(index, max)
  const pctVal = max > 0 ? (pos / max) * 100 : 0

  return (
    <div className="replay-bar">
      <div className="replay-bar__ctrls">
        <button
          type="button"
          className="btn btn--ghost btn--sm btn--icon"
          onClick={onToggle}
          disabled={!total}
          title={playing ? '暂停' : '播放'}
          aria-label={playing ? '暂停' : '播放'}
        >
          <Icon name={playing ? 'stop' : 'play'} size={14} />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm btn--icon"
          onClick={() => onStep(-1)}
          disabled={pos <= 0}
          title="上一步"
          aria-label="上一步"
        >
          <Icon name="chevronLeft" size={14} />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm btn--icon"
          onClick={() => onStep(1)}
          disabled={pos >= max}
          title="下一步"
          aria-label="下一步"
        >
          <Icon name="chevronRight" size={14} />
        </button>
      </div>

      <div className="replay-bar__scrub">
        <input
          type="range"
          className="replay-bar__range"
          min={0}
          max={max}
          value={pos}
          disabled={!total}
          onChange={(e) => onSeek(Number(e.target.value))}
          aria-label="回放进度"
          style={{ '--pct': `${pctVal}%` }}
        />
        <div className="replay-bar__meta">
          <span className="mono">
            {total ? pos + 1 : 0} / {total || 0}
          </span>
          {loaded < total && (
            <button type="button" className="replay-bar__link" onClick={onLoadMore} disabled={loading}>
              {loading ? '加载中…' : `已加载 ${loaded}，加载更多`}
            </button>
          )}
          {currentType && <span className="badge badge--outline">{typeLabel(currentType)}</span>}
          {currentAt && <span className="mono muted">{currentAt}</span>}
          {cursorActive && (
            <button type="button" className="replay-bar__link" onClick={onShowAll}>
              显示全部
            </button>
          )}
        </div>
      </div>

      <select
        className="select select--sm"
        value={speed}
        onChange={(e) => onSpeed(Number(e.target.value))}
        aria-label="回放倍速"
        title="回放倍速"
      >
        {SPEEDS.map((s) => (
          <option key={s} value={s}>
            {s}×
          </option>
        ))}
      </select>
    </div>
  )
}
