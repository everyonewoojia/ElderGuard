from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
from pydantic import Field


class FamilyAuthConfigurationError(RuntimeError):
    """Reject unusable family signing configuration without exposing its value."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    PROJECT_NAME: str = "银发守护"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    
    CORS_ORIGINS: List[str] = ["*"]
    
    DATABASE_URL: str = "sqlite:///./elderguard.db"
    
    SECRET_KEY: str = Field(default="", repr=False)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    # Device credentials never fall back to the family signing key.
    EDGE_JWT_SECRET: str = Field(default="", repr=False)
    EDGE_JWT_ISSUER: str = "elderguard-edge"
    EDGE_JWT_AUDIENCE: str = "elderguard-edge-api"
    EDGE_JWT_EXPIRE_MINUTES: int = 60 * 24
    
    ALCLOUD_ACCESS_KEY_ID: str = ""
    ALCLOUD_ACCESS_KEY_SECRET: str = ""
    ALCLOUD_APP_KEY_ASR: str = ""
    ALCLOUD_APP_KEY_TTS: str = ""
    ALCLOUD_SMS_ACCESS_KEY: str = ""
    ALCLOUD_SMS_ACCESS_SECRET: str = ""
    ALCLOUD_SMS_SIGN_NAME: str = ""
    ALCLOUD_SMS_TEMPLATE_CODE: str = ""
    
    QWEN_API_KEY: str = ""
    QWEN_MODEL: str = "qwen-turbo"

    def family_signing_key(self) -> str:
        """Validate at startup and use, without import-time authentication effects."""
        key = self.SECRET_KEY
        if (not isinstance(key, str) or key != key.strip()
                or len(key.encode("utf-8")) < 32 or len(set(key)) < 16
                or key == "your-secret-key-change-in-production"
                or key == self.EDGE_JWT_SECRET):
            raise FamilyAuthConfigurationError("SECRET_KEY configuration invalid")
        return key


settings = Settings()
