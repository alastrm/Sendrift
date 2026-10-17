export interface ShareCreateResponse {
  share_url: string;
  manage_token: string;
  filename: string;
  size_bytes: number;
  expires_at: string;
}

export interface ShareMetadataResponse {
  filename: string;
  size_bytes: number;
  expires_at: string;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "byte" : "bytes"}`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

export function formatExpiration(isoString: string): { formatted: string; relative: string } {
  try {
    const date = new Date(isoString);
    if (isNaN(date.getTime())) {
      return { formatted: isoString, relative: "" };
    }
    const now = new Date();
    const diffMs = date.getTime() - now.getTime();

    const formatted = date.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    });

    if (diffMs <= 0) {
      return { formatted, relative: "expired" };
    }

    const diffMinutes = Math.round(diffMs / (1000 * 60));
    if (diffMinutes < 60) {
      return {
        formatted,
        relative: `in ${diffMinutes} ${diffMinutes === 1 ? "minute" : "minutes"}`,
      };
    }

    const diffHours = Math.round(diffMs / (1000 * 60 * 60));
    if (diffHours < 48) {
      return {
        formatted,
        relative: `in ${diffHours} ${diffHours === 1 ? "hour" : "hours"}`,
      };
    }

    const diffDays = Math.round(diffHours / 24);
    return {
      formatted,
      relative: `in ${diffDays} ${diffDays === 1 ? "day" : "days"}`,
    };
  } catch {
    return { formatted: isoString, relative: "" };
  }
}

export function getApiUrl(): string {
  if (typeof window !== "undefined") {
    if (process.env.NEXT_PUBLIC_API_URL) {
      return process.env.NEXT_PUBLIC_API_URL.replace(/\/+$/, "");
    }
    return "";
  }
  return (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/+$/, "");
}
