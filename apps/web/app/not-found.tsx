import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh items-center justify-center px-4 py-12">
      <div className="w-full max-w-md space-y-4 rounded-card border border-line bg-bg-raised p-6">
        <h1 className="font-display text-page-title text-fg">Page not found</h1>
        <p className="text-sm text-fg-muted">
          That link does not lead anywhere in the portal. It may have been moved, or the address
          may be mistyped.
        </p>
        <Button asChild variant="primary">
          <Link href="/">Back to the dashboard</Link>
        </Button>
      </div>
    </div>
  );
}
