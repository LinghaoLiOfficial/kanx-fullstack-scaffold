"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiRequest } from "@/lib/api/client";

export default function VerifyEmailPage() {
  const [message, setMessage] = useState("Verifying email...");
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token") ?? "";
    apiRequest("/auth/verify-email", { method: "POST", authenticate: false, body: { token } })
      .then(() => setMessage("Email verified."))
      .catch((error: Error) => setMessage(error.message));
  }, []);
  return <div className="panel stack"><h1>Email verification</h1><p>{message}</p><Link href="/login">Sign in</Link></div>;
}
