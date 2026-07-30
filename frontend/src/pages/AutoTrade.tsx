import { useEffect, useState } from 'react'
import { api, percent, ton } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Page, Stat } from '../components/ui'

export default function AutoTrade() {
  const { data, error, reload } = useApi(() => api.engine(), { pollMs: 10000 })
  const config = useApi(() => api.config())
  const [whitelist, setWhitelist] = useState('')
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)

  useEffect(() => {
    const list = config.data?.risk?.collection_whitelist
    if (Array.isArray(list)) setWhitelist(list.join('\n'))
  }, [config.data])

  const patch = async (body: Record<string, unknown>) => {
    try {
      await api.updateConfig(body)
      await Promise.all([reload(), config.reload()])
      setMessage({ tone: 'info', text: 'Сохранено' })
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    }
  }

  if (error && !data) {
    return (
      <Page title="Автомат">
        <Alert tone="error">{error}</Alert>
      </Page>
    )
  }
  if (!data || !config.data) return <Page title="Автомат">Загрузка…</Page>

  const risk = data.risk
  const limits = config.data.risk as Record<string, number>
  const whitelistItems = whitelist.split('\n').map((s) => s.trim()).filter(Boolean)
  const canEnableAuto = !data.paper_mode && whitelistItems.length > 0

  return (
    <Page
      title="Автомат"
      subtitle="Автоматическая торговля работает только под жёсткими лимитами"
      actions={
        <Button variant="danger" onClick={() => void api.kill().then(() => reload())}>
          Остановить всё
        </Button>
      }
    >
      {message && (
        <div className="mb-4">
          <Alert tone={message.tone === 'error' ? 'error' : 'info'}>{message.text}</Alert>
        </div>
      )}

      {risk.tripped && (
        <div className="mb-4">
          <Alert tone="error">
            <div className="mb-2">Предохранитель сработал: {risk.trip_reason}</div>
            <Button onClick={() => void api.resetBreaker().then(() => reload())}>
              Сбросить после устранения причины
            </Button>
          </Alert>
        </div>
      )}

      <div className="card mb-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-slate-200">
              Автоматическая торговля {data.auto_trade ? 'включена' : 'выключена'}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {data.paper_mode
                ? 'Недоступна в paper-режиме: сначала выключите симуляцию в Настройках.'
                : whitelistItems.length === 0
                  ? 'Недоступна: whitelist коллекций пуст — торговать нечем.'
                  : `Автомат будет торговать только этими коллекциями: ${whitelistItems.length} шт.`}
            </p>
          </div>
          <Button
            variant={data.auto_trade ? 'danger' : 'primary'}
            disabled={!canEnableAuto && !data.auto_trade}
            onClick={() => void patch({ auto_trade: !data.auto_trade })}
          >
            {data.auto_trade ? 'Выключить' : 'Включить'}
          </Button>
        </div>
      </div>

      <div className="card mb-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-slate-200">
              Быстрая петля {data.watch_enabled ? 'включена' : 'выключена'}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Плановый цикл идёт раз в 20 минут — недооценённый лот столько не живёт.
              Быстрая петля берёт ленту свежих лотов одним запросом раз в{' '}
              {data.watch_interval_sec.toFixed(0)} с и оценивает только новые.
              Покупает лишь при включённом автомате и под теми же лимитами.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              className="rounded-lg bg-ink-700 px-3 py-2 text-sm text-slate-200"
              value={data.watch_interval_sec}
              onChange={(event) =>
                void patch({ watch_interval_sec: Number(event.target.value) })
              }
            >
              {[5, 10, 15, 30, 60].map((seconds) => (
                <option key={seconds} value={seconds}>
                  раз в {seconds} с
                </option>
              ))}
            </select>
            <Button
              variant={data.watch_enabled ? 'danger' : 'primary'}
              onClick={() => void patch({ watch_enabled: !data.watch_enabled })}
            >
              {data.watch_enabled ? 'Выключить' : 'Включить'}
            </Button>
          </div>
        </div>
      </div>

      <div className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Потрачено за сутки"
          value={ton(risk.spent_today_ton)}
          hint={`лимит ${ton(risk.daily_budget_ton)}`}
          tone={risk.spent_today_ton >= risk.daily_budget_ton * 0.9 ? 'warn' : undefined}
        />
        <Stat
          label="Результат за сутки"
          value={ton(risk.realized_today_ton)}
          tone={risk.realized_today_ton < 0 ? 'loss' : 'profit'}
        />
        <Stat
          label="Открыто позиций"
          value={`${risk.open_positions} / ${risk.max_open_positions}`}
        />
        <Stat
          label="Ошибок подряд"
          value={risk.consecutive_errors}
          tone={risk.consecutive_errors > 0 ? 'warn' : undefined}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h2 className="mb-4 text-sm font-medium text-slate-300">Лимиты</h2>
          <div className="space-y-3">
            <NumberField
              label="Дневной бюджет, TON"
              value={limits.daily_budget_ton}
              onSave={(value) => void patch({ risk: { daily_budget_ton: value } })}
            />
            <NumberField
              label="Максимум на позицию, TON"
              value={limits.max_position_ton}
              onSave={(value) => void patch({ risk: { max_position_ton: value } })}
            />
            <NumberField
              label="Максимум открытых позиций"
              value={limits.max_open_positions}
              onSave={(value) => void patch({ risk: { max_open_positions: value } })}
            />
            <NumberField
              label="Позиций в одной коллекции"
              value={limits.max_positions_per_collection}
              onSave={(value) => void patch({ risk: { max_positions_per_collection: value } })}
            />
            <NumberField
              label="Стоп по убытку за сутки, TON"
              value={limits.circuit_breaker_daily_loss_ton}
              onSave={(value) =>
                void patch({ risk: { circuit_breaker_daily_loss_ton: value } })
              }
            />
            <NumberField
              label="Стоп после N ошибок подряд"
              value={limits.circuit_breaker_errors}
              onSave={(value) => void patch({ risk: { circuit_breaker_errors: value } })}
            />
          </div>
        </div>

        <div className="card">
          <h2 className="mb-2 text-sm font-medium text-slate-300">
            Whitelist коллекций
          </h2>
          <p className="mb-3 text-xs text-slate-500">
            По одной в строке. Пустой список означает запрет автоматической торговли,
            а не разрешение торговать всем подряд.
          </p>
          <textarea
            value={whitelist}
            onChange={(event) => setWhitelist(event.target.value)}
            rows={10}
            className="w-full rounded-lg border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-xs text-slate-200"
            placeholder="Plush Pepe&#10;Durov's Cap"
          />
          <div className="mt-3">
            <Button
              variant="primary"
              onClick={() =>
                void patch({ risk: { collection_whitelist: whitelistItems } })
              }
            >
              Сохранить список
            </Button>
          </div>
        </div>
      </div>

      <div className="card mt-4">
        <h2 className="mb-2 text-sm font-medium text-slate-300">Порог безубытка</h2>
        <p className="text-sm text-slate-400">
          При текущей комиссии сделка выходит в ноль только при наценке{' '}
          <span className="font-mono text-warn">{percent(data.breakeven_markup)}</span>. Всё,
          что ниже, — убыток независимо от того, как выглядит спред.
        </p>
      </div>
    </Page>
  )
}

function NumberField({
  label,
  value,
  onSave,
}: {
  label: string
  value: number
  onSave: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])

  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-slate-400">{label}</span>
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="w-24 rounded-md border border-ink-500 bg-ink-900 px-2 py-1 text-right font-mono text-sm text-slate-200"
        />
        <Button
          disabled={draft === String(value) || Number.isNaN(Number(draft))}
          onClick={() => onSave(Number(draft))}
        >
          OK
        </Button>
      </div>
    </div>
  )
}
