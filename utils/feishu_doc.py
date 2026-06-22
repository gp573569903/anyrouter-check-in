"""Feishu wiki/docx/sheets update helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from utils.api_tokens import mask_secret

FEISHU_API_BASE = 'https://open.feishu.cn/open-apis'
UTC_PLUS_8 = timezone(timedelta(hours=8))
OVERVIEW_SHEET_TITLE = '账号总览'
TOKEN_SHEET_TITLE = 'API令牌明细'
RUN_LOG_SHEET_TITLE = '运行记录'


def _now_utc8() -> datetime:
	return datetime.now(UTC_PLUS_8)


def _format_datetime_utc8(value: datetime) -> str:
	return value.astimezone(UTC_PLUS_8).strftime('%Y-%m-%d %H:%M:%S')


@dataclass(frozen=True)
class FeishuDocConfig:
	doc_url: str
	app_id: str
	app_secret: str
	export_full_keys: bool = False
	parent_block_id: str | None = None

	@classmethod
	def load_from_env(cls) -> 'FeishuDocConfig | None':
		doc_url = os.getenv('FEISHU_DOC_URL', '').strip()
		if not doc_url:
			return None
		app_id = os.getenv('FEISHU_APP_ID', '').strip()
		app_secret = os.getenv('FEISHU_APP_SECRET', '').strip()
		if not app_id or not app_secret:
			print('[FEISHU_DOC] FEISHU_DOC_URL is set, but FEISHU_APP_ID or FEISHU_APP_SECRET is missing; skipped')
			return None
		return cls(
			doc_url=doc_url,
			app_id=app_id,
			app_secret=app_secret,
			export_full_keys=os.getenv('FEISHU_EXPORT_FULL_KEYS', '').strip().lower() in {'1', 'true', 'yes', 'on'},
			parent_block_id=os.getenv('FEISHU_DOC_PARENT_BLOCK_ID', '').strip() or None,
		)


def _feishu_api_check(payload: dict[str, Any], action: str) -> dict[str, Any]:
	if payload.get('code') != 0:
		raise RuntimeError(f'{action} failed: code={payload.get("code")}, msg={payload.get("msg")}')
	data = payload.get('data')
	return data if isinstance(data, dict) else {}


def _extract_url_token(doc_url: str) -> tuple[str, str]:
	parsed = urlparse(doc_url)
	path = parsed.path.strip('/')
	parts = path.split('/')
	if len(parts) >= 2 and parts[0] in {'wiki', 'docx', 'doc', 'sheets'}:
		return parts[0], parts[1]
	raise ValueError('Unsupported FEISHU_DOC_URL; expected /wiki/{token}, /docx/{token}, /doc/{token}, or /sheets/{token}')


def _get_tenant_access_token(client: httpx.Client, config: FeishuDocConfig) -> str:
	response = client.post(
		f'{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal',
		json={'app_id': config.app_id, 'app_secret': config.app_secret},
	)
	payload = response.json()
	if payload.get('code') != 0:
		raise RuntimeError(f'tenant token failed: code={payload.get("code")}, msg={payload.get("msg")}')
	token = payload.get('tenant_access_token')
	if not token:
		raise RuntimeError('tenant token response did not include tenant_access_token')
	return token


def _resolve_document(client: httpx.Client, doc_url: str, headers: dict[str, str]) -> tuple[str, str]:
	url_kind, url_token = _extract_url_token(doc_url)
	if url_kind == 'docx':
		return 'docx', url_token
	if url_kind == 'doc':
		return 'doc', url_token
	if url_kind == 'sheets':
		return 'sheets', url_token

	response = client.get(f'{FEISHU_API_BASE}/wiki/v2/spaces/get_node', params={'token': url_token}, headers=headers)
	data = _feishu_api_check(response.json(), 'resolve wiki node')
	node = data.get('node')
	if not isinstance(node, dict):
		raise RuntimeError('resolve wiki node failed: node missing')
	obj_type = str(node.get('obj_type') or '')
	obj_token = str(node.get('obj_token') or '')
	if not obj_type or not obj_token:
		raise RuntimeError('resolve wiki node failed: obj_type or obj_token missing')
	return obj_type, obj_token


def _format_money(value: Any) -> str:
	if isinstance(value, int | float):
		return f'${value:.2f}'
	return '-'


def _quota_to_dollars(value: Any) -> float | str:
	if isinstance(value, int | float):
		return round(value / 500000, 2)
	return '-'


def _format_time(timestamp: Any) -> str:
	if not isinstance(timestamp, int) or timestamp <= 0:
		return 'never'
	try:
		return datetime.fromtimestamp(timestamp, tz=UTC_PLUS_8).strftime('%Y-%m-%d %H:%M:%S')
	except Exception:
		return str(timestamp)


def _format_token_key(token: dict[str, Any], export_full_keys: bool) -> str:
	key = token.get('key') or token.get('token') or token.get('value') or ''
	if not key:
		return '-'
	return str(key) if export_full_keys else str(mask_secret(str(key)))


def build_snapshot_lines(accounts: list[dict[str, Any]], *, export_full_keys: bool) -> list[str]:
	lines = [
		'AnyRouter Check-in Snapshot',
		f'Updated at: {_format_datetime_utc8(_now_utc8())}',
		'',
	]
	for account in accounts:
		lines.extend(
			[
				f'Account: {account.get("name", "-")} ({account.get("provider", "-")})',
				f'Status: {"success" if account.get("success") else "failed"}',
				f'Balance: {_format_money(account.get("quota"))}',
				f'Used: {_format_money(account.get("used_quota"))}',
				'API tokens:',
			]
		)
		tokens = account.get('tokens') if isinstance(account.get('tokens'), list) else []
		if not tokens:
			lines.append('  - none or unavailable')
		for token in tokens:
			name = token.get('name') or token.get('id') or 'unnamed'
			status = token.get('status', '-')
			remain = token.get('remain_quota', '-')
			used = token.get('used_quota', '-')
			expired = _format_time(token.get('expired_time'))
			key = _format_token_key(token, export_full_keys)
			lines.append(
				f'  - {name} | key={key} | status={status} | remain_quota={remain} | used_quota={used} | expires={expired}'
			)
		error = account.get('token_error') or account.get('error')
		if error:
			lines.append(f'Error: {error}')
		lines.append('')
	return lines


def build_snapshot_rows(accounts: list[dict[str, Any]], *, export_full_keys: bool) -> list[list[Any]]:
	rows: list[list[Any]] = [
		['AnyRouter Check-in Snapshot'],
		['Updated at', _format_datetime_utc8(_now_utc8())],
		[],
		[
			'Account',
			'Provider',
			'Check-in status',
			'Balance',
			'Used',
			'Token name',
			'Token key',
			'Token status',
			'Remain quota',
			'Used quota',
			'Expires',
			'Error',
		],
	]
	for account in accounts:
		tokens = account.get('tokens') if isinstance(account.get('tokens'), list) else []
		base = [
			account.get('name', '-'),
			account.get('provider', '-'),
			'success' if account.get('success') else 'failed',
			account.get('quota', '-'),
			account.get('used_quota', '-'),
		]
		error = account.get('token_error') or account.get('error') or ''
		if not tokens:
			rows.append([*base, '', '', '', '', '', '', error])
			continue

		for token in tokens:
			rows.append(
				[
					*base,
					token.get('name') or token.get('id') or 'unnamed',
					_format_token_key(token, export_full_keys),
					token.get('status', '-'),
					token.get('remain_quota', '-'),
					token.get('used_quota', '-'),
					_format_time(token.get('expired_time')),
					error,
				]
			)
	return rows


def build_account_overview_rows(accounts: list[dict[str, Any]]) -> list[list[Any]]:
	now = _format_datetime_utc8(_now_utc8())
	rows: list[list[Any]] = [
		[
			'账号名',
			'平台',
			'签到状态',
			'当前余额($)',
			'可分配余额($)',
			'已使用($)',
			'总额度($)',
			'API令牌数',
			'有效令牌数',
			'最近更新',
			'异常',
		]
	]
	for account in accounts:
		tokens = account.get('tokens') if isinstance(account.get('tokens'), list) else []
		active_tokens = [token for token in tokens if token.get('status') == 1]
		quota = account.get('quota')
		used = account.get('used_quota')
		total = round(quota + used, 2) if isinstance(quota, int | float) and isinstance(used, int | float) else '-'
		reserved_quota = sum(
			token['remain_quota'] / 500000
			for token in tokens
			if not token.get('unlimited_quota')
			and isinstance(token.get('remain_quota'), int | float)
			and token['remain_quota'] >= 0
		)
		allocatable_quota = round(quota - reserved_quota, 2) if isinstance(quota, int | float) else '-'
		rows.append(
			[
				account.get('name', '-'),
				account.get('provider', '-'),
				'成功' if account.get('success') else '失败',
				quota if isinstance(quota, int | float) else '-',
				allocatable_quota,
				used if isinstance(used, int | float) else '-',
				total,
				len(tokens),
				len(active_tokens),
				now,
				account.get('token_error') or account.get('error') or '',
			]
		)
	return rows


def build_token_detail_rows(accounts: list[dict[str, Any]], *, export_full_keys: bool) -> list[list[Any]]:
	rows: list[list[Any]] = [
		[
			'账号名',
			'平台',
			'Token名称',
			'Token Key',
			'状态',
			'无限额度',
			'剩余额度($)',
			'已使用额度($)',
			'最后访问时间',
			'创建时间',
			'过期时间',
			'IP限制',
			'模型限制',
			'备注/错误',
		]
	]
	for account in accounts:
		tokens = account.get('tokens') if isinstance(account.get('tokens'), list) else []
		error = account.get('token_error') or account.get('error') or ''
		if not tokens:
			rows.append(
				[
					account.get('name', '-'),
					account.get('provider', '-'),
					'',
					'',
					'',
					'',
					'',
					'',
					'',
					'',
					'',
					'',
					'',
					error or '未读取到 API token',
				]
			)
			continue

		for token in tokens:
			rows.append(
				[
					account.get('name', '-'),
					account.get('provider', '-'),
					token.get('name') or token.get('id') or 'unnamed',
					_format_token_key(token, export_full_keys),
					'启用' if token.get('status') == 1 else token.get('status', '-'),
					'是' if token.get('unlimited_quota') else '否',
					_quota_to_dollars(token.get('remain_quota')),
					_quota_to_dollars(token.get('used_quota')),
					_format_time(token.get('accessed_time')),
					_format_time(token.get('created_time')),
					_format_time(token.get('expired_time')),
					token.get('allow_ips') or '无',
					token.get('model_limits') or '无',
					error,
				]
			)
	return rows


def build_run_log_row(accounts: list[dict[str, Any]]) -> list[Any]:
	token_count = 0
	token_error_count = 0
	errors = []
	for account in accounts:
		tokens = account.get('tokens') if isinstance(account.get('tokens'), list) else []
		token_count += len(tokens)
		if account.get('token_error'):
			token_error_count += 1
			errors.append(f'{account.get("name", "-")}: {account.get("token_error")}')
		if account.get('error'):
			errors.append(f'{account.get("name", "-")}: {account.get("error")}')

	success_count = sum(1 for account in accounts if account.get('success'))
	total_count = len(accounts)
	return [
		_format_datetime_utc8(_now_utc8()),
		total_count,
		success_count,
		total_count - success_count,
		token_count,
		token_error_count,
		'成功',
		'; '.join(errors),
	]


def _paragraph_block(content: str) -> dict[str, Any]:
	return {
		'block_type': 2,
		'text': {
			'elements': [
				{
					'text_run': {
						'content': content,
						'text_element_style': {},
					}
				}
			],
			'style': {},
		},
	}


def _append_docx_snapshot(
	client: httpx.Client,
	document_id: str,
	headers: dict[str, str],
	lines: list[str],
	parent_block_id: str | None,
) -> None:
	parent_id = parent_block_id or document_id
	blocks = [_paragraph_block(line or ' ') for line in lines]
	response = client.post(
		f'{FEISHU_API_BASE}/docx/v1/documents/{document_id}/blocks/{parent_id}/children',
		headers=headers,
		json={'index': 0, 'children': blocks},
	)
	_feishu_api_check(response.json(), 'append docx snapshot')


def _get_first_sheet_id(client: httpx.Client, spreadsheet_token: str, headers: dict[str, str]) -> str:
	response = client.get(
		f'{FEISHU_API_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query',
		headers=headers,
	)
	data = _feishu_api_check(response.json(), 'query spreadsheet sheets')
	sheets = data.get('sheets')
	if not isinstance(sheets, list) or not sheets:
		raise RuntimeError('query spreadsheet sheets failed: no sheet found')
	first_sheet = sheets[0]
	if not isinstance(first_sheet, dict) or not first_sheet.get('sheet_id'):
		raise RuntimeError('query spreadsheet sheets failed: sheet_id missing')
	return str(first_sheet['sheet_id'])


def _query_sheets(client: httpx.Client, spreadsheet_token: str, headers: dict[str, str]) -> dict[str, str]:
	response = client.get(
		f'{FEISHU_API_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query',
		headers=headers,
	)
	data = _feishu_api_check(response.json(), 'query spreadsheet sheets')
	sheets = data.get('sheets')
	if not isinstance(sheets, list):
		raise RuntimeError('query spreadsheet sheets failed: sheets missing')
	result: dict[str, str] = {}
	for sheet in sheets:
		if isinstance(sheet, dict) and sheet.get('title') and sheet.get('sheet_id'):
			result[str(sheet['title'])] = str(sheet['sheet_id'])
	return result


def _ensure_sheet(
	client: httpx.Client,
	spreadsheet_token: str,
	headers: dict[str, str],
	title: str,
	index: int,
) -> str:
	sheets = _query_sheets(client, spreadsheet_token, headers)
	if title in sheets:
		return sheets[title]

	response = client.post(
		f'{FEISHU_API_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update',
		headers=headers,
		json={
			'requests': [
				{
					'addSheet': {
						'properties': {
							'title': title,
							'index': index,
						}
					}
				}
			]
		},
	)
	_feishu_api_check(response.json(), f'create sheet {title}')

	sheets = _query_sheets(client, spreadsheet_token, headers)
	if title not in sheets:
		raise RuntimeError(f'create sheet {title} failed: sheet not found after creation')
	return sheets[title]


def _column_name(column_number: int) -> str:
	name = ''
	while column_number:
		column_number, remainder = divmod(column_number - 1, 26)
		name = chr(65 + remainder) + name
	return name


def _pad_sheet_rows(rows: list[list[Any]], min_rows: int = 200, min_cols: int = 12) -> list[list[Any]]:
	width = max(min_cols, *(len(row) for row in rows))
	padded = [row + [''] * (width - len(row)) for row in rows]
	while len(padded) < min_rows:
		padded.append([''] * width)
	return padded


def _write_sheet_values(
	client: httpx.Client,
	spreadsheet_token: str,
	headers: dict[str, str],
	sheet_id: str,
	rows: list[list[Any]],
	*,
	min_rows: int,
	min_cols: int,
) -> None:
	values = _pad_sheet_rows(rows, min_rows=min_rows, min_cols=min_cols)
	end_column = _column_name(len(values[0]))
	cell_range = f'{sheet_id}!A1:{end_column}{len(values)}'
	response = client.put(
		f'{FEISHU_API_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values',
		headers=headers,
		json={'valueRange': {'range': cell_range, 'values': values}},
	)
	_feishu_api_check(response.json(), 'update spreadsheet snapshot')


def _read_sheet_values(
	client: httpx.Client,
	spreadsheet_token: str,
	headers: dict[str, str],
	sheet_id: str,
	max_rows: int = 1000,
) -> list[list[Any]]:
	cell_range = f'{sheet_id}!A1:H{max_rows}'
	encoded_range = quote(cell_range, safe='')
	response = client.get(
		f'{FEISHU_API_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}',
		headers=headers,
	)
	payload = response.json()
	if payload.get('code') != 0:
		return []
	data = payload.get('data') if isinstance(payload, dict) else {}
	value_range = data.get('valueRange') if isinstance(data, dict) else {}
	values = value_range.get('values') if isinstance(value_range, dict) else []
	return values if isinstance(values, list) else []


def _update_sheet_snapshot(
	client: httpx.Client,
	spreadsheet_token: str,
	headers: dict[str, str],
	accounts: list[dict[str, Any]],
	export_full_keys: bool,
) -> None:
	overview_sheet_id = _ensure_sheet(client, spreadsheet_token, headers, OVERVIEW_SHEET_TITLE, 0)
	token_sheet_id = _ensure_sheet(client, spreadsheet_token, headers, TOKEN_SHEET_TITLE, 1)
	run_log_sheet_id = _ensure_sheet(client, spreadsheet_token, headers, RUN_LOG_SHEET_TITLE, 2)

	overview_rows = build_account_overview_rows(accounts)
	token_rows = build_token_detail_rows(accounts, export_full_keys=export_full_keys)

	existing_log_rows = _read_sheet_values(client, spreadsheet_token, headers, run_log_sheet_id)
	run_log_header = ['时间', '总账号数', '成功数', '失败数', 'API令牌数', '令牌读取失败账号数', '飞书更新', '错误摘要']
	if not existing_log_rows:
		run_log_rows = [run_log_header]
	elif existing_log_rows[0] != run_log_header:
		run_log_rows = [run_log_header, *existing_log_rows]
	else:
		run_log_rows = existing_log_rows
	run_log_rows.append(build_run_log_row(accounts))

	_write_sheet_values(
		client,
		spreadsheet_token,
		headers,
		overview_sheet_id,
		overview_rows,
		min_rows=200,
		min_cols=11,
	)
	_write_sheet_values(
		client,
		spreadsheet_token,
		headers,
		token_sheet_id,
		token_rows,
		min_rows=1000,
		min_cols=14,
	)
	_write_sheet_values(
		client,
		spreadsheet_token,
		headers,
		run_log_sheet_id,
		run_log_rows,
		min_rows=max(1000, len(run_log_rows) + 20),
		min_cols=8,
	)


def update_feishu_doc(accounts: list[dict[str, Any]]) -> bool:
	config = FeishuDocConfig.load_from_env()
	if not config:
		return False

	with httpx.Client(timeout=30.0) as client:
		tenant_token = _get_tenant_access_token(client, config)
		headers = {'Authorization': f'Bearer {tenant_token}', 'Content-Type': 'application/json; charset=utf-8'}
		doc_type, doc_token = _resolve_document(client, config.doc_url, headers)
		if doc_type in {'sheets', 'sheet', 'spreadsheet'}:
			_update_sheet_snapshot(client, doc_token, headers, accounts, config.export_full_keys)
			print(f'[FEISHU_DOC] Updated sheet snapshot, accounts={len(accounts)}')
			return True

		if doc_type != 'docx':
			raise RuntimeError(f'Feishu document type "{doc_type}" is not supported yet; please use a docx wiki page')
		lines = build_snapshot_lines(accounts, export_full_keys=config.export_full_keys)
		_append_docx_snapshot(client, doc_token, headers, lines, config.parent_block_id)
		print(f'[FEISHU_DOC] Updated docx snapshot, accounts={len(accounts)}')
		return True
