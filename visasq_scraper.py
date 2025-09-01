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
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# Playwright（オプション）
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

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

# テスト用フラグ（環境変数）
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY") == "1"  # 既読無視で通知
DRY_RUN = os.getenv("DRY_RUN") == "1"            # Slack送信せずpayload出力のみ
ENABLE_BROWSER = os.getenv("ENABLE_BROWSER") == "1"  # 動的DOMレンダリング（Playwright）を使用


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
	if ENABLE_BROWSER and PLAYWRIGHT_AVAILABLE:
		# Playwrightを使用して動的DOMをレンダリング
		try:
			with sync_playwright() as p:
				browser = p.chromium.launch()
				context = browser.new_context()
				page = context.new_page()
				page.goto(url, wait_until="networkidle")
				html = page.content()
				browser.close()
				return html
		except Exception as e:
			print(f"[ERROR] PlaywrightでHTMLを取得できませんでした: {e}")
			return ""
	else:
		# Requestsを使用して静的HTMLを取得
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
	2025年9月のUI変更に対応。
	"""
	soup = BeautifulSoup(html, "html.parser")
	items = []

	# 新しいUI構造: <li>内の<a class="_link_1ws1l_1" href="/issue/数字/">
	# または <a class="_link_1fzqm_1" href="/direct_interview/数字/">
	link_tags = soup.find_all("a", href=re.compile(r"^/(issue|direct_interview)/\d+/?$"))
	
	for a in link_tags:
		href = a.get("href", "")
		# issue番号またはdirect_interview番号を抽出
		m = re.search(r"/(issue|direct_interview)/(\d+)", href)
		if not m:
			continue
		issue_type = m.group(1)
		issue_id = m.group(2)
		url = BASE_ORIGIN + href

		# タイトル: <p class="_title_1ws1l_21"> または <p class="_title_1fzqm_21">
		title_p = a.find("p", class_=re.compile(r"_title_\w+_\d+"))
		title = title_p.get_text(strip=True) if title_p else ""

		# 説明文は新UIにはないが、カード内のテキストを探す
		description = ""
		
		# ラベル（NEW/インタビューなど）: <span class="_label_...">
		labels = []
		for span in a.find_all("span", class_=re.compile(r"_label_")):
			label_text = span.get_text(strip=True)
			if label_text and label_text not in ["NEW"]:  # NEWは別途処理
				labels.append(label_text)

		# NEWラベルの有無
		new_label = a.find("span", string=re.compile(r"^\s*NEW\s*$"))
		if new_label:
			labels.insert(0, "NEW")

		# 募集期間: "募集期間：2025/08/31 〜 2025/09/07" のようなテキスト
		created = ""
		due = ""
		period_text = ""
		for p in a.find_all("p"):
			text = p.get_text(strip=True)
			if "募集期間：" in text:
				period_text = text.replace("募集期間：", "").strip()
				# 期間から締切日を抽出（終了日）
				period_match = re.search(r"〜\s*(\d{4}/\d{2}/\d{2})", period_text)
				if period_match:
					due = period_match.group(1)
				# 開始日も取得
				start_match = re.search(r"(\d{4}/\d{2}/\d{2})\s*〜", period_text)
				if start_match:
					created = start_match.group(1)
				break

		# 報酬: <i class="i-mdi-cash-multiple">の親要素のテキスト
		reward = ""
		cash_icon = a.find("i", class_="i-mdi-cash-multiple")
		if cash_icon:
			# 親のpタグからテキストを取得
			parent_p = cash_icon.find_parent("p")
			if parent_p:
				reward_text = parent_p.get_text(strip=True)
				# "1.5万円 〜 2.5万円 （税抜）" のような形式を抽出
				reward_match = re.search(r"([\d.]+万円.*?（税抜）)", reward_text)
				if reward_match:
					reward = reward_match.group(1)

		# 時間: <i class="i-mdi-clock-time-five-outline">の親要素
		meeting_time = ""
		time_icon = a.find("i", class_="i-mdi-clock-time-five-outline")
		if time_icon:
			parent_p = time_icon.find_parent("p")
			if parent_p:
				meeting_time = parent_p.get_text(strip=True)

		# 人数: <i class="i-mdi-person">の親要素
		num_people = ""
		person_icon = a.find("i", class_="i-mdi-person")
		if person_icon:
			parent_p = person_icon.find_parent("p")
			if parent_p:
				num_people = parent_p.get_text(strip=True)

		# 場所: <i class="i-mdi-map-marker">の親要素
		location = ""
		location_icon = a.find("i", class_="i-mdi-map-marker")
		if location_icon:
			parent_p = location_icon.find_parent("p")
			if parent_p:
				location = parent_p.get_text(strip=True)

		# 会社情報: <div class="_company_info_...">
		company_info = ""
		company_div = a.find("div", class_=re.compile(r"_company_info_"))
		if company_div:
			company_info = company_div.get_text(strip=True)
			# これを説明文として使用
			if not description:
				description = company_info

		items.append({
			"id": issue_id,
			"url": url,
			"title": title,
			"description": description,
			"labels": labels,
			"created": created,   # 募集開始日
			"due": due,           # 募集終了日
			"reward": reward,     # 報酬額
			"meeting_time": meeting_time,  # 面談時間
			"num_people": num_people,      # 募集人数
			"location": location,           # 場所
			"company_info": company_info,  # 会社情報
		})
		
	return items


def fetch_issue_urls_from_sitemap() -> list[dict]:
	"""sitemap_issues.xml から issue のURLと最終更新日を取得。
	戻り値: [{id, url, lastmod}] のリスト
	"""
	sm_url = f"{BASE_ORIGIN}/sitemap_issues.xml"
	resp = requests.get(sm_url, headers=HEADERS, timeout=20)
	if resp.status_code != 200 or not resp.text:
		return []
	try:
		root = ET.fromstring(resp.text)
		ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
		entries = []
		for url_el in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
			loc_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
			lastmod_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
			if loc_el is None:
				continue
			url = loc_el.text.strip()
			m = re.search(r"/issue/(\d+)/?", url)
			if not m:
				continue
			issue_id = m.group(1)
			lastmod = (lastmod_el.text.strip() if lastmod_el is not None else "")
			entries.append({"id": issue_id, "url": url, "lastmod": lastmod})
		return entries
	except Exception:
		return []


def build_items_from_sitemap(max_fetch: int = 30) -> list[dict]:
	"""sitemap_issues.xml を基に、各案件ページの <title> からタイトルを取得。
	robots.txt の Crawl-delay=1 を尊重して1秒間隔で取得します。
	"""
	entries = fetch_issue_urls_from_sitemap()
	if not entries:
		return []
	# lastmod 降順で新しいものから取得
	def to_key(e: dict) -> str:
		return e.get("lastmod", "")
	entries.sort(key=to_key, reverse=True)
	selected = entries[:max_fetch]
	items: list[dict] = []
	for i, ent in enumerate(selected, 1):
		url = ent["url"]
		try:
			resp = requests.get(url, headers=HEADERS, timeout=20)
			if resp.status_code != 200:
				continue
			soup = BeautifulSoup(resp.text, "html.parser")
			# <title>XXX | スポットコンサル[ビザスク]
			title_tag = soup.find("title")
			title = title_tag.get_text(strip=True) if title_tag else ""
			# サイト名のサフィックスは落とす
			title = re.sub(r"\s*\|\s*.*$", "", title)
			# タイトルが空の場合のフォールバック
			if not title:
				title = f"Issue {ent['id']}"
			# デバッグ: タイトル取得状況を出力
			if i <= 3:
				print(f"[DEBUG] {ent['id']}: title='{title}'")
			
			# meta descriptionを取得
			description = ""
			meta_desc = soup.find("meta", attrs={"name": "description"})
			if not meta_desc:
				meta_desc = soup.find("meta", attrs={"property": "og:description"})
			if meta_desc:
				description = meta_desc.get("content", "")
			
			# 一覧と同様のliから情報を補足（SSRされていれば拾える）
			reward = ""
			due = ""
			created = ent.get("lastmod", "")
			# 個別ページの詳細情報を抽出
			for li in soup.find_all("li"):
				qa = li.get("qa-content", "")
				txt = li.get_text(" ", strip=True)
				icon = li.find("i")
				icon_classes = " ".join(icon.get("class", [])) if icon else ""
				if qa == "created":
					created = txt.replace("作成日:", "").strip()
				if qa == "due-date" or "i-mdi-calendar-month" in icon_classes or "i-mdi-calendar" in icon_classes:
					sp = li.find("span")
					due = (sp.get_text(" ", strip=True) if sp else txt).strip()
				if "¥" in txt or "i-mdi-tag" in icon_classes:
					mny = re.search(r"¥[\d,]+(?:\s*〜\s*¥[\d,]+)?", txt)
					reward = (mny.group(0) if mny else txt).strip()
			items.append({
				"id": ent["id"],
				"url": url,
				"title": title,
				"description": description,
				"labels": [],
				"created": created,
				"due": due,
				"reward": reward,
			})
		finally:
			# Crawl-delay
			time.sleep(1)
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
	# 一覧がJS描画等で0件の場合、sitemapフォールバック
	if not items:
		print("[INFO] 一覧から抽出0件。sitemap経由で取得します…")
		items = build_items_from_sitemap(max_fetch=50)
	# テスト時は既読を無視
	effective_seen = set() if FORCE_NOTIFY else seen
	matches = filter_new_and_match(items, effective_seen)

	# 一致があったときのみ通知
	if matches:
		payload = build_slack_blocks(matches)
		if DRY_RUN:
			print(json.dumps(payload, ensure_ascii=False, indent=2))
		else:
			post_to_slack(payload)
		# 通知したIDを既読に追加（FORCE時はスキップ）
		if not FORCE_NOTIFY:
			for it in matches:
				seen.add(it["id"])
			save_seen_ids(seen)
	else:
		print("一致なし：Slack通知はスキップしました。")


if __name__ == "__main__":
	main()


