from typing import Any
import httpx

class OdooAPIError(RuntimeError):
    pass

class OdooClient:
    def __init__(self, *, base_url: str, database: str, api_key: str, timeout_seconds: float = 30):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @property
    def headers(self):
        return {
            "Authorization": f"bearer {self.api_key}",
            "X-Odoo-Database": self.database,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "claude-odoo-mcp-readonly/1.0",
        }

    async def call(self, model: str, method: str, arguments: dict[str, Any] | None = None):
        url = f"{self.base_url}/json/2/{model}/{method}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.post(url, headers=self.headers, json=arguments or {})
        except httpx.TimeoutException as exc:
            raise OdooAPIError("Odoo request timed out.") from exc
        except httpx.RequestError as exc:
            raise OdooAPIError(f"Unable to reach Odoo: {exc}") from exc
        if response.status_code in {301, 302, 307, 308}:
            raise OdooAPIError("Odoo redirected the request. Verify ODOO_URL.")
        if response.is_error:
            raise OdooAPIError(f"Odoo HTTP {response.status_code}: {response.text[:1000]}")
        try:
            return response.json()
        except ValueError as exc:
            raise OdooAPIError("Odoo returned non-JSON content.") from exc

    async def search_read(self, *, model: str, domain: list, fields: list[str], limit: int, offset: int = 0, order: str | None = None):
        body = {"domain": domain, "fields": fields, "limit": limit, "offset": offset}
        if order:
            body["order"] = order
        result = await self.call(model, "search_read", body)
        if not isinstance(result, list):
            raise OdooAPIError(f"Unexpected response from {model}.search_read.")
        return result

    async def read(self, *, model: str, record_ids: list[int], fields: list[str]):
        result = await self.call(model, "read", {"ids": record_ids, "fields": fields})
        if not isinstance(result, list):
            raise OdooAPIError(f"Unexpected response from {model}.read.")
        return result
