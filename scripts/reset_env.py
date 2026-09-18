import subprocess

def restart_redis():
    subprocess.run(
        ["docker", "compose", "start", "redis"],
        check=True
    )

if __name__ == "__main__":
    restart_redis()
