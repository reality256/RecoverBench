from fastapi import FastAPI, HTTPException
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

app = FastAPI()

# 基准环境要求"故障必须快速可观测":
# 如果不关掉 redis-py 8.x 的默认重试(默认重试 10 次、指数退避),
# Redis 挂掉后每个请求会卡几十秒,故障注入后的验证就全被拖死了。
r = redis.Redis(
    host="redis",
    port=6379,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,          # 读超时:防止死在连接池里的旧连接挂起请求
    health_check_interval=1,   # 每次使用前检查连接是否还活着
    retry=Retry(NoBackoff(), 0)  # 关掉默认重试,失败立即抛异常
)


@app.get("/health")
def health():
    try:
        r.ping()
        return {
            "status": "healthy",
            "redis": "healthy"
        }
    except Exception:
        return {
            "status": "degraded",
            "redis": "unavailable"
        }


@app.get("/cache/{key}")
def get_cache(key: str):
    try:
        value = r.get(key)

        return {
            "key": key,
            "value": value
        }

    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Redis unavailable: {str(e)}"
        )