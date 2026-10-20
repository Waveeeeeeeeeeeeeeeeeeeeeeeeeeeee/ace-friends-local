"""Generate local service configs and initialize Vault."""
from pathlib import Path
import base64
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path('/workspace')
PRIVATE = ROOT / '.private'
PORTS = {'database': 15000, 'auth': 15100, 'gis': 15200, 'dating': 15300,
         'ai': 15400, 'notifies': 15500, 'admin': 15600}
BACKENDS = [*PORTS, 'tg-bot', 'scheduler']
ALIASES = {'authorization': 'auth', 'notify': 'notifies'}


def stack_settings():
    return {
        'project_name': os.environ.get('COMPOSE_PROJECT_NAME', 'ace-dev-stack'),
        'gateway_url': os.environ.get('GATEWAY_URL', 'http://localhost:18080'),
        'vault_url': os.environ.get('VAULT_URL', 'http://localhost:18200'),
        's3_public_url': os.environ.get('S3_PUBLIC_URL', 'http://localhost:19000'),
        'admin_origin': 'http://localhost:3031',
    }


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def source(name):
    return Path(os.environ.get('ACE_SOURCE_ROOT', ROOT.parent)) / f'ace-friends-{name}'


def prepare():
    PRIVATE.mkdir(exist_ok=True)
    local_settings = stack_settings()
    save_private(PRIVATE / 'stack-settings.json', json.dumps(local_settings, indent=2))
    credentials_file = PRIVATE / 'credentials.json'
    if credentials_file.exists():
        credentials = json.loads(credentials_file.read_text(encoding='utf-8'))
    else:
        credentials = {
            'postgres_password': secrets.token_urlsafe(28),
            'minio_password': secrets.token_urlsafe(28),
            'admin_password': secrets.token_urlsafe(22),
            'service_tokens': {name: secrets.token_urlsafe(32) for name in BACKENDS},
            'jwt_keys': [secrets.token_urlsafe(48) for _ in range(7)],
            'telegram_token': '999999999:' + secrets.token_urlsafe(27),
            'webhook_secret': secrets.token_urlsafe(32),
        }
        save_private(credentials_file, json.dumps(credentials, indent=2))
    integrations_file = PRIVATE / 'integrations.json'
    integrations = json.loads(integrations_file.read_text()) if integrations_file.exists() else {}
    if integrations.get('telegram_token'):
        credentials['telegram_token'] = integrations['telegram_token']
        save_private(credentials_file, json.dumps(credentials, indent=2))
    save_private(PRIVATE / 'postgres-password', credentials['postgres_password'])
    save_private(PRIVATE / 's3-secret', credentials['minio_password'])

    vault_state = PRIVATE / 'vault-state.json'
    vault_token = json.loads(vault_state.read_text()).get('app_token') if vault_state.exists() else None
    origins = [f'http://{host}:{port}'
               for host in ['localhost', '127.0.0.1'] for port in [3030, 3031]]
    for name in BACKENDS:
        config = yaml.safe_load((source(name) / 'config/development.yaml').read_text(encoding='utf-8'))
        if isinstance(config.get('logging'), dict):
            config['logging']['level'] = 'info'
        for section in ['api', 'startup_config']:
            api = config.get(section)
            if isinstance(api, dict):
                api.update(host='0.0.0.0', workers=1)
                if 'log_level' in api:
                    api['log_level'] = 'info'
                if 'logger_user_telegram_id' in api:
                    api['logger_user_telegram_id'] = None
                if 'cors' in api:
                    api['cors']['enabled'] = True
                    api['cors']['allow_origins'] = origins
        for section in ['redis', 'redis_pubsub']:
            if isinstance(config.get(section), dict):
                config[section]['url'] = 'redis://redis:6379/0'
                config[section]['max_connections'] = 20
        if isinstance(config.get('database'), dict):
            config['database'].update(host='postgres', port=5432, user='ace',
                                      password=credentials['postgres_password'], database='ace_local',
                                      min_connections=1, max_connections=5)
        vault = config.get('vault', config.get('openbao'))
        if vault is not None:
            vault.update(service_url='http://vault:8200/v1', authorization_token=vault_token)
            for key in ['approle_role_id', 'approle_secret_id', 'approle_secret_id_file']:
                if key in vault:
                    vault[key] = None
            for key, path in [('ecdsa_keys_secret_id', 'local/ecdsa'), ('rsa_keys_secret_id', 'local/rsa')]:
                if key in vault:
                    vault[key] = path
        for key, settings in config.get('services', {}).items():
            if not isinstance(settings, dict):
                continue
            target = ALIASES.get(key, key)
            if key == 'security':
                settings['secret_id'] = f'local/{name}'
                if 'allowed_secret_ids' in settings:
                    settings['allowed_secret_ids'] = [f'local/{name}']
            elif target in BACKENDS:
                if 'secret_id' in settings:
                    settings['secret_id'] = f'local/{target}'
                if target in PORTS:
                    url = f'http://gateway/{target}/v1'
                    for field in ['url', 'base_url']:
                        if field in settings:
                            settings[field] = url
            elif key == 'qdrant':
                settings.update(host='qdrant', port=6333, collection_name='ace_local_profiles')
            elif key == 'webapp':
                settings['url'] = f'http://localhost:{3030}'
        if isinstance(config.get('bot'), dict):
            config['bot']['authorization_token'] = credentials['telegram_token']
            config['bot']['secret_id'] = 'local/telegram-bot'
            if 'logger_user_telegram_id' in config['bot']:
                config['bot']['logger_user_telegram_id'] = None
            if 'state_url' in config['bot']:
                config['bot']['state_url'] = 'redis://redis:6379/1'
            if name == 'tg-bot':
                config['bot']['moderation']['moderators_chat_ids'] = []
        if isinstance(config.get('celery'), dict):
            config['celery']['redis_url'] = 'redis://redis:6379/2'
        if isinstance(config.get('smtp'), dict):
            config['smtp'].update(host='mailpit', port=1025, username='', password='',
                                  from_address='ace@local.test', use_tls=False, start_tls=False)
        if isinstance(config.get('jwt_authorization'), dict):
            idx = 0
            for token_settings in config['jwt_authorization'].values():
                for field in ['secret_key', 'refresh_secret_key']:
                    if field in token_settings:
                        token_settings[field] = credentials['jwt_keys'][idx]
                        idx += 1
        for provider in ['groq', 'openrouter']:
            if isinstance(config.get(provider), dict):
                config[provider]['authorization_token'] = integrations.get(provider + '_token', '')
        if isinstance(config.get('ai'), dict):
            config['ai']['provider'] = 'openrouter'
        save_private(PRIVATE / 'config' / f'{name}.yaml', yaml.safe_dump(config, allow_unicode=True, sort_keys=False))





BASE = 'http://vault:8200/v1'
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, data=None, token=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                 method=method or ('POST' if body is not None else 'GET'))
    try:
        with CLIENT.open(req, timeout=15) as response:
            result = response.read()
            return json.loads(result) if result else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Local Vault request failed: HTTP {exc.code}, path {path}') from None


def save_state(state):
    (PRIVATE / 'vault-state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')


def keys():
    openssl = shutil.which('openssl')
    if openssl is None:
        raise RuntimeError('OpenSSL is required; use the stack tooling container.')
    paths = {}
    for algorithm in ['rsa', 'ecdsa']:
        private = PRIVATE / f'{algorithm}-private.pem'
        public = PRIVATE / f'{algorithm}-public.pem'
        if not private.exists():
            args = ['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048'] if algorithm == 'rsa' else ['genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1']
            subprocess.run([str(openssl), *args, '-out', str(private)], check=True, capture_output=True)
        if not public.exists():
            subprocess.run([str(openssl), 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
        paths[algorithm] = {'private_key': base64.b64encode(private.read_bytes()).decode(),
                            'public_key': base64.b64encode(public.read_bytes()).decode(), 'ttl': 2592000}
    return paths


def main():
    prepare()
    file = PRIVATE / 'vault-state.json'
    for attempt in range(30):
        try:
            initialized = request('/sys/init')['initialized']
            break
        except (OSError, RuntimeError):
            if attempt == 29:
                raise RuntimeError('Local Vault did not become available.') from None
            time.sleep(1)
    if not initialized:
        if file.exists():
            raise RuntimeError('Saved vault keys exist but storage is empty; preserved for review.')
        result = request('/sys/init', {'secret_shares': 1, 'secret_threshold': 1}, method='PUT')
        state = {'root_token': result['root_token'], 'unseal_key': result['keys_base64'][0]}
        save_state(state)
    elif file.exists():
        state = json.loads(file.read_text())
    else:
        raise RuntimeError('Vault already initialized; matching local state file is required.')
    if request('/sys/seal-status')['sealed']:
        result = request('/sys/unseal', {'key': state['unseal_key']}, method='PUT')
        if result['sealed']:
            raise RuntimeError('Local vault remains sealed.')
    # Raft can report unsealed while still in read-only standby during election.
    # Wait for the active node before the first KV mount or secret write.
    for attempt in range(60):
        try:
            health = request('/sys/health')
            if health.get('initialized') and not health.get('sealed') and not health.get('standby'):
                break
        except (OSError, RuntimeError):
            pass
        if attempt == 59:
            raise RuntimeError('Local Vault did not become active after unsealing.')
        time.sleep(1)
    root_token = state['root_token']
    mounts = request('/sys/mounts', token=root_token)['data']
    if 'secret/' not in mounts:
        request('/sys/mounts/secret', {'type': 'kv', 'options': {'version': '2'}}, root_token)
    # The KV v2 mount can finish its background upgrade after mount creation
    # returned success. Probe its read-only config endpoint before storing keys.
    for attempt in range(60):
        try:
            request('/secret/config', token=root_token)
            break
        except (OSError, RuntimeError):
            if attempt == 59:
                raise RuntimeError('Local Vault KV v2 did not become ready.')
            time.sleep(1)
    credentials = json.loads((PRIVATE / 'credentials.json').read_text())
    for name in BACKENDS:
        token = credentials['service_tokens'][name]
        request('/secret/data/local/' + name, {'data': {'authorization_token': token, 'ttl': 2592000}}, root_token)
    request('/secret/data/local/telegram-bot', {'data': {'authorization_token': credentials['telegram_token'], 'ttl': 2592000}}, root_token)
    for algorithm, pair in keys().items():
        request('/secret/data/local/' + algorithm, {'data': pair}, root_token)
    policy = 'path "secret/data/local/*" { capabilities = ["read"] }\npath "secret/data/runtime/*" { capabilities = ["read"] }'
    request('/sys/policies/acl/ace-local-read', {'policy': policy}, root_token, method='PUT')
    valid_app_token = False
    if state.get('app_token'):
        try:
            request('/auth/token/lookup-self', token=state['app_token'])
            valid_app_token = True
        except RuntimeError:
            pass
    if not valid_app_token:
        state['app_token'] = request('/auth/token/create', {'policies': ['ace-local-read'], 'ttl': '720h',
                                                         'display_name': 'ace-dev-stack-read'}, root_token)['auth']['client_token']
        save_state(state)
    prepare()
    print('Local Vault initialized, unsealed, and seeded. Applications use a read-only token.')
    print('Telegram token is synthetic; external Telegram actions require a real test bot.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Local initialization failed: ' + type(error).__name__ + '. Check Vault and source configs.')
        raise SystemExit(1)
