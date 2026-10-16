"""Start/stop the REST APIs under test as Docker containers, from a fresh state.
The API jars come from the RESTgym API repositories (see eval/get_apis.sh)."""
import os
import subprocess
import time
import urllib.request

APIS_DIR = os.environ.get("RL_APIS_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "apis"))
JAVA_IMAGE = "eclipse-temurin:17-jre"
NETWORK = "rl-eval"

APIS = {
    "pet-clinic": {
        "port": 9966, "probe": "/petclinic/api/owners",
        "sidecar": None,
        "java": ["-Dspring.profiles.active=h2,spring-data-jpa", "-jar", "/api/spring-petclinic-rest.jar"],
    },
    "gestao-hospital": {
        "port": 9967, "probe": "/v1/hospitais/",
        "sidecar": ["mongo:6", "/api/database:/docker-entrypoint-initdb.d:ro", "MONGO_INITDB_DATABASE=HospitalDB"],
        "java": ["-Dspring.data.mongodb.host=rl-gestao-hospital-db", "-Dspring.data.mongodb.port=27017",
                 "-Dspring.data.mongodb.database=HospitalDB", "-jar", "/api/gestaohospitalar-0.0.1.jar"],
    },
    # VAmPI: an intentionally vulnerable API (OWASP-style), run from a prebuilt
    # image rather than a jar. Purpose-built to be attacked, so the oracles can
    # be checked against genuine BOLA / mass-assignment / auth bugs instead of
    # test fakes. vulnerable=1 turns the planted bugs on; /createdb seeds users
    # and books and must be hit before each round (done by `setup`).
    "vampi": {
        "port": 9968, "probe": "/",
        "image": "erev0s/vampi:latest", "container_port": 5000,
        "env": ["vulnerable=1"], "setup": "/createdb",
        # VAmPI gates most of its surface behind a JWT. Without logging in the
        # tool only sees the handful of unauthenticated endpoints and reaches
        # none of the planted auth/data bugs. These map to client.py's
        # login-token flow; kept per-API (not global env) so a parallel run of
        # several APIs does not share one credential.
        "auth": {
            "AUTH_LOGIN_PATH": "/users/v1/login",
            "AUTH_LOGIN_BODY": '{"username": "name1", "password": "pass1"}',
            "AUTH_TOKEN_FIELD": "auth_token",
            "AUTH_TOKEN_HEADER": "Authorization",
            "AUTH_TOKEN_PREFIX": "Bearer ",
        },
        # A second, distinct user, so the cross-user (BOLA) oracle can test
        # whether name2's token reaches name1's resources.
        "alt_auth": {
            "AUTH_LOGIN_PATH": "/users/v1/login",
            "AUTH_LOGIN_BODY": '{"username": "name2", "password": "pass2"}',
            "AUTH_TOKEN_FIELD": "auth_token",
            "AUTH_TOKEN_HEADER": "Authorization",
            "AUTH_TOKEN_PREFIX": "Bearer ",
        },
    },
}


def auth_for(api: str):
    """The primary identity's auth provider for `api`, or None. Built from the
    per-API config so concurrent campaigns on different APIs do not share one
    token."""
    return _provider(APIS.get(api, {}).get("auth"))


def alt_auth_for(api: str):
    """A second, distinct identity's auth provider, or None. Used by the
    cross-user oracle; a fresh provider each call so the two never share state."""
    return _provider(APIS.get(api, {}).get("alt_auth"))


def _provider(spec):
    if not spec:
        return None
    from restleague.engine.client import auth_from_env
    return auth_from_env(spec)


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def stop(api: str) -> None:
    _docker("rm", "-f", f"rl-{api}", f"rl-{api}-db", check=False)


def start(api: str, attempts: int = 3) -> str:
    """(Re)start `api` from a clean state and return its base URL once it answers.
    Retries from scratch, because the MongoDB image sometimes fails its first start."""
    for attempt in range(attempts):
        try:
            return _start_once(api)
        except RuntimeError:
            if attempt == attempts - 1:
                raise
    raise RuntimeError(f"{api} did not start")


def _start_once(api: str) -> str:
    conf = APIS[api]
    stop(api)
    _docker("network", "create", NETWORK, check=False)

    if conf.get("image"):
        # A prebuilt image: run it directly, no jar mount. Used by targets that
        # ship their own container (e.g. the intentionally-vulnerable VAmPI).
        args = ["run", "-d", "--name", f"rl-{api}", "--network", NETWORK,
                "-p", f"{conf['port']}:{conf.get('container_port', 8080)}"]
        for e in conf.get("env", []):
            args += ["-e", e]
        _docker(*args, conf["image"])
    else:
        src = os.path.abspath(os.path.join(APIS_DIR, api))
        if conf.get("sidecar"):
            image, volume, env = conf["sidecar"]
            _docker("run", "-d", "--name", f"rl-{api}-db", "--network", NETWORK,
                    "-v", volume.replace("/api", src, 1), "-e", env, image)
            time.sleep(5)
        _docker("run", "-d", "--name", f"rl-{api}", "--network", NETWORK,
                "-p", f"{conf['port']}:8080", "-v", f"{src}:/api:ro",
                JAVA_IMAGE, "java", "-Dserver.port=8080", *conf["java"])

    base = f"http://localhost:{conf['port']}"
    for _ in range(120):
        try:
            if urllib.request.urlopen(base + conf["probe"], timeout=2).status == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError(f"{api} did not start")
    # some targets need a one-shot seeding call before each round
    if conf.get("setup"):
        try:
            urllib.request.urlopen(base + conf["setup"], timeout=10).read()
        except Exception:
            pass
    return base
