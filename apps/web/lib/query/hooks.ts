"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import type {
  AiConfig,
  AnalyticsSeries,
  AuditEntry,
  BookingResource,
  CalendarBooking,
  CalendarDay,
  ChannelConfig,
  Conversation,
  ConversationDetail,
  HolidayException,
  KnowledgeSource,
  Member,
  Service,
  WeeklyHours,
} from "@/lib/api/types";
import { apiFetch } from "@/lib/api/client";
import { shiftDay, todayInTimezone } from "@/lib/calendar";
import { useKeys } from "@/lib/query/keys";
import { useTenant } from "@/components/shell/Providers";
import { toast } from "@/components/ui/toast";
import {
  MOCK_AI_CONFIG,
  MOCK_ACTIVE_CONVERSATIONS,
  MOCK_AUDIT,
  MOCK_CHANNELS,
  MOCK_CONVERSATIONS,
  MOCK_HOLIDAYS,
  MOCK_HOURS,
  MOCK_KNOWLEDGE,
  MOCK_KPIS,
  MOCK_MEMBERS,
  MOCK_MODELS,
  MOCK_RESOURCES,
  MOCK_SERVICES,
  mockBookingList,
  mockCalendarDay,
  mockConversationDetail,
  mockLatencySeries,
  mockOutcomeMix,
  mockVolumeSeries,
} from "@/lib/mock/data";

/**
 * Phase 1's API is narrow (PHASE1-API-CONTRACT.md's non-goals: bookings,
 * knowledge, voice, billing). Everything the skeleton does not serve resolves
 * from lib/mock with a small latency so loading states are real rather than
 * theoretical — a screen whose data arrives synchronously never reveals that
 * its skeleton is broken.
 *
 * Each hook is one `apiFetch` away from being live; the QUERY KEYS and the
 * cache semantics around them are already correct, which is the part that
 * matters for tenant isolation.
 */
function mockResolve<T>(value: T, ms = 220): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

// --- Dashboard -------------------------------------------------------------

export function useDashboardKpis() {
  const keys = useKeys();
  return useQuery({
    queryKey: keys.dashboard.kpis(),
    queryFn: () => mockResolve(MOCK_KPIS),
  });
}

export function useVolumeSeries() {
  const keys = useKeys();
  return useQuery<AnalyticsSeries[]>({
    queryKey: keys.dashboard.volume(),
    queryFn: () => mockResolve(mockVolumeSeries()),
  });
}

export function useLatencySeries() {
  const keys = useKeys();
  return useQuery<AnalyticsSeries[]>({
    queryKey: keys.analytics("latency", {}),
    queryFn: () => mockResolve(mockLatencySeries()),
  });
}

export function useOutcomeMix() {
  const keys = useKeys();
  return useQuery({
    queryKey: keys.analytics("outcomes", {}),
    queryFn: () => mockResolve(mockOutcomeMix()),
  });
}

// --- Conversations ---------------------------------------------------------

export interface ConversationFilters {
  q?: string;
  channel?: string;
  outcome?: string;
  contained?: string;
}

export function useConversations(filters: ConversationFilters) {
  const keys = useKeys();
  return useQuery<Conversation[]>({
    queryKey: keys.conversations.list(filters),
    queryFn: () => {
      const q = filters.q?.trim().toLowerCase();
      const filtered = MOCK_CONVERSATIONS.filter((c) => {
        if (filters.channel && filters.channel !== "all" && c.channel !== filters.channel) return false;
        if (filters.outcome && filters.outcome !== "all" && c.outcome !== filters.outcome) return false;
        if (filters.contained === "yes" && !c.contained) return false;
        if (filters.contained === "no" && c.contained) return false;
        if (q) {
          const hay = `${c.caller_name ?? ""} ${c.caller_number ?? ""} ${c.summary ?? ""} ${c.intent ?? ""}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
      return mockResolve(filtered);
    },
    // A list the user is filtering should feel immediate; keeping the previous
    // page visible while the next resolves avoids a flash of empty table on
    // every keystroke.
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  });
}

export function useConversation(id: string) {
  const keys = useKeys();
  return useQuery<ConversationDetail>({
    queryKey: keys.conversations.detail(id),
    queryFn: () => mockResolve(mockConversationDetail(id)),
    enabled: Boolean(id),
  });
}

export function useLiveConversations() {
  const keys = useKeys();
  return useQuery<Conversation[]>({
    queryKey: keys.conversations.live(),
    queryFn: () => mockResolve(MOCK_ACTIVE_CONVERSATIONS, 120),
    // Live calls are the one thing that should keep moving without a reload.
    // In the real build this is replaced by the SSE subscription in
    // lib/realtime — polling is the fallback, not the design.
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
}

// --- The diary -------------------------------------------------------------

/**
 * Whether the portal is wired to a real API.
 *
 * `NEXT_PUBLIC_API_LIVE` mirrors the server's `API_ORIGIN` (next.config.mjs
 * installs the `/api/v1/*` rewrite only when that is set). Without the flag a
 * calendar query would hit the Next dev server's 404 page, get HTML back, and
 * fail with a JSON parse error — which reads as "the calendar is broken"
 * rather than "there is no API here". Choosing the source explicitly keeps
 * both worlds honest: with the flag on, a genuine API failure surfaces as an
 * error state rather than being papered over by mock rows.
 */
const API_IS_LIVE = process.env.NEXT_PUBLIC_API_LIVE === "1";

export interface CalendarFilters {
  /** A resource id, or "all". Never a name — a tenant can rename a resource. */
  resourceId?: string;
}

/**
 * One business day of the diary.
 *
 * `date` is the tenant's own calendar day as `YYYY-MM-DD` and is sent to the
 * server as-is. It is deliberately NOT derived from a `Date` at request time:
 * the browser's midnight and the practice's midnight are different instants
 * for part of every day, and a diary that quietly showed tomorrow at 11pm
 * would be reported as data loss.
 */
export function useCalendarDay(date: string, filters: CalendarFilters = {}) {
  const keys = useKeys();
  const resourceId = filters.resourceId;

  return useQuery<CalendarDay>({
    queryKey: keys.bookings.day(date, { resourceId: resourceId ?? "all" }),
    queryFn: () => {
      if (API_IS_LIVE) {
        const params = new URLSearchParams({ date });
        // The CALENDAR always shows the whole day, past hours included: a grid
        // with the morning silently missing is not a diary, and a receptionist
        // looking at 2pm still needs to see who came in at 9. Hiding past
        // appointments is the LIST's default, where the question is "what is
        // coming up", not "what does this day look like".
        params.set("include_past", "true");
        if (resourceId && resourceId !== "all") params.set("resource_id", resourceId);
        return apiFetch<CalendarDay>(`/api/v1/bookings?${params}`);
      }
      const day = mockCalendarDay(date);
      if (!resourceId || resourceId === "all") return mockResolve(day);
      return mockResolve({
        ...day,
        items: day.items.filter((b) => b.resources.some((r) => r.id === resourceId)),
      });
    },
    // Keeping the previous day on screen while the next resolves stops the
    // grid from collapsing to skeletons on every arrow press, which at one
    // press per second reads as flicker rather than as loading.
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  });
}

/** The resources a diary can be filtered by. */
export function useResources() {
  const keys = useKeys();
  return useQuery<BookingResource[]>({
    queryKey: keys.resources.all(),
    queryFn: async () => {
      if (API_IS_LIVE) {
        const page = await apiFetch<{ items: BookingResource[] }>("/api/v1/resources");
        return page.items;
      }
      return mockResolve(MOCK_RESOURCES);
    },
    // A practice hires a hygienist a few times a decade; refetching this on
    // every window focus is pure noise.
    staleTime: 10 * 60_000,
  });
}

// --- Bookings (the list view) ----------------------------------------------

/** How far ahead the list looks. A quarter, matching the API's range cap. */
const LIST_HORIZON_DAYS = 90;
/** How far back "include past" reaches. */
const LIST_HISTORY_DAYS = 90;

export interface BookingFilters {
  /** A `BookingStatus`, or "all". */
  status?: string;
  /**
   * Show appointments that have already finished.
   *
   * Off by default because the desk's question is almost always "what is
   * coming up". Kept reachable because "who missed their appointment
   * yesterday" is a real question, and answering the first by deleting the
   * ability to answer the second would be the wrong trade.
   */
  includePast?: boolean;
}

/**
 * The booking list — the same endpoint the calendar uses, over a range.
 *
 * Both the filtering and the windowing happen SERVER-SIDE. Fetching everything
 * and filtering in the browser would mean a practice with a year of history
 * ships every past appointment over the wire so React can hide most of them,
 * and the "upcoming" default would be a lie told after the data had already
 * arrived.
 */
export function useBookings(filters: BookingFilters = {}) {
  const keys = useKeys();
  const { timezone } = useTenant();
  const status = filters.status ?? "all";
  const includePast = filters.includePast ?? false;

  // The window is anchored to the TENANT's today, not the browser's. Computed
  // once per mount so a tab left open overnight does not silently re-slice its
  // own list under the reader.
  const today = useMemo(() => todayInTimezone(timezone), [timezone]);
  const from = useMemo(
    () => shiftDay(today, includePast ? -LIST_HISTORY_DAYS : 0),
    [today, includePast],
  );
  const to = useMemo(() => shiftDay(today, LIST_HORIZON_DAYS), [today]);

  return useQuery<CalendarBooking[]>({
    queryKey: keys.bookings.list({ status, includePast, from, to }),
    queryFn: async () => {
      if (API_IS_LIVE) {
        const params = new URLSearchParams({ from, to });
        if (status !== "all") params.set("status", status);
        if (includePast) params.set("include_past", "true");
        // `cancelled` is hidden by the API's default, so asking for it
        // explicitly has to lift that as well as narrow the status.
        if (status === "cancelled") params.set("include_cancelled", "true");
        const page = await apiFetch<CalendarDay>(`/api/v1/bookings?${params}`);
        return page.items;
      }
      return mockResolve(mockBookingList({ from, to, status, includePast }));
    },
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  });
}

// --- Services --------------------------------------------------------------

export function useServices() {
  const keys = useKeys();
  return useQuery<Service[]>({
    queryKey: keys.services.list(),
    queryFn: () => mockResolve(MOCK_SERVICES),
  });
}

/**
 * A toggle is one of exactly three shapes optimism is right for (06 §11.3):
 * toggles, inline field edits, and reordering. It cannot 409, it runs no
 * workflow, and it involves no money.
 */
export function useToggleService() {
  const keys = useKeys();
  const qc = useQueryClient();

  return useMutation({
    mutationFn: async ({ id, isActive }: { id: string; isActive: boolean; version: number }) => {
      await mockResolve(null, 400);
      const svc = MOCK_SERVICES.find((s) => s.id === id);
      if (svc) {
        svc.is_active = isActive;
        svc.version += 1;
      }
      return svc!;
    },

    onMutate: async ({ id, isActive }) => {
      // Cancel in-flight refetches: one landing mid-mutation would overwrite
      // the optimistic value with stale server data and the toggle would
      // visibly flip back and then forward again.
      await qc.cancelQueries({ queryKey: keys.services.all() });
      const previous = qc.getQueriesData<Service[]>({ queryKey: keys.services.all() });
      qc.setQueriesData<Service[]>({ queryKey: keys.services.all() }, (list) =>
        list?.map((s) => (s.id === id ? { ...s, is_active: isActive } : s)),
      );
      return { previous };
    },

    onError: (_error, _vars, context) => {
      // Restore EVERY snapshot, not just the list we happened to be looking
      // at: `services` also feeds the booking form's picker and the policy
      // editor, and rolling back one leaves the others showing a value the
      // server rejected.
      context?.previous.forEach(([key, data]) => qc.setQueryData(key, data));
      toast({ title: "Could not update the service", variant: "destructive" });
    },

    // Always refetch, success or failure. On success it picks up the new
    // version for the next If-Match; on failure it resyncs after the rollback.
    onSettled: () => qc.invalidateQueries({ queryKey: keys.services.all() }),
  });
}

export function useSaveService() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (service: Partial<Service> & { name: string }) => {
      await mockResolve(null, 500);
      if (service.id) {
        const idx = MOCK_SERVICES.findIndex((s) => s.id === service.id);
        if (idx >= 0) {
          MOCK_SERVICES[idx] = { ...MOCK_SERVICES[idx]!, ...service, version: MOCK_SERVICES[idx]!.version + 1 };
        }
      } else {
        MOCK_SERVICES.push({
          id: `svc_${MOCK_SERVICES.length + 1}`,
          description: null, category: null, requires_deposit: false,
          buffer_after_minutes: 5, is_active: true, duration_minutes: 30,
          price: { amount: "0.00", currency: "GBP" }, version: 1,
          ...service,
        } as Service);
      }
      return service;
    },
    // Creating is NOT optimistic: it can fail validation server-side, and a
    // row that appears and then vanishes is worse than a half-second wait.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.services.all() });
      toast({ title: "Service saved", variant: "success" });
    },
    onError: () => toast({ title: "Could not save the service", variant: "destructive" }),
  });
}

// --- Hours -----------------------------------------------------------------

export function useWeeklyHours() {
  const keys = useKeys();
  return useQuery<WeeklyHours[]>({
    queryKey: keys.hours.weekly(),
    queryFn: () => mockResolve(MOCK_HOURS),
  });
}

export function useHolidays() {
  const keys = useKeys();
  return useQuery<HolidayException[]>({
    queryKey: keys.hours.holidays(),
    queryFn: () => mockResolve(MOCK_HOLIDAYS),
  });
}

export function useSaveHours() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (hours: WeeklyHours[]) => {
      await mockResolve(null, 600);
      return hours;
    },
    onSuccess: (hours) => {
      qc.setQueryData(keys.hours.weekly(), hours);
      toast({ title: "Opening hours saved", variant: "success" });
    },
    onError: () => toast({ title: "Could not save opening hours", variant: "destructive" }),
  });
}

export function useSaveHoliday() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (h: HolidayException) => {
      await mockResolve(null, 400);
      const idx = MOCK_HOLIDAYS.findIndex((x) => x.id === h.id);
      if (idx >= 0) MOCK_HOLIDAYS[idx] = h;
      else MOCK_HOLIDAYS.push({ ...h, id: `hol_${MOCK_HOLIDAYS.length + 1}` });
      return h;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.hours.holidays() });
      toast({ title: "Exception saved", variant: "success" });
    },
  });
}

export function useDeleteHoliday() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await mockResolve(null, 300);
      const idx = MOCK_HOLIDAYS.findIndex((x) => x.id === id);
      if (idx >= 0) MOCK_HOLIDAYS.splice(idx, 1);
      return id;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.hours.holidays() });
      toast({ title: "Exception removed" });
    },
  });
}

// --- AI config -------------------------------------------------------------

export function useAiConfig() {
  const keys = useKeys();
  return useQuery<AiConfig>({
    queryKey: keys.agent.config(),
    queryFn: () => mockResolve(MOCK_AI_CONFIG),
  });
}

export function useModels() {
  const keys = useKeys();
  return useQuery({
    queryKey: keys.agent.models(),
    queryFn: () => mockResolve(MOCK_MODELS),
    // The model catalogue changes on a provider release, not on a page view.
    staleTime: 10 * 60_000,
  });
}

export function useSaveAiConfig() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (config: AiConfig) => {
      await mockResolve(null, 700);
      Object.assign(MOCK_AI_CONFIG, config, { version: config.version + 1 });
      return MOCK_AI_CONFIG;
    },
    // Publishing an assistant configuration is NOT optimistic: 06 §11.3 lists
    // it explicitly, because a publish runs an eval suite that can block it.
    onSuccess: (config) => {
      qc.setQueryData(keys.agent.config(), config);
      toast({
        title: "Assistant configuration saved",
        description: "New conversations will use it immediately. Calls in progress finish on the previous version.",
        variant: "success",
      });
    },
    onError: () => toast({ title: "Could not save the configuration", variant: "destructive" }),
  });
}

// --- Knowledge -------------------------------------------------------------

export function useKnowledgeSources() {
  const keys = useKeys();
  return useQuery<KnowledgeSource[]>({
    queryKey: keys.knowledge.sources(),
    queryFn: () => mockResolve(MOCK_KNOWLEDGE),
  });
}

// --- Channels --------------------------------------------------------------

export function useChannels() {
  const keys = useKeys();
  return useQuery<ChannelConfig[]>({
    queryKey: keys.channels.all(),
    queryFn: () => mockResolve(MOCK_CHANNELS),
  });
}

// --- Members ---------------------------------------------------------------

export function useMembers() {
  const keys = useKeys();
  return useQuery<Member[]>({
    queryKey: keys.members.all(),
    queryFn: () => mockResolve(MOCK_MEMBERS),
  });
}

export function useSaveMember() {
  const keys = useKeys();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (member: Partial<Member> & { email: string; role: Member["role"] }) => {
      await mockResolve(null, 500);
      if (member.id) {
        const idx = MOCK_MEMBERS.findIndex((m) => m.id === member.id);
        if (idx >= 0) MOCK_MEMBERS[idx] = { ...MOCK_MEMBERS[idx]!, ...member };
      } else {
        MOCK_MEMBERS.push({
          id: `mem_${MOCK_MEMBERS.length + 1}`,
          user_id: `u_${MOCK_MEMBERS.length + 1}`,
          display_name: member.display_name ?? member.email.split("@")[0]!,
          status: "invited",
          last_active_at: null,
          invited_at: new Date().toISOString(),
          ...member,
        } as Member);
      }
      return member;
    },
    onSuccess: (m) => {
      qc.invalidateQueries({ queryKey: keys.members.all() });
      toast({
        title: m.id ? "Member updated" : "Invitation sent",
        description: m.id ? undefined : `${m.email} will receive an email with a link to join.`,
        variant: "success",
      });
    },
    onError: () => toast({ title: "Could not save the member", variant: "destructive" }),
  });
}

// --- Audit -----------------------------------------------------------------

export function useAudit(filters: { q?: string } = {}) {
  const keys = useKeys();
  return useQuery<AuditEntry[]>({
    queryKey: keys.audit.list(filters),
    queryFn: () => {
      const q = filters.q?.trim().toLowerCase();
      const filtered = q
        ? MOCK_AUDIT.filter((a) =>
            `${a.actor_name} ${a.action} ${a.target_label}`.toLowerCase().includes(q),
          )
        : MOCK_AUDIT;
      return mockResolve(filtered);
    },
    placeholderData: (prev) => prev,
  });
}
