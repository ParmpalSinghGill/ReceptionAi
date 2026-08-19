import { QueryClient } from "@tanstack/react-query";

import { ApiProblem } from "@/lib/api/client";

export function makeQueryClient(_tenantId: string): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 30 seconds: long enough that navigating between screens does not
        // refetch everything, short enough that a colleague's change appears
        // without a manual reload.
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          if (error instanceof ApiProblem) {
            // Never retry a decided answer. Retrying a 403 cannot change it
            // and burns rate-limit budget; retrying a 409 on a booking would
            // race the same lost slot three more times.
            if (error.status < 500 && error.status !== 429) return false;
            if (error.status === 429) return failureCount < 2;
          }
          return failureCount < 3;
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000) * (0.5 + Math.random()),
      },
      mutations: {
        // Mutations are never retried automatically. Every unsafe route either
        // requires an Idempotency-Key or is not safely repeatable, and a
        // library-level retry would fire without one on the routes where it is
        // optional. Retries are the caller's explicit decision.
        retry: false,
      },
    },
  });
}
