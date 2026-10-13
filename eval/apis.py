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
}


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
    src = os.path.abspath(os.path.join(APIS_DIR, api))
    stop(api)
    _docker("network", "create", NETWORK, check=False)
    if conf["sidecar"]:
        image, volume, env = conf["sidecar"]
        _docker("run", "-d", "--name", f"rl-{api}-db", "--network", NETWORK,
                "-v", volume.replace("/api", src, 1), "-e", env, image)
        time.sleep(5)
    _docker("run", "-d", "--name", f"rl-{api}", "--network", NETWORK, "-p", f"{conf['port']}:8080",
            "-v", f"{src}:/api:ro", JAVA_IMAGE, "java", "-Dserver.port=8080", *conf["java"])
    base = f"http://localhost:{conf['port']}"
    for _ in range(120):
        try:
            if urllib.request.urlopen(base + conf["probe"], timeout=2).status == 200:
                return base
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"{api} did not start")
