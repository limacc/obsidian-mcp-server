#!/usr/bin/env python3
"""sync_all.py — streaming generator 방식: 파일 발견 즉시 전송 (배치 대기 없음)"""
import json, hashlib, argparse, time
from pathlib import Path
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
VAULT_ROOT   = Path(r"G:\내 드라이브")
SERVER_URL   = "https://obsidian-mcp-server-production-764d.up.railway.app"
RELAY_SECRET = "bfc4b14e7a2812f80a8f1cae425393ca7c1d6f6dbf703bfa128aed6eae2a43a1"
BATCH_SIZE   = 5
CACHE_FILE   = Path(r"D:\workspace\.obsidian_sync_cache.json")
EXTENSIONS   = {".md", ".txt", ".yaml", ".json", ".py", ".r", ".js", ".sh", ".toml", ".csv"}
EXCLUDE_DIRS = {"graphify-out", "node_modules", "__pycache__", ".venv", "cache", ".git"}
# ─────────────────────────────────────────────────────────────────────────────

def sha256_short(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")

def send_batch(batch, cache):
    try:
        r = requests.post(
            f"{SERVER_URL}/sync?token={RELAY_SECRET}",
            json={"files": batch},
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        if r.ok:
            for item in batch:
                cache[item["path"]] = item["hash"]
            return len(batch), 0
        else:
            print(f"  서버 오류 {r.status_code}: {r.text[:100]}")
            return 0, len(batch)
    except Exception as e:
        print(f"  전송 오류: {e}")
        return 0, len(batch)

def main():
    # 서버 health 체크
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        info = r.json()
        print(f"서버: OK  (현재 파일 수: {info.get('files','?')})\n")
    except Exception as e:
        print(f"서버 연결 실패: {e}"); return

    cache = load_cache()
    batch = []
    batch_num = 0
    total_synced = 0
    total_skipped = 0
    scanned = 0

    for f in VAULT_ROOT.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in EXTENSIONS:
            continue
        if any(p.startswith(".") for p in f.parts):
            continue
        if any(p in EXCLUDE_DIRS for p in f.parts):
            continue

        scanned += 1
        if scanned % 500 == 0:
            print(f"  스캔 {scanned}개 | 배치 {batch_num} | synced {total_synced} | skipped {total_skipped}")

        try:
            if f.stat().st_size > 300_000:
                continue
            content = f.read_text(encoding="utf-8", errors="replace")
            rel = f.relative_to(VAULT_ROOT).as_posix()
            h = sha256_short(content)
            if cache.get(rel) == h:
                total_skipped += 1
                continue
            batch.append({"path": rel, "content": content, "hash": h})
            if len(batch) >= BATCH_SIZE:
                synced, _ = send_batch(batch, cache)
                batch_num += 1
                total_synced += synced
                print(f"배치 {batch_num}: synced={synced} skipped={total_skipped}")
                batch = []
                save_cache(cache)
        except Exception:
            continue

    # 마지막 잔여 배치
    if batch:
        synced, _ = send_batch(batch, cache)
        total_synced += synced
        batch_num += 1
        print(f"배치 {batch_num} (마지막): synced={synced}")
        save_cache(cache)

    print(f"\n완료: 총 {total_synced}개 동기화, {total_skipped}개 스킵 (전체 스캔 {scanned}개)")

if __name__ == "__main__":
    main()
