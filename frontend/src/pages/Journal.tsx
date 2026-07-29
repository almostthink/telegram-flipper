import { useState } from 'react'
import {
  Bar as RBar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, hours, percent, signedTon, ton } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Empty, Page, Stat, Table } from '../components/ui'

export default function Journal() {
  const stats = useApi(() => api.stats(30), { pollMs: 30000 })
  const log = useApi(() => api.journal(), { pollMs: 30000 })
  const [backtest, setBacktest] = useState<Awaited<ReturnType<typeof api.backtest>> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runBacktest = async () => {
    setBusy(true)
    setError(null)
    try {
      setBacktest(await api.backtest(30))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const data = stats.data

  return (
    <Page
      title="Журнал"
      subtitle="Своя статистика вместо чужих цифр: распределение по дням, а не только итог"
      actions={
        <Button variant="primary" disabled={busy} onClick={() => void runBacktest()}>
          {busy ? 'Считаю…' : 'Прогнать бэктест'}
        </Button>
      }
    >
      {error && (
        <div className="mb-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}

      {!data ? (
        <Empty>Загрузка…</Empty>
      ) : data.closed_trades === 0 ? (
        <Empty>
          Закрытых сделок пока нет. Статистика появится после первых продаж — до этого
          момента любые оценки доходности остаются предположениями.
        </Empty>
      ) : (
        <>
          <div className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat
              label="Реализовано"
              value={signedTon(data.realized_pnl_ton)}
              tone={data.realized_pnl_ton > 0 ? 'profit' : 'loss'}
              hint={`${data.closed_trades} сделок`}
            />
            <Stat
              label="Win rate"
              value={percent(data.win_rate)}
              hint={`средний плюс ${ton(data.avg_win_ton)}, минус ${ton(data.avg_loss_ton)}`}
            />
            <Stat
              label="Ожидание на сделку"
              value={signedTon(data.expectancy_ton, 3)}
              tone={data.expectancy_ton > 0 ? 'profit' : 'loss'}
              hint="это и есть настоящий показатель"
            />
            <Stat
              label="Макс. просадка"
              value={ton(data.max_drawdown_ton)}
              tone="warn"
              hint={`лучший день ${signedTon(data.best_day_ton)}`}
            />
          </div>

          <div className="mb-4 grid gap-4 lg:grid-cols-3">
            <div className="card lg:col-span-2">
              <h2 className="mb-3 text-sm font-medium text-slate-300">
                Результат по дням — прибыльных {data.profitable_days}, убыточных{' '}
                {data.losing_days}
              </h2>
              <div className="h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.daily}>
                    <CartesianGrid stroke="#1e2534" strokeDasharray="3 3" />
                    <XAxis dataKey="day" tick={{ fill: '#64748b', fontSize: 11 }} minTickGap={30} />
                    <YAxis tick={{ fill: '#64748b', fontSize: 11 }} width={44} />
                    <Tooltip
                      contentStyle={{
                        background: '#11151f',
                        border: '1px solid #2a3244',
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                      labelStyle={{ color: '#94a3b8' }}
                    />
                    <RBar dataKey="realized_ton">
                      {data.daily.map((day) => (
                        <Cell
                          key={day.day}
                          fill={day.realized_ton >= 0 ? '#3ecf8e' : '#ff5c5c'}
                        />
                      ))}
                    </RBar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                Один удачный день ничего не доказывает — смотрите на форму распределения
                и на медиану, а не на максимум.
              </p>
            </div>

            <div className="card">
              <h2 className="mb-3 text-sm font-medium text-slate-300">Качество модели</h2>
              <dl className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <dt className="text-slate-500">Медианный холд</dt>
                  <dd className="font-mono text-slate-200">{hours(data.median_hold_hours)}</dd>
                </div>
                <div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Смещение прогноза TTS</dt>
                    <dd
                      className={`font-mono ${
                        (data.tts_bias ?? 1) > 1.3 ? 'text-loss' : 'text-slate-200'
                      }`}
                    >
                      {data.tts_bias == null ? '—' : `×${data.tts_bias}`}
                    </dd>
                  </div>
                  <p className="mt-1 text-xs text-slate-600">
                    Больше единицы — модель обещает продажу быстрее, чем выходит.
                  </p>
                </div>
                <div className="flex justify-between border-t border-ink-600 pt-3">
                  <dt className="text-slate-500">Бумажный P&L</dt>
                  <dd className="font-mono text-warn">{signedTon(data.unrealized_pnl_ton)}</dd>
                </div>
                <p className="text-xs text-slate-600">
                  Считается отдельно от реализованного и прибылью не является.
                </p>
              </dl>
            </div>
          </div>
        </>
      )}

      {backtest && (
        <div className="card mb-4">
          <h2 className="mb-3 text-sm font-medium text-slate-300">
            Бэктест за {backtest.days} дней
          </h2>
          {backtest.trades === 0 ? (
            <Alert tone="warn">{backtest.note}</Alert>
          ) : (
            <>
              <div className="mb-3 grid grid-cols-2 gap-4 lg:grid-cols-5">
                <Stat label="Сделок" value={backtest.trades} />
                <Stat
                  label="Итог"
                  value={signedTon(backtest.net_pnl_ton)}
                  tone={backtest.net_pnl_ton > 0 ? 'profit' : 'loss'}
                />
                <Stat label="На вложенное" value={percent(backtest.return_on_invested)} />
                <Stat label="Win rate" value={percent(backtest.win_rate)} />
                <Stat
                  label="Зависло"
                  value={percent(backtest.stuck_share)}
                  tone={backtest.stuck_share > 0.2 ? 'warn' : undefined}
                />
              </div>
              <Alert tone="warn">{backtest.note}</Alert>
            </>
          )}
        </div>
      )}

      <h2 className="mb-2 text-sm font-medium text-slate-300">Лента операций</h2>
      {!log.data || log.data.entries.length === 0 ? (
        <Empty>Операций ещё не было</Empty>
      ) : (
        <Table head={['Время', 'Действие', 'Цена', 'Детали']}>
          {log.data.entries.slice(0, 100).map((entry) => (
            <tr key={entry.id} className={entry.ok ? undefined : 'bg-loss/5'}>
              <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-slate-500">
                {new Date(entry.at).toLocaleString('ru-RU')}
              </td>
              <td className="px-4 py-2">
                <span className="badge bg-ink-600 text-slate-300">{entry.action}</span>
              </td>
              <td className="px-4 py-2 font-mono text-slate-400">{ton(entry.price_ton)}</td>
              <td className="px-4 py-2 text-xs text-slate-500">{entry.detail}</td>
            </tr>
          ))}
        </Table>
      )}
    </Page>
  )
}
