from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Literal

class Settings(BaseSettings):
    # Groq
    GROQ_API_KEY: str

    # Database - single URL used for both SQLDatabase tool queries and PostgresSaver checkpoint
    DATABASE_URL: str 

    # Models
    PRIMARY_MODEL: str = "openai/gpt-oss-120b"

    # Agent behaviour
    MAX_ITERATIONS: int = 10
    MESSAGE_WINDOW: int = 10    # number of recent messages kept in LLM context

    # UI
    APP_TITLE: str = "SQL Data Analyst"

    @field_validator("DATABASE_URL")
    @classmethod
    def ensure_psycopg_dialect(cls, v: str) -> str:
        """LangGraph PostgresSaver and SQLAlchemy both need psycopg3 dialect."""
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        return v
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    

settings = Settings()