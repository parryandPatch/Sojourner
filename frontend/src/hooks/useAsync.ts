/**
 * Data-loading hooks.
 *
 * Deliberately small: the prototype has six screens reading a handful of endpoints, and
 * a caching library would be more machinery than the app needs. Each hook owns its own
 * loading and error state and exposes a `reload` for after mutations.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../api/client'

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** True when the last failure was the 403 role guard rather than a real fault. */
  forbidden: boolean
  reload: () => Promise<void>
  setData: (updater: T | ((previous: T | null) => T)) => void
}

export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [loading, setLoading] = useState(true)
  const mounted = useRef(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    setForbidden(false)
    try {
      const result = await loaderRef.current()
      if (mounted.current) setData(result)
    } catch (caught) {
      if (!mounted.current) return
      if (caught instanceof ApiError) {
        setError(caught.detail)
        setForbidden(caught.isForbidden)
      } else {
        setError(caught instanceof Error ? caught.message : 'Unexpected error')
        setForbidden(false)
      }
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  const update = useCallback((updater: T | ((previous: T | null) => T)) => {
    setData((previous) =>
      typeof updater === 'function' ? (updater as (p: T | null) => T)(previous) : updater,
    )
  }, [])

  return { data, error, loading, forbidden, reload, setData: update }
}
