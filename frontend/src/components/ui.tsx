import type { ReactNode } from 'react'

export function Page({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="mx-auto max-w-7xl">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
        </div>
        {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
      </header>
      {children}
    </div>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint?: string
  tone?: 'profit' | 'loss' | 'warn'
}) {
  const toneClass =
    tone === 'profit'
      ? 'text-profit'
      : tone === 'loss'
        ? 'text-loss'
        : tone === 'warn'
          ? 'text-warn'
          : ''
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-600">{hint}</div>}
    </div>
  )
}

export function Button({
  children,
  onClick,
  variant = 'default',
  disabled,
  type = 'button',
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'default' | 'primary' | 'danger'
  disabled?: boolean
  type?: 'button' | 'submit'
}) {
  const styles = {
    default: 'bg-ink-600 text-slate-200 hover:bg-ink-500',
    primary: 'bg-accent/20 text-accent hover:bg-accent/30',
    danger: 'bg-loss/15 text-loss hover:bg-loss/25',
  }[variant]

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${styles}`}
    >
      {children}
    </button>
  )
}

export function Alert({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'warn' | 'error'
  children: ReactNode
}) {
  const styles = {
    info: 'border-accent/30 bg-accent/5 text-slate-300',
    warn: 'border-warn/30 bg-warn/5 text-warn',
    error: 'border-loss/40 bg-loss/5 text-loss',
  }[tone]
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm ${styles}`}>{children}</div>
  )
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-ink-600">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-ink-600 bg-ink-700/50">
            {head.map((title) => (
              <th
                key={title}
                className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-slate-500"
              >
                {title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-700">{children}</tbody>
      </table>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="card text-center text-sm text-slate-500">{children}</div>
  )
}

/** Полоска 0..1. Цвет задаётся явными классами — динамические Tailwind не собирает. */
export function Bar({ value }: { value: number }) {
  const clamped = Math.max(0, Math.min(1, value))
  const color =
    clamped >= 0.6 ? 'bg-profit' : clamped >= 0.35 ? 'bg-warn' : 'bg-loss'
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-ink-600">
        <div className={`h-full ${color}`} style={{ width: `${clamped * 100}%` }} />
      </div>
      <span className="font-mono text-xs text-slate-500">{clamped.toFixed(2)}</span>
    </div>
  )
}
