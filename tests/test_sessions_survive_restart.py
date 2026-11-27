"""A deploy replaces the dashboard's pod: sessions and half-done sign-ins must not live in that pod's memory."""
import secrets
import time

from fastapi.testclient import TestClient
from test_dashboard import FakeHeimdall, FakeLogin

import dapp_login
from dapp_dashboard.app import Session, SessionBox, create_app


def app(monkeypatch, key="k1"):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "ai")
    monkeypatch.setenv("DAPP_SESSION_KEY", key)
    for v in ("DAPP_DEV", "DAPP_PROMETHEUS_URL", "DAPP_OIDC_ISSUER", "DAPP_OIDC_CLIENT_ID", "clientSecret"):
        monkeypatch.delenv(v, raising=False)
    return create_app(heimdall=FakeHeimdall(), login=FakeLogin())


def test_signed_in_after_a_restart_and_out_with_another_key(monkeypatch):
    c = TestClient(app(monkeypatch), base_url="https://testserver")
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    assert c.get("/", follow_redirects=False).status_code == 200

    new_pod = TestClient(app(monkeypatch), base_url="https://testserver", cookies=c.cookies)   # same key
    assert new_pod.get("/", follow_redirects=False).status_code == 200
    other = TestClient(app(monkeypatch, key="other"), base_url="https://testserver", cookies=c.cookies)
    assert other.get("/", follow_redirects=False).status_code == 303           # another key can't read it


def test_cookie_is_encrypted_and_big_sessions_split():
    box = SessionBox(b"k")
    s = Session("eyJ" + secrets.token_urlsafe(4500), "alice", "ai", "csrf", time.time() + 60,
                secrets.token_urlsafe(1000), time.time() + 300)
    sealed = box.seal(s)
    assert "alice" not in sealed and "eyJ" not in sealed

    class Resp:
        def __init__(self):
            self.set, self.deleted = {}, []

        def set_cookie(self, name, value, **kw):
            self.set[name] = value

        def delete_cookie(self, name, **kw):
            self.deleted.append(name)

    r = Resp()
    box.write(r, s, path="/dapp")
    assert len(r.set) >= 2 and all(len(v) <= SessionBox.CHUNK for v in r.set.values())
    assert box.read(r.set) == s
    assert box.read({"dapp_session": "garbage"}) is None


def test_another_pod_finishes_the_sign_in(monkeypatch):
    monkeypatch.setattr(dapp_login.Login, "metadata", lambda self: {"authorization_endpoint": "https://idp/auth",
                                                                       "token_endpoint": "https://idp/token"})
    sent = {}
    monkeypatch.setattr(dapp_login.Login, "_token_request",
                        lambda self, form: sent.update(form) or {"access_token": "t", "expires_at": time.time() + 60})
    monkeypatch.setenv("DAPP_PUBLIC_URL", "https://x/app")
    pod_a, pod_b = dapp_login.Login(), dapp_login.Login()
    _, cookie = pod_a.start("/app/page")
    state, verifier, back = dapp_login.Login.read_state(cookie)
    assert back == "/app/page"
    assert pod_b.exchange(state, "code", verifier)["access_token"] == "t"     # pod B never saw the start
    assert sent["code_verifier"] == verifier.rpartition(".")[0]
    assert pod_b.exchange(state, "code") is None                               # no verifier: nothing to finish with
    assert pod_b.exchange(state, "code", verifier.rpartition(".")[0] + ".1") is None   # expired
    assert dapp_login.Login.read_state("st|/old") == ("st", "", "/old")        # cookies from before this change


def test_two_sign_ins_at_once_dont_clash(monkeypatch):
    """A page and a background request both sent to /login: each sign-in keeps its own state cookie."""
    n = iter(range(100))

    class TwoStates(FakeLogin):
        def start(self, return_to):
            st = f"st{next(n)}"
            return f"https://x.dataos.cloud/oidc/auth?state={st}", f"{st}|v.9999999999|{return_to}"

        def exchange(self, state, code, verifier=""):
            return super().exchange("st1", code) if state == "st0" and verifier else None

    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DAPP_SESSION_KEY", "k")
    c = TestClient(create_app(heimdall=FakeHeimdall(), login=TwoStates()), base_url="https://testserver")
    c.get("/login", follow_redirects=False)      # st0: the one the browser follows to DataOS
    c.get("/login", follow_redirects=False)      # st1: started by a second request
    r = c.get("/_dapp/callback?code=good-code&state=st0", follow_redirects=False)
    assert "login-failed" not in r.headers["location"] and c.get("/", follow_redirects=False).status_code == 200
