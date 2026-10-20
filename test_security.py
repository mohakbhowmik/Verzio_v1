"""
test_security.py: owner change/forgot password, admin change/recover password,
and the phone menu's owner login. Throwaway database; safe to run anytime:

    python test_security.py
"""
import html, os, re, subprocess, sys, tempfile
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
from cryptography.fernet import Fernet
os.environ.update(
    SCHEDULER="off", DATABASE_URL=f"sqlite:///{tmp.name}", META_APP_SECRET="s", META_ACCESS_TOKEN="x", META_APP_ID="1",
    META_ES_CONFIG_ID="c", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(), VERZIO_ENV="development",
    VERZIO_SECRET_KEY="test-only-secret-key-not-for-production-use", ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="verzio-dev-admin", COOKIE_SECURE="false", PUBLIC_BASE_URL="https://verzio.codes")
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
import server
from database import SessionLocal, Business, Owner
from owner.owner_auth import hash_password, verify_password, SESSION_COOKIE_NAME
from account_security import PasswordReset

fails = 0
def check(label, ok, extra=""):
    global fails; fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if not ok and extra else ""))

HTML = {"Accept": "text/html"}
ALL = {d: ["09:00", "18:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

def owner_login(c, password):
    c.cookies.clear()
    r = c.post("/owner/login", data={"identifier": "o@glow.test", "password": password}, follow_redirects=False)
    tok = r.cookies.get(SESSION_COOKIE_NAME)
    if tok:
        c.cookies.set(SESSION_COOKIE_NAME, tok)
    return r, tok

def admin_login(c, password):
    c.cookies.clear()
    return c.post("/admin/login", data={"username": "admin", "password": password, "next": "/admin"},
                  headers=HTML, follow_redirects=False)

with TestClient(server.app) as c:
    db = SessionLocal()
    b = Business(name="Glow Salon", whatsapp_business_phone_number_id="PN1", manager_phone_number="919800000001",
                 timezone="Asia/Kolkata", operational_hours=ALL, holidays=[], is_active=True, accepting_bookings=True)
    db.add(b); db.commit()
    db.add(Owner(business_id=b.id, full_name="Priya Shah", email="o@glow.test", phone_number="919800000001",
                 password_hash=hash_password("ownerpass123"), is_active=True))
    db.commit()

    # ---------------- owner: change password ----------------
    r, old_token = owner_login(c, "ownerpass123")
    check("owner signs in", r.status_code == 303 and old_token)
    r = c.get("/owner/password")
    check("change-password page loads for a signed-in owner", r.status_code == 200 and 'name="current_password"' in r.text)
    c.cookies.clear()
    r = c.get("/owner/password", follow_redirects=False)
    check("change-password page needs sign-in", r.status_code == 303 and r.headers["location"] == "/owner/login")
    c.cookies.set(SESSION_COOKIE_NAME, old_token)
    for form, why in [({"current_password": "wrong", "new_password": "newpass456", "confirm_password": "newpass456"}, "isn't right"),
                      ({"current_password": "ownerpass123", "new_password": "short", "confirm_password": "short"}, "at least 8"),
                      ({"current_password": "ownerpass123", "new_password": "newpass456", "confirm_password": "newpass457"}, "don't match"),
                      ({"current_password": "ownerpass123", "new_password": "ownerpass123", "confirm_password": "ownerpass123"}, "haven't used")]:
        r = c.post("/owner/password", data=form)
        check(f"change refused: {why}", r.status_code == 200 and why in html.unescape(r.text))
    r = c.post("/owner/password", data={"current_password": "ownerpass123", "new_password": "newpass456",
                                        "confirm_password": "newpass456"}, follow_redirects=False)
    new_token = r.cookies.get(SESSION_COOKIE_NAME)
    check("change accepted, this device gets a fresh session", r.status_code == 303 and new_token and new_token != old_token)
    db.expire_all(); o = db.query(Owner).first()
    check("new password saved (hashed)", verify_password("newpass456", o.password_hash) and "newpass456" not in o.password_hash)
    c.cookies.clear(); c.cookies.set(SESSION_COOKIE_NAME, old_token)
    r = c.get("/owner/dashboard", follow_redirects=False)
    check("other devices are signed out (old session no longer works)", r.status_code == 303 and r.headers["location"] == "/owner/login")
    c.cookies.clear(); c.cookies.set(SESSION_COOKIE_NAME, new_token)
    check("this device stays signed in", c.get("/owner/dashboard", follow_redirects=False).status_code == 200)
    r, _ = owner_login(c, "ownerpass123")
    check("old password no longer signs in", r.status_code == 200 and "Invalid" in r.text)
    r, _ = owner_login(c, "newpass456")
    check("new password signs in", r.status_code == 303)

    # ---------------- owner: forgot password ----------------
    c.cookies.clear()
    r = c.get("/owner/login")
    check("login page has a Forgot password link", 'href="/owner/forgot"' in r.text)
    r = c.get("/owner/forgot")
    check("forgot page sends them to Verzio on WhatsApp", r.status_code == 200 and "wa.me/918262858396" in r.text)
    r = c.post(f"/admin/businesses/{b.id}/reset-password", auth=("admin", "verzio-dev-admin"))
    m = re.search(r"https://verzio\.codes/owner/reset/([A-Za-z0-9_\-]+)", r.text)
    check("admin creates a one-time reset link", r.status_code == 200 and m, r.status_code)
    check("admin page has Send on WhatsApp to the owner's number", "wa.me/919800000001?text=" in r.text)
    token = m.group(1) if m else "x"
    r = c.get(f"/owner/reset/{token}")
    check("reset link opens the new-password form", r.status_code == 200 and "Priya Shah" in r.text and 'name="new_password"' in r.text)
    check("reset page isn't cached and doesn't leak the link", r.headers.get("cache-control") == "no-store"
          and r.headers.get("referrer-policy") == "no-referrer")
    r = c.post(f"/owner/reset/{token}", data={"new_password": "abc", "confirm_password": "abc"})
    check("reset refuses a short password", "at least 8" in r.text)
    r = c.post(f"/owner/reset/{token}", data={"new_password": "resetpass789", "confirm_password": "resetpass789"},
               follow_redirects=False)
    check("reset sets the password and signs in", r.status_code == 303 and r.cookies.get(SESSION_COOKIE_NAME))
    r = c.get(f"/owner/reset/{token}")
    check("link works only once", "expired" in r.text and 'name="new_password"' not in r.text)
    r = c.post(f"/owner/reset/{token}", data={"new_password": "hijack12345", "confirm_password": "hijack12345"})
    db.expire_all(); o = db.query(Owner).first()
    check("used link can't set the password again", verify_password("resetpass789", o.password_hash))
    check("random token is refused", "expired" in c.get("/owner/reset/notarealtoken").text)
    # newer link cancels the older one; expired link refused
    r1 = c.post(f"/admin/businesses/{b.id}/reset-password", auth=("admin", "verzio-dev-admin"))
    r2 = c.post(f"/admin/businesses/{b.id}/reset-password", auth=("admin", "verzio-dev-admin"))
    t1 = re.search(r"/owner/reset/([A-Za-z0-9_\-]+)", r1.text).group(1)
    t2 = re.search(r"/owner/reset/([A-Za-z0-9_\-]+)", r2.text).group(1)
    check("a newer link cancels the older one", "expired" in c.get(f"/owner/reset/{t1}").text
          and 'name="new_password"' in c.get(f"/owner/reset/{t2}").text)
    row = db.get(PasswordReset, t2); row.expires_at = datetime.utcnow() - timedelta(minutes=1); db.commit()
    check("an expired link is refused", "expired" in c.get(f"/owner/reset/{t2}").text)
    r = c.post(f"/admin/businesses/{b.id}/reset-password", headers=HTML, follow_redirects=False)
    check("reset links can only be made by the admin", r.status_code in (303, 401))
    owner_login(c, "resetpass789")
    r = c.get("/owner/dashboard")
    check("owner menu links to Change password", 'href="/owner/password"' in r.text)
    r = c.get("/owner/dashboard", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"})
    check("owner phone header links to Change password", 'href="/owner/password"' in r.text)

    # ---------------- admin: change password ----------------
    r = admin_login(c, "verzio-dev-admin")
    adm_old = r.cookies.get("verzio_admin_session")
    check("admin signs in with the .env password", r.status_code == 303 and adm_old)
    c.cookies.set("verzio_admin_session", adm_old, path="/admin")
    r = c.get("/admin/password", headers=HTML)
    check("admin password page loads, linked in the menu", r.status_code == 200 and 'href="/admin/password"' in r.text)
    r = c.post("/admin/password", headers=HTML, data={"current_password": "verzio-dev-admin", "new_password": "short pass",
                                                       "confirm_password": "short pass"})
    check("admin change refused: under 12 characters", "at least 12" in r.text)
    r = c.post("/admin/password", headers=HTML, data={"current_password": "nope", "new_password": "a much longer passphrase",
                                                       "confirm_password": "a much longer passphrase"})
    check("admin change refused: wrong current password", "isn't right" in html.unescape(r.text))
    r = c.post("/admin/password", headers=HTML, follow_redirects=False,
               data={"current_password": "verzio-dev-admin", "new_password": "a much longer passphrase",
                     "confirm_password": "a much longer passphrase"})
    adm_new = r.cookies.get("verzio_admin_session")
    check("admin password changed, this browser stays signed in", r.status_code == 303 and adm_new and adm_new != adm_old)
    c.cookies.clear(); c.cookies.set("verzio_admin_session", adm_old, path="/admin")
    r = c.get("/admin", headers=HTML, follow_redirects=False)
    check("other admin sessions are signed out", r.status_code == 303 and "/admin/login" in r.headers["location"])
    check(".env password no longer works", admin_login(c, "verzio-dev-admin").status_code == 401)
    check("new admin password works", admin_login(c, "a much longer passphrase").status_code == 303)

    # ---------------- admin: recover ----------------
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "reset_admin_password.py"], env=env, capture_output=True, text=True)
    check("reset_admin_password.py clears the stored password", out.returncode == 0 and "Done." in out.stdout, out.stderr[-300:])
    check("after recovery the .env password works again (no restart)", admin_login(c, "verzio-dev-admin").status_code == 303)
    out = subprocess.run([sys.executable, "reset_admin_password.py"], env=env, capture_output=True, text=True)
    check("running it again is harmless", out.returncode == 0 and "already" in out.stdout)

    # ---------------- website: owner login visible on phones ----------------
    c.cookies.clear()
    for path in ["/", "/pricing", "/products/book"]:
        r = c.get(path)
        check(f"{path}: Owner login is in the top menu with a short phone label",
              'class="nav-login" href="/owner/login"' in r.text and ">Log in<" in r.text)
    db.close()

print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
