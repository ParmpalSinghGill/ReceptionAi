import type { QueryClient } from "@tanstack/react-query";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * MECHANISM 3 (06 §11.2) — a development-only assertion that every cache
 * entry is tenant-scoped.
 *
 * Throwing in development is deliberate. A silent cross-tenant cache entry is
 * the kind of bug that ships, sits in production for six months, and is found
 * by a customer who saw another customer's booking list. Making it a hard
 * crash on the developer's machine, at the moment the query is registered, is
 * the only reliable time to catch it.
 *
 * It is a no-op in production: the cost of being wrong about a false positive
 * in front of a user is higher than the cost of missing one, and mechanisms 1
 * and 2 are structural rather than advisory.
 */
export function installCacheGuard(client: QueryClient, tenantId: string): () => void {
  if (process.env.NODE_ENV === "production") return () => {};

  return client.getQueryCache().subscribe((event) => {
    if (event.type !== "added") return;
    const [first] = event.query.queryKey as unknown[];

    if (typeof first !== "string" || !UUID.test(first)) {
      throw new Error(
        `Query key is not tenant-scoped: ${JSON.stringify(event.query.queryKey)}. ` +
          `Every key must start with the tenant id. Use useKeys() from lib/query/keys.`,
      );
    }
    if (first !== tenantId) {
      throw new Error(
        `CROSS-TENANT CACHE KEY: key is scoped to ${first}, active tenant is ${tenantId}. ` +
          `This query was constructed with a stale tenant in scope.`,
      );
    }
  });
}
