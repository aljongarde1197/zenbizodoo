from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _csv(name: str, default: str = "") -> list[str]:
    return [
        value.strip()
        for value in os.getenv(name, default).split(",")
        if value.strip()
    ]


@dataclass(frozen=True)
class Settings:
    odoo_url: str
    odoo_database: str
    odoo_api_key: str

    transport: str
    host: str
    port: int
    request_timeout_seconds: float
    max_results: int
    log_level: str

    auth_enabled: bool
    auth_issuer_url: str | None
    auth_resource_server_url: str | None
    auth_jwks_url: str | None
    auth_audience: str | None
    auth_required_scopes: list[str]
    auth_algorithms: list[str]

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("ODOO_URL", "ODOO_DATABASE", "ODOO_API_KEY")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
            )

        transport = os.getenv("TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "streamable-http"}:
            raise ConfigurationError(
                "TRANSPORT must be 'stdio' or 'streamable-http'."
            )

        port = int(os.getenv("PORT", "8000"))
        max_results = int(os.getenv("MAX_RESULTS", "50"))
        timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be between 1 and 65535.")
        if not 1 <= max_results <= 200:
            raise ConfigurationError("MAX_RESULTS must be between 1 and 200.")

        auth_enabled = (
            os.getenv("AUTH_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        issuer = os.getenv("AUTH_ISSUER_URL", "").strip() or None
        resource = os.getenv("AUTH_RESOURCE_SERVER_URL", "").strip() or None
        jwks = os.getenv("AUTH_JWKS_URL", "").strip() or None
        audience = os.getenv("AUTH_AUDIENCE", "").strip() or None
        scopes = _csv("AUTH_REQUIRED_SCOPES", "odoo.read")
        algorithms = _csv("AUTH_ALGORITHMS", "RS256")

        if auth_enabled:
            oauth_missing = []
            if not issuer:
                oauth_missing.append("AUTH_ISSUER_URL")
            if not resource:
                oauth_missing.append("AUTH_RESOURCE_SERVER_URL")
            if not jwks:
                oauth_missing.append("AUTH_JWKS_URL")
            if not audience:
                oauth_missing.append("AUTH_AUDIENCE")
            if oauth_missing:
                raise ConfigurationError(
                    "AUTH_ENABLED=true but missing: "
                    + ", ".join(oauth_missing)
                )
            if not issuer.startswith("https://"):
                raise ConfigurationError("AUTH_ISSUER_URL must use HTTPS.")
            if not resource.startswith("https://"):
                raise ConfigurationError(
                    "AUTH_RESOURCE_SERVER_URL must use HTTPS."
                )

        return cls(
            odoo_url=os.environ["ODOO_URL"].rstrip("/"),
            odoo_database=os.environ["ODOO_DATABASE"].strip(),
            odoo_api_key=os.environ["ODOO_API_KEY"].strip(),
            transport=transport,
            host=os.getenv("HOST", "127.0.0.1").strip(),
            port=port,
            request_timeout_seconds=timeout,
            max_results=max_results,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            auth_enabled=auth_enabled,
            auth_issuer_url=issuer,
            auth_resource_server_url=resource,
            auth_jwks_url=jwks,
            auth_audience=audience,
            auth_required_scopes=scopes,
            auth_algorithms=algorithms,
        )
