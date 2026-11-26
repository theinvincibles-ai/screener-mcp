"""
screener-mcp-telegram: one-time Telegram sign-in and choosing channels to follow.

Run these in Terminal on the machine that runs the server. TELEGRAM_API_ID and
TELEGRAM_API_HASH must be set (the macOS start script reads them from the
Keychain; for the CLI, `scripts/macos/telegram.sh` does the same).

  screener-mcp-telegram login              sign in (phone, code, 2FA password)
  screener-mcp-telegram channels           list your channels and groups, with ids and topics
  screener-mcp-telegram follow ID [--topic N ...]   follow a channel (all topics, or only some)
  screener-mcp-telegram unfollow ID        stop following
  screener-mcp-telegram following          show what is followed
  screener-mcp-telegram poll               pull new posts now and print counts
  screener-mcp-telegram logout             sign out and delete the local session

The login code and password are typed here only. They are not stored, logged
or sent anywhere except to Telegram.
"""

import argparse
import asyncio
import getpass
import sys

from .core import telegram_client as tg
from .core import telegram_store as store


def _fail(msg: str) -> "None":
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _api() -> tuple[int, str]:
    if not tg.installed():
        _fail(tg.INSTALL_HINT)
    api = tg._api()
    if not api:
        _fail("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first (your app at https://my.telegram.org).")
    return api


async def _connected():
    api_id, api_hash = _api()
    session = store.read_session()
    if not session:
        _fail("Not signed in. Run: screener-mcp-telegram login")
    client = tg.make_client(session, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        _fail("The saved sign-in no longer works. Run: screener-mcp-telegram login")
    return client


async def cmd_login(_args) -> None:
    api_id, api_hash = _api()
    client = tg.make_client("", api_id, api_hash)
    await client.start(
        phone=lambda: input("Phone number with country code (e.g. +91...): ").strip(),
        code_callback=lambda: input("Login code Telegram just sent you: ").strip(),
        password=lambda: getpass.getpass("Two-step verification password (hidden): "),
    )
    me = await client.get_me()
    store.write_session(client.session.save())
    await client.disconnect()
    print(f"Signed in as {me.first_name or 'your account'}. Session saved to {store.session_path()} (owner-only).")
    print("Next: screener-mcp-telegram channels, then follow the ones you want.")


async def _topics(client, entity) -> list[tuple[int, str]]:
    from telethon.tl import functions

    res = await client(functions.messages.GetForumTopicsRequest(
        peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    return [(t.id, getattr(t, "title", "")) for t in res.topics]


async def cmd_channels(_args) -> None:
    client = await _connected()
    try:
        followed = {int(c["id"]) for c in store.read_channels()}
        print(f"{'ID':>14}  {'type':<8} {'followed':<8} title")
        async for d in client.iter_dialogs():
            e = d.entity
            if not getattr(d, "is_channel", False):
                continue
            kind = "forum" if getattr(e, "forum", False) else ("group" if getattr(e, "megagroup", False) else "channel")
            print(f"{e.id:>14}  {kind:<8} {'yes' if e.id in followed else '':<8} {d.name}")
            if kind == "forum":
                for tid, title in await _topics(client, e):
                    print(f"{'':>14}  {'':<8} {'':<8}   topic {tid}: {title}")
    finally:
        await client.disconnect()


async def cmd_follow(args) -> None:
    client = await _connected()
    try:
        entity = None
        async for d in client.iter_dialogs():
            if getattr(d, "is_channel", False) and d.entity.id == args.id:
                entity = d.entity
                break
        if entity is None:
            _fail(f"No channel or group with id {args.id} among your chats. Run `channels` to see ids.")
        forum = bool(getattr(entity, "forum", False))
        topic_titles = {}
        if forum:
            topic_titles = {str(tid): title for tid, title in await _topics(client, entity)}
            unknown = [t for t in args.topic if str(t) not in topic_titles]
            if unknown:
                _fail(f"Unknown topic id(s) {unknown}. Run `channels` to list topics.")
        elif args.topic:
            _fail("That chat has no topics; follow it without --topic.")
        channels = [c for c in store.read_channels() if int(c["id"]) != entity.id]
        channels.append({
            "id": entity.id,
            "access_hash": entity.access_hash,
            "title": getattr(entity, "title", ""),
            "forum": forum,
            "topics": sorted(args.topic) or None,
            "topic_titles": topic_titles,
        })
        store.write_channels(channels)
        what = f"topics {sorted(args.topic)}" if args.topic else "all posts"
        print(f"Following {getattr(entity, 'title', entity.id)} ({what}). The server picks it up on its next poll.")
    finally:
        await client.disconnect()


async def cmd_unfollow(args) -> None:
    channels = store.read_channels()
    keep = [c for c in channels if int(c["id"]) != args.id]
    if len(keep) == len(channels):
        _fail(f"{args.id} isn't followed.")
    store.write_channels(keep)
    print(f"Stopped following {args.id}. Stored posts are kept; delete {store.db_path()} to remove all of them.")


async def cmd_following(_args) -> None:
    channels = store.read_channels()
    if not channels:
        print("Nothing followed yet. Run: screener-mcp-telegram channels")
    for c in channels:
        topics = ", ".join(f"{t} ({c.get('topic_titles', {}).get(str(t), '')})" for t in c.get("topics") or []) or "all"
        print(f"{c['id']:>14}  {c.get('title', '')}  topics: {topics}")


async def cmd_poll(_args) -> None:
    _api()
    for r in await tg.poll_all():
        print(r)
    tg_client = tg._client
    if tg_client is not None:
        await tg_client.disconnect()


async def cmd_logout(_args) -> None:
    if store.read_session() and tg.installed() and tg._api():
        try:
            client = await _connected()
            await client.log_out()
        except SystemExit:
            pass
    store.delete_session()
    print("Signed out and deleted the local session. Stored posts are kept.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="screener-mcp-telegram", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("channels")
    f = sub.add_parser("follow")
    f.add_argument("id", type=int)
    f.add_argument("--topic", type=int, action="append", default=[])
    u = sub.add_parser("unfollow")
    u.add_argument("id", type=int)
    sub.add_parser("following")
    sub.add_parser("poll")
    sub.add_parser("logout")
    args = p.parse_args(argv)
    handler = globals()[f"cmd_{args.cmd}"]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
