#!/usr/bin/env python3
"""
asi2srt - gateway DVB-ASI / UDP / SRT  ->  SRT / UDP, em um único programa.

Captura um transport stream (placa DVB-ASI DekTec ou driver Linux ASI/DVEO, UDP,
SRT ou arquivo) e o reenvia para uma ou mais saídas SRT e/ou UDP, usando o
TSDuck (tsp) como motor. Inclui painel web para gerenciar os canais.

Subcomandos:
  rodar             executa o gateway + painel web (http://IP:8088)
  init              gera um arquivo de configuração de exemplo
  comando           mostra os comandos tsp que seriam executados (dry-run)
  verificar         confere TSDuck, plugins e placas
  instalar-tsduck   baixa e instala o TSDuck (Debian/Ubuntu/Fedora/RHEL)
  instalar-servico  instala como serviço systemd (inicia no boot)
  teste             teste ponta a ponta sem hardware

Exemplos:
  asi2srt rodar                                   # abre o painel e gerencia por lá
  asi2srt rodar -e dektec:0:0 -s "srt://203.0.113.10:9000?latency=300"
  asi2srt rodar -e linuxasi:/dev/asirx0 -s "srt://:9000?mode=listener" -s udp://239.1.1.1:1234
  asi2srt rodar -e "srt://:9000?mode=listener" -s udp://239.10.0.1:5000?ttl=8
"""

import argparse
import base64
import collections
import copy
import hmac
import json
import logging
import os
import platform
import re
import secrets
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

__version__ = "2.0.0"

try:
    import yaml  # opcional: sem PyYAML a configuração é salva em JSON
except ImportError:
    yaml = None

log = logging.getLogger("asi2srt")

TIPOS_ENTRADA = ("dektec", "linuxasi", "udp", "srt", "arquivo")
TIPOS_SAIDA = ("srt", "udp")

# ======================================================================
# Configuração de exemplo (gerada por "asi2srt init")
# ======================================================================
CONFIG_EXEMPLO = """\
# ==========================================================
#  asi2srt - configuração
#  Entrada (ASI / UDP / SRT / arquivo) -> monitoração -> saídas SRT e/ou UDP
#  Tudo aqui também pode ser editado pelo painel web (http://IP:8088).
#  Atenção: salvar pelo painel reescreve este arquivo (comentários se perdem).
# ==========================================================

canais:
  - nome: "ASI-1"
    ativo: true

    entrada:
      # tipo: dektec | linuxasi | udp | srt | arquivo
      tipo: dektec
      dispositivo: 0          # dektec: índice da placa | linuxasi: /dev/asirx0
      canal: 0                # dektec: porta de entrada da placa
      timeout_ms: 5000        # dektec: sem sinal por este tempo => reinício
      # udp:     endereco: "239.1.1.1:1234"   interface_local: "192.168.10.5"
      # srt:     modo: listener|caller   endereco: "9000" ou "host:porta"
      #          latencia_ms: 200   senha: ""   stream_id: ""
      # arquivo: caminho: teste.ts   loop: true

    processamento:
      remover_nulos: true     # tira o stuffing (PID 0x1FFF) e economiza banda
      remover_pids: []        # PIDs a descartar, ex.: ["0x0012", "0x0014"]
      bitrate_min: 1000000    # alarme abaixo disto (bit/s). 0 = desligado
      bitrate_max: 0          # alarme acima disto (bit/s). 0 = desligado
      intervalo_relatorio_s: 5

    # Uma ou mais saídas. A primeira é a principal; as demais recebem cópia.
    saidas:
      - tipo: srt
        modo: caller            # caller (conecta no destino) | listener (aguarda conexão)
        endereco: "203.0.113.10:9000"
        latencia_ms: 200        # 3 a 4x o RTT do link
        senha: ""               # passphrase AES (10 a 79 caracteres). Vazio = sem criptografia
        tamanho_chave: 16       # 16, 24 ou 32
        stream_id: ""
        overhead_pct: 25        # banda extra para retransmissões
      # - tipo: udp
      #   endereco: "239.1.1.1:1234"   # unicast ou multicast
      #   ttl: 16
      #   interface_local: ""          # IP da placa de rede de saída

supervisor:
  reinicio_min_s: 2
  reinicio_max_s: 60
  tsp: tsp                    # caminho do executável do TSDuck
  status_http: "0.0.0.0:8088" # painel web. Vazio desativa
  usuario: admin              # login do painel
  senha_painel: ""            # vazio = gera uma senha na 1ª execução (aparece no log)
  log_arquivo: ""
"""


# ======================================================================
# Leitura / escrita / normalização da configuração
# ======================================================================
def carregar_config(caminho: str) -> dict:
    with open(caminho, encoding="utf-8") as f:
        texto = f.read()
    if caminho.lower().endswith(".json"):
        return json.loads(texto or "{}")
    if yaml is None:
        sys.exit("Para configuração YAML instale o PyYAML (pip install pyyaml) "
                 "ou use um arquivo .json com a mesma estrutura.")
    return yaml.safe_load(texto) or {}


def salvar_config(caminho: str, cfg: dict):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if caminho.lower().endswith(".json") or yaml is None:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        else:
            f.write("# asi2srt - configuração (gerenciada também pelo painel web)\n")
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, caminho)


def normalizar_canal(c: dict) -> dict:
    """Aceita o formato antigo (entrada aninhada, 'saida' única) e o novo (plano, 'saidas')."""
    c = copy.deepcopy(c)
    c.setdefault("id", secrets.token_hex(4))
    c.setdefault("nome", f"canal-{c['id']}")
    c.setdefault("ativo", True)
    ent = c.get("entrada") or {}
    tipo = ent.get("tipo", "dektec")
    if isinstance(ent.get(tipo), dict):  # formato antigo: entrada.dektec.{...}
        ent = {**ent[tipo], "tipo": tipo}
    else:
        ent = {k: v for k, v in ent.items() if not isinstance(v, dict)}
    c["entrada"] = ent
    if "saidas" not in c:
        s = c.pop("saida", None)
        c["saidas"] = [dict(s, tipo=s.get("tipo", "srt"))] if s else []
    c["saidas"] = [dict(s, tipo=s.get("tipo", "srt")) for s in c["saidas"]]
    c.setdefault("processamento", {"remover_nulos": True, "intervalo_relatorio_s": 5})
    return c


# ======================================================================
# Atalhos de linha de comando: --entrada / --saida
# ======================================================================
def _srt_de_url(u, padrao_modo):
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    modo = q.get("mode", "listener" if not u.hostname else padrao_modo)
    if modo == "caller":
        if not u.hostname or not u.port:
            raise SystemExit("SRT caller precisa de host e porta: srt://host:porta")
        endereco = f"{u.hostname}:{u.port}"
    else:
        endereco = (f"{u.hostname}:{u.port}" if u.hostname and u.hostname != "0.0.0.0"
                    else str(u.port))
    return {"tipo": "srt", "modo": modo, "endereco": endereco,
            "latencia_ms": int(q.get("latency", 200)),
            "senha": q.get("passphrase", ""),
            "tamanho_chave": int(q.get("pbkeylen", 16)),
            "stream_id": q.get("streamid", ""),
            "overhead_pct": int(q.get("oheadbw", 25))}


def _udp_de_url(u):
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    host = u.hostname or ""
    return {"tipo": "udp", "endereco": f"{host}:{u.port}" if host else str(u.port),
            "ttl": int(q.get("ttl", 16)), "interface_local": q.get("local", "")}


def entrada_de_texto(txt: str) -> dict:
    """dektec:0:0 | linuxasi:/dev/asirx0 | udp:IP:PORTA[@IP] | udp://IP:PORTA?local=IP
       | srt://:9000?mode=listener | srt://host:porta | arquivo:x.ts"""
    if txt.startswith("srt://"):
        s = _srt_de_url(urlparse(txt), "caller")
        s.pop("overhead_pct", None)
        return s
    if txt.startswith("udp://"):
        u = _udp_de_url(urlparse(txt))
        return {"tipo": "udp", "endereco": u["endereco"], "interface_local": u["interface_local"]}
    tipo, _, resto = txt.partition(":")
    tipo = tipo.lower()
    if tipo == "dektec":
        p = (resto or "0:0").split(":")
        return {"tipo": "dektec", "dispositivo": int(p[0] or 0),
                "canal": int(p[1]) if len(p) > 1 else 0, "timeout_ms": 5000}
    if tipo == "linuxasi":
        return {"tipo": "linuxasi", "dispositivo": resto or "/dev/asirx0"}
    if tipo == "udp":
        end, _, local = resto.partition("@")
        return {"tipo": "udp", "endereco": end, "interface_local": local}
    if tipo in ("arquivo", "file"):
        return {"tipo": "arquivo", "caminho": resto, "loop": True}
    raise SystemExit(f"entrada inválida: {txt!r}")


def saida_de_url(url: str) -> dict:
    """srt://host:porta?mode=..&latency=..&passphrase=..&streamid=..  |  udp://IP:PORTA?ttl=..&local=IP"""
    u = urlparse(url)
    if u.scheme == "srt":
        return _srt_de_url(u, "caller")
    if u.scheme == "udp":
        return _udp_de_url(u)
    raise SystemExit(f"saída inválida: {url!r} (use srt://... ou udp://...)")


# ======================================================================
# Montagem do comando tsp
# ======================================================================
def _pid(valor):
    return valor if isinstance(valor, int) else int(str(valor).strip(), 0)


def _args_srt(s: dict, entrada: bool) -> list:
    modo = s.get("modo", "listener" if entrada else "caller")
    if modo not in ("caller", "listener"):
        raise ValueError(f"modo SRT inválido: {modo}")
    if not str(s.get("endereco", "")).strip():
        raise ValueError("SRT sem endereço")
    a = ["--" + modo, str(s["endereco"]).strip(), "--transtype", "live",
         "--latency", str(int(s.get("latencia_ms") or 200))]
    if not entrada:
        a += ["--ohead-bandwidth", str(int(s.get("overhead_pct") or 25))]
    if s.get("senha"):
        if not 10 <= len(s["senha"]) <= 79:
            raise ValueError("a senha SRT deve ter entre 10 e 79 caracteres")
        a += ["--passphrase", s["senha"], "--pbkeylen", str(int(s.get("tamanho_chave") or 16))]
    if s.get("stream_id"):
        a += ["--streamid", s["stream_id"]]
    return a


def _args_saida(s: dict) -> list:
    tipo = s.get("tipo", "srt")
    if tipo == "srt":
        return ["srt"] + _args_srt(s, entrada=False)
    if tipo == "udp":
        if not re.match(r"^[\w.\-]*:\d+$|^\d+$", str(s.get("endereco", "")).strip()):
            raise ValueError("saída UDP precisa de ip:porta")
        a = ["ip", str(s["endereco"]).strip(), "--packet-burst", "7"]
        if s.get("ttl"):
            a += ["--ttl", str(int(s["ttl"]))]
        if s.get("interface_local"):
            a += ["--local-address", s["interface_local"]]
        return a
    raise ValueError(f"tipo de saída desconhecido: {tipo}")


def montar_comando(canal: dict, tsp_bin: str = "tsp") -> list:
    canal = normalizar_canal(canal)
    ent = canal["entrada"]
    proc = canal.get("processamento") or {}
    saidas = canal.get("saidas") or []
    if not saidas:
        raise ValueError("o canal precisa de pelo menos uma saída")

    cmd = [tsp_bin, "--realtime", "--buffer-size-mb", "8", "--max-flushed-packets", "7"]

    tipo = ent.get("tipo")
    if tipo == "dektec":
        cmd += ["-I", "dektec", "--device", str(int(ent.get("dispositivo") or 0)),
                "--channel", str(int(ent.get("canal") or 0)),
                "--receive-timeout", str(int(ent.get("timeout_ms") or 5000))]
    elif tipo == "linuxasi":
        # driver Linux ASI: dispositivo de caracteres em modo 188 bytes
        cmd += ["-I", "file", str(ent.get("dispositivo") or "/dev/asirx0")]
    elif tipo == "udp":
        if not ent.get("endereco"):
            raise ValueError("entrada UDP precisa de ip:porta (ou só porta)")
        cmd += ["-I", "ip", str(ent["endereco"])]
        if ent.get("interface_local"):
            cmd += ["--local-address", ent["interface_local"]]
        cmd += ["--receive-timeout", "5000"]
    elif tipo == "srt":
        cmd += ["-I", "srt"] + _args_srt(ent, entrada=True)
    elif tipo == "arquivo":
        if not ent.get("caminho"):
            raise ValueError("entrada de arquivo precisa do caminho")
        cmd += ["-I", "file", str(ent["caminho"])]
        if ent.get("loop", True):
            cmd += ["--infinite"]
    else:
        raise ValueError(f"tipo de entrada desconhecido: {tipo}")

    cmd += ["-P", "continuity"]

    pids = [_pid(p) for p in (proc.get("remover_pids") or []) if str(p).strip()]
    if proc.get("remover_nulos", True):
        pids.append(0x1FFF)
    if pids:
        cmd += ["-P", "filter", "--negate"]
        for p in sorted(set(pids)):
            cmd += ["--pid", f"0x{p:04X}"]

    intervalo = str(int(proc.get("intervalo_relatorio_s") or 5))
    cmd += ["-P", "bitrate_monitor", "--periodic-bitrate", intervalo, "--time-interval", intervalo]
    if proc.get("bitrate_min"):
        cmd += ["--min", str(int(proc["bitrate_min"]))]
    if proc.get("bitrate_max"):
        cmd += ["--max", str(int(proc["bitrate_max"]))]

    # saídas extras: cada uma recebe uma cópia do TS por um tsp filho (plugin fork)
    for s in saidas[1:]:
        filho = [tsp_bin, "--realtime", "-O"] + _args_saida(s)
        cmd += ["-P", "fork", "--nowait", " ".join(shlex.quote(a) for a in filho)]

    cmd += ["-O"] + _args_saida(saidas[0])
    return cmd


def comando_para_log(cmd: list) -> str:
    txt = " ".join(shlex.quote(a) for a in cmd)
    return re.sub(r"(--passphrase\s+)('[^']*'|[^\s']+)", r"\1********", txt)


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
        self.log = collections.deque(maxlen=300)
        self.hist = collections.deque(maxlen=60)
        self.st = {"estado": "iniciando", "bitrate_bps": 0, "erros_cc": 0,
                   "alarmes_bitrate": 0, "reinicios": 0, "inicio_execucao": None,
                   "ultimo_bitrate_em": None, "ultima_msg": ""}

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def _registrar(self, nivel, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        log.log(nivel, "[%s] %s", self.name, msg)

    def status(self):
        with self.lock:
            s = dict(self.st)
            s["historico"] = list(self.hist)
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
                with self.lock:
                    self.hist.append(bps)
                self._set(bitrate_bps=bps, ultimo_bitrate_em=time.time(), estado="rodando")
                log.debug("[%s] %s", self.name, linha)
                continue
            if RE_CC.search(linha):
                with self.lock:
                    self.st["erros_cc"] += 1
                self._registrar(logging.WARNING, linha)
            elif RE_ALARME.search(linha):
                with self.lock:
                    self.st["alarmes_bitrate"] += 1
                self._registrar(logging.WARNING, linha)
            else:
                self._registrar(logging.INFO, linha)
            self._set(ultima_msg=linha[-300:])

    def run(self):
        espera_min = self.sup.get("reinicio_min_s", 2)
        espera_max = self.sup.get("reinicio_max_s", 60)
        espera = espera_min
        while not self.parar.is_set():
            self._registrar(logging.INFO, "iniciando: " + comando_para_log(self.cmd))
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, bufsize=1)
            except FileNotFoundError:
                self._registrar(logging.ERROR, f"'{self.cmd[0]}' não encontrado. "
                                "Rode: sudo asi2srt instalar-tsduck")
                self._set(estado="erro", ultima_msg="tsp não encontrado")
                self.parar.wait(espera_max)
                continue

            self._set(estado="conectando", inicio_execucao=t0)
            self._ler_saida(self.proc.stdout)
            rc = self.proc.wait()
            if self.parar.is_set():
                break
            if time.time() - t0 > 60:
                espera = espera_min
            with self.lock:
                self.st["reinicios"] += 1
            self._set(estado="reiniciando", bitrate_bps=0, inicio_execucao=None)
            self._registrar(logging.ERROR, f"tsp saiu (código {rc}). Reiniciando em {espera}s.")
            self.parar.wait(espera)
            espera = min(espera * 2, espera_max)
        self._set(estado="parado", bitrate_bps=0, inicio_execucao=None)

    def encerrar(self):
        self.parar.set()
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


# ======================================================================
# Gerenciador: mantém as definições, os processos e a persistência
# ======================================================================
class Gerenciador:
    def __init__(self, cfg: dict, caminho=None):
        self.caminho = caminho
        self.sup = cfg.setdefault("supervisor", {}) or {}
        self.lock = threading.RLock()
        self.defs = collections.OrderedDict()
        self.exec = {}
        for c in cfg.get("canais") or []:
            c = normalizar_canal(c)
            self.defs[c["id"]] = c

    # --- persistência ------------------------------------------------
    def persistir(self):
        if not self.caminho:
            return
        with self.lock:
            cfg = {"canais": list(self.defs.values()), "supervisor": self.sup}
            salvar_config(self.caminho, cfg)

    def garantir_senha(self):
        if not self.sup.get("senha_painel"):
            self.sup["senha_painel"] = secrets.token_urlsafe(9)
            self.persistir()
            aviso = (f"senha do painel gerada -> usuário: {self.sup.get('usuario', 'admin')}  "
                     f"senha: {self.sup['senha_painel']}"
                     + (f"  (salva em {self.caminho})" if self.caminho else ""))
            log.warning(aviso)

    # --- controle ----------------------------------------------------
    def iniciar_ativos(self):
        for cid, c in list(self.defs.items()):
            if c.get("ativo", True):
                try:
                    self.iniciar(cid, marcar=False)
                except ValueError as e:
                    log.error("[%s] configuração inválida: %s", c["nome"], e)

    def iniciar(self, cid, marcar=True):
        with self.lock:
            c = self.defs[cid]
            atual = self.exec.get(cid)
            if atual and atual.is_alive():
                return
            t = Canal(c, self.sup)
            self.exec[cid] = t
            t.start()
            if marcar and not c.get("ativo"):
                c["ativo"] = True
                self.persistir()

    def parar(self, cid, marcar=True):
        with self.lock:
            t = self.exec.pop(cid, None)
            if marcar and cid in self.defs and self.defs[cid].get("ativo"):
                self.defs[cid]["ativo"] = False
                self.persistir()
        if t:
            t.encerrar()

    def reiniciar(self, cid):
        self.parar(cid, marcar=False)
        self.iniciar(cid)

    def salvar_canal(self, dados: dict, cid=None) -> dict:
        dados = dict(dados)
        dados.pop("status", None)
        if cid:
            dados["id"] = cid
        c = normalizar_canal(dados)
        if c["entrada"].get("tipo") not in TIPOS_ENTRADA:
            raise ValueError("tipo de entrada inválido")
        montar_comando(c, self.sup.get("tsp", "tsp"))  # valida (levanta ValueError)
        rodando = cid in self.exec
        if rodando:
            self.parar(cid, marcar=False)
        with self.lock:
            self.defs[c["id"]] = c
            self.persistir()
        if c.get("ativo", True):
            self.iniciar(c["id"], marcar=False)
        return c

    def remover(self, cid):
        self.parar(cid, marcar=False)
        with self.lock:
            self.defs.pop(cid, None)
            self.persistir()

    def encerrar_tudo(self):
        for cid in list(self.exec):
            self.parar(cid, marcar=False)

    # --- consulta ----------------------------------------------------
    def listar(self):
        out = []
        with self.lock:
            itens = list(self.defs.items())
        for cid, c in itens:
            t = self.exec.get(cid)
            st = t.status() if t else {"estado": "parado", "bitrate_bps": 0, "erros_cc": 0,
                                       "alarmes_bitrate": 0, "reinicios": 0, "historico": []}
            out.append({**c, "status": st})
        return out

    def log_canal(self, cid):
        t = self.exec.get(cid)
        return list(t.log) if t else []


# ======================================================================
# Servidor web: painel + API
# ======================================================================
def servidor_web(ger: Gerenciador, endereco: str):
    host, _, porta = endereco.rpartition(":")
    usuario = ger.sup.get("usuario", "admin")

    class H(BaseHTTPRequestHandler):
        server_version = "asi2srt/" + __version__

        def log_message(self, *a):
            pass

        def _autorizado(self):
            cab = self.headers.get("Authorization", "")
            if cab.startswith("Basic "):
                try:
                    u, _, s = base64.b64decode(cab[6:]).decode().partition(":")
                except Exception:
                    return False
                return hmac.compare_digest(u, usuario) and \
                    hmac.compare_digest(s, str(ger.sup.get("senha_painel", "")))
            return False

        def _resp(self, codigo, corpo, tipo="application/json"):
            if not isinstance(corpo, bytes):
                corpo = json.dumps(corpo, ensure_ascii=False).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _negar(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="asi2srt"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _corpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _rota(self, metodo):
            if not self._autorizado():
                return self._negar()
            # proteção contra CSRF: ações exigem cabeçalho próprio
            if metodo != "GET" and self.headers.get("X-Asi2srt") != "1":
                return self._resp(403, {"erro": "cabeçalho X-Asi2srt ausente"})
            caminho = urlparse(self.path).path.rstrip("/") or "/"
            partes = caminho.strip("/").split("/")
            try:
                if metodo == "GET" and caminho == "/":
                    return self._resp(200, PAINEL_HTML.replace("__VERSAO__", __version__).encode(),
                                      "text/html; charset=utf-8")
                if metodo == "GET" and caminho == "/status.json":
                    return self._resp(200, [{"nome": c["nome"], "id": c["id"], **c["status"]}
                                            for c in ger.listar()])
                if metodo == "GET" and caminho == "/api/info":
                    return self._resp(200, {"versao": __version__, "config": ger.caminho,
                                            "persistente": bool(ger.caminho)})
                if partes[:2] == ["api", "canais"]:
                    if len(partes) == 2:
                        if metodo == "GET":
                            return self._resp(200, ger.listar())
                        if metodo == "POST":
                            dados = self._corpo()
                            dados.pop("id", None)
                            return self._resp(201, ger.salvar_canal(dados))
                    cid = partes[2] if len(partes) > 2 else None
                    if cid not in ger.defs:
                        return self._resp(404, {"erro": "canal não encontrado"})
                    if len(partes) == 3:
                        if metodo == "PUT":
                            return self._resp(200, ger.salvar_canal(self._corpo(), cid))
                        if metodo == "DELETE":
                            ger.remover(cid)
                            return self._resp(200, {"ok": True})
                    if len(partes) == 4:
                        acao = partes[3]
                        if metodo == "GET" and acao == "log":
                            return self._resp(200, ger.log_canal(cid))
                        if metodo == "GET" and acao == "comando":
                            return self._resp(200, {"comando": comando_para_log(
                                montar_comando(ger.defs[cid], ger.sup.get("tsp", "tsp")))})
                        if metodo == "POST" and acao in ("iniciar", "parar", "reiniciar"):
                            getattr(ger, acao)(cid)
                            return self._resp(200, {"ok": True})
                return self._resp(404, {"erro": "rota não encontrada"})
            except ValueError as e:
                return self._resp(400, {"erro": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("erro na API")
                return self._resp(500, {"erro": str(e)})

        def do_GET(self):
            self._rota("GET")

        def do_POST(self):
            self._rota("POST")

        def do_PUT(self):
            self._rota("PUT")

        def do_DELETE(self):
            self._rota("DELETE")

    srv = ThreadingHTTPServer((host or "0.0.0.0", int(porta)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("painel: http://%s:%s/  (usuário: %s)", host or "0.0.0.0", porta, usuario)
    return srv


# ======================================================================
# Subcomandos
# ======================================================================
def _achar_config():
    for c in ("asi2srt.yaml", "asi2srt.yml", "asi2srt.json", "config.yaml",
              "/etc/asi2srt/config.yaml", "/etc/asi2srt/config.json"):
        if os.path.exists(c):
            return c
    return None


def _carregar(args):
    """Devolve (cfg, caminho). caminho None = modo linha de comando (não persiste)."""
    if args.entrada or args.saida:
        if not (args.entrada and args.saida):
            raise SystemExit("use --entrada e --saida juntos (ou -c arquivo)")
        canal = {"nome": args.nome, "entrada": entrada_de_texto(args.entrada),
                 "saidas": [saida_de_url(s) for s in args.saida],
                 "processamento": {"remover_nulos": not args.manter_nulos,
                                   "bitrate_min": args.bitrate_min,
                                   "intervalo_relatorio_s": 5}}
        sup = {"status_http": args.status, "tsp": args.tsp, "usuario": args.usuario,
               "senha_painel": args.senha_painel}
        return {"canais": [canal], "supervisor": sup}, None
    caminho = args.config or _achar_config()
    if not caminho:
        caminho = "asi2srt.yaml" if yaml else "asi2srt.json"
        salvar_config(caminho, {"canais": [], "supervisor": {
            "reinicio_min_s": 2, "reinicio_max_s": 60, "tsp": "tsp",
            "status_http": "0.0.0.0:8088", "usuario": "admin", "senha_painel": ""}})
        log.info("nenhuma configuração encontrada; criada %s (adicione canais pelo painel)", caminho)
    cfg = carregar_config(caminho)
    sup = cfg.setdefault("supervisor", {}) or {}
    if args.tsp != "tsp":
        sup["tsp"] = args.tsp
    if args.senha_painel:
        sup["senha_painel"] = args.senha_painel
    return cfg, caminho


def executar_gateway(cfg: dict, caminho=None):
    ger = Gerenciador(cfg, caminho)
    sup = ger.sup
    if sup.get("log_arquivo"):
        logging.getLogger().addHandler(logging.FileHandler(sup["log_arquivo"]))
    log.info("asi2srt %s - %d canal(is) configurado(s)", __version__, len(ger.defs))
    if sup.get("status_http", "0.0.0.0:8088"):
        ger.garantir_senha()
        try:
            servidor_web(ger, sup.get("status_http", "0.0.0.0:8088"))
        except OSError as e:
            log.error("não foi possível abrir o painel em %s: %s", sup["status_http"], e)
    elif not ger.defs:
        raise SystemExit("nenhum canal e painel desativado: nada a fazer")
    ger.iniciar_ativos()

    fim = threading.Event()

    def sair(*_):
        log.info("encerrando...")
        ger.encerrar_tudo()
        fim.set()

    signal.signal(signal.SIGINT, sair)
    signal.signal(signal.SIGTERM, sair)
    fim.wait()


def cmd_rodar(args):
    cfg, caminho = _carregar(args)
    executar_gateway(cfg, caminho)


def cmd_comando(args):
    cfg, _ = _carregar(args)
    tsp = (cfg.get("supervisor") or {}).get("tsp", "tsp")
    for c in cfg.get("canais") or []:
        c = normalizar_canal(c)
        print(f"# {c['nome']}{'' if c.get('ativo', True) else ' (inativo)'}")
        print(comando_para_log(montar_comando(c, tsp)) + "\n")


def cmd_init(args):
    if os.path.exists(args.arquivo) and not args.forcar:
        raise SystemExit(f"{args.arquivo} já existe (use --forcar para sobrescrever)")
    with open(args.arquivo, "w", encoding="utf-8") as f:
        f.write(CONFIG_EXEMPLO)
    print(f"configuração criada: {args.arquivo}\n"
          f"edite e rode: asi2srt rodar -c {args.arquivo}   (ou edite pelo painel web)")


def cmd_verificar(args):
    ok = True
    tsp = shutil.which(args.tsp) or (args.tsp if os.path.exists(args.tsp) else None)
    if not tsp:
        print("✗ TSDuck (tsp) não encontrado  ->  sudo asi2srt instalar-tsduck")
        return 1
    ver = subprocess.run([tsp, "--version"], capture_output=True, text=True)
    print(f"✓ TSDuck: {(ver.stdout or ver.stderr).strip().splitlines()[0]}")
    plugins = subprocess.run([tsp, "--list-plugins"], capture_output=True, text=True)
    texto = plugins.stdout + plugins.stderr
    for nome, obrig in (("srt", True), ("ip", True), ("fork", True), ("continuity", True),
                        ("bitrate_monitor", True), ("filter", True), ("dektec", False)):
        tem = re.search(rf"\b{nome}\b", texto) is not None
        print(f"{'✓' if tem else ('✗' if obrig else '·')} plugin {nome}"
              f"{'' if tem or obrig else ' (só necessário para placas DekTec)'}")
        ok &= tem or not obrig
    if shutil.which("tsdektec"):
        r = subprocess.run(["tsdektec", "-a"], capture_output=True, text=True)
        print("\nPlacas DekTec:\n" + ((r.stdout or r.stderr).strip() or "  nenhuma encontrada"))
    asi = sorted(f for f in os.listdir("/dev") if f.startswith("asirx")) if os.path.isdir("/dev") else []
    print("\nDispositivos Linux ASI: " + (", ".join("/dev/" + a for a in asi) or "nenhum"))
    print("PyYAML: " + ("instalado" if yaml else "ausente (configuração será salva em JSON)"))
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
        subprocess.run(["dnf" if shutil.which("dnf") else "yum", "install", "-y", destino], check=True)
    print("TSDuck instalado. Confira com: asi2srt verificar")


UNIDADE_SYSTEMD = """\
[Unit]
Description=Gateway ASI/UDP/SRT -> SRT/UDP (asi2srt)
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
        os.chmod(args.config, 0o600)
        print(f"configuração de exemplo criada em {args.config}")
    with open("/etc/systemd/system/asi2srt.service", "w") as f:
        f.write(UNIDADE_SYSTEMD.format(exe=exe, config=args.config))
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print("serviço instalado.\n"
          "  1. sudo systemctl enable --now asi2srt\n"
          "  2. veja a senha do painel:  journalctl -u asi2srt | grep 'senha do painel'\n"
          "  3. abra http://<ip-do-servidor>:8088/ e configure os canais")


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
                       "entrada": {"tipo": "arquivo", "caminho": ts, "loop": True},
                       "processamento": {"bitrate_min": 0, "intervalo_relatorio_s": 2},
                       "saidas": [{"tipo": "srt", "modo": "listener", "endereco": str(args.porta),
                                   "latencia_ms": 120, "senha": senha},
                                  {"tipo": "udp", "endereco": "127.0.0.1:5000"}]}],
           "supervisor": {"status_http": "127.0.0.1:8088", "tsp": args.tsp,
                          "usuario": "admin", "senha_painel": "teste"}}
    url = f"srt://127.0.0.1:{args.porta}?mode=caller&passphrase={senha}&latency=120000"
    print(f"\nreceptor SRT:  ffplay \"{url}\"\nreceptor UDP:  ffplay udp://127.0.0.1:5000"
          f"\npainel:        http://127.0.0.1:8088/  (admin / teste)\nCtrl+C para sair\n")
    if shutil.which("ffplay") and not args.sem_player:
        threading.Timer(2, lambda: subprocess.Popen(
            ["ffplay", "-loglevel", "warning", url])).start()
    executar_gateway(cfg)


# ======================================================================
# Painel web (HTML + JS embutidos)
# ======================================================================
PAINEL_HTML = r"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>asi2srt</title>
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#2a313c;--txt:#e6edf3;--mut:#8b949e;--acc:#2f81f7;
--ok:#3fb950;--warn:#d29922;--err:#f85149;--in:#0d1117}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:5}
header h1{font-size:17px;margin:0;font-weight:650}header .v{color:var(--mut);font-size:12px}
header .sp{flex:1}
.resumo{display:flex;gap:18px;color:var(--mut);font-size:13px}.resumo b{color:var(--txt)}
main{padding:20px;max-width:1400px;margin:auto}
.aviso{background:#3b2e00;border:1px solid #6b5300;padding:8px 12px;border-radius:8px;margin-bottom:14px;color:#f0d67a}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:10px}
.top{display:flex;align-items:center;gap:8px}.top h2{font-size:15px;margin:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.rodando{color:var(--ok)}.conectando,.reiniciando,.iniciando{color:var(--warn)}.erro,.sem_dados{color:var(--err)}.parado{color:var(--mut)}
.fluxo{display:flex;flex-direction:column;gap:3px;font-size:12.5px}
.fluxo .l{color:var(--mut);display:inline-block;width:52px}
.tag{display:inline-block;background:var(--in);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin:1px 4px 1px 0;font-family:ui-monospace,monospace;font-size:12px}
.met{display:grid;grid-template-columns:1.3fr 1fr 1fr 1fr;gap:6px;align-items:end}
.met div{display:flex;flex-direction:column}.met small{color:var(--mut);font-size:11px}
.met b{font-size:15px;font-variant-numeric:tabular-nums}.met .big{font-size:22px}
.spark{width:100%;height:38px;display:block}.spark polyline{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}
.spark .area{fill:var(--acc);opacity:.12;stroke:none}
.msg{font-size:11.5px;color:var(--mut);font-family:ui-monospace,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:16px}
.acoes{display:flex;gap:6px;flex-wrap:wrap}
button{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 11px;font:inherit;font-size:13px;cursor:pointer}
button:hover{border-color:#8b949e}button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.ok{color:var(--ok)}button.perigo{color:var(--err)}button.mini{padding:3px 8px;font-size:12px}
.vazio{text-align:center;color:var(--mut);padding:60px 20px;border:1px dashed var(--line);border-radius:12px}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:14px;padding:0;width:min(720px,96vw);max-height:92vh}
dialog::backdrop{background:#0009}
.dh{display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}.dh h3{margin:0;flex:1;font-size:16px}
.db{padding:16px 18px;overflow:auto;max-height:calc(92vh - 130px)}
.df{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid var(--line)}
fieldset{border:1px solid var(--line);border-radius:10px;margin:0 0 14px;padding:10px 12px 12px}
legend{color:var(--mut);font-size:12px;padding:0 6px;text-transform:uppercase;letter-spacing:.05em}
.campos{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}
label.chk{flex-direction:row;align-items:center;gap:6px;color:var(--txt);font-size:13px}
input,select{background:var(--in);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:7px 8px;font:inherit;font-size:13px;width:100%}
input[type=checkbox]{width:auto}input:focus,select:focus{outline:2px solid var(--acc);outline-offset:-1px}
.saida{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;background:#11161d}
.saida .sh{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-weight:600;font-size:13px}.saida .sh span{flex:1}
pre.log{background:var(--in);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;
max-height:60vh;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:0}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#3d1214;border:1px solid var(--err);
color:#ffd8d6;padding:10px 16px;border-radius:9px;z-index:20;max-width:90vw}
.toast.bom{background:#0f2e17;border-color:var(--ok);color:#c9f5d1}
@media(max-width:600px){header{flex-wrap:wrap}.resumo{width:100%;order:3}main{padding:12px}.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>asi2srt</h1><span class="v">v__VERSAO__</span><span class="sp"></span>
<div class="resumo" id="resumo"></div><button class="pri" id="novo">+ Novo canal</button></header>
<main><div id="avisos"></div><div class="grid" id="grid"></div></main>

<dialog id="dlg"><form method="dialog" id="frm">
<div class="dh"><h3 id="dtit">Canal</h3><button type="button" class="mini" data-fechar>✕</button></div>
<div class="db" id="dcorpo"></div>
<div class="df"><button type="button" data-fechar>Cancelar</button><button class="pri" type="submit">Salvar e aplicar</button></div>
</form></dialog>

<dialog id="dlglog"><div class="dh"><h3 id="ltit">Log</h3><button class="mini" data-fechar>✕</button></div>
<div class="db"><pre class="log" id="logtxt"></pre></div></dialog>

<script>
const H={'Content-Type':'application/json','X-Asi2srt':'1'};
const $=s=>document.querySelector(s);
async function api(m,u,b){const r=await fetch(u,{method:m,headers:H,body:b?JSON.stringify(b):undefined});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.erro||r.statusText);return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(t,bom){const d=document.createElement('div');d.className='toast'+(bom?' bom':'');d.textContent=t;
 document.body.appendChild(d);setTimeout(()=>d.remove(),bom?2500:6000)}
const mb=b=>(b/1e6).toFixed(2);
function dur(s){if(!s)return'—';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h${String(m).padStart(2,'0')}`:m?`${m}m${s%60}s`:`${s}s`}

const CE={
 dektec:[['dispositivo','Placa','number',0],['canal','Porta','number',0],['timeout_ms','Timeout sem sinal (ms)','number',5000]],
 linuxasi:[['dispositivo','Dispositivo','text','/dev/asirx0']],
 udp:[['endereco','Endereço (ip:porta ou porta)','text','239.1.1.1:1234'],['interface_local','IP da interface (multicast)','text','']],
 srt:[['modo','Modo','sel:listener,caller','listener'],['endereco','Endereço (porta ou host:porta)','text','9000'],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],['stream_id','Stream ID','text','']],
 arquivo:[['caminho','Arquivo .ts','text',''],['loop','Repetir em loop','chk',true]]};
const CS={
 srt:[['modo','Modo','sel:caller,listener','caller'],['endereco','Destino (host:porta) / porta','text',''],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],
      ['stream_id','Stream ID','text',''],['overhead_pct','Overhead (%)','number',25]],
 udp:[['endereco','Destino (ip:porta)','text','239.1.1.1:1234'],['ttl','TTL','number',16],['interface_local','IP da interface de saída','text','']]};
const NOMES_E={dektec:'DekTec ASI',linuxasi:'Linux ASI (DVEO)',udp:'UDP / Multicast',srt:'SRT',arquivo:'Arquivo .ts'};

function resE(e){switch(e.tipo){case'dektec':return`DekTec ${e.dispositivo??0}:${e.canal??0}`;case'linuxasi':return`ASI ${e.dispositivo}`;
 case'udp':return`UDP ${e.endereco}`;case'srt':return`SRT ${e.modo} ${e.endereco}${e.senha?' 🔒':''}`;case'arquivo':return`Arquivo ${(e.caminho||'').split('/').pop()}`}return e.tipo}
const resS=s=>s.tipo==='udp'?`UDP ${s.endereco}`:`SRT ${s.modo} ${s.endereco}${s.senha?' 🔒':''}`;

function spark(h){if(!h||h.length<2)return'<svg class="spark"></svg>';const mx=Math.max(...h)*1.15||1,w=100,hh=38;
 const p=h.map((v,i)=>`${(i/(h.length-1)*w).toFixed(2)},${(hh-v/mx*hh).toFixed(2)}`).join(' ');
 return`<svg class="spark" viewBox="0 0 ${w} ${hh}" preserveAspectRatio="none"><polygon class="area" points="0,${hh} ${p} ${w},${hh}"/><polyline points="${p}"/></svg>`}

let dados=[],editando=null,logId=null;
function card(c){const s=c.status||{},ativo=s.estado!=='parado';
 return`<div class="card"><div class="top"><h2 title="${esc(c.nome)}">${esc(c.nome)}</h2><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span></div>
 <div class="fluxo"><div><span class="l">Entrada</span><span class="tag">${esc(resE(c.entrada))}</span></div>
 <div><span class="l">Saídas</span>${c.saidas.map(x=>`<span class="tag">${esc(resS(x))}</span>`).join('')}</div></div>
 ${spark(s.historico)}
 <div class="met"><div><small>Bitrate</small><b class="big">${mb(s.bitrate_bps||0)}<small> Mb/s</small></b></div>
 <div><small>Erros CC</small><b style="color:${s.erros_cc?'var(--err)':''}">${s.erros_cc||0}</b></div>
 <div><small>Reinícios</small><b>${s.reinicios||0}</b></div><div><small>Uptime</small><b>${dur(s.uptime_s)}</b></div></div>
 <div class="msg" title="${esc(s.ultima_msg)}">${esc(s.ultima_msg||'')}</div>
 <div class="acoes">${ativo?`<button data-a="parar" data-id="${c.id}">■ Parar</button><button data-a="reiniciar" data-id="${c.id}">↻ Reiniciar</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}">▶ Iniciar</button>`}
 <button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button>
 <button class="perigo" data-a="excluir" data-id="${c.id}">Excluir</button></div></div>`}

function render(){$('#grid').innerHTML=dados.length?dados.map(card).join(''):
 '<div class="vazio" style="grid-column:1/-1">Nenhum canal configurado.<br><br><button class="pri" onclick="abrir()">+ Criar o primeiro canal</button></div>';
 const rod=dados.filter(c=>c.status.estado==='rodando').length,tot=dados.reduce((a,c)=>a+(c.status.bitrate_bps||0),0);
 $('#resumo').innerHTML=`<span><b>${rod}</b>/${dados.length} no ar</span><span><b>${mb(tot)}</b> Mb/s total</span>`}

async function atualizar(){try{dados=await api('GET','/api/canais');render()}catch(e){}}

// ---------- formulário ----------
function campo(pref,[k,rot,t,pad],obj){const v=obj[k]??pad,n=`${pref}${k}`;
 if(t==='chk')return`<label class="chk"><input type="checkbox" name="${n}" ${v?'checked':''}> ${rot}</label>`;
 if(t.startsWith('sel:'))return`<label>${rot}<select name="${n}">${t.slice(4).split(',').map(o=>`<option ${String(v)===o?'selected':''}>${o}</option>`).join('')}</select></label>`;
 return`<label>${rot}<input type="${t}" name="${n}" value="${esc(v)}" ${t==='password'?'autocomplete="new-password"':''}></label>`}

function desenhar(){const c=editando,e=c.entrada,p=c.processamento||{};
 $('#dcorpo').innerHTML=`
 <fieldset><legend>Canal</legend><div class="campos"><label style="grid-column:span 2">Nome<input name="nome" value="${esc(c.nome)}" required></label>
 <label class="chk"><input type="checkbox" name="ativo" ${c.ativo!==false?'checked':''}> Ativo (inicia automaticamente)</label></div></fieldset>
 <fieldset><legend>Entrada</legend><div class="campos"><label>Tipo<select name="e_tipo" id="etipo">
 ${Object.entries(NOMES_E).map(([k,n])=>`<option value="${k}" ${e.tipo===k?'selected':''}>${n}</option>`).join('')}</select></label>
 ${CE[e.tipo].map(f=>campo('e.',f,e)).join('')}</div></fieldset>
 <fieldset><legend>Processamento</legend><div class="campos">
 <label class="chk"><input type="checkbox" name="p.remover_nulos" ${p.remover_nulos!==false?'checked':''}> Remover pacotes nulos</label>
 <label>PIDs a remover (ex.: 0x12, 0x14)<input name="p.remover_pids" value="${esc((p.remover_pids||[]).join(', '))}"></label>
 <label>Alarme bitrate mín. (Mb/s)<input type="number" step="0.1" name="p.bitrate_min" value="${(p.bitrate_min||0)/1e6}"></label>
 <label>Alarme bitrate máx. (Mb/s)<input type="number" step="0.1" name="p.bitrate_max" value="${(p.bitrate_max||0)/1e6}"></label></div></fieldset>
 <fieldset><legend>Saídas</legend>
 ${c.saidas.map((s,i)=>`<div class="saida"><div class="sh"><span>${i===0?'Principal':'Cópia '+i} — ${s.tipo.toUpperCase()}</span>
  <button type="button" class="mini perigo" data-rm="${i}">Remover</button></div>
  <input type="hidden" name="s.${i}.tipo" value="${s.tipo}"><div class="campos">${CS[s.tipo].map(f=>campo(`s.${i}.`,f,s)).join('')}</div></div>`).join('')}
 <button type="button" data-add="srt">+ Saída SRT</button> <button type="button" data-add="udp">+ Saída UDP</button></fieldset>`;
 $('#etipo').onchange=()=>{coletar();editando.entrada.tipo=$('#etipo').value;desenhar()};
 $('#dcorpo').querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{coletar();editando.saidas.splice(+b.dataset.rm,1);desenhar()});
 $('#dcorpo').querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{coletar();editando.saidas.push({tipo:b.dataset.add});desenhar()})}

function val(el,t){if(!el)return undefined;if(el.type==='checkbox')return el.checked;if(el.type==='number'||t==='number')return el.value===''?0:Number(el.value);return el.value.trim()}
function coletar(){const f=$('#frm'),c=editando,g=n=>f.elements[n];
 c.nome=g('nome').value.trim();c.ativo=g('ativo').checked;
 const tipo=g('e_tipo').value,ent={...c.entrada,tipo};
 CE[tipo].forEach(([k,,t])=>{const v=val(g('e.'+k),t);if(v!==undefined)ent[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});c.entrada=ent;
 c.processamento={...(c.processamento||{}),remover_nulos:g('p.remover_nulos').checked,
  remover_pids:g('p.remover_pids').value.split(/[,\s]+/).filter(Boolean),
  bitrate_min:Math.round(Number(g('p.bitrate_min').value||0)*1e6),bitrate_max:Math.round(Number(g('p.bitrate_max').value||0)*1e6)};
 c.saidas=c.saidas.map((s,i)=>{const o={tipo:s.tipo};CS[s.tipo].forEach(([k,,t])=>{const v=val(g(`s.${i}.${k}`),t);
  if(v!==undefined)o[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});return o})}

function abrir(id){const c=id?dados.find(x=>x.id===id):null;
 editando=c?JSON.parse(JSON.stringify(c)):{nome:'Novo canal',ativo:true,entrada:{tipo:'dektec'},
  processamento:{remover_nulos:true,intervalo_relatorio_s:5},saidas:[{tipo:'srt'}]};
 delete editando.status;$('#dtit').textContent=c?'Editar canal':'Novo canal';desenhar();$('#dlg').showModal()}

$('#frm').onsubmit=async ev=>{ev.preventDefault();coletar();
 try{if(editando.id)await api('PUT','/api/canais/'+editando.id,editando);else await api('POST','/api/canais',editando);
  $('#dlg').close();toast('Canal salvo e aplicado',1);atualizar()}catch(e){toast(e.message)}};

// ---------- log ----------
async function carregarLog(){if(!logId)return;try{const l=await api('GET',`/api/canais/${logId}/log`),p=$('#logtxt'),
 fim=p.scrollTop+p.clientHeight>=p.scrollHeight-20;p.textContent=l.join('\n')||'(sem mensagens — canal parado ou recém-iniciado)';if(fim)p.scrollTop=p.scrollHeight}catch(e){}}

document.addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-fechar')){b.closest('dialog').close();return}
 const a=b.dataset.a,id=b.dataset.id;if(!a)return;const c=dados.find(x=>x.id===id);
 try{if(a==='editar')return abrir(id);
  if(a==='log'){logId=id;$('#ltit').textContent='Log — '+c.nome;$('#logtxt').textContent='';$('#dlglog').showModal();return carregarLog()}
  if(a==='excluir'){if(!confirm(`Excluir o canal "${c.nome}"?`))return;await api('DELETE','/api/canais/'+id)}
  else{b.disabled=true;await api('POST',`/api/canais/${id}/${a}`)}
  atualizar()}catch(e){toast(e.message);b.disabled=false}});
$('#dlglog').addEventListener('close',()=>logId=null);
$('#novo').onclick=()=>abrir();

api('GET','/api/info').then(i=>{if(!i.persistente)$('#avisos').innerHTML='<div class="aviso">Modo linha de comando: alterações feitas aqui não são salvas em arquivo. Use <code>asi2srt rodar -c arquivo.yaml</code> para persistir.</div>'}).catch(()=>{});
atualizar();setInterval(atualizar,2000);setInterval(carregarLog,2000);
</script></body></html>
"""


# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="asi2srt",
        description="Gateway ASI/UDP/SRT -> SRT/UDP com painel web (motor TSDuck).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Entradas: dektec:PLACA:CANAL | linuxasi:/dev/asirxN | udp://IP:PORTA[?local=IP]\n"
               "          srt://:PORTA?mode=listener | srt://HOST:PORTA | arquivo:X.ts\n"
               "Saídas:   srt://HOST:PORTA?mode=caller|listener&latency=MS&passphrase=SENHA&streamid=ID\n"
               "          udp://IP:PORTA?ttl=N&local=IP    (repita -s para várias saídas)")
    ap.add_argument("--versao", action="version", version=f"asi2srt {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="log detalhado")
    sub = ap.add_subparsers(dest="cmd", metavar="subcomando")

    def opcoes_canal(p):
        p.add_argument("-c", "--config", help="arquivo YAML/JSON (padrão: asi2srt.yaml ou /etc/asi2srt/config.yaml)")
        p.add_argument("-e", "--entrada", help="atalho sem config, ex.: dektec:0:0")
        p.add_argument("-s", "--saida", action="append", help='atalho sem config (repetível), ex.: "srt://host:9000"')
        p.add_argument("--nome", default="ASI-1")
        p.add_argument("--manter-nulos", action="store_true", help="não remover pacotes nulos")
        p.add_argument("--bitrate-min", type=int, default=0, help="alarme de bitrate mínimo (bit/s)")
        p.add_argument("--status", default="0.0.0.0:8088", help="endereço do painel ('' desativa)")
        p.add_argument("--usuario", default="admin", help="usuário do painel")
        p.add_argument("--senha-painel", default="", help="senha do painel (padrão: gerada)")
        p.add_argument("--tsp", default="tsp", help="caminho do tsp")

    p = sub.add_parser("rodar", help="executa o gateway e o painel web")
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

    p = sub.add_parser("teste", help="teste sem hardware (ffmpeg -> SRT + UDP local)")
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
