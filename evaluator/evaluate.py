import requests


def evaluate():

    try:
        response = requests.get(
            "http://localhost:8000/health",
            timeout=2
        )

        data = response.json()

        if (
            data["status"] == "healthy"
            and data["redis"] == "healthy"
        ):
            return True

    except Exception:
        pass

    return False


if __name__ == "__main__":

    result = evaluate()

    print(
        "PASS"
        if result
        else "FAIL"
    )
