"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import { apiRequest, signIn } from "@/lib/api/client";

type Mode = "login" | "register" | "forgot" | "reset";

export function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setMessage(null);
    const data = new FormData(event.currentTarget);
    const email = String(data.get("email") ?? "");
    const password = String(data.get("password") ?? "");
    try {
      if (mode === "login") {
        await signIn(email, password);
        router.push("/dashboard");
      } else if (mode === "register") {
        await apiRequest("/auth/register", {
          method: "POST",
          authenticate: false,
          body: { email, password, display_name: String(data.get("displayName") ?? "") },
        });
        setMessage("Check Mailpit or your inbox for the verification link.");
      } else if (mode === "forgot") {
        await apiRequest("/auth/forgot-password", {
          method: "POST", authenticate: false, body: { email },
        });
        setMessage("If the account exists, a reset email has been queued.");
      } else {
        const token = new URLSearchParams(window.location.search).get("token") ?? "";
        await apiRequest("/auth/reset-password", {
          method: "POST", authenticate: false, body: { token, password },
        });
        setMessage("Password reset. You can now sign in.");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Request failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="panel stack" onSubmit={submit}>
      {mode === "register" ? <label className="field">Display name<input className="input" name="displayName" required /></label> : null}
      {mode !== "reset" ? <label className="field">Email<input className="input" name="email" type="email" required /></label> : null}
      {mode !== "forgot" ? <label className="field">Password<input className="input" name="password" type="password" minLength={12} required /></label> : null}
      <button className="button" disabled={pending}>{pending ? "Working..." : "Continue"}</button>
      {message ? <p role="status">{message}</p> : null}
    </form>
  );
}
