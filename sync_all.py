#!/usr/bin/env python3
"""
sync_all.py — 옵시디언 볼트 전체를 Railway MCP 서버로 일괄 동기화
──────────────────────────────────────────────────────────────────
사용법:
  python sync_all.py                      # 기본값으로 실행 (아래 CONFIG 수정)
  python sync_all.py --dry-run            # 실제 전송 없이 파일 목록만 출력
  python sync_all.py --watch              # 변경 감지 모드 (watchdog 필요)

동작:
  1. VAULT_ROOT 아래 모든 .md 파일을 읽는다
  2. 변경된 파일만 Railway /sync 엔드포인트에 POST
  3. 체크섬 캐시(.sync_cache.json)로 이중 전송 방지
  4. 배치 크기 BATCH_SIZE로 분할 전송 (서버 메모리 보호)

설치 (최초 1회):
  pip install requests watchdog --break-system-packages
"""

import json
import hashlib
import argparse
import time
from pathlib import Path

import requests

# ── CONFIG (환경에 맞게 수정) ─────────────────────────────────────────────────
VAULT_ROOT   = Path(r"G:\내 드라이브")             # ← 구글드라이브 전체 (옵시디언 한정 불필요)
SERVER_URL   = "https://obsidian-mcp-server-production-7457.up.railway.app"   # ← Railway 배포 후 URL로 교체
RELAY_SECRET = "bfc4b14e7a2812f80a8f1cae425393ca7c1d6f6dbf703bfa128aed6eae2a43a1"    # ← .env.example의 RELAY_SECRET과 동일
BATCH_SIZE   = 50    # 한 번에 전송할 파일 수 (Railway 무료: 50 이하 권장)
CACHE_FILE   = Path(r"D:\workspace\.obsidian_sync_cache.json")  # 체크섬 캐시 (G: 금지)
EXTENSIONS   = {".md", ".txt", ".yaml", ".json", ".py", ".r", ".js", ".sh", ".toml", ".csv"}  # 텍스트 계열 전부
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "X-Relay-Secret": RELAY_SECRET,
    "Content-Type":  "application/json",
}


def sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_files(vault: Path) -> list[dict]:
    """볼트 내 모든 대상 파일을 수집하고 내용을 읽는다."""
    items = []
    for f in vault.rglob("*"):
        if f.suffix.lower() not in EXTENSIONS:
            continue
        if any(part.startswith(".") for part in f.parts):
            continue  # 숨김 폴더 제외
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            rel     = f.relative_to(vault).as_posix()
            items.append({"path": rel, "content": content, "hash": sha256_short(content)})
        except Exception as e:
            print(f"  ⚠️  읽기 실패: {f}  ({e})")
    return items


def sync(files: list[dict], dry_run: bool) -> dict:
    """변경된 파일을 배치로 서버에 전송한다."""
    cache = load_cache()
    changed = [f for f in files if cache.get(f["path"]) != f["hash"]]

    print(f"  총 파일: {len(files)}  |  변경: {len(changed)}  |  스킵: {len(files)-len(changed)}")

    if not changed:
        print("  ✅ 모두 최신 상태입니다.")
        return {"synced": 0, "skipped": len(files)}

    if dry_run:
        print("  [dry-run] 전송 대상:")
        for f in changed:
            print(f"    - {f['path']}")
        return {"dry_run": True, "would_sync": len(changed)}

    total_synced = 0
    for i in range(0, len(changed), BATCH_SIZE):
        batch = changed[i : i + BATCH_SIZE]
        payload = {"files": [{"path": f["path"], "content": f["content"]} for f in batch]}
        resp = requests.post(f"{SERVER_URL}/sync?token={RELAY_SECRET}", headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        total_synced += result.get("synced", 0)
        print(f"  배치 {i//BATCH_SIZE+1}: synced={result.get('synced',0)}  skipped={result.get('skipped',0)}")

        # 캐시 갱신
        for f in batch:
            cache[f["path"]] = f["hash"]
        save_cache(cache)

    return {"synced": total_synced}


def watch_mode():
    """변경 감지 모드: watchdog으로 파일 변경 시 즉시 단건 전송."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("❌ watchdog 미설치: pip install watchdog --break-system-packages")
        return

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            if p.suffix.lower() not in EXTENSIONS:
                return
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                rel = p.relative_to(VAULT_ROOT).as_posix()
                payload = {"files": [{"path": rel, "content": content}]}
                resp = requests.post(f"{SERVER_URL}/sync?token={RELAY_SECRET}", headers=HEADERS, json=payload, timeout=10)
                resp.raise_for_status()
                r = resp.json()
                if r.get("synced", 0):
                    print(f"  📤 synced: {rel}")
            except Exception as e:
                print(f"  ⚠️  전송 실패: {p.name}  ({e})")

        on_created = on_modified

    observer = Observer()
    observer.schedule(Handler(), str(VAULT_ROOT), recursive=True)
    observer.start()
    print(f"👀 감시 중: {VAULT_ROOT}  (Ctrl+C로 종료)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def main():
    parser = argparse.ArgumentParser(description="옵시디언 볼트 → Railway MCP 동기화")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 변경 목록만 출력")
    parser.add_argument("--watch",   action="store_true", help="watchdog 실시간 감시 모드")
    args = parser.parse_args()

    # ── 기본값 확인 ──────────────────────────────────────────────────────────
    if "<your-app>" in SERVER_URL:
        print("❌ SERVER_URL을 Railway 배포 URL로 수정하세요 (sync_all.py 상단 CONFIG).")
        return
    if "your-strong" in RELAY_SECRET:
        print("❌ RELAY_SECRET을 실제 토큰으로 수정하세요.")
        return
    if not VAULT_ROOT.exists():
        print(f"❌ 볼트 경로 없음: {VAULT_ROOT}")
        return

    if args.watch:
        watch_mode()
        return

    print(f"\n🔄 Obsidian Vault Sync")
    print(f"   볼트: {VAULT_ROOT}")
    print(f"   서버: {SERVER_URL}")

    # 서버 health 체크
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        info = r.json()
        print(f"   서버: OK  (현재 파일 수: {info.get('files', '?')})")
    except Exception as e:
        print(f"   서버: ⚠️  연결 실패 ({e})")

    files  = collect_files(VAULT_ROOT)
    result = sync(files, dry_run=args.dry_run)

    print(f"\n✅ 완료: {result}")


if __name__ == "__main__":
    main()
