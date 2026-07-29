import { useCallback, useEffect, useState } from 'react'

interface State<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/** Загрузка данных с ручным обновлением и опциональным опросом. */
export function useApi<T>(
  loader: () => Promise<T>,
  { pollMs, deps = [] }: { pollMs?: number; deps?: unknown[] } = {},
) {
  const [state, setState] = useState<State<T>>({ data: null, error: null, loading: true })

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableLoader = useCallback(loader, deps)

  const reload = useCallback(async () => {
    try {
      const data = await stableLoader()
      setState({ data, error: null, loading: false })
      return data
    } catch (error) {
      setState((prev) => ({
        data: prev.data,
        error: error instanceof Error ? error.message : String(error),
        loading: false,
      }))
      return null
    }
  }, [stableLoader])

  useEffect(() => {
    void reload()
    if (!pollMs) return
    const timer = setInterval(() => void reload(), pollMs)
    return () => clearInterval(timer)
  }, [reload, pollMs])

  return { ...state, reload, setData: (data: T) => setState({ data, error: null, loading: false }) }
}
