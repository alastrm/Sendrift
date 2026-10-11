from app.core.logging import redact_url_path


def test_redact_url_path_masks_share_tokens():
    raw_path = "/s/abc123xyzSecretTokenValue"
    assert redact_url_path(raw_path) == "/s/***"


def test_redact_url_path_masks_api_shares_path():
    raw_path = "/api/v1/shares/super_secret_share_token_999/download"
    masked = redact_url_path(raw_path)
    assert "super_secret_share_token_999" not in masked
    assert masked == "/api/v1/shares/***/download"


def test_redact_url_path_preserves_unrelated_paths():
    assert redact_url_path("/health/live") == "/health/live"
    assert redact_url_path("/api/v1/health") == "/api/v1/health"
    assert redact_url_path("/") == "/"
