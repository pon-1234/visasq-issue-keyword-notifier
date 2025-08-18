#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import unicodedata
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ===== 設定 =====
TARGET_URL = "https://expert.visasq.com/issue/?is_started_only=true"
BASE_ORIGIN = "https://expert.visasq.com"
STATE_PATH = Path("state/seen_ids.json")  # 通知済みID
# Slack Incoming Webhook URL（GitHub Actionsでは Secrets から環境変数で渡す）
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

KEYWORDS = [
	"SEO", "広告運用", "SNS運用", "ブランディング", "新規事業", "企画",
	"リブランディング", "HPリニューアル", "コンセプトメイキング", "MVV開発",
	"ロゴデザイン", "VI開発", "ブランド戦略", "ブランド開発", "商品開発",
	"イベント", "展示会", "ポップアップ", "PR", "オペレーション",
	"デザイン業務", "経営課題", "ヒアリング", "言語化",
]

UA = "Mozilla/5.0 (compatible; VisasQWatcher/1.0; +https://github.com/)"
HEADERS = {
	"User-Agent": UA,
	"Accept-Language": "ja-JP,ja;q=0.9",
}


def load_seen_ids() -> set:
	STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
	if STATE_PATH.exists():
		try:
			with STATE_PATH.open("r", encoding="utf-8") as f:
				data = json.load(f)
				return set(data.get("seen_ids", []))
		except Exception:
			return set()
	return set()


def save_seen_ids(seen: set) -> None:
	STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
	with STATE_PATH.open("w", encoding="utf-8") as f:
		json.dump({"seen_ids": sorted(seen)}, f, ensure_ascii=False, indent=2)


def fetch_html(url: str) -> str:
	# 軽いリトライ
	for i in range(3):
		resp = requests.get(url, headers=HEADERS, timeout=20)
		if resp.status_code == 200 and resp.text:
			return resp.text
		time.sleep(1 + i)
	resp.raise_for_status()
	return resp.text


def normalize_text(s: str) -> str:
	# NFKCで全角半角を正規化し、大小無視のためlower()
	return unicodedata.normalize("NFKC", s).lower()


def extract_items(html: str) -> list[dict]:
	"""
	公募カード<a href="/issue/123456/">…</a> を起点に抽出。
	class名の変更に強いよう、構造／属性ベースで取得します。
	"""
	soup = BeautifulSoup(html, "html.parser")
	items = []

	# href="/issue/数字/" のaタグを全て拾う
	link_tags = soup.find_all("a", href=re.compile(r"^/issue/\d+/?$"))
	for a in link_tags:
		href = a.get("href", "")
		m = re.search(r"/issue/(\d+)", href)
		if not m:
			continue
		issue_id = m.group(1)
		url = BASE_ORIGIN + href

		# タイトル
		h3 = a.find("h3")
		title = h3.get_text(strip=True) if h3 else ""

		# 要約（説明文と思われる<p>を優先的に1つ抽出）
		# クラス名に依存しすぎないよう、子孫pのうち「長めのテキスト」を採用
		candidate_ps = [p.get_text(" ", strip=True) for p in a.find_all("p")]
		description = ""
		if candidate_ps:
			# 一番文字数が多いものを要約とみなす
			description = max(candidate_ps, key=lambda s: len(s))

		# ラベル（NEW/締め切り間近など）
		labels = []
		for sp in a.find_all("span"):
			if sp.has_attr("qa-label") or "label" in " ".join(sp.get("class", [])):
				t = sp.get_text(strip=True)
				if t:
					labels.append(t)

		# 作成日・締切・報酬など（<li qa-content="..."> を優先）
		created = ""
		due = ""
		reward = ""
		for li in a.find_all("li"):
			qa = li.get("qa-content", "")
			txt = li.get_text(" ", strip=True)
			if qa == "created":
				# 例: "作成日: 2025年08月18日"
				created = txt.replace("作成日:", "").strip()
			elif qa == "due-date":
				due = txt
			elif "¥" in txt and not reward:
				reward = txt

		items.append({
			"id": issue_id,
			"url": url,
			"title": title,
			"description": description,
			"labels": labels,
			"created": created,   # "2025年08月18日" など
			"due": due,           # "08/20 まで" など
			"reward": reward,     # "¥30,000 〜 ¥50,000" など
		})
	return items


def filter_new_and_match(items: list[dict], seen_ids: set) -> list[dict]:
	"""
	1) 未通知（idが未登録）のみ
	2) キーワード一致（タイトル or 要約）。一致キーワードも付与
	"""
	norm_keywords = [normalize_text(k) for k in KEYWORDS]

	results = []
	for it in items:
		if it["id"] in seen_ids:
			continue
		text_for_match = normalize_text(f'{it.get("title", "")} {it.get("description", "")}')
		matched = sorted({k for k in norm_keywords if k in text_for_match})
		if matched:
			# 元の表記で見せたいので、元キーワードで再構成
			display_matched = []
			for orig in KEYWORDS:
				if normalize_text(orig) in matched:
					display_matched.append(orig)
			it["matched_keywords"] = display_matched
			results.append(it)
	return results


def build_slack_blocks(matches: list[dict]) -> dict:
	jst = ZoneInfo("Asia/Tokyo")
	now = datetime.now(jst).strftime("%Y-%m-%d %H:%M")
	header_text = f"VisasQ 公募ウォッチ（{now} JST）"
	total = len(matches)

	keywords_text = "`, `".join(KEYWORDS)

	blocks = [
		{"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}},
		{"type": "section", "text": {"type": "mrkdwn",
		 "text": f"*新着一致 {total}件*｜対象URL: <{TARGET_URL}|公募一覧（募集中のみ）>\n"
				 f"キーワード: `" + keywords_text + "`"}}
	]

	if total == 0:
		# この関数は0件時には呼ばれない運用だが、保険で用意
		blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "本日は一致なしでした。"}})
		return {"blocks": blocks, "text": "VisasQ 公募ウォッチ: 一致なし"}

	for i, it in enumerate(matches, 1):
		title = it.get("title", "").strip() or f"Issue {it['id']}"
		url = it["url"]
		created = it.get("created", "").strip()
		reward = it.get("reward", "").strip()
		due = it.get("due", "").strip()
		labels = " / ".join(it.get("labels", []))
		mk = it.get("matched_keywords", [])
		mk_text = ", ".join(mk) if mk else "-"

		# 本文
		body = (
			f"*<{url}|{title}>*\n"
			f"*作成日*: {created or '-'}    *報酬*: {reward or '-'}    *締切*: {due or '-'}\n"
			f"*一致キーワード*: `" + (mk_text or "-") + "`"
			+ (f"\n*ラベル*: {labels}" if labels else "")
		)

		blocks.append({
			"type": "section",
			"text": {"type": "mrkdwn", "text": body},
			"accessory": {
				"type": "button",
				"text": {"type": "plain_text", "text": "案件を開く"},
				"url": url
			}
		})
		if i != total:
			blocks.append({"type": "divider"})

	return {"blocks": blocks, "text": f"VisasQ 公募ウォッチ: 新着一致 {total}件"}


def post_to_slack(payload: dict) -> None:
	if not SLACK_WEBHOOK_URL:
		print("[WARN] SLACK_WEBHOOK_URL が未設定のため、標準出力に出します。")
		print(json.dumps(payload, ensure_ascii=False, indent=2))
		return
	resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=20)
	if resp.status_code >= 300:
		raise RuntimeError(f"Slack通知に失敗しました: {resp.status_code} {resp.text}")


def main():
	seen = load_seen_ids()
	html = fetch_html(TARGET_URL)
	items = extract_items(html)
	matches = filter_new_and_match(items, seen)

	# 一致があったときのみ通知
	if matches:
		payload = build_slack_blocks(matches)
		post_to_slack(payload)
		# 通知したIDを既読に追加
		for it in matches:
			seen.add(it["id"])
		save_seen_ids(seen)
	else:
		print("一致なし：Slack通知はスキップしました。")


if __name__ == "__main__":
	main()


