import { useEffect, useState } from 'react'
import { api, type Status } from '../lib/api'
import { Page } from '../components/Page'

const MARKET_LABELS: Record<string, string> = {
  portals: 'Portals',
  mrkt: 'MRKT',
  tonnel: 'Tonnel',
  getgems: 'GetGems',
}

export default function Dashboard() {
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .status()
      .then(setStatus)
      .catch((e: Error) => setError(e.message))
  }, [])

  if (error) {
    return (
      <Page title="Обзор">
        <div className="card border-loss/40 text-sm text-loss">
          Не удалось получить состояние: {error}
        </div>
      </Page>
    )
  }

  return (
    <Page title="Обзор" subtitle={status?.stage}>
      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Баланс"
          value={status?.balance_ton == null ? '—' : `${status.balance_ton} TON`}
          hint={status?.balance_ton == null ? 'кошелёк не подключён' : undefined}
        />
        <Stat label="Открытых позиций" value={status?.open_positions ?? '—'} />
        <Stat
          label="Реализованный P&L"
          value={fmtTon(status?.realized_pnl_ton)}
          tone={toneOf(status?.realized_pnl_ton)}
        />
        <Stat
          label="Бумажный P&L"
          value={fmtTon(status?.unrealized_pnl_ton)}
          tone={toneOf(status?.unrealized_pnl_ton)}
          hint="не прибыль, пока не продано"
        />
      </div>

      <div className="card">
        <h2 className="mb-4 text-sm font-medium text-slate-300">Площадки</h2>
        <div className="space-y-2">
          {status &&
            Object.entries(status.marketplaces).map(([key, m]) => (
              <div
                key={key}
                className="flex items-center justify-between rounded-lg bg-ink-700 px-4 py-3"
              >
                <div className="flex items-center gap-3">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      m.connected ? 'bg-profit' : 'bg-slate-600'
                    }`}
                  />
                  <span className="text-sm text-slate-200">
                    {MARKET_LABELS[key] ?? key}
                  </span>
                  <span
                    className={`badge ${
                      m.trade_enabled
                        ? 'bg-accent/15 text-accent'
                        : 'bg-ink-500 text-slate-400'
                    }`}
                  >
                    {m.trade_enabled ? 'торговля' : 'только цены'}
                  </span>
                </div>
                <span className="text-xs text-slate-500">{m.note}</span>
              </div>
            ))}
        </div>
      </div>
    </Page>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string | number
  hint?: string
  tone?: 'profit' | 'loss'
}) {
  const toneClass =
    tone === 'profit' ? 'text-profit' : tone === 'loss' ? 'text-loss' : ''
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-600">{hint}</div>}
    </div>
  )
}

function fmtTon(value: number | undefined): string {
  if (value == null) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)} TON`
}

function toneOf(value: number | undefined): 'profit' | 'loss' | undefined {
  if (value == null || value === 0) return undefined
  return value > 0 ? 'profit' : 'loss'
}
