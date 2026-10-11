import React from "react";
import HealthStatus from "./components/HealthStatus";
import { ShieldCheck, Clock, FileCheck, Layers } from "lucide-react";

export default function HomePage() {
  return (
    <main className="flex-1 flex flex-col items-center justify-between p-6 sm:p-12 max-w-5xl mx-auto w-full">
      {/* Navigation / Header */}
      <header className="w-full flex items-center justify-between py-4 border-b border-slate-800/60 mb-12">
        <div className="flex items-center space-x-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-sky-600 to-cyan-400 flex items-center justify-center font-bold text-white shadow-md shadow-sky-500/20">
            S
          </div>
          <span className="text-lg font-bold tracking-tight text-white">Sendrift</span>
        </div>

        <div className="flex items-center space-x-3 text-xs">
          <span className="px-2.5 py-1 rounded-full bg-slate-900 border border-slate-800 text-slate-400">
            v0.1.0-alpha
          </span>
          <span className="px-2.5 py-1 rounded-full bg-sky-950 text-sky-400 border border-sky-800/40 font-medium">
            Milestone 1
          </span>
        </div>
      </header>

      {/* Main Hero & Architecture Status */}
      <div className="flex-1 flex flex-col items-center justify-center text-center w-full max-w-2xl my-auto">
        <div className="inline-flex items-center space-x-2 px-3 py-1 rounded-full bg-slate-900/90 border border-slate-800 text-xs text-slate-400 mb-6">
          <span className="w-2 h-2 rounded-full bg-sky-400 animate-pulse"></span>
          <span>Foundation &amp; Monorepo Initialization</span>
        </div>

        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight text-white mb-4">
          Share files. <span className="text-transparent bg-clip-text bg-gradient-to-r from-sky-400 to-cyan-300">Leave no clutter.</span>
        </h1>

        <p className="text-slate-400 text-sm sm:text-base mb-10 max-w-xl leading-relaxed">
          Minimalist temporary file sharing. One file, predictable TTL, cryptographic privacy, and scheduled background cleanup.
        </p>

        {/* Live Health Check Card */}
        <HealthStatus />

        {/* Architecture Spec Highlights */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 w-full max-w-xl mt-8">
          <div className="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60 text-left">
            <FileCheck className="w-4 h-4 text-sky-400 mb-1.5" />
            <h4 className="text-xs font-semibold text-slate-200">1 File</h4>
            <p className="text-[11px] text-slate-500">1 Byte to 100 MiB</p>
          </div>
          <div className="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60 text-left">
            <Clock className="w-4 h-4 text-sky-400 mb-1.5" />
            <h4 className="text-xs font-semibold text-slate-200">Preset TTL</h4>
            <p className="text-[11px] text-slate-500">1h, 24h, or 7 days</p>
          </div>
          <div className="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60 text-left">
            <ShieldCheck className="w-4 h-4 text-sky-400 mb-1.5" />
            <h4 className="text-xs font-semibold text-slate-200">Private Links</h4>
            <p className="text-[11px] text-slate-500">&ge;256-bit hashed token</p>
          </div>
          <div className="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60 text-left">
            <Layers className="w-4 h-4 text-sky-400 mb-1.5" />
            <h4 className="text-xs font-semibold text-slate-200">Zero Accounts</h4>
            <p className="text-[11px] text-slate-500">Pure ephemeral transfer</p>
          </div>
        </div>
      </div>

      {/* Footer */}
      <footer className="w-full py-6 mt-12 border-t border-slate-900 flex flex-col sm:flex-row items-center justify-between text-xs text-slate-500 gap-2">
        <p>Sendrift Modular Monolith — Next.js, FastAPI, PostgreSQL, MinIO</p>
        <p className="text-slate-600">Built according to ARCHITECTURE.md &amp; AGENTS.md</p>
      </footer>
    </main>
  );
}
