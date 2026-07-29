const BASE = '/api/v1'

// --- Типы ------------------------------------------------------------------

export interface Health {
  status: string
  version: string
  uptime_sec: number
  paper_mode: boolean
  auto_trade: boolean
  frozen: boolean
  python: string
}

export interface MarketplaceStatus {
  enabled: boolean
  trade_enabled: boolean
  connected: boolean
  note: string
}

export interface RiskStatus {
  spent_today_ton: number
  realized_today_ton: number
  open_positions: number
  daily_budget_ton: number
  max_open_positions: number
  tripped: boolean
  trip_reason: string
  consecutive_errors: number
}

export interface EngineStatus {
  running: boolean
  paper_mode: boolean
  auto_trade: boolean
  risk: RiskStatus
  disabled_markets: Record<string, string>
  breakeven_markup: number
  last_cycle: {
    signals_total: number
    signals_passed: number
    bought: number
    listed: number
    repriced: number
    closed: number
    blocked: string[]
    errors: string[]
  }
}

export interface Status {
  stage: string
  balance_ton: number | null
  open_positions: number
  realized_pnl_ton: number
  unrealized_pnl_ton: number
  invested_ton: number
  signals_pending: number
  data: { collections_tracked: number; sales_recorded: number }
  engine: EngineStatus
  marketplaces: Record<string, MarketplaceStatus>
}

export interface Signal {
  id: number
  market: string
  listing_id: string
  collection: string
  number: number | null
  number_score: number
  number_label: string | null
  model: string | null
  backdrop: string | null
  symbol: string | null
  ask_ton: number
  fair_value_ton: number
  net_roi: number
  liquidity_score: number
  confidence: number
  expected_tts_hours: number | null
  score: number
  passed: boolean
  reject_reason: string | null
  explanation: string
  created_at: string
}

export interface SignalsResponse {
  signals: Signal[]
  rejections: Record<string, number>
  min_roi: number
  min_liquidity: number
}

export interface CollectionRow {
  collection: string
  floor_ton: number
  volume_24h_ton: number | null
  sales_24h: number | null
  listed_count: number | null
}

export interface CollectionDetail {
  collection: string
  floor_ton: number
  liquidity: {
    score: number
    measured: boolean
    missing: string[]
    parts: Record<string, number>
    sales_24h: number
    sales_7d: number
    tts_median_hours: number | null
    expected_tts_hours: number
    depth_10pct: number
    floor_trend_24h: number | null
  }
  floor_history: { t: string; floor_ton: number }[]
  attributes: {
    kind: string
    name: string
    floor_ton: number
    rarity_permille: number | null
    multiplier: number | null
  }[]
  book: {
    market: string
    listing_id: string
    price_ton: number
    model: string | null
    backdrop: string | null
    symbol: string | null
    url: string | null
  }[]
  recent_sales: {
    price_ton: number
    model: string | null
    sold_at: string
    tts_hours: number | null
  }[]
}

export interface Position {
  id: number
  market: string
  collection: string
  model: string | null
  buy_price_ton: number
  ask_price_ton: number | null
  sell_price_ton: number | null
  fair_value_at_buy: number | null
  expected_tts_hours: number | null
  net_pnl_ton: number | null
  unrealized_ton: number | null
  reprice_count: number
  status: string
  bought_at: string
  sold_at: string | null
  reason: string | null
  paper: boolean
}

export interface TradingStats {
  paper: boolean
  closed_trades: number
  open_positions: number
  realized_pnl_ton: number
  unrealized_pnl_ton: number
  invested_ton: number
  win_rate: number
  avg_win_ton: number
  avg_loss_ton: number
  expectancy_ton: number
  median_hold_hours: number | null
  tts_bias: number | null
  max_drawdown_ton: number
  best_day_ton: number
  worst_day_ton: number
  profitable_days: number
  losing_days: number
  daily: { day: string; realized_ton: number; trades: number }[]
  by_collection: Record<string, number>
}

export interface BacktestResult {
  days: number
  candidates_considered: number
  trades: number
  net_pnl_ton: number
  invested_ton: number
  return_on_invested: number
  win_rate: number
  stuck_share: number
  note: string
  sample: {
    collection: string
    model: string | null
    buy_ton: number
    fair_ton: number
    exit_ton: number | null
    pnl_ton: number
    hold_hours: number | null
    outcome: string
  }[]
}

export interface AuthStatus {
  markets: Record<
    string,
    { configured: boolean; source: string | null; age_hours: number | null; stale: boolean }
  >
  vault_backend: string
  vault_secure: boolean
  userbot_available: boolean
  credentials_saved: boolean
}

export interface JournalEntry {
  id: number
  position_id: number | null
  at: string
  action: string
  market: string | null
  price_ton: number | null
  detail: string | null
  ok: boolean
  paper: boolean
}

export interface AppConfig {
  paper_mode: boolean
  auto_trade: boolean
  analytics: Record<string, number>
  collectible: {
    number_bonus: number
    preferred_backdrops: string[]
    backdrop_bonus: number
    require_collectible: boolean
    min_number_score: number
  }
  risk: Record<string, number | string[]>
  sell: Record<string, number>
  marketplaces: Record<
    string,
    { enabled: boolean; trade_enabled: boolean; fee_sell: number; fee_buy: number }
  >
}

// --- Транспорт --------------------------------------------------------------

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init)
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // тело не JSON — оставляем статус
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  health: () => request<Health>('/health'),
  status: () => request<Status>('/status'),
  config: () => request<AppConfig>('/config'),
  updateConfig: (patch: Record<string, unknown>) =>
    request<AppConfig>('/config', { ...json(patch), method: 'PUT' }),

  signals: (onlyPassed = true) =>
    request<SignalsResponse>(`/signals?only_passed=${onlyPassed}`),
  refreshSignals: () => request<Record<string, unknown>>('/signals/refresh', { method: 'POST' }),

  collections: () => request<{ collections: CollectionRow[] }>('/collections'),
  collection: (name: string) =>
    request<CollectionDetail>(`/collections/${encodeURIComponent(name)}`),

  engine: () => request<EngineStatus>('/engine'),
  runCycle: (deep = true) =>
    request<Record<string, unknown>>(`/engine/cycle?deep=${deep}`, { method: 'POST' }),
  kill: () => request<EngineStatus>('/engine/kill', { method: 'POST' }),
  resetBreaker: () => request<EngineStatus>('/engine/reset-breaker', { method: 'POST' }),

  positions: (status: 'open' | 'closed' | 'all' = 'open') =>
    request<{ positions: Position[]; paper: boolean }>(`/positions?status=${status}`),
  buy: (market: string, listingId: string) =>
    request<{ ok: boolean; detail: string }>('/positions/buy', json({ market, listing_id: listingId })),
  sell: (id: number, priceTon: number) =>
    request<{ ok: boolean; detail: string }>(`/positions/${id}/sell`, json({ price_ton: priceTon })),
  relist: (id: number, priceTon: number) =>
    request<{ ok: boolean; detail: string }>(`/positions/${id}/relist`, json({ price_ton: priceTon })),

  stats: (days = 30) => request<TradingStats>(`/stats?days=${days}`),
  backtest: (days = 30) => request<BacktestResult>(`/backtest?days=${days}`, { method: 'POST' }),
  journal: () => request<{ entries: JournalEntry[] }>('/journal'),

  auth: () => request<AuthStatus>('/auth'),
  setToken: (market: string, token: string) =>
    request<{ ok: boolean; detail: string }>('/auth/token', json({ market, token })),
  setCredentials: (apiId: string, apiHash: string) =>
    request<{ ok: boolean; detail: string }>('/auth/credentials', json({ api_id: apiId, api_hash: apiHash })),
  refreshToken: (market: string) =>
    request<{ ok: boolean; detail: string }>(`/auth/refresh/${market}`, { method: 'POST' }),

  endpoints: () => request<Record<string, { base_url: string; endpoints: Record<string, { path: string; method: string; json_path: string }> }>>('/endpoints'),
  importHar: async (market: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{
      ok: boolean
      base_url: string
      token_saved: boolean
      matched_by_fallback: boolean
      found: { endpoint: string; path: string; method: string; records_in_sample: number }[]
      skipped: number
    }>(`/endpoints/${market}/import-har`, { method: 'POST', body: form })
  },
}

// --- Форматирование ---------------------------------------------------------

export const ton = (value: number | null | undefined, digits = 2): string =>
  value == null ? '—' : `${value.toFixed(digits)} TON`

export const signedTon = (value: number | null | undefined, digits = 2): string =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(digits)} TON`

export const percent = (value: number | null | undefined, digits = 1): string =>
  value == null ? '—' : `${(value * 100).toFixed(digits)}%`

export const hours = (value: number | null | undefined): string => {
  if (value == null) return '—'
  if (value < 24) return `${value.toFixed(0)}ч`
  return `${(value / 24).toFixed(1)}д`
}
