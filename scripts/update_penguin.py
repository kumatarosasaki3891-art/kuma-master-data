#!/usr/bin/env python3
"""
kuma-master-data / penguin 定義データ更新スクリプト (Python 標準ライブラリのみ使用)

- penguin/engines.json: GitHub Releases API から安定版リリースを取得して更新
- penguin/runtimes.json: endoflife.date API からブランチ別最新パッチ・EOL を取得して更新
  ※ runtimes.json 内の advisories (緊急 advisory) は手動管理のため変更せず保持する。
- updatedAt を除く本文に差分がある場合のみファイルを更新する。
"""

from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
ENGINES_PATH = ROOT_DIR / "penguin" / "engines.json"
RUNTIMES_PATH = ROOT_DIR / "penguin" / "runtimes.json"

ENGINE_SOURCES = [
    {
        "id": "docker",
        "displayName": "Docker Engine",
        "apiUrl": "https://api.github.com/repos/moby/moby/releases?per_page=30",
        "sourceUrl": "https://api.github.com/repos/moby/moby/releases",
    },
    {
        "id": "podman",
        "displayName": "Podman",
        "apiUrl": "https://api.github.com/repos/containers/podman/releases?per_page=30",
        "sourceUrl": "https://api.github.com/repos/containers/podman/releases",
    },
    {
        "id": "nerdctl",
        "displayName": "nerdctl",
        "apiUrl": "https://api.github.com/repos/containerd/nerdctl/releases?per_page=30",
        "sourceUrl": "https://api.github.com/repos/containerd/nerdctl/releases",
    },
]

RUNTIME_SOURCES = [
    {"id": "nginx", "displayName": "nginx", "product": "nginx"},
    {"id": "php", "displayName": "PHP", "product": "php"},
    {"id": "mysql", "displayName": "MySQL", "product": "mysql"},
    {"id": "mariadb", "displayName": "MariaDB", "product": "mariadb"},
    {"id": "postgres", "displayName": "PostgreSQL", "product": "postgresql"},
    {"id": "redis", "displayName": "Redis", "product": "redis"},
    {"id": "httpd", "displayName": "Apache httpd", "product": "apache"},
]

PRERELEASE_PATTERN = re.compile(r"(?i)(?:^|[-._])(rc|beta|alpha|dev|pre|nightly)\d*")
VERSION_PATTERN = re.compile(r"(?:docker-)?v?(\d+\.\d+\.\d+)(?:$|[+-])")


def fetch_json(url: str, accept: str = "application/json", timeout: int = 20) -> Any:
    headers = {
        "User-Agent": "kuma-master-data-updater/1.0 (+https://github.com/kumatarosasaki3891-art/kuma-master-data)",
        "Accept": accept,
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def version_tuple(version_str: str) -> tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", version_str)]
    return tuple(parts)


def extract_engine_versions(releases_payload: list[dict[str, Any]]) -> list[str]:
    versions: list[str] = []
    seen: set[str] = set()
    for rel in releases_payload:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "").strip()
        if not tag or PRERELEASE_PATTERN.search(tag):
            continue
        match = VERSION_PATTERN.search(tag)
        if not match:
            continue
        ver = match.group(1)
        if ver not in seen:
            seen.add(ver)
            versions.append(ver)
    versions.sort(key=version_tuple, reverse=True)
    return versions[:5]


def update_engines(existing: dict[str, Any], failures: list[str], changes: list[str]) -> dict[str, Any]:
    existing_by_id = {e["id"]: e for e in existing.get("engines", []) if "id" in e}
    new_engines: list[dict[str, Any]] = []

    for spec in ENGINE_SOURCES:
        engine_id = spec["id"]
        prev = existing_by_id.get(engine_id)
        try:
            payload = fetch_json(spec["apiUrl"], accept="application/vnd.github+json")
            if not isinstance(payload, list):
                raise ValueError("Unexpected GitHub Releases response format")
            versions = extract_engine_versions(payload)
            if not versions:
                raise ValueError("No stable release versions found")
            latest = versions[0]
            # Keep previous latest major fallback if all 5 releases are same major and prev had older major
            entry = {
                "id": engine_id,
                "displayName": prev.get("displayName", spec["displayName"]) if prev else spec["displayName"],
                "latest": latest,
                "releases": versions,
                "eol": prev.get("eol", None) if prev else None,
                "source": spec["sourceUrl"],
            }
            if prev is None or prev.get("latest") != latest or prev.get("releases") != versions:
                prev_latest = prev.get("latest", "(none)") if prev else "(none)"
                changes.append(f"- Engine `{engine_id}`: `{prev_latest}` -> `{latest}`")
            new_engines.append(entry)
        except Exception as exc:
            failures.append(f"Engine `{engine_id}` ({spec['sourceUrl']}): {exc}")
            if prev is not None:
                new_engines.append(copy.deepcopy(prev))

    return {
        "schemaVersion": existing.get("schemaVersion", 1),
        "updatedAt": existing.get("updatedAt", ""),
        "engines": new_engines,
    }


def normalize_endoflife_cycle(cycle: dict[str, Any], today: date) -> dict[str, Any] | None:
    branch = str(cycle.get("cycle") or "").strip()
    latest = str(cycle.get("latest") or "").strip()
    if not branch or not latest:
        return None
    if not re.match(r"^\d+(?:\.\d+)*$", branch):
        return None

    raw_eol = cycle.get("eol")
    eol_str: str | None = None
    support = True

    if isinstance(raw_eol, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", raw_eol):
        eol_str = raw_eol
        try:
            support = date.fromisoformat(raw_eol) >= today
        except ValueError:
            support = False
    elif raw_eol is True:
        support = False
    elif raw_eol is False or raw_eol is None:
        eol_str = None
        raw_support = cycle.get("support")
        if isinstance(raw_support, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", raw_support):
            # Active support date might have ended while security EOL is false/none; keep supported unless EOL passed
            support = True
        elif raw_support is False:
            # Check if extended/security support exists via eol=false -> treat as supported
            support = True
        else:
            support = True

    return {
        "branch": branch,
        "latest": latest,
        "eol": eol_str,
        "support": support,
    }


def update_runtimes(existing: dict[str, Any], today: date, failures: list[str], changes: list[str]) -> dict[str, Any]:
    existing_by_id = {r["id"]: r for r in existing.get("runtimes", []) if "id" in r}
    new_runtimes: list[dict[str, Any]] = []

    for spec in RUNTIME_SOURCES:
        runtime_id = spec["id"]
        product = spec["product"]
        url = f"https://endoflife.date/api/{product}.json"
        prev = existing_by_id.get(runtime_id)
        try:
            payload = fetch_json(url)
            if not isinstance(payload, list):
                raise ValueError("Unexpected endoflife.date response format")
            branches: list[dict[str, Any]] = []
            for cycle in payload:
                if not isinstance(cycle, dict):
                    continue
                normalized = normalize_endoflife_cycle(cycle, today)
                if normalized is not None:
                    branches.append(normalized)

            branches.sort(key=lambda b: version_tuple(b["branch"]), reverse=True)
            # Keep supported branches plus up to 3 most recent EOL branches (max 6 total)
            supported = [b for b in branches if b["support"]]
            ended = [b for b in branches if not b["support"]][:3]
            selected = sorted(supported + ended, key=lambda b: version_tuple(b["branch"]), reverse=True)[:6]

            if not selected:
                raise ValueError("No valid branches parsed")

            entry: dict[str, Any] = {
                "id": runtime_id,
                "displayName": prev.get("displayName", spec["displayName"]) if prev else spec["displayName"],
                "source": url,
                "branches": selected,
            }
            if prev and "advisories" in prev:
                entry["advisories"] = copy.deepcopy(prev["advisories"])

            if prev is None or prev.get("branches") != selected:
                top_branch = selected[0]
                changes.append(f"- Runtime `{runtime_id}`: updated branches (latest `{top_branch['branch']}` -> `{top_branch['latest']}`)")
            new_runtimes.append(entry)
        except Exception as exc:
            failures.append(f"Runtime `{runtime_id}` ({url}): {exc}")
            if prev is not None:
                new_runtimes.append(copy.deepcopy(prev))

    return {
        "schemaVersion": existing.get("schemaVersion", 1),
        "updatedAt": existing.get("updatedAt", ""),
        "runtimes": new_runtimes,
    }


def strip_updated_at(data: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(data)
    copied.pop("updatedAt", None)
    return copied


def write_if_changed(path: Path, old_data: dict[str, Any], new_data: dict[str, Any], now_iso: str, dry_run: bool) -> bool:
    if strip_updated_at(old_data) == strip_updated_at(new_data):
        return False
    new_data["updatedAt"] = now_iso
    if not dry_run:
        path.write_text(json.dumps(new_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Update kuma-master-data penguin JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="Do not modify files on disk.")
    parser.add_argument("--pr-body-path", type=str, default="", help="Optional path to write PR body markdown.")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now_utc.date()

    existing_engines = json.loads(ENGINES_PATH.read_text(encoding="utf-8"))
    existing_runtimes = json.loads(RUNTIMES_PATH.read_text(encoding="utf-8"))

    failures: list[str] = []
    changes: list[str] = []

    updated_engines = update_engines(existing_engines, failures, changes)
    updated_runtimes = update_runtimes(existing_runtimes, today, failures, changes)

    engines_changed = write_if_changed(ENGINES_PATH, existing_engines, updated_engines, now_iso, args.dry_run)
    runtimes_changed = write_if_changed(RUNTIMES_PATH, existing_runtimes, updated_runtimes, now_iso, args.dry_run)

    any_changed = engines_changed or runtimes_changed

    if args.pr_body_path:
        lines = [
            f"## 🐧 penguin マスターデータ定期更新 ({today.isoformat()})",
            "",
            "### 変更内容",
        ]
        if changes:
            lines.extend(changes)
        else:
            lines.append("- 差分はありません。")
        lines.extend([
            "",
            "### 補足",
            "- `runtimes.json` の緊急 `advisories` は手動管理対象のため自動変更していません。",
        ])
        if failures:
            lines.extend(["", "### 取得に失敗した情報源（前回値を維持）"])
            for item in failures:
                lines.append(f"- {item}")
        Path(args.pr_body_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"engines_changed={engines_changed}, runtimes_changed={runtimes_changed}, failures={len(failures)}")
    if failures:
        for item in failures:
            print(f"WARN: {item}", file=sys.stderr)
    if not any_changed and len(failures) == len(ENGINE_SOURCES) + len(RUNTIME_SOURCES):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
