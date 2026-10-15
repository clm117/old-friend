"""语伴 网关账号播种 CLI（仅用标准库）

网关不提供公开注册接口（避免被刷号），账号由本脚本在服务器侧离线创建。

用法：
  python auth_seed.py add alice              # 交互式输入口令（不回显）
  python auth_seed.py add alice -p <口令>     # 非交互（口令会进 shell 历史，谨慎）
  python auth_seed.py add alice --force      # 覆盖已存在账号
  python auth_seed.py passwd alice           # 修改口令
  python auth_seed.py list                   # 列出账号
  python auth_seed.py remove alice           # 删除账号

账号文件路径由 AUTH_USERS_FILE 指定，默认 api-gateway/users.json。
"""
import argparse
import getpass
import sys

from auth_store import (
    hash_password,
    load_users,
    save_users,
    users_file_path,
    valid_username,
)

MIN_PASSWORD_LEN = 8


def _prompt_password() -> str | None:
    pwd = getpass.getpass(f"请输入口令（至少 {MIN_PASSWORD_LEN} 位，输入不回显）: ")
    if len(pwd) < MIN_PASSWORD_LEN:
        print(f"口令太短，至少 {MIN_PASSWORD_LEN} 位", file=sys.stderr)
        return None
    again = getpass.getpass("请再次输入确认: ")
    if pwd != again:
        print("两次输入不一致，已取消", file=sys.stderr)
        return None
    return pwd


def _resolve_password(arg_password: str | None) -> str | None:
    if arg_password:
        if len(arg_password) < MIN_PASSWORD_LEN:
            print(f"口令太短，至少 {MIN_PASSWORD_LEN} 位", file=sys.stderr)
            return None
        return arg_password
    return _prompt_password()


def _check_username(name: str) -> bool:
    if not valid_username(name):
        print("账号名非法：仅允许字母、数字及 _ . @ - ，长度 1-64", file=sys.stderr)
        return False
    return True


def cmd_add(args) -> int:
    if not _check_username(args.username):
        return 1
    users = load_users()
    if args.username in users and not args.force:
        print(f"账号已存在：{args.username}（如需覆盖请加 --force）", file=sys.stderr)
        return 1
    password = _resolve_password(args.password)
    if not password:
        return 1
    users[args.username] = hash_password(password)
    path = save_users(users)
    print(f"已写入账号：{args.username}")
    print(f"账号文件：{path}")
    return 0


def cmd_passwd(args) -> int:
    if not _check_username(args.username):
        return 1
    users = load_users()
    if args.username not in users:
        print(f"账号不存在：{args.username}", file=sys.stderr)
        return 1
    password = _resolve_password(args.password)
    if not password:
        return 1
    users[args.username] = hash_password(password)
    path = save_users(users)
    print(f"已更新口令：{args.username}")
    print(f"账号文件：{path}")
    return 0


def cmd_remove(args) -> int:
    if not _check_username(args.username):
        return 1
    users = load_users()
    if args.username not in users:
        print(f"账号不存在：{args.username}", file=sys.stderr)
        return 1
    del users[args.username]
    path = save_users(users)
    print(f"已删除账号：{args.username}")
    print(f"账号文件：{path}")
    return 0


def cmd_list(_args) -> int:
    users = load_users()
    print(f"账号文件：{users_file_path()}")
    if not users:
        print("（暂无账号）")
        return 0
    for name in sorted(users):
        print(f"  - {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auth_seed.py", description="语伴网关账号管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="新增账号")
    p_add.add_argument("username")
    p_add.add_argument("-p", "--password", default=None, help="口令（省略则交互输入）")
    p_add.add_argument("--force", action="store_true", help="覆盖已存在的账号")
    p_add.set_defaults(func=cmd_add)

    p_pw = sub.add_parser("passwd", help="修改口令")
    p_pw.add_argument("username")
    p_pw.add_argument("-p", "--password", default=None, help="口令（省略则交互输入）")
    p_pw.set_defaults(func=cmd_passwd)

    p_rm = sub.add_parser("remove", help="删除账号")
    p_rm.add_argument("username")
    p_rm.set_defaults(func=cmd_remove)

    p_ls = sub.add_parser("list", help="列出账号")
    p_ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
