#!/usr/bin/env python3
"""
kuma-master-data / dividend マスターデータ定期更新スクリプト (Gemini API 構造化出力)

- scripts/dividend_tickers.json の対象銘柄 (JP / US) をチャンク分割 (20〜30銘柄) で処理
- google-genai 公式 SDK + Pydantic スキーマで JSON 出力を厳密に固定
- サニティチェック（利回り 0〜25%、必須項目・型・月範囲・前値差分検証）を実施し、
  異常検知や通信失敗時は前回生成済みの dividend/dividend_master.json のレコードを維持（フォールバック）
- ログおよびサマリー出力に API キーや機密情報を一切出力しない
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent
TICKERS_PATH = ROOT_DIR / "scripts" / "dividend_tickers.json"
MASTER_PATH = ROOT_DIR / "dividend" / "dividend_master.json"
META_PATH = ROOT_DIR / "dividend" / "meta.json"

DEFAULT_MODEL = "gemini-3.8-flash"
SOURCE_NOTE_JA = (
    "本データはAIによる自動収集・集計結果であり、正確性を保証するものではありません。"
    "投資判断や確定申告の際は必ず各企業のIR情報・証券会社の報告書をご確認ください。"
)
SOURCE_NOTE_EN = (
    "This data is automatically collected and aggregated by AI and accuracy is not guaranteed. "
    "Please always verify with official corporate IR disclosures and brokerage statements "
    "before making investment decisions or filing taxes."
)
JST = timezone(timedelta(hours=9), name="JST")


class DividendStockItem(BaseModel):
    ticker: str = Field(..., description="証券コードまたはティッカー。日本株は .T 付与 (例: 8306.T, VYM)")
    market: str = Field(..., description="市場区分。JP または US")
    name_ja: str = Field(..., description="銘柄名（日本語）")
    name_en: str = Field(..., description="銘柄名（英語）")
    sector: str = Field(..., description="業種・セクター（日本語）")
    annual_dividend_forecast: float = Field(..., description="1株あたり年間予想配当額（JPは円、USは米ドル）")
    currency: str = Field(..., description="通貨コード。JPは JPY、USは USD")
    dividend_yield_percent: float = Field(..., description="参考配当利回り（%）。0〜25の範囲")
    ex_dividend_months: list[int] = Field(..., description="権利確定月の配列（1〜12の整数、例: [3, 9]）")
    payment_months: list[int] = Field(..., description="配当支払予定月の配列（1〜12の整数、例: [6, 12]）")
    consecutive_years: Optional[int] = Field(default=None, description="連続増配・非減配年数（不明時は null）")
    ai_summary_ja: str = Field(..., description="配当方針や直近の増配・減配動向の簡潔な要約（日本語100文字以内）")


class DividendBatchResponse(BaseModel):
    stocks: list[DividendStockItem] = Field(..., description="対象銘柄の配当マスター情報配列")


def redact_secrets(text: str, api_key: str | None = None) -> str:
    """API キーやシークレットらしき文字列をログ出力前に確実にマスクする。"""
    masked = str(text)
    if api_key:
        masked = masked.replace(api_key, "***REDACTED***")
    # Google API Key pattern fallback
    masked = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "AIza***REDACTED***", masked)
    masked = re.sub(r"(?i)(api[_-]?key|token|authorization)\s*[:=]\s*[^\s,;\"']+", r"\1=***REDACTED***", masked)
    return masked


def load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_summary(summary: str) -> str:
    cleaned = re.sub(r"\s+", " ", (summary or "").strip())
    if len(cleaned) > 100:
        cleaned = cleaned[:99].rstrip() + "…"
    return cleaned


def validate_months(raw_months: Any, field_name: str, allow_empty: bool) -> tuple[list[int] | None, str | None]:
    if not isinstance(raw_months, list):
        return None, f"{field_name} が配列ではありません"
    cleaned: list[int] = []
    seen: set[int] = set()
    for m in raw_months:
        if isinstance(m, bool) or not isinstance(m, int):
            return None, f"{field_name} に整数以外の要素が含まれています"
        if m < 1 or m > 12:
            return None, f"{field_name} に範囲外 (1..12) の月 {m} が含まれています"
        if m not in seen:
            seen.add(m)
            cleaned.append(m)
    if not cleaned and not allow_empty:
        return None, f"{field_name} が空です"
    return sorted(cleaned), None


def validate_and_normalize_record(
    item: DividendStockItem,
    spec: dict[str, Any],
    prev: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """
    Gemini が返した1銘柄分のレコードを検証し、正規化済み辞書を返す。
    Returns: (normalized_record_or_None, error_reason_or_None, diff_note_or_None)
    """
    expected_ticker = str(spec["ticker"]).strip()
    expected_market = str(spec["market"]).strip().upper()
    expected_currency = "JPY" if expected_market == "JP" else "USD"

    if item.ticker.strip().upper() != expected_ticker.upper():
        return None, f"ticker 不一致 ({item.ticker} != {expected_ticker})", None

    market = item.market.strip().upper()
    if market != expected_market:
        return None, f"market 不一致 ({market} != {expected_market})", None

    currency = item.currency.strip().upper()
    if currency != expected_currency:
        return None, f"currency 不一致 ({currency} != {expected_currency})", None

    name_ja = (item.name_ja or "").strip() or str(spec.get("name_ja_hint") or "").strip()
    name_en = (item.name_en or "").strip() or str(spec.get("name_en_hint") or "").strip()
    sector = (item.sector or "").strip() or str(spec.get("sector_hint") or "その他").strip()
    if not name_ja or not name_en or not sector:
        return None, "銘柄名またはセクターが空です", None

    forecast = float(item.annual_dividend_forecast)
    if not math.isfinite(forecast) or forecast < 0.0:
        return None, f"annual_dividend_forecast が不正です ({forecast})", None

    max_forecast = 50000.0 if expected_currency == "JPY" else 500.0
    if forecast > max_forecast:
        return None, f"annual_dividend_forecast が上限超過です ({forecast} {expected_currency})", None

    yield_pct = float(item.dividend_yield_percent)
    if not math.isfinite(yield_pct) or yield_pct < 0.0 or yield_pct > 25.0:
        return None, f"dividend_yield_percent が異常値範囲外です ({yield_pct}%)", None

    allow_empty_months = forecast == 0.0
    ex_months, ex_err = validate_months(item.ex_dividend_months, "ex_dividend_months", allow_empty_months)
    if ex_err or ex_months is None:
        return None, ex_err, None

    pay_months, pay_err = validate_months(item.payment_months, "payment_months", allow_empty_months)
    if pay_err or pay_months is None:
        return None, pay_err, None

    consecutive_years = item.consecutive_years
    if consecutive_years is not None:
        if isinstance(consecutive_years, bool) or not isinstance(consecutive_years, int):
            return None, "consecutive_years が整数ではありません", None
        if consecutive_years < 0 or consecutive_years > 100:
            return None, f"consecutive_years が範囲外です ({consecutive_years})", None

    summary_ja = normalize_summary(item.ai_summary_ja)
    if not summary_ja:
        return None, "ai_summary_ja が空です", None

    forecast_rounded = round(forecast, 2 if expected_currency == "JPY" else 4)
    yield_rounded = round(yield_pct, 2)

    # 前回データとの差分チェック
    diff_note: str | None = None
    if prev is not None:
        prev_forecast = float(prev.get("annual_dividend_forecast") or 0.0)
        prev_yield = float(prev.get("dividend_yield_percent") or 0.0)
        if prev_forecast > 0.0:
            ratio = forecast_rounded / prev_forecast
            if ratio > 4.0:
                return (
                    None,
                    f"前回予想比で極端な急増を検知 ({prev_forecast} -> {forecast_rounded}, x{ratio:.2f})",
                    None,
                )
            rel_diff = abs(forecast_rounded - prev_forecast) / prev_forecast
            if rel_diff >= 0.30:
                diff_note = (
                    f"{expected_ticker}: 年間配当予想が大きく変化 "
                    f"({prev_forecast} -> {forecast_rounded} {expected_currency})"
                )
        if prev_yield > 0.0 and abs(yield_rounded - prev_yield) >= 4.0:
            note = f"{expected_ticker}: 利回り変化大 ({prev_yield:.2f}% -> {yield_rounded:.2f}%)"
            diff_note = f"{diff_note}; {note}" if diff_note else note

    record = {
        "ticker": expected_ticker,
        "market": expected_market,
        "name_ja": name_ja,
        "name_en": name_en,
        "sector": sector,
        "annual_dividend_forecast": forecast_rounded,
        "currency": expected_currency,
        "dividend_yield_percent": yield_rounded,
        "ex_dividend_months": ex_months,
        "payment_months": pay_months,
        "consecutive_years": consecutive_years,
        "ai_summary_ja": summary_ja,
    }
    return record, None, diff_note


def build_prompt(chunk_specs: list[dict[str, Any]], prev_by_ticker: dict[str, dict[str, Any]], as_of_jst: str) -> str:
    payload_items = []
    for spec in chunk_specs:
        t = spec["ticker"]
        prev = prev_by_ticker.get(t)
        entry: dict[str, Any] = {
            "ticker": t,
            "market": spec["market"],
            "expected_currency": "JPY" if spec["market"] == "JP" else "USD",
            "name_ja_hint": spec.get("name_ja_hint", ""),
            "name_en_hint": spec.get("name_en_hint", ""),
            "sector_hint": spec.get("sector_hint", ""),
        }
        if prev:
            entry["previous_record"] = {
                "annual_dividend_forecast": prev.get("annual_dividend_forecast"),
                "dividend_yield_percent": prev.get("dividend_yield_percent"),
                "ex_dividend_months": prev.get("ex_dividend_months"),
                "payment_months": prev.get("payment_months"),
                "consecutive_years": prev.get("consecutive_years"),
            }
        payload_items.append(entry)

    return (
        f"基準日: {as_of_jst}\n"
        "あなたは日本株および米国株・ETFの配当マスターデータを整備する金融データアナリストです。\n"
        "以下の対象銘柄リストについて、最新の予想配当・権利確定月・支払月・セクター・連続増配/非減配年数・配当方針要約を\n"
        "指定の JSON スキーマに厳密に従って出力してください。\n\n"
        "厳守ルール:\n"
        "1. 入力リストの全銘柄を漏れなく・重複なく出力し、ticker / market / currency は入力指定と完全一致させてください。\n"
        "2. 日本株 (market=JP) は currency=\"JPY\"、1株あたり年間予想配当額（株式分割調整後）を円単位で出力してください。\n"
        "3. 米国株・ETF (market=US) は currency=\"USD\"、1株あたり年間予想配当額を米ドル単位で出力してください。\n"
        "4. dividend_yield_percent はパーセント数値（例: 3.55）で 0.0〜25.0 の妥当な範囲で出力してください。\n"
        "5. ex_dividend_months（権利確定月）と payment_months（支払予定月）は 1〜12 の整数配列で出力してください。\n"
        "6. consecutive_years は連続増配（または非減配）年数の整数、不明やETFの場合は null を設定してください。\n"
        "7. ai_summary_ja は配当方針・直近の増配/減配動向・注意点を日本語100文字以内で簡潔に要約してください。\n"
        "8. previous_record が参考値としてある場合は株式分割や最新の増減配を踏まえつつ整合性を確認してください。\n\n"
        f"対象銘柄リスト ({len(payload_items)}件):\n"
        f"{json.dumps(payload_items, ensure_ascii=False, indent=2)}"
    )


def call_gemini_chunk(
    client: Any,
    model_name: str,
    chunk_specs: list[dict[str, Any]],
    prev_by_ticker: dict[str, dict[str, Any]],
    as_of_jst: str,
    api_key: str,
    max_retries: int = 4,
    base_backoff: float = 5.0,
) -> list[DividendStockItem]:
    from google.genai import types

    prompt = build_prompt(chunk_specs, prev_by_ticker, as_of_jst)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=DividendBatchResponse,
        temperature=0.1,
    )

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, DividendBatchResponse):
                return parsed.stocks
            if isinstance(parsed, dict):
                return DividendBatchResponse.model_validate(parsed).stocks

            raw_text = (getattr(response, "text", None) or "").strip()
            if not raw_text:
                raise ValueError("Gemini API レスポンスが空でした")
            return DividendBatchResponse.model_validate_json(raw_text).stocks
        except Exception as exc:
            last_exc = exc
            safe_err = redact_secrets(str(exc), api_key)
            if attempt + 1 >= max_retries:
                raise RuntimeError(safe_err) from None
            delay = base_backoff * (2 ** attempt) + random.uniform(0.3, 1.5)
            print(
                f"WARN: チャンク取得リトライ ({attempt + 1}/{max_retries - 1}) "
                f"待機 {delay:.1f}s - 理由: {safe_err}",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(redact_secrets(str(last_exc or "Unknown error"), api_key))


def write_summary_markdown(
    summary_path: Path,
    now_jst_iso: str,
    model_name: str,
    total_target: int,
    updated_count: int,
    unchanged_count: int,
    fallback_count: int,
    anomalies: list[str],
    diff_notes: list[str],
    dry_run: bool,
) -> None:
    lines = [
        f"## 🐯 簡単配当日記 配当マスター更新レポート ({now_jst_iso})",
        "",
        f"- **モデル**: `{model_name}`",
        f"- **実行モード**: {'ドライラン (ファイル非変更)' if dry_run else '本番更新'}",
        f"- **対象銘柄数**: {total_target} 件",
        f"- **更新・差分反映**: {updated_count} 件 / **変更なし**: {unchanged_count} 件 / **前回値フォールバック**: {fallback_count} 件",
        "",
    ]
    if diff_notes:
        lines.append("### 主な配当予想・利回りの変動検知")
        for note in diff_notes[:30]:
            lines.append(f"- {note}")
        lines.append("")
    if anomalies:
        lines.append("### サニティチェック・取得エラー（前回値を維持）")
        for item in anomalies[:30]:
            lines.append(f"- {item}")
        lines.append("")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update dividend_master.json via Gemini API.")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL), help="Gemini model name")
    parser.add_argument("--chunk-size", type=int, default=25, help="Tickers per API call (1..30)")
    parser.add_argument("--sleep-seconds", type=float, default=4.0, help="Sleep seconds between chunks")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of tickers to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output files")
    parser.add_argument("--force-write", action="store_true", help="Write files even if stocks array is unchanged")
    parser.add_argument("--summary-path", default="", help="Optional path to write Markdown summary")
    args = parser.parse_args()

    chunk_size = max(1, min(30, args.chunk_size))
    sleep_seconds = max(0.0, args.sleep_seconds)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: 環境変数 GEMINI_API_KEY が設定されていません。", file=sys.stderr)
        return 1

    if not TICKERS_PATH.exists():
        print(f"ERROR: 対象銘柄リストが見つかりません: {TICKERS_PATH}", file=sys.stderr)
        return 1

    tickers_payload = json.loads(TICKERS_PATH.read_text(encoding="utf-8"))
    raw_specs = tickers_payload.get("tickers", [])
    if not isinstance(raw_specs, list) or not raw_specs:
        print("ERROR: dividend_tickers.json に有効な銘柄リストがありません。", file=sys.stderr)
        return 1

    target_specs: list[dict[str, Any]] = raw_specs[: args.limit] if args.limit and args.limit > 0 else raw_specs
    existing_master = load_json_if_exists(MASTER_PATH)
    prev_stocks_list = existing_master.get("stocks", []) if isinstance(existing_master, dict) else []
    prev_by_ticker: dict[str, dict[str, Any]] = {
        str(s["ticker"]).upper(): s for s in prev_stocks_list if isinstance(s, dict) and s.get("ticker")
    }
    prev_by_exact_ticker: dict[str, dict[str, Any]] = {
        str(s["ticker"]): s for s in prev_stocks_list if isinstance(s, dict) and s.get("ticker")
    }

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    now_utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_jst_iso = now_jst.isoformat(timespec="seconds")

    from google import genai

    client = genai.Client(api_key=api_key)

    result_by_ticker: dict[str, dict[str, Any]] = {}
    anomalies: list[str] = []
    diff_notes: list[str] = []
    chunk_failures = 0
    total_chunks = (len(target_specs) + chunk_size - 1) // chunk_size

    for chunk_idx in range(total_chunks):
        start = chunk_idx * chunk_size
        chunk_specs = target_specs[start : start + chunk_size]
        print(f"[{chunk_idx + 1}/{total_chunks}] {len(chunk_specs)} 銘柄を取得中...")

        returned_by_ticker: dict[str, DividendStockItem] = {}
        try:
            items = call_gemini_chunk(
                client=client,
                model_name=args.model,
                chunk_specs=chunk_specs,
                prev_by_ticker=prev_by_exact_ticker,
                as_of_jst=now_jst_iso,
                api_key=api_key,
            )
            for item in items:
                key = item.ticker.strip().upper()
                if key not in returned_by_ticker:
                    returned_by_ticker[key] = item
        except Exception as exc:
            chunk_failures += 1
            safe_msg = redact_secrets(str(exc), api_key)
            anomalies.append(f"チャンク {chunk_idx + 1}/{total_chunks} 失敗 ({safe_msg})")
            print(f"WARN: チャンク {chunk_idx + 1} 失敗: {safe_msg}", file=sys.stderr)

        for spec in chunk_specs:
            ticker = str(spec["ticker"]).strip()
            prev = prev_by_ticker.get(ticker.upper())
            candidate = returned_by_ticker.get(ticker.upper())

            if candidate is None:
                if prev is not None:
                    result_by_ticker[ticker] = copy.deepcopy(prev)
                    anomalies.append(f"{ticker}: 応答欠落のため前回データを維持")
                else:
                    anomalies.append(f"{ticker}: 応答欠落（前回データなし）")
                continue

            normalized, err_reason, diff_note = validate_and_normalize_record(candidate, spec, prev)
            if err_reason or normalized is None:
                anomalies.append(f"{ticker}: サニティチェック異常 ({err_reason}) -> 前回データを維持")
                if prev is not None:
                    result_by_ticker[ticker] = copy.deepcopy(prev)
                continue

            if diff_note:
                diff_notes.append(diff_note)
            result_by_ticker[ticker] = normalized

        if chunk_idx + 1 < total_chunks and sleep_seconds > 0:
            time.sleep(sleep_seconds)

    # limit 指定時は既存他銘柄を保持し、全件実行時はユニバース順に並べる
    ordered_stocks: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for spec in raw_specs:
        t = str(spec["ticker"]).strip()
        if t in result_by_ticker:
            ordered_stocks.append(result_by_ticker[t])
            seen_tickers.add(t.upper())
        elif t in prev_by_exact_ticker:
            ordered_stocks.append(copy.deepcopy(prev_by_exact_ticker[t]))
            seen_tickers.add(t.upper())

    for prev_item in prev_stocks_list:
        if isinstance(prev_item, dict) and prev_item.get("ticker"):
            t_upper = str(prev_item["ticker"]).upper()
            if t_upper not in seen_tickers:
                ordered_stocks.append(copy.deepcopy(prev_item))
                seen_tickers.add(t_upper)

    updated_count = 0
    unchanged_count = 0
    fallback_count = 0
    for stock in ordered_stocks:
        t = str(stock["ticker"])
        prev = prev_by_ticker.get(t.upper())
        if prev is None:
            updated_count += 1
        elif stock == prev:
            if any(a.startswith(f"{t}:") for a in anomalies):
                fallback_count += 1
            else:
                unchanged_count += 1
        else:
            updated_count += 1

    stocks_changed = (ordered_stocks != prev_stocks_list) or (not MASTER_PATH.exists())
    jp_count = sum(1 for s in ordered_stocks if s.get("market") == "JP")
    us_count = sum(1 for s in ordered_stocks if s.get("market") == "US")

    new_master = {
        "schemaVersion": 1,
        "updatedAt": now_utc_iso if (stocks_changed or args.force_write) else existing_master.get("updatedAt", now_utc_iso),
        "meta": {
            "schema_version": "1.0",
            "updated_at": now_jst_iso
            if (stocks_changed or args.force_write)
            else existing_master.get("meta", {}).get("updated_at", now_jst_iso),
            "source_note": SOURCE_NOTE_JA,
            "source_note_en": SOURCE_NOTE_EN,
            "total_stocks": len(ordered_stocks),
        },
        "stocks": ordered_stocks,
    }

    new_meta = {
        "schemaVersion": 1,
        "updatedAt": new_master["updatedAt"],
        "schema_version": "1.0",
        "updated_at": new_master["meta"]["updated_at"],
        "source_note": SOURCE_NOTE_JA,
        "source_note_en": SOURCE_NOTE_EN,
        "total_stocks": len(ordered_stocks),
        "jp_stocks": jp_count,
        "us_stocks": us_count,
        "data_file": "dividend/dividend_master.json",
    }

    if not args.dry_run and (stocks_changed or args.force_write or not META_PATH.exists()):
        MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        MASTER_PATH.write_text(json.dumps(new_master, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        META_PATH.write_text(json.dumps(new_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.summary_path:
        write_summary_markdown(
            summary_path=Path(args.summary_path),
            now_jst_iso=now_jst_iso,
            model_name=args.model,
            total_target=len(target_specs),
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            fallback_count=fallback_count,
            anomalies=anomalies,
            diff_notes=diff_notes,
            dry_run=args.dry_run,
        )

    print(
        f"Done: total={len(ordered_stocks)} (JP={jp_count}, US={us_count}), "
        f"updated={updated_count}, unchanged={unchanged_count}, fallback={fallback_count}, "
        f"changed={stocks_changed}, chunk_failures={chunk_failures}"
    )

    if chunk_failures == total_chunks and total_chunks > 0:
        print("ERROR: すべてのチャンク取得に失敗しました。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
