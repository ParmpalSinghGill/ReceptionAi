import { FlaskConical, Hammer } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { TenantSummary } from "@/lib/api/types";

/**
 * Production is deliberately UNLABELLED.
 *
 * If every workspace carries a badge, the badge stops being read, and the one
 * that matters — "you are about to change the live phone line" — is the one
 * that gets missed. Marking only the exceptions means the presence of a badge
 * is itself the signal.
 */
export function EnvironmentBadge({
  environment,
}: {
  environment: TenantSummary["environment"];
}) {
  if (environment === "production") return null;

  if (environment === "staging") {
    return (
      <Badge tone="warn" icon={<Hammer />}>
        Staging
      </Badge>
    );
  }
  return (
    <Badge tone="info" icon={<FlaskConical />}>
      Sandbox
    </Badge>
  );
}
