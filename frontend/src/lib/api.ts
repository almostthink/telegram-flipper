const BASE = '/api/v1'

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

export interface Status {
  stage: string
  balance_ton: number | null
  open_positions: number
  realized_pnl_ton: number
  unrealized_pnl_ton: number
  signals_pending: number
  marketplaces: Record<string, MarketplaceStatus>
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export const api = {
  health: () => get<Health>('/health'),
  status: () => get<Status>('/status'),
  config: () => get<Record<string, unknown>>('/config'),
}
