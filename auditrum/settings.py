from pydantic import Field
from pydantic_settings import BaseSettings


class PgAuditSettings(BaseSettings):
    audit_table: str = Field("auditlog", alias="AUDIT_TABLE")

    pg_host: str = Field(..., alias="PGHOST")
    pg_port: int = Field(5432, alias="PGPORT")
    pg_user: str = Field(..., alias="PGUSER")
    pg_password: str = Field(..., alias="PGPASSWORD")
    pg_dbname: str = Field(..., alias="PGDATABASE")

    @property
    def db_dsn(self) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_dbname}"

    class Config:
        env_file = ".env"
        case_sensitive = False
