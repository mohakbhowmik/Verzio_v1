"""
reset_admin_password.py: forgot the admin password? Run this ON THE SERVER:

    docker exec -it verzio_saas python reset_admin_password.py

It removes the password set in Admin -> Password, so ADMIN_PASSWORD from
~/verzio/.env works again (look it up there; never paste it into a chat).
Then sign in and set a new one in Admin -> Password.
"""
from account_security import clear_admin_password, ensure_tables

if __name__ == "__main__":
    ensure_tables()
    if clear_admin_password():
        print("Done. Sign in with ADMIN_PASSWORD from ~/verzio/.env, then change it in Admin -> Password.")
    else:
        print("No custom admin password was set. ADMIN_PASSWORD from ~/verzio/.env is already the one to use.")
