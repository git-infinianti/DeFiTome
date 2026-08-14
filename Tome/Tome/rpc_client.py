"""Network-aware Evrmore RPC routing with explicit endpoint selection.

Each request is sent only to its selected endpoint. Public and local RPC
backends never retry through one another. Both endpoint modes support mainnet
and testnet when explicitly selected.
"""

import logging
import threading
from contextlib import contextmanager

from django.conf import settings as django_settings
from evrmore_rpc import EvrmoreClient
import requests


logger = logging.getLogger(__name__)

NETWORK_MAINNET = 'mainnet'
NETWORK_TESTNET = 'testnet'
RPC_ENDPOINT_PUBLIC = 'public'
RPC_ENDPOINT_LOCAL = 'local'
_thread_local = threading.local()


def normalize_network_mode(network_mode):
    normalized = str(network_mode or '').strip().lower()
    if normalized == NETWORK_MAINNET:
        return NETWORK_MAINNET
    return NETWORK_TESTNET


def set_active_network_mode(network_mode):
    _thread_local.network_mode = normalize_network_mode(network_mode)


def get_active_network_mode():
    configured_default = getattr(django_settings, 'DEFAULT_EVRMORE_NETWORK', NETWORK_TESTNET)
    fallback_default = normalize_network_mode(configured_default)
    return normalize_network_mode(getattr(_thread_local, 'network_mode', fallback_default))


def clear_active_network_mode():
    if hasattr(_thread_local, 'network_mode'):
        delattr(_thread_local, 'network_mode')


@contextmanager
def using_network_mode(network_mode):
    previous_mode = get_active_network_mode()
    set_active_network_mode(network_mode)
    try:
        yield get_active_network_mode()
    finally:
        set_active_network_mode(previous_mode)


def normalize_rpc_endpoint_mode(rpc_endpoint_mode):
    normalized = str(rpc_endpoint_mode or '').strip().lower()
    if normalized == RPC_ENDPOINT_LOCAL:
        return RPC_ENDPOINT_LOCAL
    return RPC_ENDPOINT_PUBLIC


def set_active_rpc_endpoint_mode(rpc_endpoint_mode):
    _thread_local.rpc_endpoint_mode = normalize_rpc_endpoint_mode(rpc_endpoint_mode)


def get_active_rpc_endpoint_mode():
    configured_default = getattr(django_settings, 'DEFAULT_EVRMORE_RPC_ENDPOINT_MODE', RPC_ENDPOINT_PUBLIC)
    fallback_default = normalize_rpc_endpoint_mode(configured_default)
    return normalize_rpc_endpoint_mode(getattr(_thread_local, 'rpc_endpoint_mode', fallback_default))


def clear_active_rpc_endpoint_mode():
    if hasattr(_thread_local, 'rpc_endpoint_mode'):
        delattr(_thread_local, 'rpc_endpoint_mode')


@contextmanager
def using_rpc_endpoint_mode(rpc_endpoint_mode):
    """Temporarily route calls through one configured RPC endpoint mode."""
    previous_mode = get_active_rpc_endpoint_mode()
    set_active_rpc_endpoint_mode(rpc_endpoint_mode)
    try:
        yield get_active_rpc_endpoint_mode()
    finally:
        set_active_rpc_endpoint_mode(previous_mode)


class RoutedEvrmoreClient:
    """Proxy client that sends each RPC call to exactly one selected backend."""

    def __init__(self):
        self._clients = {}

    def __getattr__(self, method_name):
        def _wrapped(*args, **kwargs):
            return self._call_selected_endpoint(method_name, *args, **kwargs)

        return _wrapped

    def _call_selected_endpoint(self, method_name, *args, **kwargs):
        network_mode = get_active_network_mode()
        endpoint_mode = get_active_rpc_endpoint_mode()
        backend_name, client = self._get_backend(network_mode, endpoint_mode)
        method = getattr(client, method_name, None)
        if method is None:
            raise AttributeError(f'{backend_name.title()} RPC method {method_name!r} is unavailable.')
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                'RPC call failed. network=%s endpoint_mode=%s backend=%s method=%s error=%s',
                network_mode,
                endpoint_mode,
                backend_name,
                method_name,
                str(exc),
            )
            raise

    def _get_backend(self, network_mode, rpc_endpoint_mode):
        if rpc_endpoint_mode == RPC_ENDPOINT_LOCAL:
            return 'local', self._get_local_client(network_mode)
        return 'public', self._get_public_client(network_mode)

    def _get_local_client(self, network_mode):
        cache_key = ('local', network_mode)
        if cache_key in self._clients:
            return self._clients[cache_key]

        timeout = getattr(django_settings, 'RPC_TIMEOUT_SECONDS', 10)
        default_datadir = getattr(django_settings, 'RPC_DATADIR', '/tmp/evrmore')

        if network_mode == NETWORK_MAINNET:
            datadir = getattr(django_settings, 'RPC_MAINNET_DATADIR', default_datadir)
            testnet = False
            rpcuser = getattr(django_settings, 'RPC_MAINNET_USER', None)
            rpcpassword = getattr(django_settings, 'RPC_MAINNET_PASSWORD', None)
            rpcport = getattr(django_settings, 'RPC_MAINNET_PORT', None)
            rpchost = getattr(django_settings, 'RPC_MAINNET_HOST', None)
            rpcscheme = getattr(django_settings, 'RPC_MAINNET_SCHEME', 'http')
            rpcpath = getattr(django_settings, 'RPC_MAINNET_PATH', '/rpc')
            rpcurl = getattr(django_settings, 'RPC_MAINNET_URL', None)
        else:
            datadir = getattr(django_settings, 'RPC_TESTNET_DATADIR', default_datadir)
            testnet = True
            rpcuser = getattr(django_settings, 'RPC_TESTNET_USER', None)
            rpcpassword = getattr(django_settings, 'RPC_TESTNET_PASSWORD', None)
            rpcport = getattr(django_settings, 'RPC_TESTNET_PORT', None)
            rpchost = getattr(django_settings, 'RPC_TESTNET_HOST', None)
            rpcscheme = getattr(django_settings, 'RPC_TESTNET_SCHEME', 'http')
            rpcpath = getattr(django_settings, 'RPC_TESTNET_PATH', '/rpc')
            rpcurl = getattr(django_settings, 'RPC_TESTNET_URL', None)

        if rpcurl or rpchost:
            url = str(rpcurl).strip() if rpcurl else None
            if not url:
                host = str(rpchost).strip()
                path = str(rpcpath or '/rpc').strip() or '/rpc'
                if not path.startswith('/'):
                    path = f'/{path}'
                if rpcport is not None:
                    url = f"{rpcscheme}://{host}:{int(rpcport)}{path}"
                else:
                    url = f"{rpcscheme}://{host}{path}"

            client = AuthRpcClient(
                url=url,
                timeout=timeout,
                username=rpcuser,
                password=rpcpassword,
            )
            self._clients[cache_key] = client
            return client

        kwargs = {
            'datadir': datadir,
            'testnet': testnet,
            'timeout': timeout,
        }

        if rpcuser:
            kwargs['rpcuser'] = rpcuser
        if rpcpassword:
            kwargs['rpcpassword'] = rpcpassword
        if rpcport is not None:
            kwargs['rpcport'] = int(rpcport)

        client = EvrmoreClient(**kwargs)
        self._clients[cache_key] = client
        return client

    def _get_public_client(self, network_mode):
        cache_key = ('public', network_mode)
        if cache_key in self._clients:
            return self._clients[cache_key]

        timeout = getattr(django_settings, 'RPC_PUBLIC_TIMEOUT_SECONDS', 10)
        if network_mode == NETWORK_MAINNET:
            url = getattr(
                django_settings,
                'EVRMORE_PUBLIC_RPC_MAINNET_URL',
                'https://evr-rpc-mainnet.evrmorecoin.org/rpc',
            )
        else:
            url = getattr(
                django_settings,
                'EVRMORE_PUBLIC_RPC_TESTNET_URL',
                'https://evr-rpc-testnet.evrmorecoin.org/rpc',
            )

        client = PublicRpcClient(url=url, timeout=timeout)
        self._clients[cache_key] = client
        return client


class PublicRpcClient:
    """Minimal JSON-RPC client for HTTPS public endpoints."""

    def __init__(self, url, timeout=10):
        self.url = str(url).rstrip('/')
        self.timeout = timeout

    def __getattr__(self, method_name):
        def _call(*args, **kwargs):
            params = list(args)
            if kwargs:
                params.append(kwargs)

            payload = {
                'jsonrpc': '1.0',
                'id': 'defitome-public-rpc',
                'method': method_name,
                'params': params,
            }

            response = requests.post(
                self.url,
                json=payload,
                timeout=self.timeout,
            )
            body = response.json()
            if body.get('error'):
                raise Exception(str(body['error']))
            response.raise_for_status()
            return body.get('result')

        return _call


class AuthRpcClient:
    """JSON-RPC client for authenticated local/remote Evrmore node endpoints."""

    def __init__(self, url, timeout=10, username=None, password=None):
        normalized_url = str(url).strip()
        if not normalized_url:
            raise ValueError('RPC URL is required for AuthRpcClient.')
        self.url = normalized_url.rstrip('/')
        self.timeout = timeout
        self.auth = (username, password) if username or password else None

    def __getattr__(self, method_name):
        def _call(*args, **kwargs):
            params = list(args)
            if kwargs:
                params.append(kwargs)

            payload = {
                'jsonrpc': '1.0',
                'id': 'defitome-local-rpc',
                'method': method_name,
                'params': params,
            }

            response = requests.post(
                self.url,
                json=payload,
                timeout=self.timeout,
                auth=self.auth,
            )

            body = response.json()
            if body.get('error'):
                raise Exception(str(body['error']))
            response.raise_for_status()
            return body.get('result')

        return _call


RPC = RoutedEvrmoreClient()


def get_current_network_mode():
    return get_active_network_mode()


def get_current_rpc_endpoint_mode():
    return get_active_rpc_endpoint_mode()
