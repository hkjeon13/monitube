from monitube_api.settings import Settings


def test_settings_normalizes_sqlalchemy_psycopg_url_and_fingerprints_key() -> None:
    settings = Settings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://user:pass@db:5432/monitube",
            "YOUTUBE_API_KEY": "server-key-value",
            "YOUTUBE_API_KEY_SECRET_REF": "env:prod/youtube-key",
        }
    )

    assert settings.database_url == "postgresql://user:pass@db:5432/monitube"
    assert settings.youtube_api_secret_ref == "env:prod/youtube-key"
    assert settings.key_fingerprint is not None
    assert "server-key-value" not in settings.key_fingerprint


def test_settings_accepts_a_same_project_failover_key_pool() -> None:
    settings = Settings.from_environment({"YOUTUBE_API_KEYS": "key-a, key-b, key-a", "YOUTUBE_API_KEY_ENCRYPTION_KEY": "master"})
    assert settings.youtube_api_keys == ("key-a", "key-b")
    assert settings.youtube_api_key == "key-a"
    assert settings.youtube_api_key_encryption_key == "master"
