import { api, percent, ton } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Empty, Page, Stat } from '../components/ui'
import { useState } from 'react'

/**
 * Ордер-движок: заявки на покупку ниже флора.
 *
 * Способ набора позиции, обратный «Сигналам». Там мы ждём чужой ошибки —
 * кто-то выставил дешевле рынка. Здесь ошибки никто не делает: мы сами
 * встаём в очередь покупателей и ждём продавца, готового отдать со
 * скидкой. Исполненная заявка даёт подарок в инвентарь.
 */
export default function Orders() {
  const engine = useApi(() => api.engine(), { pollMs: 10000 })
  const config = useApi(() => api.config())
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const patch = async (body: Record<string, unknown>) => {
    try {
      await api.updateConfig(body)
      await Promise.all([engine.reload(), config.reload()])
      setMessage({ tone: 'info', text: 'Сохранено' })
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    }
  }

  const runOnce = async () => {
    setBusy(true)
    try {
      const report = await api.runOrders()
      await engine.reload()
      setMessage({
        tone: 'info',
        text: `Рассмотрено ${report.considered}, поставлено ${report.placed}, снято ${report.cancelled}`,
      })
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  if (!engine.data || !config.data) {
    return <Page title="Ордер-движок"><Empty>Загрузка…</Empty></Page>
  }

  const cfg = config.data.orders
  const last = engine.data.last_orders
  const enabled = engine.data.orders_enabled

  return (
    <Page
      title="Ордер-движок"
      subtitle="Заявки на покупку ниже флора с перебиванием конкурентов"
      actions={
        <Button onClick={() => void runOnce()} disabled={busy}>
          {busy ? 'Идёт пересмотр…' : 'Пересмотреть сейчас'}
        </Button>
      }
    >
      {message && (
        <div className="mb-4">
          <Alert tone={message.tone === 'error' ? 'error' : 'info'}>{message.text}</Alert>
        </div>
      )}

      <div className="card mb-4">
        <div className="flex items-start justify-between gap-6">
          <div>
            <div className="text-sm font-medium text-neutral-200">
              Движок {enabled ? 'включён' : 'выключен'}
            </div>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-neutral-500">
              Бот ставит заявки на коллекции и держит их в пределах спреда от флора,
              перебивая конкурентов на один шаг. Заявка исполняется — подарок попадает
              в инвентарь и уходит в обычный цикл перепродажи.
              <br />
              Прибыль здесь берётся из разницы «купил ниже флора — продал у флора»,
              поэтому спред меньше комиссии площадки бессмыслен: движок такие заявки
              не ставит вовсе и говорит об этом в отчёте.
            </p>
          </div>
          <Button
            variant={enabled ? 'danger' : 'primary'}
            onClick={() => void patch({ orders: { enabled: !enabled } })}
          >
            {enabled ? 'Выключить' : 'Включить'}
          </Button>
        </div>
      </div>

      <div className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Рассмотрено коллекций" value={last.considered} />
        <Stat
          label="Заявок поставлено"
          value={last.placed}
          hint={config.data.paper_mode ? 'в PAPER только считается' : 'за последний проход'}
        />
        <Stat label="Заявок снято" value={last.cancelled} hint="вышли за спред" />
        <Stat
          label="Симулировано"
          value={last.simulated}
          hint="PAPER: денег не двигаем"
        />
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h2 className="mb-4 text-sm font-medium text-neutral-300">Стратегия ставок</h2>
          <div className="space-y-3">
            <Field
              label="Целевой спред"
              hint={`сейчас ${percent(cfg.target_spread)} от флора`}
              value={cfg.target_spread}
              onSave={(value) => void patch({ orders: { target_spread: value } })}
            />
            <Field
              label="Мин. флор, TON"
              value={cfg.min_floor_ton}
              onSave={(value) => void patch({ orders: { min_floor_ton: value } })}
            />
            <Field
              label="Макс. флор, TON"
              value={cfg.max_floor_ton}
              onSave={(value) => void patch({ orders: { max_floor_ton: value } })}
            />
            <Field
              label="Количество в заявке"
              value={cfg.amount}
              onSave={(value) => void patch({ orders: { amount: value } })}
            />
            <Field
              label="Максимум заявок"
              hint="прямое ограничение занятого капитала"
              value={cfg.max_orders}
              onSave={(value) => void patch({ orders: { max_orders: value } })}
            />
            <Field
              label="Шаг перебивания, TON"
              value={cfg.tick_ton}
              onSave={(value) => void patch({ orders: { tick_ton: value } })}
            />
            <Field
              label="Период пересмотра, с"
              value={cfg.interval_sec}
              onSave={(value) => void patch({ orders: { interval_sec: value } })}
            />
          </div>
        </div>

        <div className="card">
          <h2 className="mb-3 text-sm font-medium text-neutral-300">Что делал движок</h2>
          {last.log.length === 0 ? (
            <div className="text-xs text-neutral-500">
              Пока ничего. Движок ставит заявки только там, где флор попадает в
              заданные границы, а спред покрывает комиссию.
            </div>
          ) : (
            <div className="max-h-80 space-y-1 overflow-y-auto font-mono text-xs text-neutral-400">
              {last.log.map((line, index) => (
                <div key={`${index}-${line}`}>{line}</div>
              ))}
            </div>
          )}

          {Object.keys(last.skipped).length > 0 && (
            <div className="mt-4 border-t border-ink-600 pt-3">
              <div className="mb-2 text-xs text-neutral-500">Пропущено</div>
              <div className="max-h-40 space-y-1 overflow-y-auto text-xs text-neutral-600">
                {Object.entries(last.skipped).map(([name, reason]) => (
                  <div key={name}>
                    <span className="text-neutral-400">{name}</span> — {reason}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {last.errors.length > 0 && (
        <Alert tone="error">
          <div className="space-y-1">
            {last.errors.map((text) => (
              <div key={text}>{text}</div>
            ))}
          </div>
        </Alert>
      )}

      <div className="mt-4 text-xs text-neutral-600">
        Доступный баланс площадки:{' '}
        {engine.data.balance_ton == null ? '—' : ton(engine.data.balance_ton)}
      </div>
    </Page>
  )
}

function Field({
  label,
  hint,
  value,
  onSave,
}: {
  label: string
  hint?: string
  value: number
  onSave: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-sm text-neutral-400">{label}</span>
        {hint && <span className="text-xs text-neutral-600">{hint}</span>}
      </div>
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="w-full rounded border border-ink-500 bg-ink-900 px-3 py-1.5 font-mono text-sm text-neutral-200"
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
