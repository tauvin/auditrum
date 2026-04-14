
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PgAuditSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    audit_table: str = Field("auditlog", alias="AUDIT_TABLE")

    pg_host: str | None = Field(None, alias="PGHOST")
    pg_port: int = Field(5432, alias="PGPORT")
    pg_user: str | None = Field(None, alias="PGUSER")
    pg_password: str | None = Field(None, alias="PGPASSWORD")
    pg_dbname: str | None = Field(None, alias="PGDATABASE")

    @property
    def db_dsn(self) -> str | None:
        if not all([self.pg_host, self.pg_user, self.pg_password, self.pg_dbname]):
            return None
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_dbname}"
        )
