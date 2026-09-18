import subprocess

def inject_redis_down():
    subprocess.run(
        ["docker", "compose", "stop", "redis"],
        check=True
    )

if __name__ == "__main__":
    inject_redis_down()
