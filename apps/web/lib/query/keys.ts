"use client";

import { useMemo } from "react";

import { useTenant } from "@/components/shell/Providers";

/**
 * MECHANISM 2 (06 §11.2) — tenant-scoped query key factory.
 *
 * Every key begins with the tenant id. This is belt-and-braces over the
 * per-tenant QueryClient (mechanism 1), and it is what makes a leak visible in
 * devtools: a key without a UUID prefix is immediately wrong to anyone looking
 * at the query list.
 *
 * The factory is a HOOK, not a module-level object, so a key literally cannot
 * be constructed without a tenant in scope. A plain exported
 * `keys.services.list()` would be callable from anywhere, including a module
 * that has no idea which tenant is active — and that is exactly the call site
 * that would produce a cross-tenant key.
 */
export function useKeys() {
  const { tenantId } = useTenant();

  return useMemo(
    () =>
      ({
        all: [tenantId] as const,

        session: () => [tenantId, "session"] as const,
        navigation: () => [tenantId, "navigation"] as const,

        dashboard: {
          kpis: () => [tenantId, "dashboard", "kpis"] as const,
          volume: () => [tenantId, "dashboard", "volume"] as const,
        },
        services: {
          all: () => [tenantId, "services"] as const,
          list: (filters: object = {}) => [tenantId, "services", "list", filters] as const,
          detail: (id: string) => [tenantId, "services", "detail", id] as const,
        },
        bookings: {
          all: () => [tenantId, "bookings"] as const,
          list: (filters: object = {}) => [tenantId, "bookings", "list", filters] as const,
          detail: (id: string) => [tenantId, "bookings", "detail", id] as const,
          /** One business day of the diary. `date` is `YYYY-MM-DD`. */
          day: (date: string, filters: object = {}) =>
            [tenantId, "bookings", "day", date, filters] as const,
        },
        resources: {
          all: () => [tenantId, "resources"] as const,
        },
        conversations: {
          all: () => [tenantId, "conversations"] as const,
          list: (filters: object = {}) => [tenantId, "conversations", "list", filters] as const,
          detail: (id: string) => [tenantId, "conversations", "detail", id] as const,
          turns: (id: string) => [tenantId, "conversations", id, "turns"] as const,
          live: () => [tenantId, "conversations", "live"] as const,
        },
        hours: {
          weekly: () => [tenantId, "hours", "weekly"] as const,
          holidays: () => [tenantId, "hours", "holidays"] as const,
        },
        agent: {
          config: () => [tenantId, "agent", "config"] as const,
          models: () => [tenantId, "agent", "models"] as const,
        },
        knowledge: {
          sources: () => [tenantId, "knowledge", "sources"] as const,
          job: (id: string) => [tenantId, "knowledge", "job", id] as const,
        },
        channels: {
          all: () => [tenantId, "channels"] as const,
        },
        members: {
          all: () => [tenantId, "members"] as const,
        },
        audit: {
          list: (filters: object = {}) => [tenantId, "audit", "list", filters] as const,
        },
        settings: {
          business: () => [tenantId, "settings", "business"] as const,
        },
        forms: {
          doc: (formKey: string, context: string, locale: string) =>
            [tenantId, "form", formKey, context, locale] as const,
          options: (endpoint: string, search: string) =>
            [tenantId, "form-options", endpoint, search] as const,
        },
        analytics: (endpoint: string, params: object = {}) =>
          [tenantId, "analytics", endpoint, params] as const,
      }) as const,
    [tenantId],
  );
}

export type Keys = ReturnType<typeof useKeys>;
