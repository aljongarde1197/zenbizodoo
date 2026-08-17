import asyncio
from app.config import Settings
from app.odoo_client import OdooClient, OdooAPIError

async def main():
    s = Settings.from_env()
    c = OdooClient(base_url=s.odoo_url, database=s.odoo_database, api_key=s.odoo_api_key, timeout_seconds=s.request_timeout_seconds)
    print("Connecting to Odoo JSON-2 API...")
    try:
        rows = await c.search_read(model="crm.lead", domain=[], fields=["id","name","create_date"], limit=5, order="create_date desc")
    except OdooAPIError as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
    print("Authenticated successfully.")
    for row in rows:
        print(f"[{row.get('id')}] {row.get('name')}")

if __name__ == "__main__":
    asyncio.run(main())
