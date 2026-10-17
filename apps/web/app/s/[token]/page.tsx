"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Clock,
  Download,
  FileText,
  Key,
  Loader2,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import BrandMark from "../../components/BrandMark";
import {
  formatFileSize,
  formatExpiration,
  getApiUrl,
  type ShareMetadataResponse,
} from "../../lib/format";

type PageState = "loading" | "ready" | "not_found" | "error" | "revoked";

export default function DownloadPage() {
  const params = useParams();
  const token = typeof params.token === "string" ? params.token : "";

  const [state, setState] = useState<PageState>("loading");
  const [metadata, setMetadata] = useState<ShareMetadataResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  // Revocation states
  const [showRevokeInput, setShowRevokeInput] = useState(false);
  const [manageTokenInput, setManageTokenInput] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [isRevoking, setIsRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState("");

  const apiUrl = getApiUrl();

  const fetchMetadata = useCallback(async () => {
    if (!token) {
      setState("not_found");
      return;
    }

    setState("loading");
    setErrorMessage("");

    try {
      const res = await fetch(`${apiUrl}/api/v1/shares/${encodeURIComponent(token)}`, {
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });

      if (res.status === 200) {
        const data: ShareMetadataResponse = await res.json();
        setMetadata(data);
        setState("ready");
      } else if (res.status === 404) {
        setState("not_found");
      } else if (res.status === 503) {
        setErrorMessage("Storage service is temporarily unavailable. Please try again shortly.");
        setState("error");
      } else {
        setErrorMessage("An unexpected error occurred while looking up this file.");
        setState("error");
      }
    } catch {
      setErrorMessage("Network error: Could not reach Sendrift server.");
      setState("error");
    }
  }, [apiUrl, token]);

  useEffect(() => {
    fetchMetadata();
  }, [fetchMetadata]);

  async function handleRevokeSubmit() {
    if (!token || !manageTokenInput.trim()) return;

    setIsRevoking(true);
    setRevokeError("");

    try {
      const res = await fetch(`${apiUrl}/api/v1/shares/${encodeURIComponent(token)}`, {
        method: "DELETE",
        headers: {
          "X-Manage-Token": manageTokenInput.trim(),
        },
        referrerPolicy: "no-referrer",
      });

      if (res.status === 202) {
        setState("revoked");
      } else if (res.status === 404) {
        setRevokeError("Invalid management key or file has already expired.");
      } else {
        setRevokeError("Failed to delete share. Please try again.");
      }
    } catch {
      setRevokeError("Network error while revoking share.");
    } finally {
      setIsRevoking(false);
    }
  }

  const downloadUrl = token ? `${apiUrl}/api/v1/shares/${encodeURIComponent(token)}/download` : "#";

  return (
    <main className="sendrift-home">
      <div className="application">
        <header className="application-header">
          <Link href="/" className="wordmark" aria-label="Sendrift home">
            <BrandMark />
            <span>Sendrift</span>
          </Link>
        </header>

        <div className="application-content">
          <section className="product-intro" aria-labelledby="download-heading">
            <p className="product-eyebrow">Direct file transfer</p>
            <h1 id="download-heading">
              Download file<br /> <span>from Sendrift.</span>
            </h1>
            <p className="product-description">
              Files on Sendrift are stored temporarily in private storage and are permanently deleted once their retention period expires.
            </p>
          </section>

          <section className="share-panel" aria-labelledby="file-status-heading">
            {state === "loading" && (
              <div className="download-state-box">
                <Loader2 size={32} className="spin-icon download-state-icon" aria-hidden="true" />
                <h2 id="file-status-heading" className="download-state-title">Checking file details...</h2>
                <p className="download-state-desc">Retrieving metadata and verifying expiration status.</p>
              </div>
            )}

            {state === "not_found" && (
              <div className="download-state-box">
                <div className="download-state-icon not-found">
                  <AlertCircle size={28} aria-hidden="true" />
                </div>
                <h2 id="file-status-heading" className="download-state-title">File not available</h2>
                <p className="download-state-desc">
                  This share link has expired, was revoked by the sender, or never existed. Files are automatically erased upon expiration.
                </p>
                <div style={{ marginTop: 22, width: "100%" }}>
                  <Link href="/" className="file-picker-button" style={{ width: "100%", textAlign: "center" }}>
                    <ArrowLeft size={15} style={{ marginRight: 6, display: "inline" }} />
                    Upload your own file
                  </Link>
                </div>
              </div>
            )}

            {state === "error" && (
              <div className="download-state-box">
                <div className="download-state-icon error">
                  <ShieldAlert size={28} aria-hidden="true" />
                </div>
                <h2 id="file-status-heading" className="download-state-title">Service error</h2>
                <p className="download-state-desc">{errorMessage || "Failed to retrieve file details."}</p>
                <div style={{ marginTop: 22, width: "100%", display: "flex", gap: 10 }}>
                  <button type="button" className="file-picker-button" style={{ flex: 1 }} onClick={fetchMetadata}>
                    Try again
                  </button>
                  <Link href="/" className="secondary-button" style={{ flex: 1, textAlign: "center" }}>
                    Go to home
                  </Link>
                </div>
              </div>
            )}

            {state === "revoked" && (
              <div className="download-state-box">
                <div className="download-state-icon success">
                  <Check size={28} aria-hidden="true" />
                </div>
                <h2 id="file-status-heading" className="download-state-title">File deleted</h2>
                <p className="download-state-desc">
                  This file was successfully revoked and scheduled for permanent deletion. The download link is now permanently inactive.
                </p>
                <div style={{ marginTop: 22, width: "100%" }}>
                  <Link href="/" className="file-picker-button" style={{ width: "100%", textAlign: "center" }}>
                    Upload a file
                  </Link>
                </div>
              </div>
            )}

            {state === "ready" && metadata && (
              <div className="download-ready-content">
                <div className="file-heading-row">
                  <h2 id="file-status-heading">File ready</h2>
                  <span className="file-count">Verified</span>
                </div>

                <div className="selected-file-card" style={{ marginTop: 16 }}>
                  <span className="selected-file-icon">
                    <FileText size={26} strokeWidth={1.5} aria-hidden="true" />
                  </span>
                  <div className="selected-file-details">
                    <p className="selected-file-name" title={metadata.filename}>
                      {metadata.filename}
                    </p>
                    <p>{formatFileSize(metadata.size_bytes)}</p>
                  </div>
                </div>

                {metadata.expires_at && (
                  <div className="expiration-summary">
                    <Clock size={14} aria-hidden="true" />
                    <span>
                      Expires {formatExpiration(metadata.expires_at).formatted} ({formatExpiration(metadata.expires_at).relative})
                    </span>
                  </div>
                )}

                <div className="download-actions" style={{ marginTop: 24 }}>
                  <a
                    href={downloadUrl}
                    download={metadata.filename}
                    className="upload-button is-active"
                    rel="noreferrer noopener"
                    style={{ textDecoration: "none" }}
                  >
                    <Download size={18} strokeWidth={2} aria-hidden="true" />
                    <span>Download file ({formatFileSize(metadata.size_bytes)})</span>
                  </a>
                  <p className="download-security-note">
                    Direct stream with strict security headers. No third-party scripts.
                  </p>
                </div>

                {/* Revoke section for uploader */}
                <div className="download-revoke-section">
                  {!showRevokeInput ? (
                    <button
                      type="button"
                      className="revoke-toggle-link"
                      onClick={() => setShowRevokeInput(true)}
                    >
                      <Key size={13} aria-hidden="true" />
                      <span>Are you the uploader? Revoke this file early</span>
                    </button>
                  ) : (
                    <div className="revoke-input-card">
                      <div className="revoke-input-header">
                        <h4>Revoke file access</h4>
                        <button
                          type="button"
                          className="close-revoke-btn"
                          aria-label="Close revocation form"
                          onClick={() => {
                            setShowRevokeInput(false);
                            setConfirmRevoke(false);
                            setRevokeError("");
                          }}
                        >
                          ✕
                        </button>
                      </div>
                      <p className="revoke-input-desc">
                        Enter the management key issued when you uploaded this file to immediately disable the link and schedule deletion.
                      </p>
                      <input
                        type="password"
                        placeholder="Paste management key"
                        value={manageTokenInput}
                        onChange={(e) => setManageTokenInput(e.target.value)}
                        className="copy-input"
                        style={{ width: "100%", marginBottom: 10, fontFamily: "monospace" }}
                      />

                      {!confirmRevoke ? (
                        <button
                          type="button"
                          className="subtle-danger-button"
                          disabled={!manageTokenInput.trim()}
                          onClick={() => setConfirmRevoke(true)}
                        >
                          <Trash2 size={14} aria-hidden="true" />
                          <span>Revoke and delete</span>
                        </button>
                      ) : (
                        <div className="confirm-delete-box" role="alert">
                          <p>Confirm immediate deletion? This cannot be undone.</p>
                          <div className="confirm-delete-buttons">
                            <button
                              type="button"
                              className="danger-button"
                              disabled={isRevoking}
                              onClick={handleRevokeSubmit}
                            >
                              {isRevoking && <Loader2 size={14} className="spin-icon" />}
                              <span>{isRevoking ? "Deleting..." : "Confirm delete"}</span>
                            </button>
                            <button
                              type="button"
                              className="secondary-button"
                              disabled={isRevoking}
                              onClick={() => setConfirmRevoke(false)}
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      )}

                      {revokeError && (
                        <p className="file-error" role="alert" style={{ marginTop: 8 }}>
                          <AlertCircle size={14} aria-hidden="true" />
                          {revokeError}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </section>
        </div>
      </div>
    </main>
  );
}
