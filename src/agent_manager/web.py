"""Web blueprint — login, logout, change-password UI.

Sessions are Flask's signed-cookie sessions. We never store the
password, just ``{"user": <name>, "uid": <n>}``. Login validates
against :mod:`agent_manager.auth` (crypt(3) vs /etc/shadow).

If the operator populated ``server.web_allowed_users`` in the config,
only those accounts can log in — others see a 403 even if the
credentials are correct. This lets you expose the web UI on a wider
network while restricting it to, say, the SRE team.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .auth import (
    AuthError,
    InvalidCredentials,
    PasswordChangeFailed,
    UserNotFound,
    WeakPassword,
    authenticate,
    change_password,
)

bp = Blueprint("web", __name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg() -> Any:
    return current_app.config["AGENT_MANAGER_CONFIG"]


def _is_web_user_allowed(username: str) -> bool:
    allowed = _cfg().server.web_allowed_users
    if not allowed:
        return True
    return username in allowed


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("web.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
def index():
    if session.get("user"):
        return redirect(url_for("web.change_password_view"))
    return redirect(url_for("web.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            result = authenticate(username, password)
        except UserNotFound:
            flash("Invalid credentials", "error")
        except InvalidCredentials:
            flash("Invalid credentials", "error")
        except AuthError as exc:
            # configuration / environment issue — surface as 500
            current_app.logger.exception("auth backend error")
            return render_template("login.html", error=str(exc)), 500
        else:
            if not _is_web_user_allowed(result.user):
                flash("Account not permitted to use the web UI", "error")
            else:
                session.clear()
                session["user"] = result.user
                session["uid"] = result.uid
                nxt = request.args.get("next") or url_for("web.change_password_view")
                return redirect(nxt)

    return render_template("login.html")


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password_view():
    username = session.get("user", "")
    if request.method == "POST":
        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if new_pw != confirm:
            flash("New password and confirmation do not match", "error")
        else:
            try:
                # Re-auth to prevent session-fixation / cookie-theft
                # attacks from being a one-step password reset.
                authenticate(username, current_pw)
                change_password(username, new_pw)
            except UserNotFound:
                # Should not happen — session was issued for a real user.
                session.clear()
                flash("Account no longer exists; please log in again.", "error")
                return redirect(url_for("web.login"))
            except InvalidCredentials:
                flash("Current password is incorrect", "error")
            except WeakPassword as exc:
                flash(f"Weak password: {exc.reason}", "error")
            except PasswordChangeFailed as exc:
                current_app.logger.error("chpasswd failed for %s: %s", username, exc)
                flash("Failed to update password; check daemon logs.", "error")
            else:
                flash("Password updated successfully", "success")
                return redirect(url_for("web.change_password_view"))

    return render_template("change_password.html", username=username)


__all__ = ["bp"]