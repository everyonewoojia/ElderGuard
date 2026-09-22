from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    PROJECT_NAME: str = "银发守护"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    
    CORS_ORIGINS: List[str] = ["*"]
    
    DATABASE_URL: str = "sqlite:///./elderguard.db"
    
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7
    
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
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
