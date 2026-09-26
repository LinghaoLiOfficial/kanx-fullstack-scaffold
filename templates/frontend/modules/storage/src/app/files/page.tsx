"use client";

import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import type { UserContext } from "@/app/dashboard/page";
import { apiRequest } from "@/lib/api/client";

type UploadedFile = { id: string; filename: string; status: string; size: number | null };

export default function FilesPage() {
  const [result, setResult] = useState<UploadedFile | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const context = useQuery({ queryKey: ["user-context"], queryFn: () => apiRequest<UserContext>("/users/me/context"), retry: false });
  const organization = context.data?.organizations[0];

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("file") as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || !organization) return;
    setMessage("Creating upload..."); setProgress(10);
    try {
      const created = await apiRequest<{ id: string; upload_url: string }>(`/organizations/${organization.id}/files/uploads`, { method: "POST", body: { filename: file.name, content_type: file.type || "application/octet-stream", size: file.size } });
      setMessage("Uploading to object storage..."); setProgress(40);
      const response = await fetch(created.upload_url, { method: "PUT", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
      if (!response.ok) throw new Error(`Object upload failed (${response.status})`);
      setProgress(80);
      await apiRequest(`/organizations/${organization.id}/files/${created.id}/complete`, { method: "POST" });
      const metadata = await apiRequest<UploadedFile>(`/organizations/${organization.id}/files/${created.id}`);
      setResult(metadata); setProgress(100); setMessage("Upload complete.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Upload failed"); }
  }

  async function download() {
    if (!organization || !result) return;
    const value = await apiRequest<{ download_url: string }>(`/organizations/${organization.id}/files/${result.id}/download`, { method: "POST" });
    window.location.assign(value.download_url);
  }

  if (context.isPending) return <p>Restoring session...</p>;
  if (!organization) return <div className="panel"><p className="error">Sign in to an organization before uploading files.</p></div>;
  return <div className="stack"><h1>Files</h1><form className="panel stack" onSubmit={upload}><input className="input" name="file" type="file" required /><button className="button">Upload</button><progress max={100} value={progress} style={{ width: "100%" }} />{message ? <p>{message}</p> : null}</form>{result ? <section className="panel"><h2>{result.filename}</h2><p>Status: {result.status}</p><button className="button secondary" onClick={download}>Download</button></section> : null}</div>;
}
