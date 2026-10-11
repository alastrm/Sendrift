from app.core.config import Settings


def test_settings_default_values():
    settings = Settings()
    assert settings.APP_NAME == "Sendrift API"
    assert settings.MAX_FILE_SIZE_BYTES == 104857600
    assert "http://localhost:3000" in settings.CORS_ORIGINS


def test_cors_origins_json_parsing():
    settings = Settings(CORS_ORIGINS='["http://example.com", "https://sendrift.app"]')
    assert settings.CORS_ORIGINS == ["http://example.com", "https://sendrift.app"]


def test_cors_origins_comma_separated():
    settings = Settings(CORS_ORIGINS="http://foo.com, https://bar.com")
    assert settings.CORS_ORIGINS == ["http://foo.com", "https://bar.com"]


def test_database_urls():
    settings = Settings(
        POSTGRES_USER="testuser",
        POSTGRES_PASSWORD="testpassword",
        POSTGRES_HOST="dbhost",
        POSTGRES_PORT=5432,
        POSTGRES_DB="testdb",
        DATABASE_URL=None,
        DATABASE_SYNC_URL=None,
    )
    assert settings.async_database_url == "postgresql+asyncpg://testuser:testpassword@dbhost:5432/testdb"
    assert settings.sync_database_url == "postgresql://testuser:testpassword@dbhost:5432/testdb"
