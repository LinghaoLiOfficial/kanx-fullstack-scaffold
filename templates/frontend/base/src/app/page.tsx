"use client";

import { useQuery } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import { env } from "@/lib/env";

export default function Home() {
  const health = useQuery({
    queryKey: ["backend-health"],
    queryFn: () => apiRequest<{ status: string }>("/health/live", { authenticate: false }),
    retry: false,
  });

  return (
    <div className="stack">
      <div>
        <h1>System status</h1>
        <p className="muted">Generated profile: <code>{{PROFILE}}</code></p>
      </div>
      <section className="panel stack">
        <h2>Backend connection</h2>
        {health.isPending ? <p>Checking {env.NEXT_PUBLIC_API_BASE_URL}...</p> : null}
        {health.data ? <p className="ok">Connected. API status: {health.data.status}</p> : null}
        {health.error ? <p className="error">{health.error.message}</p> : null}
        <button className="button secondary" onClick={() => health.refetch()}>Check again</button>
      </section>
      <section className="grid">
        <div className="panel"><strong>Frontend</strong><p className="muted">Next.js development server on port 3000.</p></div>
        <div className="panel"><strong>Backend</strong><p className="muted">FastAPI service on port 8000.</p></div>
      </section>
    </div>
  );
}
