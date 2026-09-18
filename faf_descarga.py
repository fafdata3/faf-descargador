# -*- coding: utf-8 -*-
"""FAF Stats - descargador portatil para tu PC.

Descargado desde la web (faf-stats.pages.dev/data/faf_descarga.py), se ejecuta
en cualquier ordenador con python:

    python faf_descarga.py

Hace TODO automatico:
  1. crea un entorno virtual local e instala las librerias (selenium,
     undetected-chromedriver, boto3, ...),
  2. descarga el runner (engine+monolito) publicado en la web,
  3. usa las credenciales de la nube incrustadas (funciona en cualquier
     ordenador, sin configurar nada),
  4. SIEMBRA el corpus local DESDE LA WEB PUBLICA (faf-stats.pages.dev, sin
     pasar por B2: evita el tope Class B que bloquea lecturas del bucket) y
     descarga actas NUEVAS con Chrome real (IP residencial: sin bloqueos),
  5. sube automaticamente las actas y el estado a la nube (S3/B2) de modo que
     el cron de GitHub detecta las nuevas y actualiza la web,
  6. al terminar borra lo descargado de este ordenador (actas/json/html) y
     deja solo el corpus + el entorno para la proxima vez.

Uso rapido (en cualquier PC):
    python faf_descarga.py            # pasada normal (baja/ sube / limpia)
    python faf_descarga.py --todo     # igual que la pasada normal
    python faf_descarga.py --fuentes bizkaia,cantabria
    python faf_descarga.py --min-extra 30
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

URL_RUNNER = "https://faf-stats.pages.dev/data/faf_runner.zip"
URL_WEB_DATA = "https://faf-stats.pages.dev/data"

# Fuentes soportadas (el acta_id de la web ya trae el prefijo: E_10043037...)
PREFIXES = {
    "alava": "", "euskadi": "E_", "gipuzkoa": "G_", "bizkaia": "B_",
    "navarra": "NA_", "rfef": "N_", "rioja": "R_", "cantabria": "C_",
}
DEPENDENCIAS = [
    "requests", "beautifulsoup4", "selenium", "undetected-chromedriver",
    "boto3", "python-dotenv",
    # python 3.12+ quita distutils y undetected-chromedriver lo necesita
    "setuptools", "wheel",
]
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faf_pc_work")
CONFIG_FILE = os.path.join(WORK, "faf_config.json")
VER_MIN = (3, 8)

# Credenciales del almacen B2 incrustadas: el descargador funciona en cualquier
# ordenador sin pedir configuracion. Estan en claro porque la web es publica;
# si cambias el bucket/keys, actualiza este diccionario.
CREDENCIALES = {
    "S3_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
    "S3_ACCESS_KEY": "005f41eb56fe6800000000004",
    "S3_SECRET_KEY": "K005Y3PCLJbP/rnB4XdxAhXGU/7MeZc",
    "S3_BUCKET": "faf-actas",
    "S3_REGION": "us-east-1",
    # Espejo a Supabase (tabla public.actas): cada acta nueva también se
    # inserta ahí (upsert idempotente). Sin esto no rompe nada, pero la tabla
    # queda sin alimentar.
    "SUPABASE_URL": "https://ufgierhigpeitznwdtkn.supabase.co",
    "SUPABASE_SERVICE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVmZ2llcmhpZ3BlaXR6bndkdGtuIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NzI5NzI4MCwiZXhwIjoyMTAyODczMjgwfQ.TBk5joTPYPReUbrFDBAsjYw7BuDabHi_l8E8fKIen9g",
}


def _ok(msg):
    print("[+] " + msg)


def _err(msg):
    print("[-] " + msg)


def _step(msg):
    print("\n==> " + msg)


def comprobar_python():
    ver = sys.version_info[:2]
    if ver < VER_MIN:
        _err(f"Necesitas python {VER_MIN[0]}.{VER_MIN[1]}+ (tienes {'.'.join(map(str, ver))}).")
        print("   Windows:  winget install -e --id Python.Python.3.12")
        print("   macOS:    brew install python")
        print("   Linux:    sudo apt install python3 python3-venv")
        sys.exit(1)
    _ok(f"python {'.'.join(map(str, ver))}")


def entorno_local():
    """Crea (si falta) un venv en WORK/venv y devuelve su python."""
    os.makedirs(WORK, exist_ok=True)
    py = os.path.join(WORK, "venv", "Scripts", "python.exe")
    if not py:
        py = os.path.join(WORK, "venv", "Scripts", "python.exe")
    bin_dir = os.path.dirname(py)
    if os.path.exists(py):
        _ok("entorno virtual ya listo")
        return py
    _step("Creando entorno virtual local...")
    subprocess.run([sys.executable, "-m", "venv", os.path.join(WORK, "venv")],
                   check=True)
    _ok("venv creado")
    _step("Instalando librerias (primera vez, tarda un poco)...")
    subprocess.run(
        [py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=True)
    subprocess.run([py, "-m", "pip", "install", "--quiet"] + DEPENDENCIAS,
                   check=True)
    _ok("librerias instaladas")
    return py


def _descargar(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _step(f"Descargando {os.path.basename(dest)}...")
    # Cloudflare devuelve 403 al User-Agent de urllib; usamos uno de navegador.
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    _ok(f"descargado {os.path.getsize(dest)} bytes")


def _hash(path, chunk=65536):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def runner_local():
    """Baja el runner publicado en la web y lo extrae siempre (así recibe
    actualizaciones). Si el zip es idéntico al anterior, no re-extrae."""
    zip_file = os.path.join(WORK, "faf_runner.zip")
    dest_dir = os.path.join(WORK, "runner")
    old_sum = ""
    if os.path.exists(zip_file):
        try:
            old_sum = _hash(zip_file)
        except Exception:
            pass
    try:
        _descargar(URL_RUNNER, zip_file)
    except Exception as e:
        _err(f"No se pudo bajar el runner: {e}")
        print("   La web se actualiza con el runner cada pocas horas (cron).")
        print("   Reintentalo en un rato, o comprueba la URL si cambió.")
        sys.exit(1)
    if old_sum == _hash(zip_file) and os.path.exists(
            os.path.join(dest_dir, "sync_all.py")):
        _ok("runner ya actualizado")
    else:
        shutil.rmtree(dest_dir, ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(zip_file) as z:
            z.extractall(dest_dir)
        _ok("runner extraido/actualizado")
    return os.path.join(dest_dir, "sync_all.py")


def _leer_env_formato(dotenv_file):
    """Parsea un fichero .env sencillo (clave=valor, comentarios #)."""
    cfg = {}
    try:
        with open(dotenv_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return cfg


def credenciales():
    """Devuelve credenciales incrustadas (funciona en cualquier ordenador).
    Un faf_config.json local o .env pueden sobreescribirlas si alguna vez
    cambian, pero en un PC nuevo no se pide nada."""
    # 1) config guardado (prioridad, p.ej. si se rotaron las claves)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        cfg.setdefault("SUPABASE_URL", CREDENCIALES.get("SUPABASE_URL", ""))
        cfg.setdefault("SUPABASE_SERVICE_KEY",
                       CREDENCIALES.get("SUPABASE_SERVICE_KEY", ""))
        return cfg
    # 2) .env en el mismo directorio que este script (clave=valor)
    dotenv_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_file):
        env_cfg = _leer_env_formato(dotenv_file)
        if env_cfg.get("S3_ENDPOINT") and env_cfg.get("S3_ACCESS_KEY") \
                and env_cfg.get("S3_SECRET_KEY"):
            _ok("credenciales leidas de .env")
            cfg = {
                "S3_ENDPOINT": env_cfg["S3_ENDPOINT"],
                "S3_ACCESS_KEY": env_cfg["S3_ACCESS_KEY"],
                "S3_SECRET_KEY": env_cfg["S3_SECRET_KEY"],
                "S3_BUCKET": env_cfg.get("S3_BUCKET", "faf-actas"),
                "S3_REGION": env_cfg.get("S3_REGION", "auto"),
            }
            cfg.setdefault("SUPABASE_URL", CREDENCIALES.get("SUPABASE_URL", ""))
            cfg.setdefault("SUPABASE_SERVICE_KEY",
                           CREDENCIALES.get("SUPABASE_SERVICE_KEY", ""))
            return cfg
    # 3) incrustadas (default)
    _ok("credenciales incrustadas")
    return dict(CREDENCIALES)


def limpiar_local():
    """Borra lo que no hace falta conservar entre pasadas en este PC.

    Se guarda SOLO el corpus (data/fuentes + state.json): así la siguiente
    pasada no vuelve a bajar 67k actas ni a re-descargar las ya existentes.
    Se borran el runner extraído, el zip y los ficheros temporales de build."""
    data = os.path.join(WORK, "data")
    runner = os.path.join(WORK, "runner")
    zipf = os.path.join(WORK, "faf_runner.zip")
    for p in (runner, zipf):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    build = os.path.join(data, "build")
    try:
        if os.path.isdir(build):
            shutil.rmtree(build, ignore_errors=True)
    except Exception:
        pass
    _step("Limpieza local")
    _ok("borrado: runner y temporales (se conserva el corpus para la proxima vez)")


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _salvar_state(data_dir, state):
    """Escribe data/state.json (atomic: tmp + os.replace). state es un dict
    {fuente: {"high": int, "cursor": int}} para que el runner arranque con
    el high watermark real sin leer B2."""
    p = os.path.join(data_dir, "state.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json.tmp", dir=os.path.dirname(p) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def sembrar_desde_web(data_dir):
    """Reconstruye el corpus local y el estado SIN leer de B2 (que tiene tope
    de transacciones Class B): se bajan del propio sitio web público:
      - sync_status.json  -> high/cursor por fuente -> data/state.json
      - manifest.json + actas.NNN.json (corpus completo) -> fuentes/<f>/json
    Si el corpus ya existe en disco (pasadas siguientes) solo refresca el
    estado. Devuelve el numero de actas disponibles localmente."""
    fuentes_dir = os.path.join(data_dir, "fuentes")
    for k in PREFIXES:
        os.makedirs(os.path.join(fuentes_dir, k, "json"), exist_ok=True)

    # 1) estado: highs/cursors publicados por el cron
    state = {}
    try:
        st = _http_json(f"{URL_WEB_DATA}/sync_status.json")
        for fk in PREFIXES:
            s = (st or {}).get("sources", {}).get(fk) or {}
            high = s.get("high")
            if high is not None:
                state[fk] = {"high": high, "cursor": s.get("cursor")}
    except Exception as e:
        print(f"[web-seed] aviso: no se pudo leer el estado de la web ({e})")

    # 2) ya hay corpus en disco -> solo refrescar estado
    corpus = sum(1 for k in PREFIXES
                 for _ in os.listdir(os.path.join(fuentes_dir, k, "json")))
    if corpus:
        _salvar_state(data_dir, state)
        print(f"[web-seed] corpus ya en disco ({corpus} actas); "
              f"estado refrescado desde la web (sin B2)")
        return corpus

    # 3) corpus fresco: bajar los chunks publicos y escribir acta a acta
    _step("Sembrando corpus desde la web (sin B2, primera vez)...")
    if not state:
        raise RuntimeError(
            "No hay corpus local ni estado publico accesible. Revisa la "
            "conexion a faf-stats.pages.dev y reintenta.")
    try:
        m = _http_json(f"{URL_WEB_DATA}/manifest.json")
    except Exception as e:
        raise RuntimeError(f"No se pudo leer manifest.json: {e}")
    chunks = m.get("chunks") or []
    total = 0
    for i, ch in enumerate(chunks, 1):
        recs = _http_json(f"{URL_WEB_DATA}/{ch}")
        for r in recs or []:
            fk = r.get("fuente")
            aid = r.get("acta_id")
            if not fk or fk not in PREFIXES or not aid:
                continue
            # acta_id ya incluye el prefijo de fuente (p.ej. 'E_10043037'):
            # el fichero JSON es <acta_id>.json tal cual.
            d = os.path.join(fuentes_dir, fk, "json")
            with open(os.path.join(d, f"{aid}.json"), "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False)
            total += 1
        if i % 5 == 0 or i == len(chunks):
            print(f"[web-seed] {i}/{len(chunks)} chunks ({total} actas)")
    _salvar_state(data_dir, state)
    print(f"[web-seed] corpus listo: {total} actas desde la web (sin B2)")
    return total


def main():
    _step("FAF Stats - descargador portatil")
    comprobar_python()
    py = entorno_local()
    runner_py = runner_local()
    cfg = credenciales()

    args = [a for a in sys.argv[1:] if a not in ("--todo",)]
    # SIN --bootstrap: el corpus NO se baja de B2 (tope Class B). Se siembra
    # desde la web publica abajo; el runner solo descarga actas NUEVAS.
    extra = ["--portable", "--once"] + args

    env = dict(os.environ)
    env.update({
        "FAF_DATA_DIR": os.path.join(WORK, "data"),
        "FAF_BUILD_DIR": os.path.join(WORK, "data", "build"),
        "FAF_FAFSTATS_DIR": os.path.join(WORK, "runner", "blobs"),
        "FAF_MONOLITO": os.path.join(WORK, "runner", "blobs", "FUT_stats_fixed.py"),
        "FAF_DEPLOY": "0",
        "FAF_M7": "1",
        "FAF_PACING": "6",
        "FAF_RUN_BUDGET_SEC": "5400",
        "FAF_MAX_PER_RUN": "200",
        "FAF_M7_MAX_PER_RUN": "6000",
        # Chrome con VENTANA real (no headless): el anti-bot de las
        # federaciones detecta mucho menos a un navegador visible con scrolls
        # y ratón que a uno invisible con ritmo constante.
        "FAF_HEADLESS": "0",
        # Sin verificación head_object tras cada subida: cada HEAD es una
        # transacción Class B (B2 la limita a ~2500/día) y subir cientos de
        # actas la agotaría. La verificación la hace el cron al releer.
        "FAF_S3_VERIFY": "0",
        # El corpus viene del web-seed, no del bootstrap S3.
        "FAF_REQUIRE_BOOTSTRAP": "0",
        # Fuentes en hilos paralelos (cada una con su Chrome real e IP
        # residencial): una federación lenta no bloquea a las demás.
        "FAF_PARALLEL": "1",
    })
    for k, v in cfg.items():
        env[k] = v

    sembrar_desde_web(os.path.join(WORK, "data"))

    _step("Lanzando descarga (Chrome real, IP residencial)...")
    rc = subprocess.call([py, runner_py] + extra, env=env)

    if rc != 0:
        _err(f"El runner terminó con error (codigo {rc}). Revisa arriba.")
        sys.exit(rc)
    limpiar_local()
    _ok("Terminado: actas descargadas y subidas a la nube, local limpio.")


if __name__ == "__main__":
    main()