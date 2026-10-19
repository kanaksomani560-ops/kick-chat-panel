import os
import requests

CHANNEL = "chimper"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(BASE_DIR, "tokens.txt")
OUTPUT_FILE = os.path.join(BASE_DIR, "output.txt")


def check_follow(token, channel):
    bare = token.strip()
    if ":" in bare: bare = bare.split(":")[-1]
    if "|" in bare: bare = bare.split("|")[-1]

    headers = {
        "Authorization": f"Bearer {bare}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://kick.com/{channel}",
        "Origin": "https://kick.com",
    }

    try:
        r = requests.get(
            f"https://kick.com/api/v2/channels/{channel}/me",
            headers=headers,
            timeout=15,
        )
        print("TOKEN:", bare[:8], "STATUS:", r.status_code)
        print(r.text[:300])

        if r.status_code == 200:
            return r.json().get("is_following", False)

        return False

    except Exception as e:
        print("ERROR:", token[:8], type(e).__name__, e)
        return False


def main():
    print("Working dir:", os.getcwd())
    print("Input file:", INPUT_FILE)

    if not os.path.exists(INPUT_FILE):
        print("MISSING FILE")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        tokens = [line.strip() for line in f if line.strip()]

    print("Loaded tokens:", len(tokens))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("")

    for number, token in enumerate(tokens, 1):
        if check_follow(token, CHANNEL):
            print(f"[{number}] YES")
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(token + "\n")
        else:
            print(f"[{number}] NO")

    print("Finished.")


if __name__ == "__main__":
    main()