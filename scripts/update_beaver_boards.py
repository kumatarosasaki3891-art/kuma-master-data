#!/usr/bin/env python3
"""
kuma-master-data / beaver マザーボードマスターデータ生成・更新スクリプト (Gemini API 構造化出力)

- beaver/boards.json の対象基板リストを読み込み、スペックページ取得（または raw_spec_text 優先利用）
- google-genai SDK (gemini-2.5-flash) + Pydantic スキーマで物理スロット数・規格を JSON 抽出
- 厳格な型・範囲バリデーションを実施し、取得失敗や検証エラー時は前回生成済みのレコードを保持
- ログおよびサマリー出力に API キーや機密情報を一切出力しない
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Optional
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent
BOARDS_PATH = ROOT_DIR / "beaver" / "boards.json"
MASTER_PATH = ROOT_DIR / "beaver" / "motherboard_master.json"
META_PATH = ROOT_DIR / "beaver" / "meta.json"

DEFAULT_MODEL = "gemini-2.5-flash"
SCHEMA_VERSION = 1
MAX_SPEC_TEXT_CHARS = 45000
JST = timezone(timedelta(hours=9), name="JST")

VALID_FORM_FACTORS = ("ATX", "Micro-ATX", "Mini-ITX", "E-ATX")
STANDARD_SIZES_MM: dict[str, tuple[int, int]] = {
    "ATX": (305, 244),
    "Micro-ATX": (244, 244),
    "Mini-ITX": (170, 170),
    "E-ATX": (305, 330),
}

SLOT_RANGES: dict[str, tuple[int, int]] = {
    "cpu_socket": (1, 1),
    "cpu_8pin": (1, 4),
    "atx_24pin": (1, 1),
    "dimm": (1, 8),
    "pcie_x16": (1, 4),
    "pcie_x1": (0, 6),
    "m2": (0, 6),
    "sata": (0, 12),
}


class ExtractedSlots(BaseModel):
    cpu_socket: int = Field(..., description="物理CPUソケット数（通常1）")
    cpu_8pin: int = Field(..., description="物理CPU補助電源コネクタ数（8-pin/4-pin EPSヘッダの本数、通常1〜2）")
    atx_24pin: int = Field(..., description="ATX 24ピンメイン電源コネクタ数（通常1）")
    dimm: int = Field(..., description="物理メモリスロット（DIMM）数（例: 2, 4, 8）")
    pcie_x16: int = Field(..., description="物理PCIe x16形状スロット数（帯域やx4動作に関わらずx16長スロットをカウント）")
    pcie_x1: int = Field(..., description="物理PCIe x1スロット数（x4形状等を含む場合は物理数に合わせる）")
    m2: int = Field(..., description="物理M.2スロット総数（排他注記は無視し、物理コネクタ数をカウント）")
    sata: int = Field(..., description="物理SATA 6Gb/sポート総数（排他無効化注記は無視し、物理ポート数をカウント）")


class ExtractedBoardSpec(BaseModel):
    form_factor: str = Field(..., description="フォームファクタ: ATX, Micro-ATX, Mini-ITX, E-ATX のいずれか")
    width_mm: Optional[int] = Field(default=None, description="基板高さ/長辺または仕様上の幅(mm)。ATXは305等")
    height_mm: Optional[int] = Field(default=None, description="基板奥行/短辺(mm)。ATXは244等")
    socket: str = Field(..., description="CPUソケット規格 (例: AM5, LGA1700, LGA1851)")
    chipset: str = Field(..., description="チップセット名 (例: B650E, Z790, X870)")
    memory_type: str = Field(..., description="対応メモリ規格 (例: DDR5, DDR4)")
    slots: ExtractedSlots = Field(..., description="基板上の物理スロット数")


class _FallbackTextParser(HTMLParser):
    """BeautifulSoup 未導入環境向けの簡易 HTML テキスト抽出器。"""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(self._chunks)


def redact_secrets(text: str, api_key: str | None = None) -> str:
    """API キーやシークレットらしき文字列をログ出力前にマスクする。"""
    masked = str(text)
    if api_key:
        masked = masked.replace(api_key, "***REDACTED***")
    masked = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "AIza***REDACTED***", masked)
    masked = re.sub(r"(?i)(api[_-]?key|token|authorization)\s*[:=]\s*[^\s,;\"']+", r"\1=***REDACTED***", masked)
    return masked


def is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_json_if_exists(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def html_to_clean_text(html_body: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            tag.decompose()
        raw_text = soup.get_text(separator="\n")
    except ImportError:
        parser = _FallbackTextParser()
        parser.feed(html_body)
        raw_text = parser.text()

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def fetch_url_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    try:
        import httpx  # type: ignore

        with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} ({url})")
            return resp.text
    except ImportError:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status} ({url})")
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} ({url})") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"URL取得エラー ({exc.reason})") from None


def acquire_spec_text(entry: dict[str, Any]) -> tuple[str, str]:
    """
    raw_spec_text が設定されていれば HTTP フェッチをスキップして優先利用する。
    Returns: (spec_text, source_kind)
    """
    raw_override = str(entry.get("raw_spec_text") or "").strip()
    if raw_override:
        return raw_override[:MAX_SPEC_TEXT_CHARS], "raw_spec_text"

    spec_url = str(entry.get("spec_url") or "").strip()
    if not spec_url:
        raise RuntimeError("spec_url と raw_spec_text が両方空です")

    html_body = fetch_url_html(spec_url)
    cleaned = html_to_clean_text(html_body)
    lowered = cleaned.lower()

    block_markers = (
        "access denied",
        "attention required!",
        "enable javascript and cookies to continue",
        "just a moment...",
        "403 forbidden",
    )
    if len(cleaned) < 120 or (len(cleaned) < 800 and any(m in lowered for m in block_markers)):
        raise RuntimeError(
            "スペック本文を十分に取得できませんでした（SPA描画または403ブロックの可能性）。"
            "boards.json の raw_spec_text に仕様テキストを設定してください。"
        )

    return cleaned[:MAX_SPEC_TEXT_CHARS], "http_fetch"


def build_prompt(entry: dict[str, Any], spec_text: str) -> str:
    maker = str(entry.get("maker") or "").strip()
    model = str(entry.get("model") or "").strip()
    spec_url = str(entry.get("spec_url") or "").strip()

    return (
        "あなたは自作PC向けマザーボード仕様を正確に構造化するハードウェアデータエンジニアです。\n"
        "以下のマザーボード公式スペックテキストから仕様を抽出し、指定の JSON スキーマに厳密に従って出力してください。\n\n"
        "厳守ルール:\n"
        "1. 物理的に基板上に存在するスロット・コネクタ数を正確にカウントすること。\n"
        "   「M.2_3 を使用すると SATA6G_5/6 は無効」「PCIe x16_2 は x4 動作」などの排他・帯域共有の注釈は無視し、物理コネクタ本数のみを数えること。\n"
        "2. form_factor は必ず \"ATX\", \"Micro-ATX\", \"Mini-ITX\", \"E-ATX\" のいずれかに正規化すること。\n"
        "3. サイズ（width_mm, height_mm）が本文に明記されていない場合は、フォームファクタの標準サイズを補完すること：\n"
        "   - ATX: 305 x 244\n"
        "   - Micro-ATX: 244 x 244\n"
        "   - Mini-ITX: 170 x 170\n"
        "   - E-ATX: 305 x 330\n"
        "4. slots の各項目ルール:\n"
        "   - cpu_socket: 1\n"
        "   - cpu_8pin: 基板上の CPU 補助電源コネクタ（8-pin / 4-pin ProCool/EPS）の本数（例: 8-pin + 8-pin なら 2、8-pin のみなら 1）\n"
        "   - atx_24pin: 1\n"
        "   - dimm: メモリスロット総数（例: 4 または 2）\n"
        "   - pcie_x16: 物理的に x16 長形状の拡張スロット数\n"
        "   - pcie_x1: 物理的な PCIe x1（および x16 未満の小型）スロット数\n"
        "   - m2: 物理的な M.2 スロット総数\n"
        "   - sata: 物理的な SATA 6Gb/s ポート総数\n\n"
        f"対象製品: {maker} {model}\n"
        f"参照URL: {spec_url}\n\n"
        "--- スペックテキストここから ---\n"
        f"{spec_text}\n"
        "--- スペックテキストここまで ---"
    )


def call_gemini_extract(
    client: Any,
    model_name: str,
    entry: dict[str, Any],
    spec_text: str,
    api_key: str,
    max_retries: int = 4,
    base_backoff: float = 5.0,
) -> ExtractedBoardSpec:
    from google.genai import types  # type: ignore

    prompt = build_prompt(entry, spec_text)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ExtractedBoardSpec,
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
            if isinstance(parsed, ExtractedBoardSpec):
                return parsed
            if isinstance(parsed, dict):
                return ExtractedBoardSpec.model_validate(parsed)

            raw_text = (getattr(response, "text", None) or "").strip()
            if not raw_text:
                raise ValueError("Gemini API レスポンスが空でした")
            return ExtractedBoardSpec.model_validate_json(raw_text)
        except Exception as exc:
            last_exc = exc
            safe_err = redact_secrets(str(exc), api_key)
            if attempt + 1 >= max_retries:
                raise RuntimeError(safe_err) from None
            delay = base_backoff * (2 ** attempt) + random.uniform(0.3, 1.5)
            print(
                f"WARN: Gemini リトライ ({attempt + 1}/{max_retries - 1}) "
                f"待機 {delay:.1f}s - {entry.get('id')}: {safe_err}",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(redact_secrets(str(last_exc or "Unknown Gemini error"), api_key))


def validate_slots_dict(slots_dict: dict[str, Any]) -> str | None:
    for key, (min_v, max_v) in SLOT_RANGES.items():
        if key not in slots_dict:
            return f"slots.{key} が欠落しています"
        val = slots_dict[key]
        if not is_strict_int(val):
            return f"slots.{key} が整数ではありません ({val!r})"
        if val < min_v or val > max_v:
            return f"slots.{key} が許容範囲 ({min_v}..{max_v}) 外です ({val})"
    return None


def validate_and_build_record(
    extracted: ExtractedBoardSpec,
    entry: dict[str, Any],
    today_jst: str,
) -> tuple[dict[str, Any] | None, str | None]:
    board_id = str(entry["id"]).strip()
    maker = str(entry["maker"]).strip()
    model = str(entry["model"]).strip()
    source_url = str(entry.get("spec_url") or "").strip()
    is_premium = bool(entry.get("is_premium", True))

    ff = (extracted.form_factor or "").strip()
    if ff not in VALID_FORM_FACTORS:
        return None, f"form_factor が不正です ({ff!r})"

    std_w, std_h = STANDARD_SIZES_MM[ff]
    width_mm = std_w if extracted.width_mm is None else extracted.width_mm
    height_mm = std_h if extracted.height_mm is None else extracted.height_mm

    if not is_strict_int(width_mm) or width_mm < 100 or width_mm > 400:
        return None, f"width_mm が範囲外 (100..400) です ({width_mm!r})"
    if not is_strict_int(height_mm) or height_mm < 100 or height_mm > 400:
        return None, f"height_mm が範囲外 (100..400) です ({height_mm!r})"

    socket_name = (extracted.socket or "").strip()
    chipset = (extracted.chipset or "").strip()
    memory_type = (extracted.memory_type or "").strip()
    if not socket_name or not chipset or not memory_type:
        return None, "socket / chipset / memory_type のいずれかが空です"

    slots_dict = extracted.slots.model_dump()
    slot_err = validate_slots_dict(slots_dict)
    if slot_err:
        return None, slot_err

    record = {
        "id": board_id,
        "maker": maker,
        "model": model,
        "display_name": f"{maker} {model}".strip(),
        "form_factor": ff,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "socket": socket_name,
        "chipset": chipset,
        "memory_type": memory_type,
        "is_premium": is_premium,
        "slots": {
            "cpu_socket": slots_dict["cpu_socket"],
            "cpu_8pin": slots_dict["cpu_8pin"],
            "atx_24pin": slots_dict["atx_24pin"],
            "dimm": slots_dict["dimm"],
            "pcie_x16": slots_dict["pcie_x16"],
            "pcie_x1": slots_dict["pcie_x1"],
            "m2": slots_dict["m2"],
            "sata": slots_dict["sata"],
        },
        "source_url": source_url,
        "updated_at": today_jst,
    }
    return record, None


def validate_existing_board_record(rec: Any) -> str | None:
    if not isinstance(rec, dict):
        return "レコードが辞書ではありません"
    for req_str in ("id", "maker", "model", "display_name", "socket", "chipset", "memory_type", "updated_at"):
        val = rec.get(req_str)
        if not isinstance(val, str) or not val.strip():
            return f"{req_str} が空または文字列ではありません"
    if rec.get("form_factor") not in VALID_FORM_FACTORS:
        return f"form_factor が不正です ({rec.get('form_factor')!r})"
    w = rec.get("width_mm")
    h = rec.get("height_mm")
    if not is_strict_int(w) or w < 100 or w > 400:
        return f"width_mm が不正です ({w!r})"
    if not is_strict_int(h) or h < 100 or h > 400:
        return f"height_mm が不正です ({h!r})"
    if not isinstance(rec.get("is_premium"), bool):
        return "is_premium が bool ではありません"
    slots = rec.get("slots")
    if not isinstance(slots, dict):
        return "slots が辞書ではありません"
    return validate_slots_dict(slots)


def validate_boards_input(raw_boards: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_boards, list):
        raise ValueError("beaver/boards.json は配列である必要があります")
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_boards):
        if not isinstance(entry, dict):
            raise ValueError(f"boards.json[{idx}] がオブジェクトではありません")
        bid = str(entry.get("id") or "").strip()
        maker = str(entry.get("maker") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not bid or not maker or not model:
            raise ValueError(f"boards.json[{idx}] に必須項目 (id/maker/model) が不足しています")
        if bid in seen_ids:
            raise ValueError(f"boards.json に重複した id があります: {bid}")
        seen_ids.add(bid)
        validated.append(entry)
    return validated


def select_target_entries(
    entries: list[dict[str, Any]],
    prev_by_id: dict[str, dict[str, Any]],
    target_id: str,
    force_all: bool,
) -> list[dict[str, Any]]:
    tid = (target_id or "").strip()
    if tid:
        matched = [e for e in entries if str(e["id"]).strip() == tid]
        if not matched:
            raise ValueError(f"target_id '{tid}' が beaver/boards.json に存在しません")
        return matched
    if force_all:
        return list(entries)
    return [e for e in entries if str(e["id"]).strip() not in prev_by_id]


def write_markdown_report(
    report_path: Path,
    now_jst_iso: str,
    model_name: str,
    target_count: int,
    added_ids: list[str],
    updated_ids: list[str],
    fallback_ids: list[str],
    warnings: list[str],
    total_boards: int,
    dry_run: bool,
) -> None:
    lines = [
        f"## 🦫 簡単自作PC管理 マザーボードマスター更新レポート ({now_jst_iso})",
        "",
        f"- **モデル**: `{model_name}`",
        f"- **実行モード**: {'ドライラン (ファイル非変更)' if dry_run else '本番更新'}",
        f"- **今回処理対象**: {target_count} 件 / **総収録数**: {total_boards} 件",
        f"- **新規追加**: {len(added_ids)} 件 / **更新**: {len(updated_ids)} 件 / **前回値保持**: {len(fallback_ids)} 件",
        "",
    ]
    if added_ids:
        lines.append("### 新規追加")
        for bid in added_ids:
            lines.append(f"- `{bid}`")
        lines.append("")
    if updated_ids:
        lines.append("### 更新")
        for bid in updated_ids:
            lines.append(f"- `{bid}`")
        lines.append("")
    if warnings:
        lines.append("### 警告・スキップ（既存レコードを保持）")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_validate_only() -> int:
    if not BOARDS_PATH.exists():
        print(f"ERROR: {BOARDS_PATH} が存在しません", file=sys.stderr)
        return 1
    entries = validate_boards_input(json.loads(BOARDS_PATH.read_text(encoding="utf-8")))
    master = load_json_if_exists(MASTER_PATH, {})
    boards = master.get("boards", []) if isinstance(master, dict) else []
    if not isinstance(boards, list):
        print("ERROR: motherboard_master.json の boards が配列ではありません", file=sys.stderr)
        return 1
    for rec in boards:
        err = validate_existing_board_record(rec)
        if err:
            print(f"ERROR: motherboard_master.json 検証エラー ({rec.get('id')}): {err}", file=sys.stderr)
            return 1
    meta = load_json_if_exists(META_PATH, {})
    if not isinstance(meta, dict) or meta.get("app") != "EasyCustomPC":
        print("ERROR: beaver/meta.json の app が不正です", file=sys.stderr)
        return 1
    if meta.get("template_count") != len(boards):
        print(
            f"ERROR: meta.json の template_count ({meta.get('template_count')}) と boards 件数 ({len(boards)}) が不一致です",
            file=sys.stderr,
        )
        return 1
    print(f"Validation OK: boards.json={len(entries)}, motherboard_master.json={len(boards)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Update beaver/motherboard_master.json via Gemini API.")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL), help="Gemini model name")
    parser.add_argument("--target-id", default="", help="Process only a specific board id")
    parser.add_argument("--force-all", action="store_true", help="Re-fetch and overwrite all entries in boards.json")
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Sleep seconds between API requests")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output files")
    parser.add_argument("--validate-only", action="store_true", help="Validate existing JSON files and exit")
    parser.add_argument("--summary-path", default="", help="Optional path to write Markdown summary")
    parser.add_argument("--pr-body-path", default="", help="Optional path to write PR description Markdown")
    args = parser.parse_args()

    if args.validate_only:
        return run_validate_only()

    if not BOARDS_PATH.exists():
        print(f"ERROR: 入力ファイルが見つかりません: {BOARDS_PATH}", file=sys.stderr)
        return 1

    try:
        entries = validate_boards_input(json.loads(BOARDS_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"ERROR: boards.json の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    existing_master = load_json_if_exists(MASTER_PATH, {})
    prev_boards_list = existing_master.get("boards", []) if isinstance(existing_master, dict) else []
    prev_by_id: dict[str, dict[str, Any]] = {
        str(b["id"]).strip(): b for b in prev_boards_list if isinstance(b, dict) and b.get("id")
    }

    try:
        targets = select_target_entries(entries, prev_by_id, args.target_id, args.force_all)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    now_utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_jst_iso = now_jst.isoformat(timespec="seconds")
    today_jst = now_jst.strftime("%Y-%m-%d")

    result_by_id: dict[str, dict[str, Any]] = {bid: copy.deepcopy(rec) for bid, rec in prev_by_id.items()}
    added_ids: list[str] = []
    updated_ids: list[str] = []
    fallback_ids: list[str] = []
    warnings: list[str] = []

    if targets:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            print("ERROR: 環境変数 GEMINI_API_KEY が設定されていません。", file=sys.stderr)
            return 1

        from google import genai  # type: ignore

        client = genai.Client(api_key=api_key)
        sleep_seconds = max(0.0, args.sleep_seconds)

        for idx, entry in enumerate(targets):
            bid = str(entry["id"]).strip()
            prev = prev_by_id.get(bid)
            print(f"[{idx + 1}/{len(targets)}] {bid} を処理中...")

            try:
                spec_text, source_kind = acquire_spec_text(entry)
                print(f"  - スペック取得元: {source_kind} ({len(spec_text)} chars)")
                extracted = call_gemini_extract(
                    client=client,
                    model_name=args.model,
                    entry=entry,
                    spec_text=spec_text,
                    api_key=api_key,
                )
                record, val_err = validate_and_build_record(extracted, entry, today_jst)
                if val_err or record is None:
                    raise ValueError(val_err or "バリデーションエラー")

                result_by_id[bid] = record
                if prev is None:
                    added_ids.append(bid)
                elif record != prev:
                    updated_ids.append(bid)
            except Exception as exc:
                safe_msg = redact_secrets(str(exc), api_key)
                warn_msg = f"`{bid}`: 取得・検証失敗 ({safe_msg})"
                warnings.append(warn_msg)
                print(f"WARN: {warn_msg}", file=sys.stderr)
                if prev is not None:
                    result_by_id[bid] = copy.deepcopy(prev)
                    fallback_ids.append(bid)

            if idx + 1 < len(targets) and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    else:
        print("処理対象の差分はありません（すべて motherboard_master.json に登録済み）。")

    ordered_boards: list[dict[str, Any]] = []
    seen_out: set[str] = set()
    for entry in entries:
        bid = str(entry["id"]).strip()
        if bid in result_by_id:
            ordered_boards.append(result_by_id[bid])
            seen_out.add(bid)

    for prev_rec in prev_boards_list:
        if isinstance(prev_rec, dict) and prev_rec.get("id"):
            bid = str(prev_rec["id"]).strip()
            if bid not in seen_out:
                ordered_boards.append(copy.deepcopy(prev_rec))
                seen_out.add(bid)

    boards_changed = (ordered_boards != prev_boards_list) or (not MASTER_PATH.exists())
    prev_meta = load_json_if_exists(META_PATH, {})

    effective_utc = now_utc_iso if boards_changed else existing_master.get("updatedAt", now_utc_iso)
    effective_jst = (
        now_jst_iso
        if boards_changed
        else (prev_meta.get("updated_at") if isinstance(prev_meta, dict) else None) or now_jst_iso
    )

    new_master = {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": effective_utc,
        "meta": {
            "app": "EasyCustomPC",
            "schema_version": SCHEMA_VERSION,
            "updated_at": effective_jst,
            "template_count": len(ordered_boards),
        },
        "boards": ordered_boards,
    }

    new_meta = {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": effective_utc,
        "app": "EasyCustomPC",
        "schema_version": SCHEMA_VERSION,
        "updated_at": effective_jst,
        "template_count": len(ordered_boards),
        "data_file": "beaver/motherboard_master.json",
    }

    if not args.dry_run and (boards_changed or not META_PATH.exists()):
        MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        MASTER_PATH.write_text(json.dumps(new_master, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        META_PATH.write_text(json.dumps(new_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for report_arg in (args.summary_path, args.pr_body_path):
        if report_arg:
            write_markdown_report(
                report_path=Path(report_arg),
                now_jst_iso=now_jst_iso,
                model_name=args.model,
                target_count=len(targets),
                added_ids=added_ids,
                updated_ids=updated_ids,
                fallback_ids=fallback_ids,
                warnings=warnings,
                total_boards=len(ordered_boards),
                dry_run=args.dry_run,
            )

    print(
        f"Done: total={len(ordered_boards)}, targets={len(targets)}, "
        f"added={len(added_ids)}, updated={len(updated_ids)}, fallback={len(fallback_ids)}, "
        f"warnings={len(warnings)}, changed={boards_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
