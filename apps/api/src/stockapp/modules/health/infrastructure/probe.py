import psycopg
from redis.asyncio import Redis
from stockapp.config import Settings


class InfrastructureProbe:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def postgres(self) -> bool:
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url, connect_timeout=2
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                return await cursor.fetchone() == (1,)

    async def redis(self) -> bool:
        async with Redis.from_url(
            self.settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        ) as client:
            return bool(await client.ping())
