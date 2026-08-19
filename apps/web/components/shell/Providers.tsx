"use client";

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { Me, RoleKey, TenantSummary } from "@/lib/api/types";
import { makeQueryClient } from "@/lib/query/client";
import { installCacheGuard } from "@/lib/query/guard";

export interface TenantContextValue {
  tenantId: string;
  tenantName: string;
  plan: string;
  locale: string;
  timezone: string;
  currency: string;
  verticalKey: string;
  manifestVersion: string;
  environment: TenantSummary["environment"];
  permissions: ReadonlySet<string>;
  roles: readonly RoleKey[];
  aal: number;
  userId: string;
  /** Every tenant the principal can reach, for the switcher. */
  availableTenants: readonly TenantSummary[];
  switchTenant: (tenantId: string) => void;
}

const TenantContext = createContext<TenantContextValue | null>(null);

export interface SessionContextValue {
  me: Me;
  displayName: string;
  email: string;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function Providers({ session, children }: { session: Me; children: ReactNode }) {
  // The active tenant is client state because the switcher changes it without
  // a server round trip. It is seeded from the server-resolved session.
  const [activeTenantId, setActiveTenantId] = useState(session.active_tenant_id);

  /**
   * MECHANISM 1 (06 §11.2) — one QueryClient per TENANT, not per app.
   *
   * Switching tenants constructs a NEW client; the old cache is unreachable
   * and garbage-collected. Even a query key with no tenant prefix cannot leak,
   * because there is no shared store to leak through.
   *
   * `queryClient.clear()` would be the obvious alternative and it RACES: an
   * in-flight request started under tenant A can resolve after clear() and
   * write tenant A's data into tenant B's fresh cache. Constructing a new
   * client means the resolving promise writes into an orphaned cache nobody
   * reads. That is the entire reason this mechanism takes this specific form.
   */
  const clientRef = useRef<{
    tenantId: string;
    client: QueryClient;
    disposeGuard: () => void;
  } | null>(null);

  if (clientRef.current?.tenantId !== activeTenantId) {
    // Tear the previous guard down BEFORE the new client exists. The guard
    // closes over the tenant id it was installed with, so a lingering
    // subscription from tenant A would reject tenant B's perfectly correct
    // keys — a false positive that crashes the very screen the switch was
    // meant to show.
    clientRef.current?.disposeGuard();
    const client = makeQueryClient(activeTenantId);
    clientRef.current = {
      tenantId: activeTenantId,
      client,
      // MECHANISM 3 is installed HERE, during render, not in an effect.
      // Effects run after children have mounted and registered their queries,
      // so an effect-installed guard both misses the first batch of keys and
      // races the outgoing guard. Subscribing at construction means the guard
      // and the cache it watches always agree on which tenant is active.
      disposeGuard: installCacheGuard(client, activeTenantId),
    };
  }
  const client = clientRef.current.client;

  // Release the last guard when the shell itself unmounts.
  useEffect(() => () => clientRef.current?.disposeGuard(), []);

  const activeTenant = useMemo(() => {
    const found = session.tenants.find((t) => t.id === activeTenantId);
    if (!found) {
      // Deliberately not `?? session.tenants[0]`. Falling back to "some other
      // tenant" is exactly the failure this whole module exists to prevent:
      // the cache keys would be scoped to one workspace while the chrome
      // showed another. Throwing turns a data-exposure bug into a render
      // error the first test catches.
      throw new Error(
        `Active tenant ${activeTenantId} is not one this principal can access.`,
      );
    }
    return found;
  }, [session.tenants, activeTenantId]);

  const tenant = useMemo<TenantContextValue>(
    () => ({
      tenantId: activeTenant.id,
      tenantName: activeTenant.name,
      plan: activeTenant.plan,
      locale: activeTenant.locale,
      timezone: activeTenant.timezone,
      currency: activeTenant.currency,
      verticalKey: activeTenant.vertical_key,
      manifestVersion: activeTenant.manifest_version,
      environment: activeTenant.environment,
      permissions: new Set(session.permissions),
      roles: session.roles,
      aal: session.aal,
      userId: session.user_id,
      availableTenants: session.tenants,
      switchTenant: setActiveTenantId,
    }),
    [activeTenant, session],
  );

  const sessionValue = useMemo<SessionContextValue>(
    () => ({ me: session, displayName: session.display_name, email: session.email }),
    [session],
  );

  return (
    <QueryClientProvider client={client}>
      <SessionContext.Provider value={sessionValue}>
        <TenantContext.Provider value={tenant}>{children}</TenantContext.Provider>
      </SessionContext.Provider>
    </QueryClientProvider>
  );
}

export function useTenant(): TenantContextValue {
  const ctx = useContext(TenantContext);
  if (!ctx) {
    // Not a nicety. A component rendering outside the provider would fall back
    // to *some* default tenant, and that default would be wrong for somebody.
    // Throwing turns a data-leak class of bug into a render error caught by
    // the first test that mounts the component.
    throw new Error("useTenant must be used inside the authenticated shell");
  }
  return ctx;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside the authenticated shell");
  return ctx;
}
