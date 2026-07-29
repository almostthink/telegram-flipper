import type { ReactNode } from 'react'

interface PageProps {
  title: string
  subtitle?: string
  children?: ReactNode
}

export function Page({ title, subtitle, children }: PageProps) {
  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </header>
      {children}
    </div>
  )
}

/** Заглушка страницы: честно показывает, что появится и на каком этапе. */
export function Planned({ stage, items }: { stage: number; items: string[] }) {
  return (
    <div className="card">
      <div className="mb-3 flex items-center gap-2">
        <span className="badge bg-accent/15 text-accent">Этап {stage}</span>
        <span className="text-sm text-slate-500">ещё не реализовано</span>
      </div>
      <ul className="space-y-2">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-sm text-slate-400">
            <span className="text-slate-600">—</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
