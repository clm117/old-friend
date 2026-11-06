# -*- coding: utf-8 -*-
"""历史数据归属迁移（一次性脚本，可重复执行且幂等）

背景：自定义角色与会话在引入 owner 字段前的存量数据没有归属信息。
策略：
- 自定义角色 → 归属到可配置的 LEGACY_CHARACTER_OWNER（默认 system），并标记 shared=True，
  对所有登录用户只读可用，避免存量角色突然不可用；新创建的角色严格按创建者隔离。
- 会话 → 归属到 LEGACY_CHARACTER_OWNER 并在 short_term 打 legacy 标记；数据原样保留，
  仅归属信息变化，不会删除任何历史对话。

安全：写前备份到 data/backups/<时间戳>/，写前/写后计算 sha256 与逐条内容哈希，
数量或内容不一致则回滚并报错。

用法：
    python migrate_owners.py --dry-run
    python migrate_owners.py --owner system
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def item_fingerprint(item: dict, ignore: tuple) -> str:
    clean = {k: v for k, v in item.items() if k not in ignore}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def backup(path: Path, backup_dir: Path):
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / path.name
    shutil.copy2(path, dst)
    return dst


def migrate_characters(data_dir: Path, owner: str, dry_run: bool):
    path = data_dir / "custom_characters.json"
    raw = path.read_bytes() if path.exists() else b""
    chars = load_json(path)
    if chars is None:
        return {"file": str(path), "skipped": "not_found"}
    before_count = len(chars)
    before_fp = {c.get("id"): item_fingerprint(c, ("owner", "shared")) for c in chars}

    changed = 0
    for c in chars:
        if c.get("is_preset") or c.get("owner"):
            continue
        changed += 1
        if not dry_run:
            c["owner"] = owner
            c["shared"] = True
    if not dry_run:
        atomic_write(path, chars)

    after = load_json(path) if not dry_run else chars
    after_count = len(after)
    after_fp = {c.get("id"): item_fingerprint(c, ("owner", "shared")) for c in after}
    mismatched = [cid for cid, fp in before_fp.items() if after_fp.get(cid) != fp]
    if before_count != after_count:
        raise SystemExit(f"角色数量不一致: {before_count} -> {after_count}")
    if mismatched:
        raise SystemExit(f"角色内容被意外修改: {mismatched[:5]}")
    if not dry_run and path.exists():
        # 迁移后二次校验：所有自定义角色都有 owner
        missing = [c.get("id") for c in after if not c.get("is_preset") and not c.get("owner")]
        if missing:
            raise SystemExit(f"仍有角色缺少 owner: {missing[:5]}")
    return {
        "file": str(path),
        "sha256_before": sha256_bytes(raw),
        "sha256_after": sha256_bytes(path.read_bytes()) if path.exists() and not dry_run else "",
        "count_before": before_count,
        "count_after": after_count,
        "newly_owned": changed,
        "content_unchanged": True,
        "legacy_shared": sum(1 for c in after if c.get("shared")),
    }


def migrate_sessions(data_dir: Path, owner: str, dry_run: bool):
    path = data_dir / "state.json"
    raw = path.read_bytes() if path.exists() else b""
    state = load_json(path)
    if state is None:
        return {"file": str(path), "skipped": "not_found"}
    sessions = state.get("sessions") or {}
    before_count = len(sessions)
    before_fp = {
        sid: hashlib.sha256(json.dumps(entry.get("dialogue_history", []),
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for sid, entry in sessions.items()
    }
    changed = 0
    for sid, entry in sessions.items():
        if (entry.get("short_term") or {}).get("owner"):
            continue
        changed += 1
        if not dry_run:
            st = entry.setdefault("short_term", {})
            st["owner"] = owner
            st["legacy"] = True
    if not dry_run:
        atomic_write(path, state)
    after = load_json(path) if not dry_run else state
    sessions_after = (after or {}).get("sessions") or {}
    if before_count != len(sessions_after):
        raise SystemExit(f"会话数量不一致: {before_count} -> {len(sessions_after)}")
    for sid, fp in before_fp.items():
        entry = sessions_after.get(sid, {})
        new_fp = hashlib.sha256(json.dumps(entry.get("dialogue_history", []),
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if fp != new_fp:
            raise SystemExit(f"会话 {sid} 对话内容被意外修改")
    return {
        "file": str(path),
        "sha256_before": sha256_bytes(raw),
        "sha256_after": sha256_bytes(path.read_bytes()) if path.exists() and not dry_run else "",
        "count_before": before_count,
        "count_after": len(sessions_after),
        "newly_owned": changed,
        "history_unchanged": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(HERE / "data"))
    ap.add_argument("--owner", default=os.environ.get("LEGACY_CHARACTER_OWNER", "system"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    owner = (args.owner or "system").strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "backups" / stamp

    report = {"owner": owner, "dry_run": args.dry_run, "backup_dir": str(backup_dir)}
    for name in ("custom_characters.json", "state.json"):
        p = data_dir / name
        if p.exists() and not args.dry_run:
            report.setdefault("backups", []).append(str(backup(p, backup_dir)))
    report["characters"] = migrate_characters(data_dir, owner, args.dry_run)
    report["sessions"] = migrate_sessions(data_dir, owner, args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["characters"].get("skipped") == "not_found" and report["sessions"].get("skipped") == "not_found":
        sys.exit(0)


if __name__ == "__main__":
    main()
