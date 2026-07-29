import { useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, hours, percent, ton } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Bar, Empty, Page, Stat, Table } from '../components/ui'

const KIND_LABELS: Record<string, string> = {
  model: 'Модель',
  backdrop: 'Фон',
  symbol: 'Символ',
}

export default function Market() {
  const [selected, setSelected] = useState<string | null>(null)
  const list = useApi(() => api.collections(), { pollMs: 60000 })
  const detail = useApi(
    () => (selected ? api.collection(selected) : Promise.resolve(null)),
    { deps: [selected] },
  )

  return (
    <Page
      title="Рынок"
      subtitle="Разбор коллекции по моделям, фонам и символам с оценкой ликвидности"
    >
      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <div className="card max-h-[calc(100vh-12rem)] overflow-y-auto p-2">
          {!list.data || list.data.collections.length === 0 ? (
            <div className="p-4 text-sm text-slate-500">
              Коллекций пока нет. Запустите цикл на «Обзоре».
            </div>
          ) : (
            list.data.collections.map((row) => (
              <button
                key={row.collection}
                onClick={() => setSelected(row.collection)}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                  selected === row.collection
                    ? 'bg-accent/15 text-accent'
                    : 'text-slate-400 hover:bg-ink-700'
                }`}
              >
                <span className="truncate">{row.collection}</span>
                <span className="ml-2 shrink-0 font-mono text-xs text-slate-500">
                  {row.floor_ton.toFixed(1)}
                </span>
              </button>
            ))
          )}
        </div>

        <div>
          {!selected ? (
            <Empty>Выберите коллекцию слева</Empty>
          ) : detail.error ? (
            <Alert tone="error">{detail.error}</Alert>
          ) : !detail.data ? (
            <Empty>Загрузка…</Empty>
          ) : (
            <CollectionView data={detail.data} />
          )}
        </div>
      </div>
    </Page>
  )
}

function CollectionView({ data }: { data: NonNullable<Awaited<ReturnType<typeof api.collection>>> }) {
  const liq = data.liquidity

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Флор" value={ton(data.floor_ton)} />
        <Stat
          label="Ликвидность"
          value={liq.measured ? liq.score.toFixed(2) : '—'}
          tone={
            !liq.measured
              ? 'warn'
              : liq.score >= 0.5
                ? 'profit'
                : liq.score >= 0.35
                  ? 'warn'
                  : 'loss'
          }
          hint={
            liq.measured ? `продаж за 7д: ${liq.sales_7d}` : 'нет истории сделок'
          }
        />
        <Stat
          label="Ожидаемая продажа"
          value={hours(liq.expected_tts_hours)}
          hint={liq.tts_median_hours ? 'по факту замеров' : 'оценка по скорости'}
        />
        <Stat
          label="Тренд флора 24ч"
          value={liq.floor_trend_24h == null ? '—' : percent(liq.floor_trend_24h)}
          tone={
            liq.floor_trend_24h == null
              ? undefined
              : liq.floor_trend_24h < -0.1
                ? 'loss'
                : liq.floor_trend_24h > 0
                  ? 'profit'
                  : undefined
          }
          hint={`${liq.depth_10pct} лотов у флора`}
        />
      </div>

      {!liq.measured && (
        <Alert tone="warn">
          По коллекции не записано ни одной сделки, поэтому балл ликвидности
          ничего не измеряет: он означает «неизвестно», а не «продаётся плохо».
          Скорость продаж и время до продажи дают 65% балла и сейчас недоступны.
          Нужен рабочий адрес ленты сделок — импортируйте HAR с открытым
          разделом истории.
        </Alert>
      )}

      <div className="card">
        <h2 className="mb-3 text-sm font-medium text-slate-300">Состав балла ликвидности</h2>
        <div className="grid gap-3 sm:grid-cols-4">
          {Object.entries(liq.parts).map(([name, value]) => (
            <div key={name}>
              <div className="mb-1 text-xs text-slate-500">
                {PART_LABELS[name] ?? name}
                {liq.missing.includes(name) && (
                  <span className="ml-1 text-slate-600">— нет источника</span>
                )}
              </div>
              {liq.missing.includes(name) ? (
                <div className="text-xs text-slate-600">
                  вес перераспределён на остальные
                </div>
              ) : (
                <Bar value={value} />
              )}
            </div>
          ))}
        </div>
      </div>

      {data.floor_history.length > 1 && (
        <div className="card">
          <h2 className="mb-3 text-sm font-medium text-slate-300">Флор за 14 дней</h2>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.floor_history}>
                <CartesianGrid stroke="#1e2534" strokeDasharray="3 3" />
                <XAxis dataKey="t" tick={{ fill: '#64748b', fontSize: 11 }} minTickGap={40} />
                <YAxis tick={{ fill: '#64748b', fontSize: 11 }} width={48} />
                <Tooltip
                  contentStyle={{
                    background: '#11151f',
                    border: '1px solid #2a3244',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelStyle={{ color: '#94a3b8' }}
                />
                <Line
                  type="monotone"
                  dataKey="floor_ton"
                  stroke="#4c8dff"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {data.attributes.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-slate-300">
            Атрибуты и их премия к флору
          </h2>
          <Table head={['Тип', 'Значение', 'Флор', 'Редкость', 'Множитель']}>
            {data.attributes.slice(0, 30).map((attr) => (
              <tr key={`${attr.kind}-${attr.name}`}>
                <td className="px-4 py-2 text-slate-500">{KIND_LABELS[attr.kind] ?? attr.kind}</td>
                <td className="px-4 py-2 text-slate-200">{attr.name}</td>
                <td className="px-4 py-2 font-mono text-slate-400">{ton(attr.floor_ton)}</td>
                <td className="px-4 py-2 font-mono text-slate-500">
                  {/* Площадки отдают промилле, но знак ‰ в мелком кегле
                      неотличим от %, поэтому показываем проценты. */}
                  {attr.rarity_permille == null
                    ? '—'
                    : `${(attr.rarity_permille / 10).toFixed(1)}%`}
                </td>
                <td className="px-4 py-2 font-mono text-accent">
                  {attr.multiplier == null ? '—' : `×${attr.multiplier}`}
                </td>
              </tr>
            ))}
          </Table>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <h2 className="mb-2 text-sm font-medium text-slate-300">Книга заявок</h2>
          <Table head={['Цена', 'Модель', 'Площадка']}>
            {data.book.slice(0, 20).map((item) => (
              <tr key={`${item.market}-${item.listing_id}`}>
                <td className="px-4 py-2 font-mono text-slate-200">{ton(item.price_ton)}</td>
                <td className="px-4 py-2 text-slate-400">{item.model ?? '—'}</td>
                <td className="px-4 py-2 text-xs text-slate-600">{item.market}</td>
              </tr>
            ))}
          </Table>
        </div>

        <div>
          <h2 className="mb-2 text-sm font-medium text-slate-300">Последние сделки</h2>
          {data.recent_sales.length === 0 ? (
            <Empty>История сделок ещё не собрана</Empty>
          ) : (
            <Table head={['Цена', 'Модель', 'Продалось за']}>
              {data.recent_sales.slice(0, 20).map((sale, index) => (
                <tr key={`${sale.sold_at}-${index}`}>
                  <td className="px-4 py-2 font-mono text-slate-200">{ton(sale.price_ton)}</td>
                  <td className="px-4 py-2 text-slate-400">{sale.model ?? '—'}</td>
                  <td className="px-4 py-2 font-mono text-slate-500">{hours(sale.tts_hours)}</td>
                </tr>
              ))}
            </Table>
          )}
        </div>
      </div>
    </div>
  )
}

const PART_LABELS: Record<string, string> = {
  velocity: 'Скорость продаж',
  tts: 'Время до продажи',
  bid_support: 'Поддержка офферами',
  depth: 'Разреженность книги',
}
