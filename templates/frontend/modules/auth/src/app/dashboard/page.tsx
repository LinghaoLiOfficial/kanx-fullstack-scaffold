"use client";

import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { apiRequest, signOut } from "@/lib/api/client";

export type UserContext = {
  user: { id: string; email: string; display_name: string; email_verified: boolean };
  organizations: Array<{ id: string; name: string; slug: string; permissions: string[] }>;
};

export default function DashboardPage() {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const context = useQuery({ queryKey: ["user-context"], queryFn: () => apiRequest<UserContext>("/users/me/context"), retry: false });

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await apiRequest("/auth/change-password", { method: "POST", body: {
        current_password: String(data.get("currentPassword")), new_password: String(data.get("newPassword")),
      }});
      setMessage("Password changed. Sign in again with the new password.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Request failed"); }
  }

  if (context.isPending) return <p>Restoring session...</p>;
  if (context.error) return <div className="panel stack"><p className="error">{context.error.message}</p><button className="button" onClick={() => router.push("/login")}>Sign in</button></div>;
  const value = context.data!;
  return <div className="stack">
    <div className="actions"><h1 style={{ marginRight: "auto" }}>Account</h1><button className="button secondary" onClick={async () => { await signOut(); router.push("/login"); }}>Sign out</button></div>
    <section className="panel"><strong>{value.user.display_name}</strong><p>{value.user.email}</p><p className="muted">Verified: {String(value.user.email_verified)}</p></section>
    <section className="stack"><h2>Organizations</h2>{value.organizations.map((organization) => <div className="panel" key={organization.id}><strong>{organization.name}</strong><p className="muted">{organization.permissions.join(", ")}</p></div>)}</section>
    <form className="panel stack" onSubmit={changePassword}><h2>Change password</h2><input className="input" name="currentPassword" type="password" placeholder="Current password" required /><input className="input" name="newPassword" type="password" minLength={12} placeholder="New password" required /><button className="button">Change password</button>{message ? <p>{message}</p> : null}</form>
  </div>;
}
