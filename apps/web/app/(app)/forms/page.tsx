"use client";

import { useState } from "react";

import { PageHeader, PageShell } from "@/components/PageHeader";
import { DynamicForm, type SubmitResult } from "@/components/forms/DynamicForm";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import type { FormValues } from "@/lib/forms/types";
import { RENDER_DOCUMENTS } from "@/lib/mock/render-documents";

const TABS = [
  {
    key: "patient_intake",
    label: "Medical",
    note: "The medical vertical's intake form: conditional clinical questions, a pain slider that appears only for a pain visit, and two fields marked sensitive.",
  },
  {
    key: "table_booking",
    label: "Restaurant",
    note: "A different vertical. A party-size slider, a seating combobox and a high-chair switch — none of which exist in the medical manifest. Zero files under apps/web changed to render it.",
  },
  {
    key: "post_visit_feedback",
    label: "Newer schema",
    note: "Compiled by a manifest schema NEWER than this build. It contains a widget (nps_wheel) and a predicate operator (resembles) that do not exist here. Nothing crashes and nothing is silently dropped.",
  },
] as const;

export default function FormsPage() {
  const [active, setActive] = useState<string>("patient_intake");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (values: FormValues): Promise<SubmitResult> => {
    setSubmitting(true);
    await new Promise((r) => setTimeout(r, 600));
    setSubmitting(false);

    // A deliberate server-side rejection, to exercise the path where the
    // server's field errors are mapped back onto the right controls — the
    // errors a client-side mirror can never produce on its own.
    if (typeof values.email === "string" && values.email.endsWith("@example.com")) {
      toast({
        title: "The server rejected one field",
        description: "Server-side rules run on submit regardless of what the client checked.",
        variant: "warning",
      });
      return {
        ok: false,
        fieldErrors: {
          email: ["We could not deliver to that address. Check it and try again."],
        },
      };
    }

    toast({
      title: "Submitted",
      description: `${Object.keys(values).length} fields sent. Hidden fields were excluded from the payload, not sent as null.`,
      variant: "success",
    });
    return { ok: true };
  };

  return (
    <PageShell>
      <PageHeader
        title="Dynamic forms"
        description="Every form in the portal is compiled server-side from a vertical's manifest and rendered by one component. A new vertical needs a YAML file, not a new screen."
      />

      <Tabs value={active} onValueChange={setActive}>
        <TabsList>
          {TABS.map((t) => (
            <TabsTrigger key={t.key} value={t.key}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        {TABS.map((t) => {
          const doc = RENDER_DOCUMENTS[t.key]!;
          return (
            <TabsContent key={t.key} value={t.key}>
              <div className="space-y-4">
                <p className="max-w-prose text-sm text-fg-muted">{t.note}</p>
                <p className="font-mono text-xs text-fg-subtle">
                  {doc.meta.vertical_key} · {doc.meta.manifest_version} ·{" "}
                  {doc.meta.resolved_hash}
                </p>

                <Card>
                  <CardContent className="p-6">
                    <DynamicForm
                      // Keying on the resolved hash remounts the form when the
                      // document genuinely changes, rather than trying to
                      // reconcile two unrelated field sets.
                      key={doc.meta.resolved_hash}
                      doc={doc}
                      onSubmit={handleSubmit}
                      submitting={submitting}
                    />
                  </CardContent>
                </Card>
              </div>
            </TabsContent>
          );
        })}
      </Tabs>
    </PageShell>
  );
}
