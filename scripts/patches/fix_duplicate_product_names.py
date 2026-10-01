import os
import requests

# 1. Configuration matching your local Docker setup
ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "odoo")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")

def jsonrpc_call(service, method, args):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1
    }
    try:
        r = requests.post(f"{ODOO_URL}/jsonrpc", json=payload, timeout=30).json()
        if "error" in r:
            raise RuntimeError(r["error"])
        return r["result"]
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Could not connect to Odoo at {ODOO_URL}. Is your container running?")

# 2. Authenticate
uid = jsonrpc_call("common", "login", [ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD])
print(f"🔒 Authenticated successfully. User ID: {uid}")

# 3. Define target standalone products to create
new_standalone_products = [
    {"name": "Customizable Desk (Compact)", "default_code": "FURN_0096", "list_price": 750.00},
    {"name": "Customizable Desk (Standard)", "default_code": "FURN_0097", "list_price": 750.00},
    {"name": "Customizable Desk (Executive)", "default_code": "FURN_0098", "list_price": 800.40},
    {"name": "Conference Chair (Black)", "default_code": "E-COM12", "list_price": 33.00},
    {"name": "Conference Chair (Grey)", "default_code": "E-COM13", "list_price": 33.00},
]

# 4. Clean up old variant templates by archiving them to avoid search collisions
old_skus = ["FURN_0096", "FURN_0097", "FURN_0098", "E-COM12", "E-COM13"]
old_product_ids = jsonrpc_call("object", "execute_kw", [
    ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search",
    [[["default_code", "in", old_skus]]]
])

if old_product_ids:
    jsonrpc_call("object", "execute_kw", [
        ODOO_DB, uid, ODOO_PASSWORD, "product.template", "write",
        [old_product_ids, {"active": False}]
    ])
    print(f"📦 Archived {len(old_product_ids)} old variant template items to clear collisions.")

# 5. Insert clean, discrete production products
for prod in new_standalone_products:
    new_template_id = jsonrpc_call("object", "execute_kw", [
        ODOO_DB, uid, ODOO_PASSWORD, "product.template", "create",
        [{
            "name": prod["name"],
            "default_code": prod["default_code"],
            "list_price": prod["list_price"],
            "detailed_type": "product", # Storable product in modern Odoo
        }]
    ])
    print(f"🚀 Successfully created clean product: {prod['name']} (ID: {new_template_id})")

print("\n✨ Catalog data patch complete! Ready for n8n resync.")
