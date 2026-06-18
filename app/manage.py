import os
import sys
import time
import json
import shutil
import logging
import socket
import subprocess
import urllib.request
import urllib.parse
import http.server

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("manage")

CHROME_USER_DATA = "/app/data/chrome-profile"
COOKIES_FILE = "/app/cookies.txt"


def _port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cmd_auth(args):
    """One-time YouTube authentication via Google OAuth device flow.

    No browser or display needed on the server.  You authenticate from your
    phone, tablet, or desktop by opening a URL and entering a code.

    Steps:
      1. Run this command (interactive terminal required).
      2. Open the printed URL on any device.
      3. Enter the printed code and log into your Google/YouTube account.
      4. Come back here – cookies are saved automatically.
    """
    print("")
    print("  YouTube Authentication (one-time setup)")
    print("  =======================================")
    print("")
    print("  Starting OAuth device flow...")
    print("")

    # Step 1: get device code from Google
    # yt-dlp bundles a YouTube-branded OAuth client ID
    CLIENT_ID = "861556708454-d6trmres6svigj4e8jk9qtglasqbfh9l.apps.googleusercontent.com"
    CLIENT_SECRET = "SboVDocKvCkBV0lWqBVolFkB"

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": "http://gdata.youtube.com https://www.googleapis.com/auth/youtube",
    }).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/device/code",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = urllib.request.urlopen(req)
    device = json.loads(resp.read())

    ver_url = device.get("verification_url", "https://www.google.com/device")
    user_code = device.get("user_code", "")
    device_code = device.get("device_code", "")
    interval = device.get("interval", 5)

    print(f"  Open this URL in your browser:")
    print(f"  {ver_url}")
    print(f"")
    print(f"  Enter this code:")
    print(f"  {user_code}")
    print(f"")
    print(f"  Waiting for you to authenticate...")
    sys.stdout.flush()

    # Step 2: poll for completion
    token_data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }).encode()

    deadline = time.time() + 300
    tokens = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp = urllib.request.urlopen(req)
            tokens = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            err = body.get("error", "")
            if err == "authorization_pending":
                sys.stdout.write(".")
                sys.stdout.flush()
                continue
            elif err == "slow_down":
                interval += 5
                sys.stdout.write(".")
                sys.stdout.flush()
                continue
            else:
                logger.error(f"OAuth error: {body}")
                sys.exit(1)

    if not tokens:
        logger.error("Timed out waiting for authentication.")
        sys.exit(1)

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    logger.info("")
    logger.info("  Authentication successful!")
    logger.info("")

    # Step 3: Use token to get YouTube session cookies
    logger.info("  Extracting YouTube cookies...")

    # We need to make a request to YouTube that sets the session cookies.
    # Use the token to call the YouTube API first, then redirect to youtube.com.
    cookie_jar_dir = os.path.join(CHROME_USER_DATA, "Default")
    os.makedirs(cookie_jar_dir, exist_ok=True)

    # Use yt-dlp with the token embedded as a header
    # yt-dlp doesn't directly accept bearer tokens, but we can set HTTP headers.
    # The approach: use `--add-header "Authorization: Bearer TOKEN"`
    auth_header = f"Authorization: Bearer {access_token}"

    result = subprocess.run(
        [
            "yt-dlp",
            "--add-header", auth_header,
            "--cookies", COOKIES_FILE,
            "--skip-download",
            "--quiet",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0 and os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            content = f.read()
        if "__Secure-3PSID" in content:
            logger.info(f"  Cookies saved to {COOKIES_FILE}")
            # Save refresh token for future re-auth
            tok_path = os.path.join(CHROME_USER_DATA, "yt_refresh_token.json")
            with open(tok_path, "w") as f:
                json.dump({"refresh_token": refresh_token}, f)
            logger.info(f"  Refresh token saved – auto-refresh enabled")
            logger.info("")
            logger.info("  DONE — YouTube is now authenticated!")
            logger.info("  (Cookies will be refreshed automatically if they expire)")
            return

    # Fallback: try extracting cookies from Chrome
    logger.info("  Trying Chrome-based cookie extraction...")
    result = subprocess.run(
        [
            "google-chrome-stable", "--no-sandbox", "--headless=new",
            f"--user-data-dir={CHROME_USER_DATA}",
            f"--auth-server-whitelist=*.youtube.com",
            "--disable-features=ChromeWhatsNewUI",
            "--disable-sync",
            "--no-first-run",
            "https://www.youtube.com",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30,
    )

    # Wait for cookies to be written
    time.sleep(5)

    result = subprocess.run(
        [
            "yt-dlp",
            "--cookies-from-browser", f"chrome:{CHROME_USER_DATA}",
            "--cookies", COOKIES_FILE,
            "--skip-download", "--quiet",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0 and os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            content = f.read()
        if "__Secure-3PSID" in content:
            logger.info(f"  Cookies saved to {COOKIES_FILE}")
            logger.info("")
            logger.info("  DONE — YouTube is now authenticated!")
            return

    logger.error("  Could not extract cookies. Try manually uploading cookies.txt")
    logger.error(f"  Expected location: {COOKIES_FILE}")
    sys.exit(1)


def cmd_refresh(args):
    """Refresh expired cookies using saved refresh token or Chrome profile."""
    # Try refresh token first
    tok_path = os.path.join(CHROME_USER_DATA, "yt_refresh_token.json")
    if os.path.exists(tok_path):
        logger.info("Refreshing cookies via OAuth refresh token...")
        with open(tok_path) as f:
            data = json.load(f)
        refresh_token = data.get("refresh_token", "")

        CLIENT_ID = "861556708454-d6trmres6svigj4e8jk9qtglasqbfh9l.apps.googleusercontent.com"
        CLIENT_SECRET = "SboVDocKvCkBV0lWqBVolFkB"
        token_data = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        try:
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp = urllib.request.urlopen(req)
            tokens = json.loads(resp.read())
            access_token = tokens.get("access_token")

            # Export cookies using yt-dlp with bearer token
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--add-header", f"Authorization: Bearer {access_token}",
                    "--cookies", COOKIES_FILE,
                    "--skip-download", "--quiet",
                    "--print", "title",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(COOKIES_FILE):
                with open(COOKIES_FILE) as f:
                    content = f.read()
                if "__Secure-3PSID" in content:
                    logger.info("Cookies refreshed successfully!")
                    return
        except Exception as e:
            logger.warning(f"Refresh token failed: {e}")

    # Fallback: try Chrome profile
    cookie_db = os.path.join(CHROME_USER_DATA, "Default", "Cookies")
    if os.path.exists(cookie_db):
        logger.info("Refreshing cookies from Chrome profile...")
        result = subprocess.run(
            [
                "yt-dlp",
                "--cookies-from-browser", f"chrome:{CHROME_USER_DATA}",
                "--cookies", COOKIES_FILE,
                "--skip-download", "--quiet",
                "--print", "title",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE) as f:
                content = f.read()
            if "__Secure-3PSID" in content:
                logger.info("Cookies refreshed from Chrome profile!")
                return

    logger.error("Could not refresh cookies. Run `python manage.py auth` again.")
    sys.exit(1)


def cmd_test(args):
    """Test whether yt-dlp can download a YouTube video with current cookies."""
    url = args[0] if args else "https://www.youtube.com/watch?v=Me3TB6nOT_0"
    logger.info(f"Testing: {url}")

    result = subprocess.run(
        [
            "yt-dlp",
            "--cookies", COOKIES_FILE,
            "--extractor-args", "youtube:player_client=android_tv,m_web,web",
            "--skip-download",
            "--print", "title",
            url,
        ],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0:
        logger.info(f"OK — {result.stdout.strip()}")
    else:
        err = result.stderr[:500]
        if os.path.exists(COOKIES_FILE):
            logger.error(f"FAIL — cookies file exists but may be expired. Run `python manage.py auth` to refresh.")
        else:
            logger.error(f"FAIL — no cookies file found at {COOKIES_FILE}. Run `python manage.py auth` first.")
        logger.error(f"Details: {err}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python manage.py <command> [args...]")
        print("")
        print("Commands:")
        print("  auth       One-time YouTube authentication")
        print("  refresh    Refresh cookies (run periodically or on auth error)")
        print("  test       Test cookies against a YouTube video")
        sys.exit(1)

    command = sys.argv[1]
    rest = sys.argv[2:]

    if command == "auth":
        cmd_auth(rest)
    elif command == "refresh":
        cmd_refresh(rest)
    elif command == "test":
        cmd_test(rest)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
