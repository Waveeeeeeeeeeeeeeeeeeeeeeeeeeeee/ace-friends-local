"""Restore the initial database, upload photos and index profiles in Qdrant."""
from pathlib import Path
from pathlib import Path, PurePosixPath
import argparse
import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time

from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from qdrant_client import AsyncQdrantClient
import asyncpg
import bcrypt
import boto3
import redis.asyncio as redis

ROOT = Path('/workspace')
DATING_SOURCE = Path('/sources/dating')


def connection(database):
    if database != 'ace_local':
        raise RuntimeError('Only the local ace_local database is supported.')
    credentials = json.loads((ROOT / '.private/credentials.json').read_text())
    return {'host': 'postgres', 'port': 5432, 'user': 'ace',
            'password': credentials['postgres_password'], 'database': database}


def encrypt(value):
    public = serialization.load_pem_public_key((ROOT / '.private/rsa-public.pem').read_bytes())
    return base64.b64encode(public.encrypt(value.encode(), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()


def s3_client():
    credentials = json.loads((ROOT / '.private/credentials.json').read_text())
    return boto3.client('s3', endpoint_url='http://s3:8333', region_name='us-east-1',
                        aws_access_key_id='ace-local', aws_secret_access_key=credentials['minio_password'],
                        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                                      retries={'max_attempts': 2}))


async def index_profiles(params, collection):
    # The existing Dating script owns SQL, vector normalization and search payloads.
    os.environ['CONFIG_PATH'] = str(ROOT / '.private/config/dating.yaml')
    os.environ.update({key: str(params[field]) for key, field in {
        'POSTGRES_HOST': 'host', 'POSTGRES_PORT': 'port', 'POSTGRES_USER': 'user',
        'POSTGRES_PASSWORD': 'password', 'POSTGRES_DB': 'database'}.items()})
    os.environ.pop('DATABASE_DIRECT_DSN', None)
    sys.path.insert(0, str(DATING_SOURCE))
    spec = importlib.util.spec_from_file_location(
        'ace_local_qdrant_migration', DATING_SOURCE / 'scripts/migrate_to_qdrant.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.config.services.qdrant.host = 'qdrant'
    module.config.services.qdrant.port = 6333
    module.config.services.qdrant.collection_name = collection
    client = module.QdrantClient(module.config.services.qdrant)
    await client.initialize()
    try:
        repository = module.QdrantRepository(qdrant_client=client, settings=module.config.services)
        ids = await module._get_profile_ids_from_postgres()
        for batch in module._chunked(ids, 100):
            profiles = await module._get_profiles_from_postgres(batch)
            if len(profiles) != len(batch):
                raise RuntimeError('PostgreSQL profile batch was incomplete.')
            for profile in profiles:
                if not await repository.create_or_update_profile_vector(
                        profile['id'], module._normalize_profile_for_qdrant(profile)):
                    raise RuntimeError('A Qdrant upsert failed; retry initialization.')
            actual = await client.client.retrieve(
                collection_name=collection, ids=batch, with_payload=True, with_vectors=False)
            if {row.id for row in actual} != set(batch):
                raise RuntimeError('Qdrant verification did not find every indexed profile.')
        if not ids:
            raise RuntimeError('There are no eligible profiles to index.')
        return len(ids)
    finally:
        await client.close()




BUNDLE = ROOT / "data"
MARKER = "_ace_local_bootstrap"
LOCK = 18451020
FORMAT = 1


class DataError(RuntimeError):
    pass


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def contained(base, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise DataError("Bundle path must stay inside its directory.")
    resolved = (base / str(path)).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise DataError("Bundle path escapes its directory.")
    return resolved


def manifest():
    path = BUNDLE / "manifest.json"
    if not path.is_file():
        raise DataError("data/ is missing. Use the complete archive.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT or not data.get("bundle_id"):
        raise DataError("Unsupported data manifest format.")
    for item in [data["database"], *data["objects"], *data.get("extra_files", [])]:
        file = contained(BUNDLE, item["file"])
        if not file.is_file() or file.stat().st_size != item["bytes"] or sha256(file) != item["sha256"]:
            raise DataError("Data integrity check failed: " + item["file"])
    return data


def pg_command(program, params, *args, input_data=None):
    result = subprocess.run(
        [program, "-h", params["host"], "-p", str(params["port"]), "-U", params["user"],
         "-d", params["database"], *args],
        input=input_data, capture_output=True,
        env={**os.environ, "PGPASSWORD": params["password"]},
    )
    if result.returncode:
        raise DataError(program + " failed; SQL values and credentials are not printed.")
    return result.stdout


async def wait_database(params):
    for attempt in range(60):
        try:
            return await asyncpg.connect(**params, timeout=5)
        except (OSError, asyncpg.PostgresError):
            if attempt == 59:
                raise DataError("Local PostgreSQL did not become ready.") from None
            await asyncio.sleep(1)


async def counts(conn):
    return {table: await conn.fetchval(f'SELECT count(*) FROM public."{table}"')
            for table in ["users", "dating_profiles", "dating_games", "countries", "cities"]}


async def restore_or_preserve(params, data):
    """SQL restoration is atomic; its marker is included in the same dump."""
    conn = await wait_database(params)
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK)
    try:
        exists = await conn.fetchval("SELECT to_regclass($1)", "public." + MARKER)
        row = await conn.fetchrow(f"SELECT * FROM {MARKER} WHERE name='default'") if exists else None
        if not row:
            table_count = await conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            legacy = await conn.fetchval("SELECT to_regclass('public._ace_local_seed_runs')")
            if table_count and legacy:
                state = await conn.fetchrow("SELECT manifest FROM _ace_local_seed_runs WHERE name='default'")
                if not state or json.loads(state["manifest"]).get("status") != "complete":
                    raise DataError("An incomplete legacy seed was preserved; finish it before migration.")
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {MARKER} (name text PRIMARY KEY,bundle_id text NOT NULL,"
                    "status text NOT NULL,origin text NOT NULL,manifest jsonb NOT NULL DEFAULT '{}'::jsonb)")
                await conn.execute(
                    f"INSERT INTO {MARKER}(name,bundle_id,status,origin) VALUES ('default',$1,'complete','existing')",
                    data["bundle_id"])
                row = await conn.fetchrow(f"SELECT * FROM {MARKER} WHERE name='default'")
                print("Existing local data adopted; rows, passwords and keys were preserved.", flush=True)
            elif table_count:
                raise DataError("Existing unmanaged database preserved. Automatic dump restoration requires an empty database.")
            else:
                print("First start: restoring sanitized PostgreSQL dump...", flush=True)
                sql = gzip.decompress(contained(BUNDLE, data["database"]["file"]).read_bytes())
                pg_command("psql", params, "-X", "--single-transaction", "-v", "ON_ERROR_STOP=1", input_data=sql)
                row = await conn.fetchrow(f"SELECT * FROM {MARKER} WHERE name='default'")
                if not row:
                    raise DataError("The dump did not contain its bootstrap marker.")
        if row["bundle_id"] != data["bundle_id"]:
            raise DataError("A different fixture bundle is already installed; existing data was preserved.")
        if row["status"] == "restored":
            credentials = json.loads((ROOT / ".private/credentials.json").read_text())
            password_hash = repr(bcrypt.hashpw(credentials["admin_password"].encode(), bcrypt.gensalt()))
            settings = json.loads((ROOT / ".private/stack-settings.json").read_text())
            public_s3 = settings["s3_public_url"]
            async with conn.transaction():
                await conn.execute("UPDATE s3_keys SET encrypted_access_key=$1,encrypted_secret_key=$2,endpoint='http://s3:8333'",
                                   encrypt("ace-local"), encrypt(credentials["minio_password"]))
                await conn.execute("UPDATE buckets SET public_base_url=$1 || '/' || name", public_s3)
                await conn.execute("UPDATE users SET password_hash=$1 WHERE email='admin@ace.local'", password_hash)
                await conn.execute(f"UPDATE {MARKER} SET status='database_ready' WHERE name='default'")
            print("Generated this device's admin password and S3 key binding; no old secrets were imported.", flush=True)
        return conn
    except BaseException:
        await conn.close()
        raise


def upload_missing(data):
    s3 = s3_client()
    for bucket in sorted({o["bucket"] for o in data["objects"]}):
        for attempt in range(60):
            try:
                try:
                    s3.head_bucket(Bucket=bucket)
                except ClientError as error:
                    if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                        raise
                    s3.create_bucket(Bucket=bucket)
                policy = {"Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"}]}
                s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
                break
            except Exception:
                if attempt == 59:
                    raise DataError("Local S3 did not become ready.") from None
                time.sleep(1)
    added = 0
    for obj in data["objects"]:
        present = False
        try:
            s3.head_object(Bucket=obj["bucket"], Key=obj["key"])
            present = True
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        if not present:
            s3.upload_file(str(contained(BUNDLE, obj["file"])), obj["bucket"], obj["key"],
                           ExtraArgs={"ContentType": obj["content_type"]})
            added += 1
    return added


async def apply_bundle(repair=False):
    data = manifest()  # Verify all files before connecting to the database.
    params = connection("ace_local")
    conn = await restore_or_preserve(params, data)
    try:
        if os.environ.get("ACE_TEST_FAIL_AFTER_DATABASE") == "1":
            raise DataError("Intentional first-start interruption after the database phase.")
        added = upload_missing(data)
        client = AsyncQdrantClient(host="qdrant", port=6333)
        try:
            for attempt in range(60):
                try:
                    present = await client.collection_exists("ace_local_profiles")
                    break
                except Exception:
                    if attempt == 59:
                        raise DataError("Local Qdrant did not become ready.") from None
                    await asyncio.sleep(1)
            indexed = (await client.count("ace_local_profiles", exact=True)).count if present else 0
        finally:
            await client.close()
        eligible = await conn.fetchval(
            "SELECT count(*) FROM dating_profiles p JOIN users u ON u.id=p.user_id "
            "WHERE NOT u.is_blocked AND p.locations IS NOT NULL "
            "AND jsonb_typeof(p.locations::jsonb)='array' AND jsonb_array_length(p.locations::jsonb)>0")
        state = await conn.fetchval(f"SELECT status FROM {MARKER} WHERE name='default'")
        if repair or state != "complete" or indexed != eligible:
            print("Building local Qdrant index from PostgreSQL...", flush=True)
            indexed = await index_profiles(params, "ace_local_profiles")
        report = {"status": "complete", "bundle_id": data["bundle_id"], "counts": await counts(conn),
                  "media_objects": len(data["objects"]), "uploaded_now": added, "indexed_profiles": indexed}
        await conn.execute(f"UPDATE {MARKER} SET status='complete',manifest=$1 WHERE name='default'", json.dumps(report))
        cache = redis.from_url("redis://redis:6379/0")
        try:
            await cache.incr("search:profiles:version")
        finally:
            await cache.aclose()
        (ROOT / ".state").mkdir(exist_ok=True)
        (ROOT / ".state/bootstrap-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Local data ready: " + json.dumps(report), flush=True)
    finally:
        await conn.close()  # Releases the first-start advisory lock.


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "apply", "repair"])
    args = parser.parse_args()
    if args.action == "check":
        data = manifest()
        print("Data verified: " + json.dumps({"counts": data["counts"], "media_objects": len(data["objects"])}))
        return
    await apply_bundle(repair=args.action == "repair")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(main())
    except DataError as error:
        print("BOOTSTRAP ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("BOOTSTRAP ERROR: " + type(error).__name__ + ". No SQL values or credentials printed.", file=sys.stderr)
        sys.exit(1)
