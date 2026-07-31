import { useRef, useState } from 'react'
import { api, type AuthStatus } from '../lib/api'
import { useApi } from '../lib/useApi'
import { Alert, Button, Page } from '../components/ui'

interface MarketRow {
  key: string
  label: string
  tradable: boolean
}

/** Список площадок берём у бэкенда, а не пишем руками.
 *
 * Рукописный список уже разошёлся с действительностью: Tonnel работал,
 * а в настройках его не было — ни токен вставить, ни выключить.
 */
function marketRows(auth: AuthStatus | null, configKeys: string[]): MarketRow[] {
  const known = Object.entries(auth?.markets ?? {})
  if (known.length > 0) {
    return known.map(([key, state]) => ({
      key,
      label: state.label,
      tradable: state.tradable,
    }))
  }
  // Ответ /auth ещё не пришёл — показываем хотя бы то, что есть в конфиге.
  return configKeys.map((key) => ({ key, label: key, tradable: false }))
}

export default function Settings() {
  const auth = useApi(() => api.auth())
  const config = useApi(() => api.config())
  const markets = marketRows(auth.data, Object.keys(config.data?.marketplaces ?? {}))
  const [message, setMessage] = useState<{ tone: 'info' | 'error' | 'warn'; text: string } | null>(
    null,
  )

  const report = (tone: 'info' | 'error' | 'warn', text: string) => setMessage({ tone, text })

  const patch = async (body: Record<string, unknown>) => {
    try {
      await api.updateConfig(body)
      await config.reload()
      report('info', 'Сохранено')
    } catch (e) {
      report('error', e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Page title="Настройки" subtitle="Доступы, комиссии и пороги отбора">
      {message && (
        <div className="mb-4">
          <Alert tone={message.tone === 'warn' ? 'warn' : message.tone}>{message.text}</Alert>
        </div>
      )}

      {auth.data && !auth.data.vault_secure && (
        <div className="mb-4">
          <Alert tone="warn">
            Системное хранилище секретов недоступно ({auth.data.vault_backend}). Токены
            сохраняются в файл с правами 0600 в папке данных — это менее защищено, чем
            Windows DPAPI.
          </Alert>
        </div>
      )}

      <div className="mb-4 card">
        <h2 className="mb-1 text-sm font-medium text-neutral-300">Режим работы</h2>
        <p className="mb-4 text-xs text-neutral-500">
          Paper-режим симулирует сделки на реальных рыночных данных. Выключайте его
          только после того, как бэктест и журнал покажут осмысленный результат.
        </p>
        {config.data && (
          <div className="flex items-center justify-between rounded-lg bg-ink-700 px-4 py-3">
            <div>
              <div className="text-sm text-neutral-200">
                {config.data.paper_mode ? 'PAPER — симуляция' : 'LIVE — реальные деньги'}
              </div>
              <div className="text-xs text-neutral-500">
                {config.data.paper_mode
                  ? 'Деньги не тратятся'
                  : 'Сделки исполняются на вашем балансе'}
              </div>
            </div>
            <Button
              variant={config.data.paper_mode ? 'danger' : 'primary'}
              onClick={() => void patch({ paper_mode: !config.data?.paper_mode })}
            >
              {config.data.paper_mode ? 'Перейти в LIVE' : 'Вернуть PAPER'}
            </Button>
          </div>
        )}
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <TokensCard auth={auth} markets={markets} onReport={report} />
        <UserbotCard auth={auth} onReport={report} />
      </div>

      {config.data && (
        <div className="mb-4">
          <NotifyCard config={config.data} onPatch={patch} onReport={report} />
        </div>
      )}

      <HarCard markets={markets} onReport={report} onDone={() => void auth.reload()} />

      {config.data && (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <div className="card">
            <h2 className="mb-1 text-sm font-medium text-neutral-300">Пороги отбора</h2>
            <p className="mb-4 text-xs text-neutral-500">
              Лот должен пройти все пороги одновременно.
            </p>
            <div className="space-y-3">
              <Field
                label="Минимальный чистый ROI"
                value={config.data.analytics.min_roi}
                onSave={(value) => void patch({ analytics: { min_roi: value } })}
              />
              <Field
                label="Минимальная ликвидность (0..1)"
                value={config.data.analytics.min_liquidity_score}
                onSave={(value) => void patch({ analytics: { min_liquidity_score: value } })}
              />
              <Field
                label="Максимальное время до продажи, ч"
                value={config.data.analytics.max_tts_hours}
                onSave={(value) => void patch({ analytics: { max_tts_hours: value } })}
              />
              <Field
                label="Максимальное падение флора за 24ч"
                value={config.data.analytics.max_floor_drop_24h}
                onSave={(value) => void patch({ analytics: { max_floor_drop_24h: value } })}
              />
              <Field
                label="Газ на транзакцию, TON"
                value={config.data.analytics.gas_ton}
                onSave={(value) => void patch({ analytics: { gas_ton: value } })}
              />
            </div>
          </div>

          <div className="card">
            <h2 className="mb-1 text-sm font-medium text-neutral-300">Стратегия продажи</h2>
            <p className="mb-4 text-xs text-neutral-500">
              Выставляем с наценкой, затем снижаем цену шагами до порога безубытка.
            </p>
            <div className="space-y-3">
              <Field
                label="Стартовая наценка"
                value={config.data.sell.initial_markup}
                onSave={(value) => void patch({ sell: { initial_markup: value } })}
              />
              <Field
                label="Интервал снижения, ч"
                value={config.data.sell.reprice_interval_hours}
                onSave={(value) => void patch({ sell: { reprice_interval_hours: value } })}
              />
              <Field
                label="Шаг снижения"
                value={config.data.sell.reprice_step}
                onSave={(value) => void patch({ sell: { reprice_step: value } })}
              />
              <Field
                label="Максимальный холд, ч"
                value={config.data.sell.max_hold_hours}
                onSave={(value) => void patch({ sell: { max_hold_hours: value } })}
              />
            </div>
          </div>

          <div className="card lg:col-span-2">
            <h2 className="mb-1 text-sm font-medium text-neutral-300">
              Коллекционная ценность
            </h2>
            <p className="mb-4 text-xs leading-relaxed text-neutral-500">
              Рынок платит надбавку за порядковый номер, за отдельные фоны и
              за монохром — независимо от редкости модели. Подарок #1 стоит
              кратно дороже #40597 при одинаковых атрибутах. Эти настройки
              влияют на оценку, пока нет истории сделок; как только она
              накопится, регрессия оценит те же факторы по фактическим ценам
              сама.
              <br />
              Монохром — когда цвет модели совпадает с цветом фона. Цвет фона
              площадка отдаёт числом, а цвет модели приходится доставать из её
              анимации: он разбирается по нескольку моделей за проход и потом
              хранится навсегда. Пока цвет модели не добыт, монохром у её лотов
              не определяется — но и оценку не занижает.
              <br />
              <span className="text-warn">
                Учтите: коллекционные лоты дороже, но продаются дольше —
                круг покупателей у них уже.
              </span>
            </p>

            <div className="grid gap-3 lg:grid-cols-2">
              <div className="space-y-3">
                <Field
                  label="Надбавка за номер #1 (доля)"
                  value={config.data.collectible.number_bonus}
                  onSave={(value) =>
                    void patch({ collectible: { number_bonus: value } })
                  }
                />
                <Field
                  label="Надбавка за ценный фон (доля)"
                  value={config.data.collectible.backdrop_bonus}
                  onSave={(value) =>
                    void patch({ collectible: { backdrop_bonus: value } })
                  }
                />
                <Field
                  label="Порог «заметного» номера (0..1)"
                  value={config.data.collectible.min_number_score}
                  onSave={(value) =>
                    void patch({ collectible: { min_number_score: value } })
                  }
                />
                <Field
                  label="Надбавка за монохром (доля)"
                  value={config.data.collectible.monochrome_bonus}
                  onSave={(value) =>
                    void patch({ collectible: { monochrome_bonus: value } })
                  }
                />
                <Field
                  label="Порог совпадения цветов (0..1)"
                  value={config.data.collectible.min_monochrome_score}
                  onSave={(value) =>
                    void patch({ collectible: { min_monochrome_score: value } })
                  }
                />
                <div className="flex items-center justify-between gap-3 pt-1">
                  <span className="text-sm text-neutral-400">
                    Покупать только коллекционные
                  </span>
                  <Button
                    variant={
                      config.data.collectible.require_collectible ? 'primary' : 'default'
                    }
                    onClick={() =>
                      void patch({
                        collectible: {
                          require_collectible:
                            !config.data?.collectible.require_collectible,
                        },
                      })
                    }
                  >
                    {config.data.collectible.require_collectible ? 'включено' : 'выключено'}
                  </Button>
                </div>
              </div>

              <BackdropList
                value={config.data.collectible.preferred_backdrops}
                onSave={(list) =>
                  void patch({ collectible: { preferred_backdrops: list } })
                }
              />
            </div>
          </div>

          <div className="card lg:col-span-2">
            <h2 className="mb-1 text-sm font-medium text-neutral-300">Комиссии площадок</h2>
            <p className="mb-4 text-xs leading-relaxed text-neutral-500">
              Указаны долей: 0.05 = 5%. Комиссия MRKT подтверждена записью
              трафика — 2%, и берётся она с покупателя сверх цены продавца.
              <br />
              Торгуем на MRKT и Tonnel; аукцион есть только на Tonnel. У Tonnel
              торговля пока выключена — тела торговых запросов не подтверждены
              записью трафика, а покупать по угаданному пути нельзя. Чтение при
              этом работает: флоры, лоты и сделки собираются.
              <br />
              GetGems выключен: у него GraphQL с persisted queries, хеш запроса
              меняется с каждым обновлением их фронтенда.
            </p>

            <div className="mb-4 space-y-2">
              {markets.map((market) => {
                const cfg = config.data!.marketplaces[market.key]
                if (!cfg) return null
                return (
                  <div
                    key={market.key}
                    className="flex items-center justify-between rounded-lg bg-ink-700 px-4 py-2.5"
                  >
                    <div className="text-sm text-neutral-300">
                      {market.label}
                      <span className="ml-2 text-xs text-neutral-500">
                        {cfg.trade_enabled
                          ? 'торговля'
                          : cfg.enabled
                            ? 'цены для сверки'
                            : 'выключена'}
                      </span>
                    </div>
                    <Button
                      variant={cfg.enabled ? 'default' : 'primary'}
                      onClick={() =>
                        void patch({
                          marketplaces: {
                            ...config.data!.marketplaces,
                            [market.key]: { ...cfg, enabled: !cfg.enabled },
                          },
                        })
                      }
                    >
                      {cfg.enabled ? 'Выключить' : 'Включить'}
                    </Button>
                  </div>
                )
              })}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {markets.map((market) => (
                <Field
                  key={market.key}
                  label={`${market.label} — комиссия продавца`}
                  value={config.data!.marketplaces[market.key]?.fee_sell ?? 0.05}
                  onSave={(value) =>
                    void patch({
                      marketplaces: {
                        ...config.data!.marketplaces,
                        [market.key]: {
                          ...config.data!.marketplaces[market.key],
                          fee_sell: value,
                        },
                      },
                    })
                  }
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </Page>
  )
}

function TokensCard({
  auth,
  markets,
  onReport,
}: {
  auth: ReturnType<typeof useApi<Awaited<ReturnType<typeof api.auth>>>>
  markets: MarketRow[]
  onReport: (tone: 'info' | 'error', text: string) => void
}) {
  const [market, setMarket] = useState('mrkt')
  const [token, setToken] = useState('')

  const save = async () => {
    try {
      const result = await api.setToken(market, token)
      onReport('info', result.detail)
      setToken('')
      await auth.reload()
    } catch (e) {
      onReport('error', e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card">
      <h2 className="mb-1 text-sm font-medium text-neutral-300">Токены площадок</h2>
      <p className="mb-4 text-xs leading-relaxed text-neutral-500">
        DevTools → Network → любой запрос к площадке → заголовок Authorization.
        Вставляйте значение <span className="text-neutral-300">как есть</span>:
        схемы у площадок разные и приложение ничего не дописывает.
      </p>

      <ul className="mb-4 space-y-1 text-xs leading-relaxed text-neutral-500">
        <li>
          <span className="text-neutral-300">MRKT</span> — собственный токен без
          префикса, UUID из 36 символов. Площадка выдаёт его в обмен на
          initData через <span className="font-mono">POST /api/v1/auth</span>.
        </li>
        <li>
          <span className="text-neutral-300">Portals</span> —{' '}
          <span className="font-mono">tma&nbsp;…</span>, то есть initData как есть.
        </li>
        <li>
          <span className="text-neutral-300">Tonnel</span> — заголовка тоже нет:
          площадка кладёт initData в тело запроса, полем{' '}
          <span className="font-mono">authData</span>. Откройте любой запрос к{' '}
          <span className="font-mono">gifts2.tonnel.network</span> → вкладка{' '}
          <span className="font-mono">Payload</span> → скопируйте значение{' '}
          <span className="font-mono">authData</span> (или{' '}
          <span className="font-mono">user_auth</span> — это одно и то же).
        </li>
        <li>
          <span className="text-neutral-300">GetGems</span> — заголовка
          Authorization нет вовсе, сессия лежит в cookie. Скопируйте строку
          Cookie целиком (там{' '}
          <span className="font-mono">AUTH_TOKEN</span> и{' '}
          <span className="font-mono">JWT_TOKEN</span>) — приложение отправит
          её в нужный заголовок.
        </li>
      </ul>
      <p className="mb-4 text-xs text-neutral-500">Живёт 1–7 дней.</p>

      <div className="mb-4 space-y-2">
        {markets.map((item) => {
          const state = auth.data?.markets[item.key]
          return (
            <div
              key={item.key}
              className="flex items-center justify-between rounded-lg bg-ink-700 px-3 py-2 text-sm"
            >
              <span className="text-neutral-300">
                {item.label}
                {!item.tradable && (
                  <span className="ml-2 text-xs text-neutral-600">только цены</span>
                )}
              </span>
              <span
                className={`text-xs ${
                  !state?.configured
                    ? 'text-neutral-600'
                    : state.stale
                      ? 'text-warn'
                      : 'text-profit'
                }`}
              >
                {!state?.configured
                  ? 'не задан'
                  : state.stale
                    ? `устарел (${state.age_hours}ч)`
                    : `активен (${state.source})`}
              </span>
            </div>
          )
        })}
      </div>

      <div className="space-y-2">
        <select
          value={market}
          onChange={(event) => setMarket(event.target.value)}
          className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 text-sm text-neutral-200"
        >
          {markets.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <textarea
          value={token}
          onChange={(event) => setToken(event.target.value)}
          rows={3}
          placeholder="tma query_id=..."
          className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-xs text-neutral-200"
        />
        <Button variant="primary" disabled={!token.trim()} onClick={() => void save()}>
          Сохранить токен
        </Button>
      </div>
    </div>
  )
}

function UserbotCard({
  auth,
  onReport,
}: {
  auth: ReturnType<typeof useApi<Awaited<ReturnType<typeof api.auth>>>>
  onReport: (tone: 'info' | 'error' | 'warn', text: string) => void
}) {
  const [apiId, setApiId] = useState('')
  const [apiHash, setApiHash] = useState('')

  const save = async () => {
    try {
      const result = await api.setCredentials(apiId, apiHash)
      onReport('info', result.detail)
      setApiId('')
      setApiHash('')
      await auth.reload()
    } catch (e) {
      onReport('error', e instanceof Error ? e.message : String(e))
    }
  }

  const available = auth.data?.userbot_available

  return (
    <div className="card">
      <h2 className="mb-1 text-sm font-medium text-neutral-300">
        Автообновление токена (userbot)
      </h2>
      <p className="mb-4 text-xs text-neutral-500">
        Приложение само логинится в Telegram и обновляет токен. Работает автономно,
        но требует хранить сессию Telegram локально и повышает риск блокировки
        аккаунта — используйте отдельный.
      </p>

      {!available ? (
        <Alert tone="warn">
          Pyrogram недоступен в этой сборке. Ставить его через <code>pip</code> нет
          смысла: приложение — собранный exe со своим окружением, системный
          site-packages он не видит, поэтому эта надпись от установки не исчезнет.
          Обновите приложение — начиная со свежей сборки Pyrogram входит внутрь.
          Либо пользуйтесь ручным вводом токена: он работает без зависимостей вовсе.
        </Alert>
      ) : (
        <div className="space-y-2">
          <div className="text-xs text-neutral-500">
            Получить на my.telegram.org →&nbsp;API development tools
          </div>
          <input
            value={apiId}
            onChange={(event) => setApiId(event.target.value)}
            placeholder="api_id"
            className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-sm text-neutral-200"
          />
          <input
            value={apiHash}
            onChange={(event) => setApiHash(event.target.value)}
            placeholder="api_hash"
            type="password"
            className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-sm text-neutral-200"
          />
          <div className="flex gap-2">
            <Button
              variant="primary"
              disabled={!apiId.trim() || !apiHash.trim()}
              onClick={() => void save()}
            >
              Сохранить
            </Button>
            {auth.data?.credentials_saved && (
              <Button
                onClick={() =>
                  void api
                    .refreshToken('portals')
                    .then((r) => onReport('info', r.detail))
                    .catch((e: Error) => onReport('error', e.message))
                }
              >
                Обновить токен Portals
              </Button>
            )}
          </div>

          {auth.data?.credentials_saved && (
            <TelegramLogin auth={auth} onReport={onReport} />
          )}
        </div>
      )}
    </div>
  )
}

/** Вход в Telegram по шагам: телефон → код → облачный пароль. */
function TelegramLogin({
  auth,
  onReport,
}: {
  auth: { data: AuthStatus | null; reload: () => Promise<unknown> | void }
  onReport: (tone: 'info' | 'error' | 'warn', text: string) => void
}) {
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [stage, setStage] = useState<'phone' | 'code' | 'password'>('phone')
  const [busy, setBusy] = useState(false)

  const run = async (action: () => Promise<{ detail: string; needs_password?: boolean }>) => {
    setBusy(true)
    try {
      const result = await action()
      onReport('info', result.detail)
      return result
    } catch (e) {
      onReport('error', e instanceof Error ? e.message : String(e))
      return null
    } finally {
      setBusy(false)
    }
  }

  if (auth.data?.session_ready && stage === 'phone') {
    return (
      <div className="mt-3 flex items-center justify-between rounded-lg bg-ink-700 px-3 py-2">
        <span className="text-xs text-neutral-400">Вход в Telegram выполнен</span>
        <Button
          onClick={() =>
            void api
              .telegramLogout()
              .then((r) => onReport('info', r.detail))
              .then(() => auth.reload())
              .catch((e: Error) => onReport('error', e.message))
          }
        >
          Выйти
        </Button>
      </div>
    )
  }

  return (
    <div className="mt-3 space-y-2 border-t border-ink-500 pt-3">
      <div className="text-xs text-neutral-500">
        Вход в Telegram. Код придёт в само приложение — введите его сюда, а не в консоль.
      </div>

      <ProxyField auth={auth} onReport={onReport} />

      {stage === 'phone' && (
        <>
          <input
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
            placeholder="+79991234567"
            className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-sm text-neutral-200"
          />
          <Button
            variant="primary"
            disabled={busy || !phone.trim()}
            onClick={() =>
              void run(() => api.telegramCode(phone)).then((r) => r && setStage('code'))
            }
          >
            Получить код
          </Button>
        </>
      )}

      {stage === 'code' && (
        <>
          <input
            value={code}
            onChange={(event) => setCode(event.target.value)}
            placeholder="код из Telegram"
            className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-sm text-neutral-200"
          />
          <Button
            variant="primary"
            disabled={busy || !code.trim()}
            onClick={() =>
              void run(() => api.telegramSignIn(code)).then(async (result) => {
                if (!result) return
                if (result.needs_password) {
                  setStage('password')
                  return
                }
                setStage('phone')
                await auth.reload()
              })
            }
          >
            Подтвердить
          </Button>
        </>
      )}

      {stage === 'password' && (
        <>
          <input
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
            placeholder="облачный пароль"
            className="w-full rounded-md border border-ink-500 bg-ink-900 px-3 py-2 text-sm text-neutral-200"
          />
          <Button
            variant="primary"
            disabled={busy || !password}
            onClick={() =>
              void run(() => api.telegramPassword(password)).then(async (result) => {
                if (!result) return
                setPassword('')
                setStage('phone')
                await auth.reload()
              })
            }
          >
            Войти
          </Button>
        </>
      )}
    </div>
  )
}

function HarCard({
  markets,
  onReport,
  onDone,
}: {
  markets: MarketRow[]
  onReport: (tone: 'info' | 'error' | 'warn', text: string) => void
  onDone: () => void
}) {
  const [market, setMarket] = useState('mrkt')
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.importHar>> | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const upload = async (file: File) => {
    try {
      const response = await api.importHar(market, file)
      setResult(response)
      onReport(
        'info',
        `Найдено эндпоинтов: ${response.found.length}${
          response.token_saved ? ', токен сохранён' : ''
        }`,
      )
      onDone()
    } catch (e) {
      onReport('error', e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card">
      <h2 className="mb-1 text-sm font-medium text-neutral-300">Импорт эндпоинтов из HAR</h2>
      <p className="mb-3 text-xs leading-relaxed text-neutral-500">
        У MRKT и Portals нет официального API, и адреса запросов меняются. HAR —
        это запись сетевых запросов браузера: из неё приложение достанет реальные
        пути и заголовок авторизации. Файл разбирается локально и не сохраняется.
      </p>

      <ol className="mb-4 space-y-1.5 text-xs leading-relaxed text-neutral-400">
        <li>
          <span className="text-neutral-500">1.</span> Откройте{' '}
          <span className="font-mono text-neutral-300">web.telegram.org/k/</span> в Chrome
          или Edge и войдите в аккаунт. Мобильное приложение не подойдёт — нужны
          инструменты разработчика.
        </li>
        <li>
          <span className="text-neutral-500">2.</span> Найдите бота площадки и запустите
          мини-апп. Нажмите <span className="font-mono text-neutral-300">F12</span> →
          вкладка <span className="font-mono text-neutral-300">Network</span>, включите{' '}
          <span className="font-mono text-neutral-300">Preserve log</span>, фильтр{' '}
          <span className="font-mono text-neutral-300">Fetch/XHR</span>.
        </li>
        <li>
          <span className="text-neutral-500">3.</span> Пройдите по разделам: список
          коллекций → конкретная коллекция → прокрутите лоты → фильтр по модели →
          история сделок → профиль с балансом. Каждое действие даёт свой эндпоинт.
        </li>
        <li>
          <span className="text-neutral-500">4.</span> Правый клик в таблице запросов →{' '}
          <span className="font-mono text-neutral-300">Save all as HAR with content</span>.
          Если DevTools предложит два варианта, берите{' '}
          <span className="font-mono text-neutral-300">with sensitive data</span>:
          «sanitized» вырезает заголовок авторизации. Одного пункта в меню тоже
          достаточно — он и есть полный.
        </li>
        <li>
          <span className="text-neutral-500">5.</span> Загрузите файл кнопкой ниже.
          Импорт можно повторять: новые пути накладываются поверх сохранённых,
          остальные остаются как были.
        </li>
      </ol>

      <div className="mb-4 space-y-2">
        <Alert tone="info">
          Урезанный HAR тоже подойдёт. Пути эндпоинтов восстанавливаются и без тел
          ответов — они нужны лишь для автоопределения того, где в JSON лежит
          массив, а адаптер находит его и по типичным именам полей. Если в файле
          не окажется токена, вставьте его отдельно в блоке выше: в DevTools
          откройте любой запрос → Headers → Request Headers → правый клик по{' '}
          <span className="font-mono">Authorization</span> → Copy value.
        </Alert>
        <Alert tone="warn">
          HAR содержит токен вашей сессии — обращайтесь с ним как с паролем и никому
          не пересылайте.
        </Alert>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={market}
          onChange={(event) => setMarket(event.target.value)}
          className="rounded-md border border-ink-500 bg-ink-900 px-3 py-2 text-sm text-neutral-200"
        >
          {markets.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <input
          ref={fileRef}
          type="file"
          accept=".har,application/json"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void upload(file)
          }}
        />
        <Button variant="primary" onClick={() => fileRef.current?.click()}>
          Выбрать HAR-файл
        </Button>
        <Button
          onClick={() =>
            void api
              .resetEndpoints(market)
              .then((r) => onReport('info', r.detail))
              .catch((e: Error) => onReport('error', e.message))
          }
        >
          Сбросить адреса
        </Button>
      </div>
      <p className="mt-2 text-xs text-neutral-500">
        Сброс возвращает адреса площадки к значениям по умолчанию — на случай,
        если импорт записал не то.
      </p>

      {result && (
        <div className="mt-4 space-y-1 rounded-lg bg-ink-700 p-3 text-xs">
          {result.matched_by_fallback && (
            <div className="mb-2">
              <Alert tone="warn">
                Запросов с именем площадки в домене не нашлось — адреса взяты с{' '}
                {result.base_url}. Проверьте, что это действительно её API, а не
                сторонний сервис.
              </Alert>
            </div>
          )}
          {result.rejected_hosts.length > 0 && (
            <div className="mb-2 text-neutral-500">
              Пропущены находки с посторонних доменов:{' '}
              {result.rejected_hosts.join(', ')}
            </div>
          )}
          <div className="mb-2 text-neutral-400">Базовый адрес: {result.base_url}</div>
          {result.found.map((item) => (
            <div key={item.endpoint} className="flex justify-between font-mono text-neutral-500">
              <span className="text-neutral-300">{item.endpoint}</span>
              <span>
                {item.method} {item.path}
                {item.records_in_sample > 0 && ` (${item.records_in_sample} записей)`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function BackdropList({
  value,
  onSave,
}: {
  value: string[]
  onSave: (list: string[]) => void
}) {
  const [draft, setDraft] = useState(value.join('\n'))

  return (
    <div>
      <div className="mb-1 text-sm text-neutral-400">Ценные фоны</div>
      <p className="mb-2 text-xs text-neutral-500">
        По одному в строке. Регистр не важен.
      </p>
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        rows={6}
        className="w-full rounded-lg border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-xs text-neutral-200"
        placeholder="Black&#10;Onyx Black"
      />
      <div className="mt-2">
        <Button
          onClick={() =>
            onSave(draft.split('\n').map((line) => line.trim()).filter(Boolean))
          }
        >
          Сохранить фоны
        </Button>
      </div>
    </div>
  )
}

function NotifyCard({
  config,
  onPatch,
  onReport,
}: {
  config: NonNullable<Awaited<ReturnType<typeof api.config>>>
  onPatch: (body: Record<string, unknown>) => Promise<void>
  onReport: (tone: 'info' | 'error' | 'warn', text: string) => void
}) {
  const notify = config.notify
  const transfer = config.transfer
  const [chat, setChat] = useState(notify.chat)

  return (
    <div className="card">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-sm font-medium text-neutral-300">Уведомления в Telegram</h2>
        <div className="flex gap-2">
          <Button
            onClick={() =>
              void api
                .notifyTest()
                .then((r) => onReport('info', r.detail))
                .catch((e: Error) => onReport('error', e.message))
            }
          >
            Проверить
          </Button>
          <Button
            variant={notify.enabled ? 'default' : 'primary'}
            onClick={() => void onPatch({ notify: { enabled: !notify.enabled } })}
          >
            {notify.enabled ? 'Выключить' : 'Включить'}
          </Button>
        </div>
      </div>
      <p className="mb-4 text-xs leading-relaxed text-neutral-500">
        Приходят вашей же сессией Telegram — отдельный бот не нужен, но нужен
        выполненный вход выше. В сообщении о покупке идут цены коллекции по всем
        площадкам: решение о переносе принимаете вы, и без цен его не принять.
        <br />
        Перенос делается вручную — через ЛС бота площадки, и стоит{' '}
        {transfer.stars_per_gift} ⭐. Чтобы движок не продал подарок, пока вы его
        переносите, нажмите <span className="text-neutral-300">«Заморозить»</span>{' '}
        в Инвентаре. Без заморозки лот продаётся там, где куплен.
      </p>

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm text-neutral-400">Куда слать</span>
          <div className="flex gap-2">
            <input
              value={chat}
              onChange={(event) => setChat(event.target.value)}
              placeholder="me"
              className="w-40 rounded-md border border-ink-500 bg-ink-900 px-2 py-1 font-mono text-sm text-neutral-200"
            />
            <Button
              disabled={chat === notify.chat}
              onClick={() => void onPatch({ notify: { chat: chat.trim() || 'me' } })}
            >
              OK
            </Button>
          </div>
        </div>
        <p className="text-xs text-neutral-600">
          <span className="font-mono">me</span> — «Избранное». Можно указать
          @username или id чата.
        </p>

        <div className="flex flex-wrap gap-2">
          <Button
            variant={notify.on_buy ? 'primary' : 'default'}
            onClick={() => void onPatch({ notify: { on_buy: !notify.on_buy } })}
          >
            О покупках: {notify.on_buy ? 'да' : 'нет'}
          </Button>
          <Button
            variant={notify.on_sell ? 'primary' : 'default'}
            onClick={() => void onPatch({ notify: { on_sell: !notify.on_sell } })}
          >
            О продажах: {notify.on_sell ? 'да' : 'нет'}
          </Button>
        </div>

        <Field
          label="Сообщать о разнице площадок от"
          value={notify.min_spread}
          onSave={(value) => void onPatch({ notify: { min_spread: value } })}
        />
        <Field
          label="Звёзд за перенос"
          value={transfer.stars_per_gift}
          onSave={(value) => void onPatch({ transfer: { stars_per_gift: value } })}
        />
        <Field
          label="Цена звезды в TON"
          value={transfer.star_price_ton}
          onSave={(value) => void onPatch({ transfer: { star_price_ton: value } })}
        />
        <p className="text-xs text-neutral-600">
          Перенос обходится в{' '}
          {(transfer.stars_per_gift * transfer.star_price_ton).toFixed(2)} TON — эта
          сумма показывается в уведомлении рядом с разницей цен.
        </p>
      </div>
    </div>
  )
}

function Field({
  label,
  value,
  onSave,
}: {
  label: string
  value: number
  onSave: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))

  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-neutral-400">{label}</span>
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="w-24 rounded-md border border-ink-500 bg-ink-900 px-2 py-1 text-right font-mono text-sm text-neutral-200"
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

/** Прокси для Telegram: без него вход не состоится там, где он заблокирован. */
function ProxyField({
  auth,
  onReport,
}: {
  auth: { data: AuthStatus | null; reload: () => Promise<unknown> | void }
  onReport: (tone: 'info' | 'error' | 'warn', text: string) => void
}) {
  const [url, setUrl] = useState(auth.data?.proxy_url ?? '')

  return (
    <div className="space-y-1">
      <div className="flex gap-2">
        <input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="socks5://127.0.0.1:1080 — прокси, если нужен"
          className="flex-1 rounded-md border border-ink-500 bg-ink-900 px-3 py-2 font-mono text-xs text-neutral-200"
        />
        <Button
          onClick={() =>
            void api
              .telegramProxy(url)
              .then((r) => onReport('info', r.detail))
              .then(() => auth.reload())
              .catch((e: Error) => onReport('error', e.message))
          }
        >
          Сохранить
        </Button>
      </div>
      <div className="text-xs text-neutral-600">
        Заполняйте, только если Telegram недоступен напрямую. Пустое поле убирает прокси.
      </div>
    </div>
  )
}
