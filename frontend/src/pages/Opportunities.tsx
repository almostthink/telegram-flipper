import { Fragment, useState } from 'react'
import { api, hours, percent, ton, type Signal } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Bar, Button, Empty, Page, Table } from '../components/ui'

export default function Opportunities() {
  const [onlyPassed, setOnlyPassed] = useState(true)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const { data, error, reload } = useApi(() => api.signals(onlyPassed), {
    pollMs: 20000,
    deps: [onlyPassed],
  })

  const refresh = async () => {
    setBusy(true)
    try {
      await api.refreshSignals()
      await reload()
      setMessage(null)
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const buy = async (signal: Signal) => {
    setBusy(true)
    try {
      const result = await api.buy(signal.market, signal.listing_id)
      setMessage({ tone: 'info', text: result.detail })
      await reload()
    } catch (e) {
      setMessage({ tone: 'error', text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Page
      title="Сигналы"
      subtitle="ROI считается по консервативной цене выхода, а не по справедливой — иначе прогноз систематически завышен"
      actions={
        <>
          <Button onClick={() => setOnlyPassed(!onlyPassed)}>
            {onlyPassed ? 'Показать отклонённые' : 'Только прошедшие'}
          </Button>
          <Button onClick={() => void refresh()} disabled={busy} variant="primary">
            Пересчитать
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

      {data && Object.keys(data.rejections).length > 0 && (
        <div className="card mb-4">
          <h2 className="mb-3 text-sm font-medium text-slate-300">Что отсекает лоты</h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(data.rejections).map(([reason, count]) => (
              <span key={reason} className="badge bg-ink-600 text-slate-400">
                {reason}: {count}
              </span>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Пороги: ROI от {percent(data.min_roi)}, ликвидность от{' '}
            {data.min_liquidity.toFixed(2)}. Меняются в Настройках.
          </p>
        </div>
      )}

      {!data || data.signals.length === 0 ? (
        <Empty>
          {onlyPassed
            ? 'Ни один лот не прошёл фильтры. Это нормальное состояние большую часть времени — хорошие сделки редки.'
            : 'Сигналов нет. Запустите цикл на странице «Обзор».'}
        </Empty>
      ) : (
        <Table
          head={[
            'Коллекция',
            'Модель',
            'Аск',
            'Справедливо',
            'ROI',
            'Ликвидность',
            'Продажа',
            'Доверие',
            '',
          ]}
        >
          {data.signals.map((signal) => (
            <Fragment key={signal.id}>
              <tr
                className={`cursor-pointer hover:bg-ink-700/40 ${signal.passed ? '' : 'opacity-50'}`}
                onClick={() => setExpanded(expanded === signal.id ? null : signal.id)}
              >
                <td className="px-4 py-2.5">
                  <div className="text-slate-200">{signal.collection}</div>
                  <div className="text-xs text-slate-600">{signal.market}</div>
                </td>
                <td className="px-4 py-2.5 text-slate-400">{signal.model ?? '—'}</td>
                <td className="px-4 py-2.5 font-mono text-slate-200">{ton(signal.ask_ton)}</td>
                <td className="px-4 py-2.5 font-mono text-slate-400">
                  {ton(signal.fair_value_ton)}
                </td>
                <td
                  className={`px-4 py-2.5 font-mono ${signal.net_roi > 0 ? 'text-profit' : 'text-loss'}`}
                >
                  {percent(signal.net_roi)}
                </td>
                <td className="px-4 py-2.5">
                  <Bar value={signal.liquidity_score} />
                </td>
                <td className="px-4 py-2.5 font-mono text-slate-400">
                  {hours(signal.expected_tts_hours)}
                </td>
                <td className="px-4 py-2.5 font-mono text-slate-400">
                  {signal.confidence.toFixed(2)}
                </td>
                <td className="px-4 py-2.5 text-right">
                  {signal.passed ? (
                    <Button variant="primary" disabled={busy} onClick={() => void buy(signal)}>
                      Купить
                    </Button>
                  ) : (
                    <span className="text-xs text-slate-600">{signal.reject_reason}</span>
                  )}
                </td>
              </tr>
              {expanded === signal.id && (
                <tr className="bg-ink-900/60">
                  <td colSpan={9} className="px-4 py-3 text-xs leading-relaxed text-slate-400">
                    {signal.explanation}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </Table>
      )}
    </Page>
  )
}
