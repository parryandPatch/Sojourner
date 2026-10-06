/**
 * WebSocket live feed.
 *
 * The socket is notification-only: it pushes event, plan, proposal and approval
 * messages. Nothing a client sends over it can change state, and the hook never sends
 * anything except a keepalive. Reconnection is automatic with a fixed backoff.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { liveSocketUrl } from '../api/client'
import type { LiveMessage } from '../types/api'

export type ConnectionState = 'connecting' | 'open' | 'closed'

export interface LiveFeed {
  messages: LiveMessage[]
  connection: ConnectionState
  latest: LiveMessage | null
  clear: () => void
}

const MAX_BUFFERED = 50

export function useLiveFeed(onMessage?: (message: LiveMessage) => void): LiveFeed {
  const [messages, setMessages] = useState<LiveMessage[]>([])
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const timerRef = useRef<number | null>(null)
  const handlerRef = useRef(onMessage)
  handlerRef.current = onMessage

  const connect = useCallback(() => {
    const socket = new WebSocket(liveSocketUrl())
    socketRef.current = socket

    socket.onopen = () => {
      retryRef.current = 0
      setConnection('open')
    }

    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data as string) as LiveMessage
        setMessages((previous) => [message, ...previous].slice(0, MAX_BUFFERED))
        handlerRef.current?.(message)
      } catch {
        // Ignore malformed frames rather than tearing down the connection.
      }
    }

    socket.onclose = () => {
      setConnection('closed')
      // Fixed backoff; this is a local demo feed, so there is nothing clever to do.
      const delay = Math.min(1000 * 2 ** retryRef.current, 10_000)
      retryRef.current += 1
      timerRef.current = window.setTimeout(connect, delay)
    }

    socket.onerror = () => socket.close()
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
    }
  }, [connect])

  const clear = useCallback(() => setMessages([]), [])

  return { messages, connection, latest: messages[0] ?? null, clear }
}
