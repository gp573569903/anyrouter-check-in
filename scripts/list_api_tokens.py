#!/usr/bin/env python3
"""Export API token information for configured accounts."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
	sys.path.insert(0, str(ROOT_DIR))

from checkin import login_with_credentials, parse_cookies, prepare_cookies
from utils.api_tokens import fetch_api_tokens, mask_token_record
from utils.config import AccountConfig, AppConfig, load_accounts_config

OUTPUT_PATH = Path('api_tokens.local.json')


async def resolve_auth(account: AccountConfig, account_index: int, app_config: AppConfig):
	account_name = account.get_display_name(account_index)
	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		raise RuntimeError(f'Provider "{account.provider}" not found')

	if account.has_login_credentials():
		assert account.email is not None and account.password is not None
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
		)
		if not login_result:
			raise RuntimeError('email/password login failed')
		return provider_config, login_result.cookies, login_result.api_user

	user_cookies = parse_cookies(account.cookies)
	if not user_cookies:
		raise RuntimeError('invalid cookies configuration')
	all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
	if not all_cookies:
		raise RuntimeError('failed to prepare cookies')
	return provider_config, all_cookies, account.api_user


async def main() -> int:
	load_dotenv()

	app_config = AppConfig.load_from_env()
	accounts = load_accounts_config()
	if not accounts:
		return 1

	exported = {
		'exported_at': datetime.now().isoformat(timespec='seconds'),
		'accounts': [],
	}

	for index, account in enumerate(accounts):
		account_name = account.get_display_name(index)
		print(f'\n[PROCESSING] {account_name}: loading API tokens')
		try:
			provider_config, cookies, api_user = await resolve_auth(account, index, app_config)
			tokens, endpoint = fetch_api_tokens(account_name, provider_config, cookies, api_user)
			exported['accounts'].append(
				{
					'name': account_name,
					'provider': account.provider,
					'domain': provider_config.domain,
					'endpoint': endpoint,
					'tokens': tokens,
				}
			)

			print(f'[SUCCESS] {account_name}: found {len(tokens)} token(s) via {endpoint}')
			for token in tokens:
				print(json.dumps(mask_token_record(token), ensure_ascii=False, sort_keys=True))
		except Exception as exc:
			print(f'[FAILED] {account_name}: {exc}')
			exported['accounts'].append(
				{
					'name': account_name,
					'provider': account.provider,
					'error': str(exc),
				}
			)

	OUTPUT_PATH.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding='utf-8')
	print(f'\n[INFO] Full token export written to {OUTPUT_PATH.resolve()}')
	return 0


if __name__ == '__main__':
	raise SystemExit(asyncio.run(main()))
