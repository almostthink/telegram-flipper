import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useHealth } from './lib/useHealth'
import Dashboard from './pages/Dashboard'
import Opportunities from './pages/Opportunities'
import Orders from './pages/Orders'
import Market from './pages/Market'
import Inventory from './pages/Inventory'
import AutoTrade from './pages/AutoTrade'
import Journal from './pages/Journal'
import Settings from './pages/Settings'

const NAV = [
  { to: '/dashboard', label: 'Обзор' },
  { to: '/opportunities', label: 'Сигналы' },
  { to: '/orders', label: 'Ордер-движок' },
  { to: '/market', label: 'Рынок' },
  { to: '/inventory', label: 'Инвентарь' },
  { to: '/auto', label: 'Автомат' },
  { to: '/journal', label: 'Журнал' },
  { to: '/settings', label: 'Настройки' },
]

export default function App() {
  const { health, online } = useHealth()

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 shrink-0 flex-col border-r border-ink-600 bg-ink-800 p-4">
        <div className="mb-6">
          <div className="text-sm font-semibold text-neutral-100">Gift Flipper</div>
          <div className="font-mono text-xs text-neutral-500">v{health?.version ?? '—'}</div>
        </div>

        <nav className="flex flex-col gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-accent/15 text-accent'
                    : 'text-neutral-400 hover:bg-ink-700 hover:text-neutral-200'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto space-y-2 pt-4">
          <div className="flex items-center gap-2 px-1 text-xs">
            <span className={`h-2 w-2 rounded-full ${online ? 'bg-profit' : 'bg-loss'}`} />
            <span className="text-neutral-500">
              {online ? 'backend на связи' : 'нет связи'}
            </span>
          </div>
          {health && (
            <div
              className={`badge block text-center ${
                health.paper_mode ? 'bg-warn/15 text-warn' : 'bg-loss/15 text-loss'
              }`}
            >
              {health.paper_mode ? 'PAPER — симуляция' : 'LIVE — реальные деньги'}
            </div>
          )}
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-x-auto p-8">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/opportunities" element={<Opportunities />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/market" element={<Market />} />
          <Route path="/inventory" element={<Inventory />} />
          <Route path="/auto" element={<AutoTrade />} />
          <Route path="/journal" element={<Journal />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}
