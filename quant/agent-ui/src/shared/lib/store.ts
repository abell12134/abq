import { create } from 'zustand'
import type { Prediction, SupervisorSession, SystemStatus } from '../api/types'

interface AgentState {
  status: SystemStatus | null
  predictions: Prediction[]
  selectedPredId: string | null
  session: SupervisorSession | null
  supervisorOpen: boolean
  setStatus: (s: SystemStatus) => void
  setPredictions: (p: Prediction[]) => void
  selectPred: (id: string | null) => void
  setSession: (s: SupervisorSession | null) => void
  setSupervisorOpen: (v: boolean) => void
}

export const useAgentStore = create<AgentState>((set) => ({
  status: null,
  predictions: [],
  selectedPredId: null,
  session: null,
  supervisorOpen: false,
  setStatus: (status) => set({ status }),
  setPredictions: (predictions) => set({ predictions }),
  selectPred: (selectedPredId) => set({ selectedPredId }),
  setSession: (session) => set({ session }),
  setSupervisorOpen: (supervisorOpen) => set({ supervisorOpen }),
}))
