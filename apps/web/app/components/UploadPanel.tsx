"use client";

import { useRef, useState, type DragEvent, type ChangeEvent } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock,
  Copy,
  FileText,
  FileUp,
  Info,
  Key,
  Link2,
  Loader2,
  Trash2,
  X,
} from "lucide-react";
import {
  formatFileSize,
  formatExpiration,
  getApiUrl,
  type ShareCreateResponse,
} from "../lib/format";

const MAX_FILE_BYTES = 100 * 1024 * 1024; // 100 MiB strict limit
const EXPIRATIONS = [
  { value: "1h", label: "1 hour" },
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
] as const;
type Expiration = (typeof EXPIRATIONS)[number]["value"];

type UploadStatus = "idle" | "uploading" | "processing" | "success";

export default function UploadPanel() {
  const [file, setFile] = useState<File | null>(null);
  const [expiration, setExpiration] = useState<Expiration>("24h");
  const [status, setStatus] = useState<UploadStatus>("idle");
  const [progress, setProgress] = useState<{ loaded: number; total: number; percent: number }>({
    loaded: 0,
    total: 0,
    percent: 0,
  });
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [successData, setSuccessData] = useState<ShareCreateResponse | null>(null);

  // Copy feedback states
  const [copiedLink, setCopiedLink] = useState(false);
  const [copiedKey, setCopiedKey] = useState(false);

  // Revocation state within success panel
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokedMessage, setRevokedMessage] = useState("");
  const [revokeError, setRevokeError] = useState("");

  const inputRef = useRef<HTMLInputElement>(null);
  const pickerRef = useRef<HTMLButtonElement>(null);
  const dragDepth = useRef(0);
  const xhrRef = useRef<XMLHttpRequest | null>(null);

  function selectFiles(files: FileList | File[]) {
    if (files.length !== 1) {
      setError("Select one file only.");
      return;
    }
    const selected = files[0];
    if (selected.size === 0) {
      setError("Empty files aren’t supported. Select a non-empty file.");
      return;
    }
    if (selected.size > MAX_FILE_BYTES) {
      setError("File exceeds the 100 MiB limit.");
      return;
    }
    setFile(selected);
    setError("");
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files?.length) {
      selectFiles(event.target.files);
    }
    event.target.value = "";
  }

  function handleDragEnter(event: DragEvent<HTMLDivElement>) {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    selectFiles(event.dataTransfer.files);
  }

  function removeFile() {
    setFile(null);
    setError("");
    pickerRef.current?.focus();
  }

  function cancelUpload() {
    if (xhrRef.current) {
      xhrRef.current.abort();
      xhrRef.current = null;
    }
    setStatus("idle");
    setProgress({ loaded: 0, total: 0, percent: 0 });
    setError("Upload cancelled.");
  }

  function resetForm() {
    setFile(null);
    setExpiration("24h");
    setStatus("idle");
    setProgress({ loaded: 0, total: 0, percent: 0 });
    setError("");
    setSuccessData(null);
    setCopiedLink(false);
    setCopiedKey(false);
    setConfirmRevoke(false);
    setRevoking(false);
    setRevokedMessage("");
    setRevokeError("");
    if (inputRef.current) inputRef.current.value = "";
  }

  async function handleCopy(text: string, type: "link" | "key") {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      if (type === "link") {
        setCopiedLink(true);
        setTimeout(() => setCopiedLink(false), 2000);
      } else {
        setCopiedKey(true);
        setTimeout(() => setCopiedKey(false), 2000);
      }
    } catch {
      setError("Failed to copy to clipboard.");
    }
  }

  function startUpload() {
    if (!file) return;

    setError("");
    setStatus("uploading");
    setProgress({ loaded: 0, total: file.size, percent: 0 });

    const apiUrl = getApiUrl();
    const formData = new FormData();
    formData.append("file", file);
    formData.append("ttl", expiration);

    const xhr = new XMLHttpRequest();
    xhrRef.current = xhr;

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
        setProgress({ loaded: event.loaded, total: event.total, percent });
        if (event.loaded >= event.total) {
          setStatus("processing");
        }
      }
    };

    xhr.onload = () => {
      xhrRef.current = null;
      if (xhr.status === 201) {
        try {
          const res: ShareCreateResponse = JSON.parse(xhr.responseText);
          setSuccessData(res);
          setStatus("success");
        } catch {
          setStatus("idle");
          setError("Invalid response format from server.");
        }
      } else {
        setStatus("idle");
        let errorDetail = "Upload failed. Please try again.";
        try {
          const errRes = JSON.parse(xhr.responseText);
          if (errRes.detail) {
            errorDetail = typeof errRes.detail === "string" ? errRes.detail : JSON.stringify(errRes.detail);
          }
        } catch {
          if (xhr.status === 413) {
            errorDetail = "File size exceeds the 100 MiB limit.";
          } else if (xhr.status === 503) {
            errorDetail = "Storage service temporarily unavailable. Please retry.";
          }
        }
        setError(errorDetail);
      }
    };

    xhr.onerror = () => {
      xhrRef.current = null;
      setStatus("idle");
      setError("Network error while connecting to upload service.");
    };

    xhr.onabort = () => {
      xhrRef.current = null;
      setStatus("idle");
    };

    xhr.open("POST", `${apiUrl}/api/v1/shares`);
    xhr.send(formData);
  }

  async function handleRevoke() {
    if (!successData) return;
    const shareToken = successData.share_url.split("/s/").pop();
    if (!shareToken) return;

    setRevoking(true);
    setRevokeError("");

    try {
      const apiUrl = getApiUrl();
      const res = await fetch(`${apiUrl}/api/v1/shares/${shareToken}`, {
        method: "DELETE",
        headers: {
          "X-Manage-Token": successData.manage_token,
        },
        referrerPolicy: "no-referrer",
      });

      if (res.status === 202) {
        setRevokedMessage("File has been deleted and the share link is permanently revoked.");
      } else {
        const errData = await res.json().catch(() => null);
        setRevokeError(errData?.detail || "Failed to revoke share.");
      }
    } catch {
      setRevokeError("Network error while revoking share.");
    } finally {
      setRevoking(false);
    }
  }

  // Render Success State
  if (status === "success" && successData) {
    const exp = formatExpiration(successData.expires_at);
    return (
      <section id="share-panel" className="share-panel" aria-labelledby="success-heading">
        <div className="file-heading-row">
          <h2 id="success-heading">Share link ready</h2>
          <span className="file-count">Ready to share</span>
        </div>

        <div className="success-banner" role="status">
          <CheckCircle2 size={18} aria-hidden="true" />
          <span>Upload complete. Your link has been generated.</span>
        </div>

        <div className="selected-file-card" style={{ marginTop: 14 }}>
          <span className="selected-file-icon">
            <FileText size={24} strokeWidth={1.5} aria-hidden="true" />
          </span>
          <div className="selected-file-details">
            <p className="selected-file-name" title={successData.filename}>
              {successData.filename}
            </p>
            <p>{formatFileSize(successData.size_bytes)}</p>
          </div>
        </div>

        <div className="expiration-summary">
          <Clock size={14} aria-hidden="true" />
          <span>Expires {exp.formatted} ({exp.relative})</span>
        </div>

        <div className="share-link-group">
          <label htmlFor="public-share-url" className="share-link-label">
            Public share link
          </label>
          <div className="copy-input-row">
            <input
              id="public-share-url"
              type="text"
              readOnly
              value={successData.share_url}
              className="copy-input"
              aria-label="Public share link"
              onFocus={(e) => e.target.select()}
            />
            <button
              type="button"
              className="copy-button"
              onClick={() => handleCopy(successData.share_url, "link")}
              aria-label="Copy public share link"
            >
              {copiedLink ? <Check size={15} /> : <Copy size={15} />}
              <span>{copiedLink ? "Copied" : "Copy"}</span>
            </button>
          </div>
          <p className="field-hint">Anyone with this link can view details and download the file.</p>
        </div>

        <div className="manage-token-card">
          <h3 className="manage-token-title">
            <Key size={14} aria-hidden="true" />
            Management key (save this now)
          </h3>
          <p className="manage-token-desc">
            This secret is shown <strong>only once</strong> and is required if you want to revoke or delete this file early before it expires.
          </p>
          <div className="copy-input-row">
            <code className="manage-token-code">{successData.manage_token}</code>
            <button
              type="button"
              className="secondary-button"
              style={{ minHeight: 34, padding: "6px 12px", fontSize: 12 }}
              onClick={() => handleCopy(successData.manage_token, "key")}
              aria-label="Copy management secret key"
            >
              {copiedKey ? <Check size={14} /> : <Copy size={14} />}
              <span>{copiedKey ? "Copied" : "Copy"}</span>
            </button>
          </div>
        </div>

        {revokedMessage ? (
          <div className="revocation-banner" role="status">
            <Check size={16} aria-hidden="true" />
            <span>{revokedMessage}</span>
          </div>
        ) : (
          <div className="management-actions">
            {!confirmRevoke ? (
              <button
                type="button"
                className="subtle-danger-button"
                onClick={() => setConfirmRevoke(true)}
              >
                <Trash2 size={14} aria-hidden="true" />
                <span>Delete file early</span>
              </button>
            ) : (
              <div className="confirm-delete-box" role="alert">
                <p>Are you sure? The share link will immediately stop working.</p>
                <div className="confirm-delete-buttons">
                  <button
                    type="button"
                    className="danger-button"
                    disabled={revoking}
                    onClick={handleRevoke}
                  >
                    {revoking && <Loader2 size={14} className="spin-icon" />}
                    <span>{revoking ? "Deleting..." : "Yes, delete now"}</span>
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={revoking}
                    onClick={() => setConfirmRevoke(false)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {revokeError && (
              <p className="file-error" role="alert" style={{ marginTop: 8 }}>
                <Info size={14} aria-hidden="true" />
                {revokeError}
              </p>
            )}
          </div>
        )}

        <div className="new-upload-action">
          <button type="button" className="file-picker-button" style={{ width: "100%" }} onClick={resetForm}>
            Share another file
          </button>
        </div>
      </section>
    );
  }

  // Render Uploading / File Selection State
  const isUploading = status === "uploading" || status === "processing";

  return (
    <section id="share-panel" className="share-panel" aria-labelledby="file-heading">
      <div className="file-column">
        <div className="file-heading-row">
          <h2 id="file-heading">Upload file</h2>
          <span className="file-count">{file ? "1 file selected" : "1 file max"}</span>
        </div>
        <div
          className={`drop-zone${dragging ? " is-dragging" : ""}${file ? " has-file" : ""}${isUploading ? " is-locked" : ""}`}
          role="group"
          aria-label="File selection"
          aria-describedby={error ? "file-error" : "file-limit"}
          onDragEnter={isUploading ? undefined : handleDragEnter}
          onDragLeave={
            isUploading
              ? undefined
              : (event) => {
                  event.preventDefault();
                  dragDepth.current = Math.max(0, dragDepth.current - 1);
                  if (dragDepth.current === 0) setDragging(false);
                }
          }
          onDragOver={
            isUploading
              ? undefined
              : (event) => {
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "copy";
                }
          }
          onDrop={isUploading ? undefined : handleDrop}
        >
          {file ? (
            <div className="selected-file-content">
              <div className="selected-file-card">
                <span className="selected-file-icon">
                  <FileText size={26} strokeWidth={1.5} aria-hidden="true" />
                </span>
                <div className="selected-file-details">
                  <p className="selected-file-name" title={file.name}>
                    {file.name}
                  </p>
                  <p>{formatFileSize(file.size)}</p>
                </div>
                {!isUploading && (
                  <button
                    type="button"
                    className="remove-file"
                    aria-label="Remove selected file"
                    onClick={removeFile}
                  >
                    <X size={16} aria-hidden="true" />
                  </button>
                )}
              </div>
              <p className="selected-file-note">
                {isUploading ? (status === "processing" ? "Processing on server..." : "Transmitting bytes...") : "Not uploaded"}
              </p>
            </div>
          ) : (
            <div className="empty-file-content">
              <span className="upload-icon">
                <FileUp size={25} strokeWidth={1.6} aria-hidden="true" />
              </span>
              <p className="drop-zone-title">
                {dragging ? "Release to select file" : "Drop a file here"}
              </p>
            </div>
          )}

          {!isUploading && (
            <button
              type="button"
              className="file-picker-button"
              ref={pickerRef}
              onClick={() => inputRef.current?.click()}
            >
              {file ? "Replace file" : "Choose file"}
            </button>
          )}

          <input
            ref={inputRef}
            type="file"
            className="sr-only"
            tabIndex={-1}
            disabled={isUploading}
            aria-label="Choose one file, up to 100 MiB"
            onChange={handleFileChange}
          />
          <p id="file-limit" className="file-limit">
            Maximum file size: 100 MiB
          </p>
        </div>

        {/* Real Progress Section */}
        {isUploading && (
          <div className="upload-progress-container" aria-live="polite">
            <div className="upload-progress-bar" role="progressbar" aria-valuenow={progress.percent} aria-valuemin={0} aria-valuemax={100}>
              <div
                className="upload-progress-fill"
                style={{ width: `${progress.percent}%` }}
              />
            </div>
            <div className="upload-progress-info">
              <span>
                {status === "processing"
                  ? "Finalizing and securing file..."
                  : `${formatFileSize(progress.loaded)} of ${formatFileSize(progress.total)} (${progress.percent}%)`}
              </span>
              <button
                type="button"
                className="cancel-upload-link"
                onClick={cancelUpload}
                aria-label="Cancel file upload"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="file-feedback">
          {error ? (
            <p id="file-error" className="file-error" role="alert">
              <AlertCircle size={14} aria-hidden="true" />
              {error}
            </p>
          ) : (
            <span className="sr-only" role="status">
              {file ? (isUploading ? "Uploading file..." : "File selected. Not uploaded.") : "No file selected."}
            </span>
          )}
        </div>
      </div>

      <div className="settings-column">
        <fieldset className="expiration-fieldset" disabled={isUploading}>
          <legend>Expires after</legend>
          <div className="expiration-options">
            {EXPIRATIONS.map((option) => (
              <label key={option.value} className="expiration-choice">
                <input
                  type="radio"
                  name="expiration"
                  value={option.value}
                  checked={expiration === option.value}
                  disabled={isUploading}
                  onChange={() => setExpiration(option.value)}
                  aria-label={option.label}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <p className="expiration-note">Starts when the upload finishes.</p>
        <div className="upload-action">
          <button
            type="button"
            className="upload-button"
            disabled={!file || isUploading}
            onClick={startUpload}
            aria-describedby={!file ? "upload-availability" : undefined}
          >
            {isUploading ? (
              <>
                <Loader2 size={17} className="spin-icon" aria-hidden="true" />
                <span>{status === "processing" ? "Finalizing..." : "Uploading..."}</span>
              </>
            ) : (
              <>
                <span>Create link</span>
                <Link2 size={17} strokeWidth={1.8} aria-hidden="true" />
              </>
            )}
          </button>
          {!file && <p id="upload-availability">Select a file to create a link.</p>}
        </div>
      </div>
    </section>
  );
}
