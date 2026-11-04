"""语伴数据备份脚本（标准库实现，跨平台）

备份范围（不包含 .env / 模型权重，避免泄露与体积膨胀）：
  - authdata/          账号表（users.json）
  - ai-service/data/   自定义角色、会话状态（state.json）

用法：
  python infrastructure/backup/backup_data.py
  python infrastructure/backup/backup_data.py --dest D:/backup/yuban --keep 14

产物：
  yuban-backup-YYYYmmdd-HHMMSS.zip        数据归档
  yuban-backup-YYYYmmdd-HHMMSS.zip.sha256 归档校验值
  归档内 manifest.json                    文件清单、哈希、账号/角色/会话数量
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKUP_ITEMS = ("authdata", "ai-service/data", "api-gateway/users.json")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "node_modules"}
EXCLUDE_SUFFIXES = {".tmp", ".log"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for item in BACKUP_ITEMS:
        base = root / item
        if not base.exists():
            continue
        if base.is_file():
            rel = base.relative_to(root)
            if rel.suffix not in EXCLUDE_SUFFIXES:
                files.append(rel)
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES or path.name.endswith(".tmp"):
                continue
            files.append(rel)
    return sorted(files)


def build_manifest(root: Path, files: list[Path]) -> dict:
    entries = {}
    for rel in files:
        p = root / rel
        entries[str(rel).replace("\\", "/")] = {
            "sha256": sha256_file(p),
            "size": p.stat().st_size,
        }
    counts = {"accounts": 0, "characters": 0, "sessions": 0}
    users_file = root / "authdata" / "users.json"
    if not users_file.exists():
        users_file = root / "api-gateway" / "users.json"
    if users_file.exists():
        try:
            counts["accounts"] = len(json.loads(users_file.read_text(encoding="utf-8")) or {})
        except (json.JSONDecodeError, OSError):
            pass
    chars_file = root / "ai-service" / "data" / "custom_characters.json"
    if chars_file.exists():
        try:
            data = json.loads(chars_file.read_text(encoding="utf-8"))
            counts["characters"] = len(data) if isinstance(data, (list, dict)) else 0
        except (json.JSONDecodeError, OSError):
            pass
    state_file = root / "ai-service" / "data" / "state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            counts["sessions"] = len(data.get("sessions", {})) if isinstance(data, dict) else 0
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": socket.gethostname(),
        "project_root": str(root),
        "items": list(BACKUP_ITEMS),
        "counts": counts,
        "files": entries,
    }


def prune_old(dest: Path, keep: int) -> list[str]:
    archives = sorted(dest.glob("yuban-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in archives[keep:]:
        old.unlink(missing_ok=True)
        sidecar = old.with_name(old.name + ".sha256")
        sidecar.unlink(missing_ok=True)
        removed.append(str(old))
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语伴数据备份")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根目录")
    parser.add_argument("--dest", default=os.environ.get("BACKUP_DIR", str(PROJECT_ROOT / "backups")), help="备份输出目录")
    parser.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "7")), help="保留最近 N 份归档")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    dest = Path(args.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    files = collect_files(root)
    if not files:
        print(f"未找到可备份数据（authdata/ 或 ai-service/data/ 为空）：{root}", file=sys.stderr)
        return 2

    manifest = build_manifest(root, files)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = dest / f"yuban-backup-{stamp}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in files:
            zf.write(root / rel, arcname=str(rel).replace("\\", "/"))

    digest = sha256_file(archive)
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    removed = prune_old(dest, max(args.keep, 1))

    print(json.dumps({
        "archive": str(archive),
        "sha256": digest,
        "size_bytes": archive.stat().st_size,
        "file_count": len(files),
        "counts": manifest["counts"],
        "pruned": removed,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
