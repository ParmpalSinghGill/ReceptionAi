"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { setAccessToken } from "@/lib/auth/token-store";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  // Prefilled with the seeded demo credentials. Safe because they only exist
  // in a locally seeded database — `tools/seed_demo.py` creates this user and
  // nothing else does, so the pair is worthless against any real deployment.
  // Left in the field rather than printed in a hint below it: a password you
  // have to copy from help text is a password you mistype.
  const [email, setEmail] = useState("demo@sunrisedental.in");
  const [password, setPassword] = useState("demo1234");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  /**
   * Redirect-to-intended-destination.
   *
   * The `next` param is validated to be a PATH on this origin. Without that
   * check `?next=https://evil.example` turns the login page into an open
   * redirect that phishers can point at from a real-looking link.
   */
  const rawNext = params.get("next");
  const next = rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//") ? rawNext : "/";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });
      if (!res.ok) {
        const problem = (await res.json().catch(() => ({}))) as { title?: string; detail?: string };
        // Deliberately does not distinguish "no such account" from "wrong
        // password" — that difference is an account-enumeration oracle.
        setError(problem.detail ?? problem.title ?? "That email and password did not match.");
        return;
      }
      const body = (await res.json()) as { access_token: string; expires_in: number };
      setAccessToken(body.access_token, body.expires_in);
      router.push(next);
    } catch {
      setError("Could not reach the server. Check your connection and try again.");
    } finally {
      setPending(false);
    }
  };

  return (
    <Card>
      <CardContent className="p-6 pt-6">
        <h1 className="font-display text-3xl text-fg">Sign in</h1>
        <p className="mt-1 text-sm text-fg-muted">to the ReceptionAi admin portal</p>

        <form onSubmit={submit} noValidate className="mt-6 space-y-4">
          {error && (
            // role="alert" so it is announced the moment it appears — the user
            // just pressed the button and is waiting for exactly this answer.
            <div
              role="alert"
              className="rounded-input border border-danger/40 bg-danger-dim p-3 text-sm text-danger-fg"
            >
              {error}
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="email">Email address</Label>
            <Input
              id="email"
              type="email"
              name="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-baseline justify-between">
              <Label htmlFor="password">Password</Label>
              <a href="/forgot" className="text-xs text-accent underline underline-offset-2">
                Forgot password?
              </a>
            </div>
            {/*
              3.3.8 Accessible Authentication: paste is ALLOWED (password
              managers), and there is no cognitive-function test anywhere in
              this path — no puzzle, no "type the third character".
            */}
            <Input
              id="password"
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          <Button type="submit" variant="primary" size="lg" dot className="w-full" disabled={pending}>
            {pending ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-6 border-t border-line pt-4 text-xs text-fg-subtle">
          This build runs in demo mode against the seeded Sunrise Dental workspace — the portal
          opens without a password. Sign-in is wired to the real endpoint and will be used as soon
          as an API origin is configured.
        </p>
        <Button
          variant="secondary"
          className="mt-3 w-full"
          onClick={() => router.push(next)}
        >
          Continue to the demo workspace
        </Button>
      </CardContent>
    </Card>
  );
}

export default function LoginPage() {
  // useSearchParams needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
