/**
 * Active role for the whole app.
 *
 * The role selector exists so a reviewer can see both sides of the approval guard
 * without editing code. It is explicitly NOT a security control: the backend checks the
 * same role header on every request, and the capabilities shown here come from the
 * server's own `role_matrix` so the UI cannot claim permissions the API will refuse.
 */
import { createContext, useCallback, useContext } from 'react'

import type { Role, RoleCapabilities } from '../types/api'

export const ROLES: Role[] = ['OBSERVER', 'PLANNER', 'MAINTAINER', 'COMMANDER']

export interface RoleContextValue {
  role: Role
  setRole: (role: Role) => void
  capabilities: RoleCapabilities | null
}

export const RoleContext = createContext<RoleContextValue>({
  role: 'OBSERVER',
  setRole: () => undefined,
  capabilities: null,
})

export function useRole(): RoleContextValue {
  return useContext(RoleContext)
}

/** Convenience for the common "can this role do X" question. */
export function useCan(): (action: keyof RoleCapabilities) => boolean {
  const { capabilities } = useRole()
  return useCallback(
    (action: keyof RoleCapabilities) => Boolean(capabilities?.[action]),
    [capabilities],
  )
}
