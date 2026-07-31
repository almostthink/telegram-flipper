import { useState } from 'react'
import { api, hours, signedTon, ton, type Position } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Empty, Page, Table } from '../components/ui'

export default function Inventory() {
  const [tab, setTab] = useState<'open' | 'closed'>('open')
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const { data, error, reload } = useApi(() => api.positions(tab), {
    pollMs: 15000,
    deps: [tab],
  })

  const act = async (fn: () => Promise<{ detail: string }>) => {
    try {
      const result = await fn()
      setMessage({ tone: 'info', text: result.detail })
      await reload()
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    }
  }

  return (
    <Page
      title="Инвентарь"
      subtitle="Купленные подарки, лестница переоценки и результат по каждой позиции"
      actions={
        <>
          <Button onClick={() => setTab('open')} variant={tab === 'open' ? 'primary' : 'default'}>
            Открытые
          </Button>
          <Button onClick={() => setTab('closed')} variant={tab === 'closed' ? 'primary' : 'default'}>
            Закрытые
          </Button>
        </>
      }
    >
      {message && (
        <div className="mb-4">
          <Alert tone={message.tone === 'error' ? 'error' : 'info'}>{message.text}</Alert>
        </div>
      )}
      {error && (
        <div className="mb-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}

      {!data || data.positions.length === 0 ? (
        <Empty>
          {tab === 'open' ? 'Открытых позиций нет' : 'Закрытых сделок пока нет'}
        </Empty>
      ) : tab === 'open' ? (
        <Table head={['Коллекция', 'Куплено', 'Аск', 'Бумажный P&L', 'В позиции', 'Переоценок', '']}>
          {data.positions.map((position) => (
            <OpenRow key={position.id} position={position} onAct={act} />
          ))}
        </Table>
      ) : (
        <Table head={['Коллекция', 'Куплено', 'Продано', 'Результат', 'Держали', 'Прогноз был']}>
          {data.positions.map((position) => (
            <tr key={position.id}>
              <td className="px-4 py-2.5">
                <div className="text-neutral-200">{position.collection}</div>
                <div className="text-xs text-neutral-600">{position.model ?? '—'}</div>
              </td>
              <td className="px-4 py-2.5 font-mono text-neutral-400">
                {ton(position.buy_price_ton)}
              </td>
              <td className="px-4 py-2.5 font-mono text-neutral-400">
                {ton(position.sell_price_ton)}
              </td>
              <td
                className={`px-4 py-2.5 font-mono ${
                  (position.net_pnl_ton ?? 0) > 0 ? 'text-profit' : 'text-loss'
                }`}
              >
                {signedTon(position.net_pnl_ton, 3)}
              </td>
              <td className="px-4 py-2.5 font-mono text-neutral-500">
                {hours(heldHours(position))}
              </td>
              <td className="px-4 py-2.5 font-mono text-neutral-600">
                {hours(position.expected_tts_hours)}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Page>
  )
}

function OpenRow({
  position,
  onAct,
}: {
  position: Position
  onAct: (fn: () => Promise<{ detail: string }>) => Promise<void>
}) {
  const [price, setPrice] = useState(
    (position.ask_price_ton ?? position.buy_price_ton * 1.18).toFixed(2),
  )
  const held = heldHours(position)
  const overdue =
    position.expected_tts_hours != null && held > position.expected_tts_hours * 1.5

  return (
    <tr className={overdue ? 'bg-warn/5' : undefined}>
      <td className="px-4 py-2.5">
        <div className="text-neutral-200">
          {position.collection}
          {position.frozen && (
            <span className="badge ml-2 bg-ink-500 text-neutral-300">заморожена</span>
          )}
        </div>
        <div className="text-xs text-neutral-600">
          {position.frozen ? 'ждёт переноса вручную' : (position.model ?? '—')}
        </div>
      </td>
      <td className="px-4 py-2.5 font-mono text-neutral-400">{ton(position.buy_price_ton)}</td>
      <td className="px-4 py-2.5 font-mono text-neutral-200">{ton(position.ask_price_ton)}</td>
      <td
        className={`px-4 py-2.5 font-mono ${
          (position.unrealized_ton ?? 0) > 0 ? 'text-profit' : 'text-loss'
        }`}
      >
        {signedTon(position.unrealized_ton, 3)}
      </td>
      <td className="px-4 py-2.5 font-mono text-neutral-500">
        {hours(held)}
        {overdue && <span className="ml-1 text-warn">!</span>}
      </td>
      <td className="px-4 py-2.5 font-mono text-neutral-500">{position.reprice_count}</td>
      <td className="px-4 py-2.5">
        <div className="flex items-center justify-end gap-2">
          <input
            value={price}
            onChange={(event) => setPrice(event.target.value)}
            className="w-20 rounded-md border border-ink-500 bg-ink-900 px-2 py-1 text-right font-mono text-xs text-neutral-200"
          />
          {/* Заморозка: подарок остаётся за нами, движок его не трогает.
              Нужна ровно для переноса на другую площадку руками. */}
          <Button
            title={
              position.frozen
                ? 'Вернуть позицию движку — продаётся там, где куплена'
                : 'Движок не будет её выставлять и переоценивать, лот снимется с продажи'
            }
            onClick={() => void onAct(() => api.freeze(position.id, !position.frozen))}
          >
            {position.frozen ? 'Разморозить' : 'Заморозить'}
          </Button>
          <Button
            disabled={position.frozen}
            onClick={() => void onAct(() => api.relist(position.id, Number(price)))}
          >
            {position.status === 'open' ? 'Выставить' : 'Цена'}
          </Button>
          <Button
            variant="danger"
            onClick={() => void onAct(() => api.sell(position.id, Number(price)))}
          >
            Закрыть
          </Button>
        </div>
      </td>
    </tr>
  )
}

function heldHours(position: Position): number {
  const start = new Date(position.bought_at).getTime()
  const end = position.sold_at ? new Date(position.sold_at).getTime() : Date.now()
  return (end - start) / 3600000
}
