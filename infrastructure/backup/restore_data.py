"""语伴数据恢复脚本（标准库实现）

校验归档内 manifest.json 的 SHA-256 后才写入，默认要求显式 --confirm；
恢复前会把目标目录改名为 .restore-rollback-<时间戳>，便于回滚。

用法：
  python infrastructure/backup/restore_data.py --archive backups/yuban-backup-xxx.zip --dry-run
  python infrastructure/backup/restore_data.py --archive backups/yuban-backup-xxx.zip --confirm
  python infrastructure/backup/restore_data.py --archive ... --confirm --target-root D:/tmp/restore-drill
"""
import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_PREFIXES = ("authdata/", "ai-service/data/", "api-gateway/users.json")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relpath(name: str) -> Path:
    rel = Path(name.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"归档内存在非法路径：{name}")
    normalized = str(rel).replace("\\", "/")
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise ValueError(f"归档内存在不在允许范围的路径：{name}")
    return rel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语伴数据恢复")
    parser.add_argument("--archive", required=True, help="备份 zip 路径")
    parser.add_argument("--target-root", default=str(PROJECT_ROOT), help="恢复到哪个项目根目录")
    parser.add_argument("--confirm", action="store_true", help="确认执行写入（否则只做校验）")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印清单，不写文件")
    parser.add_argument("--skip-rollback", action="store_true", help="不保留恢复前的目标目录副本")
    args = parser.parse_args(argv)

    archive = Path(args.archive).expanduser().resolve()
    target_root = Path(args.target_root).expanduser().resolve()
    if not archive.exists():
        print(f"归档不存在：{archive}", file=sys.stderr)
        return 2

    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").split()[0].strip()
        actual = sha256_bytes(archive.read_bytes())
        if expected != actual:
            print(f"归档哈希不匹配，拒绝恢复：expected={expected} actual={actual}", file=sys.stderr)
            return 3

    if args.confirm and not args.dry_run and not args.skip_rollback:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rollback_root = target_root / f".restore-rollback-{stamp}"
        for prefix in ALLOWED_PREFIXES:
            src = target_root / prefix.rstrip("/")
            if src.exists():
                dst = rollback_root / prefix.rstrip("/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
        print(f"恢复前副本已保存到：{rollback_root}")

    with zipfile.ZipFile(archive) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            print("归档缺少 manifest.json，拒绝恢复", file=sys.stderr)
            return 3
        files = manifest.get("files", {})
        entries = [name for name in zf.namelist() if name != "manifest.json"]
        for name in entries:
            rel = safe_relpath(name)
            data = zf.read(name)
            expected_hash = files.get(name.replace("\\", "/"), {}).get("sha256")
            if expected_hash and sha256_bytes(data) != expected_hash:
                print(f"文件哈希不匹配：{name}", file=sys.stderr)
                return 3
            if args.dry_run:
                continue
            if not args.confirm:
                print("校验通过；未指定 --confirm，未写入任何文件", file=sys.stderr)
                return 0
            dst = target_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass

    summary = {
        "archive": str(archive),
        "target_root": str(target_root),
        "manifest_generated_at": manifest.get("generated_at"),
        "counts": manifest.get("counts"),
        "file_count": len(entries),
        "written": not args.dry_run and args.confirm,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
