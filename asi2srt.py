#!/usr/bin/env python3
"""
asi2srt - gateway DVB-ASI -> SRT em um único programa.

Captura o transport stream de uma entrada DVB-ASI (DekTec, driver Linux ASI/DVEO),
UDP ou arquivo, e o encapsula em SRT usando o TSDuck (tsp) como motor.

Subcomandos:
  rodar             executa o gateway (a partir de config ou direto pela linha de comando)
  init              gera um arquivo de configuração de exemplo
  comando           mostra os comandos tsp que seriam executados (dry-run)
  verificar         confere TSDuck, plugins e placas DekTec
  instalar-tsduck   baixa e instala o TSDuck (Debian/Ubuntu/Fedora/RHEL)
  instalar-servico  instala como serviço systemd (inicia no boot)
  teste             teste ponta a ponta sem hardware (ffmpeg -> gateway -> SRT)

Exemplos rápidos:
  asi2srt rodar --entrada dektec:0:0 --saida "srt://203.0.113.10:9000?latency=200"
  asi2srt rodar --entrada linuxasi:/dev/asirx0 --saida "srt://:9000?mode=listener&passphrase=minhasenha123"
  asi2srt init && asi2srt rodar -c asi2srt.yaml
"""

import argparse
import html
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = "1.0.0"

try:
    import yaml  # opcional: sem PyYAML, use configuração em JSON
except ImportError:
    yaml = None

log = logging.getLogger("asi2srt")

# ======================================================================
# Configuração de exemplo (gerada por "asi2srt init")
# ======================================================================
CONFIG_EXEMPLO = """\
# ==========================================================
#  asi2srt - configuração
#  Captura DVB-ASI  ->  (monitoração / filtros)  ->  SRT
# ==========================================================

# Uma entrada por porta ASI. Cada canal vira um processo tsp
# independente, supervisionado e reiniciado se cair.
canais:
  - nome: "ASI-1"
    ativo: true

    entrada:
      # tipo: dektec | linuxasi | udp | arquivo
      #   dektec   -> placas DekTec (DTA-2145, DTA-2138, DTU-245...) via DTAPI
      #   linuxasi -> driver Linux ASI (DVEO/Computer Modules): /dev/asirx0
      #   udp      -> TS em UDP/multicast
      #   arquivo  -> arquivo .ts em loop (teste sem hardware)
      tipo: dektec
      dektec:
        dispositivo: 0        # índice da placa ("asi2srt verificar" lista as placas)
        canal: 0              # porta de entrada na placa
        timeout_ms: 5000      # sem sinal por este tempo => erro e reinício
      linuxasi:
        dispositivo: /dev/asirx0
      udp:
        endereco: "239.1.1.1:1234"
        interface_local: ""   # IP da placa de rede que recebe o multicast
      arquivo:
        caminho: teste.ts
        loop: true

    processamento:
      remover_nulos: true     # tira o stuffing (PID 0x1FFF) e economiza banda
      remover_pids: []        # PIDs a descartar, ex.: [0x0012, 0x0014]
      bitrate_min: 1000000    # alarme abaixo disto (bit/s). 0 = desligado
      bitrate_max: 0          # alarme acima disto (bit/s). 0 = desligado
      intervalo_relatorio_s: 5

    saida:
      modo: caller            # caller (conecta no destino) | listener (aguarda conexão)
      endereco: "203.0.113.10:9000"   # caller: host:porta | listener: porta ou ip:porta
      latencia_ms: 200        # 3 a 4x o RTT do link
      senha: ""               # passphrase AES (10 a 79 caracteres). Vazio = sem criptografia
      tamanho_chave: 16       # 16, 24 ou 32 (AES-128/192/256)
      stream_id: ""           # para SRT Live Server, Haivision, etc.
      overhead_pct: 25        # banda extra reservada para retransmissões

supervisor:
  reinicio_min_s: 2
  reinicio_max_s: 60
  tsp: tsp                    # caminho do executável do TSDuck
  status_http: "0.0.0.0:8088" # painel web e /status.json. Vazio desativa
  log_arquivo: ""             # ex.: /var/log/asi2srt.log
"""


def carregar_config(caminho: str) -> dict:
    with open(caminho, encoding="utf-8") as f:
        texto = f.read()
    if caminho.lower().endswith(".json"):
        return json.loads(texto)
    if yaml is None:
        sys.exit("Para configuração YAML instale o PyYAML (pip install pyyaml) "
                 "ou use um arquivo .json com a mesma estrutura.")
    return yaml.safe_load(texto) or {}


# ======================================================================
# Atalhos de linha de comando: --entrada / --saida
# ======================================================================
def entrada_de_texto(txt: str) -> dict:
    """dektec:0:0 | linuxasi:/dev/asirx0 | udp:239.1.1.1:1234[@ip_local] | arquivo:x.ts"""
    tipo, _, resto = txt.partition(":")
    tipo = tipo.lower()
    if tipo == "dektec":
        partes = (resto or "0:0").split(":")
        return {"tipo": "dektec", "dektec": {
            "dispositivo": int(partes[0] or 0),
            "canal": int(partes[1]) if len(partes) > 1 else 0,
            "timeout_ms": 5000}}
    if tipo == "linuxasi":
        return {"tipo": "linuxasi", "linuxasi": {"dispositivo": resto or "/dev/asirx0"}}
    if tipo == "udp":
        end, _, local = resto.partition("@")
        return {"tipo": "udp", "udp": {"endereco": end, "interface_local": local}}
    if tipo in ("arquivo", "file"):
        return {"tipo": "arquivo", "arquivo": {"caminho": resto, "loop": True}}
    raise SystemExit(f"entrada inválida: {txt!r} (use dektec:N:C, linuxasi:/dev/asirxN, "
                     "udp:ip:porta ou arquivo:caminho.ts)")


def saida_de_url(url: str) -> dict:
    """srt://host:porta?mode=caller|listener&latency=ms&passphrase=x&pbkeylen=16&streamid=y"""
    u = urlparse(url)
    if u.scheme != "srt":
        raise SystemExit(f"saída inválida: {url!r} (esperado srt://host:porta?...)")
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    modo = q.get("mode", "listener" if not u.hostname else "caller")
    if modo == "caller":
        if not u.hostname or not u.port:
            raise SystemExit("modo caller precisa de host e porta: srt://host:porta")
        endereco = f"{u.hostname}:{u.port}"
    else:
        endereco = f"{u.hostname}:{u.port}" if u.hostname and u.hostname != "0.0.0.0" else str(u.port)
    return {"modo": modo, "endereco": endereco,
            "latencia_ms": int(q.get("latency", 200)),
            "senha": q.get("passphrase", ""),
            "tamanho_chave": int(q.get("pbkeylen", 16)),
            "stream_id": q.get("streamid", ""),
            "overhead_pct": int(q.get("oheadbw", 25))}


# ======================================================================
# Montagem do comando tsp
# ======================================================================
def _pid(valor):
    return valor if isinstance(valor, int) else int(str(valor), 0)


def montar_comando(canal: dict, tsp_bin: str = "tsp") -> list:
    ent = canal["entrada"]
    proc = canal.get("processamento") or {}
    sai = canal["saida"]

    cmd = [tsp_bin, "--realtime", "--buffer-size-mb", "8", "--max-flushed-packets", "7"]

    tipo = ent["tipo"]
    if tipo == "dektec":
        d = ent.get("dektec") or {}
        cmd += ["-I", "dektec",
                "--device", str(d.get("dispositivo", 0)),
                "--channel", str(d.get("canal", 0)),
                "--receive-timeout", str(d.get("timeout_ms", 5000))]
    elif tipo == "linuxasi":
        # O driver Linux ASI entrega o TS como dispositivo de caracteres (modo 188 bytes).
        cmd += ["-I", "file", (ent.get("linuxasi") or {}).get("dispositivo", "/dev/asirx0")]
    elif tipo == "udp":
        u = ent.get("udp") or {}
        cmd += ["-I", "ip", u["endereco"]]
        if u.get("interface_local"):
            cmd += ["--local-address", u["interface_local"]]
        cmd += ["--receive-timeout", "5000"]
    elif tipo == "arquivo":
        a = ent.get("arquivo") or {}
        cmd += ["-I", "file", a["caminho"]]
        if a.get("loop", True):
            cmd += ["--infinite"]
    else:
        raise ValueError(f"tipo de entrada desconhecido: {tipo}")

    cmd += ["-P", "continuity"]

    pids = [_pid(p) for p in (proc.get("remover_pids") or [])]
    if proc.get("remover_nulos", True):
        pids.append(0x1FFF)
    if pids:
        cmd += ["-P", "filter", "--negate"]
        for p in sorted(set(pids)):
            cmd += ["--pid", f"0x{p:04X}"]

    intervalo = str(proc.get("intervalo_relatorio_s", 5))
    cmd += ["-P", "bitrate_monitor", "--periodic-bitrate", intervalo, "--time-interval", intervalo]
    if proc.get("bitrate_min"):
        cmd += ["--min", str(proc["bitrate_min"])]
    if proc.get("bitrate_max"):
        cmd += ["--max", str(proc["bitrate_max"])]

    cmd += ["-O", "srt"]
    modo = sai.get("modo", "caller")
    if modo == "caller":
        cmd += ["--caller", str(sai["endereco"])]
    elif modo == "listener":
        cmd += ["--listener", str(sai["endereco"])]
    else:
        raise ValueError(f"modo SRT desconhecido: {modo}")

    cmd += ["--transtype", "live",
            "--latency", str(sai.get("latencia_ms", 200)),
            "--ohead-bandwidth", str(sai.get("overhead_pct", 25))]
    if sai.get("senha"):
        if not 10 <= len(sai["senha"]) <= 79:
            raise ValueError("a senha SRT deve ter entre 10 e 79 caracteres")
        cmd += ["--passphrase", sai["senha"], "--pbkeylen", str(sai.get("tamanho_chave", 16))]
    if sai.get("stream_id"):
        cmd += ["--streamid", sai["stream_id"]]
    return cmd


def comando_para_log(cmd: list) -> str:
    out = list(cmd)
    for i, a in enumerate(out[:-1]):
        if a == "--passphrase":
            out[i + 1] = "********"
    return " ".join(shlex.quote(a) for a in out)


# ======================================================================
# Supervisor de um canal
# ======================================================================
RE_BITRATE = re.compile(r"bitrate_monitor:.*?([\d,.]+)\s*b(?:its)?/s", re.I)
RE_CC = re.compile(r"continuity:.*(missing|discontinuity|duplicate)", re.I)
RE_ALARME = re.compile(r"bitrate_monitor:.*(lower|greater|exceed|below|above|alarm)", re.I)


class Canal(threading.Thread):
    def __init__(self, cfg: dict, sup: dict):
        super().__init__(daemon=True, name=cfg.get("nome", "canal"))
        self.cfg = cfg
        self.sup = sup
        self.cmd = montar_comando(cfg, sup.get("tsp", "tsp"))
        self.proc = None
        self.parar = threading.Event()
        self.lock = threading.Lock()
        self.st = {
            "nome": self.name, "estado": "iniciando", "bitrate_bps": 0,
            "erros_cc": 0, "alarmes_bitrate": 0, "reinicios": 0,
            "inicio_execucao": None, "ultimo_bitrate_em": None, "ultima_msg": "",
            "destino": f'{cfg["saida"].get("modo", "caller")} {cfg["saida"]["endereco"]}',
            "entrada": cfg["entrada"]["tipo"],
        }

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def status(self):
        with self.lock:
            s = dict(self.st)
        if s["inicio_execucao"]:
            s["uptime_s"] = int(time.time() - s["inicio_execucao"])
        intervalo = (self.cfg.get("processamento") or {}).get("intervalo_relatorio_s", 5)
        if s["estado"] == "rodando" and s["ultimo_bitrate_em"] \
                and time.time() - s["ultimo_bitrate_em"] > 3 * intervalo:
            s["estado"] = "sem_dados"
        return s

    def _ler_saida(self, stream):
        for linha in iter(stream.readline, ""):
            linha = linha.rstrip()
            if not linha:
                continue
            m = RE_BITRATE.search(linha)
            if m:
                bps = int(float(m.group(1).replace(",", "")))
                self._set(bitrate_bps=bps, ultimo_bitrate_em=time.time(), estado="rodando")
                log.debug("[%s] %s", self.name, linha)
                continue
            if RE_CC.search(linha):
                with self.lock:
                    self.st["erros_cc"] += 1
                log.warning("[%s] %s", self.name, linha)
            elif RE_ALARME.search(linha):
                with self.lock:
                    self.st["alarmes_bitrate"] += 1
                log.warning("[%s] %s", self.name, linha)
            else:
                log.info("[%s] %s", self.name, linha)
            self._set(ultima_msg=linha[-300:])

    def run(self):
        espera_min = self.sup.get("reinicio_min_s", 2)
        espera_max = self.sup.get("reinicio_max_s", 60)
        espera = espera_min
        while not self.parar.is_set():
            log.info("[%s] iniciando: %s", self.name, comando_para_log(self.cmd))
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, bufsize=1)
            except FileNotFoundError:
                log.error("[%s] '%s' não encontrado. Rode: asi2srt instalar-tsduck",
                          self.name, self.cmd[0])
                self._set(estado="erro", ultima_msg="tsp não encontrado")
                self.parar.wait(espera_max)
                continue

            self._set(estado="conectando", inicio_execucao=t0)
            self._ler_saida(self.proc.stdout)
            rc = self.proc.wait()
            if self.parar.is_set():
                break
            if time.time() - t0 > 60:  # rodou estável: zera o backoff
                espera = espera_min
            with self.lock:
                self.st["reinicios"] += 1
            self._set(estado="reiniciando", bitrate_bps=0, inicio_execucao=None)
            log.error("[%s] tsp saiu (código %s). Reiniciando em %ss.", self.name, rc, espera)
            self.parar.wait(espera)
            espera = min(espera * 2, espera_max)
        self._set(estado="parado")

    def encerrar(self):
        self.parar.set()
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ======================================================================
# Painel de status HTTP
# ======================================================================
PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3"><title>ASI → SRT</title>
<style>body{{font-family:system-ui,sans-serif;margin:16px;background:#111;color:#eee}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border-bottom:1px solid #333;text-align:left}}
.rodando{{color:#4ade80}}.conectando,.reiniciando,.iniciando{{color:#facc15}}
.erro,.sem_dados,.parado{{color:#f87171}}small{{color:#999}}a{{color:#93c5fd}}
.wrap{{overflow-x:auto}}</style></head><body><h2>Gateway ASI → SRT</h2><div class="wrap">
<table><tr><th>Canal</th><th>Estado</th><th>Entrada</th><th>Destino SRT</th><th>Bitrate</th>
<th>Erros CC</th><th>Alarmes</th><th>Reinícios</th><th>Uptime</th></tr>{linhas}</table></div>
<p><small>asi2srt {versao} · atualiza a cada 3 s · <a href="/status.json">status.json</a></small></p>
</body></html>"""


def servidor_status(canais, endereco):
    host, _, porta = endereco.rpartition(":")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            dados = [c.status() for c in canais]
            if self.path.startswith("/status.json"):
                corpo = json.dumps(dados, ensure_ascii=False, indent=2).encode()
                tipo = "application/json"
            else:
                linhas = "".join(
                    f'<tr><td>{html.escape(d["nome"])}</td>'
                    f'<td class="{d["estado"]}">{d["estado"]}</td>'
                    f'<td>{html.escape(d["entrada"])}</td><td>{html.escape(d["destino"])}</td>'
                    f'<td>{d["bitrate_bps"] / 1e6:.2f} Mb/s</td><td>{d["erros_cc"]}</td>'
                    f'<td>{d["alarmes_bitrate"]}</td><td>{d["reinicios"]}</td>'
                    f'<td>{d.get("uptime_s", 0)} s</td></tr>' for d in dados)
                corpo = PAGINA.format(linhas=linhas, versao=__version__).encode()
                tipo = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    srv = ThreadingHTTPServer((host or "0.0.0.0", int(porta)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("painel de status: http://%s:%s/", host or "0.0.0.0", porta)
    return srv


# ======================================================================
# Subcomandos
# ======================================================================
def _config_da_linha(args) -> dict:
    """Monta a config a partir de -c ou de --entrada/--saida."""
    if args.entrada or args.saida:
        if not (args.entrada and args.saida):
            raise SystemExit("use --entrada e --saida juntos (ou -c arquivo.yaml)")
        canal = {"nome": args.nome, "entrada": entrada_de_texto(args.entrada),
                 "saida": saida_de_url(args.saida),
                 "processamento": {"remover_nulos": not args.manter_nulos,
                                   "bitrate_min": args.bitrate_min,
                                   "intervalo_relatorio_s": 5}}
        return {"canais": [canal], "supervisor": {"status_http": args.status, "tsp": args.tsp}}
    caminho = args.config or _achar_config()
    if not caminho:
        raise SystemExit("nenhuma configuração. Use -c arquivo.yaml, --entrada/--saida, "
                         "ou gere uma com: asi2srt init")
    cfg = carregar_config(caminho)
    sup = cfg.setdefault("supervisor", {}) or {}
    if args.tsp != "tsp":
        sup["tsp"] = args.tsp
    return cfg


def _achar_config():
    for c in ("asi2srt.yaml", "asi2srt.yml", "asi2srt.json", "config.yaml",
              "/etc/asi2srt/config.yaml", "/etc/asi2srt/config.json"):
        if os.path.exists(c):
            return c
    return None


def executar_gateway(cfg: dict):
    sup = cfg.get("supervisor") or {}
    if sup.get("log_arquivo"):
        logging.getLogger().addHandler(logging.FileHandler(sup["log_arquivo"]))
    ativos = [c for c in cfg.get("canais", []) if c.get("ativo", True)]
    if not ativos:
        raise SystemExit("nenhum canal ativo na configuração")

    canais = [Canal(c, sup) for c in ativos]
    log.info("asi2srt %s - %d canal(is)", __version__, len(canais))
    for c in canais:
        c.start()
    if sup.get("status_http"):
        try:
            servidor_status(canais, sup["status_http"])
        except OSError as e:
            log.error("não foi possível abrir o painel em %s: %s", sup["status_http"], e)

    fim = threading.Event()

    def sair(*_):
        log.info("encerrando...")
        for c in canais:
            c.encerrar()
        fim.set()

    signal.signal(signal.SIGINT, sair)
    signal.signal(signal.SIGTERM, sair)
    fim.wait()


def cmd_rodar(args):
    executar_gateway(_config_da_linha(args))


def cmd_comando(args):
    cfg = _config_da_linha(args)
    tsp = (cfg.get("supervisor") or {}).get("tsp", "tsp")
    for c in cfg.get("canais", []):
        if c.get("ativo", True):
            print(f"# {c.get('nome', 'canal')}")
            print(comando_para_log(montar_comando(c, tsp)) + "\n")


def cmd_init(args):
    if os.path.exists(args.arquivo) and not args.forcar:
        raise SystemExit(f"{args.arquivo} já existe (use --forcar para sobrescrever)")
    with open(args.arquivo, "w", encoding="utf-8") as f:
        f.write(CONFIG_EXEMPLO)
    print(f"configuração criada: {args.arquivo}\n"
          f"edite a entrada ASI e o destino SRT, depois rode: asi2srt rodar -c {args.arquivo}")


def cmd_verificar(args):
    ok = True
    tsp = shutil.which(args.tsp) or (args.tsp if os.path.exists(args.tsp) else None)
    if not tsp:
        print("✗ TSDuck (tsp) não encontrado  ->  asi2srt instalar-tsduck")
        return 1
    ver = subprocess.run([tsp, "--version"], capture_output=True, text=True)
    print(f"✓ TSDuck: {(ver.stdout or ver.stderr).strip().splitlines()[0]}")
    plugins = subprocess.run([tsp, "--list-plugins"], capture_output=True, text=True)
    texto = plugins.stdout + plugins.stderr
    for nome, obrig in (("srt", True), ("dektec", False), ("continuity", True),
                        ("bitrate_monitor", True), ("filter", True)):
        tem = re.search(rf"\b{nome}\b", texto) is not None
        print(f"{'✓' if tem else ('✗' if obrig else '·')} plugin {nome}"
              f"{'' if tem or obrig else ' (só necessário para placas DekTec)'}")
        ok &= tem or not obrig
    if shutil.which("tsdektec"):
        r = subprocess.run(["tsdektec", "-a"], capture_output=True, text=True)
        print("\nPlacas DekTec:\n" + ((r.stdout or r.stderr).strip() or "  nenhuma encontrada"))
    asi = sorted(f for f in os.listdir("/dev") if f.startswith("asirx")) if os.path.isdir("/dev") else []
    print("\nDispositivos Linux ASI: " + (", ".join("/dev/" + a for a in asi) or "nenhum"))
    print("PyYAML: " + ("instalado" if yaml else "ausente (use config .json ou pip install pyyaml)"))
    return 0 if ok else 1


def cmd_instalar_tsduck(args):
    if os.geteuid() != 0:
        raise SystemExit("rode como root: sudo asi2srt instalar-tsduck")
    if shutil.which("tsp") and not args.forcar:
        print("TSDuck já instalado (use --forcar para reinstalar)")
        return
    info = {}
    with open("/etc/os-release") as f:
        for linha in f:
            k, _, v = linha.strip().partition("=")
            info[k] = v.strip('"')
    distro, versao = info.get("ID", ""), info.get("VERSION_ID", "")
    arq = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    deb = distro in ("ubuntu", "debian") or "debian" in info.get("ID_LIKE", "")
    print(f"sistema: {distro} {versao} ({arq}) - buscando a versão mais recente do TSDuck...")
    req = urllib.request.Request("https://api.github.com/repos/tsduck/tsduck/releases/latest",
                                 headers={"User-Agent": "asi2srt"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    nomes = [a for a in rel["assets"] if "-dev" not in a["name"] and "devel" not in a["name"]]
    if deb:
        cand = [a for a in nomes if a["name"].endswith(f"_{arq}.deb")]
        pref = [a for a in cand if f"{distro}{versao}".replace(".", "") in a["name"].replace(".", "")]
    else:
        arq_rpm = {"amd64": "x86_64", "arm64": "aarch64"}.get(arq, arq)
        cand = [a for a in nomes if a["name"].endswith(f".{arq_rpm}.rpm")]
        pref = [a for a in cand if f"el{versao.split('.')[0]}" in a["name"]
                or f"fc{versao}" in a["name"]]
    escolha = (pref or cand or [None])[0]
    if not escolha:
        raise SystemExit("não achei pacote para este sistema; baixe em https://tsduck.io/download/")
    destino = os.path.join(tempfile.gettempdir(), escolha["name"])
    print(f"baixando {escolha['name']}...")
    urllib.request.urlretrieve(escolha["browser_download_url"], destino)
    if deb:
        subprocess.run(["apt-get", "update"], check=False)
        subprocess.run(["apt-get", "install", "-y", destino], check=True)
    else:
        gerenciador = "dnf" if shutil.which("dnf") else "yum"
        subprocess.run([gerenciador, "install", "-y", destino], check=True)
    print("TSDuck instalado. Confira com: asi2srt verificar")


UNIDADE_SYSTEMD = """\
[Unit]
Description=Gateway DVB-ASI -> SRT (asi2srt)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe} rodar -c {config}
Restart=always
RestartSec=3
Nice=-10
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=50
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
"""


def cmd_instalar_servico(args):
    if os.geteuid() != 0:
        raise SystemExit("rode como root: sudo asi2srt instalar-servico")
    # copia este programa para /usr/local/bin
    origem = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    destino = "/usr/local/bin/asi2srt"
    if os.path.abspath(origem) != destino:
        shutil.copy2(origem, destino)
        os.chmod(destino, 0o755)
    exe = destino if getattr(sys, "frozen", False) else f"{sys.executable} {destino}"
    os.makedirs(os.path.dirname(args.config), exist_ok=True)
    if not os.path.exists(args.config):
        with open(args.config, "w", encoding="utf-8") as f:
            f.write(CONFIG_EXEMPLO)
        print(f"configuração de exemplo criada em {args.config} - EDITE antes de iniciar")
    with open("/etc/systemd/system/asi2srt.service", "w") as f:
        f.write(UNIDADE_SYSTEMD.format(exe=exe, config=args.config))
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print(f"serviço instalado.\n"
          f"  1. edite {args.config}\n"
          f"  2. sudo systemctl enable --now asi2srt\n"
          f"  3. journalctl -u asi2srt -f\n"
          f"  4. painel: http://<ip-do-servidor>:8088/")


def cmd_teste(args):
    if not shutil.which("ffmpeg"):
        raise SystemExit("o teste precisa do ffmpeg (apt install ffmpeg)")
    pasta = tempfile.mkdtemp(prefix="asi2srt-teste-")
    ts = os.path.join(pasta, "teste.ts")
    print("gerando TS de teste (barras + tom de 1 kHz, 30 s)...")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=frequency=1000",
                    "-t", "30", "-c:v", "libx264", "-b:v", "4M", "-c:a", "aac",
                    "-f", "mpegts", ts], check=True)
    senha = "asi2srt-teste"
    cfg = {"canais": [{"nome": "TESTE",
                       "entrada": {"tipo": "arquivo", "arquivo": {"caminho": ts, "loop": True}},
                       "processamento": {"bitrate_min": 0, "intervalo_relatorio_s": 2},
                       "saida": {"modo": "listener", "endereco": str(args.porta),
                                 "latencia_ms": 120, "senha": senha}}],
           "supervisor": {"status_http": "127.0.0.1:8088", "tsp": args.tsp}}
    url = f"srt://127.0.0.1:{args.porta}?mode=caller&passphrase={senha}&latency=120000"
    print(f"\nreceptor:  ffplay \"{url}\"\npainel:    http://127.0.0.1:8088/\nCtrl+C para sair\n")
    if shutil.which("ffplay") and not args.sem_player:
        threading.Timer(2, lambda: subprocess.Popen(
            ["ffplay", "-loglevel", "warning", url])).start()
    executar_gateway(cfg)


# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="asi2srt",
        description="Gateway DVB-ASI -> SRT (motor TSDuck).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Entradas: dektec:PLACA:CANAL | linuxasi:/dev/asirxN | udp:IP:PORTA[@IP_LOCAL] | arquivo:X.ts\n"
               "Saída:    srt://HOST:PORTA?mode=caller|listener&latency=MS&passphrase=SENHA&streamid=ID")
    ap.add_argument("--versao", action="version", version=f"asi2srt {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="log detalhado")
    sub = ap.add_subparsers(dest="cmd", metavar="subcomando")

    def opcoes_canal(p):
        p.add_argument("-c", "--config", help="arquivo YAML/JSON (padrão: procura asi2srt.yaml, /etc/asi2srt/config.yaml)")
        p.add_argument("-e", "--entrada", help="atalho sem config, ex.: dektec:0:0")
        p.add_argument("-s", "--saida", help='atalho sem config, ex.: "srt://host:9000?latency=200"')
        p.add_argument("--nome", default="ASI-1")
        p.add_argument("--manter-nulos", action="store_true", help="não remover pacotes nulos")
        p.add_argument("--bitrate-min", type=int, default=0, help="alarme de bitrate mínimo (bit/s)")
        p.add_argument("--status", default="0.0.0.0:8088", help="endereço do painel ('' desativa)")
        p.add_argument("--tsp", default="tsp", help="caminho do tsp")

    p = sub.add_parser("rodar", help="executa o gateway")
    opcoes_canal(p)
    p.set_defaults(func=cmd_rodar)

    p = sub.add_parser("comando", help="mostra os comandos tsp (dry-run)")
    opcoes_canal(p)
    p.set_defaults(func=cmd_comando)

    p = sub.add_parser("init", help="gera configuração de exemplo")
    p.add_argument("arquivo", nargs="?", default="asi2srt.yaml")
    p.add_argument("--forcar", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("verificar", help="confere TSDuck, plugins e placas")
    p.add_argument("--tsp", default="tsp")
    p.set_defaults(func=cmd_verificar)

    p = sub.add_parser("instalar-tsduck", help="instala o TSDuck (root)")
    p.add_argument("--forcar", action="store_true")
    p.set_defaults(func=cmd_instalar_tsduck)

    p = sub.add_parser("instalar-servico", help="instala serviço systemd (root)")
    p.add_argument("-c", "--config", default="/etc/asi2srt/config.yaml")
    p.set_defaults(func=cmd_instalar_servico)

    p = sub.add_parser("teste", help="teste sem hardware (ffmpeg -> SRT local)")
    p.add_argument("--porta", type=int, default=9000)
    p.add_argument("--sem-player", action="store_true", help="não abrir o ffplay")
    p.add_argument("--tsp", default="tsp")
    p.set_defaults(func=cmd_teste)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    try:
        return args.func(args) or 0
    except ValueError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
