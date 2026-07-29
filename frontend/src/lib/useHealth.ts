import { useEffect, useState } from 'react'
import { api, type Health } from './api'

const POLL_MS = 5000

/** Пульс backend для индикатора связи в шапке. */
export function useHealth() {
  const [health, setHealth] = useState<Health | null>(null)
  const [online, setOnline] = useState(false)

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      try {
        const data = await api.health()
        if (cancelled) return
        setHealth(data)
        setOnline(true)
      } catch {
        if (!cancelled) setOnline(false)
      }
    }

    void tick()
    const timer = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return { health, online }
}
