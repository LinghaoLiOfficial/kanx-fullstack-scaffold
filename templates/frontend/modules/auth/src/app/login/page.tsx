import Link from "next/link";
import { AuthForm } from "@/components/auth-form";

export default function LoginPage() {
  return <div className="stack"><h1>Sign in</h1><AuthForm mode="login" /><div className="actions"><Link href="/register">Create account</Link><Link href="/forgot-password">Forgot password</Link></div></div>;
}
