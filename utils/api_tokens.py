"""API token lookup helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx

from utils.debug import is_debug_enabled
from utils.proxy import get_proxy_server


def mask_secret(value: object) -> object:
	if not isinstance(value, str):
		return value
	if len(value) <= 10:
		return '*' * len(value)
	return f'{value[:6]}...{value[-4:]}'


def mask_token_record(record: dict[str, Any]) -> dict[str, Any]:
	masked = dict(record)
	for key in ('key', 'token', 'value'):
		if key in masked:
			masked[key] = mask_secret(masked[key])
	return masked


def normalize_token_payload(payload: object) -> list[dict[str, Any]] | None:
	if not isinstance(payload, dict):
		return None

	data = payload.get('data')
	if isinstance(data, list):
		return [item for item in data if isinstance(item, dict)]

	if isinstance(data, dict):
		for list_key in ('items', 'tokens', 'list', 'records', 'rows'):
			items = data.get(list_key)
			if isinstance(items, list):
				return [item for item in items if isinstance(item, dict)]

	for list_key in ('items', 'tokens', 'list', 'records', 'rows'):
		items = payload.get(list_key)
		if isinstance(items, list):
			return [item for item in items if isinstance(item, dict)]

	return None


def build_token_headers(provider_config, api_user: str | None = None) -> dict[str, str]:
	headers = {
		'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Referer': f'{provider_config.domain}/token',
		'Origin': provider_config.domain,
		'X-Requested-With': 'XMLHttpRequest',
	}
	if api_user:
		headers[provider_config.api_user_key] = api_user
	return headers


def fetch_api_tokens_with_client(
	client: httpx.Client,
	account_name: str,
	provider_config,
	headers: dict[str, str],
) -> tuple[list[dict[str, Any]], str]:
	candidate_paths = (
		'/api/token/?p=0&size=100',
		'/api/token/?p=1&size=100',
		'/api/token/?page=1&page_size=100',
		'/api/token/?p=0&page_size=100',
		'/api/token/',
	)

	last_error = ''
	for path in candidate_paths:
		url = f'{provider_config.domain}{path}'
		try:
			response = client.get(url, headers=headers)
		except Exception as exc:
			last_error = f'{path}: request error: {exc}'
			continue

		if is_debug_enabled():
			print(f'[DEBUG] {account_name}: {path} -> HTTP {response.status_code}')

		if response.status_code != 200:
			last_error = f'{path}: HTTP {response.status_code}'
			continue

		try:
			payload = response.json()
		except json.JSONDecodeError:
			last_error = f'{path}: non-JSON response'
			continue

		tokens = normalize_token_payload(payload)
		if tokens is not None:
			return tokens, path

		last_error = f'{path}: JSON shape not recognized'

	raise RuntimeError(f'could not load token list ({last_error})')


def fetch_api_tokens(
	account_name: str,
	provider_config,
	cookies: dict[str, str],
	api_user: str | None,
) -> tuple[list[dict[str, Any]], str]:
	client_kwargs: dict[str, Any] = {'http2': True, 'timeout': 30.0}
	proxy_url = get_proxy_server(use_proxy=provider_config.use_proxy)
	if proxy_url:
		client_kwargs['proxy'] = proxy_url

	headers = build_token_headers(provider_config, api_user)
	with httpx.Client(**client_kwargs) as client:
		client.cookies.update(cookies)
		return fetch_api_tokens_with_client(client, account_name, provider_config, headers)
