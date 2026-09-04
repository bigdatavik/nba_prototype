import { create } from "zustand";
import { api, AskResponse } from "./api";

export interface GenieTurn {
  q: string;
  res: AskResponse | null; // null while pending
  error?: string;
}

interface GenieState {
  open: boolean; // global AssistantDrawer visibility
  conversationId: string | null;
  history: GenieTurn[];
  loading: boolean;
  // A member id the launcher can weave into a suggested question (context-aware).
  contextMemberId: string | null;

  setOpen: (open: boolean) => void;
  setContextMember: (id: string | null) => void;
  ask: (question: string) => Promise<void>;
  reset: () => void;
}

export const useGenie = create<GenieState>((set, get) => ({
  open: false,
  conversationId: null,
  history: [],
  loading: false,
  contextMemberId: null,

  setOpen: (open) => set({ open }),
  setContextMember: (id) => set({ contextMemberId: id }),

  ask: async (question: string) => {
    const q = question.trim();
    if (!q || get().loading) return;
    // optimistic pending turn
    set((s) => ({ loading: true, history: [...s.history, { q, res: null }] }));
    try {
      const res = await api.askNba(q, get().conversationId);
      set((s) => {
        const history = [...s.history];
        history[history.length - 1] = { q, res };
        return {
          history,
          loading: false,
          conversationId: res.conversation_id ?? s.conversationId,
        };
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      set((s) => {
        const history = [...s.history];
        history[history.length - 1] = { q, res: null, error: msg };
        return { history, loading: false };
      });
    }
  },

  reset: () => set({ conversationId: null, history: [] }),
}));
