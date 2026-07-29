import { useState } from 'react'
import { api, signedTon, ton } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Empty, Page, Stat } from '../components/ui'

const MARKET_LABELS: Record<string, string> = {
  portals: 'Portals',
  mrkt: 'MRKT',
  tonnel: 'Tonnel',
  getgems: 'GetGems',
}

export default function Dashboard() {
  const { data, error, reload } = useApi(() => api.status(), { pollMs: 10000 })
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const runCycle = async () => {
    setBusy(true)
    setMessage(null)
    try {
      await api.runCycle(true)
      await reload()
      setMessage('Цикл завершён')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) {
    return (
      <Page title="Обзор">
        <Alert tone="error">Не удалось получить состояние: {error}</Alert>
      </Page>
    )
  }
  if (!data) return <Page title="Обзор"><Empty>Загрузка…</Empty></Page>

  const engine = data.engine
  const risk = engine.risk

  return (
    <Page
      title="Обзор"
      subtitle={
        engine.paper_mode
          ? 'Paper-режим: сделки симулируются, деньги не тратятся'
          : 'LIVE-режим: сделки исполняются на реальные средства'
      }
      actions={
        <Button onClick={runCycle} disabled={busy} variant="primary">
          {busy ? 'Выполняется…' : 'Запустить цикл'}
        </Button>
      }
    >
      {message && (
        <div className="mb-4">
          <Alert tone="info">{message}</Alert>
        </div>
      )}

      {risk.tripped && (
        <div className="mb-4">
          <Alert tone="error">
            Предохранитель сработал: {risk.trip_reason}. Торговля остановлена — сбросьте
            его в разделе «Автомат» после устранения причины.
          </Alert>
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Реализовано за 30д"
          value={signedTon(data.realized_pnl_ton)}
          tone={data.realized_pnl_ton > 0 ? 'profit' : data.realized_pnl_ton < 0 ? 'loss' : undefined}
          hint="после комиссий и газа"
        />
        <Stat
          label="Бумажный P&L"
          value={signedTon(data.unrealized_pnl_ton)}
          tone="warn"
          hint="не прибыль, пока не продано"
        />
        <Stat
          label="Открытых позиций"
          value={`${data.open_positions} / ${risk.max_open_positions}`}
          hint={`вложено ${ton(data.invested_ton)}`}
        />
        <Stat
          label="Сигналов к покупке"
          value={data.signals_pending}
          hint={`бюджет: ${ton(risk.spent_today_ton)} из ${ton(risk.daily_budget_ton)}`}
        />
      </div>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h2 className="mb-4 text-sm font-medium text-slate-300">Площадки</h2>
          <div className="space-y-2">
            {Object.entries(data.marketplaces).map(([key, market]) => (
              <div
                key={key}
                className="flex items-center justify-between rounded-lg bg-ink-700 px-4 py-2.5"
              >
                <div className="flex items-center gap-3">
                  <span
                    className={`h-2 w-2 rounded-full ${market.connected ? 'bg-profit' : 'bg-slate-600'}`}
                  />
                  <span className="text-sm text-slate-200">{MARKET_LABELS[key] ?? key}</span>
                  <span
                    className={`badge ${
                      market.trade_enabled ? 'bg-accent/15 text-accent' : 'bg-ink-500 text-slate-400'
                    }`}
                  >
                    {market.trade_enabled ? 'торговля' : 'только цены'}
                  </span>
                </div>
                <span className="text-xs text-slate-500">{market.note}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="card">
          <h2 className="mb-4 text-sm font-medium text-slate-300">Последний цикл</h2>
          <dl className="space-y-2 text-sm">
            <Row label="Сигналов оценено" value={engine.last_cycle.signals_total} />
            <Row label="Прошло фильтры" value={engine.last_cycle.signals_passed} />
            <Row label="Куплено" value={engine.last_cycle.bought} />
            <Row label="Выставлено" value={engine.last_cycle.listed} />
            <Row label="Переоценено" value={engine.last_cycle.repriced} />
            <Row label="Закрыто" value={engine.last_cycle.closed} />
          </dl>

          {engine.last_cycle.errors.length > 0 && (
            <div className="mt-4 space-y-1 border-t border-ink-600 pt-3">
              {engine.last_cycle.errors.map((text) => (
                <div key={text} className="text-xs text-loss">
                  {text}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h2 className="mb-3 text-sm font-medium text-slate-300">Накоплено данных</h2>
        <div className="flex flex-wrap gap-6 text-sm">
          <div>
            <span className="text-slate-500">Коллекций: </span>
            <span className="font-mono text-slate-200">{data.data.collections_tracked}</span>
          </div>
          <div>
            <span className="text-slate-500">Сделок в истории: </span>
            <span className="font-mono text-slate-200">{data.data.sales_recorded}</span>
          </div>
          <div>
            <span className="text-slate-500">Безубыток при наценке: </span>
            <span className="font-mono text-warn">
              {(engine.breakeven_markup * 100).toFixed(1)}%
            </span>
          </div>
        </div>
        {data.data.sales_recorded < 100 && (
          <p className="mt-3 text-xs text-slate-500">
            Модель цены калибруется по истории сделок. Пока их меньше сотни, оценка
            опирается на флоры атрибутов и заведомо грубая — дайте сканеру поработать.
          </p>
        )}
      </div>
    </Page>
  )
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex justify-between">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-mono text-slate-200">{value}</dd>
    </div>
  )
}
