import type { Metadata } from "next";
import Link from "next/link";
import HealthStatus from "../components/HealthStatus";

export const metadata: Metadata = {
  title: "Service diagnostics — Sendrift",
  robots: { index: false, follow: false },
};

export default function HealthPage() {
  return (
    <main className="min-h-screen bg-slate-950 text-slate-100 p-6 sm:p-12 flex flex-col items-center justify-center gap-7">
      <h1 className="text-2xl font-semibold">Service diagnostics</h1>
      <HealthStatus />
      <Link href="/" className="text-sm text-slate-300 underline underline-offset-4 hover:text-white">Back to Sendrift</Link>
    </main>
  );
}
