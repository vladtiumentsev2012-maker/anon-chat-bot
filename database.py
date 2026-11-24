import asyncpg
from datetime import datetime, timedelta, timezone

pool: asyncpg.Pool | None = None


async def init_db(dsn: str):
    global pool
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                gender TEXT DEFAULT NULL,
                vip_until TIMESTAMPTZ DEFAULT NULL,
                last_partner BIGINT DEFAULT NULL,
                referrals INT DEFAULT 0,
                referred_by BIGINT DEFAULT NULL,
                reports INT DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE,
                chats_count INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def get_or_create_user(user_id: int, referred_by: int | None = None):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (user_id, referred_by) VALUES ($1, $2)",
                user_id,
                referred_by if referred_by != user_id else None,
            )
            if referred_by and referred_by != user_id:
                await conn.execute(
                    "UPDATE users SET referrals = referrals + 1 WHERE user_id = $1",
                    referred_by,
                )
                ref_row = await conn.fetchrow(
                    "SELECT referrals FROM users WHERE user_id = $1", referred_by
                )
                if ref_row and ref_row["referrals"] % 2 == 0:
                    await add_vip_hours(referred_by, 24)
                    return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id), referred_by
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return user, None


async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)


async def set_gender(user_id: int, gender: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET gender = $1 WHERE user_id = $2", gender, user_id)


async def is_vip(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user or not user["vip_until"]:
        return False
    return user["vip_until"] > datetime.now(timezone.utc)


async def add_vip_hours(user_id: int, hours: int):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT vip_until FROM users WHERE user_id = $1", user_id)
        now = datetime.now(timezone.utc)
        base = user["vip_until"] if (user and user["vip_until"] and user["vip_until"] > now) else now
        new_until = base + timedelta(hours=hours)
        await conn.execute("UPDATE users SET vip_until = $1 WHERE user_id = $2", new_until, user_id)


async def save_last_partner(user_id: int, partner_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_partner = $1, chats_count = chats_count + 1 WHERE user_id = $2",
            partner_id,
            user_id,
        )


async def add_report(target_id: int) -> int:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET reports = reports + 1 WHERE user_id = $1", target_id)
        row = await conn.fetchrow("SELECT reports FROM users WHERE user_id = $1", target_id)
        reps = row["reports"] if row else 0
        if reps >= 3:
            await conn.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1", target_id)
        return reps


async def set_ban(target_id: int, banned: bool):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = $1 WHERE user_id = $2", banned, target_id)


async def get_stats():
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        vips = await conn.fetchval("SELECT COUNT(*) FROM users WHERE vip_until > NOW()")
        banned = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
        return total, vips, banned


async def get_all_user_ids():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE")
        return [r["user_id"] for r in rows]
