"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Activity, CheckCircle2, AlertCircle, RefreshCw, Server, Database, HardDrive } from "lucide-react";

interface HealthState {
  status: "ok" | "degraded" | "error";
  app?: string;
  version?: string;
  database?: "connected" | "disconnected";
  storage?: "connected" | "disconnected";
  timestamp?: string;
  latencyMs?: number;
  error?: string;
}

export default function HealthStatus() {
  const [data, setData] = useState<HealthState | null>(null);
  const [loading, setLoading] = useState(true);

  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  const checkHealth = useCallback(async () => {
    setLoading(true);
    const start = performance.now();
    try {
      // Test liveness probe
      const liveRes = await fetch(`${apiUrl}/health/live`, {
        cache: "no-store",
      });

      if (!liveRes.ok) {
        throw new Error(`API returned HTTP ${liveRes.status}`);
      }
      const liveData = await liveRes.json();

      // Test readiness probe
      let readinessData: { database?: "connected" | "disconnected"; storage?: "connected" | "disconnected" } = {};
      try {
        const readyRes = await fetch(`${apiUrl}/health/ready`, {
          cache: "no-store",
        });
        readinessData = await readyRes.json();
      } catch {
        // Readiness might fail if DB/storage are down, but API process is alive
      }

      const elapsed = Math.round(performance.now() - start);

      setData({
        status: liveData.status,
        app: liveData.app,
        version: liveData.version,
        database: readinessData.database || "disconnected",
        storage: readinessData.storage || "disconnected",
        timestamp: liveData.timestamp,
        latencyMs: elapsed,
      });
    } catch (err: unknown) {
      const elapsed = Math.round(performance.now() - start);
      const message = err instanceof Error ? err.message : "Failed to connect to API";
      setData({
        status: "error",
        error: message,
        latencyMs: elapsed,
      });
    } finally {
      setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    checkHealth();
  }, [checkHealth]);

  const isHealthy = data?.status === "ok";
  const isDegraded = data?.status === "degraded";

  return (
    <div className="w-full max-w-xl bg-slate-900/80 border border-slate-800 rounded-2xl p-6 backdrop-blur shadow-xl">
      <div className="flex items-center justify-between pb-4 border-b border-slate-800/80">
        <div className="flex items-center space-x-3">
          <div className="p-2 bg-sky-950/60 text-sky-400 rounded-lg border border-sky-800/50">
            <Activity className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-semibold text-slate-100 text-sm">System Connectivity</h3>
            <p className="text-xs text-slate-400">Target: <code className="text-slate-300 bg-slate-800 px-1 py-0.5 rounded">{apiUrl || "Same origin"}</code></p>
          </div>
        </div>

        <button
          onClick={checkHealth}
          disabled={loading}
          className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded-lg transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-4">
        {/* API Process */}
        <div className="p-3 bg-slate-950/60 rounded-xl border border-slate-800/60 flex flex-col justify-between">
          <div className="flex items-center justify-between text-xs text-slate-400 mb-2">
            <span className="flex items-center space-x-1.5">
              <Server className="w-3.5 h-3.5" />
              <span>FastAPI</span>
            </span>
            {loading ? (
              <span className="w-2 h-2 rounded-full bg-slate-600 animate-pulse" />
            ) : isHealthy ? (
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
            ) : (
              <span className="w-2 h-2 rounded-full bg-rose-400" />
            )}
          </div>
          <div className="text-sm font-medium">
            {loading ? (
              <span className="text-slate-500">Checking...</span>
            ) : data?.status === "ok" ? (
              <span className="text-emerald-400 flex items-center space-x-1">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Online</span>
              </span>
            ) : (
              <span className="text-rose-400 flex items-center space-x-1">
                <AlertCircle className="w-3.5 h-3.5" />
                <span>Offline</span>
              </span>
            )}
          </div>
          {data?.latencyMs !== undefined && (
            <span className="text-[10px] text-slate-500 mt-1">{data.latencyMs}ms ping</span>
          )}
        </div>

        {/* Database */}
        <div className="p-3 bg-slate-950/60 rounded-xl border border-slate-800/60 flex flex-col justify-between">
          <div className="flex items-center justify-between text-xs text-slate-400 mb-2">
            <span className="flex items-center space-x-1.5">
              <Database className="w-3.5 h-3.5" />
              <span>PostgreSQL</span>
            </span>
            {loading ? (
              <span className="w-2 h-2 rounded-full bg-slate-600 animate-pulse" />
            ) : data?.database === "connected" ? (
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
            ) : (
              <span className="w-2 h-2 rounded-full bg-amber-400" />
            )}
          </div>
          <div className="text-sm font-medium">
            {loading ? (
              <span className="text-slate-500">Checking...</span>
            ) : data?.database === "connected" ? (
              <span className="text-emerald-400 flex items-center space-x-1">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Ready</span>
              </span>
            ) : (
              <span className="text-amber-400 flex items-center space-x-1">
                <AlertCircle className="w-3.5 h-3.5" />
                <span>Standby</span>
              </span>
            )}
          </div>
          <span className="text-[10px] text-slate-500 mt-1">Metadata store</span>
        </div>

        {/* Object Storage */}
        <div className="p-3 bg-slate-950/60 rounded-xl border border-slate-800/60 flex flex-col justify-between">
          <div className="flex items-center justify-between text-xs text-slate-400 mb-2">
            <span className="flex items-center space-x-1.5">
              <HardDrive className="w-3.5 h-3.5" />
              <span>MinIO / S3</span>
            </span>
            {loading ? (
              <span className="w-2 h-2 rounded-full bg-slate-600 animate-pulse" />
            ) : data?.storage === "connected" ? (
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
            ) : (
              <span className="w-2 h-2 rounded-full bg-amber-400" />
            )}
          </div>
          <div className="text-sm font-medium">
            {loading ? (
              <span className="text-slate-500">Checking...</span>
            ) : data?.storage === "connected" ? (
              <span className="text-emerald-400 flex items-center space-x-1">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Ready</span>
              </span>
            ) : (
              <span className="text-amber-400 flex items-center space-x-1">
                <AlertCircle className="w-3.5 h-3.5" />
                <span>Standby</span>
              </span>
            )}
          </div>
          <span className="text-[10px] text-slate-500 mt-1">Private bucket</span>
        </div>
      </div>

      {data?.error && (
        <div className="mt-4 p-3 bg-rose-950/40 border border-rose-900/60 rounded-xl text-xs text-rose-300">
          <p className="font-medium">Connection diagnostic notice:</p>
          <p className="mt-0.5 text-rose-400 font-mono text-[11px]">{data.error}</p>
          <p className="mt-1 text-slate-400">Ensure the FastAPI service is running locally on port 8000 or via Docker Compose.</p>
        </div>
      )}

      {data?.timestamp && (
        <div className="mt-3 text-[11px] text-slate-500 flex justify-between items-center">
          <span>Version: {data.version || "0.1.0"}</span>
          <span>Checked at: {new Date(data.timestamp).toLocaleTimeString()}</span>
        </div>
      )}
    </div>
  );
}
