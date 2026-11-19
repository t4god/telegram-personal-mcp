"""Single-owner OAuth with persistent registrations, PKCE and rotating tokens."""
import os
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
    RegistrationError, TokenError, AuthorizeError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerOAuth:
    def __init__(self, store, base, password_file, owner):
        self.store, self.base, self.owner = store, base.rstrip('/'), str(owner)
        self.password_hash = hashed(password_file.read_text().strip())
        self.failures = []
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS dot_oauth(kind TEXT, id TEXT, payload TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(kind,id))')

    def put(self, kind, key, payload, ttl=31536000):
        with self.store.connection() as db:
            db.execute('DELETE FROM dot_oauth WHERE expires < ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO dot_oauth VALUES(?,?,?,?)', (kind, hashed(key), json.dumps(payload), time.time()+ttl))

    def get(self, kind, key, consume=False):
        with self.store.connection() as db:
            if consume:
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM dot_oauth WHERE kind=? AND id=? AND expires>?', (kind, hashed(key), time.time())).fetchone()
            if consume and row:
                db.execute('DELETE FROM dot_oauth WHERE kind=? AND id=?', (kind, hashed(key)))
            return json.loads(row[0]) if row else None

    async def get_client(self, client_id):
        data = self.get('client', client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        # ChatGPT cloud callbacks and Codex's RFC 8252 loopback callback.
        for uri in client_info.redirect_uris:
            u = urlsplit(str(uri))
            valid_path = u.path.startswith('/connector/oauth/') or u.path == '/connector_platform_oauth_redirect'
            cloud = u.scheme=='https' and u.hostname=='chatgpt.com' and u.port in (None,443) and valid_path
            loopback = u.scheme=='http' and u.hostname in ('127.0.0.1','::1') and u.port is not None and 1024<=u.port<=65535 and re.fullmatch(r'/(?:oauth/)?callback(?:/[A-Za-z0-9_-]{8,128})?',u.path) is not None
            configured = str(uri) in {x.strip() for x in os.environ.get('DOT_OAUTH_REDIRECT_URIS', '').split(',') if x.strip()} and u.scheme == 'https'
            if u.username or u.password or u.fragment or u.query or not (cloud or loopback or configured):
                raise RegistrationError('invalid_redirect_uri', 'Callback not allowed: '+u.scheme+'://'+str(u.hostname)+':'+str(u.port)+u.path)
        self.put('client', client_info.client_id, client_info.model_dump(mode='json'))

    async def authorize(self, client, params):
        if params.resource not in (None, self.base+'/mcp', self.base, self.base+'/'):
            raise AuthorizeError('invalid_target', 'Unknown MCP resource')
        if not params.code_challenge:
            raise AuthorizeError('invalid_request', 'PKCE is required')
        key = secrets.token_urlsafe(32)
        self.put('login', key, {'client_id':client.client_id, 'params':params.model_dump(mode='json')}, 600)
        return self.base+'/owner-login?request='+key

    async def login(self, request):
        headers = {'Cache-Control':'no-store', 'Referrer-Policy':'strict-origin', 'X-Frame-Options':'DENY',
                   'Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
        if request.method == 'GET':
            key = request.query_params.get('request','')
            login = self.get('login', key)
            if not login:
                return HTMLResponse('Ссылка входа истекла. Начните подключение заново.', 400, headers=headers)
            # Permit only this request's registered and validated OAuth callback.
            callback = str(login['params']['redirect_uri'])
            headers['Content-Security-Policy'] = (
                "default-src 'none'; form-action 'self' " + callback +
                "; frame-ancestors 'none'; base-uri 'none'"
            )
            page = '<!doctype html><meta charset="utf-8"><title>Telegram — доступ для Dot</title><h1>Подключение Telegram к ChatGPT</h1><p>Разрешить чтение всех облачных чатов и подготовку сообщений. Отправка требует вашего подтверждения в Dot. Для плагина включите запрос подтверждения перед выполнением действий.</p><form method="post" action="/owner-login"><input type="hidden" name="request" value="'+html.escape(key,quote=True)+'"><label>Ключ подключения <input type="password" name="password" required autocomplete="current-password"></label><button type="submit">Разрешить подключение</button></form>'
            return HTMLResponse(page, headers=headers)
        origin = request.headers.get('origin')
        if origin != self.base:
            return HTMLResponse('Origin rejected', 403, headers=headers)
        form = await request.form()
        key, password = str(form.get('request','')), str(form.get('password',''))
        self.failures = [t for t in self.failures if t > time.time()-300]
        if len(self.failures) >= 20:
            return HTMLResponse('Слишком много попыток. Повторите через пять минут.', 429, headers=headers)
        data = self.get('login', key)
        if not data or not secrets.compare_digest(hashed(password), self.password_hash):
            self.failures.append(time.time())
            return HTMLResponse('Неверный ключ или ссылка истекла.', 403, headers=headers)
        if not self.get('login', key, consume=True):
            return HTMLResponse('Ссылка уже использована.', 400, headers=headers)
        params = AuthorizationParams.model_validate(data['params'])
        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(code=code, scopes=params.scopes or ['telegram'], expires_at=time.time()+120,
            client_id=data['client_id'], code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self.base+'/mcp', subject=self.owner)
        self.put('code', code, record.model_dump(mode='json'), 120)
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 303, headers=headers)

    async def load_authorization_code(self, client, authorization_code):
        data = self.get('code', authorization_code)
        return AuthorizationCode.model_validate(data) if data and data['client_id']==client.client_id else None

    def issue(self, client_id, scopes, resource):
        family = secrets.token_urlsafe(24)
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        common = dict(client_id=client_id, scopes=scopes, resource=resource, subject=self.owner)
        self.put('access', access, {**common,'expires_at':int(time.time()+3600),'family':family}, 3600)
        self.put('refresh', refresh, {**common,'expires_at':int(time.time()+2592000),'family':family}, 2592000)
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=3600, refresh_token=refresh, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.get('code', authorization_code.code, consume=True)
        if not data or data['client_id'] != client.client_id:
            raise TokenError('invalid_grant', 'Authorization code used or expired')
        return self.issue(client.client_id, data['scopes'], data['resource'])

    async def load_refresh_token(self, client, refresh_token):
        data = self.get('refresh', refresh_token)
        return RefreshToken(token=refresh_token, **{k:v for k,v in data.items() if k!='family'}) if data and data['client_id']==client.client_id else None

    def revoke_family(self, family):
        with self.store.connection() as db:
            db.execute("DELETE FROM dot_oauth WHERE kind IN ('access','refresh') AND json_extract(payload,'$.family')=?", (family,))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.get('refresh', refresh_token.token, consume=True)
        if not data or data['client_id']!=client.client_id or not set(scopes).issubset(data['scopes']):
            raise TokenError('invalid_grant', 'Refresh token invalid')
        self.revoke_family(data['family'])
        return self.issue(client.client_id, scopes, data['resource'])

    async def load_access_token(self, token):
        data = self.get('access', token)
        return AccessToken(token=token, **{k:v for k,v in data.items() if k!='family'}) if data else None

    async def revoke_token(self, token):
        data = self.get('access' if isinstance(token,AccessToken) else 'refresh', token.token)
        if data:
            self.revoke_family(data['family'])

