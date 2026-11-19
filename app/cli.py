from __future__ import annotations

import argparse
import asyncio
import sys

import psycopg

from app.auth import VALID_ROLES, generate_raw_token, hash_token
from app.db import close_pool, configure_windows_event_loop, get_pool


async def _create_user(username: str, role: str, rotate: bool = False) -> int:
    cleaned_username = username.strip()
    if not cleaned_username:
        print("Error: username cannot be empty.", file=sys.stderr)
        return 1

    if role not in VALID_ROLES:
        print(
            f"Error: invalid role '{role}'. Must be one of: {', '.join(VALID_ROLES)}",
            file=sys.stderr,
        )
        return 1

    raw_token = generate_raw_token()
    token_hash = hash_token(raw_token)

    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                if rotate:
                    await cur.execute(
                        """
                        INSERT INTO users (username, role, token_hash, active)
                        VALUES (%(username)s, %(role)s, %(token_hash)s, TRUE)
                        ON CONFLICT (username) DO UPDATE
                        SET role = EXCLUDED.role,
                            token_hash = EXCLUDED.token_hash,
                            active = TRUE
                        RETURNING id, username, role, created_at;
                        """,
                        {
                            "username": cleaned_username,
                            "role": role,
                            "token_hash": token_hash,
                        },
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO users (username, role, token_hash, active)
                        VALUES (%(username)s, %(role)s, %(token_hash)s, TRUE)
                        RETURNING id, username, role, created_at;
                        """,
                        {
                            "username": cleaned_username,
                            "role": role,
                            "token_hash": token_hash,
                        },
                    )
                row = await cur.fetchone()
            await conn.commit()
    except psycopg.errors.UniqueViolation:
        print(
            f"Error: user '{cleaned_username}' already exists. "
            "Pass --rotate to issue a new token for this user.",
            file=sys.stderr,
        )
        return 1

    assert row is not None
    print(f"User created/updated: id={row['id']} username={row['username']} role={row['role']}")
    print(f"Token (save this now; it is stored hashed and will NOT be shown again):\n{raw_token}")
    return 0


async def _list_users() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, username, role, active, left(token_hash, 12) AS hash_prefix, created_at
                FROM users
                ORDER BY id ASC;
                """
            )
            rows = await cur.fetchall()

    if not rows:
        print("No users found.")
        return 0

    print(f"{'ID':<4} {'USERNAME':<18} {'ROLE':<10} {'ACTIVE':<8} {'HASH_PREFIX':<14} CREATED_AT")
    print("-" * 78)
    for r in rows:
        print(
            f"{r['id']:<4} {r['username']:<18} {r['role']:<10} "
            f"{str(r['active']):<8} {r['hash_prefix']:<14} {r['created_at'].isoformat()}"
        )
    return 0


async def _run_cli(args: argparse.Namespace) -> int:
    try:
        if args.command == "create-user":
            return await _create_user(args.username, args.role, rotate=args.rotate)
        if args.command == "list-users":
            return await _list_users()
        return 1
    finally:
        await close_pool()


def main() -> None:
    configure_windows_event_loop()
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Team Ops MCP user & token management CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create-user",
        help="Create a user with a role and print their Bearer token once",
    )
    create_parser.add_argument("username", help="Unique username (e.g. alice)")
    create_parser.add_argument(
        "role",
        choices=VALID_ROLES,
        help="User role: viewer (read-only), editor (read+write), admin (all + /usage)",
    )
    create_parser.add_argument(
        "--rotate",
        action="store_true",
        help="Rotate token and update role if the username already exists",
    )

    subparsers.add_parser("list-users", help="List all users (without raw tokens)")

    parsed_args = parser.parse_args()
    exit_code = asyncio.run(_run_cli(parsed_args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()