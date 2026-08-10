import type { TransferFileStatus, TransferTreeNode } from './transferTree'

const STATUS_FLAGS: Record<TransferFileStatus, { label: string; glyph: string }> = {
  done: { label: 'done', glyph: '✓' },
  current: { label: 'current', glyph: '●' },
  prefetch: { label: 'prefetch', glyph: '◌' },
  error: { label: 'error', glyph: '!' },
  queued: { label: 'queued', glyph: '○' },
}

function StatusFlag({ status }: { status: TransferFileStatus }) {
  const { label, glyph } = STATUS_FLAGS[status]
  return (
    <span className={`transfer-batch-flag ${status}`} title={label}>
      {glyph}
    </span>
  )
}

function TreeRows({
  nodes,
  statuses,
  depth,
}: {
  nodes: TransferTreeNode[]
  statuses: Record<string, TransferFileStatus>
  depth: number
}) {
  return (
    <>
      {nodes.map((node) => {
        const status = node.kind === 'file' ? (statuses[node.path] ?? 'queued') : null
        return (
          <div key={node.path} className="transfer-batch-node">
            <div
              className={`transfer-batch-row ${node.kind}${status ? ` is-${status}` : ''}`}
              style={{ paddingLeft: 8 + depth * 12 }}
              title={node.path}
            >
              <span className="transfer-batch-kind">{node.kind === 'dir' ? '▸' : '·'}</span>
              <span className="transfer-batch-name">{node.name}</span>
              {status ? <StatusFlag status={status} /> : null}
            </div>
            {node.kind === 'dir' && node.children && node.children.length > 0 ? (
              <TreeRows nodes={node.children} statuses={statuses} depth={depth + 1} />
            ) : null}
          </div>
        )
      })}
    </>
  )
}

export function TransferBatchPanel({
  kind,
  tree,
  statuses,
  onClose,
}: {
  kind: 'upload' | 'download'
  tree: TransferTreeNode[]
  statuses: Record<string, TransferFileStatus>
  onClose: () => void
}) {
  const values = Object.values(statuses)
  const done = values.filter((s) => s === 'done').length
  const total = values.length
  const currents = Object.entries(statuses)
    .filter(([, s]) => s === 'current')
    .map(([path]) => path)
  const prefetching = Object.entries(statuses)
    .filter(([, s]) => s === 'prefetch')
    .map(([path]) => path)
  const active = currents.length + prefetching.length

  return (
    <div className="transfer-batch-panel" role="dialog" aria-label="Transfer queue">
      <div className="transfer-batch-header">
        <div className="transfer-batch-title">
          {kind === 'upload' ? 'Upload queue' : 'Download queue'}
        </div>
        <span className="transfer-batch-count">
          {done}/{total}
          {active > 1 ? ` · ${active} active` : ''}
        </span>
        <button
          type="button"
          className="btn-micro btn-icon"
          onClick={onClose}
          title="Hide queue"
          aria-label="Hide queue"
        >
          ×
        </button>
      </div>
      {currents.length === 1 ? (
        <div className="transfer-batch-current" title={currents[0]}>
          <span className="transfer-batch-flag current">●</span>
          <span className="transfer-file">{currents[0]}</span>
        </div>
      ) : null}
      {prefetching.length > 0 ? (
        <div className="transfer-batch-prefetch" title={prefetching.join(', ')}>
          <span className="transfer-batch-flag prefetch">◌</span>
          <span className="transfer-file">
            Prefetching {prefetching.length}: {prefetching[0]}
            {prefetching.length > 1 ? ` +${prefetching.length - 1}` : ''}
          </span>
        </div>
      ) : null}
      <div className="transfer-batch-body">
        {tree.length === 0 ? (
          <div className="empty-hint">No files</div>
        ) : (
          <TreeRows nodes={tree} statuses={statuses} depth={0} />
        )}
      </div>
      <div className="transfer-batch-legend">
        <span>
          <span className="transfer-batch-flag done">✓</span> done
        </span>
        <span>
          <span className="transfer-batch-flag current">●</span> current
        </span>
        {kind === 'download' ? (
          <span>
            <span className="transfer-batch-flag prefetch">◌</span> prefetch
          </span>
        ) : null}
        <span>
          <span className="transfer-batch-flag queued">○</span> queued
        </span>
      </div>
    </div>
  )
}
