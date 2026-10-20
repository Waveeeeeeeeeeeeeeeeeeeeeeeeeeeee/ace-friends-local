"""Exercise local Auth -> Dating -> Database, Mailpit, S3 and Qdrant without a UI.

Tokens and OTP codes stay in process memory and never enter the report.
"""
import asyncio
import hashlib
import base64
import json
import re
from pathlib import Path

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import seed as storage

ROOT = Path('/workspace')


async def main():
    credentials = json.loads((ROOT / ".private/credentials.json").read_text())
    settings = json.loads((ROOT / ".private/stack-settings.json").read_text())
    bundle = json.loads((ROOT / "data/manifest.json").read_text())
    report = {}
    origin = settings["admin_origin"]
    async with httpx.AsyncClient(base_url="http://gateway", timeout=30, trust_env=False,
                                 headers={"Origin": origin}) as client:
        async def request(method, path, **kwargs):
            result = await client.request(method, path, **kwargs)
            if result.status_code != 200:
                raise RuntimeError(f"Local API check failed: {path}, HTTP {result.status_code}")
            return result.json()
        admin = await request("POST", "/auth/v1/admin/login",
                              json={"email": "admin@ace.local", "password": credentials["admin_password"]})
        verified = await request("POST", "/auth/v1/jwt/verify",
                                 headers={"X-Access-Token": credentials["service_tokens"]["auth"]},
                                 json={"access_token": admin["access_token"], "token_version": admin["token_version"]})
        assert verified.get("user_id"), "Admin JWT did not identify its owner"
        await request("GET", "/database/v1/common/users/get_users_count",
                      headers={"X-Access-Token": admin["access_token"], "X-Token-Version": admin["token_version"]})
        report["admin_login_jwt_and_database"] = "passed"
        email = "language-scenario-1@example.com"
        await request("POST", "/auth/v1/email/send-otp", json={"email": email})
        async with httpx.AsyncClient(timeout=10, trust_env=False) as mail:
            messages = (await mail.get("http://mailpit:8025/api/v1/messages")).json()["messages"]
            message = next(m for m in messages if any(a.get("Address") == email for a in m.get("To", [])))
            body = (await mail.get("http://mailpit:8025/api/v1/message/" + message["ID"])).json()
            code = re.search(r"<strong>(\d{6})</strong>", body["HTML"]).group(1)
        user = await request("POST", "/auth/v1/email/verify-otp", json={"email": email, "otp": code})
        assert user["access_token"] and not user["is_new_user"], "Expected an existing fixture user"
        report["email_otp_and_mailpit"] = "passed"
        profile = await request("GET", "/dating/v1/profiles/me", params={"search_type": "JUST_PLAY"},
                                headers={"X-Access-Token": user["access_token"], "X-Token-Version": user["token_version"]})
        assert profile.get("name"), "Fixture profile has no display name"
        report["user_profile_through_auth_dating_database"] = "passed"
        async with websockets.connect("ws://gateway/dating/v1/profiles_search/ws", origin=origin, open_timeout=15) as ws:
            await asyncio.wait_for(ws.recv(), 20)
            await ws.send(json.dumps({"provided_access_token": user["access_token"], "provided_token_version": user["token_version"]}))
            auth = json.loads(await asyncio.wait_for(ws.recv(), 20))
            assert auth.get("result") == "SUCCEED", "Search websocket rejected user"
            await ws.send(json.dumps({"cmd": "find", "request_id": "stand-verification", "params":
                                     {"search_type": "JUST_PLAY", "country_codes": ["RU"], "limit": 10}}))
            result = None
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), 30)
                if raw == "searching profiles...":
                    continue
                result = json.loads(raw)
                break
            assert result and result.get("users"), "Search returned no fixture profiles"
            report["authenticated_search_profiles"] = len(result["users"])
    conn = await asyncpg.connect(**storage.connection("ace_local"))
    try:
        report["counts"] = {table: await conn.fetchval(f"SELECT count(*) FROM {table}")
                            for table in ["users", "dating_profiles", "dating_games", "countries", "cities"]}
        report["bootstrap_status"] = await conn.fetchval("SELECT status FROM _ace_local_bootstrap WHERE name='default'")
        assert report["bootstrap_status"] == "complete"
        encrypted = await conn.fetchval("SELECT encrypted_secret_key FROM s3_keys ORDER BY id LIMIT 1")
        # Read the key through the same Vault API as services; the PEM on disk
        # deliberately belongs to the bootstrap user and can have mode 0600.
        state = json.loads((ROOT / ".private/vault-state.json").read_text())
        async with httpx.AsyncClient(timeout=15, trust_env=False) as vault:
            response = await vault.get("http://vault:8200/v1/secret/data/local/rsa",
                                       headers={"X-Vault-Token": state["app_token"]})
            assert response.status_code == 200, "Local Vault RSA key was unavailable"
            encoded_key = response.json()["data"]["data"]["private_key"]
        private = serialization.load_pem_private_key(base64.b64decode(encoded_key), password=None)
        secret = private.decrypt(base64.b64decode(encrypted),
                                 padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                   algorithm=hashes.SHA256(), label=None)).decode()
        assert secret == credentials["minio_password"], "S3 key is not bound to this device"
        report["s3_key_bound_to_local_vault_rsa"] = "passed"
    finally:
        await conn.close()
    s3 = storage.s3_client()
    for obj in bundle["objects"]:
        body = s3.get_object(Bucket=obj["bucket"], Key=obj["key"])["Body"].read()
        assert hashlib.sha256(body).hexdigest() == obj["sha256"], "S3 fixture bytes changed"
    obj = bundle["objects"][0]
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        public = await client.get(f'http://s3:8333/{obj["bucket"]}/{obj["key"]}')
        assert public.status_code == 200 and hashlib.sha256(public.content).hexdigest() == obj["sha256"]
    report["s3_objects_verified"] = len(bundle["objects"])
    qdrant = AsyncQdrantClient(host="qdrant", port=6333)
    try:
        report["qdrant_points"] = (await qdrant.count("ace_local_profiles", exact=True)).count
    finally:
        await qdrant.close()
    report["status"] = "passed"
    (ROOT / ".state").mkdir(exist_ok=True)
    (ROOT / ".state/verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # API/driver errors can include request bodies; expose only safe failure metadata.
        print("VERIFY FAILED: " + type(error).__name__ + ". No passwords, OTP codes or tokens printed.")
        raise SystemExit(1)
