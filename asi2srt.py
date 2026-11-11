#!/usr/bin/env python3
"""
asi2srt - gateway DVB-ASI / UDP / SRT  ->  SRT / UDP, em um único programa.

Captura um transport stream (placa DVB-ASI DekTec ou driver Linux ASI/DVEO, UDP,
SRT ou arquivo) e o reenvia para uma ou mais saídas SRT e/ou UDP, usando o
TSDuck (tsp) como motor. Inclui painel web para gerenciar os canais e análise
TR 101 290 Prioridade 1 (sync loss, sync byte, CC, PAT, PMT, PID) de cada entrada.

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
import hashlib
import hmac
import html
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
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

__version__ = "2.24.2"
TITULO_PADRAO = "Engenharia TV Cultura"

try:
    import yaml  # opcional: sem PyYAML a configuração é salva em JSON
except ImportError:
    yaml = None

log = logging.getLogger("asi2srt")

TIPOS_ENTRADA = ("dektec", "linuxasi", "udp", "srt", "arquivo")
# O DTAPI da DekTec só existe para Linux em processadores Intel/AMD (não há para ARM/Raspberry Pi)
DEKTEC_DISPONIVEL = platform.machine().lower() in ("x86_64", "amd64", "i686", "i386")
TIPOS_SAIDA = ("srt", "udp", "rtp", "rtmp")

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
      analise_tr101290: true  # sync loss, sync byte, CC, PAT, PMT, PID (TR 101 290 P1)
      remover_nulos: true     # tira o stuffing (PID 0x1FFF) e economiza banda
      remover_pids: []        # PIDs a descartar, ex.: ["0x0012", "0x0014"]
      bitrate_min: 1000000    # alarme abaixo disto (bit/s). 0 = desligado
      bitrate_max: 0          # alarme acima disto (bit/s). 0 = desligado
      intervalo_relatorio_s: 5
      reiniciar_sem_sinal_s: 20   # reinicia a entrada se ficar muda (0 = desligado)

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
  usuario: admin              # login inicial do painel (depois os usuários ficam em 'usuarios', com senha em hash)
  senha_painel: admin         # senha inicial; troque pelo painel. Para recuperar o acesso, escreva aqui uma nova
  log_arquivo: ""
  # rotas_por_placa: true     # cada placa sai pelo próprio gateway (placa reserva em outra rede/operadora)
  # failover: {sem_sinal_s: 15, voltar_apos_s: 30}
"""


CONFIG_INICIAL = """\
# asi2srt - configuração. Crie e edite os canais pelo painel web (http://IP:8088).
# Exemplo comentado de todas as opções: asi2srt init exemplo.yaml
canais: []
supervisor:
  reinicio_min_s: 2
  reinicio_max_s: 60
  tsp: tsp
  status_http: "0.0.0.0:8088"
  usuario: admin
  senha_painel: admin
  titulo: "Engenharia TV Cultura"   # nome exibido no painel (também editável pelo botão ⚙)
  logo: ""                          # arquivo do logo (enviado pelo painel)
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
    try:
        os.chmod(tmp, 0o600)   # guarda os hashes de senha dos usuários
    except OSError:
        pass
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
    if txt.startswith(("srt://", "udp://")):
        q = {k: v[-1] for k, v in parse_qs(urlparse(txt).query).items()}
        extra = {"interface": q["interface"]} if q.get("interface") else {}
    if txt.startswith("srt://"):
        s = _srt_de_url(urlparse(txt), "caller")
        s.pop("overhead_pct", None)
        return {**s, **extra}
    if txt.startswith("udp://"):
        u = _udp_de_url(urlparse(txt))
        return {"tipo": "udp", "endereco": u["endereco"], "interface_local": u["interface_local"], **extra}
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


def _com_servicos(d, url):
    q = {k: v[-1] for k, v in parse_qs(urlparse(url).query).items()}
    if q.get("interface"):
        d["interface"] = q["interface"]
    if q.get("servico"):
        d["servicos"] = [x for x in q["servico"].split(",") if x.strip()]
        if d.get("tipo") == "rtmp":  # o parâmetro é nosso: não vai para o servidor RTMP
            d["url"] = re.sub(r"([?&])servico=[^&]*&?", r"\1", d["url"]).rstrip("?&")
    return d


def saida_de_url(url: str) -> dict:
    return _com_servicos(_saida_de_url(url), url)


def _saida_de_url(url: str) -> dict:
    """srt://host:porta?mode=..&latency=..&passphrase=..&streamid=..  |  udp://IP:PORTA?ttl=..&local=IP"""
    u = urlparse(url)
    if u.scheme == "srt":
        return _srt_de_url(u, "caller")
    if u.scheme == "udp":
        return _udp_de_url(u)
    if u.scheme == "rtp":
        d = _udp_de_url(u)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        d.update(tipo="rtp", payload_type=int(q.get("pt", 33)), ssrc=q.get("ssrc", ""))
        return d
    if u.scheme in ("rtmp", "rtmps"):
        # a URL vai inteira para o servidor (pode ter parâmetros próprios); opções no painel ou na config
        return {"tipo": "rtmp", "url": url, "video": "copiar", "audio": "aac",
                "video_kbps": 4000, "audio_kbps": 128, "programa": ""}
    raise SystemExit(f"saída inválida: {url!r} (use srt://, udp://, rtp://, rtmp:// ou rtmps://)")


# ======================================================================
# Montagem do comando tsp
# ======================================================================
def _pid(valor):
    return valor if isinstance(valor, int) else int(str(valor).strip(), 0)


def _args_srt(s: dict, entrada: bool, prefixo=None) -> list:
    modo = s.get("modo", "listener" if entrada else "caller")
    if modo not in ("caller", "listener"):
        raise ValueError(f"modo SRT inválido: {modo}")
    end = str(s.get("endereco", "")).strip()
    if not end:
        raise ValueError("SRT sem endereço")
    if modo == "caller" and not re.match(r"^[A-Za-z0-9.\-]+:\d{1,5}$", end):
        raise ValueError(f"endereço SRT caller inválido: {end!r} (use IP:porta, ex.: 187.60.226.82:6001)")
    if modo == "listener" and not re.match(r"^([A-Za-z0-9.\-]+:)?\d{1,5}$", end):
        raise ValueError(f"endereço SRT listener inválido: {end!r} (use a porta, ex.: 9000)")
    a = ["--" + modo, str(s["endereco"]).strip(), "--transtype", "live",
         "--latency", str(int(s.get("latencia_ms") or 200))]
    if not entrada:
        a += ["--ohead-bw", str(int(s.get("overhead_pct") or 25))]
    if s.get("senha"):
        if not 10 <= len(s["senha"]) <= 79:
            raise ValueError("a senha SRT deve ter entre 10 e 79 caracteres")
        a += ["--passphrase", s["senha"], "--pbkeylen", str(int(s.get("tamanho_chave") or 16))]
    if s.get("stream_id"):
        a += ["--streamid", s["stream_id"]]
    ip = _ip_local(s)
    if ip:
        if modo == "caller":
            a += ["--local-interface", ip]          # sai pela placa escolhida
        elif re.match(r"^\d{1,5}$", end):
            a[1] = f"{ip}:{end}"                     # listener: escuta só no IP da placa escolhida
    if prefixo:  # estatísticas do link SRT a cada 1 s, em JSON, identificadas pelo prefixo
        a += ["--statistics-interval", "1000", f"--json-line={prefixo}:"]
    return a


def _args_saida(s: dict, prefixo=None) -> list:
    tipo = s.get("tipo", "srt")
    if tipo == "srt":
        return ["srt"] + _args_srt(s, entrada=False, prefixo=prefixo)
    if tipo == "udp":
        if not re.match(r"^[\w.\-]*:\d+$|^\d+$", str(s.get("endereco", "")).strip()):
            raise ValueError("saída UDP precisa de ip:porta")
        a = ["ip", str(s["endereco"]).strip(), "--packet-burst", "7"]
        if s.get("ttl"):
            a += ["--ttl", str(int(s["ttl"]))]
        ip = _ip_local(s)
        if ip:
            a += ["--local-address", ip]
        return a
    if tipo == "rtp":
        a = _args_saida({**s, "tipo": "udp"})
        a += ["--rtp"]
        if s.get("payload_type") not in (None, "", 33):
            pt = int(s["payload_type"])
            if not 0 <= pt <= 127:
                raise ValueError("payload type RTP deve estar entre 0 e 127")
            a += ["--payload-type", str(pt)]
        if str(s.get("ssrc") or "").strip():
            a += ["--ssrc-identifier", str(int(str(s["ssrc"]).strip(), 0))]
        return a
    raise ValueError(f"tipo de saída desconhecido: {tipo}")


def _args_ffmpeg_rtmp(s: dict, porta_entrada) -> list:
    """Saída RTMP/RTMPS via ffmpeg (o TSDuck não tem RTMP). RTMP (FLV) aceita H.264 + AAC:
    por padrão copia o vídeo e converte o áudio para AAC (ISDB-Tb usa AAC LATM, que o FLV não aceita)."""
    url = str(s.get("url", "")).strip()
    chave = str(s.get("chave") or "").strip()
    if chave:  # servidor e chave em campos separados (como no YouTube/Facebook)
        url = url.rstrip("/") + "/" + chave.lstrip("/")
    if not re.match(r"^rtmps?://[^/\s]+/\S+", url):
        raise ValueError("saída RTMP precisa da URL completa, ex.: rtmp://servidor/app/chave")
    video, audio = s.get("video", "copiar"), s.get("audio", "aac")
    if video not in ("copiar", "h264") or audio not in ("aac", "copiar"):
        raise ValueError("vídeo deve ser 'copiar' ou 'h264' e áudio 'aac' ou 'copiar'")
    servs = servicos_da_saida(s)
    if len(servs) > 1:
        raise ValueError("a saída RTMP envia um serviço só: escolha apenas um")
    prog = servs[0] if servs else ""
    if prog and not prog.isdigit():
        raise ValueError("na saída RTMP informe o número do serviço (ex.: 257), não o nome")
    sel = f"p:{prog}:" if prog else ""
    entrada = (f"udp://127.0.0.1:{porta_entrada or 'PORTA_SAIDA'}?fifo_size=1000000&overrun_nonfatal=1"
               f"&buffer_size=4194304&timeout=10000000")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1",
           "-stats_period", "2", "-fflags", "+genpts+discardcorrupt", "-i", entrada,
           "-map", f"0:{sel}v:0", "-map", f"0:{sel}a:0?"]
    if video == "copiar":
        cmd += ["-c:v", "copy"]
    else:
        vk = int(s.get("video_kbps") or 4000)
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-b:v", f"{vk}k", "-maxrate", f"{vk}k", "-bufsize", f"{2 * vk}k", "-g", "60"]
    if audio == "copiar":
        cmd += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{int(s.get('audio_kbps') or 128)}k", "-ar", "48000", "-ac", "2"]
    cmd += ["-rw_timeout", "15000000",  # desiste em 15 s se o servidor RTMP não responder
            "-f", "flv", "-flvflags", "no_duration_filesize", url]
    return cmd


_FABRICANTES_PCI = {
    "8086": "Intel", "10ec": "Realtek", "14e4": "Broadcom", "15b3": "Mellanox/NVIDIA", "1d6a": "Aquantia/Marvell",
    "11ab": "Marvell", "1924": "Solarflare/AMD", "1425": "Chelsio", "19a2": "Emulex", "1077": "QLogic",
    "1022": "AMD", "1af4": "Virtio (VM)", "15ad": "VMware (VM)", "1414": "Microsoft Hyper-V (VM)", "1969": "Qualcomm Atheros",
    "168c": "Qualcomm Atheros", "14c3": "MediaTek", "177d": "Cavium", "1137": "Cisco", "8088": "Wangxun",
    "1dab": "Pensando", "1fc9": "Tehuti", "19ee": "Netronome", "1ae0": "Google (VM)", "1d0f": "Amazon (VM)",
}
_MODELO_CACHE = {}
_TRAFEGO_ANT = {}
_PREFIXOS_INTERNOS = ("docker", "veth", "br-", "virbr", "vnet", "cni", "flannel", "kube", "cali", "vxlan", "ifb",
                      "dummy", "lxc", "lxd", "podman")


def _ler_sys(caminho):
    try:
        with open(caminho) as f:
            return f.read().strip()
    except OSError:
        return ""


def _pci_do_dispositivo(nome):
    """Endereço PCI (ex. 0000:03:00.0) da placa, também quando ela é virtio (um nível abaixo do PCI)."""
    try:
        real = os.path.realpath(f"/sys/class/net/{nome}/device")
    except OSError:
        return None
    for parte in reversed(real.split("/")):
        if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", parte):
            return parte
    return None


def _modelo_pci(slot):
    if not slot:
        return None
    if slot in _MODELO_CACHE:
        return _MODELO_CACHE[slot]
    modelo = None
    if shutil.which("lspci"):
        try:
            r = subprocess.run(["lspci", "-mm", "-s", slot], capture_output=True, text=True, timeout=5)
            campos = shlex.split(r.stdout.strip()) if r.stdout.strip() else []
            if len(campos) >= 4:      # slot, classe, fabricante, dispositivo
                modelo = f"{campos[2]} {campos[3]}".strip()
        except (OSError, ValueError, subprocess.SubprocessError):
            modelo = None
    if not modelo:
        base = f"/sys/bus/pci/devices/{slot}"
        ven = _ler_sys(f"{base}/vendor").replace("0x", "")
        dev = _ler_sys(f"{base}/device").replace("0x", "")
        if ven:
            modelo = f"{_FABRICANTES_PCI.get(ven, 'fabricante ' + ven)} (id {ven}:{dev})"
    _MODELO_CACHE[slot] = modelo
    return modelo


def _tipo_interface(nome):
    base = f"/sys/class/net/{nome}"
    if os.path.isdir(f"{base}/wireless") or os.path.isdir(f"{base}/phy80211"):
        return "wifi"
    if os.path.isdir(f"{base}/bonding"):
        return "bond"
    if os.path.isdir(f"{base}/bridge"):
        return "bridge" if not nome.startswith(("docker", "br-", "virbr", "cni", "lxc")) else "interna"
    if os.path.exists(f"/proc/net/vlan/{nome}"):
        return "vlan"
    if nome.startswith(_PREFIXOS_INTERNOS):
        return "interna"
    if os.path.exists(f"{base}/device"):
        return "fisica"
    if nome.startswith(("tun", "tap", "ppp", "wwan", "wg", "zt", "tailscale")):
        return "vpn"
    return "virtual"


def listar_interfaces(todas=False):
    """Placas de rede do computador: nome, IPs, estado do link, velocidade, MAC, tipo, modelo, driver,
    gateway e tráfego. Sem 'todas', as interfaces internas (Docker, VM, containers) ficam de fora."""
    import socket
    ips, ips6, gw = {}, {}, {}
    if shutil.which("ip"):
        try:
            r = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=5)
            for it in json.loads(r.stdout or "[]"):
                ips[it["ifname"]] = [x["local"] for x in it.get("addr_info", []) if x.get("family") == "inet"]
                ips6[it["ifname"]] = [x["local"] for x in it.get("addr_info", []) if x.get("family") == "inet6"
                                      and x.get("scope") == "global"]
            r = subprocess.run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=5)
            for it in json.loads(r.stdout or "[]"):
                if it.get("dev") and it.get("gateway"):
                    gw.setdefault(it["dev"], it["gateway"])
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            ips, ips6 = {}, {}
    if not gw:   # sem o comando ip: tabela de rotas do kernel
        try:
            with open("/proc/net/route") as f:
                next(f, None)
                for ln in f:
                    c = ln.split()
                    if len(c) > 2 and c[1] == "00000000" and c[2] != "00000000":
                        g = bytes.fromhex(c[2])[::-1]
                        gw.setdefault(c[0], ".".join(str(b) for b in g))
        except (OSError, ValueError):
            pass

    agora = time.monotonic()
    out = []
    try:
        nomes = [n for _, n in socket.if_nameindex()]
    except OSError:
        nomes = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []
    for nome in nomes:
        if nome == "lo":
            continue
        tipo = _tipo_interface(nome)
        if tipo == "interna" and not todas:
            continue
        base = f"/sys/class/net/{nome}"
        lista = ips.get(nome)
        if lista is None:
            lista = []
            try:
                import fcntl
                import struct
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    lista = [socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915,  # SIOCGIFADDR
                                                          struct.pack("256s", nome[:15].encode()))[20:24])]
                finally:
                    s.close()
            except (OSError, ImportError):
                lista = []
        vel = _ler_sys(f"{base}/speed")
        carrier = _ler_sys(f"{base}/carrier") == "1"
        estado = _ler_sys(f"{base}/operstate") or "desconhecido"
        slot = _pci_do_dispositivo(nome)
        drv = os.path.basename(os.path.realpath(f"{base}/device/driver")) if os.path.exists(f"{base}/device/driver") else None
        modelo = _modelo_pci(slot)
        if not modelo and os.path.exists(f"{base}/device"):   # USB: fabricante/produto do dispositivo
            raiz = os.path.realpath(f"{base}/device")
            for _ in range(3):
                prod = _ler_sys(f"{raiz}/product")
                if prod:
                    modelo = f"{_ler_sys(f'{raiz}/manufacturer')} {prod}".strip() + " (USB)"
                    break
                raiz = os.path.dirname(raiz)
        rx = _ler_sys(f"{base}/statistics/rx_bytes")
        tx = _ler_sys(f"{base}/statistics/tx_bytes")
        rx, tx = (int(rx) if rx.isdigit() else 0), (int(tx) if tx.isdigit() else 0)
        ant = _TRAFEGO_ANT.get(nome)
        taxa_rx = taxa_tx = None
        if ant and agora - ant[0] >= 0.5 and rx >= ant[1] and tx >= ant[2]:
            dt = agora - ant[0]
            taxa_rx, taxa_tx = int((rx - ant[1]) * 8 / dt), int((tx - ant[2]) * 8 / dt)
        if not ant or agora - ant[0] >= 0.5:
            _TRAFEGO_ANT[nome] = (agora, rx, tx)
        membros = None
        if tipo == "bond":
            membros = _ler_sys(f"{base}/bonding/slaves").split() or None
        mestre = os.path.basename(os.path.realpath(f"{base}/master")) if os.path.exists(f"{base}/master") else None
        out.append({"nome": nome, "ips": lista, "ipv6": ips6.get(nome, []), "estado": estado,
                    "link": carrier and estado in ("up", "unknown"), "mac": _ler_sys(f"{base}/address"),
                    "velocidade_mbps": int(vel) if vel.lstrip("-").isdigit() and int(vel) > 0 else None,
                    "duplex": _ler_sys(f"{base}/duplex") or None, "mtu": int(_ler_sys(f"{base}/mtu") or 0) or None,
                    "virtual": tipo not in ("fisica", "wifi"), "tipo": tipo, "modelo": modelo, "driver": drv,
                    "pci": slot, "gateway": gw.get(nome), "membros": membros, "mestre": mestre,
                    "rx_bytes": rx, "tx_bytes": tx, "rx_bps": taxa_rx, "tx_bps": taxa_tx})
    ordem = {"fisica": 0, "wifi": 1, "bond": 2, "vlan": 3, "bridge": 4, "vpn": 5, "virtual": 6, "interna": 7}
    out.sort(key=lambda i: (ordem.get(i["tipo"], 9), not i["link"], not i["ips"], i["nome"]))
    return out


def placas_sem_driver():
    """Controladoras de rede vistas no barramento PCI que não viraram interface (falta driver/firmware)."""
    out = []
    base = "/sys/bus/pci/devices"
    if not os.path.isdir(base):
        return out
    for slot in sorted(os.listdir(base)):
        if not _ler_sys(f"{base}/{slot}/class").startswith("0x02"):     # classe 02 = controladora de rede
            continue
        d = f"{base}/{slot}"
        tem_net = os.path.isdir(f"{d}/net") or any(os.path.isdir(os.path.join(d, x, "net")) for x in os.listdir(d)
                                                    if x.startswith("virtio"))
        if not tem_net:
            drv = os.path.basename(os.path.realpath(f"{d}/driver")) if os.path.exists(f"{d}/driver") else None
            out.append({"pci": slot, "modelo": _modelo_pci(slot), "driver": drv})
    return out


def ip_da_interface(nome: str) -> str:
    for i in listar_interfaces(todas=True):
        if i["nome"] == nome:
            if not i["ips"]:
                raise ValueError(f"a placa de rede {nome} não tem endereço IPv4")
            return i["ips"][0]
    raise ValueError(f"placa de rede {nome!r} não encontrada neste computador")


def _ip_local(cfg: dict) -> str:
    """IP local a usar: da placa escolhida (campo 'interface') ou o IP informado ('interface_local')."""
    nome = str(cfg.get("interface") or "").strip()
    if nome:
        if not re.match(r"^[\w.\-:@]{1,32}$", nome):
            raise ValueError(f"nome de placa de rede inválido: {nome!r}")
        return ip_da_interface(nome)
    return str(cfg.get("interface_local") or "").strip()


# ======================================================================
# Placa de rede reserva (failover automático por entrada e por saída)
# ======================================================================
_NICS_CACHE = {"t": 0.0, "v": {}}


def estado_nics(cache_s=1.5):
    """{nome: {"ligada": bool, "ip": str|None, "estado": str}} - link (operstate/carrier) e IPv4 de cada placa."""
    agora = time.monotonic()
    if agora - _NICS_CACHE["t"] < cache_s:
        return _NICS_CACHE["v"]
    out = {}
    for i in listar_interfaces(todas=True):
        nome = i["nome"]
        try:
            with open(f"/sys/class/net/{nome}/carrier") as f:
                carrier = f.read().strip() == "1"
        except OSError:
            carrier = False          # placa desligada: o kernel nem deixa ler o carrier
        est = i.get("estado") or ""
        ligada = (est == "up") or (est == "unknown" and carrier)
        out[nome] = {"ligada": bool(ligada and (carrier or est == "up")), "ip": (i["ips"] or [None])[0], "estado": est}
    _NICS_CACHE.update(t=agora, v=out)
    return out


def nic_utilizavel(nome, nics=None):
    n = (nics if nics is not None else estado_nics()).get(nome)
    return bool(n and n["ligada"] and n["ip"])


def _validar_reserva(item):
    res = str(item.get("interface_reserva") or "").strip()
    if not res:
        return
    if not re.match(r"^[\w.\-:@]{1,32}$", res):
        raise ValueError(f"nome de placa reserva inválido: {res!r}")
    pri = str(item.get("interface") or "").strip()
    if not pri:
        raise ValueError("para usar placa reserva, escolha também a placa principal")
    if pri == res:
        raise ValueError("a placa reserva precisa ser diferente da principal")


class FailoverNic:
    """Escolhe a placa de uma conexão (entrada ou saída): principal, ou a reserva quando a principal
    perde o link/IP ou a conexão não funciona por ela. Volta para a principal quando ela fica estável."""

    def __init__(self, item, rotulo, estavel_s=30, sem_sinal_s=15):
        self.principal = str(item.get("interface") or "").strip()
        self.reserva = str(item.get("interface_reserva") or "").strip()
        self.rotulo = rotulo
        self.estavel_s = estavel_s
        self.sem_sinal_s = sem_sinal_s
        nics = estado_nics()
        self.ativa = self.principal if (nic_utilizavel(self.principal, nics) or not nic_utilizavel(self.reserva, nics)) \
            else self.reserva
        self.motivo = None if self.ativa == self.principal else "placa principal sem link ao iniciar"
        self.desde = time.monotonic()
        self.eventos = collections.deque(maxlen=30)
        self.ok_principal_desde = None
        self.falhas_seguidas = 0       # trocas por falta de sinal sem sucesso (espaça as tentativas)
        if self.ativa != self.principal:
            self._evento(self.principal, self.ativa, self.motivo)

    @property
    def ativo(self):
        return bool(self.reserva)

    def aplicar(self, item):
        """Cópia da configuração usando a placa ativa."""
        d = dict(item)
        if self.ativo:
            d["interface"] = self.ativa
            d.pop("interface_local", None)
            d.pop("interface_reserva", None)   # o processo só precisa da placa em uso agora
        return d

    def _evento(self, de, para, motivo):
        self.eventos.append({"em": time.time(), "de": de, "para": para, "motivo": motivo})

    def avaliar(self, agora, sem_sinal_ha=None):
        """Devolve (nova_placa, motivo) quando é preciso trocar; senão None.
        sem_sinal_ha: há quantos segundos a conexão não funciona pela placa ativa (None = não se aplica)."""
        if not self.ativo:
            return None
        nics = estado_nics()
        outra = self.reserva if self.ativa == self.principal else self.principal
        ok_ativa, ok_outra = nic_utilizavel(self.ativa, nics), nic_utilizavel(outra, nics)
        if not ok_ativa and ok_outra:
            n = nics.get(self.ativa) or {}
            causa = "sem link (cabo/porta)" if not n.get("ligada") else "sem endereço IP"
            return outra, f"placa {self.ativa} {causa}"
        # conexão não funciona pela placa ativa: tenta a outra (com espera crescente se nenhuma funcionar)
        espera = self.sem_sinal_s * min(2 ** self.falhas_seguidas, 8)
        if sem_sinal_ha is not None and ok_outra and sem_sinal_ha > espera and agora - self.desde > espera:
            return outra, f"sem conexão/sinal pela placa {self.ativa} há {int(sem_sinal_ha)} s"
        # volta para a principal quando ela está estável (só se saiu dela por falta de link/IP)
        if self.ativa == self.reserva and self.motivo and \
                any(x in self.motivo for x in ("sem link", "sem endereço", "ao iniciar")):
            if nic_utilizavel(self.principal, nics):
                if self.ok_principal_desde is None:
                    self.ok_principal_desde = agora
                elif agora - self.ok_principal_desde >= self.estavel_s:
                    return self.principal, f"placa {self.principal} de volta e estável há {self.estavel_s} s"
            else:
                self.ok_principal_desde = None
        return None

    def trocar(self, para, motivo, agora):
        de = self.ativa
        if "sem conexão" in motivo:
            self.falhas_seguidas += 1
        else:
            self.falhas_seguidas = 0
        self.ativa, self.motivo, self.desde, self.ok_principal_desde = para, (None if para == self.principal and
                                                                            "de volta" in motivo else motivo), agora, None
        self._evento(de, para, motivo)
        return de

    def sucesso(self):
        """A conexão funcionou pela placa ativa."""
        self.falhas_seguidas = 0

    def status(self):
        nics = estado_nics()
        return {"principal": self.principal, "reserva": self.reserva, "ativa": self.ativa,
                "em_reserva": self.ativa != self.principal, "motivo": self.motivo,
                "principal_ok": nic_utilizavel(self.principal, nics), "reserva_ok": nic_utilizavel(self.reserva, nics),
                "eventos": list(self.eventos)[-10:], "trocas": len(self.eventos)}


def aplicar_rotas_por_placa():
    """Roteamento pela origem: o tráfego que sai com o IP de uma placa usa o gateway DESSA placa.
    Necessário quando a reserva está em outra rede/operadora e a principal ainda tem link (falha adiante).
    Idempotente; precisa de root. Devolve a lista de comandos aplicados."""
    if not shutil.which("ip"):
        return []
    try:
        rotas = json.loads(subprocess.run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True,
                                          text=True, timeout=5).stdout or "[]")
        regras = json.loads(subprocess.run(["ip", "-j", "-4", "rule", "show"], capture_output=True,
                                           text=True, timeout=5).stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    gw = {r["dev"]: r["gateway"] for r in rotas if r.get("dev") and r.get("gateway")}
    feitos = []
    for nome, info in estado_nics(cache_s=0).items():
        if nome not in gw or not info["ip"]:
            continue
        try:
            with open(f"/sys/class/net/{nome}/ifindex") as f:
                idx = int(f.read().strip())
        except (OSError, ValueError):
            continue
        tabela = str(200 + idx)
        cmds = [["ip", "-4", "route", "replace", "default", "via", gw[nome], "dev", nome, "table", tabela]]
        if not any(r.get("src") == info["ip"] and str(r.get("table")) == tabela for r in regras):
            cmds.append(["ip", "-4", "rule", "add", "from", f"{info['ip']}/32", "table", tabela, "priority", str(1000 + idx)])
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode == 0:
                feitos.append(" ".join(c))
            else:
                log.warning("rotas por placa: %s -> %s", " ".join(c), (r.stderr or "").strip())
    return feitos


def _args_ip_entrada(endereco, interface_local="") -> list:
    """Argumentos da entrada 'ip' do TSDuck. Ela só aceita IP no endereço se for MULTICAST
    ("address X is not multicast"); para unicast usa-se só a porta + --local-address."""
    end = str(endereco).strip()
    host, _, porta = end.rpartition(":")
    if not porta.isdigit():
        raise ValueError(f"endereço UDP inválido: {end!r} (use ip:porta ou só a porta)")
    host = host.strip("[]")
    multicast = False
    if host:
        try:
            import ipaddress
            multicast = ipaddress.ip_address(host).is_multicast
        except ValueError:
            raise ValueError(f"endereço UDP inválido: {end!r} (use o IP numérico, ex.: 239.1.1.1:1234)")
    if multicast:
        a = [f"{host}:{porta}"]
        if interface_local:
            a += ["--local-address", interface_local]
        return a
    local = interface_local or ("" if host in ("", "0.0.0.0") else host)
    return [porta] + (["--local-address", local] if local else [])


BUFFER_UDP = 16 * 1024 * 1024   # buffer de recepção UDP (o kernel limita a net.core.rmem_max; ver instalar-servico)


def udp_descartes():
    """Datagramas que o kernel descartou em cada porta UDP local (buffer cheio = o processo não deu conta)."""
    out = {}
    for arq in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(arq) as f:
                next(f, None)
                for ln in f:
                    c = ln.split()
                    if len(c) >= 13:
                        porta = int(c[1].rsplit(":", 1)[1], 16)
                        out[porta] = out.get(porta, 0) + int(c[-1])
        except (OSError, ValueError, IndexError):
            pass
    return out


def _cfg_validacao(c):
    """Para validar com a placa principal fora do ar: usa a reserva, como o canal faria ao iniciar."""
    c = copy.deepcopy(c)
    for it in [c.get("entrada") or {}] + list(c.get("saidas") or []):
        res, pri = it.get("interface_reserva"), it.get("interface")
        if res and pri and not nic_utilizavel(pri) and nic_utilizavel(res):
            it["interface"] = res
    return c


def montar_comando(canal: dict, tsp_bin: str = "tsp", porta_analise=None, porta_dist=None) -> list:
    """porta_analise: porta UDP local onde o analisador TR 101 290 recebe uma cópia do TS."""
    canal = normalizar_canal(canal)
    ent = canal["entrada"]
    _validar_reserva(ent)
    proc = canal.get("processamento") or {}
    saidas = canal.get("saidas") or []
    so_analise = not saidas  # sem saídas: o canal só recebe e analisa

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
        cmd += ["-I", "ip"] + _args_ip_entrada(ent["endereco"], _ip_local(ent))
        cmd += ["--receive-timeout", "5000", "--buffer-size", str(BUFFER_UDP)]
    elif tipo == "srt":
        cmd[1:1] = ["--verbose"]  # mostra o endereço do outro lado ("connected from ...")
        cmd += ["-I", "srt"] + _args_srt(ent, entrada=True, prefixo="SRTIN")
    elif tipo == "arquivo":
        if not ent.get("caminho"):
            raise ValueError("entrada de arquivo precisa do caminho")
        cmd += ["-I", "file", str(ent["caminho"])]
        if ent.get("loop", True):
            cmd += ["--infinite"]
    else:
        raise ValueError(f"tipo de entrada desconhecido: {tipo}")

    # cópia do TS bruto (logo após a entrada, antes de filtros) para o analisador TR 101 290
    if porta_analise and proc.get("analise_tr101290", True):
        filho = [tsp_bin, "-O", "ip", f"127.0.0.1:{porta_analise}", "--packet-burst", "7"]
        cmd += ["-P", "fork", "--nowait", "--ignore-abort", "--buffered-packets", "7",
                " ".join(shlex.quote(a) for a in filho)]

    pids =[_pid(p) for p in (proc.get("remover_pids") or []) if str(p).strip()]
    if proc.get("remover_nulos", True) and not so_analise:
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

    # As saídas NÃO ficam neste processo: se uma saída falhar (ex.: SRT sem resposta), o TSDuck
    # derrubaria a entrada e a análise junto. Este processo entrega o TS a um distribuidor local
    # e cada saída roda em seu próprio processo (montar_saida), supervisionado separadamente.
    for i, s in enumerate(saidas):
        montar_saida(s, i, tsp_bin)  # valida a configuração de cada saída
    if so_analise and not porta_dist:
        cmd += ["-O", "drop"]
    else:  # o distribuidor alimenta as saídas e a janela de imagem
        cmd += ["-O", "ip", f"127.0.0.1:{porta_dist or 'PORTA_DIST'}", "--packet-burst", "7"]
    return cmd


def servicos_da_saida(s: dict) -> list:
    """Serviços escolhidos para uma saída (vazio = TS completo). Aceita número (decimal/hex) ou nome."""
    v = s.get("servicos")
    if v in (None, "", []):
        v = [s["programa"]] if str(s.get("programa") or "").strip() else []   # campo antigo do RTMP
    if isinstance(v, (str, int)):
        v = [v]
    out = []
    for x in v:
        x = str(x).strip()
        if not x:
            continue
        if len(x) > 64 or not re.match(r"^[\w À-ÿ.+&'()/-]+$", x):
            raise ValueError(f"serviço inválido: {x!r} (use o número, ex.: 257, ou o nome do serviço)")
        if x.lower().startswith("0x"):
            x = str(int(x, 16))
        if x not in out:
            out.append(x)
    return out


def montar_saida(s: dict, indice: int, tsp_bin: str = "tsp", porta_entrada=None) -> list:
    """Processo de uma saída: recebe o TS do distribuidor local e envia por SRT, UDP, RTP ou RTMP."""
    if s.get("tipo") == "rtmp":
        return _args_ffmpeg_rtmp(s, porta_entrada)
    _validar_reserva(s)
    cmd = [tsp_bin, "--realtime"]
    if s.get("tipo", "srt") == "srt":
        cmd.append("--verbose")  # mostra "connected to ..." com o endereço do outro lado
    cmd += ["-I", "ip", str(porta_entrada or "PORTA_SAIDA"), "--local-address", "127.0.0.1", "--buffer-size", str(BUFFER_UDP)]
    servs = servicos_da_saida(s)
    if servs:  # só os serviços escolhidos: zap refaz PAT/PMT/SDT; --ignore-absent não derruba a saída
        cmd += ["-P", "zap", "--ignore-absent", "--eit"] + servs
    cmd += ["-P", "bitrate_monitor", "--periodic-bitrate", "2",   # taxa real enviada por esta saída
            "-O"] + _args_saida(s, prefixo=f"SRTOUT{indice}")
    return cmd


def saidas_publicas(saidas):
    """Configuração das saídas sem senhas nem chaves RTMP (para relatórios e telas)."""
    out = []
    for s in saidas:
        tinha_chave = bool(str(s.get("chave") or "").strip())
        s = {k: v for k, v in s.items() if k not in ("senha", "chave")}
        if tinha_chave:
            s["chave_oculta"] = True          # a chave estava em campo separado e foi retirada
        url = str(s.get("url") or "")
        # rtmp://servidor/app = só o servidor; rtmp://servidor/app/chave = chave no fim da URL
        if url and (not tinha_chave or len([x for x in urlparse(url).path.split("/") if x]) >= 2):
            s["url"] = mascarar(url)
        out.append(s)
    return out


def mascarar(txt: str) -> str:
    """Esconde senhas SRT e chaves de transmissão RTMP em textos que vão para log/painel."""
    txt = re.sub(r"(--passphrase\s+)('[^']*'|[^\s']+)", r"\1********", txt)
    txt = re.sub(r"(passphrase=)([^&\s']+)", r"\1********", txt)
    return re.sub(r"(rtmps?://[^\s'\"]+/)([^/\s'\".:]+)", r"\1********", txt)


def comando_para_log(cmd: list) -> str:
    return mascarar(" ".join(shlex.quote(a) for a in cmd))


# ======================================================================
# Analisador de TS: TR 101 290 (P1, P2, P3), PIDs, serviços e PCR
# ======================================================================
TR_ITENS = [
    # chave, prioridade, código, nome, descrição
    ("sync_loss", 1, "1.1", "TS sync loss",
     "2 ou mais sync bytes corrompidos seguidos, ou perda do sinal de entrada (sem pacotes por mais de 1 s)"),
    ("sync_byte", 1, "1.2", "Sync byte error", "Pacote com sync byte diferente de 0x47"),
    ("pat", 1, "1.3", "PAT error", "PAT ausente por mais de 0,5 s, table_id diferente de 0x00 ou PAT embaralhada"),
    ("cc", 1, "1.4", "Continuity count error",
     "Contador de continuidade fora de ordem, pacote perdido ou duplicado mais de uma vez"),
    ("pmt", 1, "1.5", "PMT error", "PMT ausente por mais de 0,5 s, table_id diferente de 0x02 ou PMT embaralhada"),
    ("pid", 1, "1.6", "PID error", "PID citado em uma PMT ausente por mais de 5 s"),
    ("transport", 2, "2.1", "Transport error", "Pacote com transport_error_indicator = 1"),
    ("crc", 2, "2.2", "CRC error", "Seção PSI/SI (PAT, CAT, PMT, NIT, SDT, EIT, TOT) com CRC32 inválido"),
    ("pcr_rep", 2, "2.3a", "PCR repetition error", "Intervalo entre PCRs consecutivos maior que 40 ms"),
    ("pcr_disc", 2, "2.3b", "PCR discontinuity error",
     "Diferença entre PCRs fora de 0–100 ms sem discontinuity_indicator"),
    ("pcr_acc", 2, "2.4", "PCR accuracy error", "Jitter de PCR maior que ±500 ns (só se aplica a TS de taxa constante)"),
    ("pts", 2, "2.5", "PTS error", "PTS ausente por mais de 700 ms em um PID de vídeo ou áudio"),
    ("cat", 2, "2.6", "CAT error", "Pacotes embaralhados sem CAT, ou table_id diferente de 0x01 no PID 1"),
    ("nit", 3, "3.1", "NIT error", "NIT actual ausente por mais de 10 s, ou table_id inválido no PID 0x10"),
    ("unref", 3, "3.4", "Unreferenced PID", "PID presente que não é citado em PAT, PMT, CAT nem é PID de SI"),
    ("sdt", 3, "3.5", "SDT error", "SDT actual ausente por mais de 2 s, ou table_id inválido no PID 0x11"),
    ("eit", 3, "3.6", "EIT error", "EIT presente/seguinte ausente por mais de 2 s, ou table_id inválido no PID 0x12"),
    ("tdt", 3, "3.8", "TDT error", "TDT/TOT ausente por mais de 30 s, ou table_id inválido no PID 0x14"),
]
TIPOS_TR101290 = tuple(i[0] for i in TR_ITENS)

TIPOS_STREAM = {
    0x01: ("MPEG-1 vídeo", "video"), 0x02: ("MPEG-2 vídeo", "video"), 0x03: ("MPEG-1 áudio", "audio"),
    0x04: ("MPEG-2 áudio", "audio"), 0x05: ("Seções privadas", "dados"), 0x06: ("PES privado", "dados"),
    0x0B: ("DSM-CC", "dados"), 0x0C: ("DSM-CC", "dados"), 0x0D: ("DSM-CC", "dados"),
    0x0F: ("AAC áudio", "audio"), 0x10: ("MPEG-4 vídeo", "video"), 0x11: ("AAC LATM áudio", "audio"),
    0x15: ("Metadados", "dados"), 0x1B: ("H.264/AVC vídeo", "video"), 0x24: ("H.265/HEVC vídeo", "video"),
    0x42: ("AVS vídeo", "video"), 0x81: ("AC-3 áudio", "audio"), 0x86: ("SCTE-35", "dados"),
    0x87: ("E-AC-3 áudio", "audio"),
}
DESCRITOR_PES_PRIVADO = {
    0x6A: ("AC-3 áudio", "audio"), 0x7A: ("E-AC-3 áudio", "audio"), 0x7C: ("AAC áudio", "audio"),
    0x56: ("Teletexto", "dados"), 0x59: ("Legendas DVB", "dados"), 0xFD: ("Dados ISDB (legenda/CC)", "dados"),
}
TIPOS_SERVICO = {
    0x01: "TV digital", 0x02: "Rádio digital", 0x0C: "Dados", 0x11: "TV HD MPEG-2", 0x16: "TV SD H.264",
    0x19: "TV HD H.264", 0x1F: "TV HEVC", 0xA1: "TV 1-seg", 0xC0: "Dados ISDB",
}

def classificar_taxa(valores, limite_cbr_pct, janela=30, minimo=8):
    """CBR ou VBR a partir das taxas medidas a cada segundo (bit/s).
    Usa o coeficiente de variação (desvio padrão / média) dos últimos 'janela' segundos com sinal."""
    v = [x for x in list(valores)[-janela:] if x > 0]
    if len(v) < minimo:
        return {"modo": None, "variacao_pct": None, "amostras": len(v)}
    media = sum(v) / len(v)
    desvio = (sum((x - media) ** 2 for x in v) / len(v)) ** 0.5
    cv = 100.0 * desvio / media if media else 0.0
    return {"modo": "CBR" if cv < limite_cbr_pct else "VBR", "variacao_pct": round(cv, 1),
            "min": int(min(v)), "max": int(max(v)), "media": int(media), "amostras": len(v)}


_INV_BITS = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def crc32_mpeg(dados: bytes) -> int:
    """CRC-32/MPEG-2 (usado nas seções PSI/SI). Calculado com zlib sobre os bits invertidos."""
    import zlib
    c = zlib.crc32(dados.translate(_INV_BITS)) ^ 0xFFFFFFFF
    return int(f"{c:032b}"[::-1], 2)


def _descritores(buf, ini, fim):
    j, out = ini, []
    while j + 2 <= fim:
        tag, n = buf[j], buf[j + 1]
        out.append((tag, buf[j + 2:j + 2 + n]))
        j += 2 + n
    return out


def _texto_dvb(b: bytes) -> str:
    if b and b[0] < 0x20:
        b = b[3:] if b[0] == 0x10 else b[1:]
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        s = b.decode("latin-1")
    return "".join(ch for ch in s if ch >= " ").strip()


# PIDs fixos do ISDB-T/ISDB-Tb (ARIB STD-B10 / ABNT NBR 15603): não são "não referenciados"
PIDS_ISDB = {0x20: "LIT (ISDB)", 0x21: "ERT (ISDB)", 0x22: "PCAT (ISDB)", 0x23: "SDTT (ISDB)",
             0x24: "BIT (ISDB)", 0x25: "NBIT/LDT (ISDB)", 0x26: "EIT (ISDB, M-EIT)", 0x27: "EIT (ISDB, L-EIT)",
             0x28: "SDTT (ISDB)", 0x29: "CDT (ISDB)", 0x2F: "Cabeçalho multiframe (ISDB)",
             0x1FF0: "IIP (ISDB, sincronismo SFN)"}
PIDS_ISDB.update({p: "PMT 1-seg (ISDB)" for p in range(0x1FC8, 0x1FD0)})
# PIDs que vêm no TS mas ficam "fora do pacote" (dados privados do multiplexador): aparecem nas tabelas,
# mas não geram erro (nem "não referenciado", nem continuidade). Lista editável no painel (⚙).
PIDS_FORA_PADRAO = [5000, 5016, 5017, 8184]
# Repetição do PCR (TR 101 290 2.3a). A versão antiga usava 40 ms; desde 2005 o DVB (TS 101 154) e a nota 2 da
# tabela 5.0b da TR 101 290 recomendam o limite de 100 ms da ISO/IEC 13818-1, que é também o do ISDB-Tb.
PCR_REP_OPCOES = (40, 100)
PCR_REP_PADRAO_MS = 100


def aplicar_lim_pcr_texto(ms):
    """Atualiza a descrição do indicador 2.3a (painel e relatórios) para o limite em uso."""
    for i, it in enumerate(TR_ITENS):
        if it[0] == "pcr_rep":
            TR_ITENS[i] = (it[0], it[1], it[2], it[3], f"Intervalo entre PCRs consecutivos maior que {int(ms)} ms")


aplicar_lim_pcr_texto(PCR_REP_PADRAO_MS)


def ler_pids(v):
    """'5000, 0x1398 5017' ou [5000, "0x1399"] -> lista ordenada de PIDs válidos (0 a 8191)."""
    itens = re.split(r"[\s,;]+", v.strip()) if isinstance(v, str) else list(v or [])
    out = set()
    for x in itens:
        if x in ("", None):
            continue
        try:
            n = int(str(x), 0)
        except ValueError:
            raise ValueError(f"PID inválido: {x!r} (use números como 5000 ou 0x1388)")
        if not 0 <= n <= 0x1FFF:
            raise ValueError(f"PID fora da faixa 0 a 8191: {n}")
        out.add(n)
    return sorted(out)
LIMITE_CBR_TS_PCT = 2.0       # TS: taxa total variando menos que isso = CBR
LIMITE_CBR_SERVICO_PCT = 3.0  # serviço: encoder CBR de verdade fica abaixo de ~3% (janelas de 1 s, pelo PCR)


class AnalisadorTS:
    """Analisa o TS recebido (TR 101 290 prioridades 1, 2 e 3, bitrate por PID, serviços e PCR).

    Os métodos recebem o instante (monotônico) como parâmetro para facilitar testes.
    As verificações de prioridade 3 só passam a valer depois que a tabela foi vista ao menos
    uma vez (muitos feeds de contribuição não carregam NIT/SDT/EIT/TDT).
    """
    LIM_PAT = 0.5
    LIM_PMT = 0.5
    LIM_PID = 5.0
    LIM_SINAL = 1.0
    LIM_PTS = 0.7
    LIM_PCR_AUSENTE = 0.5
    LIM_SI = {"nit": 10.0, "sdt": 2.0, "eit": 2.0, "tdt": 30.0}
    PCR_REP = int(0.040 * 27_000_000)
    PCR_DISC = int(0.100 * 27_000_000)
    PCR_ACC_NS = 500
    RECENTE_S = 10.0

    def __init__(self):
        self.pids_ignorados = set()      # PIDs fora do pacote: não geram erro
        self.pcr_rep_ticks = int(PCR_REP_PADRAO_MS / 1000 * 27_000_000)   # limite da repetição do PCR
        self.lock = threading.Lock()
        self.zerar()
        self.reiniciar_estado()
        self._sock = None
        self._parar = threading.Event()
        self.porta = None

    # --- estado -------------------------------------------------------
    def zerar(self):
        self.est_taxa = [0, 0.0, None, None]   # n, soma, mín, máx (taxa ÚTIL por segundo, sem nulos)
        self._semente = False
        self.picos = 0              # segundos com rajada de chegada (rede/SRT segurou e soltou de uma vez)
        self.perda_local = 0        # datagramas descartados pelo sistema antes de chegar ao analisador
        self.cont = {t: 0 for t in TIPOS_TR101290}
        self.ultimo_erro = {}
        self.cc_pid = {}
        self.desde = time.time()

    def reiniciar_estado(self):
        """Esquece o estado do fluxo (em cada (re)início do tsp)."""
        self.cc = {}
        self.visto = {}
        self.ref = {}
        self.reportado = {}
        self.pmt_pids = {}          # pid PMT -> número do programa
        self.es_pids = {}           # pid -> (pid PMT, stream_type, tipo, nome)
        self.programas = {}         # pid PMT -> {"pcr_pid": int, "ca": set}
        self.secoes = {}
        self.em_sync = True
        self.ruins = 0
        self.bons = 0
        self.ultimo_pacote = None
        self.inicio_fluxo = None
        self.fluindo = False
        self.pacotes = 0
        # contagem e taxa por PID
        self.n_pid = {}
        self.n_ant = {}
        self.t_taxa = None
        self.taxa = {}
        self.taxa_total = 0
        self.hist_total = collections.deque(maxlen=300)
        self.hist_util = collections.deque(maxlen=300)   # taxa sem pacotes nulos, a cada 1 s
        self.hist_serv = {}         # número do programa -> deque das taxas por segundo (por chegada)
        # taxa medida pelo relógio do próprio fluxo (PCR): não depende de quando os pacotes chegam
        self.pid_prog = {}          # pid -> número do programa
        self.pcr_prog = {}          # pid de PCR -> número do programa
        self.cnt_prog = {}          # número do programa -> pacotes contados
        self.pcr_cnt = {}           # pid de PCR -> cnt_prog no último PCR
        self.acc_ts = {}            # pid de PCR -> taxas do TS em cada intervalo de PCR (1 s corrente)
        self.acc_prog = {}          # programa -> taxas do programa em cada intervalo de PCR
        self.hist_pcr_ts = collections.deque(maxlen=300)
        self.hist_serv_pcr = {}
        self.emb = {}               # pid -> último pacote embaralhado?
        self.ultimo_scr = None
        # PCR
        self.pcr = {}               # pid -> [pcr, idx, pcr_ancora, idx_ancora, pacotes_por_tick]
        self.pcr_chegada = {}
        self.pcr_janela = {}        # pid -> [intervalo máx (ticks), jitter máx (ns)]
        self.pcr_info = {}          # pid -> {"intervalo_ms", "jitter_ns"}
        self.pts_visto = {}
        # SI
        self.tsid = None
        self.onid = None
        self.rede = None
        self.hora_ts = None
        self.servicos_sdt = {}      # service_id -> (nome, provedor, tipo)
        self.ca_pids = set()
        self.si_visto = {}
        self.cat_visto = None
        self.cat_erro_tid = False
        self.nao_ref = set()

    def _erro(self, tipo, agora):
        self.cont[tipo] += 1
        self.ultimo_erro[tipo] = agora

    def _retomar(self, agora):
        self.cc.clear()
        self.visto.clear()
        self.reportado.clear()
        self.secoes.clear()
        self.pcr.clear()
        self.pcr_chegada.clear()
        self.pts_visto.clear()
        self.ref = {p: agora for p in list(self.pmt_pids) + list(self.es_pids)}
        self.si_visto = {k: agora for k in self.si_visto}
        self.inicio_fluxo = agora

    # --- processamento por pacote --------------------------------------
    def processar(self, d, agora):
        if self.ultimo_pacote is None or agora - self.ultimo_pacote >= self.LIM_SINAL:
            self._retomar(agora)
        self.ultimo_pacote = agora
        self.fluindo = True
        n = len(d) // 188
        idx0 = self.pacotes
        self.pacotes += n
        npid, visto, cc_est, es_pids, pmt_pids = self.n_pid, self.visto, self.cc, self.es_pids, self.pmt_pids
        pid_prog, cnt_prog = self.pid_prog, self.cnt_prog
        for k in range(n):
            i = k * 188
            if d[i] != 0x47:
                self._erro("sync_byte", agora)
                self.ruins += 1
                self.bons = 0
                if self.em_sync and self.ruins >= 2:
                    self.em_sync = False
                    self._erro("sync_loss", agora)
                continue
            self.ruins = 0
            if not self.em_sync:
                self.bons += 1
                if self.bons >= 5:
                    self.em_sync = True
                    self.bons = 0
                continue
            b1 = d[i + 1]
            pid = ((b1 & 0x1F) << 8) | d[i + 2]
            npid[pid] = npid.get(pid, 0) + 1
            visto[pid] = agora
            pp = pid_prog.get(pid)
            if pp is not None:
                cnt_prog[pp] = cnt_prog.get(pp, 0) + 1
            if pid == 0x1FFF:
                continue
            if b1 & 0x80:
                self._erro("transport", agora)
            b3 = d[i + 3]
            afc = (b3 >> 4) & 3
            scr = b3 >> 6
            if scr:
                self.emb[pid] = True
                self.ultimo_scr = agora
            elif pid in self.emb:
                self.emb[pid] = False
            disc = 0
            if afc & 2:
                alen = d[i + 4]
                if alen:
                    flags = d[i + 5]
                    disc = flags & 0x80
                    if flags & 0x10 and alen >= 7:
                        self._pcr(pid, d, i, idx0 + k, disc, agora)
            if afc & 1 and pid not in self.pids_ignorados:
                cc = b3 & 0x0F
                ant = cc_est.get(pid)
                if ant is None or disc:
                    cc_est[pid] = (cc, False)
                elif cc == ant[0]:
                    if ant[1]:
                        self._erro("cc", agora)
                        self.cc_pid[pid] = self.cc_pid.get(pid, 0) + 1
                    cc_est[pid] = (cc, True)
                else:
                    if cc != (ant[0] + 1) & 0x0F:
                        self._erro("cc", agora)
                        self.cc_pid[pid] = self.cc_pid.get(pid, 0) + 1
                    cc_est[pid] = (cc, False)
                if b1 & 0x40 and pid in es_pids:
                    self._pes(pid, d, i, afc, agora)
            if pid < 0x20 or pid in pmt_pids:
                if scr and (pid == 0 or pid in pmt_pids):
                    self._erro("pat" if pid == 0 else "pmt", agora)
                elif afc & 1 and not scr:
                    self._psi(pid, d[i:i + 188], afc, agora)

    def _pcr(self, pid, d, i, idx, disc, agora):
        b0, b1, b2, b3, b4, b5 = d[i + 6:i + 12]
        pcr = ((b0 << 25) | (b1 << 17) | (b2 << 9) | (b3 << 1) | (b4 >> 7)) * 300 + (((b4 & 1) << 8) | b5)
        self.pcr_chegada[pid] = agora
        jan = self.pcr_janela.setdefault(pid, [0, 0.0])
        st = self.pcr.get(pid)
        if st is None or disc:
            self.pcr[pid] = [pcr, idx, pcr, idx, st[4] if st and not disc else None]
            num = self.pcr_prog.get(pid)
            self.pcr_cnt[pid] = self.cnt_prog.get(num, 0) if num is not None else None
            return
        ult, uidx, apcr, aidx, taxa = st
        dif = pcr - ult
        num = self.pcr_prog.get(pid)
        c_agora = self.cnt_prog.get(num, 0) if num is not None else None
        c_ant = self.pcr_cnt.get(pid)
        self.pcr_cnt[pid] = c_agora
        if dif < 0 or dif > self.PCR_DISC:
            self._erro("pcr_disc", agora)
            self.pcr[pid] = [pcr, idx, pcr, idx, None]
            return
        if dif > self.pcr_rep_ticks:
            self._erro("pcr_rep", agora)
        if dif > 0 and idx > uidx:
            dt = dif / 27_000_000
            self.acc_ts.setdefault(pid, []).append((idx - uidx) * 1504 / dt)
            if c_agora is not None and c_ant is not None and c_agora >= c_ant:
                self.acc_prog.setdefault(num, []).append((c_agora - c_ant) * 1504 / dt)
        if dif > jan[0]:
            jan[0] = dif
        if taxa:
            previsto = ult + (idx - uidx) / taxa
            jit = abs(pcr - previsto) * 1000.0 / 27.0
            if jit > jan[1]:
                jan[1] = jit
            if jit > self.PCR_ACC_NS and self.taxa.get(0x1FFF, 0) > 0:  # só em TS CBR
                self._erro("pcr_acc", agora)
        span = pcr - apcr
        if span >= 27_000_000:
            taxa = (idx - aidx) / span
            if span > 27_000_000 * 30:
                apcr, aidx = pcr, idx
        self.pcr[pid] = [pcr, idx, apcr, aidx, taxa]

    def _pes(self, pid, d, i, afc, agora):
        ini = i + 5 + d[i + 4] if afc & 2 else i + 4
        if ini + 9 > i + 188:
            return
        if d[ini] == 0 and d[ini + 1] == 0 and d[ini + 2] == 1:
            sid = d[ini + 3]
            if (0xC0 <= sid <= 0xEF or sid == 0xBD) and d[ini + 7] & 0x80:
                self.pts_visto[pid] = agora

    def _psi(self, pid, p, afc, agora):
        ini = 5 + p[4] if afc & 2 else 4
        if ini >= 188:
            return
        carga = p[ini:]
        if p[1] & 0x40:
            ptr = carga[0]
            anterior = self.secoes.pop(pid, None)
            if anterior is not None and ptr:
                anterior += carga[1:1 + ptr]
                self._extrair(pid, anterior, agora)
            buf = bytearray(carga[1 + ptr:])
        else:
            buf = self.secoes.get(pid)
            if buf is None:
                return
            buf += carga
        resto = self._extrair(pid, buf, agora)
        if resto:
            self.secoes[pid] = resto
        else:
            self.secoes.pop(pid, None)

    def _extrair(self, pid, buf, agora):
        while len(buf) >= 3 and buf[0] != 0xFF:
            tam = (((buf[1] & 0x0F) << 8) | buf[2]) + 3
            if tam > 4096:
                return None
            if len(buf) < tam:
                return buf
            self._tabela(pid, bytes(buf[:tam]), agora)
            buf = buf[tam:]
        return None

    def _tabela(self, pid, sec, agora):
        tid = sec[0]
        if tid == 0x72:          # stuffing table
            return
        if sec[1] & 0x80 and len(sec) >= 12 and crc32_mpeg(sec) != 0:
            self._erro("crc", agora)
            return
        if pid == 0:
            self._pat(sec, agora)
        elif pid == 1:
            if tid != 0x01:
                self._erro("cat", agora)
                return
            self.cat_visto = agora
            for tag, dd in _descritores(sec, 8, len(sec) - 4):
                if tag == 0x09 and len(dd) >= 4:
                    self.ca_pids.add(((dd[2] & 0x1F) << 8) | dd[3])
        elif pid in self.pmt_pids:
            self._pmt(pid, sec, agora)
        elif pid == 0x10:
            if tid == 0x40 and len(sec) >= 12:
                self.si_visto["nit"] = agora
                ndl = ((sec[8] & 0x0F) << 8) | sec[9]
                for tag, dd in _descritores(sec, 10, min(10 + ndl, len(sec) - 4)):
                    if tag == 0x40:
                        self.rede = _texto_dvb(dd)
            elif tid != 0x41:
                self._erro("nit", agora)
        elif pid == 0x11:
            if tid == 0x42:
                self.si_visto["sdt"] = agora
                self._sdt(sec)
            elif tid not in (0x46, 0x4A):
                self._erro("sdt", agora)
        elif pid == 0x12:
            if tid == 0x4E:
                self.si_visto["eit"] = agora
            elif not 0x4F <= tid <= 0x6F:
                self._erro("eit", agora)
        elif pid == 0x14:
            if tid in (0x70, 0x73) and len(sec) >= 8:
                self.si_visto["tdt"] = agora
                self.hora_ts = self._hora_mjd(sec[3:8])
            else:
                self._erro("tdt", agora)

    def _pat(self, sec, agora):
        if sec[0] != 0x00:
            self._erro("pat", agora)
            return
        if len(sec) < 12:
            return
        self.tsid = (sec[3] << 8) | sec[4]
        novos = {}
        for j in range(8, len(sec) - 4 - 3, 4):
            prog = (sec[j] << 8) | sec[j + 1]
            ppid = ((sec[j + 2] & 0x1F) << 8) | sec[j + 3]
            if prog != 0:
                novos[ppid] = prog
        if novos != self.pmt_pids:
            for ppid in novos:
                if ppid not in self.pmt_pids:
                    self.ref[ppid] = agora
            self.pmt_pids = novos
            self.es_pids = {k: v for k, v in self.es_pids.items() if v[0] in novos}
            self.programas = {k: v for k, v in self.programas.items() if k in novos}
            self._mapear_programas()

    def _pmt(self, pid, sec, agora):
        if sec[0] != 0x02:
            self._erro("pmt", agora)
            return
        if len(sec) < 16:
            return
        pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        ca = set()
        for tag, dd in _descritores(sec, 12, min(12 + pil, len(sec) - 4)):
            if tag == 0x09 and len(dd) >= 4:
                ca.add(((dd[2] & 0x1F) << 8) | dd[3])
        j = 12 + pil
        es = {}
        while j + 5 <= len(sec) - 4:
            st = sec[j]
            epid = ((sec[j + 1] & 0x1F) << 8) | sec[j + 2]
            eil = ((sec[j + 3] & 0x0F) << 8) | sec[j + 4]
            nome, tipo = TIPOS_STREAM.get(st, (f"stream_type 0x{st:02X}", "dados"))
            for tag, dd in _descritores(sec, j + 5, min(j + 5 + eil, len(sec) - 4)):
                if tag == 0x09 and len(dd) >= 4:
                    ca.add(((dd[2] & 0x1F) << 8) | dd[3])
                elif st == 0x06 and tag in DESCRITOR_PES_PRIVADO:
                    nome, tipo = DESCRITOR_PES_PRIVADO[tag]
            es[epid] = (pid, st, tipo, nome)
            j += 5 + eil
        for epid in es:
            if epid not in self.es_pids:
                self.ref[epid] = agora
        self.es_pids = {k: v for k, v in self.es_pids.items() if v[0] != pid}
        self.es_pids.update(es)
        self.programas[pid] = {"pcr_pid": pcr_pid, "ca": ca}
        self.ca_pids |= ca
        self._mapear_programas()

    def _mapear_programas(self):
        pp, pcr = {}, {}
        for ppid, num in self.pmt_pids.items():
            pp[ppid] = num
            c = (self.programas.get(ppid) or {}).get("pcr_pid")
            if c is not None and c != 0x1FFF:
                pcr[c] = num
                pp.setdefault(c, num)
        for e, v in self.es_pids.items():
            if v[0] in self.pmt_pids:
                pp[e] = self.pmt_pids[v[0]]
        self.pid_prog.clear()
        self.pid_prog.update(pp)
        self.pcr_prog = pcr

    def _sdt(self, sec):
        if len(sec) < 15:
            return
        self.onid = (sec[8] << 8) | sec[9]
        j = 11
        while j + 5 <= len(sec) - 4:
            sid = (sec[j] << 8) | sec[j + 1]
            dll = ((sec[j + 3] & 0x0F) << 8) | sec[j + 4]
            for tag, dd in _descritores(sec, j + 5, min(j + 5 + dll, len(sec) - 4)):
                if tag == 0x48 and len(dd) >= 3:
                    tipo = dd[0]
                    lp = dd[1]
                    prov = _texto_dvb(dd[2:2 + lp])
                    ln = dd[2 + lp] if len(dd) > 2 + lp else 0
                    nome = _texto_dvb(dd[3 + lp:3 + lp + ln])
                    self.servicos_sdt[sid] = (nome, prov, tipo)
            j += 5 + dll

    @staticmethod
    def _hora_mjd(b):
        import datetime
        try:
            mjd = (b[0] << 8) | b[1]
            dia = datetime.date(1858, 11, 17) + datetime.timedelta(days=mjd)
            bcd = [((x >> 4) * 10 + (x & 0x0F)) for x in b[2:5]]
            return f"{dia.isoformat()} {bcd[0]:02d}:{bcd[1]:02d}:{bcd[2]:02d}"
        except (ValueError, OverflowError):
            return None

    # --- verificações por tempo ---------------------------------------
    def verificar(self, agora):
        """Checagens por tempo e cálculo de taxas (chamar algumas vezes por segundo)."""
        if self.t_taxa is None:
            self.t_taxa, self.n_ant = agora, dict(self.n_pid)
        elif agora - self.t_taxa >= 1.0:
            dt = agora - self.t_taxa
            self.taxa = {p: (n - self.n_ant.get(p, 0)) * 1504 / dt for p, n in self.n_pid.items()}
            self.taxa_total = sum(self.taxa.values())
            self.hist_total.append(int(self.taxa_total))
            if self.acc_ts:
                ref = max(self.acc_ts, key=lambda p: len(self.acc_ts[p]))
                v = self.acc_ts[ref]
                self.hist_pcr_ts.append(int(sum(v) / len(v)))
            for num, v in self.acc_prog.items():
                self.hist_serv_pcr.setdefault(num, collections.deque(maxlen=60)).append(int(sum(v) / len(v)))
            self.acc_ts, self.acc_prog = {}, {}
            for ppid, num in self.pmt_pids.items():
                pids = {ppid} | {e for e, v in self.es_pids.items() if v[0] == ppid}
                pcr = (self.programas.get(ppid) or {}).get("pcr_pid")
                if pcr is not None and pcr != 0x1FFF:
                    pids.add(pcr)
                self.hist_serv.setdefault(num, collections.deque(maxlen=60)).append(
                    int(sum(self.taxa.get(p, 0) for p in pids)))
            util = self.taxa_total - self.taxa.get(0x1FFF, 0)
            self.hist_util.append(int(util))
            fluindo = self.ultimo_pacote is not None and agora - self.ultimo_pacote < self.LIM_SINAL
            if util > 0 and fluindo:
                e = self.est_taxa
                e[0] += 1
                e[1] += util
                # Mín./máx. pela média de 5 s, ignorando os 3 s após o sinal voltar: assim uma rajada
                # (pacotes retidos pela rede e entregues de uma vez) não vira "máximo de 60 Mb/s".
                jan = list(self.hist_util)[-5:]
                if len(jan) == 5 and all(jan) and self.inicio_fluxo is not None and agora - self.inicio_fluxo >= 8:
                    m5 = sum(jan) / 5
                    if e[2] is None or self._semente:
                        e[2] = e[3] = m5
                        self._semente = False
                    else:
                        e[2], e[3] = min(e[2], m5), max(e[3], m5)
                    ant = sorted(list(self.hist_util)[-11:-1])
                    if len(ant) >= 5 and util > 1e6 and util > 2.5 * ant[len(ant) // 2]:
                        self.picos += 1
                elif e[2] is None and self.inicio_fluxo is not None and agora - self.inicio_fluxo >= 3:
                    e[2] = e[3] = util   # sessão curta: valor provisório até ter a média de 5 s
                    self._semente = True
            self.pcr_info = {p: {"intervalo_ms": round(j[0] / 27000, 2), "jitter_ns": int(j[1])}
                             for p, j in self.pcr_janela.items()}
            self.pcr_janela = {}
            self.n_ant, self.t_taxa = dict(self.n_pid), agora

        if self.ultimo_pacote is None:
            return
        fluindo = agora - self.ultimo_pacote < self.LIM_SINAL
        if self.fluindo and not fluindo:
            self._erro("sync_loss", agora)
            self.taxa = {}
            self.taxa_total = 0
        self.fluindo = fluindo
        if not fluindo:
            return

        def ausente(chave, lim, tipo, desde):
            ult = self.visto.get(chave, desde) if isinstance(chave, int) else desde
            if agora - ult > lim and agora - self.reportado.get((tipo, chave), -1e9) > lim:
                self._erro(tipo, agora)
                self.reportado[(tipo, chave)] = agora

        ausente(0, self.LIM_PAT, "pat", self.inicio_fluxo)
        for ppid in self.pmt_pids:
            ausente(ppid, self.LIM_PMT, "pmt", self.ref.get(ppid, agora))
        for epid, (_, _, tipo, _) in self.es_pids.items():
            ausente(epid, self.LIM_PID, "pid", self.ref.get(epid, agora))
            if tipo in ("video", "audio") and epid in self.pts_visto:
                ausente(f"pts{epid}", self.LIM_PTS, "pts", self.pts_visto[epid])
        for prog in self.programas.values():
            pp = prog["pcr_pid"]
            if pp != 0x1FFF and pp in self.pcr_chegada:
                ausente(f"pcr{pp}", self.LIM_PCR_AUSENTE, "pcr_rep", self.pcr_chegada[pp])
        if self.ultimo_scr and agora - self.ultimo_scr < 1.0 and \
                (self.cat_visto is None or agora - self.cat_visto > 10.0):
            ausente("cat", 1.0, "cat", -1e9)
        for chave, lim in self.LIM_SI.items():
            if chave in self.si_visto:
                ausente(f"si_{chave}", lim, chave, self.si_visto[chave])
        if agora - self.inicio_fluxo > 2.0 and 0 in self.visto:
            refs = self._referenciados()
            for p, t in self.taxa.items():
                if t > 0 and p not in refs and p not in self.nao_ref:
                    self.nao_ref.add(p)
                    self._erro("unref", agora)

    def _referenciados(self):
        refs = set(range(0x20)) | set(PIDS_ISDB) | self.pids_ignorados | {0x1FFF} | set(self.pmt_pids) | set(self.es_pids) | self.ca_pids
        refs |= {p["pcr_pid"] for p in self.programas.values()}
        return refs

    # --- consultas ----------------------------------------------------
    def status(self, agora=None):
        agora = time.monotonic() if agora is None else agora
        with self.lock:
            fluindo = self.ultimo_pacote is not None and agora - self.ultimo_pacote < self.LIM_SINAL
            ausentes = sorted(p for p in self.es_pids
                              if agora - self.visto.get(p, self.ref.get(p, agora)) > self.LIM_PID)
            return {
                "contadores": dict(self.cont),
                "recentes": [t for t, q in self.ultimo_erro.items() if agora - q < self.RECENTE_S],
                "sinal": fluindo,
                "em_sync": self.em_sync,
                "programas": len(self.pmt_pids),
                "pids_referenciados": len(self.es_pids),
                "pids_ausentes": [f"0x{p:04X}" for p in ausentes],
                "pacotes": self.pacotes,
                "desde": self.desde,
                "nao_aplicavel": self._nao_aplicavel(agora),
            }

    def _nao_aplicavel(self, agora):
        """Indicadores que não se aplicam a este TS (mesma regra da análise completa)."""
        na = set()
        pcr_pids = {p["pcr_pid"] for p in self.programas.values()}
        if self.taxa.get(0x1FFF, 0) <= 0 or not pcr_pids:
            na.add("pcr_acc")
        if not pcr_pids:
            na |= {"pcr_rep", "pcr_disc"}
        if not any(v[2] in ("video", "audio") for v in self.es_pids.values()):
            na.add("pts")
        if self.ultimo_scr is None and self.cat_visto is None and not self.cont["cat"]:
            na.add("cat")
        for chave in self.LIM_SI:
            if chave not in self.si_visto and not self.cont[chave]:
                na.add(chave)
        recentes = {t for t, q in self.ultimo_erro.items() if agora - q < self.RECENTE_S}
        return sorted(na - recentes)

    def servicos_resumo(self):
        """[{numero, nome, video, taxa}] dos serviços presentes na entrada (PAT/PMT/SDT)."""
        with self.lock:
            fluindo = self.ultimo_pacote is not None and time.monotonic() - self.ultimo_pacote < self.LIM_SINAL
            out = []
            for ppid, num in sorted(self.pmt_pids.items(), key=lambda x: x[1]):
                sdt = self.servicos_sdt.get(num)
                pids = {ppid} | {e for e, v in self.es_pids.items() if v[0] == ppid}
                pcr = (self.programas.get(ppid) or {}).get("pcr_pid")
                if pcr is not None and pcr != 0x1FFF:
                    pids.add(pcr)
                out.append({"numero": num, "nome": sdt[0] if sdt and sdt[0] else f"Programa {num}",
                            "video": any(v[0] == ppid and v[2] == "video" for v in self.es_pids.values()),
                            "taxa": int(sum(self.taxa.get(p, 0) for p in pids)) if fluindo else 0,
                            "modo": self._modo_serv(num)})
            return out

    def _modo_ts(self):
        if len(self.hist_pcr_ts) >= 8:
            return {**classificar_taxa(self.hist_pcr_ts, LIMITE_CBR_TS_PCT), "fonte": "PCR"}
        return {**classificar_taxa(self.hist_total, LIMITE_CBR_TS_PCT), "fonte": "chegada"}

    def _modo_serv(self, num):
        h = self.hist_serv_pcr.get(num)
        if h and len(h) >= 8:
            return {**classificar_taxa(h, LIMITE_CBR_SERVICO_PCT), "fonte": "PCR"}
        return {**classificar_taxa(self.hist_serv.get(num, ()), LIMITE_CBR_SERVICO_PCT), "fonte": "chegada"}

    def taxas(self):
        """Taxa total do TS e taxa útil (sem pacotes nulos), em bit/s, medidas a cada 1 s, e se é CBR ou VBR."""
        with self.lock:
            fluindo = self.ultimo_pacote is not None and time.monotonic() - self.ultimo_pacote < self.LIM_SINAL
            modo = self._modo_ts()
            if not fluindo:
                return {"taxa_ts": 0, "taxa_util": 0, "taxa_nulos": 0, "modo_ts": modo}
            nulos = int(self.taxa.get(0x1FFF, 0))
            return {"taxa_ts": int(self.taxa_total), "taxa_util": int(self.taxa_total) - nulos, "taxa_nulos": nulos,
                    "modo_ts": modo}

    def _desc_pid(self, pid):
        fixos = {0: "PAT", 1: "CAT", 2: "TSDT", 0x10: "NIT", 0x11: "SDT/BAT", 0x12: "EIT",
                 0x13: "RST", 0x14: "TDT/TOT", 0x1FFF: "Pacotes nulos"}
        if pid in self.pmt_pids:
            return f"PMT (programa {self.pmt_pids[pid]})", "psi"
        if pid in self.es_pids:
            return self.es_pids[pid][3], self.es_pids[pid][2]
        if pid in fixos:
            return fixos[pid], ("nulo" if pid == 0x1FFF else "psi")
        if pid in self.ca_pids:
            return "ECM/EMM (acesso condicional)", "ca"
        if pid in {p["pcr_pid"] for p in self.programas.values()}:
            return "PCR", "pcr"
        if pid in PIDS_ISDB:
            return PIDS_ISDB[pid], "psi"
        if pid in self.pids_ignorados:
            return "Fora do pacote (ignorado)", "ignorado"
        if pid < 0x20:
            return "SI reservado", "psi"
        return "Não referenciado", "desconhecido"

    def taxa_sessao(self):
        with self.lock:
            n, soma, mn, mx = self.est_taxa
            picos, perda = self.picos, self.perda_local
        return {"media": int(soma / n) if n else 0, "min": int(mn or 0), "max": int(mx or 0), "segundos_com_sinal": n,
                "rajadas": picos, "perda_interna": perda}

    def relatorio(self, agora_mono=None, agora_wall=None):
        """Análise congelada para guardar: horários absolutos no lugar de 'há N s'."""
        agora_mono = time.monotonic() if agora_mono is None else agora_mono
        agora_wall = time.time() if agora_wall is None else agora_wall
        a = self.analise(agora_mono)
        tr = a["tr101290"]
        with self.lock:
            tr["ultimo_em"] = {k: agora_wall - (agora_mono - v) for k, v in self.ultimo_erro.items()}
            n, soma, mn, mx = self.est_taxa
            picos, perda = self.picos, self.perda_local
        tr.pop("ha_s", None)
        tr["recentes"] = []
        a["resumo"]["sinal"] = False
        a["taxa_sessao"] = {"media": int(soma / n) if n else 0, "min": int(mn or 0), "max": int(mx or 0),
                            "segundos_com_sinal": n, "rajadas": picos, "perda_interna": perda}
        return a

    def analise(self, agora=None):
        agora = time.monotonic() if agora is None else agora
        with self.lock:
            fluindo = self.ultimo_pacote is not None and agora - self.ultimo_pacote < self.LIM_SINAL
            total = self.taxa_total if fluindo else 0
            prog_de_pid = {}
            for ppid, num in self.pmt_pids.items():
                prog_de_pid[ppid] = num
            for epid, v in self.es_pids.items():
                prog_de_pid[epid] = self.pmt_pids.get(v[0])
            for ppid, pr in self.programas.items():
                if pr["pcr_pid"] != 0x1FFF:      # 0x1FFF = sem PCR: os nulos não pertencem ao serviço
                    prog_de_pid.setdefault(pr["pcr_pid"], self.pmt_pids.get(ppid))

            def nome_serv(num):
                if num is None:
                    return ""
                s = self.servicos_sdt.get(num)
                return s[0] if s and s[0] else f"Programa {num}"

            pcr_pids = {p["pcr_pid"] for p in self.programas.values()}
            pids = []
            for pid in sorted(set(self.n_pid) | set(self.es_pids) | set(self.pmt_pids)):
                desc, tipo = self._desc_pid(pid)
                t = self.taxa.get(pid, 0) if fluindo else 0
                pids.append({
                    "pid": pid, "hex": f"0x{pid:04X}", "desc": desc, "tipo": tipo,
                    "servico": nome_serv(prog_de_pid.get(pid)),
                    "taxa": int(t), "pct": round(100 * t / total, 2) if total else 0,
                    "pacotes": self.n_pid.get(pid, 0), "cc": self.cc_pid.get(pid, 0),
                    "emb": bool(self.emb.get(pid)), "pcr": pid in pcr_pids,
                    "ausente": pid not in self.n_pid or (fluindo and agora - self.visto.get(pid, 0) > self.LIM_PID),
                })
            servicos = []
            for ppid, num in sorted(self.pmt_pids.items(), key=lambda x: x[1]):
                pr = self.programas.get(ppid, {"pcr_pid": None, "ca": set()})
                sdt = self.servicos_sdt.get(num, ("", "", None))
                comps = [{"pid": e, "hex": f"0x{e:04X}", "desc": v[3], "tipo": v[2],
                          "stream_type": f"0x{v[1]:02X}", "taxa": int(self.taxa.get(e, 0)) if fluindo else 0,
                          "emb": bool(self.emb.get(e))}
                         for e, v in sorted(self.es_pids.items()) if v[0] == ppid]
                pcr_pid = pr["pcr_pid"]
                tx = sum(c["taxa"] for c in comps) + (int(self.taxa.get(ppid, 0)) if fluindo else 0)
                # PCR_PID 0x1FFF = "serviço sem PCR" (comum em rádio no ISDB-Tb): não somar os pacotes nulos
                if pcr_pid is not None and pcr_pid != 0x1FFF and pcr_pid not in self.es_pids:
                    tx += int(self.taxa.get(pcr_pid, 0)) if fluindo else 0
                servicos.append({
                    "modo": self._modo_serv(num),
                    "numero": num, "nome": sdt[0] or f"Programa {num}", "provedor": sdt[1],
                    "tipo": TIPOS_SERVICO.get(sdt[2], f"0x{sdt[2]:02X}" if sdt[2] is not None else ""),
                    "pmt": f"0x{ppid:04X}", "pcr_pid": f"0x{pcr_pid:04X}" if pcr_pid not in (None, 0x1FFF) else "",
                    "pcr": self.pcr_info.get(pcr_pid) if pcr_pid is not None else None,
                    "embaralhado": bool(pr["ca"]) or any(c["emb"] for c in comps),
                    "taxa": tx, "componentes": comps,
                })
            na = []
            if self.taxa.get(0x1FFF, 0) <= 0:
                na.append("pcr_acc")
            if not pcr_pids:
                na += ["pcr_rep", "pcr_disc", "pcr_acc"]
            if not any(v[2] in ("video", "audio") for v in self.es_pids.values()):
                na.append("pts")
            if self.ultimo_scr is None and self.cat_visto is None and not self.cont["cat"]:
                na.append("cat")
            for chave in self.LIM_SI:
                if chave not in self.si_visto and not self.cont[chave]:
                    na.append(chave)
            return {
                "resumo": {
                    "sinal": fluindo, "em_sync": self.em_sync, "taxa_total": int(total),
                    "taxa_util": int(total - (self.taxa.get(0x1FFF, 0) if fluindo else 0)),
                    "tsid": self.tsid, "onid": self.onid, "rede": self.rede, "hora_ts": self.hora_ts,
                    "n_pids": len(self.n_pid), "n_servicos": len(self.pmt_pids),
                    "cbr": self.taxa.get(0x1FFF, 0) > 0, "modo_taxa": self._modo_ts(),
                    "pacotes": self.pacotes, "desde": self.desde,
                },
                "tr101290": {
                    "contadores": dict(self.cont),
                    "ha_s": {t: round(agora - q, 1) for t, q in self.ultimo_erro.items()},
                    "recentes": [t for t, q in self.ultimo_erro.items() if agora - q < self.RECENTE_S],
                    "nao_aplicavel": sorted(set(na) - {t for t, q in self.ultimo_erro.items()
                                                       if agora - q < self.RECENTE_S}),
                },
                "pids": pids,
                "servicos": servicos,
                "historico": list(self.hist_util),
                "historico_ts": list(self.hist_total),
            }

    # --- recepção pela rede local -------------------------------------
    def abrir(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_RCVBUFFORCE", socket.SO_RCVBUF), BUFFER_UDP)
        except OSError:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_UDP)
            except OSError:
                pass
        s.bind(("127.0.0.1", 0))
        s.settimeout(0.25)
        self._sock = s
        self.porta = s.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True, name="tr101290").start()
        return self.porta

    def _loop(self):
        ultima = 0.0
        falhas = 0
        while not self._parar.is_set():
            agora = time.monotonic()
            try:
                dados = self._sock.recv(65536)
                agora = time.monotonic()
                with self.lock:
                    self.processar(dados, agora)
            except OSError:
                agora = time.monotonic()
            except Exception:   # pacote/tabela malformada nunca pode parar a análise
                falhas += 1
                if falhas <= 5 or falhas % 1000 == 0:
                    log.exception("analisador: erro ao processar pacote (%d)", falhas)
            if agora - ultima >= 0.25:
                ultima = agora
                try:
                    with self.lock:
                        self.verificar(agora)
                except Exception:
                    log.exception("analisador: erro na verificação periódica")

    def fechar(self):
        self._parar.set()
        if self._sock:
            self._sock.close()


# ======================================================================
# Estatísticas de conexões SRT (lidas do JSON que o TSDuck imprime a cada 1 s)
# ======================================================================
RE_SRT_ESTAT = re.compile(r"(SRTIN|SRTOUT\d+):(\{.*\})\s*$")
RE_SRT_PEER = re.compile(r"srt: connected from (\S+)")


class EstatSRT:
    TIMEOUT = 3.0   # sem estatística por mais que isso = desconectado

    def __init__(self, papel, cfg, rotulo):
        self.papel = papel                 # "entrada" ou "saida"
        self.rotulo = rotulo
        self.modo = cfg.get("modo", "listener" if papel == "entrada" else "caller")
        self.endereco = str(cfg.get("endereco", ""))
        self.peer = None
        self.ultimo = None
        self.atual = {}
        self.hist = collections.deque(maxlen=120)   # [taxa Mb/s, perda %, RTT ms]
        self.tot = {"pacotes": 0, "perdidos": 0, "retransmitidos": 0, "descartados": 0}
        self.conexoes = 0
        self.conectado_desde = None
        self.rtt = [0.0, 0, 0.0]                    # soma, n, máx

    def atualizar(self, j, agora):
        rx = self.papel == "entrada"
        bloco = j.get("receive" if rx else "send", {})
        iv, ins = bloco.get("interval", {}), bloco.get("instant", {})
        n = lambda d, k: float(d.get(k) or 0)  # noqa: E731
        taxa = n(iv, "rate-mbps" if rx else "send-rate-mbps")
        pac, perd = n(iv, "packets"), n(iv, "lost-packets")
        retr = n(iv, "retransmitted-packets" if rx else "retransmit-packets")
        desc = n(iv, "dropped-packets")
        rtt = n(j.get("global", {}).get("instant", {}), "rtt-ms")
        if self.ultimo is None or agora - self.ultimo > self.TIMEOUT:
            self.conexoes += 1
            self.conectado_desde = agora
        self.ultimo = agora
        perda_pct = 100.0 * perd / (pac + perd) if pac + perd else 0.0
        for k, v in (("pacotes", pac), ("perdidos", perd), ("retransmitidos", retr), ("descartados", desc)):
            self.tot[k] += int(v)
        if rtt > 0:
            self.rtt[0] += rtt
            self.rtt[1] += 1
            self.rtt[2] = max(self.rtt[2], rtt)
        self.atual = {
            "taxa_mbps": round(taxa, 3), "perda_pct": round(perda_pct, 3), "perdidos": int(perd),
            "retransmitidos": int(retr), "descartados": int(desc), "rtt_ms": round(rtt, 2),
            "buffer_ms": int(n(ins, "buffer-ack-ms" if rx else "snd-buffer-ms")),
            "latencia_ms": int(n(ins, "delivery-delay-ms")),
            "banda_mbps": round(n(j.get("send", {}).get("instant", {}), "estimated-link-bandwidth-mbps"), 1),
        }
        self.hist.append([round(taxa, 3), round(perda_pct, 3), round(rtt, 1)])

    def status(self, agora=None, rodando=True):
        agora = time.monotonic() if agora is None else agora
        conectado = self.ultimo is not None and agora - self.ultimo <= self.TIMEOUT
        estado = "conectado" if conectado else ("aguardando" if rodando else "parado")
        return {
            "rotulo": self.rotulo, "papel": self.papel, "modo": self.modo, "endereco": self.endereco,
            "peer": self.peer if conectado else None, "estado": estado,
            "conectado_s": int(agora - self.conectado_desde) if conectado and self.conectado_desde else 0,
            "conexoes": self.conexoes, **(self.atual if conectado else {}), "totais": dict(self.tot),
            "rtt_medio_ms": round(self.rtt[0] / self.rtt[1], 2) if self.rtt[1] else 0,
            "rtt_max_ms": round(self.rtt[2], 2), "hist": list(self.hist),
        }


# ======================================================================
# Saídas independentes e distribuidor local
# ======================================================================
RE_SRT_PEER_SAIDA = re.compile(r"srt: connected to (\S+)")
# mensagens informativas do TSDuck que não indicam problema
RE_TSP_AVISO_NORMAL = re.compile(r"unknown initial input bitrate|srt: connected (from|to)|^\s*\*\s*srt: starting|"
                                 r"no input bitrate|bitrate is now|user interrupt")
# avisos normais do ffmpeg ao entrar no meio de um fluxo ao vivo (não são falhas)
RE_FFMPEG_AVISO_NORMAL = re.compile(r"no frame!|non-existing PPS|decode_slice_header error|"
                                    r"Last message repeated|Packet corrupt|PES packet size mismatch|"
                                    r"Invalid timestamps|co located POCs unavailable|mmco: unref short failure|"
                                    r"timestamp discontinuity|Non-monotonic DTS|Queue input is backward")


def _porta_udp_livre():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


class ProcSaida(threading.Thread):
    """Uma saída do canal rodando no próprio processo tsp, com reinício independente."""

    def __init__(self, canal, indice, cfg):
        super().__init__(daemon=True, name=f"{canal.name}-saida{indice + 1}")
        self.canal = canal
        self.indice = indice
        self.cfg = cfg
        self.chave = f"SRTOUT{indice}"
        self.porta = _porta_udp_livre()
        self.fo = FailoverNic(cfg, f"Saída {indice + 1}", **canal._fo_args) \
            if cfg.get("interface_reserva") and cfg.get("tipo") != "rtmp" else None
        self._troca = False
        self._sem_desde = None
        self.cmd = montar_saida(self.fo.aplicar(cfg) if self.fo else cfg, indice, canal.sup.get("tsp", "tsp"), self.porta)
        self.proc = None
        self.parar = threading.Event()
        self.st = {"estado": "iniciando", "ultima_msg": "", "reinicios": 0, "desde": None, "info": {}}
        self.atividade = time.monotonic()
        self._prog = {}
        self.hist = collections.deque(maxlen=150)      # taxa de saída (kb/s), um ponto a cada ~2 s
        self.enviado = 0                                # bytes enviados na sessão (todas as conexões)
        self._enviado_conexao = 0                       # bytes da conexão atual
        self._ultima_taxa = None
        self.conexoes = 0
        self.tempo_no_ar = 0.0                          # soma do tempo no ar de todas as conexões

    def _no_ar(self):
        """Marca a saída como no ar (conta uma conexão nova se estava fora)."""
        if self.st["estado"] != "rodando":
            self.conexoes += 1
            self._enviado_conexao = 0
            self.st.update(estado="rodando", desde=time.time())

    def _fora_do_ar(self, estado):
        if self.st["estado"] == "rodando" and self.st["desde"]:
            self.tempo_no_ar += time.time() - self.st["desde"]
        self.st.update(estado=estado, desde=None)
        self._ultima_taxa = None

    def _taxa(self, kbps, agora=None):
        agora = time.monotonic() if agora is None else agora
        if self._ultima_taxa is not None:
            dt = min(agora - self._ultima_taxa[0], 10.0)
            b = (self._ultima_taxa[1] + kbps) / 2 * 1000 / 8 * dt
            self.enviado += b
            self._enviado_conexao += b
        self._ultima_taxa = (agora, kbps)
        self.st["info"] = {**(self.st.get("info") or {}), "taxa_kbps": round(kbps, 1)}
        self.hist.append(round(kbps, 1))

    def status(self):
        s = dict(self.st)
        s["info"] = dict(self.st.get("info") or {})
        no_ar = s["estado"] == "rodando" and s["desde"]
        if no_ar:
            s["uptime_s"] = int(time.time() - s["desde"])
        s["enviado_bytes"] = int(self.enviado)
        s["enviado_conexao_bytes"] = int(self._enviado_conexao)
        s["conexoes"] = self.conexoes
        s["tempo_no_ar_s"] = int(self.tempo_no_ar + (time.time() - s["desde"] if no_ar else 0))
        s["hist"] = list(self.hist)
        return s

    def trocar_nic(self, para, motivo, agora):
        n = self.indice + 1
        try:
            de = self.fo.trocar(para, motivo, agora)
            self.cmd = montar_saida(self.fo.aplicar(self.cfg), self.indice, self.canal.sup.get("tsp", "tsp"), self.porta)
        except ValueError as e:
            self.canal._registrar(logging.ERROR, f"[saída {n}] rede: não foi possível usar a placa {para}: {e}")
            return
        self.canal._registrar(logging.WARNING, f"[saída {n}] rede: passou da placa {de} para {para} ({motivo})")
        self.st["ultima_msg"] = f"rede: saída na placa {para} ({motivo})"
        self.canal._rotas_por_placa()
        self._troca = True
        self._sem_desde = None
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _ler(self, stream):
        est = self.canal.srt.get(self.chave)
        for linha in iter(stream.readline, ""):
            self.atividade = time.monotonic()
            linha = linha.rstrip()
            if not linha:
                continue
            ms = RE_SRT_ESTAT.search(linha)
            if ms:
                if est and ms.group(1) == self.chave:
                    try:
                        dados = json.loads(ms.group(2))
                        with self.canal.lock:
                            est.atualizar(dados, time.monotonic())
                        self._no_ar()
                    except ValueError:
                        pass
                continue
            mb = None if RE_ALARME.search(linha) else RE_BITRATE.search(linha)
            if mb:   # bitrate_monitor deste processo: taxa realmente entregue à rede
                self._taxa(float(mb.group(1).replace(",", "")) / 1000)
                if self.cfg.get("tipo") in ("udp", "rtp"):
                    self._no_ar()
                continue
            if self.cfg.get("tipo") == "rtmp" and re.match(r"^[a-z_0-9]+=", linha):
                k, _, v = linha.partition("=")   # progresso do ffmpeg (-progress pipe:1)
                self._prog[k] = v.strip()
                if k == "progress":
                    br = re.match(r"([\d.]+)kbits/s", self._prog.get("bitrate", ""))
                    total = int(self._prog.get("total_size", 0) or 0) if str(self._prog.get("total_size", "")).isdigit() else 0
                    self._no_ar()
                    if total:   # o ffmpeg informa o total exato da conexão atual
                        self.enviado += max(0, total - self._enviado_conexao)
                        self._enviado_conexao = total
                    self.st["info"] = {"taxa_kbps": float(br.group(1)) if br else 0,
                                       "velocidade": self._prog.get("speed", "").strip()}
                    if br:
                        self.hist.append(round(float(br.group(1)), 1))
                continue
            mp = RE_SRT_PEER_SAIDA.search(linha)
            if mp and est:
                est.peer = mp.group(1)
                self._no_ar()
            linha = mascarar(linha)
            if RE_FFMPEG_AVISO_NORMAL.search(linha):
                log.debug("[%s] [saída %d] %s", self.canal.name, self.indice + 1, linha)
                continue
            self.canal._registrar(logging.INFO, f"[saída {self.indice + 1}] {linha}")
            self.st["ultima_msg"] = linha[-300:]

    def run(self):
        espera_min = self.canal.sup.get("reinicio_min_s", 2)
        espera_max = self.canal.sup.get("reinicio_max_s", 60)
        espera = espera_min
        while not self.parar.is_set():
            self.canal._registrar(logging.INFO, f"[saída {self.indice + 1}] iniciando: {comando_para_log(self.cmd)}")
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             text=True, bufsize=1)
            except FileNotFoundError:
                self.st.update(estado="erro", ultima_msg=f"{self.cmd[0]} não encontrado"
                               + (" (sudo apt install ffmpeg)" if self.cmd[0] == "ffmpeg" else ""))
                self.parar.wait(espera_max)
                continue
            # UDP/RTP não têm "conexão": ficam no ar quando a primeira taxa é medida
            self.atividade = time.monotonic()
            self.st.update(estado="conectando", desde=None, info={})
            self._ler(self.proc.stdout)
            rc = self.proc.wait()
            if self.parar.is_set():
                break
            if self._troca:            # troca de placa de rede: reinicia na hora, sem contar como queda
                self._troca = False
                espera = espera_min
                self._fora_do_ar("conectando")
                continue
            if time.time() - t0 > 60:
                espera = espera_min
            self.st["reinicios"] += 1
            self._fora_do_ar("reiniciando")
            self.canal._registrar(logging.ERROR, f"[saída {self.indice + 1}] {os.path.basename(self.cmd[0])} saiu (código {rc}). "
                                  f"Tentando de novo em {espera}s.")
            self.parar.wait(espera)
            espera = min(espera * 2, espera_max)
        self._fora_do_ar("parado")

    def encerrar(self):
        self.parar.set()
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


_FFMPEG_RECURSOS = None


def _prioridade_baixa():
    """A janela de imagem é acessória: roda com prioridade baixa para nunca atrasar o fluxo."""
    try:
        os.nice(15)
    except OSError:
        pass


def recursos_ffmpeg():
    """Detecta (uma vez) se há ffmpeg e quais opções a versão instalada aceita."""
    global _FFMPEG_RECURSOS
    if _FFMPEG_RECURSOS is None:
        ff = shutil.which("ffmpeg")
        r = {"ffmpeg": ff, "fps_mode": False, "atomic": False}
        if ff:
            try:
                h = subprocess.run([ff, "-hide_banner", "-h", "full"], capture_output=True, text=True, timeout=15).stdout
                r["fps_mode"] = "fps_mode" in h
                m = subprocess.run([ff, "-hide_banner", "-h", "muxer=image2"], capture_output=True, text=True,
                                   timeout=15).stdout
                r["atomic"] = "atomic_writing" in m
            except (OSError, subprocess.SubprocessError):
                pass
        _FFMPEG_RECURSOS = r
    return _FFMPEG_RECURSOS


class Previa(threading.Thread):
    """Janela de imagem: miniatura de cada serviço com vídeo, gerada pelo ffmpeg decodificando
    só os quadros-chave. Roda só enquanto alguém está olhando (pedidos nos últimos 30 s)."""
    OCIOSO_S = 30
    VALIDADE_S = 20

    def __init__(self, canal):
        super().__init__(daemon=True, name=f"{canal.name}-previa")
        self.canal = canal
        self.porta = _porta_udp_livre()
        self.pasta = tempfile.mkdtemp(prefix="asi2srt-previa-")
        self.ultimo_pedido = 0.0
        self.proc = None
        self.servs = ()
        self.parar = threading.Event()
        self.motivo = ""
        self._proxima = 0.0

    def pedir(self):
        self.ultimo_pedido = time.monotonic()

    def comando(self, servs):
        r = recursos_ffmpeg()
        cmd = [r["ffmpeg"] or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-skip_frame", "nokey", "-fflags", "+discardcorrupt", "-analyzeduration", "4000000",
               "-probesize", "8000000",
               "-i", f"udp://127.0.0.1:{self.porta}?fifo_size=1000000&overrun_nonfatal=1&timeout=10000000"]
        for sid in servs:
            cmd += ["-map", f"0:p:{sid}:v:0", "-an", "-sn", "-dn", "-vf", "scale=480:-2", "-q:v", "5"]
            cmd += ["-fps_mode", "vfr"] if r["fps_mode"] else ["-vsync", "vfr"]
            if r["atomic"]:
                cmd += ["-atomic_writing", "1"]
            cmd += ["-update", "1", "-y", os.path.join(self.pasta, f"{sid}.jpg")]
        return cmd

    def _parar_ffmpeg(self):
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def run(self):
        while not self.parar.wait(1):
            agora = time.monotonic()
            if agora - self.ultimo_pedido > self.OCIOSO_S:
                if self.proc:
                    self._parar_ffmpeg()
                continue
            if not recursos_ffmpeg()["ffmpeg"]:
                self.motivo = "ffmpeg não instalado (sudo apt install ffmpeg)"
                continue
            servs = tuple(s["numero"] for s in self.canal.analisador.servicos_resumo() if s["video"])
            if not servs:
                self.motivo = "aguardando serviços com vídeo na entrada"
                continue
            vivo = self.proc is not None and self.proc.poll() is None
            if vivo and servs == self.servs:
                continue
            if self.proc is not None and not vivo:   # caiu: espera um pouco antes de tentar de novo
                err = [x for x in getattr(self, "_err", ()) if x.strip()]
                self.motivo = mascarar(err[-1][:200]) if err else "ffmpeg encerrou"
                self.proc = None
                self._proxima = agora + 5
            if agora < self._proxima:
                continue
            self._parar_ffmpeg()
            self.servs = servs
            try:
                self.proc = subprocess.Popen(self.comando(servs), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                             preexec_fn=_prioridade_baixa)
                self._err = collections.deque(maxlen=5)
                threading.Thread(target=self._ler_err, args=(self.proc, self._err), daemon=True).start()
                self.motivo = "gerando imagem…"
            except OSError as e:
                self.motivo = str(e)
                self._proxima = agora + 30
        self._parar_ffmpeg()
        shutil.rmtree(self.pasta, ignore_errors=True)

    @staticmethod
    def _ler_err(proc, fila):
        try:
            for ln in iter(proc.stderr.readline, ""):
                fila.append(ln.rstrip())
        except (OSError, ValueError):
            pass

    def imagem(self, sid):
        cam = os.path.join(self.pasta, f"{int(sid)}.jpg")
        try:
            if time.time() - os.path.getmtime(cam) > self.VALIDADE_S:
                return None
            with open(cam, "rb") as f:
                d = f.read()
        except OSError:
            return None
        return d if d[:2] == b"\xff\xd8" and d[-2:] == b"\xff\xd9" else None   # JPEG completo

    def encerrar(self):
        self.parar.set()


class Distribuidor:
    """Recebe o TS do processo de entrada e entrega uma cópia a cada saída (UDP local)."""

    def __init__(self):
        import socket
        self._s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_RCVBUFFORCE", socket.SO_RCVBUF), BUFFER_UDP)
        except OSError:
            try:
                self._s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_UDP)
            except OSError:
                pass
        self._s.bind(("127.0.0.1", 0))
        self._s.settimeout(0.5)
        self.porta = self._s.getsockname()[1]
        self.destinos = []
        self.pacotes = 0
        self._parar = threading.Event()

    def iniciar(self, portas):
        self.destinos = [("127.0.0.1", p) for p in portas]
        threading.Thread(target=self._loop, daemon=True, name="distribuidor").start()

    def _loop(self):
        while not self._parar.is_set():
            try:
                d = self._s.recv(65536)
            except OSError:
                continue
            self.pacotes += len(d) // 188
            for dst in self.destinos:
                try:
                    self._s.sendto(d, dst)
                except Exception:
                    pass  # saída reiniciando: o datagrama é simplesmente descartado

    def fechar(self):
        self._parar.set()
        self._s.close()


# ======================================================================
# Supervisor de um canal
# ======================================================================
RE_BITRATE = re.compile(r"bitrate_monitor:.*?([\d,.]+)\s*b(?:its)?/s", re.I)
RE_ALARME = re.compile(r"bitrate_monitor:.*(lower|greater|exceed|below|above|alarm)", re.I)


class Canal(threading.Thread):
    def __init__(self, cfg: dict, sup: dict):
        super().__init__(daemon=True, name=cfg.get("nome", "canal"))
        self.cfg = cfg
        self.sup = sup
        ent0 = normalizar_canal(cfg)["entrada"]
        fo = (sup.get("failover") or {})
        self._fo_args = {"estavel_s": int(fo.get("voltar_apos_s", 30)), "sem_sinal_s": int(fo.get("sem_sinal_s", 15))}
        self.fo_ent = FailoverNic(ent0, "Entrada", **self._fo_args) \
            if ent0.get("interface_reserva") and ent0.get("tipo") in ("udp", "srt") else None
        self._troca = False
        montar_comando(self._cfg_rede(), sup.get("tsp", "tsp"))  # valida antes de abrir recursos
        self.analisador = AnalisadorTS()
        try:
            self.analisador.pids_ignorados = set(ler_pids(sup.get("pids_ignorados", PIDS_FORA_PADRAO)))
        except ValueError:
            self.analisador.pids_ignorados = set(PIDS_FORA_PADRAO)
        ms = sup.get("pcr_repeticao_ms", PCR_REP_PADRAO_MS)
        self.analisador.pcr_rep_ticks = int((ms if ms in PCR_REP_OPCOES else PCR_REP_PADRAO_MS) / 1000 * 27_000_000)
        self.inicio_sessao = time.time()
        porta = self.analisador.abrir()
        c0 = normalizar_canal(cfg)
        self.distribuidor = Distribuidor()
        self.cmd = montar_comando(self._cfg_rede(), sup.get("tsp", "tsp"), porta_analise=porta,
                                  porta_dist=self.distribuidor.porta if self.distribuidor else None)
        self.proc = None
        self.parar = threading.Event()
        self.lock = threading.Lock()
        self.log = collections.deque(maxlen=300)
        self.hist = collections.deque(maxlen=60)
        self.st = {"estado": "iniciando", "bitrate_bps": 0,
                   "alarmes_bitrate": 0, "reinicios": 0, "inicio_execucao": None,
                   "ultimo_bitrate_em": None, "ultima_msg": ""}
        c = normalizar_canal(cfg)
        self.srt = {}
        if c["entrada"].get("tipo") == "srt":
            self.srt["SRTIN"] = EstatSRT("entrada", c["entrada"], "Entrada")
        for i, s in enumerate(c["saidas"]):
            if s.get("tipo") == "srt":
                self.srt[f"SRTOUT{i}"] = EstatSRT("saida", s, f"Saída {i + 1}")
        self.procs_saida = [ProcSaida(self, i, s) for i, s in enumerate(c["saidas"])]
        self.previa = Previa(self)
        self.t_proc = time.monotonic()
        self.perda = {"analise": 0, "distribuidor": 0, "saidas": [0] * len(self.procs_saida)}
        self.perda_ate = 0.0
        self._aviso_perda = 0.0
        self.reinicios_vigia = 0

    def _cfg_rede(self):
        """Configuração do canal com a placa de rede ativa da entrada (principal ou reserva)."""
        c = normalizar_canal(self.cfg)
        if getattr(self, "fo_ent", None):
            c["entrada"] = self.fo_ent.aplicar(c["entrada"])
        return c

    def rede_status(self):
        return {"entrada": self.fo_ent.status() if self.fo_ent else None,
                "saidas": [ps.fo.status() if ps.fo else None for ps in self.procs_saida]}

    def _rotas_por_placa(self):
        if self.sup.get("rotas_por_placa"):
            threading.Thread(target=aplicar_rotas_por_placa, daemon=True).start()

    def _trocar_nic_entrada(self, para, motivo, agora):
        try:
            de = self.fo_ent.trocar(para, motivo, agora)
            self.cmd = montar_comando(self._cfg_rede(), self.sup.get("tsp", "tsp"), porta_analise=self.analisador.porta,
                                      porta_dist=self.distribuidor.porta)
        except ValueError as e:
            self._registrar(logging.ERROR, f"rede: não foi possível usar a placa {para}: {e}")
            return
        self._registrar(logging.WARNING, f"rede: entrada passou da placa {de} para {para} ({motivo})")
        self._set(ultima_msg=f"rede: entrada na placa {para} ({motivo})")
        self._rotas_por_placa()
        self._troca = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def srt_status(self):
        rodando = not self.parar.is_set()
        with self.lock:
            return {k: v.status(rodando=rodando) for k, v in self.srt.items()}

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def _registrar(self, nivel, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        log.log(nivel, "[%s] %s", self.name, msg)

    def status(self):
        with self.lock:
            s = dict(self.st)
            s["historico"] = list(self.hist)   # substituído abaixo pela taxa útil do analisador
        if s["inicio_execucao"]:
            s["uptime_s"] = int(time.time() - s["inicio_execucao"])
        intervalo = (self.cfg.get("processamento") or {}).get("intervalo_relatorio_s", 5)
        if s["estado"] == "rodando" and s["ultimo_bitrate_em"] \
                and time.time() - s["ultimo_bitrate_em"] > 3 * intervalo:
            s["estado"] = "sem_dados"
        s["tr101290"] = self.analisador.status()
        s["erros_cc"] = s["tr101290"]["contadores"]["cc"]
        s["srt"] = self.srt_status()
        s["perda_interna"] = self.perda_status()
        s["rede"] = self.rede_status()
        s["saidas"] = [p.status() for p in self.procs_saida]
        s["servicos"] = self.analisador.servicos_resumo()
        s.update(self.analisador.taxas())
        with self.analisador.lock:
            hu = list(self.analisador.hist_util)[-60:]
        if any(hu):
            s["historico"] = hu
        return s

    # --- vigia: sinal parado, saída travada e perda dentro do próprio servidor -----------
    def _vigia(self):
        base = {}
        ult_dist, t_dist = -1, time.monotonic()
        while not self.parar.wait(2):
            try:
                ult_dist, t_dist = self._vigiar(base, ult_dist, t_dist)
            except Exception:
                log.exception("[%s] vigia", self.name)

    def _vigiar(self, base, ult_dist, t_dist):
        agora = time.monotonic()
        proc_cfg = self.cfg.get("processamento") or {}
        # 1) perda interna (kernel descartou datagramas porque o processo não leu a tempo)
        drops = udp_descartes()
        alvos = [("analise", self.analisador.porta, None), ("distribuidor", self.distribuidor.porta, None)]
        alvos += [("saidas", ps.porta, i) for i, ps in enumerate(self.procs_saida)]
        novas = 0
        for nome, porta, i in alvos:
            d = drops.get(porta)
            if d is None:
                continue
            ant = base.get(porta, 0)
            if d < ant:          # processo da saída reiniciou: socket novo, contador do zero
                ant = 0
            delta, base[porta] = d - ant, d
            if delta <= 0:
                continue
            novas += delta
            if i is None:
                self.perda[nome] += delta
            else:
                self.perda["saidas"][i] += delta
            if nome == "analise":
                with self.analisador.lock:
                    self.analisador.perda_local += delta
        if novas:
            self.perda_ate = agora + 15
            if agora - self._aviso_perda > 60:
                self._aviso_perda = agora
                self._registrar(logging.WARNING,
                                f"o servidor descartou {novas} datagrama(s) internamente (CPU sobrecarregada ou "
                                "buffer UDP pequeno). Erros de CC neste momento podem não estar no sinal.")
        # 2) entrada parada: conectada, mas sem pacotes (ex.: SRT ativo e o encoder parou de enviar)
        dist = self.distribuidor.pacotes
        if dist != ult_dist:
            ult_dist, t_dist = dist, agora
        with self.analisador.lock:
            up = self.analisador.ultimo_pacote
        ultimo = max(x for x in (up, t_dist if ult_dist > 0 else None, 0.0) if x is not None)
        limite = float(proc_cfg.get("reiniciar_sem_sinal_s", 20) or 0)
        p = self.proc
        if limite > 0 and p is not None and p.poll() is None:
            tipo = normalizar_canal(self.cfg)["entrada"].get("tipo")
            teve_dados = ultimo > self.t_proc
            parado = agora - max(ultimo, self.t_proc)
            if (teve_dados and parado > limite) or (not teve_dados and tipo == "linuxasi" and parado > max(limite, 30)):
                self.reinicios_vigia += 1
                self._registrar(logging.WARNING, f"entrada sem sinal há {int(parado)} s — reiniciando a entrada")
                self._set(ultima_msg=f"sem sinal há {int(parado)} s: entrada reiniciada")
                try:
                    p.terminate()
                except OSError:
                    pass
        # 2b) placa de rede reserva da entrada
        if self.fo_ent and p is not None and p.poll() is None:
            fluindo = agora - ultimo < 3 and ultimo > self.t_proc
            if fluindo:
                self.fo_ent.sucesso()
            sem = None if fluindo else agora - max(ultimo, self.t_proc)
            r = self.fo_ent.avaliar(agora, sem)
            if r:
                self._trocar_nic_entrada(r[0], r[1], agora)
        # 2c) placa de rede reserva de cada saída
        for ps in self.procs_saida:
            if not ps.fo:
                continue
            sem = None
            pp = ps.proc
            if ps.cfg.get("tipo") == "srt" and ps.cfg.get("modo", "caller") != "listener" and pp is not None \
                    and pp.poll() is None:
                if ps.st.get("estado") == "rodando":
                    ps._sem_desde = None
                    ps.fo.sucesso()
                else:
                    ps._sem_desde = ps._sem_desde or agora
                    sem = agora - ps._sem_desde
            r = ps.fo.avaliar(agora, sem)
            if r:
                ps.trocar_nic(r[0], r[1], agora)
        # 3) saída travada: estava no ar, a entrada está chegando, mas o processo parou de responder
        entrada_ok = agora - ultimo < 3
        for i, ps in enumerate(self.procs_saida):
            pp = ps.proc
            if entrada_ok and pp is not None and pp.poll() is None and ps.st.get("estado") == "rodando" \
                    and agora - ps.atividade > 30:
                self._registrar(logging.WARNING, f"[saída {i + 1}] sem atividade há {int(agora - ps.atividade)} s "
                                "com a entrada no ar — reiniciando a saída")
                ps.atividade = agora
                try:
                    pp.terminate()
                except OSError:
                    pass
        return ult_dist, t_dist

    def perda_status(self):
        return {**self.perda, "recente": time.monotonic() < self.perda_ate}

    def relatorio(self, motivo):
        agora_w = time.time()
        a = self.analisador.relatorio(time.monotonic(), agora_w)
        prio = {i[0]: i[1] for i in TR_ITENS}
        cont = a["tr101290"]["contadores"]
        totais = {f"p{p}": sum(v for k, v in cont.items() if prio[k] == p) for p in (1, 2, 3)}
        return {"inicio": self.inicio_sessao, "fim": agora_w, "duracao_s": int(agora_w - self.inicio_sessao),
                "motivo": motivo, "reinicios": self.st.get("reinicios", 0),
                "taxa": a.pop("taxa_sessao"), "totais": totais, "analise": a, "srt": self.srt_status(),
                "saidas": [p.status() for p in self.procs_saida],
                "saidas_cfg": saidas_publicas(normalizar_canal(self.cfg)["saidas"]),
                "perda_interna": self.perda_status(), "reinicios_sem_sinal": self.reinicios_vigia,
                "rede": self.rede_status()}

    def zerar_contadores(self):
        with self.analisador.lock:
            self.analisador.zerar()
        with self.lock:
            self.st["alarmes_bitrate"] = 0
            self.st["reinicios"] = 0

    def _ler_saida(self, stream):
        for linha in iter(stream.readline, ""):
            linha = linha.rstrip()
            if not linha:
                continue
            ms = RE_SRT_ESTAT.search(linha)
            if ms:
                est = self.srt.get(ms.group(1))
                if est:
                    try:
                        dados = json.loads(ms.group(2))
                        with self.lock:
                            est.atualizar(dados, time.monotonic())
                    except ValueError:
                        pass
                continue
            mp = RE_SRT_PEER.search(linha)
            if mp and "SRTIN" in self.srt:
                self.srt["SRTIN"].peer = mp.group(1)
            m = None if RE_ALARME.search(linha) else RE_BITRATE.search(linha)
            if m:
                bps = int(float(m.group(1).replace(",", "")))
                with self.lock:
                    self.hist.append(bps)
                self._set(bitrate_bps=bps, ultimo_bitrate_em=time.time(), estado="rodando")
                log.debug("[%s] %s", self.name, linha)
                continue
            linha = mascarar(linha)
            if RE_ALARME.search(linha):
                with self.lock:
                    self.st["alarmes_bitrate"] += 1
                self._registrar(logging.WARNING, linha)
            else:
                self._registrar(logging.INFO, linha)
            if not RE_TSP_AVISO_NORMAL.search(linha):
                self._set(ultima_msg=linha[-300:])

    def run(self):
        espera_min = self.sup.get("reinicio_min_s", 2)
        espera_max = self.sup.get("reinicio_max_s", 60)
        espera = espera_min
        if self.distribuidor:
            self.distribuidor.iniciar([p.porta for p in self.procs_saida] + [self.previa.porta])
            for p in self.procs_saida:
                p.start()
            self.previa.start()
        threading.Thread(target=self._vigia, daemon=True, name=f"{self.name}-vigia").start()
        while not self.parar.is_set():
            self._registrar(logging.INFO, "iniciando: " + comando_para_log(self.cmd))
            with self.analisador.lock:
                self.analisador.reiniciar_estado()
            t0 = time.time()
            self.t_proc = time.monotonic()
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
            if self._troca:            # troca de placa de rede: reinicia na hora, sem contar como queda
                self._troca = False
                espera = espera_min
                self._set(estado="conectando", bitrate_bps=0, inicio_execucao=None)
                continue
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
        for ps in self.procs_saida:
            ps.encerrar()
        self.previa.encerrar()
        if self.distribuidor:
            self.distribuidor.fechar()
        self.analisador.fechar()


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
        self.rel_caminho = (os.path.splitext(caminho)[0] + "-relatorios.json") if caminho else None
        self.relatorios = {}
        if self.rel_caminho and os.path.exists(self.rel_caminho):
            try:
                with open(self.rel_caminho, encoding="utf-8") as f:
                    self.relatorios = json.load(f)
            except (OSError, ValueError) as e:
                log.error("não foi possível ler %s: %s", self.rel_caminho, e)
        self._parar_autosave = threading.Event()
        threading.Thread(target=self._autosave, daemon=True, name="autosave").start()

    # --- relatórios de análise ---------------------------------------
    MAX_RELATORIOS = 20

    def _gravar_relatorios(self):
        if not self.rel_caminho:
            return
        tmp = self.rel_caminho + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.relatorios, f, ensure_ascii=False)
        os.replace(tmp, self.rel_caminho)

    def _guardar(self, cid, t, motivo):
        if t.analisador.pacotes == 0:
            return  # nada foi recebido nesta sessão
        rel = t.relatorio(motivo)
        rel["canal"] = self.defs.get(cid, {}).get("nome", cid)
        with self.lock:
            lista = self.relatorios.setdefault(cid, [])
            if lista and lista[0].get("inicio") == rel["inicio"]:
                lista[0] = rel           # mesma sessão (autosave anterior): substitui
            else:
                lista.insert(0, rel)
            del lista[self.MAX_RELATORIOS:]
            try:
                self._gravar_relatorios()
            except OSError as e:
                log.error("não foi possível salvar relatórios: %s", e)

    def _autosave(self):
        """Salva a sessão em andamento a cada 60 s (preserva os dados se o processo cair)."""
        while not self._parar_autosave.wait(60):
            if getattr(self, "acesso", None):
                self.acesso.gravar_sessoes(forcar=False)
            for cid, t in list(self.exec.items()):
                try:
                    self._guardar(cid, t, "em andamento")
                except Exception:  # noqa: BLE001
                    log.exception("autosave")

    def resumo_relatorio(self, rel):
        a = rel["analise"]
        return {"inicio": rel["inicio"], "fim": rel["fim"], "duracao_s": rel["duracao_s"],
                "motivo": rel["motivo"], "taxa": rel["taxa"], "totais": rel["totais"],
                "contadores": a["tr101290"]["contadores"], "n_pids": a["resumo"]["n_pids"],
                "n_servicos": a["resumo"]["n_servicos"], "servicos": [s["nome"] for s in a["servicos"]],
                "servicos_lista": [{"numero": s["numero"], "nome": s["nome"]} for s in a["servicos"]]}

    def relatorio_salvo(self, cid, idx=0):
        lista = self.relatorios.get(cid) or []
        if not 0 <= idx < len(lista):
            return None
        rel = lista[idx]
        analise = dict(rel["analise"])
        # sessões salvas antes de o sistema conhecer os PIDs do ISDB-Tb: corrige a descrição ao exibir
        ign = set(self.aparencia()["pids_ignorados"])
        analise["pids"] = [{**p, "desc": PIDS_ISDB[p["pid"]], "tipo": "psi"}
                           if p.get("tipo") == "desconhecido" and p.get("pid") in PIDS_ISDB else
                           {**p, "desc": "Fora do pacote (ignorado)", "tipo": "ignorado"}
                           if p.get("tipo") == "desconhecido" and p.get("pid") in ign else p
                           for p in analise.get("pids") or []]
        # sessões salvas antes da 2.24.1: serviço sem PCR (0x1FFF) somava os pacotes nulos na taxa
        tx_pid = {p.get("pid"): p.get("taxa") or 0 for p in analise["pids"]}
        servs = []
        for s in analise.get("servicos") or []:
            if s.get("pcr_pid") == "0x1FFF":
                pmt = int(str(s.get("pmt") or "0"), 16)
                s = {**s, "pcr_pid": "", "taxa": sum(c.get("taxa") or 0 for c in s.get("componentes") or []) + tx_pid.get(pmt, 0)}
            servs.append(s)
        analise["servicos"] = servs
        analise["pids"] = [{**p, "servico": ""} if p.get("pid") == 0x1FFF else p for p in analise["pids"]]
        return {**analise, "estado": "parado", "salvo": True, "sessao": idx,
                "periodo": {k: rel[k] for k in ("inicio", "fim", "duracao_s", "motivo")},
                "taxa_sessao": rel["taxa"], "totais": rel["totais"], "srt": rel.get("srt") or {},
                "saidas": rel.get("saidas") or [], "saidas_cfg": rel.get("saidas_cfg") or [], "rede": rel.get("rede")}

    # --- aparência (nome e logo do painel) -----------------------------
    TIPOS_LOGO = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp"}
    MAX_LOGO = 2 * 1024 * 1024

    def _pasta_dados(self):
        return os.path.dirname(os.path.abspath(self.caminho)) if self.caminho else os.getcwd()

    def logo_arquivo(self):
        nome = self.sup.get("logo") or ""
        if nome == "-":          # removido de propósito pelo painel
            return None
        if not nome:             # padrão: logo-painel.* junto da configuração (o instalador coloca lá)
            for ext in (".png", ".svg", ".webp", ".jpg"):
                cam = os.path.join(self._pasta_dados(), "logo-painel" + ext)
                if os.path.isfile(cam):
                    return cam
            return None
        cam = nome if os.path.isabs(nome) else os.path.join(self._pasta_dados(), nome)
        return cam if os.path.isfile(cam) else None

    def aparencia(self):
        cam = self.logo_arquivo()
        try:
            ign = ler_pids(self.sup.get("pids_ignorados", PIDS_FORA_PADRAO))
        except ValueError:
            ign = list(PIDS_FORA_PADRAO)
        ms = self.sup.get("pcr_repeticao_ms", PCR_REP_PADRAO_MS)
        ms = ms if ms in PCR_REP_OPCOES else PCR_REP_PADRAO_MS
        aplicar_lim_pcr_texto(ms)
        return {"titulo": self.sup.get("titulo") or TITULO_PADRAO,
                "logo": bool(cam), "logo_v": int(os.path.getmtime(cam)) if cam else 0, "pids_ignorados": ign,
                "pcr_repeticao_ms": ms}

    def salvar_aparencia(self, dados):
        titulo = str(dados.get("titulo", self.sup.get("titulo") or TITULO_PADRAO)).strip()
        if not titulo or len(titulo) > 80:
            raise ValueError("o nome deve ter entre 1 e 80 caracteres")
        ign = ler_pids(dados["pids_ignorados"]) if "pids_ignorados" in dados else None
        pcr_ms = None
        if "pcr_repeticao_ms" in dados:
            try:
                pcr_ms = int(dados["pcr_repeticao_ms"])
            except (TypeError, ValueError):
                pcr_ms = -1
            if pcr_ms not in PCR_REP_OPCOES:
                raise ValueError("limite de repetição do PCR: 40 ou 100 ms")
        with self.lock:
            self.sup["titulo"] = titulo
            if pcr_ms is not None:
                self.sup["pcr_repeticao_ms"] = pcr_ms
                for c in list(self.exec.values()):
                    with c.analisador.lock:
                        c.analisador.pcr_rep_ticks = int(pcr_ms / 1000 * 27_000_000)
            if ign is not None:
                self.sup["pids_ignorados"] = ign
                for c in list(self.exec.values()):
                    with c.analisador.lock:
                        c.analisador.pids_ignorados = set(ign)
            if dados.get("remover_logo"):
                antigo = self.logo_arquivo()
                self.sup["logo"] = "-"
                if antigo and os.path.basename(antigo).startswith("logo-painel"):
                    try:
                        os.remove(antigo)
                    except OSError:
                        pass
            elif dados.get("logo_base64"):
                ext = self.TIPOS_LOGO.get(str(dados.get("logo_tipo", "")).lower())
                if not ext:
                    raise ValueError("logo deve ser PNG, JPG, SVG ou WebP")
                try:
                    conteudo = base64.b64decode(dados["logo_base64"], validate=True)
                except ValueError:
                    raise ValueError("arquivo de logo inválido")
                if len(conteudo) > self.MAX_LOGO:
                    raise ValueError("o logo deve ter no máximo 2 MB")
                antigo = self.logo_arquivo()
                nome = "logo-painel" + ext
                cam = os.path.join(self._pasta_dados(), nome)
                with open(cam + ".tmp", "wb") as f:
                    f.write(conteudo)
                os.replace(cam + ".tmp", cam)
                if antigo and antigo != cam and os.path.basename(antigo).startswith("logo-painel"):
                    try:
                        os.remove(antigo)
                    except OSError:
                        pass
                self.sup["logo"] = nome
            self.persistir()
        return self.aparencia()

    # --- persistência ------------------------------------------------
    def persistir(self):
        if not self.caminho:
            return
        with self.lock:
            cfg = {"canais": list(self.defs.values()), "supervisor": self.sup}
            salvar_config(self.caminho, cfg)

    def garantir_senha(self):
        if getattr(self, "acesso", None) is None:
            self.acesso = Acesso(self)
        return self.acesso

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
            try:
                self._guardar(cid, t, "parado" if marcar else "encerrado")
            except Exception:  # noqa: BLE001
                log.exception("erro ao salvar relatório")

    def reiniciar(self, cid):
        self.parar(cid, marcar=False)
        self.iniciar(cid)

    def zerar(self, cid):
        t = self.exec.get(cid)
        if t:
            t.zerar_contadores()

    def salvar_canal(self, dados: dict, cid=None) -> dict:
        dados = dict(dados)
        dados.pop("status", None)
        if cid:
            dados["id"] = cid
        c = normalizar_canal(dados)
        if c["entrada"].get("tipo") not in TIPOS_ENTRADA:
            raise ValueError("tipo de entrada inválido")
        montar_comando(_cfg_validacao(c), self.sup.get("tsp", "tsp"))  # valida (levanta ValueError)
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
            if self.relatorios.pop(cid, None) is not None:
                self._gravar_relatorios()

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
                                       "alarmes_bitrate": 0, "reinicios": 0, "historico": [],
                                       "tr101290": None}
            rels = self.relatorios.get(cid) or []
            ult = self.resumo_relatorio(rels[0]) if rels else None
            if ult and t is None and ult["motivo"] == "em andamento":
                ult["motivo"] = "interrompido"
            out.append({**c, "status": st, "ultimo_relatorio": ult})
        return out

    def log_canal(self, cid):
        t = self.exec.get(cid)
        return list(t.log) if t else []


# ======================================================================
# Relatório para download: HTML (formatado, imprimível) e TXT
# ======================================================================
def _f_taxa(b):
    b = float(b or 0)
    return f"{b / 1e6:.2f} Mb/s" if b >= 1e6 else f"{b / 1e3:.1f} kb/s"


def _f_dur(s):
    s = int(s or 0)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}min{s:02d}s" if h else (f"{m}min{s:02d}s" if m else f"{s}s")


def _f_dh(ts):
    return time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(ts)) if ts else "—"


def _f_n(v):
    return f"{int(v or 0):,}".replace(",", ".")


def _desc_entrada(e):
    t = e.get("tipo")
    if t == "srt":
        txt = f"SRT {e.get('modo', 'caller')} {e.get('endereco', '')}" + (f", latência {e.get('latencia_ms')} ms" if e.get("latencia_ms") else "")
    elif t == "udp":
        txt = f"UDP {e.get('endereco', '')}"
    elif t == "dektec":
        txt = f"ASI DekTec placa {e.get('dispositivo', 0)} porta {e.get('canal', 0)}"
    elif t == "linuxasi":
        txt = f"ASI {e.get('dispositivo', '')}"
    else:
        txt = f"{t} {e.get('caminho', '')}"
    nic = e.get("interface") or e.get("interface_local")
    return txt + (f" (placa {nic})" if nic and t in ("srt", "udp") else "")


def _desc_saida(s):
    t = s.get("tipo")
    if t == "rtmp":
        url = str(s.get("url", "")).rstrip("/")
        txt = "RTMP " + (url + "/********" if s.get("chave") or s.get("chave_oculta") else mascarar(url))
    elif t == "srt":
        txt = f"SRT {s.get('modo', 'caller')} {s.get('endereco', '')}"
    else:
        txt = f"{t.upper()} {s.get('endereco', '')}"
    nic = s.get("interface") or s.get("interface_local")
    if nic and t != "rtmp":
        txt += f" (placa {nic})"
    sv = s.get("servicos") or ([s["programa"]] if s.get("programa") else [])
    return txt + (f" — serviços {', '.join(map(str, sv))}" if sv else " — TS completo")


def _rede_relatorio(rede):
    """Placas reserva configuradas e as trocas que aconteceram na sessão."""
    itens, eventos = [], []
    if not rede:
        return itens, eventos
    for rot, fo in [("Entrada", rede.get("entrada"))] + [(f"Destino {i + 1}", x) for i, x in enumerate(rede.get("saidas") or [])]:
        if not fo:
            continue
        itens.append({"onde": rot, "principal": fo.get("principal"), "reserva": fo.get("reserva"), "ativa": fo.get("ativa"),
                      "trocas": fo.get("trocas", 0)})
        eventos += [{"onde": rot, **ev} for ev in fo.get("eventos") or []]
    eventos.sort(key=lambda x: x.get("em") or 0)
    return itens, eventos


def dados_relatorio(dados, canal, titulo):
    """Organiza tudo o que vai no relatório (o mesmo conteúdo para HTML e TXT)."""
    r, tr, per = dados.get("resumo", {}), dados.get("tr101290", {}), dados.get("periodo", {})
    agora = time.time()
    ts = dados.get("taxa_sessao") or {}
    cont = tr.get("contadores", {})
    na = set(tr.get("nao_aplicavel") or [])
    ultimo = dict(tr.get("ultimo_em") or {})
    for k, ha in (tr.get("ha_s") or {}).items():
        ultimo.setdefault(k, agora - ha)
    indicadores = [{"prio": prio, "cod": cod, "nome": nome, "desc": desc, "valor": cont.get(k, 0),
                    "na": k in na and not cont.get(k, 0), "ultimo": ultimo.get(k)}
                   for k, prio, cod, nome, desc in TR_ITENS]
    modo = r.get("modo_taxa") or {}
    return {
        "titulo": titulo, "canal": canal.get("nome", ""), "gerado": agora,
        "salvo": bool(dados.get("salvo")), "motivo": per.get("motivo"),
        "inicio": per.get("inicio"), "fim": per.get("fim") or agora, "duracao": per.get("duracao_s", 0),
        "entrada": _desc_entrada(canal.get("entrada", {})),
        "taxa": {"media": ts.get("media"), "min": ts.get("min"), "max": ts.get("max"),
                 "atual_util": r.get("taxa_util"), "atual_ts": r.get("taxa_total"),
                 "rajadas": ts.get("rajadas") or 0, "perda_interna": ts.get("perda_interna") or 0},
        "modo": modo, "tsid": r.get("tsid"), "rede": r.get("rede"), "hora_ts": r.get("hora_ts"),
        "n_pids": r.get("n_pids"), "pacotes": r.get("pacotes"),
        "indicadores": indicadores,
        "totais": {p: sum(i["valor"] for i in indicadores if i["prio"] == p) for p in (1, 2, 3)},
        "servicos": dados.get("servicos", []), "pids": dados.get("pids", []),
        "srt": list((dados.get("srt") or {}).values()),
        "saidas": [(c, (dados.get("saidas") or [None] * 99)[i] if i < len(dados.get("saidas") or []) else None)
                   for i, c in enumerate(dados.get("saidas_cfg") or [])],
        "placas": _rede_relatorio(dados.get("rede")),
    }


MOTIVOS_REL = {"parado": "encerrada pelo usuário", "encerrado": "encerrada (serviço desligado)",
               "interrompido": "interrompida", "em andamento": "em andamento (último salvamento automático)"}


def relatorio_txt(d) -> str:
    L = []
    linha = "=" * 78
    L += [linha, f"{d['titulo']} — Relatório de análise", linha,
          f"Canal ............: {d['canal']}",
          f"Entrada ..........: {d['entrada']}",
          f"Período ..........: {_f_dh(d['inicio'])}  até  {_f_dh(d['fim'])}  ({_f_dur(d['duracao'])})",
          f"Situação .........: {MOTIVOS_REL.get(d['motivo'], 'sessão ao vivo (no momento da geração)')}",
          f"Gerado em ........: {_f_dh(d['gerado'])}", ""]
    tx = d["taxa"]
    L += ["TAXA (útil, sem pacotes nulos)", "-" * 78]
    if tx.get("media"):
        L += [f"  Média {_f_taxa(tx['media'])}   ·   mínima {_f_taxa(tx['min'])}   ·   máxima {_f_taxa(tx['max'])}"
              "   (mín./máx. = médias de 5 s)"]
    if tx.get("rajadas"):
        L += [f"  Rajadas de chegada: {tx['rajadas']} s (pacotes retidos pela rede/SRT e entregues de uma vez)"]
    if tx.get("perda_interna"):
        L += [f"  ATENÇÃO: o servidor descartou {_f_n(tx['perda_interna'])} datagramas internamente "
              "(CPU/buffer); erros de CC nesses momentos podem não estar no sinal"]
    if not d["salvo"] and tx.get("atual_util") is not None:
        L += [f"  Agora {_f_taxa(tx['atual_util'])} úteis   (TS total com nulos: {_f_taxa(tx['atual_ts'])})"]
    m = d["modo"]
    if m.get("modo"):
        L += [f"  Tipo do TS: {m['modo']} (variação {m['variacao_pct']}%)"]
    L += [f"  TS ID {d['tsid'] if d['tsid'] is not None else '—'}   ·   Rede {d['rede'] or '—'}   ·   "
          f"{len(d['servicos'])} serviço(s)   ·   {d['n_pids'] or 0} PIDs   ·   {_f_n(d['pacotes'])} pacotes", ""]
    L += ["TR 101 290", "-" * 78]
    for p in (1, 2, 3):
        L.append(f"  Prioridade {p}  (total {_f_n(d['totais'][p])})")
        for i in (x for x in d["indicadores"] if x["prio"] == p):
            val = "n/a" if i["na"] else _f_n(i["valor"])
            ult = f"último em {_f_dh(i['ultimo'])}" if i["ultimo"] and i["valor"] else ""
            L.append(f"    {i['cod']:<5} {i['nome']:<28} {val:>10}   {ult}")
        L.append("")
    L += ["SERVIÇOS", "-" * 78]
    for s in d["servicos"]:
        md = (s.get("modo") or {}).get("modo")
        L.append(f"  {s['nome']}  (programa {s['numero']})  {s.get('provedor') or ''}  {s.get('tipo') or ''}"
                 f"  {_f_taxa(s.get('taxa'))}{'  ' + md if md else ''}")
        pcr = s.get("pcr") or {}
        L.append(f"    PMT {s.get('pmt')}  PCR {s.get('pcr_pid') or '—'}"
                 + (f"  intervalo PCR {pcr.get('intervalo_ms')} ms  jitter {pcr.get('jitter_ns')} ns" if pcr else ""))
        for c in s.get("componentes", []):
            L.append(f"      {c['hex']}  {c['desc']:<26} {c['stream_type']}  {_f_taxa(c['taxa'])}")
    if not d["servicos"]:
        L.append("  (nenhum serviço identificado)")
    L += ["", "PIDS", "-" * 78, f"  {'PID':<8}{'Dec':>6}  {'Descrição':<28}{'Bitrate':>13}{'%':>7}{'Pacotes':>13}{'Erros CC':>10}"]
    for p in d["pids"]:
        L.append(f"  {p['hex']:<8}{p['pid']:>6}  {p['desc'][:27]:<28}{_f_taxa(p['taxa']):>13}{p['pct']:>7.1f}"
                 f"{_f_n(p['pacotes']):>13}{p['cc']:>10}")
    if d["srt"]:
        L += ["", "CONEXÕES SRT", "-" * 78]
        for e in d["srt"]:
            t = e.get("totais") or {}
            L.append(f"  {e.get('rotulo')}: {e.get('modo')} {e.get('endereco')}  ·  {e.get('conexoes', 0)} conexão(ões)")
            L.append(f"    RTT médio/máx {e.get('rtt_medio_ms', 0)}/{e.get('rtt_max_ms', 0)} ms  ·  pacotes {_f_n(t.get('pacotes'))}"
                     f"  ·  perdidos {_f_n(t.get('perdidos'))}  ·  retransmitidos {_f_n(t.get('retransmitidos'))}"
                     f"  ·  não recuperados {_f_n(t.get('descartados'))}")
    if d["saidas"]:
        L += ["", "SAÍDAS", "-" * 78]
        for i, (c, st) in enumerate(d["saidas"], 1):
            L.append(f"  Destino {i}: {_desc_saida(c)}")
            if st:
                L.append(f"    enviado {_f_n((st.get('enviado_bytes') or 0) / 1e6)} MB  ·  tempo no ar {_f_dur(st.get('tempo_no_ar_s'))}"
                         f"  ·  conexões {st.get('conexoes', 0)}  ·  quedas {st.get('reinicios', 0)}")
    itens, eventos = d.get("placas") or ([], [])
    if itens:
        L += ["", "PLACAS DE REDE (RESERVA AUTOMÁTICA)", "-" * 78]
        for it in itens:
            L.append(f"  {it['onde']}: principal {it['principal']}  ·  reserva {it['reserva']}  ·  "
                     f"{'na RESERVA' if it['ativa'] != it['principal'] else 'na principal'} no fim  ·  {it['trocas']} troca(s)")
        for ev in eventos:
            L.append(f"    {_f_dh(ev.get('em'))}  {ev['onde']}: {ev.get('de')} -> {ev.get('para')}  ({ev.get('motivo')})")
    L += ["", linha, f"Gerado por {d['titulo']} — asi2srt {__version__}", linha]
    return "\n".join(L) + "\n"


def relatorio_html(d, logo=None) -> str:
    e = html.escape
    cor = lambda i: "na" if i["na"] else ("err" if i["valor"] else "ok")  # noqa: E731
    logo_tag = ""
    if logo:
        tipo, dados_logo = logo
        logo_tag = f'<img class="logo" src="data:{tipo};base64,{base64.b64encode(dados_logo).decode()}" alt="">'
    tx = d["taxa"]
    m = d["modo"]
    kpis = []
    if tx.get("media"):
        kpis += [("Bitrate médio (útil)", _f_taxa(tx["media"])), ("Mínimo", _f_taxa(tx["min"])), ("Máximo", _f_taxa(tx["max"]))]
    if not d["salvo"] and tx.get("atual_util") is not None:
        kpis += [("Bitrate útil agora", _f_taxa(tx["atual_util"]))]
    kpis += [("Tipo do TS", f"{m['modo']} ±{m['variacao_pct']}%" if m.get("modo") else "—"),
             ("Duração", _f_dur(d["duracao"])), ("Serviços", str(len(d["servicos"]))), ("PIDs", str(d["n_pids"] or 0)),
             ("TS ID", str(d["tsid"]) if d["tsid"] is not None else "—"), ("Rede", d["rede"] or "—")]
    if tx.get("rajadas"):
        kpis.append(("Rajadas de chegada", f"{tx['rajadas']} s"))
    if tx.get("perda_interna"):
        kpis.append(("Perda interna (servidor)", _f_n(tx["perda_interna"])))
    H = [f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Relatório — {e(d['canal'])}</title>
<style>
*{{box-sizing:border-box}}body{{font:13px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2329;margin:0;background:#f4f6f8}}
.pag{{max-width:1050px;margin:24px auto;background:#fff;padding:28px 34px;border-radius:10px;box-shadow:0 1px 4px #0001}}
header{{display:flex;align-items:center;gap:16px;border-bottom:3px solid #00a859;padding-bottom:14px;margin-bottom:18px}}
.logo{{height:52px}}h1{{font-size:20px;margin:0}}h1 small{{display:block;font-size:13px;font-weight:400;color:#5b6570;margin-top:2px}}
h2{{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:#3a4550;margin:24px 0 8px;border-bottom:1px solid #e3e7eb;padding-bottom:4px}}
.info{{display:grid;grid-template-columns:150px 1fr;gap:3px 12px;font-size:13px}}.info b{{color:#5b6570;font-weight:500}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}}
.kpi{{border:1px solid #e3e7eb;border-radius:8px;padding:8px 10px}}.kpi small{{display:block;color:#6b7580;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
.kpi b{{font-size:16px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}}
th{{text-align:left;color:#5b6570;font-weight:600;font-size:11.5px;padding:6px 8px;border-bottom:2px solid #e3e7eb;background:#fafbfc}}
td{{padding:5px 8px;border-bottom:1px solid #eef1f3}}td.n,th.n{{text-align:right;white-space:nowrap}}.mono{{font-family:ui-monospace,Consolas,monospace;white-space:nowrap}}
.st{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}}
.ok .st{{background:#1f9d55}}.err .st{{background:#d64045}}.na .st{{background:#b8c0c8}}
tr.err td{{color:#b3261e;font-weight:600}}tr.na td{{color:#9aa3ab}}
.prio td{{background:#f4f6f8;font-weight:700;color:#3a4550}}
.serv{{border:1px solid #e3e7eb;border-radius:8px;padding:10px 12px;margin-bottom:10px}}.serv h3{{margin:0 0 4px;font-size:14px}}
.meta{{color:#5b6570;font-size:12px;margin-bottom:6px}}.tag{{display:inline-block;background:#eef3ff;color:#2f5fb3;border-radius:4px;padding:0 6px;font-size:11px;font-weight:600}}
.res{{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:12px}}
.res.ok{{background:#e5f6ec;color:#17733f}}.res.err{{background:#fdecec;color:#b3261e}}
footer{{margin-top:26px;color:#8a949e;font-size:11px;border-top:1px solid #e3e7eb;padding-top:8px}}
@media print{{body{{background:#fff}}.pag{{box-shadow:none;margin:0;max-width:none;padding:0}}h2{{break-after:avoid}}.serv,tr{{break-inside:avoid}}}}
</style></head><body><div class="pag">
<header>{logo_tag}<h1>{e(d['titulo'])}<small>Relatório de análise · {e(d['canal'])}</small></h1>
</header>
<div class="info"><b>Canal</b><span>{e(d['canal'])}</span><b>Entrada</b><span>{e(d['entrada'])}</span>
<b>Período</b><span>{_f_dh(d['inicio'])} até {_f_dh(d['fim'])} ({_f_dur(d['duracao'])})</span>
<b>Situação</b><span>{e(MOTIVOS_REL.get(d['motivo'], 'sessão ao vivo (no momento da geração)'))}</span>
<b>Gerado em</b><span>{_f_dh(d['gerado'])}</span></div>
<h2>Resumo</h2><div class="kpis">{''.join(f'<div class="kpi"><small>{e(a)}</small><b>{e(b)}</b></div>' for a, b in kpis)}</div>
<h2>TR 101 290</h2><table><tr><th>Código</th><th>Indicador</th><th class="n">Ocorrências</th><th>Última ocorrência</th><th>Regra</th></tr>"""]
    for p in (1, 2, 3):
        H.append(f'<tr class="prio"><td colspan="5">Prioridade {p} — total {_f_n(d["totais"][p])}</td></tr>')
        for i in (x for x in d["indicadores"] if x["prio"] == p):
            H.append(f'<tr class="{cor(i)}"><td class="mono"><span class="st"></span>{e(i["cod"])}</td><td>{e(i["nome"])}</td>'
                     f'<td class="n">{"n/a" if i["na"] else _f_n(i["valor"])}</td>'
                     f'<td>{_f_dh(i["ultimo"]) if i["ultimo"] and i["valor"] else ""}</td><td style="color:#6b7580">{e(i["desc"])}</td></tr>')
    H.append("</table><h2>Serviços</h2>")
    for s in d["servicos"]:
        pcr = s.get("pcr") or {}
        md = (s.get("modo") or {}).get("modo")
        H.append(f'<div class="serv"><h3>{e(s["nome"])} <span class="tag">programa {s["numero"]}</span> '
                 f'{f"<span class=tag>{e(md)}</span>" if md else ""} <span style="float:right">{_f_taxa(s.get("taxa"))}</span></h3>'
                 f'<div class="meta">{e(s.get("provedor") or "")} {e(s.get("tipo") or "")} · PMT <span class="mono">{e(s.get("pmt") or "")}</span> · '
                 f'PCR <span class="mono">{e(s.get("pcr_pid") or "—")}</span>'
                 + (f' · intervalo PCR {pcr.get("intervalo_ms")} ms · jitter {pcr.get("jitter_ns")} ns' if pcr else "")
                 + '</div><table><tr><th>PID</th><th>Componente</th><th>stream_type</th><th class="n">Bitrate</th></tr>'
                 + "".join(f'<tr><td class="mono">{e(c["hex"])}</td><td>{e(c["desc"])}</td><td class="mono">{e(c["stream_type"])}</td>'
                           f'<td class="n">{_f_taxa(c["taxa"])}</td></tr>' for c in s.get("componentes", []))
                 + "</table></div>")
    if not d["servicos"]:
        H.append("<p>Nenhum serviço identificado.</p>")
    H.append('<h2>PIDs</h2><table><tr><th>PID</th><th class="n">Dec</th><th>Descrição</th><th>Serviço</th><th class="n">Bitrate</th>'
             '<th class="n">%</th><th class="n">Pacotes</th><th class="n">Erros CC</th></tr>')
    for p in d["pids"]:
        H.append(f'<tr class="{"err" if p["cc"] or p["tipo"] == "desconhecido" else ""}"><td class="mono">{e(p["hex"])}</td><td class="n">{p["pid"]}</td>'
                 f'<td>{e(p["desc"])}</td><td>{e(p.get("servico") or "")}</td><td class="n">{_f_taxa(p["taxa"])}</td>'
                 f'<td class="n">{p["pct"]:.1f}</td><td class="n">{_f_n(p["pacotes"])}</td><td class="n">{p["cc"]}</td></tr>')
    H.append("</table>")
    if d["srt"]:
        H.append('<h2>Conexões SRT</h2><table><tr><th>Conexão</th><th>Endereço</th><th class="n">Conexões</th><th class="n">RTT médio / máx</th>'
                 '<th class="n">Pacotes</th><th class="n">Perdidos</th><th class="n">Retransmitidos</th><th class="n">Não recuperados</th></tr>')
        for s in d["srt"]:
            t = s.get("totais") or {}
            H.append(f'<tr class="{"err" if t.get("descartados") else ""}"><td>{e(s.get("rotulo") or "")}</td><td class="mono">{e(s.get("modo") or "")} {e(s.get("endereco") or "")}</td>'
                     f'<td class="n">{s.get("conexoes", 0)}</td><td class="n">{s.get("rtt_medio_ms", 0)} / {s.get("rtt_max_ms", 0)} ms</td>'
                     f'<td class="n">{_f_n(t.get("pacotes"))}</td><td class="n">{_f_n(t.get("perdidos"))}</td>'
                     f'<td class="n">{_f_n(t.get("retransmitidos"))}</td><td class="n">{_f_n(t.get("descartados"))}</td></tr>')
        H.append("</table>")
    if d["saidas"]:
        H.append('<h2>Saídas</h2><table><tr><th>Destino</th><th>Configuração</th><th class="n">Enviado</th><th class="n">Tempo no ar</th>'
                 '<th class="n">Conexões</th><th class="n">Quedas</th></tr>')
        for i, (c, st) in enumerate(d["saidas"], 1):
            st = st or {}
            H.append(f'<tr class="{"err" if st.get("reinicios") else ""}"><td>Destino {i}</td><td>{e(_desc_saida(c))}</td>'
                     f'<td class="n">{(st.get("enviado_bytes") or 0) / 1e6:.1f} MB</td><td class="n">{_f_dur(st.get("tempo_no_ar_s"))}</td>'
                     f'<td class="n">{st.get("conexoes", 0)}</td><td class="n">{st.get("reinicios", 0)}</td></tr>')
        H.append("</table>")
    itens, eventos = d.get("placas") or ([], [])
    if itens:
        H.append('<h2>Placas de rede (reserva automática)</h2><table><tr><th>Onde</th><th>Principal</th><th>Reserva</th>'
                 '<th>No fim da sessão</th><th class="n">Trocas</th></tr>')
        for it in itens:
            res = it["ativa"] != it["principal"]
            H.append(f'<tr class="{"err" if res else ""}"><td>{e(it["onde"])}</td><td class="mono">{e(it["principal"] or "")}</td>'
                     f'<td class="mono">{e(it["reserva"] or "")}</td><td>{"na reserva" if res else "na principal"}</td>'
                     f'<td class="n">{it["trocas"]}</td></tr>')
        H.append("</table>")
        if eventos:
            H.append('<table style="margin-top:8px"><tr><th>Quando</th><th>Onde</th><th>De → para</th><th>Motivo</th></tr>'
                     + "".join(f'<tr><td>{_f_dh(ev.get("em"))}</td><td>{e(ev["onde"])}</td><td class="mono">{e(ev.get("de") or "")} → '
                               f'{e(ev.get("para") or "")}</td><td>{e(ev.get("motivo") or "")}</td></tr>' for ev in eventos) + "</table>")
    H.append(f'<footer>Gerado por {e(d["titulo"])} em {_f_dh(d["gerado"])} · asi2srt {__version__}</footer></div></body></html>')
    return "\n".join(H)


def pdf_disponivel():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def relatorio_pdf(d, logo=None) -> bytes:
    """Mesmo conteúdo do relatório HTML, em PDF A4 (precisa do reportlab: apt install python3-reportlab)."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    def tx(v):   # as fontes padrão do PDF só têm o conjunto Windows-1252
        s = str("" if v is None else v).replace("→", "->").replace("…", "...").replace("⚠", "!")
        return s.encode("cp1252", "replace").decode("cp1252")

    VERDE, VERM, CINZA = colors.HexColor("#1f9d55"), colors.HexColor("#b3261e"), colors.HexColor("#9aa3ab")
    LINHA, FUNDO, TXT2 = colors.HexColor("#e3e7eb"), colors.HexColor("#f4f6f8"), colors.HexColor("#5b6570")
    s_cel = ParagraphStyle("cel", fontName="Helvetica", fontSize=8, leading=10)
    s_celr = ParagraphStyle("celr", parent=s_cel, alignment=2)
    s_err = ParagraphStyle("err", parent=s_cel, textColor=VERM)
    s_errr = ParagraphStyle("errr", parent=s_celr, textColor=VERM)
    s_h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=19)
    s_sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=12, textColor=TXT2)
    s_h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
                          textColor=colors.HexColor("#3a4550"), spaceBefore=10, spaceAfter=4,
                          keepWithNext=1)
    s_h3 = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=9.5, leading=12)
    s_meta = ParagraphStyle("meta", parent=s_cel, textColor=TXT2)
    largura = A4[0] - 30 * mm

    def P(v, st=None):
        return Paragraph(html.escape(tx(v)), st or s_cel)

    def tabela(cab, linhas, larg, direita=(), estilos=(), vermelhas=()):
        dados = [[P(c, s_celr if i in direita else s_cel) for i, c in enumerate(cab)]]
        for n, ln in enumerate(linhas, 1):
            erro = n in vermelhas
            dados.append([c if isinstance(c, Paragraph) else
                          P(c, (s_errr if erro else s_celr) if i in direita else (s_err if erro else s_cel))
                          for i, c in enumerate(ln)])
        tb = Table(dados, colWidths=[largura * f for f in larg], repeatRows=1)
        st = [("BACKGROUND", (0, 0), (-1, 0), FUNDO), ("LINEBELOW", (0, 0), (-1, 0), 1, LINHA),
              ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINHA), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
        tb.setStyle(TableStyle(st + list(estilos)))
        return tb

    def cor_txt(v, c, st=None):
        return Paragraph(f'<font color="{c}">{html.escape(tx(v))}</font>', st or s_cel)

    taxa, m = d["taxa"], d["modo"]
    E = []
    # cabeçalho: logo, título e resultado geral
    cab = []
    if logo:
        try:
            img = Image(io.BytesIO(logo[1]))
            r = img.imageWidth / float(img.imageHeight or 1)
            img.drawHeight = 14 * mm
            img.drawWidth = min(14 * mm * r, 45 * mm)
            cab.append(img)
        except Exception:
            cab = []
    bloco = [Paragraph(html.escape(tx(d["titulo"])), s_h1),
             Paragraph("Relatório de análise · " + html.escape(tx(d["canal"])), s_sub)]
    larg_cab = ([50 * mm] if cab else []) + [None]
    tcab = Table([cab + [bloco]], colWidths=larg_cab)
    tcab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, 0), 2, VERDE),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    E += [tcab, Spacer(1, 6)]
    info = [("Canal", d["canal"]), ("Entrada", d["entrada"]),
            ("Período", f"{_f_dh(d['inicio'])} até {_f_dh(d['fim'])} ({_f_dur(d['duracao'])})"),
            ("Situação", MOTIVOS_REL.get(d["motivo"], "sessão ao vivo (no momento da geração)")),
            ("Gerado em", _f_dh(d["gerado"]))]
    ti = Table([[P(a, s_meta), P(b)] for a, b in info], colWidths=[28 * mm, largura - 28 * mm])
    ti.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                            ("LEFTPADDING", (0, 0), (0, -1), 0)]))
    E.append(ti)
    # resumo em quadros
    kpis = []
    if taxa.get("media"):
        kpis += [("Bitrate médio (útil)", _f_taxa(taxa["media"])), ("Mínimo (5 s)", _f_taxa(taxa["min"])),
                 ("Máximo (5 s)", _f_taxa(taxa["max"]))]
    if not d["salvo"] and taxa.get("atual_util") is not None:
        kpis.append(("Bitrate útil agora", _f_taxa(taxa["atual_util"])))
    kpis += [("Tipo do TS", f"{m['modo']} ±{m['variacao_pct']}%" if m.get("modo") else "—"),
             ("Duração", _f_dur(d["duracao"])), ("Serviços", str(len(d["servicos"]))), ("PIDs", str(d["n_pids"] or 0)),
             ("TS ID", str(d["tsid"]) if d["tsid"] is not None else "—"), ("Rede", d["rede"] or "—")]
    if taxa.get("rajadas"):
        kpis.append(("Rajadas de chegada", f"{taxa['rajadas']} s"))
    if taxa.get("perda_interna"):
        kpis.append(("Perda interna (servidor)", _f_n(taxa["perda_interna"])))
    s_kt = ParagraphStyle("kt", parent=s_cel, fontSize=6.8, textColor=TXT2)
    s_kv = ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=10.5, leading=13)
    cel = [[Paragraph(html.escape(tx(a.upper())), s_kt), Paragraph(html.escape(tx(b)), s_kv)] for a, b in kpis]
    n_col = 5
    grade = [cel[i:i + n_col] + [""] * (n_col - len(cel[i:i + n_col])) for i in range(0, len(cel), n_col)]
    tk = Table(grade, colWidths=[largura / n_col] * n_col)
    tk.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINHA), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINHA),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    E += [Paragraph("RESUMO", s_h2), tk]
    # TR 101 290
    linhas, estilos = [], []
    for pr in (1, 2, 3):
        linhas.append([Paragraph(f"<b>Prioridade {pr} — total {_f_n(d['totais'][pr])}</b>", s_cel), "", "", "", ""])
        n = len(linhas)
        estilos += [("SPAN", (0, n), (-1, n)), ("BACKGROUND", (0, n), (-1, n), FUNDO)]
        for i in (x for x in d["indicadores"] if x["prio"] == pr):
            c = "#9aa3ab" if i["na"] else ("#b3261e" if i["valor"] else "#1f9d55")
            ctxt = "#b3261e" if i["valor"] else ("#9aa3ab" if i["na"] else "#1d2329")
            linhas.append([Paragraph(f'<font color="{c}"><b>•</b></font> ' + html.escape(tx(i["cod"])), s_cel),
                           cor_txt(i["nome"], ctxt),
                           cor_txt("n/a" if i["na"] else _f_n(i["valor"]), ctxt, s_celr),
                           P(_f_dh(i["ultimo"]) if i["ultimo"] and i["valor"] else ""),
                           cor_txt(i["desc"], "#6b7580")])
    E += [Paragraph("TR 101 290", s_h2),
          tabela(["Código", "Indicador", "Ocorrências", "Última ocorrência", "Regra"], linhas,
                 [.09, .2, .12, .17, .42], direita=(2,), estilos=estilos)]
    # serviços
    tit_serv = [Paragraph("SERVIÇOS", s_h2)]     # vai junto com o 1º serviço (não fica sozinho no fim da página)
    for s in d["servicos"]:
        pcr = s.get("pcr") or {}
        md = (s.get("modo") or {}).get("modo")
        meta = (f'{s.get("provedor") or ""} {s.get("tipo") or ""} · PMT {s.get("pmt") or ""} · '
                f'PCR {s.get("pcr_pid") or "—"}'
                + (f' · intervalo PCR {pcr.get("intervalo_ms")} ms · jitter {pcr.get("jitter_ns")} ns' if pcr else ""))
        titulo = Table([[Paragraph(f'{html.escape(tx(s["nome"]))} <font size="8" color="#2f5fb3">programa {s["numero"]}'
                                   + (f" · {html.escape(md)}" if md else "") + "</font>", s_h3),
                         Paragraph(_f_taxa(s.get("taxa")), ParagraphStyle("tr", parent=s_h3, alignment=2))]],
                       colWidths=[largura * .7, largura * .3])
        titulo.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        comp = tabela(["PID", "Componente", "stream_type", "Bitrate"],
                      [[c["hex"], c["desc"], c["stream_type"], _f_taxa(c["taxa"])] for c in s.get("componentes", [])],
                      [.14, .46, .2, .2], direita=(3,))
        E.append(KeepTogether(tit_serv + [titulo, P(meta.strip(), s_meta), Spacer(1, 2), comp, Spacer(1, 7)]))
        tit_serv = []
    if not d["servicos"]:
        E += tit_serv + [P("Nenhum serviço identificado.")]
    # PIDs
    linhas = [[p["hex"], str(p["pid"]), p["desc"], p.get("servico") or "", _f_taxa(p["taxa"]),
               f'{p["pct"]:.1f}', _f_n(p["pacotes"]), str(p["cc"])] for p in d["pids"]]
    verm = {i for i, p in enumerate(d["pids"], 1) if p["cc"] or p["tipo"] == "desconhecido"}
    E += [Paragraph("PIDS", s_h2),
          tabela(["PID", "Dec", "Descrição", "Serviço", "Bitrate", "%", "Pacotes", "Erros CC"], linhas,
                 [.08, .06, .26, .17, .13, .07, .12, .11], direita=(1, 4, 5, 6, 7), vermelhas=verm)]
    if d["srt"]:
        linhas, verm = [], set()
        for n, s in enumerate(d["srt"], 1):
            tt = s.get("totais") or {}
            if tt.get("descartados"):
                verm.add(n)
            linhas.append([s.get("rotulo") or "", f'{s.get("modo") or ""} {s.get("endereco") or ""}',
                           str(s.get("conexoes", 0)), f'{s.get("rtt_medio_ms", 0)} / {s.get("rtt_max_ms", 0)} ms',
                           _f_n(tt.get("pacotes")), _f_n(tt.get("perdidos")), _f_n(tt.get("retransmitidos")),
                           _f_n(tt.get("descartados"))])
        E += [Paragraph("CONEXÕES SRT", s_h2),
              tabela(["Conexão", "Endereço", "Conexões", "RTT méd./máx.", "Pacotes", "Perdidos", "Retransm.",
                      "Não recup."], linhas, [.1, .23, .1, .14, .11, .1, .11, .11], direita=(2, 3, 4, 5, 6, 7),
                     vermelhas=verm)]
    if d["saidas"]:
        linhas, verm = [], set()
        for n, (c, st) in enumerate(d["saidas"], 1):
            st = st or {}
            if st.get("reinicios"):
                verm.add(n)
            linhas.append([f"Destino {n}", _desc_saida(c), f'{(st.get("enviado_bytes") or 0) / 1e6:.1f} MB',
                           _f_dur(st.get("tempo_no_ar_s")), str(st.get("conexoes", 0)), str(st.get("reinicios", 0))])
        E += [Paragraph("SAÍDAS", s_h2),
              tabela(["Destino", "Configuração", "Enviado", "Tempo no ar", "Conexões", "Quedas"], linhas,
                     [.11, .45, .12, .12, .1, .1], direita=(2, 3, 4, 5), vermelhas=verm)]
    itens, eventos = d.get("placas") or ([], [])
    if itens:
        verm = {n for n, it in enumerate(itens, 1) if it["ativa"] != it["principal"]}
        E += [Paragraph("PLACAS DE REDE (RESERVA AUTOMÁTICA)", s_h2),
              tabela(["Onde", "Principal", "Reserva", "No fim da sessão", "Trocas"],
                     [[it["onde"], it["principal"] or "", it["reserva"] or "",
                       "na reserva" if it["ativa"] != it["principal"] else "na principal", str(it["trocas"])] for it in itens],
                     [.16, .2, .2, .3, .14], direita=(4,), vermelhas=verm)]
        if eventos:
            E += [Spacer(1, 5), tabela(["Quando", "Onde", "De -> para", "Motivo"],
                                      [[_f_dh(ev.get("em")), ev["onde"], f'{ev.get("de")} -> {ev.get("para")}', ev.get("motivo") or ""]
                                       for ev in eventos], [.2, .14, .2, .46])]

    rodape = tx(f'{d["titulo"]} · {d["canal"]} · gerado em {_f_dh(d["gerado"])} · asi2srt {__version__}')

    def pagina(cv, doc):
        cv.saveState()
        cv.setFont("Helvetica", 7)
        cv.setFillColor(CINZA)
        cv.drawString(15 * mm, 9 * mm, rodape)
        cv.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Página {doc.page}")
        cv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm,
                            bottomMargin=16 * mm, title=tx(f"Relatório — {d['canal']}"), author=tx(d["titulo"]))
    doc.build(E, onFirstPage=pagina, onLaterPages=pagina)
    return buf.getvalue()


# ======================================================================
# Usuários do painel e sessões (tela de login)
# ======================================================================
PAPEIS = {"admin": "Administrador", "operador": "Operador"}
SESSAO_OCIOSA_S = 12 * 3600          # sessão expira após 12 h sem uso
SESSAO_MAX_S = 30 * 24 * 3600        # e no máximo 30 dias após o login
COOKIE_SESSAO = "asi2srt_sessao"
RE_USUARIO = re.compile(r"^[A-Za-z0-9._@-]{2,40}$")


def hash_senha(senha: str, iteracoes: int = 120_000) -> str:
    sal = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, iteracoes)
    return f"pbkdf2_sha256${iteracoes}${sal.hex()}${h.hex()}"


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        alg, it, sal, h = str(guardado).split("$")
        if alg != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


class Acesso:
    """Usuários (no arquivo de configuração, com senha em hash), sessões e bloqueio por tentativas."""

    MAX_FALHAS = 5
    JANELA_FALHAS_S = 600
    BLOQUEIO_S = 300

    def __init__(self, ger):
        self.ger = ger
        self.lock = threading.RLock()
        self.sessoes = {}       # sha256(token) -> {usuario, criada, uso}
        self.falhas = {}        # ip -> [instantes]
        self.bloqueado = {}     # ip -> até quando
        self._sujo = False
        self.arq = (os.path.splitext(ger.caminho)[0] + "-sessoes.json") if ger.caminho else None
        self._falso = hash_senha(secrets.token_hex(8))
        self._cache_basic = {}  # (usuario, sha256(senha)) -> válido até (evita PBKDF2 a cada requisição de script)
        self._migrar()
        self._carregar_sessoes()

    # --- usuários -----------------------------------------------------
    @property
    def usuarios(self):
        return self.ger.sup.setdefault("usuarios", [])

    def _migrar(self):
        """Converte o login antigo (usuario/senha_painel em texto) para a lista de usuários com hash.
        Também serve para recuperar o acesso: 'senha_painel: NOVA' no arquivo redefine a senha."""
        sup = self.ger.sup
        mudou = False
        plano = sup.pop("senha_painel", None)
        nome = str(sup.pop("usuario", None) or "admin")
        if not self.usuarios:
            self.usuarios.append({"usuario": nome, "nome": "Administrador", "papel": "admin",
                                  "senha": hash_senha(str(plano or "admin"))})
            mudou = True
        elif plano:
            u = self._achar(nome)
            if u:
                u["senha"] = hash_senha(str(plano))
                u["papel"] = "admin"
            else:
                self.usuarios.append({"usuario": nome, "nome": nome, "papel": "admin", "senha": hash_senha(str(plano))})
            log.warning("senha do usuário '%s' redefinida pelo arquivo de configuração", nome)
            mudou = True
        if mudou:
            self.ger.persistir()
        if any(conferir_senha("admin", u.get("senha", "")) for u in self.usuarios):
            log.warning("há usuário com a senha padrão 'admin' - troque pelo painel (menu do usuário > Trocar senha)")

    def _achar(self, usuario):
        u = str(usuario or "").lower()
        return next((x for x in self.usuarios if x["usuario"].lower() == u), None)

    @staticmethod
    def publico(u):
        return {"usuario": u["usuario"], "nome": u.get("nome") or u["usuario"], "papel": u.get("papel", "operador"),
                "papel_nome": PAPEIS.get(u.get("papel"), "Operador"), "criado": u.get("criado"),
                "ultimo_acesso": u.get("ultimo_acesso")}

    def listar(self):
        with self.lock:
            return [self.publico(u) for u in self.usuarios]

    def senha_padrao(self, usuario):
        u = self._achar(usuario)
        return bool(u and conferir_senha("admin", u.get("senha", "")))

    @staticmethod
    def _validar_senha(s):
        s = str(s or "")
        if len(s) < 6:
            raise ValueError("a senha precisa ter pelo menos 6 caracteres")
        if len(s) > 128:
            raise ValueError("senha longa demais")
        return s

    def _admins(self):
        return [u for u in self.usuarios if u.get("papel") == "admin"]

    def salvar(self, dados, existente=None):
        papel = dados.get("papel", "operador")
        if papel not in PAPEIS:
            raise ValueError("perfil inválido (admin ou operador)")
        nome = str(dados.get("nome") or "").strip()[:60]
        with self.lock:
            if existente is None:
                usuario = str(dados.get("usuario") or "").strip()
                if not RE_USUARIO.match(usuario):
                    raise ValueError("usuário: 2 a 40 caracteres (letras, números, ponto, hífen, _ ou @)")
                if self._achar(usuario):
                    raise ValueError("já existe um usuário com esse nome")
                u = {"usuario": usuario, "nome": nome or usuario, "papel": papel,
                     "senha": hash_senha(self._validar_senha(dados.get("senha"))), "criado": int(time.time())}
                self.usuarios.append(u)
            else:
                u = self._achar(existente)
                if not u:
                    raise KeyError(existente)
                if u.get("papel") == "admin" and papel != "admin" and len(self._admins()) <= 1:
                    raise ValueError("é preciso ter pelo menos um administrador")
                u["papel"] = papel
                if nome:
                    u["nome"] = nome
                if dados.get("senha"):
                    u["senha"] = hash_senha(self._validar_senha(dados["senha"]))
                    self._encerrar_de(u["usuario"])
            self.ger.persistir()
            return self.publico(u)

    def remover(self, usuario, por):
        with self.lock:
            u = self._achar(usuario)
            if not u:
                raise KeyError(usuario)
            if u["usuario"].lower() == str(por).lower():
                raise ValueError("você não pode excluir o próprio usuário")
            if u.get("papel") == "admin" and len(self._admins()) <= 1:
                raise ValueError("é preciso ter pelo menos um administrador")
            self.usuarios.remove(u)
            self._encerrar_de(u["usuario"])
            self.ger.persistir()

    def trocar_senha(self, usuario, atual, nova, token_atual=None):
        with self.lock:
            u = self._achar(usuario)
            if not u or not conferir_senha(str(atual or ""), u.get("senha", "")):
                raise ValueError("senha atual incorreta")
            u["senha"] = hash_senha(self._validar_senha(nova))
            self._encerrar_de(u["usuario"], exceto=token_atual)   # derruba as outras sessões
            self.ger.persistir()

    # --- login e bloqueio ---------------------------------------------
    def espera_bloqueio(self, ip):
        with self.lock:
            ate = self.bloqueado.get(ip, 0)
            return max(0, int(ate - time.time()))

    def autenticar(self, usuario, senha, ip):
        """Devolve o usuário ou None. Bloqueia o IP após várias senhas erradas."""
        agora = time.time()
        with self.lock:
            if self.bloqueado.get(ip, 0) > agora:
                return None
            u = self._achar(usuario)
            guardado = u.get("senha", "") if u else self._falso
        ok = conferir_senha(str(senha or ""), guardado) and u is not None   # hash fora do lock (é lento)
        with self.lock:
            if ok:
                self.falhas.pop(ip, None)
                u["ultimo_acesso"] = int(agora)
                return u
            lst = [t for t in self.falhas.get(ip, []) if agora - t < self.JANELA_FALHAS_S] + [agora]
            self.falhas[ip] = lst
            if len(lst) >= self.MAX_FALHAS:
                self.bloqueado[ip] = agora + self.BLOQUEIO_S
                self.falhas.pop(ip, None)
                log.warning("painel: %d senhas erradas de %s - bloqueado por %d min", len(lst), ip, self.BLOQUEIO_S // 60)
        return None

    def autenticar_basic(self, usuario, senha, ip):
        """Login por cabeçalho (scripts, monitoração externa, curl -u)."""
        chave = (str(usuario).lower(), hashlib.sha256(str(senha).encode()).hexdigest())
        agora = time.time()
        with self.lock:
            if self._cache_basic.get(chave, 0) > agora:
                u = self._achar(usuario)
                if u:
                    return u
        u = self.autenticar(usuario, senha, ip)
        if u:
            with self.lock:
                if len(self._cache_basic) > 200:
                    self._cache_basic.clear()
                self._cache_basic[chave] = agora + 60
        return u

    # --- sessões --------------------------------------------------------
    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def criar_sessao(self, u):
        token = secrets.token_urlsafe(32)
        agora = time.time()
        with self.lock:
            self.sessoes[self._h(token)] = {"usuario": u["usuario"], "criada": agora, "uso": agora}
            self._sujo = True
        self.gravar_sessoes()
        self.ger.persistir()     # guarda o último acesso
        return token

    def sessao(self, token):
        if not token:
            return None
        agora = time.time()
        with self.lock:
            s = self.sessoes.get(self._h(token))
            if not s:
                return None
            u = self._achar(s["usuario"])
            if not u or agora - s["uso"] > SESSAO_OCIOSA_S or agora - s["criada"] > SESSAO_MAX_S:
                self.sessoes.pop(self._h(token), None)
                self._sujo = True
                return None
            if agora - s["uso"] > 60:
                self._sujo = True
            s["uso"] = agora
            return u

    def encerrar(self, token):
        with self.lock:
            self.sessoes.pop(self._h(token or ""), None)
            self._sujo = True
        self.gravar_sessoes()

    def _encerrar_de(self, usuario, exceto=None):
        hx = self._h(exceto) if exceto else None
        with self.lock:
            self._cache_basic.clear()
            for k in [k for k, s in self.sessoes.items() if s["usuario"].lower() == usuario.lower() and k != hx]:
                del self.sessoes[k]
            self._sujo = True
        self.gravar_sessoes()

    def _carregar_sessoes(self):
        if not self.arq or not os.path.exists(self.arq):
            return
        try:
            with open(self.arq, encoding="utf-8") as f:
                self.sessoes = {k: v for k, v in (json.load(f) or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.sessoes = {}

    def gravar_sessoes(self, forcar=True):
        """Sessões sobrevivem a reinícios do serviço (só o hash do token vai para o disco)."""
        if not self.arq or not (forcar or self._sujo):
            return
        with self.lock:
            agora = time.time()
            self.sessoes = {k: s for k, s in self.sessoes.items()
                            if agora - s["uso"] <= SESSAO_OCIOSA_S and agora - s["criada"] <= SESSAO_MAX_S}
            dados = json.dumps(self.sessoes)
            self._sujo = False
        try:
            tmp = self.arq + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(dados)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.arq)
        except OSError as e:
            log.error("não foi possível salvar as sessões: %s", e)


# ícone da aba do navegador quando não há logo: antena transmitindo, nas cores do painel
ICONE_PADRAO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0b0f14"/>'
    '<g fill="none" stroke="#00a859" stroke-width="4.5" stroke-linecap="round">'
    '<path d="M20 22a17 17 0 0 0 0 20"/><path d="M44 22a17 17 0 0 1 0 20"/>'
    '<path d="M12 15a28 28 0 0 0 0 34" opacity=".6"/><path d="M52 15a28 28 0 0 1 0 34" opacity=".6"/></g>'
    '<circle cx="32" cy="32" r="6" fill="#00a859"/><path d="M32 38l-7 18h14z" fill="#2f81f7"/></svg>')

LOGIN_HTML = r"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar · __TITULO__</title>
<link rel="icon" href="/favicon.ico?v=__ICONV__">
<meta name="robots" content="noindex">
<style>
:root{--bg:#0b0f14;--card:#121821;--line:#232c37;--txt:#e6edf3;--mut:#8b96a3;--acc:#2f81f7;--err:#f85149;--ok:#00a859}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#060a0f;color:var(--txt);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 display:flex;align-items:center;justify-content:center;padding:16px;overflow:hidden;position:relative}
/* fundo: brilho da marca, ondas de sinal e grade de estúdio */
.fundo{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.fundo .luz{position:absolute;border-radius:50%;filter:blur(70px);opacity:.9}
.fundo .l1{width:55vmax;height:55vmax;left:-12vmax;top:-22vmax;background:radial-gradient(circle,#00a85980,transparent 62%);animation:flut 18s ease-in-out infinite alternate}
.fundo .l2{width:50vmax;height:50vmax;right:-15vmax;bottom:-20vmax;background:radial-gradient(circle,#1f6feb70,transparent 62%);animation:flut 22s ease-in-out infinite alternate-reverse}
.fundo .l3{width:30vmax;height:30vmax;right:8vmax;top:6vmax;background:radial-gradient(circle,#00a85922,transparent 70%);animation:flut 26s ease-in-out infinite alternate}
@keyframes flut{to{transform:translate(4vmax,3vmax) scale(1.08)}}
.fundo .grade{position:absolute;left:-50%;right:-50%;bottom:0;height:48vh;
 background-image:linear-gradient(#3fb9806b 1px,transparent 1px),linear-gradient(90deg,#2f81f75c 1px,transparent 1px);
 background-size:56px 56px;transform:perspective(420px) rotateX(62deg);transform-origin:50% 100%;
 -webkit-mask-image:linear-gradient(to top,#000 25%,transparent 95%);mask-image:linear-gradient(to top,#000 25%,transparent 95%);
 animation:anda 9s linear infinite}
@keyframes anda{to{background-position:0 56px,0 0}}
.fundo svg{position:absolute;left:0;top:50%;width:100%;height:46vh;transform:translateY(-58%);opacity:.9}
.fundo path{fill:none;stroke-linecap:round;vector-effect:non-scaling-stroke}
.fundo .o1{stroke:#00c46a;stroke-width:2;opacity:.75;filter:drop-shadow(0 0 4px #00c46a);stroke-dasharray:6 10;animation:corre 14s linear infinite}
.fundo .o2{stroke:#4c9bff;stroke-width:1.5;opacity:.6;animation:corre 20s linear infinite reverse;stroke-dasharray:2 7}
.fundo .o3{stroke:#7ee2a8;stroke-width:1.2;opacity:.35}
.fundo .o4{stroke:#9ec5ff;stroke-width:1;opacity:.25}
@keyframes corre{to{stroke-dashoffset:-320}}
.fundo .scan{position:absolute;inset:0;background:repeating-linear-gradient(0deg,#ffffff05 0 1px,transparent 1px 3px)}
.fundo .vin{position:absolute;inset:0;background:radial-gradient(ellipse at center,transparent 55%,#060a0f99 100%)}
@media(prefers-reduced-motion:reduce){.fundo *{animation:none!important}}
.caixa{position:relative;z-index:1;width:min(400px,100%);background:#121821cc;border:1px solid #2a3440;border-radius:16px;padding:30px 28px 24px;
 box-shadow:0 30px 80px #000a,0 0 0 1px #ffffff08 inset;border-top:3px solid var(--ok);
 -webkit-backdrop-filter:blur(14px) saturate(1.2);backdrop-filter:blur(14px) saturate(1.2)}
.aovivo{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;letter-spacing:.12em;color:#7ee2a8;
 background:#00a8591a;border:1px solid #00a85955;border-radius:99px;padding:3px 10px 3px 8px}
.aovivo i{width:7px;height:7px;border-radius:50%;background:#00d46a;box-shadow:0 0 0 0 #00d46a88;animation:pul 2s infinite}
@keyframes pul{70%{box-shadow:0 0 0 7px #00d46a00}100%{box-shadow:0 0 0 0 #00d46a00}}
.topo{display:flex;flex-direction:column;align-items:center;gap:12px;margin-bottom:22px;text-align:center}
.topo img{max-height:64px;max-width:200px}
.topo h1{font-size:19px;margin:0;font-weight:650}
.topo p{margin:0;color:var(--mut);font-size:13px}
label{display:block;font-size:12px;color:var(--mut);margin:0 0 14px;text-transform:uppercase;letter-spacing:.04em}
input{display:block;width:100%;margin-top:6px;padding:11px 12px;font:inherit;font-size:15px;color:var(--txt);background:#0a0e13d9;
 border:1px solid var(--line);border-radius:9px;outline:none;text-transform:none;letter-spacing:0}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px #2f81f733}
.senha{position:relative}.senha input{padding-right:64px}
.ver{position:absolute;right:6px;bottom:6px;background:none;border:0;color:var(--mut);font:inherit;font-size:12px;cursor:pointer;padding:6px 8px;border-radius:6px}
.ver:hover{color:var(--txt);background:#ffffff0d}
button.ent{width:100%;margin-top:6px;padding:11px;font:inherit;font-size:15px;font-weight:600;color:#fff;background:var(--acc);border:0;border-radius:9px;cursor:pointer}
button.ent:hover{filter:brightness(1.08)}button.ent:disabled{opacity:.6;cursor:default}
.erro{min-height:20px;color:var(--err);font-size:13px;margin:10px 0 0;text-align:center}
.rod{margin-top:18px;color:var(--mut);font-size:11.5px;text-align:center}
</style></head><body>
<div class="fundo" aria-hidden="true"><div class="luz l1"></div><div class="luz l2"></div><div class="luz l3"></div>
<div class="grade"></div><svg viewBox="0 0 1200 200" preserveAspectRatio="none" aria-hidden="true"><path class="o3" d="M0,157.6 L13,159.7 L27,159.9 L40,158.4 L53,155.4 L67,151.0 L80,145.6 L93,139.3 L107,132.6 L120,125.7 L133,118.9 L147,112.5 L160,106.6 L173,101.4 L187,97.0 L200,93.4 L213,90.6 L227,88.6 L240,87.2 L253,86.4 L267,85.9 L280,85.8 L293,85.8 L307,85.9 L320,86.0 L333,86.2 L347,86.4 L360,86.8 L373,87.3 L387,88.2 L400,89.5 L413,91.3 L427,93.8 L440,97.1 L453,101.1 L467,105.9 L480,111.3 L493,117.3 L507,123.8 L520,130.3 L533,136.8 L547,142.9 L560,148.4 L573,152.8 L587,156.0 L600,157.8 L613,157.8 L627,156.2 L640,152.7 L653,147.5 L667,140.7 L680,132.4 L693,123.0 L707,112.8 L720,102.2 L733,91.5 L747,81.1 L760,71.4 L773,62.8 L787,55.5 L800,49.7 L813,45.7 L827,43.5 L840,43.0 L853,44.1 L867,46.8 L880,50.8 L893,55.9 L907,61.7 L920,68.0 L933,74.4 L947,80.7 L960,86.8 L973,92.3 L987,97.2 L1000,101.3 L1013,104.8 L1027,107.5 L1040,109.5 L1053,111.0 L1067,112.0 L1080,112.7 L1093,113.2 L1107,113.6 L1120,113.9 L1133,114.2 L1147,114.4 L1160,114.6 L1173,114.6 L1187,114.3 L1200,113.7"/><path class="o4" d="M0,106.0 L10,104.7 L20,103.2 L30,101.6 L40,99.9 L50,98.0 L60,96.1 L70,94.2 L80,92.3 L90,90.5 L100,88.9 L110,87.7 L120,87.0 L130,86.8 L140,87.4 L150,88.8 L160,90.9 L170,93.9 L180,97.7 L190,102.0 L200,106.8 L210,111.8 L220,116.7 L230,121.2 L240,125.0 L250,127.9 L260,129.6 L270,129.9 L280,128.6 L290,125.8 L300,121.5 L310,115.8 L320,109.0 L330,101.5 L340,93.5 L350,85.6 L360,78.1 L370,71.5 L380,66.1 L390,62.3 L400,60.3 L410,60.2 L420,62.1 L430,65.7 L440,71.0 L450,77.6 L460,85.1 L470,93.2 L480,101.4 L490,109.3 L500,116.5 L510,122.5 L520,127.2 L530,130.3 L540,131.8 L550,131.7 L560,130.1 L570,127.1 L580,123.1 L590,118.3 L600,113.0 L610,107.6 L620,102.3 L630,97.4 L640,93.2 L650,89.8 L660,87.3 L670,85.7 L680,85.0 L690,85.2 L700,86.0 L710,87.4 L720,89.2 L730,91.2 L740,93.4 L750,95.7 L760,97.8 L770,99.8 L780,101.7 L790,103.3 L800,104.7 L810,106.0 L820,107.0 L830,107.8 L840,108.3 L850,108.6 L860,108.5 L870,108.1 L880,107.2 L890,105.9 L900,104.2 L910,101.9 L920,99.2 L930,96.2 L940,92.9 L950,89.5 L960,86.2 L970,83.2 L980,80.8 L990,79.0 L1000,78.3 L1010,78.6 L1020,80.1 L1030,82.9 L1040,86.8 L1050,91.8 L1060,97.7 L1070,104.2 L1080,111.0 L1090,117.7 L1100,123.9 L1110,129.3 L1120,133.5 L1130,136.2 L1140,137.3 L1150,136.5 L1160,133.9 L1170,129.5 L1180,123.6 L1190,116.5 L1200,108.4"/><path class="o1" d="M0,108.3 L15,114.0 L30,120.1 L45,126.2 L60,132.2 L75,137.9 L90,143.0 L105,147.3 L120,150.8 L135,153.3 L150,154.6 L165,154.8 L180,153.8 L195,151.7 L210,148.6 L225,144.6 L240,139.8 L255,134.5 L270,128.8 L285,122.9 L300,117.1 L315,111.6 L330,106.4 L345,101.7 L360,97.7 L375,94.3 L390,91.7 L405,89.7 L420,88.3 L435,87.6 L450,87.2 L465,87.3 L480,87.5 L495,87.9 L510,88.3 L525,88.7 L540,88.9 L555,89.1 L570,89.1 L585,89.0 L600,88.9 L615,88.9 L630,89.0 L645,89.3 L660,90.1 L675,91.3 L690,93.1 L705,95.5 L720,98.6 L735,102.3 L750,106.6 L765,111.4 L780,116.7 L795,122.2 L810,127.9 L825,133.4 L840,138.7 L855,143.4 L870,147.5 L885,150.8 L900,153.0 L915,154.2 L930,154.2 L945,153.1 L960,150.8 L975,147.5 L990,143.2 L1005,138.2 L1020,132.5 L1035,126.4 L1050,120.1 L1065,113.9 L1080,107.8 L1095,102.2 L1110,97.1 L1125,92.6 L1140,88.9 L1155,86.1 L1170,84.0 L1185,82.6 L1200,82.0"/><path class="o2" d="M0,77.4 L12,74.0 L24,72.0 L36,71.2 L48,71.8 L60,73.7 L72,76.7 L84,80.6 L96,85.1 L108,90.1 L120,95.1 L132,99.9 L144,104.4 L156,108.2 L168,111.2 L180,113.3 L192,114.6 L204,115.0 L216,114.7 L228,113.7 L240,112.1 L252,110.3 L264,108.2 L276,106.0 L288,103.9 L300,102.0 L312,100.3 L324,98.8 L336,97.5 L348,96.4 L360,95.6 L372,94.9 L384,94.4 L396,94.0 L408,93.7 L420,93.7 L432,93.8 L444,94.2 L456,94.9 L468,96.0 L480,97.5 L492,99.3 L504,101.6 L516,104.1 L528,106.9 L540,109.8 L552,112.6 L564,115.1 L576,117.2 L588,118.6 L600,119.2 L612,118.9 L624,117.5 L636,115.1 L648,111.7 L660,107.5 L672,102.5 L684,97.1 L696,91.4 L708,85.9 L720,80.8 L732,76.5 L744,73.1 L756,70.9 L768,70.1 L780,70.6 L792,72.5 L804,75.6 L816,79.8 L828,84.9 L840,90.5 L852,96.4 L864,102.2 L876,107.6 L888,112.3 L900,116.2 L912,119.1 L924,120.9 L936,121.5 L948,121.1 L960,119.7 L972,117.6 L984,114.8 L996,111.7 L1008,108.4 L1020,105.2 L1032,102.1 L1044,99.4 L1056,97.1 L1068,95.3 L1080,94.0 L1092,93.2 L1104,92.8 L1116,92.7 L1128,93.0 L1140,93.4 L1152,94.0 L1164,94.8 L1176,95.6 L1188,96.6 L1200,97.6"/></svg><div class="scan"></div><div class="vin"></div></div>
<main class="caixa">
 <div class="topo">__LOGO__<h1>__TITULO__</h1><p>Acesso ao sistema de monitoração e distribuição de sinais</p><span class="aovivo"><i></i>ASI · SDI · PROTOCOLOS IP</span></div>
 <form id="f" autocomplete="on">
  <label>Usuário<input name="usuario" id="u" autocomplete="username" autocapitalize="off" spellcheck="false" required autofocus></label>
  <label class="senha">Senha<input name="senha" id="s" type="password" autocomplete="current-password" required>
   <button type="button" class="ver" id="ver" aria-label="mostrar senha">mostrar</button></label>
  <button class="ent" id="b" type="submit">Entrar</button>
  <p class="erro" id="e" role="alert">__MSG__</p>
 </form>
 <p class="rod">Acesso restrito · __VERSAO__</p>
</main>
<script>
const f=document.getElementById('f'),b=document.getElementById('b'),e=document.getElementById('e'),s=document.getElementById('s');
document.getElementById('ver').onclick=ev=>{const v=s.type==='password';s.type=v?'text':'password';ev.target.textContent=v?'ocultar':'mostrar'};
f.onsubmit=async ev=>{ev.preventDefault();b.disabled=true;e.textContent='';
 try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Asi2srt':'1'},
   body:JSON.stringify({usuario:f.usuario.value.trim(),senha:f.senha.value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){let v=new URLSearchParams(location.search).get('volta')||('/'+location.hash);if(!v.startsWith('/')||v.startsWith('//'))v='/';location.replace(v);return}
  e.textContent=j.erro||'Não foi possível entrar';s.select()}
 catch(x){e.textContent='Sem conexão com o servidor'}
 b.disabled=false};
</script></body></html>
"""


# ======================================================================
# Servidor web: painel + API
# ======================================================================
def servidor_web(ger: Gerenciador, endereco: str):
    host, _, porta = endereco.rpartition(":")
    acesso = ger.garantir_senha()

    class H(BaseHTTPRequestHandler):
        server_version = "asi2srt/" + __version__

        def log_message(self, *a):
            pass

        def _ip(self):
            return self.client_address[0]

        def _token(self):
            try:
                m = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE_SESSAO)
            except Exception:  # noqa: BLE001
                return None
            return m.value if m else None

        def _quem(self):
            """Usuário logado: sessão (cookie da tela de login) ou cabeçalho Basic (scripts/monitoração)."""
            u = acesso.sessao(self._token())
            if u:
                return u
            cab = self.headers.get("Authorization", "")
            if cab.startswith("Basic "):
                try:
                    us, _, s = base64.b64decode(cab[6:]).decode().partition(":")
                except Exception:  # noqa: BLE001
                    return None
                return acesso.autenticar_basic(us, s, self._ip())
            return None

        def _redir(self, destino, cookie=None):
            self.send_response(302)
            self.send_header("Location", destino)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _pagina_login(self):
            ap = ger.aparencia()
            logo = f'<img src="/logo?v={ap.get("logo_v", "")}" alt="">' if ger.logo_arquivo() else ""
            pag = (LOGIN_HTML.replace("__TITULO__", html.escape(ap["titulo"])).replace("__LOGO__", logo)
                   .replace("__ICONV__", str(ap.get("logo_v", 0)))
                   .replace("__VERSAO__", "v" + __version__).replace("__MSG__", ""))
            return self._resp(200, pag.encode(), "text/html; charset=utf-8",
                              cab={"X-Frame-Options": "DENY", "Content-Security-Policy":
                                   "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'"})

        def _login(self):
            ip = self._ip()
            espera = acesso.espera_bloqueio(ip)
            if espera:
                return self._resp(429, {"erro": f"Muitas tentativas erradas. Aguarde {max(1, round(espera / 60))} min."})
            d = self._corpo()
            u = acesso.autenticar(d.get("usuario", ""), d.get("senha", ""), ip)
            if not u:
                time.sleep(0.5)
                espera = acesso.espera_bloqueio(ip)
                return self._resp(429 if espera else 401, {"erro": f"Muitas tentativas erradas. Aguarde {max(1, round(espera / 60))} min."
                                                           if espera else "Usuário ou senha incorretos"})
            token = acesso.criar_sessao(u)
            log.info("painel: login de %s (%s) a partir de %s", u["usuario"], u.get("papel"), ip)
            return self._resp(200, acesso.publico(u), cab={"Set-Cookie": f"{COOKIE_SESSAO}={token}; Path=/; HttpOnly; "
                                                                      f"SameSite=Strict; Max-Age={SESSAO_MAX_S}"})

        @staticmethod
        def _so_admin(metodo, caminho, partes):
            if partes[:2] == ["api", "usuarios"]:
                return True
            if caminho == "/api/aparencia" and metodo != "GET":
                return True
            if partes[:2] == ["api", "canais"]:
                return (len(partes) == 2 and metodo == "POST") or (len(partes) == 3 and metodo in ("PUT", "DELETE"))
            return False

        def _resp(self, codigo, corpo, tipo="application/json", baixar=None, cab=None):
            if not isinstance(corpo, bytes):
                corpo = json.dumps(corpo, ensure_ascii=False, indent=2 if baixar else None).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            for k, v in (cab or {}).items():
                self.send_header(k, v)
            if baixar:
                self.send_header("Content-Disposition", f'attachment; filename="{baixar}"')
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)


        def _corpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4 * 1024 * 1024:
                raise ValueError("requisição grande demais")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _rota(self, metodo):
            url = urlparse(self.path)
            caminho = url.path.rstrip("/") or "/"
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            partes = caminho.strip("/").split("/")
            # proteção contra CSRF: ações exigem cabeçalho próprio (e o cookie é SameSite=Strict)
            if metodo != "GET" and self.headers.get("X-Asi2srt") != "1":
                return self._resp(403, {"erro": "cabeçalho X-Asi2srt ausente"})
            try:
                if metodo == "GET" and caminho == "/login":
                    if acesso.sessao(self._token()):
                        return self._redir("/")
                    return self._pagina_login()
                if metodo == "POST" and caminho == "/api/login":
                    return self._login()
                if metodo == "GET" and caminho == "/favicon.ico":
                    cam = ger.logo_arquivo()
                    if cam:
                        ext = os.path.splitext(cam)[1].lower()
                        tipo = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                ".svg": "image/svg+xml", ".webp": "image/webp"}.get(ext, "image/png")
                        with open(cam, "rb") as f:
                            return self._resp(200, f.read(), tipo, cab={"Cache-Control": "max-age=86400"})
                    return self._resp(200, ICONE_PADRAO.encode(), "image/svg+xml", cab={"Cache-Control": "max-age=86400"})
                u = None if (metodo == "GET" and caminho == "/logo") else self._quem()
                if u is None and caminho != "/logo":
                    if metodo == "GET" and (caminho == "/" or caminho.startswith("/analise")):
                        volta = "" if self.path in ("", "/") else "?volta=" + quote(self.path, safe="")
                        return self._redir("/login" + volta)
                    if acesso.espera_bloqueio(self._ip()):
                        return self._resp(429, {"erro": "muitas tentativas erradas; aguarde alguns minutos"})
                    return self._resp(401, {"erro": "login necessário"})
                if caminho == "/api/logout" and metodo == "POST":
                    acesso.encerrar(self._token())
                    return self._resp(200, {"ok": True}, cab={"Set-Cookie": f"{COOKIE_SESSAO}=; Path=/; HttpOnly; "
                                                                             "SameSite=Strict; Max-Age=0"})
                if caminho == "/api/eu" and metodo == "GET":
                    return self._resp(200, {**acesso.publico(u), "senha_padrao": acesso.senha_padrao(u["usuario"])})
                if caminho == "/api/eu/senha" and metodo == "PUT":
                    d = self._corpo()
                    acesso.trocar_senha(u["usuario"], d.get("atual"), d.get("nova"), self._token())
                    return self._resp(200, {"ok": True})
                if u is not None and u.get("papel") != "admin" and self._so_admin(metodo, caminho, partes):
                    return self._resp(403, {"erro": "apenas administradores podem fazer isso"})
                if partes[:2] == ["api", "usuarios"]:
                    if len(partes) == 2 and metodo == "GET":
                        return self._resp(200, acesso.listar())
                    if len(partes) == 2 and metodo == "POST":
                        return self._resp(201, acesso.salvar(self._corpo()))
                    if len(partes) == 3:
                        try:
                            if metodo == "PUT":
                                return self._resp(200, acesso.salvar(self._corpo(), partes[2]))
                            if metodo == "DELETE":
                                acesso.remover(partes[2], u["usuario"])
                                return self._resp(200, {"ok": True})
                        except KeyError:
                            return self._resp(404, {"erro": "usuário não encontrado"})
                    return self._resp(404, {"erro": "rota não encontrada"})
                if metodo == "GET" and (caminho == "/" or caminho.startswith("/analise")):
                    titulo = html.escape(ger.aparencia()["titulo"])
                    return self._resp(200, PAINEL_HTML.replace("__VERSAO__", __version__)
                                      .replace("__TITULO__", titulo)
                                      .replace("__ICONV__", str(ger.aparencia().get("logo_v", 0))).encode(),
                                      "text/html; charset=utf-8")
                if metodo == "GET" and caminho == "/logo":
                    cam = ger.logo_arquivo()
                    if not cam:
                        return self._resp(404, {"erro": "sem logo"})
                    ext = os.path.splitext(cam)[1].lower()
                    tipo = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".svg": "image/svg+xml", ".webp": "image/webp"}.get(ext, "application/octet-stream")
                    with open(cam, "rb") as f:
                        corpo = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", tipo)
                    self.send_header("Content-Length", str(len(corpo)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    # um SVG aberto direto não pode executar scripts no painel
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                    self.end_headers()
                    self.wfile.write(corpo)
                    return
                if metodo == "GET" and caminho == "/api/interfaces":
                    return self._resp(200, listar_interfaces())
                if metodo == "GET" and caminho == "/api/rede":
                    uso = {}
                    for c in ger.defs.values():
                        c = normalizar_canal(c)
                        for rot, it in [("entrada", c["entrada"])] + [(f"destino {i + 1}", s) for i, s in enumerate(c["saidas"])]:
                            for k, papel in (("interface", "principal"), ("interface_reserva", "reserva")):
                                if it.get(k):
                                    uso.setdefault(it[k], []).append({"canal": c["nome"], "onde": rot, "papel": papel})
                    return self._resp(200, {"interfaces": listar_interfaces(todas=q.get("todas") == "1"),
                                            "sem_driver": placas_sem_driver(), "uso": uso})
                if caminho == "/api/aparencia":
                    if metodo == "GET":
                        return self._resp(200, ger.aparencia())
                    if metodo == "PUT":
                        return self._resp(200, ger.salvar_aparencia(self._corpo()))
                if metodo == "GET" and caminho == "/status.json":
                    return self._resp(200, [{"nome": c["nome"], "id": c["id"],
                                             **{k: v for k, v in c["status"].items() if k != "historico"}}
                                            for c in ger.listar()])
                if metodo == "GET" and caminho == "/api/info":
                    ap = ger.aparencia()
                    return self._resp(200, {"versao": __version__, "config": ger.caminho,
                                            "persistente": bool(ger.caminho), "tr101290": TR_ITENS,
                                            "plataforma": platform.machine(), "dektec": DEKTEC_DISPONIVEL,
                                            "aparencia": ap})
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
                        if metodo == "GET" and acao == "relatorios":
                            return self._resp(200, [{"sessao": i, **ger.resumo_relatorio(r)}
                                                    for i, r in enumerate(ger.relatorios.get(cid) or [])])
                        if metodo == "GET" and acao == "analise":
                            t = ger.exec.get(cid)
                            if "sessao" in q or not t:
                                dados = ger.relatorio_salvo(cid, int(q.get("sessao", 0)))
                                if dados is None:
                                    return self._resp(409, {"erro": "canal parado e sem análise salva"})
                            else:
                                agora_w = time.time()
                                dados = {**t.analisador.analise(), "estado": t.status()["estado"], "srt": t.srt_status(),
                                         "saidas": [p.status() for p in t.procs_saida],
                                         "saidas_cfg": saidas_publicas(normalizar_canal(ger.defs[cid])["saidas"]),
                                         "salvo": False, "taxa_sessao": t.analisador.taxa_sessao(), "rede": t.rede_status(),
                                         "periodo": {"inicio": t.inicio_sessao, "fim": agora_w,
                                                     "duracao_s": int(agora_w - t.inicio_sessao)}}
                            if q.get("baixar"):
                                nome = re.sub(r"[^\w.-]+", "_", ger.defs[cid]["nome"])
                                data = time.strftime("%Y%m%d-%H%M", time.localtime(dados["periodo"]["inicio"]))
                                fmt = q.get("formato", "json")
                                if fmt == "pdf" and not pdf_disponivel():
                                    return self._resp(503, {"erro": "PDF indisponível: instale com "
                                                                    "sudo apt install python3-reportlab"})
                                if fmt in ("html", "txt", "pdf"):
                                    canal = {**normalizar_canal(ger.defs[cid])}
                                    d = dados_relatorio(dados, canal, ger.aparencia()["titulo"])
                                    if fmt == "txt":
                                        corpo = relatorio_txt(d).encode("utf-8")
                                        return self._resp(200, corpo, "text/plain; charset=utf-8",
                                                          baixar=f"relatorio-{nome}-{data}.txt")
                                    logo = None
                                    cam = ger.logo_arquivo()
                                    if cam:
                                        ext = os.path.splitext(cam)[1].lower()
                                        tipo = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                                ".webp": "image/webp"}.get(ext)
                                        if tipo:   # SVG fica de fora do HTML exportado (não executa nada)
                                            with open(cam, "rb") as f:
                                                logo = (tipo, f.read())
                                    if fmt == "pdf":
                                        return self._resp(200, relatorio_pdf(d, logo), "application/pdf",
                                                          baixar=f"relatorio-{nome}-{data}.pdf")
                                    corpo = relatorio_html(d, logo).encode("utf-8")
                                    return self._resp(200, corpo, "text/html; charset=utf-8",
                                                      baixar=f"relatorio-{nome}-{data}.html")
                                return self._resp(200, dados, baixar=f"relatorio-{nome}-{data}.json")
                            return self._resp(200, dados)
                        if metodo == "GET" and acao == "comando":
                            c = normalizar_canal(ger.defs[cid])
                            tspb = ger.sup.get("tsp", "tsp")
                            return self._resp(200, {"comando": comando_para_log(montar_comando(c, tspb)),
                                                    "saidas": [comando_para_log(montar_saida(s, i, tspb))
                                                               for i, s in enumerate(c["saidas"])]})
                        if metodo == "POST" and acao in ("iniciar", "parar", "reiniciar", "zerar"):
                            getattr(ger, acao)(cid)
                            return self._resp(200, {"ok": True})
                    if len(partes) == 5 and metodo == "GET" and partes[3] == "previa":
                        t = ger.exec.get(cid)
                        sid = partes[4].removesuffix(".jpg")
                        if not t or not sid.isdigit():
                            return self._resp(404, {"erro": "canal parado"})
                        t.previa.pedir()
                        img = t.previa.imagem(sid)
                        if not img:
                            return self._resp(404, {"erro": t.previa.motivo or "aguardando imagem"})
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(img)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(img)
                        return
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
    log.info("painel: http://%s:%s/  (%d usuário(s))", host or "0.0.0.0", porta, len(acesso.usuarios))
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
        if not args.entrada:
            raise SystemExit("--saida precisa de --entrada (sem --saida o canal só analisa)")
        canal = {"nome": args.nome, "entrada": entrada_de_texto(args.entrada),
                 "saidas": [saida_de_url(s) for s in (args.saida or [])],
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
            "status_http": "0.0.0.0:8088", "usuario": "admin", "senha_painel": "admin"}})
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
    try:
        for i in listar_interfaces():
            log.info("placa de rede %s: %s, %s%s%s", i["nome"], ", ".join(i["ips"]) or "sem IP",
                     "link OK" if i["link"] else "sem link", f", {i['velocidade_mbps']} Mb/s" if i["velocidade_mbps"] else "",
                     f", {i['modelo']}" if i.get("modelo") else "")
        for pl in placas_sem_driver():
            log.warning("placa de rede no PCI %s (%s) sem driver: não aparece para o sistema", pl["pci"], pl.get("modelo"))
    except Exception:  # noqa: BLE001
        log.exception("listar placas de rede")
    if sup.get("rotas_por_placa"):
        for c in aplicar_rotas_por_placa():
            log.info("rotas por placa: %s", c)
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
        print(comando_para_log(montar_comando(c, tsp, porta_analise="PORTA_ANALISE")))
        for i, s in enumerate(c["saidas"]):
            print(f"  saída {i + 1}: " + comando_para_log(montar_saida(s, i, tsp)))
        print()


def cmd_init(args):
    if os.path.exists(args.arquivo) and not args.forcar:
        raise SystemExit(f"{args.arquivo} já existe (use --forcar para sobrescrever)")
    with open(args.arquivo, "w", encoding="utf-8") as f:
        f.write(CONFIG_EXEMPLO)
    print(f"configuração criada: {args.arquivo}\n"
          f"edite e rode: asi2srt rodar -c {args.arquivo}   (ou edite pelo painel web)")


def cmd_verificar(args):
    ok = True
    print(f"Plataforma: {platform.machine()} · {_os_release().get('PRETTY_NAME', platform.system())}"
          + ("" if DEKTEC_DISPONIVEL else "  (placas DekTec não são suportadas nesta arquitetura)"))
    tsp = shutil.which(args.tsp) or (args.tsp if os.path.exists(args.tsp) else None)
    if not tsp:
        print("✗ TSDuck (tsp) não encontrado  ->  sudo asi2srt instalar-tsduck")
        return 1
    ver = subprocess.run([tsp, "--version"], capture_output=True, text=True)
    print(f"✓ TSDuck: {(ver.stdout or ver.stderr).strip().splitlines()[0]}")
    plugins = subprocess.run([tsp, "--list-plugins"], capture_output=True, text=True)
    texto = plugins.stdout + plugins.stderr
    for nome, obrig in (("srt", True), ("ip", True), ("fork", True),
                        ("bitrate_monitor", True), ("filter", True), ("dektec", False)):
        tem = re.search(rf"\b{nome}\b", texto) is not None
        print(f"{'✓' if tem else ('✗' if obrig else '·')} plugin {nome}"
              f"{'' if tem or obrig else ' (só necessário para placas DekTec)'}")
        if nome == "dektec" and not DEKTEC_DISPONIVEL:
            continue
        ok &= tem or not obrig
    placas = detectar_dektec()
    if placas or DEKTEC_DISPONIVEL:
        print(f"\nPlacas DekTec no barramento: {len(placas)}"
              + "".join(f"\n  {p['barramento']} {p['endereco']} {modelo_dektec(p)}" for p in placas))
        if placas:
            print("Driver DekTec: " + ("carregado ✓" if driver_dektec_carregado()
                                       else "NÃO carregado ✗  ->  sudo asi2srt instalar-dektec"))
    if shutil.which("tsdektec"):
        r = subprocess.run(["tsdektec", "-a"], capture_output=True, text=True)
        print("Placas vistas pelo TSDuck:\n" + ((r.stdout or r.stderr).strip() or "  nenhuma encontrada"))
    asi = sorted(f for f in os.listdir("/dev") if f.startswith("asirx")) if os.path.isdir("/dev") else []
    print("\nDispositivos Linux ASI: " + (", ".join("/dev/" + a for a in asi) or "nenhum"))
    print("\nPlacas de rede:")
    for i in listar_interfaces():
        print(f"  {i['nome']:<12} {', '.join(i['ips']) or 'sem IP':<18} {'link OK' if i['link'] else 'sem link':<9}"
              + (f" · {i['velocidade_mbps']} Mb/s" if i['velocidade_mbps'] else "") + f" · {i['tipo']}"
              + (f" · {i['modelo']}" if i.get("modelo") else "") + (f" · driver {i['driver']}" if i.get("driver") else ""))
    for p in placas_sem_driver():
        print(f"  ✗ {p['pci']}  {p.get('modelo') or 'placa de rede'}: detectada no PCI, mas SEM DRIVER (sem interface)")
    print("PyYAML: " + ("instalado" if yaml else "ausente (configuração será salva em JSON)"))
    ff = shutil.which("ffmpeg")
    if ff:
        r = subprocess.run([ff, "-hide_banner", "-protocols"], capture_output=True, text=True)
        tem = "rtmps" in r.stdout
        print(f"✓ ffmpeg (saídas RTMP{'/RTMPS' if tem else ''})")
    else:
        print("· ffmpeg ausente (só necessário para saídas RTMP)  ->  sudo apt install ffmpeg")
    return 0 if ok else 1


def _os_release():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for linha in f:
                k, _, v = linha.strip().partition("=")
                info[k] = v.strip('"')
    except OSError:
        pass
    return info


def _arquitetura_pacote(deb: bool) -> str:
    """Arquitetura do sistema de pacotes (no Raspberry, kernel 64 bits pode ter userland armhf)."""
    if deb and shutil.which("dpkg"):
        r = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    m = platform.machine()
    if deb:
        return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf", "armv6l": "armhf"}.get(m, m)
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def escolher_pacote_tsduck(nomes, info, arq, deb):
    """Escolhe o pacote do TSDuck de uma release: mesma distro e versão > mesma distro >
    distro "pai" (ID_LIKE) > nenhum. Ignora pacotes de desenvolvimento."""
    distro = info.get("ID", "").lower()
    major = info.get("VERSION_ID", "").split(".")[0]
    familia = [distro] + info.get("ID_LIKE", "").lower().split()
    if deb:
        rx = re.compile(r"^tsduck_[\d.\-]+\.([a-z]+)(\d+)_" + re.escape(arq) + r"\.deb$")
    else:
        rx = re.compile(r"^tsduck-[\d.\-]+\.(el|fc)(\d+)\." + re.escape(arq) + r"\.rpm$")
        familia = ["fc" if distro == "fedora" else "el"]
        distro = familia[0]
    cands = []
    for n in nomes:
        m = rx.match(n)
        if not m:
            continue
        d, v = m.group(1), m.group(2)
        if d == distro and v == major:
            nota = 3
        elif d == distro:
            nota = 2
        elif d in familia:
            nota = 1
        else:
            continue
        cands.append((nota, int(v), n))
    return max(cands)[2] if cands else None


def _compilar_tsduck():
    """Compila o TSDuck a partir do código-fonte (sem DekTec, Java e smartcard)."""
    opcoes = ["NOTEST=1", "NODEKTEC=1", "NOJAVA=1", "NOPCSC=1", "NOVATEK=1", "NOHIDES=1"]
    src = "/usr/local/src/tsduck"
    print("compilando o TSDuck a partir do código-fonte.\n"
          "ATENÇÃO: no Raspberry Pi isso leva de 1 a 3 horas; em modelos com 1-2 GB de RAM,\n"
          "aumente a swap antes (sudo dphys-swapfile setup com CONF_SWAPSIZE=2048).")
    if not shutil.which("git"):
        subprocess.run(["apt-get", "install", "-y", "git"], check=True)
    if not os.path.isdir(src):
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/tsduck/tsduck", src], check=True)
    subprocess.run(["bash", "scripts/install-prerequisites.sh", "NODOXYGEN=1"] + opcoes, cwd=src, check=True)
    nucleos = str(max(1, min(os.cpu_count() or 1, 4)))
    subprocess.run(["make", "-j" + nucleos] + opcoes, cwd=src, check=True)
    subprocess.run(["make", "install"] + opcoes, cwd=src, check=True)
    subprocess.run(["ldconfig"], check=False)


def cmd_instalar_tsduck(args):
    if os.geteuid() != 0:
        raise SystemExit("rode como root: sudo asi2srt instalar-tsduck")
    if shutil.which("tsp") and not args.forcar:
        print("TSDuck já instalado (use --forcar para reinstalar)")
        return
    if args.compilar:
        _compilar_tsduck()
        print("TSDuck instalado. Confira com: asi2srt verificar")
        return
    info = _os_release()
    distro, versao = info.get("ID", ""), info.get("VERSION_ID", "")
    deb = bool(shutil.which("dpkg")) or distro in ("ubuntu", "debian", "raspbian")
    arq = _arquitetura_pacote(deb)
    print(f"sistema: {info.get('PRETTY_NAME', distro + ' ' + versao)} ({arq}) - procurando o TSDuck...")
    escolha = None
    try:
        req = urllib.request.Request("https://api.github.com/repos/tsduck/tsduck/releases/latest",
                                     headers={"User-Agent": "asi2srt"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.load(r)
        assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
        escolha = escolher_pacote_tsduck(list(assets), info, arq, deb)
    except OSError as e:
        print(f"não foi possível consultar o GitHub: {e}")
    if escolha:
        destino = os.path.join(tempfile.gettempdir(), escolha)
        print(f"baixando {escolha}...")
        urllib.request.urlretrieve(assets[escolha], destino)
        if deb:
            subprocess.run(["apt-get", "update"], check=False)
            subprocess.run(["apt-get", "install", "-y", destino], check=True)
        else:
            subprocess.run(["dnf" if shutil.which("dnf") else "yum", "install", "-y", destino], check=True)
    elif deb and subprocess.run(["bash", "-c", "apt-cache policy tsduck | grep -q 'Candidate: [0-9]'"]).returncode == 0:
        print("sem pacote na release do GitHub; instalando o TSDuck do repositório da distribuição...")
        subprocess.run(["apt-get", "install", "-y", "tsduck"], check=True)
    else:
        raise SystemExit(f"não há pacote pronto do TSDuck para {distro} {versao} ({arq}).\n"
                         "Compile a partir do código-fonte com:  sudo asi2srt instalar-tsduck --compilar")
    print("TSDuck instalado. Confira com: asi2srt verificar")


# ----------------------------------------------------------------------
# Driver DekTec (DTA PCIe / DTU USB)
# ----------------------------------------------------------------------
DEKTEC_VENDOR = "1a0e"          # ID de fabricante da DekTec (PCI e USB)
DEKTEC_MODULOS = ("DtPcie", "Dta", "Dtu")


def modelo_dektec(p):
    """O código de produto da DekTec é o número do modelo em hexadecimal (0x0861 = DTA-2145)."""
    try:
        n = int(str(p.get("produto")), 16)
    except (TypeError, ValueError):
        return f"(produto {p.get('produto')})"
    tipo = "DTU" if str(p.get("barramento", "")).upper() == "USB" else "DTA"
    return f"{tipo}-{n} (produto 0x{p['produto']})"


def detectar_dektec(raiz="/sys/bus"):
    """Lista placas DekTec presentes (lendo o sysfs, sem depender de lspci/lsusb)."""
    achados = []
    for barramento, arq_vend, arq_prod in (("pci", "vendor", "device"), ("usb", "idVendor", "idProduct")):
        base = os.path.join(raiz, barramento, "devices")
        try:
            nomes = sorted(os.listdir(base))
        except OSError:
            continue
        for n in nomes:
            try:
                with open(os.path.join(base, n, arq_vend)) as f:
                    vend = f.read().strip().lower()
            except OSError:
                continue
            if vend.replace("0x", "") != DEKTEC_VENDOR:
                continue
            try:
                with open(os.path.join(base, n, arq_prod)) as f:
                    prod = f.read().strip().lower().replace("0x", "")
            except OSError:
                prod = "?"
            achados.append({"barramento": barramento.upper(), "endereco": n, "produto": prod})
    return achados


def driver_dektec_carregado(modulos="/proc/modules"):
    try:
        with open(modulos) as f:
            texto = f.read()
    except OSError:
        return False
    return any(re.search(rf"^{m}\s", texto, re.M) for m in DEKTEC_MODULOS)


def procurar_sdk_dektec(pastas=None):
    """Procura o LinuxSDK_v*.tar.gz baixado do site da DekTec; devolve o mais recente."""
    import glob
    if pastas is None:
        pastas = [os.getcwd(), os.path.expanduser("~"), "/root", "/tmp", "/opt", "/usr/local/src"]
        for casa in glob.glob("/home/*"):
            pastas += [casa, os.path.join(casa, "Downloads"), os.path.join(casa, "Transferências")]
    principais, outros = set(), set()
    for d in pastas:
        for ext in ("*.tar.gz", "*.tgz"):
            for arq in glob.glob(os.path.join(d, ext)):
                nome = os.path.basename(arq).lower()
                if nome.startswith("linuxsdk"):
                    principais.add(arq)
                # pacote "DTAPI - C++ API for all DTA, DTU, DTE devices" (traz os drivers);
                # ignora DTAPI-TS (só análise) e DTAPI ARM (só DTA-2116)
                elif "dtapi" in nome and not re.search(r"dtapi[-_ ]?ts|arm", nome):
                    outros.add(arq)
    achados = principais or outros
    return max(achados, key=os.path.getmtime) if achados else None


def cmd_instalar_dektec(args):
    if os.geteuid() != 0:
        raise SystemExit("rode como root: sudo asi2srt instalar-dektec")
    if not DEKTEC_DISPONIVEL:
        print(f"placas DekTec não são suportadas em {platform.machine()} (o driver só existe para PC Intel/AMD).")
        return 0 if args.se_houver_placa else 1
    placas = detectar_dektec()
    if placas:
        for p in placas:
            print(f"placa DekTec encontrada: {p['barramento']} {p['endereco']} {modelo_dektec(p)}")
    elif args.se_houver_placa:
        print("nenhuma placa DekTec detectada - etapa ignorada")
        return 0
    else:
        print("aviso: nenhuma placa DekTec detectada; instalando o driver mesmo assim")
    if driver_dektec_carregado() and not args.forcar:
        print("driver DekTec já está carregado (use --forcar para reinstalar)")
        return 0

    sdk = args.sdk or os.environ.get("DEKTEC_SDK") or procurar_sdk_dektec()
    if not sdk:
        print("\nPara instalar o driver é preciso o Linux SDK da DekTec (a DekTec não oferece link fixo):\n"
              "  1. em https://www.dektec.com/downloads/SDK/ -> 'DekTec SDKs for Linux', baixe o primeiro:\n"
              "     'DTAPI - C++ API for all DTA, DTU, DTE devices' (traz os drivers; arquivo LinuxSDK_v*.tar.gz)\n"
              "  2. deixe o arquivo na pasta Downloads (ou em /tmp) e rode de novo, ou indique o caminho:\n"
              "       sudo asi2srt instalar-dektec --sdk /caminho/LinuxSDK_vXXXX.tar.gz")
        return 2
    if sdk.startswith(("http://", "https://")):
        destino = os.path.join(tempfile.gettempdir(), os.path.basename(urlparse(sdk).path) or "LinuxSDK.tar.gz")
        print(f"baixando {sdk}...")
        urllib.request.urlretrieve(sdk, destino)
        sdk = destino
    print(f"usando o SDK: {sdk}")

    kernel = platform.release()
    print("instalando ferramentas de compilação, DKMS e headers do kernel...")
    if shutil.which("apt-get"):
        subprocess.run(["apt-get", "update", "-qq"], check=False)
        subprocess.run(["apt-get", "install", "-y", "dkms", "build-essential", f"linux-headers-{kernel}"], check=False)
    elif shutil.which("dnf"):
        subprocess.run(["dnf", "install", "-y", "dkms", "gcc", "make", "elfutils-libelf-devel",
                        f"kernel-devel-{kernel}"], check=False)

    pasta = tempfile.mkdtemp(prefix="dektec-")
    subprocess.run(["tar", "xzf", sdk, "-C", pasta], check=True)
    raizes = [os.path.join(r, d) for r, ds, _ in os.walk(pasta) for d in ds if d == "Drivers"]
    if not raizes:
        raise SystemExit("o arquivo não parece ser o LinuxSDK da DekTec (pasta Drivers não encontrada)")
    drivers = min(raizes, key=len)
    instalador = os.path.join(drivers, "Install")
    if os.path.isfile(instalador):
        print("instalando o driver via DKMS (recompila sozinho quando o kernel for atualizado)...")
        subprocess.run(["bash", instalador], cwd=drivers, check=True)
    else:  # SDK anterior a 2024.06: compila cada driver manualmente
        print("SDK antigo (sem instalador DKMS): compilando os drivers...")
        feito = False
        for m in DEKTEC_MODULOS:
            src = os.path.join(drivers, m, "Source", "Linux")
            if os.path.isdir(src):
                subprocess.run(["make"], cwd=src, check=True)
                subprocess.run(["make", "install"], cwd=src, check=True)
                feito = True
        if not feito:
            raise SystemExit("nenhum driver encontrado dentro do SDK")
    subprocess.run(["depmod", "-a"], check=False)
    for m in DEKTEC_MODULOS:
        subprocess.run(["modprobe", m], capture_output=True)

    if driver_dektec_carregado():
        print("driver DekTec carregado.")
    else:
        sb = subprocess.run(["mokutil", "--sb-state"], capture_output=True, text=True) if shutil.which("mokutil") else None
        if sb and "enabled" in (sb.stdout or "").lower():
            print("o driver foi instalado, mas o Secure Boot está ATIVO e bloqueia drivers não assinados.\n"
                  "Desative o Secure Boot na BIOS/UEFI (ou registre a chave do DKMS com mokutil) e reinicie.")
        else:
            print("o driver foi instalado; reinicie o servidor (sudo reboot) para carregá-lo.")
    if shutil.which("tsdektec"):
        r = subprocess.run(["tsdektec", "-a"], capture_output=True, text=True)
        print("\nPlacas vistas pelo TSDuck:\n" + ((r.stdout or r.stderr).strip() or "  nenhuma (reinicie e rode: tsdektec -a)"))
    return 0


UNIDADE_SYSTEMD = """\
[Unit]
Description=Gateway ASI/UDP/SRT -> SRT/UDP (asi2srt)
After=network-online.target
Wants=network-online.target
# nunca desistir de religar (o padrão do systemd desiste após 5 falhas em 10 s)
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exe} rodar -c {config}
Restart=always
RestartSec=3
TimeoutStopSec=20
# prioridade alta, mas sem tempo real (SCHED_FIFO podia travar a máquina com o ffmpeg da janela de imagem)
Nice=-10
IOSchedulingClass=best-effort
IOSchedulingPriority=0
LimitNOFILE=65536
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
"""


SYSCTL_ASI2SRT = """\
# asi2srt: buffers UDP grandes para não perder pacotes em rajadas (TS de até ~200 Mb/s)
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 4194304
net.core.netdev_max_backlog = 10000
"""


def ajustar_rede():
    """Grava e aplica /etc/sysctl.d/90-asi2srt.conf (sem isso o kernel corta o buffer UDP em ~200 KB)."""
    try:
        with open("/etc/sysctl.d/90-asi2srt.conf", "w") as f:
            f.write(SYSCTL_ASI2SRT)
        subprocess.run(["sysctl", "-q", "-p", "/etc/sysctl.d/90-asi2srt.conf"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("buffers de rede ajustados (/etc/sysctl.d/90-asi2srt.conf)")
    except OSError as e:
        print(f"aviso: não consegui ajustar os buffers de rede: {e}")


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
            f.write(CONFIG_INICIAL)
        os.chmod(args.config, 0o600)
        print(f"configuração criada em {args.config} (sem canais: crie pelo painel)")
    with open("/etc/systemd/system/asi2srt.service", "w") as f:
        f.write(UNIDADE_SYSTEMD.format(exe=exe, config=args.config))
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    ajustar_rede()
    print("serviço instalado.\n"
          "  1. sudo systemctl enable --now asi2srt\n"
          "  2. login inicial do painel: admin / admin  (troque no menu do usuário > Trocar minha senha)\n"
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
                          "usuario": "admin", "senha_painel": "admin"}}
    url = f"srt://127.0.0.1:{args.porta}?mode=caller&passphrase={senha}&latency=120000"
    print(f"\nreceptor SRT:  ffplay \"{url}\"\nreceptor UDP:  ffplay udp://127.0.0.1:5000"
          f"\npainel:        http://127.0.0.1:8088/  (admin / admin)\nCtrl+C para sair\n")
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
<title>__TITULO__</title>
<link rel="icon" id="fav" href="/favicon.ico?v=__ICONV__">
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#2a313c;--txt:#e6edf3;--mut:#8b949e;--acc:#2f81f7;
--ok:#3fb950;--warn:#d29922;--err:#f85149;--in:#0d1117}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:5}
header h1{font-size:17px;margin:0;font-weight:650}header .v{color:var(--mut);font-size:12px}
.marca{display:flex;align-items:center;gap:12px;min-width:0}.marca img{height:38px;max-width:160px;object-fit:contain;display:block}
.marca .tx{display:flex;flex-direction:column;line-height:1.15;min-width:0}.marca h1{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.marca .v{font-size:11.5px}#bap{padding:6px 9px}
.prev{display:flex;align-items:center;gap:12px;padding:10px;border:1px dashed var(--line);border-radius:10px;min-height:64px;background:var(--in)}
.prev img{max-height:48px;max-width:220px;object-fit:contain}.prev span{color:var(--mut);font-size:12.5px}
header .sp{flex:1}
.resumo{display:flex;gap:18px;color:var(--mut);font-size:13px}.resumo b{color:var(--txt)}
main{padding:20px;max-width:1400px;margin:auto}
/* usuário logado */
.usr{position:relative}.usr>button{display:flex;align-items:center;gap:7px;padding:5px 10px 5px 6px}
.usr .av{width:24px;height:24px;border-radius:50%;background:#1f6feb33;color:#9ec5ff;display:flex;align-items:center;justify-content:center;font-size:11.5px;font-weight:700}
.usr .un{display:flex;flex-direction:column;line-height:1.1;text-align:left}.usr .un small{color:var(--mut);font-size:10.5px}
.usr .mn{position:absolute;right:0;top:calc(100% + 6px);min-width:210px;background:var(--card);border:1px solid var(--line);border-radius:10px;
 box-shadow:0 12px 30px #0008;padding:6px;z-index:30}
.usr .mn button{display:block;width:100%;text-align:left;border:0;background:none;padding:8px 10px;border-radius:6px}
.usr .mn button:hover{background:#ffffff0d}.usr .mn .q{padding:8px 10px 10px;border-bottom:1px solid var(--line);margin-bottom:4px;font-size:12px;color:var(--mut)}
.usr .mn .q b{display:block;color:var(--txt);font-size:13px}
body.op #novo,body.op #bap,body.op [data-a="editar"],body.op [data-a="excluir"],body.op .so-adm{display:none!important}
table.us{width:100%;border-collapse:collapse;font-size:13px}table.us th{text-align:left;color:var(--mut);font-weight:500;font-size:11.5px;padding:6px 8px;border-bottom:1px solid var(--line)}
table.us td{padding:7px 8px;border-bottom:1px solid #20262f}table.us td:last-child{text-align:right;white-space:nowrap}
.papel{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:99px;letter-spacing:.03em;text-transform:uppercase}
.papel.admin{background:#3a2a1f;color:#f5c48a}.papel.operador{background:#1f2a3a;color:#9ec5ff}
@media(max-width:760px){.usr .un{display:none}}
/* placas de rede */
.nics{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.nic{border:1px solid var(--line);border-radius:10px;background:#11161d;padding:10px 12px;display:flex;flex-direction:column;gap:6px;font-size:12.5px}
.nic.off{opacity:.75}.nic .nh{display:flex;align-items:center;gap:8px}.nic .nh b{font-family:ui-monospace,monospace;font-size:14px}
.nic .lk{width:10px;height:10px;border-radius:50%;background:#484f58;flex:none}.nic .lk.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.nic .lk.dn{background:var(--err);box-shadow:0 0 6px var(--err)}
.nic .tpn{font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:1px 6px;border-radius:4px;background:#1f2a3a;color:#9ec5ff}
.nic .tpn.wifi{background:#2a1f3a;color:#d2b8ff}.nic .tpn.bond,.nic .tpn.vlan,.nic .tpn.bridge{background:#22301f;color:#a6e3a1}
.nic .tpn.virtual,.nic .tpn.interna,.nic .tpn.vpn{background:#262b33;color:var(--mut)}
.nic .md{color:var(--mut);font-size:12px;min-height:16px}
.nic dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0}.nic dt{color:var(--mut)}.nic dd{margin:0;font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}
.nic .ntr{display:flex;gap:14px;font-variant-numeric:tabular-nums;font-size:12px;border-top:1px solid #1c222a;padding-top:6px}.nic .ntr span b{color:var(--txt)}.nic .ntr span{color:var(--mut);white-space:nowrap}
.nic .uso{display:flex;flex-wrap:wrap;gap:4px}.nic .uso span{font-size:11px;background:var(--in);border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.nic .uso span.r{border-style:dashed;color:var(--mut)}
.aviso{background:#3b2e00;border:1px solid #6b5300;padding:8px 12px;border-radius:8px;margin-bottom:14px;color:#f0d67a}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:10px}
.top{display:flex;align-items:center;gap:8px}.top h2{font-size:15px;margin:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill.pint{color:var(--warn)}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.rodando{color:var(--ok)}.conectando,.reiniciando,.iniciando{color:var(--warn)}.erro,.sem_dados{color:var(--err)}.parado{color:var(--mut)}
.fluxo{display:flex;flex-direction:column;gap:3px;font-size:12.5px}
.fluxo .l{color:var(--mut);display:inline-block;width:52px}
.tag{display:inline-block;background:var(--in);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin:1px 4px 1px 0;font-family:ui-monospace,monospace;font-size:12px}
.met{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:6px;align-items:end}
.met div{display:flex;flex-direction:column}.met small{color:var(--mut);font-size:11px}
.met b{font-size:15px;font-variant-numeric:tabular-nums}.met .big{font-size:22px}
.spark{width:100%;height:38px;display:block}.spark polyline{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}
.spark .area{fill:var(--acc);opacity:.12;stroke:none}
.card .lamps{grid-template-columns:repeat(2,1fr)}.card .lamp{padding:5px 8px}
.trs{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:7px 0 4px}.trh+.trs{margin-top:0}
.card .svs{padding:0;gap:8px}.card .pv{width:calc(50% - 4px)}
.tr{border:1px solid var(--line);border-radius:9px;padding:8px;background:#11161d}
.trh{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--mut);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em}
.trh .semsinal{color:var(--err);font-weight:600}
.lamps{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.lamp{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:6px;padding:5px 7px;border-radius:6px;background:var(--in);border:1px solid transparent}
.lamp i{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok)}
.lamp.w i{background:var(--warn);box-shadow:0 0 6px var(--warn)}
.lamp.e{border-color:#5c1f22}.lamp.e i{background:var(--err);box-shadow:0 0 9px var(--err)}
.lamp.x i{background:#484f58;box-shadow:none}
.lamp small{font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lamp b{font-variant-numeric:tabular-nums;font-size:12.5px}.lamp.e b{color:var(--err)}
.aus{font-size:11.5px;color:var(--err);margin-top:6px;font-family:ui-monospace,monospace}
.tr.rel{border-style:dashed}.tr.rel .met{margin-top:2px}
.aban{background:#10233d;border:1px solid #1f4b85;color:#cfe3ff;padding:9px 12px;border-radius:9px;margin-bottom:12px;font-size:13px}
#asess{width:auto;max-width:330px}a.btn{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 11px;font-size:13px;text-decoration:none}
a.btn:hover{border-color:#8b949e}
.baixar{display:inline-flex;align-items:center;gap:4px}.baixar .bl{font-size:12px;color:var(--mut);margin-right:2px}.baixar a.btn{padding:6px 9px}
.btnf{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 11px;font-size:13px}.btnf:hover{border-color:#8b949e}
.trp{display:flex;gap:10px;align-items:center;margin-top:7px;font-size:11.5px;color:var(--mut)}
.trp .e{color:var(--err)}.trp .w{color:var(--warn)}.trp a{margin-left:auto;color:var(--acc);cursor:pointer;text-decoration:none}
#vana{padding:16px 20px;max-width:1500px;margin:auto}
.atop{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.atop h2{margin:0;font-size:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.kpi small{display:block;color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.kpi b{font-size:16px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.agrid{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:12px;margin-bottom:12px;align-items:start}
.acol{display:flex;flex-direction:column;gap:12px;min-width:0}
.painel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}
.painel h3{margin:0 0 10px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;display:flex;gap:8px}
.painel h3 span{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400}
.trg h4{margin:10px 0 5px;font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}.trg:first-child h4{margin-top:0}
.trl{display:grid;grid-template-columns:12px 38px 1fr auto 44px;align-items:center;gap:7px;padding:6px 8px;border-radius:7px;background:var(--in);margin-bottom:4px;border:1px solid transparent;font-size:13px}
.trl i{width:10px;height:10px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok)}
.trl.w i{background:var(--warn);box-shadow:0 0 6px var(--warn)}.trl.e{border-color:#5c1f22}.trl.e i{background:var(--err);box-shadow:0 0 9px var(--err)}
.trl.x{opacity:.55}.trl.x i{background:#484f58;box-shadow:none}
.trl .cod{color:var(--mut);font-family:ui-monospace,monospace;font-size:12px}.trl .ha{color:var(--mut);font-size:11px;white-space:nowrap}
.trl b{text-align:right;font-variant-numeric:tabular-nums}.trl.e b{color:var(--err)}
.serv{border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
.serv .sh{display:flex;gap:8px;align-items:baseline;padding:9px 11px;background:#11161d;flex-wrap:wrap}
.serv .sh b{font-size:14px}.serv .sh .m{color:var(--mut);font-size:12px}.serv .sh .tx{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
.serv .meta{display:flex;gap:14px;flex-wrap:wrap;padding:6px 11px;font-size:12px;color:var(--mut);border-bottom:1px solid var(--line)}
.serv .meta b{color:var(--txt);font-weight:500;font-family:ui-monospace,monospace}
.badge{font-size:10.5px;padding:1px 6px;border-radius:5px;border:1px solid var(--line);color:var(--mut)}.badge.r{color:var(--err);border-color:#5c1f22}
.tabw{overflow-x:auto}
table.t{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
table.t th{text-align:left;color:var(--mut);font-weight:500;font-size:11.5px;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
table.t th.n,table.t td.n{text-align:right}table.t td{padding:5px 8px;border-bottom:1px solid #20262f;white-space:nowrap}
table.t tr:hover td{background:#1b222c}table.t .mono{font-family:ui-monospace,monospace}
table.t tr.unref td{color:var(--err)}table.t tr.ign td{color:var(--mut);font-style:italic}table.t tr.aus td{opacity:.45}table.t td.ruim{color:var(--err);font-weight:600}
.bar{display:inline-block;height:7px;border-radius:4px;background:var(--acc);vertical-align:middle;margin-right:6px;opacity:.85}
.graf .gw{position:relative}.graf .gw span{position:absolute;left:4px;transform:translateY(-110%);font-size:10.5px;color:var(--mut)}
.graf svg{width:100%;height:150px;display:block}.graf .gl{stroke:#232a34;stroke-width:1}.graf text{fill:var(--mut);font-size:10px}
.graf polyline{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}.graf polygon{fill:var(--acc);opacity:.12}
@media(max-width:900px){.agrid{grid-template-columns:1fr}#vana{padding:12px}}
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
@media(max-width:420px){.lamps{grid-template-columns:repeat(2,1fr)}}
.vis{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.vis button{border:0;border-radius:0;padding:6px 10px}
.vis button.on{background:#30363d;color:#fff}
.rotas{display:flex;flex-direction:column;gap:10px}
/* modo compacto da rota (padrão): TR numa linha só, imagem e gráfico menores */
.rota.cp .rh{padding:7px 12px;gap:8px}.rota.cp .rl{padding:2px 12px 6px}.rota.cp .no{padding:4px 0}
.rota.cp .svs{gap:8px;padding:4px 0 2px 96px}.rota.cp .pv{width:124px}.rota.cp .pv .pvn{padding:3px 6px;font-size:11px}
.rota.cp .pv .pvn .pvt{font-size:10.5px}.rota.cp .gpv{min-width:220px;max-width:460px}.rota.cp .gpv .gv{min-height:46px}
.rota.cp .gpv .gh,.rota.cp .gpv .gf{padding:3px 8px;font-size:11px}
.indc{display:flex;gap:6px 14px;flex-wrap:wrap;align-items:center;padding:5px 12px;border-bottom:1px solid #20262f;background:#13181f}
.cg{display:inline-flex;gap:5px;align-items:center;flex-wrap:wrap}
.cgl{font-size:10.5px;font-weight:700;letter-spacing:.05em;color:var(--mut);padding:1px 6px;border-radius:4px;background:#1b2129}
.cg.e .cgl{background:#3a1416;color:#ff8f8a}.cg.w .cgl{background:#3a2f10;color:#f2cc60}.cg.ok .cgl{background:#14301f;color:#7ee2a8}
.cok{font-size:11.5px;color:#7ee2a8}.cg.x .cok{color:var(--mut)}
.indc .ic{padding:1px 7px;font-size:11.5px}.indc .sub{color:var(--mut);font-size:11px}
.bx{padding:4px 8px!important;font-size:12px!important;min-width:30px}
/* visão Lista: uma linha por sinal */
/* comparação de sinais */
.cmpsel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:12px}
.cmpsel-h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}.cmpsel-h span{color:var(--mut);font-size:12px}
.ccols{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch}
.ccol{display:flex;gap:8px;align-items:flex-end;background:#11161d;border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.ccol label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.ccol select{min-width:150px;max-width:240px;text-transform:none;letter-spacing:0}
.cnum{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;background:#1f6feb33;color:#9ec5ff;font-weight:700;font-size:12px;flex:none}
.ccol .cnum{align-self:center}
.cadd{border-style:dashed!important;color:var(--mut)}
.cmpb{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.cmpb .lb{font-size:12px;color:var(--mut);margin-right:2px}
.cmpb button.sel{display:inline-flex;align-items:center;gap:6px}.cmpb button.sel i{width:8px;height:8px;border-radius:50%;background:#484f58}
.cmpb button.sel i.on{background:var(--ok)}.cmpb button.sel.on{border-color:var(--acc);background:#1f6feb26;color:#cfe3ff}
.cmpb .sp{flex:1}
.cmp{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto;margin-bottom:12px}
.cmp h3{margin:0;padding:10px 12px 8px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--line);
 display:flex;gap:10px;align-items:baseline}.cmp h3 small{text-transform:none;letter-spacing:0;font-weight:400}
.cmp table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
.cmp th{text-align:left;color:var(--mut);font-weight:500;font-size:11px;padding:7px 10px;border-bottom:1px solid var(--line);background:#13181f;position:sticky;top:0;white-space:nowrap}
.cmp th.cn{color:var(--txt);font-size:12px;font-weight:600;border-left:1px solid var(--line);text-align:center}
.cmp td{padding:5px 10px;border-bottom:1px solid #1c222a;white-space:nowrap}.cmp td.c{border-left:1px solid #1c222a;text-align:center}
.cmp td.n,.cmp th.n{text-align:right}.cmp tr:last-child td{border-bottom:0}.cmp tr:hover td{background:#ffffff05}
.cmp tr.dif td:first-child{box-shadow:inset 3px 0 0 var(--warn)}.cmp tr.err td:first-child{box-shadow:inset 3px 0 0 var(--err)}
.cmp .aus{color:var(--warn);font-size:11.5px}.cmp .cce{color:#ff8f8a;font-weight:700}.cmp .cc0{color:var(--mut)}
.cmp .sm{color:var(--mut);font-size:11px;margin-left:6px}.cmp .neq{color:var(--warn);font-weight:700;margin-left:4px;cursor:help}
.cmp .vazio{padding:18px;color:var(--mut);text-align:center}
.lista{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto}
.lista table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
.lista th{text-align:left;color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;
 border-bottom:1px solid var(--line);background:#13181f;position:sticky;top:0;white-space:nowrap}
.lista td{padding:6px 10px;border-bottom:1px solid #1c222a;white-space:nowrap;vertical-align:middle}
.lista tr:last-child td{border-bottom:0}.lista tr:hover td{background:#ffffff05}
.lista td.n,.lista th.n{text-align:right}.lista .rn{font-weight:650;cursor:pointer}.lista .rn:hover{color:var(--acc)}
.lista tr td:first-child{border-left:3px solid transparent}.lista tr.e-rodando td:first-child{border-left-color:var(--ok)}
.lista tr.e-erro td:first-child,.lista tr.e-sem_dados td:first-child{border-left-color:var(--err)}
.lista tr.e-reiniciando td:first-child,.lista tr.e-conectando td:first-child,.lista tr.e-iniciando td:first-child{border-left-color:var(--warn)}
.lista .th{width:64px;height:36px;border-radius:4px;background:#05070a;display:block;object-fit:cover}
.lista .th.v{display:flex;align-items:center;justify-content:center;color:#484f58;font-size:10px}
.lista code{font-family:ui-monospace,monospace;font-size:12px}.lista .ent{max-width:380px;overflow:hidden;text-overflow:ellipsis}
.lista .acs{display:flex;gap:5px;justify-content:flex-end}.lista .acs button{padding:3px 8px;font-size:12px}
.pb{display:inline-block;min-width:34px;text-align:center;padding:1px 7px;border-radius:99px;font-weight:600;font-size:11.5px}
.pb.ok{background:#14301f;color:#7ee2a8}.pb.w{background:#3a2f10;color:#f2cc60}.pb.e{background:#3a1416;color:#ff8f8a}.pb.x{background:#1b2129;color:var(--mut)}
.rota{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.rota.e-rodando{border-left:3px solid var(--ok)}.rota.e-reiniciando,.rota.e-conectando,.rota.e-iniciando{border-left:3px solid var(--warn)}
.rota.e-erro,.rota.e-sem_dados{border-left:3px solid var(--err)}.rota.e-parado{border-left:3px solid #484f58}
.rh{display:flex;align-items:center;gap:10px;padding:10px 14px;flex-wrap:wrap;border-bottom:1px solid #20262f}
.rh .rn{font-size:15px;font-weight:650;cursor:pointer}.rh .rn:hover{color:var(--acc)}
.rh .rb{font-variant-numeric:tabular-nums;font-weight:600;font-size:15px;min-width:92px;text-align:right}
.rh .rb small{color:var(--mut);font-weight:400;font-size:11px}
.dots{display:flex;gap:4px;align-items:center}.dots i{width:9px;height:9px;border-radius:50%;background:var(--ok)}
.dots i.w{background:var(--warn)}.dots i.e{background:var(--err);box-shadow:0 0 6px var(--err)}.dots i.x{background:#484f58}
.dots small{color:var(--mut);font-size:11px;margin-left:4px}
.rh .acs{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}.rh .acs button{padding:4px 9px;font-size:12.5px}
.rl{padding:6px 14px 10px}
.svs{display:flex;gap:10px;flex-wrap:wrap;padding:8px 0 4px 96px}
.gpv{flex:1 1 300px;min-width:260px;max-width:620px;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:8px;background:#0b0f14;overflow:hidden}
.gpv .gh,.gpv .gf{display:flex;gap:12px;align-items:center;padding:5px 9px;font-size:12px;color:var(--mut);flex-wrap:wrap}
.gpv .gh{background:#11161d;border-bottom:1px solid var(--line)}.gpv .gf{border-top:1px solid var(--line);font-size:11.5px}
.gpv b{color:var(--txt,#e6edf3);font-weight:600;font-variant-numeric:tabular-nums}.gpv .gh small{margin-left:auto}
.gpv .gh .w b{color:var(--warn)}.gpv .gh .e b{color:var(--err)}
.gpv i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}.gpv i.lb{background:var(--acc)}.gpv i.lr{background:var(--err)}
.gpv .gv{position:relative;flex:1;min-height:70px}.gpv .gv.gm{display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:12px}
.gpv svg{position:absolute;inset:6px 0 0 0;width:100%;height:calc(100% - 6px);display:block}
.gpv polyline{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}.gpv polyline.lp{stroke:var(--err);stroke-width:1.3}
.gpv .ar{fill:var(--acc);opacity:.13}.gpv .gg line{stroke:#1d242d;stroke-width:1;vector-effect:non-scaling-stroke}
.gpv .gx{position:absolute;left:6px;font-size:10px;color:var(--mut);font-variant-numeric:tabular-nums;pointer-events:none}.gpv .gx.t{top:4px}.gpv .gx.b{bottom:2px}
.pv{position:relative;width:176px;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#05070a}
.pv .im{position:relative;aspect-ratio:16/9;background:#05070a;display:flex;align-items:center;justify-content:center}
.pv img{width:100%;height:100%;object-fit:contain;display:block}
.pv .pvm{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:8px;
 color:var(--mut);font-size:11px;line-height:1.3}
.pv [hidden]{display:none!important}
.pv .pvn{display:flex;gap:6px;align-items:baseline;padding:5px 8px;font-size:12px;background:#11161d;border-top:1px solid var(--line)}
.modo{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:4px;vertical-align:middle}
.modo small{font-weight:400;opacity:.8}.modo.cbr{background:#1f2a3a;color:#9ec5ff}.modo.vbr{background:#2d2414;color:#f5c48a}
.modo.med{background:#1c2128;color:var(--mut);font-weight:400}
.pv .pvn .pvt{font-size:11.5px;font-variant-numeric:tabular-nums;color:#9ec5ff;white-space:nowrap}
.rb .ut{display:block;font-size:11px;font-weight:400;color:var(--mut)}.rb .ut b{color:#a6e3a1;font-weight:600}
.pv .pvn{flex-wrap:wrap;row-gap:2px}
.pv .pvn b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1 1 100%}.pv .pvn small{color:var(--mut);font-family:ui-monospace,monospace}
.pv.sai{outline:2px solid #2f81f7;outline-offset:-1px}
.pv.g{width:240px;flex:none}.serv .sh .pv{margin:-2px 4px -2px -4px}
.svout{display:inline-flex;align-items:center;gap:6px}.svout img{width:48px;height:27px;object-fit:cover;border-radius:4px;border:1px solid var(--line);background:#05070a}
.saiem{font-size:12px;color:var(--mut);padding:6px 11px;border-bottom:1px solid var(--line)}.saiem b{color:var(--txt);font-weight:500}
.servsel{display:flex;flex-wrap:wrap;gap:6px 14px;margin:2px 0 6px}
@media(max-width:760px){.svs{padding-left:0}.pv{width:calc(50% - 5px)}}
.ind{display:flex;flex-direction:column;gap:6px;padding:8px 14px;border-bottom:1px solid #20262f;background:#13181f}
.ig{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.igl{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;width:92px;flex:none}
.igl.w{color:#f2cc60}.igl.e{color:#ff8f8a}
.ind .sub{margin-left:0}
.ic{display:inline-flex;align-items:center;gap:6px;font-size:12px;background:var(--in);border:1px solid var(--line);border-radius:7px;padding:3px 9px;white-space:nowrap}
.ic i{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 5px var(--ok)}
.ic b{font-variant-numeric:tabular-nums;margin-left:2px}
.ic.w{border-color:#5c4a12}.ic.w i{background:var(--warn);box-shadow:0 0 5px var(--warn)}.ic.w b{color:#f2cc60}
.ic.e{border-color:#5c1f22;background:#2a1215}.ic.e i{background:var(--err);box-shadow:0 0 8px var(--err)}.ic.e b{color:#ff8f8a}
.ic.x{opacity:.5}.ic.x i{background:#484f58;box-shadow:none}
.ind .sub{color:var(--mut);font-size:11px;margin-left:4px}
.no{display:grid;grid-template-columns:86px minmax(170px,330px) 1fr auto;gap:10px;align-items:center;padding:6px 0;border-bottom:1px dashed #20262f;font-size:13px}
.no:last-child{border-bottom:0}.no .pp{color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
.no .ed{display:flex;align-items:center;gap:7px;min-width:0}.no .ed code{font-family:ui-monospace,monospace;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.no .tp{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:4px;background:#1f2a3a;color:#9ec5ff;letter-spacing:.03em}
.nicb{font-size:10.5px;font-family:ui-monospace,monospace;color:#c9b8ff;background:#251f3a;border-radius:4px;padding:1px 6px;white-space:nowrap;flex:none}
.nicb.res{color:#1b1300;background:#f2cc60;font-weight:700}
.nicr{font-size:10px;font-family:ui-monospace,monospace;color:var(--mut);border:1px dashed #3a3f4b;border-radius:4px;padding:0 5px;white-space:nowrap;flex:none}
.no .tp.udp,.lista .tp.udp{background:#22301f;color:#a6e3a1}.no .tp.asi,.lista .tp.asi{background:#3a2a1f;color:#f5c48a}
.lista .tp{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:4px;background:#1f2a3a;color:#9ec5ff;letter-spacing:.03em}
.st{display:inline-flex;align-items:center;gap:5px;font-size:12px;white-space:nowrap}.st i{width:8px;height:8px;border-radius:50%;background:#484f58}
.st.conectado i,.st.ativo i{background:var(--ok);box-shadow:0 0 5px var(--ok)}.st.aguardando i{background:var(--warn)}
.st.conectado{color:#a6e3a1}.st.aguardando{color:#f2cc60}.st.parado{color:var(--mut)}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chip{font-size:11.5px;background:var(--in);border:1px solid var(--line);border-radius:6px;padding:2px 7px;white-space:nowrap;font-variant-numeric:tabular-nums}
.chip small{color:var(--mut);margin-right:4px}.chip.w{border-color:#5c4a12;color:#f2cc60}.chip.e{border-color:#5c1f22;color:#ff8f8a}
.mspark{width:90px;height:22px;display:block}.mspark polyline{fill:none;stroke:var(--acc);stroke-width:1.4;vector-effect:non-scaling-stroke}
.mspark .lp{stroke:var(--err)}
.rrel{font-size:12px;color:var(--mut);padding:2px 0 4px}.rrel b{color:var(--txt);font-weight:500}
@media(max-width:760px){.no{grid-template-columns:1fr;gap:4px}.rh .acs{margin-left:0;width:100%}}
.srtl{border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
.srtl .sh{display:flex;gap:8px;align-items:center;padding:8px 11px;background:#11161d;flex-wrap:wrap;font-size:13px}
.srtl .sh code{font-family:ui-monospace,monospace}.srtl .sk{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:6px;padding:9px 11px}
.srtl .sk div{background:var(--in);border-radius:7px;padding:6px 8px}.srtl .sk small{display:block;color:var(--mut);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
.srtl .sk b{font-variant-numeric:tabular-nums;font-size:14px}.srtl .sk b.w{color:var(--warn)}.srtl .sk b.e{color:var(--err)}
.srtl .g{padding:0 11px 10px}.srtl .g svg{width:100%;height:90px;display:block}.srtl .g .leg{font-size:11px;color:var(--mut);display:flex;gap:12px;margin-bottom:3px}
@media(max-width:600px){header{flex-wrap:wrap}.resumo{width:100%;order:3}main{padding:12px}.grid{grid-template-columns:1fr}}
</style></head><body>
<header><div class="marca" title="versão __VERSAO__"><img id="mlogo" alt="" hidden><div class="tx"><h1 id="mtit">__TITULO__</h1></div></div><span class="sp"></span>
<div class="resumo" id="resumo"></div><div class="vis" id="vis"><button data-vis="rotas">Rotas</button><button data-vis="lista" title="uma linha por sinal">Lista</button><button data-vis="cards">Cards</button><button data-vis="comparar" title="comparar serviços e PIDs de vários sinais">Comparar</button></div>
<button id="brede" title="Placas de rede do servidor">Rede</button><button id="bap" title="Aparência (nome e logo) e PIDs fora do pacote">⚙</button><button class="pri" id="novo">+ Novo canal</button><div class="usr" id="usr"></div></header>
<dialog id="dlgap"><form method="dialog" id="frmap">
<div class="dh"><h3>Aparência e análise</h3><button type="button" class="mini" data-fechar>✕</button></div>
<div class="db"><fieldset><legend>Nome do painel</legend><label>Nome<input name="titulo" maxlength="80" required></label></fieldset>
<fieldset><legend>Logo</legend><div class="prev" id="apprev"></div>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap"><label class="chk" style="cursor:pointer"><input type="file" id="aplogo" accept="image/png,image/jpeg,image/svg+xml,image/webp" hidden><span class="btnf">Escolher arquivo…</span></label>
<button type="button" id="aprem" class="perigo">Remover logo</button></div>
<p class="msg" style="white-space:normal;margin:8px 0 0">PNG, JPG, SVG ou WebP, até 2 MB. Fica salvo no servidor, junto da configuração. Prefira fundo transparente.</p></fieldset>
<fieldset><legend>Repetição do PCR (TR 101 290 · 2.3a)</legend><label>Limite<select name="pcr_ms">
<option value="100">100 ms — ISO/IEC 13818-1, ISDB-Tb e recomendação atual da TR 101 290</option>
<option value="40">40 ms — critério antigo do DVB (TR 101 290 até 2005)</option></select></label>
<p class="msg" style="white-space:normal;margin:8px 0 0">Gera erro 2.3a quando o intervalo entre dois PCRs do mesmo programa passa do limite. Vale para todos os canais, na hora.</p></fieldset>
<fieldset><legend>PIDs fora do pacote</legend><label>PIDs (separados por vírgula; decimal ou 0x…)<input name="pids_ign" placeholder="5000, 5016, 5017, 8184" spellcheck="false"></label>
<p class="msg" style="white-space:normal;margin:8px 0 0">PIDs que vêm no TS mas não pertencem a nenhum serviço (dados do multiplexador). Continuam aparecendo nas tabelas, em cinza, mas <b>não geram erro</b>: nem "PID não referenciado", nem erro de continuidade (CC). Vale para todos os canais, na hora.</p></fieldset>
<p class="msg" style="margin:4px 0 0">Versão do sistema: __VERSAO__</p></div>
<div class="df"><button type="button" data-fechar>Cancelar</button><button class="pri" type="submit">Salvar</button></div></form></dialog>
<dialog id="dlgrede" style="width:min(980px,96vw)"><div class="dh"><h3>Placas de rede do servidor</h3>
<label class="chk" style="margin-right:12px;font-size:12px"><input type="checkbox" id="redetodas"> mostrar internas (Docker/VM)</label><button type="button" class="mini" data-fechar>✕</button></div>
<div class="db" id="redecorpo"></div></dialog>
<dialog id="dlgsenha" style="width:min(420px,96vw)"><form method="dialog" id="frmsenha">
<div class="dh"><h3>Trocar minha senha</h3><button type="button" class="mini" data-fechar>✕</button></div>
<div class="db" style="display:grid;gap:12px"><label>Senha atual<input type="password" name="atual" autocomplete="current-password" required></label>
<label>Nova senha<input type="password" name="nova" autocomplete="new-password" minlength="6" required></label>
<label>Repita a nova senha<input type="password" name="nova2" autocomplete="new-password" minlength="6" required></label>
<p class="msg" style="white-space:normal;margin:0">Mínimo de 6 caracteres. Ao trocar, as outras sessões abertas com este usuário são encerradas.</p></div>
<div class="df"><button type="button" data-fechar>Cancelar</button><button class="pri" type="submit">Trocar senha</button></div></form></dialog>
<dialog id="dlgus"><div class="dh"><h3>Usuários</h3><button type="button" class="mini" data-fechar>✕</button></div>
<div class="db"><table class="us" id="tus"></table>
<form id="frmus" style="margin-top:16px"><fieldset><legend id="uslg">Novo usuário</legend><div class="campos">
<label>Usuário<input name="usuario" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="40" required></label>
<label>Nome<input name="nome" maxlength="60" placeholder="ex.: Bruno Alves"></label>
<label>Perfil<select name="papel"><option value="operador">Operador</option><option value="admin">Administrador</option></select></label>
<label>Senha<input type="password" name="senha" autocomplete="new-password" minlength="6"></label></div>
<p class="msg" style="white-space:normal;margin:10px 0 0"><b>Administrador</b>: tudo, inclusive criar/editar/excluir canais, aparência e usuários.
<b>Operador</b>: vê tudo, inicia/para/reinicia canais, zera contadores e baixa relatórios.</p>
<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px"><button type="button" id="uscanc" hidden>Cancelar edição</button><button class="pri" type="submit" id="ussub">Adicionar usuário</button></div>
</fieldset></form></div></dialog>
<main id="vlista"><div id="avisos"></div><div class="grid" id="grid"></div></main>
<section id="vana" hidden>
 <div class="atop"><button id="voltar">← Canais</button><h2 id="anome"></h2><span class="pill" id="aest"></span>
  <span class="sp" style="flex:1"></span><select id="asess" title="Sessões de análise salvas"></select>
  <span class="baixar"><span class="bl">Baixar relatório:</span><a class="btn" id="abx_pdf" href="#" title="relatório em PDF (A4), pronto para enviar ou arquivar">PDF</a><a class="btn" id="abx_html" href="#" title="relatório formatado, com logo; abre no navegador">HTML</a><a class="btn" id="abx_txt" href="#" title="texto simples">TXT</a><a class="btn" id="abx_json" href="#" title="dados completos">JSON</a></span><button id="azerar">Zerar contadores</button></div>
 <div class="aban" id="aban" hidden></div>
 <div class="kpis" id="akpi"></div>
 <div class="agrid"><div class="painel"><h3>TR 101 290<span id="adesde"></span></h3><div id="atr"></div></div>
  <div class="acol"><div class="painel" id="psrt" hidden><h3>Saídas e conexões<span id="asrtn"></span></h3><div id="asrt"></div></div>
  <div class="painel"><h3>Bitrate útil<span>sem nulos · últimos 5 min</span></h3><div class="graf" id="agraf"></div></div>
  <div class="painel"><h3>Serviços<span id="anserv"></span></h3><div id="aserv"></div></div></div></div>
 <div class="painel"><h3>PIDs<span id="anpid"></span></h3><div class="tabw"><table class="t" id="apids"></table></div></div>
</section>

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
function irLogin(){location.href='/login?volta='+encodeURIComponent(location.pathname+location.search+location.hash)}
async function api(m,u,b){const r=await fetch(u,{method:m,headers:H,body:b?JSON.stringify(b):undefined});
 if(r.status===401&&!u.startsWith('/api/eu/senha')){irLogin();throw new Error('sessão encerrada')}
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.erro||r.statusText);return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(t,bom){const d=document.createElement('div');d.className='toast'+(bom?' bom':'');d.textContent=t;
 document.body.appendChild(d);setTimeout(()=>d.remove(),bom?2500:6000)}
const mb=b=>(b/1e6).toFixed(2);
function dur(s){if(!s)return'—';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h${String(m).padStart(2,'0')}`:m?`${m}m${s%60}s`:`${s}s`}

const CE={
 dektec:[['dispositivo','Placa','number',0],['canal','Porta','number',0],['timeout_ms','Timeout sem sinal (ms)','number',5000]],
 linuxasi:[['dispositivo','Dispositivo','text','/dev/asirx0']],
 udp:[['endereco','Endereço (ip:porta ou porta)','text','239.1.1.1:1234'],['interface','Placa de rede (entrada)','nic',''],['interface_reserva','Placa reserva (failover)','nic2','']],
 srt:[['modo','Modo','sel:caller,listener','caller'],['endereco','Endereço (caller: IP:porta · listener: porta)','text',''],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],['stream_id','Stream ID','text',''],
      ['interface','Placa de rede (entrada)','nic',''],['interface_reserva','Placa reserva (failover)','nic2','']],
 arquivo:[['caminho','Arquivo .ts','text',''],['loop','Repetir em loop','chk',true]]};
const CS={
 srt:[['servicos','Serviços a enviar','serv',[]],['modo','Modo','sel:caller,listener','caller'],['endereco','Destino (host:porta) / porta','text',''],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],
      ['stream_id','Stream ID','text',''],['overhead_pct','Overhead (%)','number',25],['interface','Placa de rede (saída)','nic',''],['interface_reserva','Placa reserva (failover)','nic2','']],
 udp:[['servicos','Serviços a enviar','serv',[]],['endereco','Destino (ip:porta)','text','239.1.1.1:1234'],['ttl','TTL','number',16],['interface','Placa de rede (saída)','nic',''],['interface_reserva','Placa reserva (failover)','nic2','']],
 rtp:[['servicos','Serviços a enviar','serv',[]],['endereco','Destino (ip:porta)','text','239.1.1.1:5004'],['ttl','TTL','number',16],['interface','Placa de rede (saída)','nic',''],['interface_reserva','Placa reserva (failover)','nic2',''],
      ['payload_type','Payload type','number',33],['ssrc','SSRC (opcional)','text','']],
 rtmp:[['url','Servidor (ex.: rtmp://a.rtmp.youtube.com/live2)','text',''],['chave','Chave de transmissão','password',''],['video','Vídeo','sel:copiar,h264','copiar'],['audio','Áudio','sel:aac,copiar','aac'],
      ['video_kbps','Vídeo kb/s (se h264)','number',4000],['audio_kbps','Áudio kb/s','number',128],['servicos','Serviço a enviar (RTMP leva um só)','serv1',[]]]};
const NOMES_E={srt:'SRT',udp:'UDP / Multicast',dektec:'ASI — placa DekTec',linuxasi:'ASI — driver Linux (DVEO)',arquivo:'Arquivo .ts'};

function resE(e){switch(e.tipo){case'dektec':return`DekTec ${e.dispositivo??0}:${e.canal??0}`;case'linuxasi':return`ASI ${e.dispositivo}`;
 case'udp':return`UDP ${e.endereco}`;case'srt':return`SRT ${e.modo} ${e.endereco}${e.senha?' 🔒':''}`;case'arquivo':return`Arquivo ${(e.caminho||'').split('/').pop()}`}return e.tipo}
const urlSemChave=u=>String(u||'').replace(/^(rtmps?:\/\/[^?#]+\/)[^/?#]+/,'$1••••');
let resS=s=>s.tipo==='udp'?`UDP ${s.endereco}`:s.tipo==='rtp'?`RTP ${s.endereco}`:s.tipo==='rtmp'?(s.chave?String(s.url||'').replace(/\/+$/,'')+'/••••':urlSemChave(s.url)).replace(/^rtmp/,'RTMP').replace(/^RTMPs/,'RTMPS'):`SRT ${s.modo} ${s.endereco}${s.senha?' 🔒':''}`;

function spark(h){if(!h||h.length<2)return'<svg class="spark"></svg>';const mx=Math.max(...h)*1.15||1,w=100,hh=38;
 const p=h.map((v,i)=>`${(i/(h.length-1)*w).toFixed(2)},${(hh-v/mx*hh).toFixed(2)}`).join(' ');
 return`<svg class="spark" viewBox="0 0 ${w} ${hh}" preserveAspectRatio="none"><polygon class="area" points="0,${hh} ${p} ${w},${hh}"/><polyline points="${p}"/></svg>`}

let INFO={},dados=[],editando=null,logId=null,TRI=[],anaId=null,ordem={k:'pid',d:1},ana=null;
const TR=[['sync_loss','TS sync loss','1.1 Perda de sincronismo: 2+ sync bytes errados seguidos ou perda do sinal de entrada'],
 ['sync_byte','Sync byte','1.2 Pacote com sync byte diferente de 0x47'],
 ['pat','PAT','1.3 PAT ausente por mais de 0,5 s, table_id errado ou embaralhada'],
 ['cc','Continuity','1.4 Contador de continuidade fora de ordem, perdido ou duplicado'],
 ['pmt','PMT','1.5 PMT ausente por mais de 0,5 s, table_id errado ou embaralhada'],
 ['pid','PID','1.6 PID citado na PMT ausente por mais de 5 s']];
const TR2=[['transport','Transport'],['crc','CRC'],['pcr_rep','PCR repetition'],['pcr_disc','PCR discontinuity'],['pcr_acc','PCR accuracy'],['pts','PTS'],['cat','CAT']];
const CURTO={};
function lampada(t,k,n,d){const v=t.contadores[k]||0,na=(t.nao_aplicavel||[]).includes(k)&&!v,c=t.recentes.includes(k)?'e':v?'w':(t.sinal&&!na?'':'x');
 const nome=(TRI.find(x=>x[0]===k)||[])[3]||n;
 return`<div class="lamp ${c}" title="${esc(nome+' — '+(d||''))}"><i></i><small>${n}</small><b>${v.toLocaleString('pt-BR')}</b></div>`}
function tr(s){const t=s.tr101290;if(!t)return'';
 const info=t.sinal?`${t.programas} prog · ${t.pids_referenciados} PIDs`:'<span class="semsinal">sem sinal</span>';
 const desc=Object.fromEntries(TRI.map(x=>[x[0],x[2]+' '+x[4]]));
 return`<div class="tr"><div class="trh"><span>TR 101 290</span><span>${info}</span></div>
 <div class="trs">Prioridade 1</div><div class="lamps">
 ${TR.map(([k,n,d])=>lampada(t,k,CURTO[k]||n,d)).join('')}</div>
 <div class="trs">Prioridade 2</div><div class="lamps">${TR2.map(([k,n])=>lampada(t,k,n,desc[k])).join('')}</div>
 ${t.pids_ausentes.length?`<div class="aus">PIDs ausentes: ${t.pids_ausentes.map(esc).join(', ')}</div>`:''}
 ${TRI.length?(()=>{const soma=p=>TRI.filter(x=>x[1]===p).reduce((a,x)=>a+(t.contadores[x[0]]||0),0),
  rec=p=>TRI.some(x=>x[1]===p&&t.recentes.includes(x[0])),cl=p=>rec(p)?'e':soma(p)?'w':'';
  return`<div class="trp"><span class="${cl(3)}">Prioridade 3 · ${soma(3)}</span><a data-ana="${s._id}">Análise completa →</a></div>`})():''}</div>`}
const fmtDH=s=>new Date(s*1000).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
const fmtH=s=>new Date(s*1000).toLocaleTimeString('pt-BR');
const MOTIVO={parado:'parada',encerrado:'serviço desligado',interrompido:'interrompida','em andamento':'em andamento'};
const MOTIVO_L={parado:'encerrada pelo usuário',encerrado:'encerrada (serviço desligado)',interrompido:'interrompida (o programa foi fechado inesperadamente)','em andamento':'em andamento'};
function relCard(c){const r=c.ultimo_relatorio,n=r.contadores;
 return`<div class="tr rel"><div class="trh"><span>Última análise · ${esc(MOTIVO[r.motivo]||r.motivo)}</span><span>${fmtDH(r.inicio)} → ${fmtH(r.fim)}</span></div>
 <div class="met"><div><small>Bitrate médio</small><b class="big">${mb(r.taxa.media)}<small> Mb/s</small></b></div>
 <div><small>Mín / máx</small><b>${mb(r.taxa.min)} / ${mb(r.taxa.max)}</b></div><div><small>Duração</small><b>${dur(r.duracao_s)}</b></div></div>
 <div class="lamps" style="margin-top:8px">${TR.map(([k,nm,d])=>{const v=n[k]||0;return`<div class="lamp ${v?'w':''}" title="${esc(d)}"><i></i><small>${nm}</small><b>${v}</b></div>`}).join('')}</div>
 <div class="trp"><span class="${r.totais.p2?'w':''}">P2 · ${r.totais.p2}</span><span class="${r.totais.p3?'w':''}">P3 · ${r.totais.p3}</span>
 <span>${r.n_servicos} serv · ${r.n_pids} PIDs</span><a data-ana="${c.id}">Ver relatório →</a></div>
 ${r.servicos.length?`<div class="msg" style="margin-top:4px" title="${esc(r.servicos.join(', '))}">${esc(r.servicos.join(' · '))}</div>`:''}</div>`}
function pInt(s){const p=s&&s.perda_interna;return p&&p.recente?`<span class="pill pint" title="O próprio servidor descartou pacotes agora (CPU sobrecarregada ou buffer UDP pequeno). Erros de CC neste momento podem não estar no sinal. Total: análise ${p.analise}, distribuidor ${p.distribuidor}${p.saidas.some(x=>x)?', saídas '+p.saidas.join('/'):''}">⚠ perda interna</span>`:''}
function card(c){const s=c.status||{},ativo=s.estado!=='parado';s._id=c.id;
 return`<div class="card"><div class="top"><h2 title="${esc(c.nome)}">${esc(c.nome)}</h2><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span>${pInt(s)}</div>
 <div class="fluxo"><div><span class="l">Entrada</span><span class="tag">${esc(resE(c.entrada))}</span></div>
 <div><span class="l">Saídas</span>${c.saidas.length?c.saidas.map(x=>{const r=servRotulo(x,s.servicos);return`<span class="tag">${esc(resS(x))}${r?' · '+esc(r.map(q=>q.nome).join(', ')):''}</span>`}).join(''):'<span class="tag" style="color:var(--mut)">somente análise</span>'}</div></div>
 ${ativo&&(s.servicos||[]).length?`<div class="svs">${s.servicos.map(sv=>{const sai=c.saidas.some(x=>{const r=servRotulo(x,s.servicos);return !r||r.some(q=>String(q.numero)===String(sv.numero))});
   return prevBox(c.id,sv,sai&&c.saidas.length?'sai':'')}).join('')}</div>`:''}
 ${!ativo&&c.ultimo_relatorio?relCard(c):`${spark(s.historico)}
 <div class="met"><div>${(()=>{const x=taxaTS(s);return`<small>Bitrate ${seloModo(s.modo_ts,true)}</small><b class="big" title="taxa útil, sem pacotes nulos de enchimento${x.temNulos?` (o TS chega com ${mb(x.ts)} Mb/s)`:''}">${mb(x.ut)}<small> Mb/s</small></b>`})()}</div>
 <div><small>Reinícios</small><b>${s.reinicios||0}</b></div><div><small>Uptime</small><b>${dur(s.uptime_s)}</b></div></div>`}
 ${tr(s)}
 <div class="msg" title="${esc(s.ultima_msg)}">${esc(s.ultima_msg||'')}</div>
 <div class="acoes">${ativo?`<button data-a="parar" data-id="${c.id}">■ Parar</button><button data-a="reiniciar" data-id="${c.id}">↻ Reiniciar</button><button data-a="zerar" data-id="${c.id}" title="Zerar contadores">Zerar</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}">▶ Iniciar</button>`}
 ${ativo?`<button data-a="analise" data-id="${c.id}">Análise</button>`:c.ultimo_relatorio?`<button data-a="analise" data-id="${c.id}">Relatório</button>`:''}<button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button>
 <button class="perigo" data-a="excluir" data-id="${c.id}">Excluir</button></div></div>`}

let VIS='rotas';try{VIS=localStorage.getItem('asi2srt.vis')||'rotas'}catch(e){}
const TIPO_TXT={srt:'SRT',udp:'UDP',rtp:'RTP',rtmp:'RTMP',dektec:'ASI',linuxasi:'ASI',arquivo:'ARQ'};
const viaNic=x=>x.interface?` · placa ${x.interface}`:x.interface_local?` · IP ${x.interface_local}`:'';
function endTxt(x){return viaNic0(x)}
function viaNic0(x){return endTxtBase(x)+(x.tipo==='rtmp'?'':viaNic(x))}
{const _rE=resE,_rS=resS;resE=e=>_rE(e)+(['udp','srt'].includes(e.tipo)?viaNic(e):'');resS=s=>_rS(s)+(s.tipo==='rtmp'?'':viaNic(s))}
function endTxtBase(x){return x.tipo==='rtmp'?(x.chave?String(x.url||'').replace(/\/+$/,'')+'/••••':urlSemChave(x.url))+(x.video==='h264'?' · vídeo→H.264':'')+(x.audio==='aac'?' · áudio→AAC':''):x.tipo==='rtp'?x.endereco:x.tipo==='srt'?`${x.modo==='listener'?'listener :':'caller → '}${x.endereco}`:x.tipo==='udp'?x.endereco:x.tipo==='dektec'?`placa ${x.dispositivo??0} · porta ${x.canal??0}`:x.tipo==='linuxasi'?x.dispositivo:(x.caminho||'').split('/').pop()}
function mspark(h){if(!h||h.length<2)return'<svg class="mspark"></svg>';const v=h.map(x=>x[0]),mx=Math.max(...v)*1.2||1,mxp=Math.max(...h.map(x=>x[1]),1),W=90,H=22;
 const p=v.map((y,i)=>`${(i/(v.length-1)*W).toFixed(1)},${(H-y/mx*H).toFixed(1)}`).join(' ');
 const lp=h.some(x=>x[1]>0)?`<polyline class="lp" points="${h.map((x,i)=>`${(i/(h.length-1)*W).toFixed(1)},${(H-x[1]/mxp*H*.9).toFixed(1)}`).join(' ')}"/>`:'';
 return`<svg class="mspark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${p}"/>${lp}</svg>`}
function chipsSRT(e){if(e.estado!=='conectado')return'';const lat=e.latencia_ms||0;
 const cp=e.perda_pct>1?'e':e.perda_pct>0?'w':'',cr=lat&&e.rtt_ms>lat/4?'w':'';
 return`<span class="chip ${cr}"><small>RTT</small>${e.rtt_ms} ms</span><span class="chip ${cp}"><small>Perda</small>${(e.perda_pct||0).toFixed(2)}%</span>
 <span class="chip ${e.retransmitidos?'w':''}"><small>Retrans</small>${e.retransmitidos}/s</span><span class="chip"><small>Taxa</small>${(e.taxa_mbps||0).toFixed(2)} Mb/s</span>
 ${e.papel==='entrada'?`<span class="chip ${e.descartados?'e':''}" title="pacotes que o SRT não conseguiu recuperar a tempo (viram erros no TS)"><small>Não recup.</small>${e.descartados}/s</span>`:''}
 ${e.banda_mbps&&e.papel!=='entrada'?`<span class="chip"><small>Banda</small>${e.banda_mbps} Mb/s</span>`:''}<span class="chip"><small>Buffer</small>${e.buffer_ms} ms</span>`}
const msgCurta=m=>String(m||'').replace(/^\*\s*(Error|Warning):\s*/i,'').replace(/^srt:\s*/,'').replace(/^\[saída \d+\]\s*/,'').slice(0,90);
function nicBadge(x,fo,ativo){if(x.tipo==='rtmp')return'';
 if(fo&&ativo){const tip=`Principal: ${fo.principal} (${fo.principal_ok?'ok':'fora do ar'}) · Reserva: ${fo.reserva} (${fo.reserva_ok?'ok':'fora do ar'})`
   +(fo.motivo?`\nNa reserva porque: ${fo.motivo}`:'')+(fo.trocas?`\n${fo.trocas} troca(s) nesta sessão`:'');
  return`<span class="nicb${fo.em_reserva?' res':''}" title="${esc(tip)}">${fo.em_reserva?'⚠ ':''}${esc(fo.ativa)}${fo.em_reserva?' · reserva':''}</span>${fo.em_reserva?'':`<span class="nicr" title="${esc(tip)}">↺ ${esc(fo.reserva)}</span>`}`}
 const n=x.interface||x.interface_local;if(!n)return'';
 return`<span class="nicb" title="placa de rede usada">${esc(n)}</span>${x.interface_reserva?`<span class="nicr" title="placa reserva (failover)">↺ ${esc(x.interface_reserva)}</span>`:''}`}
function pRede(s){const r=s&&s.rede;if(!r)return'';const l=[r.entrada,...(r.saidas||[])].filter(f=>f&&f.em_reserva);
 return l.length?`<span class="pill pint" title="${esc(l.map(f=>`${f.ativa} no lugar de ${f.principal}: ${f.motivo||''}`).join('\n'))}">⚠ placa reserva</span>`:''}
function no(rot,x,e,ativo,ps,cid,lista,fo){const tp=TIPO_TXT[x.tipo]||x.tipo,cl=x.tipo==='udp'?'udp':(x.tipo==='dektec'||x.tipo==='linuxasi')?'asi':'';
 const falha=ativo&&ps&&ps.estado!=='rodando'&&ps.ultima_msg&&(!e||e.estado!=='conectado')?`<span class="chip w" title="${esc(ps.ultima_msg)}">⚠ ${esc(msgCurta(ps.ultima_msg))}${ps.reinicios?` · ${ps.reinicios} tentativa(s)`:''}</span>`:'';
 let st;if(e){const tx={conectado:'conectado',aguardando:x.modo==='listener'?'aguardando conexão':'conectando…',parado:'parado'}[e.estado];
  st=`<span class="st ${e.estado}"><i></i>${tx}${e.peer?` · <code>${esc(e.peer)}</code>`:''}${e.estado==='conectado'&&e.conectado_s?` · ${dur(e.conectado_s)}`:''}</span>`}
 else if(ps&&ativo){const ok=ps.estado==='rodando';st=`<span class="st ${ok?'conectado':'aguardando'}"><i></i>${ok?'no ar':({conectando:'iniciando…',reiniciando:'tentando de novo'}[ps.estado]||ps.estado)}${ok&&ps.uptime_s?` · ${dur(ps.uptime_s)}`:''}</span>`}
 else st=`<span class="st ${ativo?'ativo':'parado'}"><i></i>${ativo?'ativo':'parado'}</span>`;
 const sr=rot.startsWith('Destino')?servRotulo(x,lista):null;
 const chipServ=rot.startsWith('Destino')?(sr?`<span class="chip svout" title="serviços enviados por este destino">${sr.length===1&&sr[0].video&&PREV[cid+':'+sr[0].numero]&&PREV[cid+':'+sr[0].numero].u?`<img data-prev="${cid}:${sr[0].numero}" src="${PREV[cid+':'+sr[0].numero].u}">`:''}<small>Serviço</small>${esc(sr.map(s=>s.nome).join(', '))}</span>`
  :`<span class="chip"><small>Serviços</small>todos (TS completo)</span>`):'';
 return`<div class="no"><span class="pp">${rot}</span><span class="ed"><span class="tp ${cl}">${tp}</span><code title="${esc(endTxt(x))}">${esc(endTxtBase(x))}</code>${nicBadge(x,fo,ativo)}</span>
 <span class="chips">${st}${chipServ}${e?chipsSRT(e):''}${ps&&ps.estado==='rodando'?`${!e&&ps.info&&ps.info.taxa_kbps?`<span class="chip"><small>Taxa</small>${(ps.info.taxa_kbps/1000).toFixed(2)} Mb/s</span>`:''}<span class="chip"><small>Enviado</small>${fmtB(ps.enviado_conexao_bytes)}</span>${ps.info&&ps.info.velocidade?`<span class="chip"><small>Veloc.</small>${esc(ps.info.velocidade)}</span>`:''}${ps.reinicios?`<span class="chip w" title="vezes que a saída caiu e reconectou"><small>Quedas</small>${ps.reinicios}</span>`:''}`:''}${falha}</span>${rot==='Origem'?'<span></span>':e&&e.estado==='conectado'?mspark(e.hist):ps&&ps.estado==='rodando'&&ps.hist&&ps.hist.length>1?mspark(ps.hist.map(v=>[v,0,0])):'<span></span>'}</div>`}
const PREV={};let prevAlvos=new Set();
const mbps=b=>b>0&&b<1e6?(b/1e3).toFixed(b<1e4?1:0)+' kb/s':(b/1e6).toFixed(2)+' Mb/s';
function seloModo(m,curto){if(!m)return'';const ehTS=!curto||true;if(!m.modo)return curto?'':`<span class="modo med" title="medindo a variação da taxa (${m.amostras||0} s)">medindo…</span>`;
 const tip=`${m.modo==='CBR'?'Taxa constante':'Taxa variável'} — variação ${m.variacao_pct}% nos últimos ${m.amostras} s`+(m.min?` (mín ${mbps(m.min)}, máx ${mbps(m.max)})`:'')
  +(m.modo==='CBR'?'. No TS, CBR costuma significar enchimento com pacotes nulos (removidos nas saídas).':'');
 return`<span class="modo ${m.modo.toLowerCase()}" title="${esc(tip)}">${m.modo}${curto?'':` <small>±${m.variacao_pct}%</small>`}</span>`}
function taxaTS(s){const ts=s.taxa_ts||s.bitrate_bps||0,nul=s.taxa_nulos||0,ut=s.taxa_util||(s.taxa_ts?0:ts);
 return{ts,ut,nul,temNulos:ts>0&&nul/ts>0.02}}
function prevBox(cid,s,cls){const k=`${cid}:${s.numero}`,p=PREV[k]||{};
 const msg=!s.video?'♪ sem vídeo':p.u?'':esc(p.erro||'carregando imagem…');
 if(s.video)prevAlvos.add(k);
 return`<div class="pv ${cls||''}" title="${esc(s.nome)} · serviço ${s.numero}"><div class="im">${s.video?`<img data-prev="${k}" ${p.u?`src="${p.u}"`:'hidden'} alt="">`:''}
 <div class="pvm" data-prevmsg="${k}" ${p.u?'hidden':''}>${msg}</div></div><div class="pvn"><b>${esc(s.nome)}</b>${s.taxa?`<span class="pvt" title="taxa deste serviço (vídeo + áudio + dados do programa)">${mbps(s.taxa)}</span>${seloModo(s.modo,true)}`:''}<small>${s.numero}</small></div></div>`}
let prevOcupado=false;
async function atualizarPrevias(){if(prevOcupado||document.hidden)return;prevOcupado=true;
 try{for(const k of [...prevAlvos]){const [cid,sid]=k.split(':');
  try{const r=await fetch(`/api/canais/${cid}/previa/${sid}.jpg`,{cache:'no-store'});
   if(r.ok){const u=URL.createObjectURL(await r.blob()),old=PREV[k];PREV[k]={u};
    document.querySelectorAll(`img[data-prev="${k}"]`).forEach(i=>{i.src=u;i.hidden=false});
    document.querySelectorAll(`[data-prevmsg="${k}"]`).forEach(m=>m.hidden=true);
    if(old&&old.u)setTimeout(()=>URL.revokeObjectURL(old.u),5000)}
   else{const j=await r.json().catch(()=>({}));if(!(PREV[k]||{}).u){PREV[k]={erro:j.erro||'sem imagem'};
    document.querySelectorAll(`[data-prevmsg="${k}"]`).forEach(m=>m.textContent=PREV[k].erro)}}}catch(e){}}}
 finally{prevOcupado=false}}
setInterval(atualizarPrevias,3000);
function servRotulo(x,lista){const sv=x.servicos||(x.programa?[x.programa]:[]);if(!sv.length)return null;
 return sv.map(v=>{const n=(lista||[]).find(s=>String(s.numero)===String(v)||s.nome.toLowerCase().replace(/\s/g,'')===String(v).toLowerCase().replace(/\s/g,''));
  return n?{numero:n.numero,nome:n.nome,video:n.video}:{numero:v,nome:String(v),video:false}})}
const IND=[['sync_loss','TS sync loss'],['cc','Continuity count'],['pcr_rep','PCR repetition'],['pcr_disc','PCR discontinuity'],['pts','PTS error']];
const NOME_IND={sync_loss:'TS sync loss',sync_byte:'Sync byte',pat:'PAT',cc:'Continuity count',pmt:'PMT',pid:'PID',
 transport:'Transport',crc:'CRC',pcr_rep:'PCR repetition',pcr_disc:'PCR discontinuity',pcr_acc:'PCR accuracy',pts:'PTS',cat:'CAT',
 nit:'NIT',unref:'PID não referenciado',sdt:'SDT',eit:'EIT',tdt:'TDT'};
function indicadores(t,r){const cont=t?t.contadores:r?r.contadores:null;if(!cont||!TRI.length)return'';const rec=t?t.recentes||[]:[],na=t?t.nao_aplicavel||[]:[];
 const grupo=p=>{const itens=TRI.filter(x=>x[1]===p),soma=itens.reduce((a,x)=>a+(cont[x[0]]||0),0),err=itens.some(x=>rec.includes(x[0]));
  return`<div class="ig"><span class="igl ${err?'e':soma?'w':''}">Prioridade ${p}</span>${itens.map(([k,,cod,nome,desc])=>{const v=cont[k]||0,
   cl=rec.includes(k)?'e':v?'w':(t&&!t.sinal)||na.includes(k)?'x':'';
   return`<span class="ic ${cl}" title="${esc(cod+' '+nome+' — '+desc)}${na.includes(k)&&!v?' (não se aplica a este TS)':''}"><i></i>${esc(NOME_IND[k]||nome)}<b>${na.includes(k)&&!v?'n/a':v.toLocaleString('pt-BR')}</b></span>`}).join('')}</div>`};
 return`<div class="ind">${grupo(1)}${grupo(2)}${grupo(3)}<span class="sub">${t?(t.sinal?'sessão atual':'sem sinal'):'última sessão'}</span></div>`}
function grafRota(s,e){const h=(s.historico||[]).slice(-60),W=300,H=100;
 const ep=e&&e.estado==='conectado'&&e.hist&&e.hist.length>1?e.hist.slice(-60).map(x=>x[1]):null;
 if(h.length<2)return`<div class="gpv"><div class="gh"><b>Taxa útil</b></div><div class="gv gm">aguardando dados…</div></div>`;
 const mx=Math.max(...h)*1.15||1,xs=(i,n)=>(i/(n-1)*W).toFixed(1),p=h.map((y,i)=>`${xs(i,h.length)},${(H-y/mx*H).toFixed(1)}`).join(' ');
 const at=h[h.length-1],hv=h.filter(x=>x>0),mn=hv.length?Math.min(...hv):0,mxv=Math.max(...h);
 let lp='',pa='';if(ep){const mp=Math.max(...ep,1);
  lp=`<polyline class="lp" points="${ep.map((y,i)=>`${xs(i,ep.length)},${(H-y/mp*H*.9).toFixed(1)}`).join(' ')}"/>`;
  const pv=ep[ep.length-1];pa=`<span class="${pv>1?'e':pv>0?'w':''}"><i class="lr"></i>Perda SRT <b>${pv.toFixed(2)}%</b></span>`}
 const grade=[.25,.5,.75].map(f=>`<line x1="0" x2="${W}" y1="${(H*f).toFixed(1)}" y2="${(H*f).toFixed(1)}"/>`).join('');
 return`<div class="gpv" title="taxa útil (sem pacotes nulos) nos últimos ${h.length} s${ep?'; vermelho = perda SRT na entrada':''}">
 <div class="gh"><span><i class="lb"></i>Taxa útil <b>${mbps(at)}</b></span>${pa}<small>${h.length} s</small></div>
 <div class="gv"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><g class="gg">${grade}</g><polygon class="ar" points="0,${H} ${p} ${W},${H}"/><polyline points="${p}"/>${lp}</svg>
 <span class="gx t">${mbps(mx)}</span><span class="gx b">0</span></div>
 <div class="gf"><span>mín <b>${mbps(mn)}</b></span><span>máx <b>${mbps(mxv)}</b></span>${s.modo_ts&&s.modo_ts.modo?seloModo(s.modo_ts):''}</div></div>`}
let ABERTOS=new Set();try{ABERTOS=new Set(JSON.parse(localStorage.getItem('asi2srt.abertos')||'[]'))}catch(e){}
function indCompacto(t,r){const cont=t?t.contadores:r?r.contadores:null;if(!cont||!TRI.length)return'';const rec=t?t.recentes||[]:[],na=t?t.nao_aplicavel||[]:[];
 const g=p=>{const it=TRI.filter(x=>x[1]===p),ruins=it.filter(([k])=>cont[k]||rec.includes(k)),err=ruins.some(([k])=>rec.includes(k)),
  oks=it.filter(([k])=>!na.includes(k)&&!cont[k]),cls=err?'e':ruins.length?'w':(t&&!t.sinal)?'x':'ok';
  return`<span class="cg ${cls}"><span class="cgl" title="Prioridade ${p}">P${p}</span>${ruins.map(([k,,cod,nome,desc])=>`<span class="ic ${rec.includes(k)?'e':'w'}" title="${esc(cod+' '+nome+' — '+desc)}"><i></i>${esc(NOME_IND[k]||nome)}<b>${(cont[k]||0).toLocaleString('pt-BR')}</b></span>`).join('')}${oks.length?`<span class="cok" title="sem erros: ${esc(oks.map(([k,,,n])=>NOME_IND[k]||n).join(', '))}">${ruins.length?'+':'✓'} ${oks.length} ok</span>`:''}</span>`};
 return`<div class="indc">${g(1)}${g(2)}${g(3)}<span class="sub">${t?(t.sinal?'sessão atual':'sem sinal'):'última sessão'}</span></div>`}
document.addEventListener('click',e=>{const b=e.target.closest('[data-x]');if(!b)return;const id=b.dataset.x;
 ABERTOS.has(id)?ABERTOS.delete(id):ABERTOS.add(id);try{localStorage.setItem('asi2srt.abertos',JSON.stringify([...ABERTOS]))}catch(x){}render()});
function linha(c){const s=c.status||{},ativo=s.estado!=='parado',srt=s.srt||{},t=s.tr101290,r=c.ultimo_relatorio,ab=ABERTOS.has(c.id);
 const dots='';const _dotsAntigo=t?`<span class="dots" title="TR 101 290 · prioridade 1">${TR.map(([k,n,d])=>{const v=t.contadores[k]||0;return`<i class="${t.recentes.includes(k)?'e':v?'w':t.sinal?'':'x'}" title="${esc(n)}: ${v}"></i>`}).join('')}
  <small>${(()=>{const p2=TRI.filter(x=>x[1]===2).reduce((a,x)=>a+(t.contadores[x[0]]||0),0),p3=TRI.filter(x=>x[1]===3).reduce((a,x)=>a+(t.contadores[x[0]]||0),0);return`P2 ${p2} · P3 ${p3}`})()}</small></span>`
  :r?`<span class="dots">${TR.map(([k,n])=>`<i class="${(r.contadores[k]||0)?'w':''}" title="${esc(n)}: ${r.contadores[k]||0}"></i>`).join('')}<small>última sessão</small></span>`:'';
 const acs=(ativo?`<button data-a="parar" data-id="${c.id}">■ Parar</button><button data-a="reiniciar" data-id="${c.id}">↻</button><button data-a="analise" data-id="${c.id}">Análise</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}">▶ Iniciar</button>${r?`<button data-a="analise" data-id="${c.id}">Relatório</button>`:''}`)
  +`<button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button><button class="perigo" data-a="excluir" data-id="${c.id}">✕</button>`
  +`<button class="bx" data-x="${c.id}" title="${ab?'recolher detalhes':'ver todos os indicadores e imagens maiores'}">${ab?'▴':'▾'}</button>`;
 const destinos=c.saidas.length?c.saidas.map((x,i)=>no('Destino '+(i+1),x,srt['SRTOUT'+i],ativo,(s.saidas||[])[i],c.id,s.servicos,((s.rede||{}).saidas||[])[i])).join(''):'<div class="no"><span class="pp">Destino</span><span class="ed" style="color:var(--mut)">somente análise</span><span></span><span></span></div>';
 return`<div class="rota e-${esc(s.estado)}${ab?'':' cp'}"><div class="rh"><b class="rn" data-ana="${c.id}">${esc(c.nome)}</b><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span>${pInt(s)}${pRede(s)}
 ${dots}<span class="rb">${ativo?(()=>{const x=taxaTS(s);return`<span title="taxa útil: só o conteúdo, sem pacotes nulos de enchimento${x.temNulos?` (o TS chega com ${mb(x.ts)} Mb/s com enchimento)`:''}">${mb(x.ut)} <small>Mb/s</small></span> ${seloModo(s.modo_ts)}`})():r?`<small>méd.</small> ${mb(r.taxa.media)} <small>Mb/s</small>`:''}</span><span class="acs">${acs}</span></div>
 ${ab?indicadores(ativo?t:null,ativo?null:r):indCompacto(ativo?t:null,ativo?null:r)}
 <div class="rl">${no('Origem',c.entrada,srt.SRTIN,ativo,undefined,undefined,undefined,(s.rede||{}).entrada)}
 ${ativo&&(s.servicos||[]).length?`<div class="svs">${s.servicos.map(sv=>{const sai=c.saidas.some(x=>{const r=servRotulo(x,s.servicos);return !r||r.some(q=>String(q.numero)===String(sv.numero))});
   return prevBox(c.id,sv,sai&&c.saidas.length?'sai':'')}).join('')}${grafRota(s,srt.SRTIN)}</div>`:''}${destinos}
 ${!ativo&&r?`<div class="rrel">Última análise ${esc(MOTIVO[r.motivo]||r.motivo)}: <b>${fmtDH(r.inicio)} → ${fmtH(r.fim)}</b> · ${dur(r.duracao_s)} · ${r.n_servicos} serviço(s)${r.servicos.length?': '+esc(r.servicos.join(', ')):''}</div>`:''}
 ${ativo&&s.estado!=='rodando'&&s.ultima_msg?`<div class="rrel" style="color:var(--warn)">${esc(s.ultima_msg)}</div>`:''}</div></div>`}
document.addEventListener('click',e=>{const b=e.target.closest('[data-vis]');if(!b)return;VIS=b.dataset.vis;try{localStorage.setItem('asi2srt.vis',VIS)}catch(x){}render()});
function linhaLista(c){const s=c.status||{},ativo=s.estado!=='parado',t=s.tr101290,r=c.ultimo_relatorio,e=(s.srt||{}).SRTIN;
 const cont=ativo&&t?t.contadores:r?r.contadores:null,rec=ativo&&t?t.recentes||[]:[];
 const pr=p=>{if(!cont||!TRI.length)return'<td class="n">—</td>';const it=TRI.filter(x=>x[1]===p),soma=it.reduce((a,[k])=>a+(cont[k]||0),0),err=it.some(([k])=>rec.includes(k));
  const det=it.filter(([k])=>cont[k]).map(([k,,,n])=>`${NOME_IND[k]||n}: ${cont[k].toLocaleString('pt-BR')}`).join('\n');
  return`<td class="n"><span class="pb ${err?'e':soma?'w':ativo&&t&&!t.sinal?'x':'ok'}" title="${esc(det||'sem erros')}">${soma.toLocaleString('pt-BR')}</span></td>`};
 const sv=(ativo?s.servicos||[]:[]).find(x=>x.video);let th='<span class="th v">—</span>';
 if(sv){const k=`${c.id}:${sv.numero}`,pv=PREV[k]||{};prevAlvos.add(k);th=pv.u?`<img class="th" data-prev="${k}" src="${pv.u}" alt="">`:`<span class="th v" data-prevmsg="${k}">…</span>`}
 const x=taxaTS(s),said=s.saidas||[],noar=said.filter(p=>p.estado==='rodando').length;
 const tp=TIPO_TXT[c.entrada.tipo]||c.entrada.tipo,cl=c.entrada.tipo==='udp'?'udp':(c.entrada.tipo==='dektec'||c.entrada.tipo==='linuxasi')?'asi':'';
 const srtTxt=e&&e.estado==='conectado'?` <span class="chip ${e.perda_pct>1?'e':e.perda_pct>0?'w':''}" title="SRT: RTT ${e.rtt_ms} ms, perda ${(e.perda_pct||0).toFixed(2)}%"><small>RTT</small>${e.rtt_ms} · <small>perda</small>${(e.perda_pct||0).toFixed(1)}%</span>`:'';
 const acs=(ativo?`<button data-a="parar" data-id="${c.id}" title="parar">■</button><button data-a="analise" data-id="${c.id}">Análise</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}" title="iniciar">▶</button>${r?`<button data-a="analise" data-id="${c.id}">Relatório</button>`:''}`)
  +`<button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button>`;
 return`<tr class="e-${esc(s.estado)}"><td>${th}</td><td><b class="rn" data-ana="${c.id}">${esc(c.nome)}</b> ${pInt(s)}${pRede(s)}</td>
 <td><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span></td>
 <td class="n">${ativo?`<b>${mb(x.ut)}</b> <small style="color:var(--mut)">Mb/s</small>`:r?`<small style="color:var(--mut)">méd.</small> ${mb(r.taxa.media)}`:'—'}</td>
 <td>${ativo?seloModo(s.modo_ts,true):''}</td>${pr(1)}${pr(2)}${pr(3)}
 <td class="ent" title="${esc(endTxt(c.entrada))}"><span class="tp ${cl}">${tp}</span> <code>${esc(endTxtBase(c.entrada))}</code> ${ativo?nicBadge(c.entrada,(s.rede||{}).entrada,ativo):''}${srtTxt}</td>
 <td>${c.saidas.length?`<span class="pb ${!ativo?'x':noar===c.saidas.length?'ok':noar?'w':'e'}">${ativo?noar:0}/${c.saidas.length}</span> <small style="color:var(--mut)">no ar</small>`:'<small style="color:var(--mut)">só análise</small>'}</td>
 <td><div class="acs">${acs}</div></td></tr>`}
function lista(){return`<div class="lista"><table><tr><th></th><th>Canal</th><th>Estado</th><th class="n">Taxa útil</th><th>Tipo</th>
 <th class="n" title="TR 101 290 prioridade 1">P1</th><th class="n" title="prioridade 2">P2</th><th class="n" title="prioridade 3">P3</th><th>Entrada</th><th>Saídas</th><th></th></tr>${dados.map(linhaLista).join('')}</table></div>`}
// ---------- comparação de sinais (o usuário escolhe as entradas) ----------
let CMP=null,CMP_DADOS={},CMP_SESS={},CMP_SO_DIF=false,CMP_SO_ERR=false,CMP_SEM_NREF=false,CMP_ULT=0,CMP_BAR='';
try{const v=JSON.parse(localStorage.getItem('asi2srt.cmp2')||'null');if(Array.isArray(v))CMP=v.filter(x=>x&&x.id)}catch(e){}
function cmpSalvar(){try{localStorage.setItem('asi2srt.cmp2',JSON.stringify(CMP))}catch(e){}}
const vivoC=id=>{const c=dados.find(x=>x.id===id);return!!(c&&(c.status||{}).estado!=='parado')};
const ckey=x=>`${x.id}|${x.sessao==null?'v':x.sessao}`;
function cmpIniciar(){if(!CMP){const r=dados.filter(c=>vivoC(c.id)).slice(0,2).map(c=>({id:c.id,sessao:null}));
  CMP=r.length>=2?r:dados.slice(0,2).map(c=>({id:c.id,sessao:vivoC(c.id)?null:0}));cmpSalvar()}
 CMP=CMP.filter(x=>dados.some(c=>c.id===x.id))}
async function cmpSessoes(id){try{CMP_SESS[id]=await api('GET',`/api/canais/${id}/relatorios`)}catch(e){CMP_SESS[id]=[]}}
async function cmpBuscar(){if(VIS!=='comparar'||anaId||!CMP)return;CMP_ULT=Date.now();
 await Promise.all(CMP.map(async x=>{if(x.sessao!=null&&CMP_DADOS[ckey(x)]&&!CMP_DADOS[ckey(x)].erro)return;  // sessão salva não muda
  try{const r=await fetch(`/api/canais/${x.id}/analise`+(x.sessao!=null?`?sessao=${x.sessao}`:''),{headers:H});
   if(r.status===401){irLogin();return}const j=await r.json().catch(()=>({}));CMP_DADOS[ckey(x)]=r.ok?j:{erro:j.erro||'sem dados'}}catch(e){CMP_DADOS[ckey(x)]={erro:'sem conexão'}}}));
 render()}
const ccServ=(a,sv)=>(sv.componentes||[]).reduce((s,c)=>s+(((a.pids||[]).find(p=>p.pid===c.pid)||{}).cc||0),0);
function rotSess(x){if(x.sessao==null)return'ao vivo';const s=(CMP_SESS[x.id]||[]).find(r=>r.sessao===x.sessao);return s?`sessão ${fmtDH(s.inicio)}`:`sessão ${x.sessao+1}`}
function cmpBarra(){const cols=CMP.map((x,i)=>{const viv=vivoC(x.id),ss=CMP_SESS[x.id]||[];
  const opC=dados.map(c=>`<option value="${c.id}" ${c.id===x.id?'selected':''}>${esc(c.nome)}${vivoC(c.id)?'':' (parado)'}</option>`).join('');
  const opS=(viv?[`<option value="v" ${x.sessao==null?'selected':''}>Ao vivo</option>`]:[]).concat(ss.map(r=>`<option value="${r.sessao}" ${x.sessao===r.sessao?'selected':''}>${fmtDH(r.inicio)} → ${fmtH(r.fim)} (${dur(r.duracao_s)})</option>`)).join('')||'<option value="">sem sessões salvas</option>';
  return`<div class="ccol"><span class="cnum">${String.fromCharCode(65+i)}</span><label>Entrada<select data-cc="${i}" data-k="id">${opC}</select></label>
   <label>Dados<select data-cc="${i}" data-k="sessao">${opS}</select></label>${CMP.length>2?`<button class="mini" data-ccx="${i}" title="tirar esta entrada da comparação">✕</button>`:''}</div>`}).join('');
 return`<div class="cmpsel"><div class="cmpsel-h"><b>Entradas comparadas</b><span>Escolha o canal de cada coluna e se quer os dados ao vivo ou de uma sessão salva (dá para comparar o mesmo canal agora × ontem).</span></div>
  <div class="ccols">${cols}${CMP.length<6?`<button class="cadd" data-ccadd="1">+ Adicionar entrada</button>`:''}</div>
  <div class="cmpb"><span class="sp"></span><button data-cmpf="dif" class="${CMP_SO_DIF?'on':''}" title="mostrar só linhas que diferem entre as entradas">Só diferenças</button>
  <button data-cmpf="err" class="${CMP_SO_ERR?'on':''}" title="mostrar só PIDs/serviços com erro de continuidade">Só com erros CC</button><button data-cmpf="nref" class="${CMP_SEM_NREF?'on':''}" title="esconder PIDs que não aparecem em nenhuma PMT (dados privados do multiplexador)">Ocultar não referenciados</button><button data-cmpf="csv">Baixar CSV</button></div></div>`}
const melhor=ps=>{const v=ps.filter(Boolean);return v.find(p=>p.tipo!=='desconhecido')||v[0]||{}};
function comparar(){const A=CMP.map(x=>CMP_DADOS[ckey(x)]||{carregando:true}),sel=CMP.map(x=>dados.find(c=>c.id===x.id)||{nome:'?'});
 const OK=A.map(a=>!a.erro&&!a.carregando),semd=i=>`<span class="sm" title="${esc(A[i].erro||'carregando')}">${A[i].carregando?'…':'sem dados'}</span>`;
 if(CMP.length<2)return`<div class="cmp"><div class="vazio">Adicione pelo menos 2 entradas para comparar.</div></div>`;
 const cab=CMP.map((x,i)=>`<th class="cn" title="${A[i].erro?esc(A[i].erro):''}"><span class="cnum">${String.fromCharCode(65+i)}</span> ${esc(sel[i].nome)}<div class="sm" style="margin:2px 0 0">${esc(rotSess(x))}</div></th>`).join('');
 const prio=(a,p)=>{const c=a&&a.tr101290&&a.tr101290.contadores;return c&&TRI.length?TRI.filter(x=>x[1]===p).reduce((s,[k])=>s+(c[k]||0),0):null};
 const ccTot=a=>(a.pids||[]).reduce((s,p)=>s+(p.cc||0),0);
 const lin=(rot,f)=>`<tr><td>${rot}</td>${A.map((a,i)=>`<td class="c">${OK[i]?f(a):a.erro?'<span class="aus">'+esc(a.erro)+'</span>':'…'}</td>`).join('')}</tr>`;
 const num=v=>v==null?'—':v.toLocaleString('pt-BR'),cor=v=>v?`<span class="cce">${num(v)}</span>`:`<span class="cc0">0</span>`;
 const resumo=`<div class="cmp"><h3>Resumo</h3><table><tr><th></th>${cab}</tr>
  ${lin('Taxa útil',a=>{const r=a.resumo||{},tx=a.salvo&&a.taxa_sessao?a.taxa_sessao.media:r.taxa_util;return tx!=null?mb(tx)+' Mb/s'+(a.salvo?' <span class="sm">média</span>':''):'—'})}
  ${lin('Tipo do TS',a=>{const m=(a.resumo||{}).modo_taxa;return m&&m.modo?seloModo(m,true):'—'})}
  ${lin('Serviços',a=>num((a.servicos||[]).length))}${lin('PIDs',a=>num((a.pids||[]).length))}
  ${lin('Erros CC (total)',a=>cor(ccTot(a)))}${lin('TR 101 290 · P1',a=>cor(prio(a,1)))}${lin('TR 101 290 · P2',a=>cor(prio(a,2)))}${lin('TR 101 290 · P3',a=>cor(prio(a,3)))}
 </table></div>`;
 const nums=[...new Set(A.flatMap(a=>(a.servicos||[]).map(s=>s.numero)))].sort((x,y)=>x-y);
 let ls=nums.map(n=>{const ss=A.map(a=>(a.servicos||[]).find(s=>s.numero===n));const nomes=new Set(ss.filter(Boolean).map(s=>s.nome));
  const dif=ss.some((s,i)=>!s&&OK[i])||nomes.size>1,ccs=ss.map((s,i)=>s?ccServ(A[i],s):0),err=ccs.some(x=>x);
  return{dif,err,html:`<tr class="${err?'err':dif?'dif':''}"><td class="n">${n}</td><td>${esc((ss.find(Boolean)||{}).nome||'')}${nomes.size>1?`<span class="neq" title="nome diferente entre as entradas">≠</span>`:''}</td>
  ${ss.map((s,i)=>`<td class="c">${!OK[i]?semd(i):s?`${nomes.size>1?esc(s.nome)+' · ':''}${mbps(s.taxa)} <span class="sm">CC</span> ${cor(ccs[i])}`:'<span class="aus">ausente</span>'}</td>`).join('')}</tr>`}});
 if(CMP_SO_DIF)ls=ls.filter(x=>x.dif);if(CMP_SO_ERR)ls=ls.filter(x=>x.err);
 const servs=`<div class="cmp"><h3>Serviços <small>${nums.length} no total · bitrate e erros CC de cada entrada</small></h3><table><tr><th class="n">Nº</th><th>Serviço</th>${cab}</tr>
  ${ls.map(x=>x.html).join('')||`<tr><td colspan="${2+CMP.length}" class="vazio">nenhum serviço ${CMP_SO_DIF||CMP_SO_ERR?'com esse filtro':''}</td></tr>`}</table></div>`;
 const pids=[...new Set(A.flatMap(a=>(a.pids||[]).map(p=>p.pid)))].sort((x,y)=>x-y);
 let lp=pids.map(pid=>{const ps=A.map(a=>(a.pids||[]).find(p=>p.pid===pid));const um=melhor(ps);
  const descs=new Set(ps.filter(p=>p&&p.tipo!=='desconhecido').map(p=>p.desc)),svs=new Set(ps.filter(Boolean).map(p=>p.servico||''));
  const nref=ps.map(p=>!!p&&p.tipo==='desconhecido'),algumNref=nref.some(Boolean)&&um.tipo!=='desconhecido';
  const dif=ps.some((p,i)=>!p&&OK[i])||descs.size>1||svs.size>1||algumNref,err=ps.some(p=>p&&p.cc),desc=um.tipo==='desconhecido';
  return{dif,err,pid,ps,um,html:`<tr class="${err?'err':dif?'dif':''}"><td class="n">${pid}</td><td><code>${esc(um.hex||'')}</code></td>
  <td style="${desc?'color:#ff8f8a':um.tipo==='ignorado'?'color:var(--mut);font-style:italic':''}">${esc(um.desc||'')}${descs.size>1?`<span class="neq" title="${esc([...descs].join(' / '))}">≠</span>`:''}</td>
  <td>${esc(um.servico||'')}${svs.size>1?`<span class="neq" title="${esc([...svs].join(' / '))}">≠</span>`:''}</td>
  ${ps.map((p,i)=>`<td class="c">${!OK[i]?semd(i):p?`${cor(p.cc)}<span class="sm">${mbps(p.taxa)}</span>${nref[i]&&algumNref?'<span class="neq" title="nesta entrada o PID não está em nenhuma PMT">não ref.</span>':''}`:'<span class="aus">ausente</span>'}</td>`).join('')}</tr>`}});
 const nNref=lp.filter(x=>x.um.tipo==='desconhecido').length;
 if(CMP_SO_DIF)lp=lp.filter(x=>x.dif);if(CMP_SO_ERR)lp=lp.filter(x=>x.err);if(CMP_SEM_NREF)lp=lp.filter(x=>x.um.tipo!=='desconhecido');
 window._cmpCsv=()=>{const q=v=>`"${String(v==null?'':v).replace(/"/g,'""')}"`,nm=CMP.map((x,i)=>`${String.fromCharCode(65+i)} ${sel[i].nome} (${rotSess(x)})`);
  const L=[['Dec','Hex','Descrição','Serviço',...nm.flatMap(n=>[n+' - Erros CC',n+' - Bitrate (b/s)'])].map(q).join(';')];
  lp.forEach(x=>L.push([x.pid,x.um.hex,x.um.desc,x.um.servico,...x.ps.flatMap((p,i)=>!OK[i]?['sem dados','']:p?[p.cc,Math.round(p.taxa||0)]:['ausente',''])].map(q).join(';')));
  const b=new Blob(['﻿'+L.join('\r\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(b),el=document.createElement('a');
  el.href=u;el.download='comparacao-pids-'+new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')+'.csv';document.body.appendChild(el);el.click();el.remove();setTimeout(()=>URL.revokeObjectURL(u),2000)};
 const tabp=`<div class="cmp"><h3>PIDs <small>${pids.length} no total${nNref?` · <span style="color:#ff8f8a">${nNref} não referenciado(s)</span>`:''} · erros de continuidade (CC) e bitrate em cada entrada · <span style="color:var(--warn)">▌</span> difere · <span style="color:var(--err)">▌</span> com erro CC</small></h3>
  <table><tr><th class="n">Dec</th><th>Hex</th><th>Descrição</th><th>Serviço</th>${cab}</tr>
  ${lp.map(x=>x.html).join('')||`<tr><td colspan="${4+CMP.length}" class="vazio">nenhum PID ${CMP_SO_DIF||CMP_SO_ERR?'com esse filtro':''}</td></tr>`}</table></div>`;
 return resumo+servs+tabp}
async function cmpMudou(){cmpSalvar();CMP_BAR='';await Promise.all([...new Set(CMP.map(x=>x.id))].map(cmpSessoes));CMP_BAR='';render();cmpBuscar()}
document.addEventListener('change',e=>{const s=e.target.closest('[data-cc]');if(!s)return;const i=+s.dataset.cc,x=CMP[i];
 if(s.dataset.k==='id'){x.id=s.value;x.sessao=vivoC(x.id)?null:0}else x.sessao=s.value==='v'||s.value===''?null:+s.value;cmpMudou()});
document.addEventListener('click',e=>{const b=e.target.closest('[data-ccx],[data-ccadd],[data-cmpf]');if(!b)return;
 if(b.dataset.ccx!=null){CMP.splice(+b.dataset.ccx,1);cmpMudou();return}
 if(b.dataset.ccadd){const usados=new Set(CMP.map(x=>x.id)),c=dados.find(c=>!usados.has(c.id))||dados[0];if(c){CMP.push({id:c.id,sessao:vivoC(c.id)?null:0});cmpMudou()}return}
 const f=b.dataset.cmpf;if(f==='nref')CMP_SEM_NREF=!CMP_SEM_NREF;else if(f==='dif')CMP_SO_DIF=!CMP_SO_DIF;else if(f==='err')CMP_SO_ERR=!CMP_SO_ERR;else if(f==='csv'){window._cmpCsv&&window._cmpCsv();return}CMP_BAR='';render()});
setInterval(()=>{if(VIS==='comparar'&&!anaId&&Date.now()-CMP_ULT>2500)cmpBuscar()},3000);
function render(){if(anaId)return;prevAlvos=new Set();document.querySelectorAll('[data-vis]').forEach(b=>b.classList.toggle('on',b.dataset.vis===VIS));
 if(VIS==='comparar'&&dados.length){cmpIniciar();const g=$('#grid');g.className='';
  // a barra de escolha só é redesenhada quando muda (senão a lista aberta fecharia a cada atualização)
  const bar=cmpBarra();if(bar!==CMP_BAR||!document.getElementById('cmpcorpo')){CMP_BAR=bar;g.innerHTML=bar+'<div id="cmpcorpo"></div>';
   CMP.forEach(x=>{if(!CMP_SESS[x.id])cmpSessoes(x.id).then(()=>{CMP_BAR='';render()})})}
  $('#cmpcorpo').innerHTML=comparar();if(Date.now()-CMP_ULT>2500){CMP_ULT=Date.now();setTimeout(cmpBuscar,30)}return}
 $('#grid').className=VIS==='rotas'?'rotas':VIS==='lista'?'':'grid';
 $('#grid').innerHTML=dados.length?(VIS==='lista'?lista():dados.map(VIS==='rotas'?linha:card).join('')):
 '<div class="vazio" style="grid-column:1/-1">Nenhum canal configurado.<br><br><button class="pri so-adm" onclick="abrir()">+ Criar o primeiro canal</button></div>';
 const rod=dados.filter(c=>c.status.estado==='rodando').length,tot=dados.reduce((a,c)=>a+(c.status.estado!=='parado'?taxaTS(c.status).ut:0),0);
 $('#resumo').innerHTML=`<span><b>${rod}</b>/${dados.length} no ar</span><span><b>${mb(tot)}</b> Mb/s total</span>`}

async function atualizar(){try{dados=await api('GET','/api/canais');render()}catch(e){}}

// ---------- formulário ----------
let servConhecidos=[];
function campoServ(n,rot,t,v){const sel=(Array.isArray(v)?v:(v||v===0?[v]:[])).map(String),um=t==='serv1';
 const lista=servConhecidos,outros=sel.filter(x=>!lista.some(s=>String(s.numero)===x));
 const op=lista.map(s=>`<label class="chk"><input type="${um?'radio':'checkbox'}" name="${n}.cb" value="${s.numero}" ${sel.includes(String(s.numero))?'checked':''}> ${esc(s.nome)} <small style="color:var(--mut)">${s.numero}</small></label>`).join('');
 return`<div style="grid-column:1/-1"><div style="font-size:12px;color:var(--mut);margin-bottom:4px">${rot}</div>
 <div class="servsel">${um?`<label class="chk"><input type="radio" name="${n}.cb" value="" ${sel.length?'':'checked'}> o primeiro do TS</label>`
  :`<span style="font-size:12.5px;color:var(--mut)">${sel.length?'':'nenhum marcado = TS completo'}</span>`}${op}</div>
 ${lista.length?'':`<p class="msg" style="white-space:normal;margin:0 0 6px">Os serviços da entrada aparecem aqui depois que o canal recebe sinal.</p>`}
 <label>${lista.length?'Outros (número ou nome, separados por vírgula)':'Número ou nome do serviço (separe por vírgula)'}<input name="${n}.txt" value="${esc(outros.join(', '))}" placeholder="ex.: 257"></label></div>`}
let INTERFACES=[];
function campoNic(n,rot,obj){let v=obj.interface||'';
 if(!v&&obj.interface_local){const m=INTERFACES.find(i=>i.ips.includes(obj.interface_local));v=m?m.nome:''}
 const ops=INTERFACES.map(i=>`<option value="${esc(i.nome)}" ${v===i.nome?'selected':''}>${esc(nicOpt(i))}</option>`);
 if(v&&!INTERFACES.some(i=>i.nome===v))ops.push(`<option value="${esc(v)}" selected>${esc(v)} — não encontrada neste computador</option>`);
 const avulso=!v&&obj.interface_local?`<option value="" selected>IP ${esc(obj.interface_local)} (configurado à mão)</option>`:'';
 return`<label title="Placa de rede usada por esta conexão. Automática = o sistema escolhe pela rota.">${rot}<select name="${n}">
 ${avulso||`<option value="">Automática (rota do sistema)</option>`}${ops.join('')}</select></label>`}
const nicOpt=i=>`${i.nome} — ${i.ips.join(', ')||'sem IP'}${i.link===false||i.estado==='down'?' · SEM LINK':''}${i.velocidade_mbps?' · '+(i.velocidade_mbps>=1000?i.velocidade_mbps/1000+'G':i.velocidade_mbps+'M'):''}${i.tipo&&i.tipo!=='fisica'?' · '+(TIPO_NIC[i.tipo]||i.tipo):''}`;
function campoNic2(n,rot,obj){const v=obj.interface_reserva||'';
 const ops=INTERFACES.map(i=>`<option value="${esc(i.nome)}" ${v===i.nome?'selected':''}>${esc(nicOpt(i))}</option>`);
 if(v&&!INTERFACES.some(i=>i.nome===v))ops.push(`<option value="${esc(v)}" selected>${esc(v)} — não encontrada neste computador</option>`);
 return`<label title="Se a placa principal perder o link/IP, ou a conexão não funcionar por ela, o sistema passa sozinho para esta placa e volta quando a principal ficar estável. Precisa escolher a placa principal.">${rot}<select name="${n}">
 <option value="">Nenhuma</option>${ops.join('')}</select></label>`}
function campo(pref,[k,rot,t,pad],obj){
 if(t==='nic')return campoNic(`${pref}${k}`,rot,obj);if(t==='nic2')return campoNic2(`${pref}${k}`,rot,obj);const v=obj[k]??(k==='servicos'&&obj.programa?[obj.programa]:pad),n=`${pref}${k}`;
 if(t==='serv'||t==='serv1')return campoServ(n,rot,t,v);
 if(t==='chk')return`<label class="chk"><input type="checkbox" name="${n}" ${v?'checked':''}> ${rot}</label>`;
 if(t.startsWith('sel:'))return`<label>${rot}<select name="${n}">${t.slice(4).split(',').map(o=>`<option ${String(v)===o?'selected':''}>${o}</option>`).join('')}</select></label>`;
 return`<label>${rot}<input type="${t}" name="${n}" value="${esc(v)}" ${t==='password'?'autocomplete="new-password"':''}></label>`}

function desenhar(){const c=editando,e=c.entrada,p=c.processamento||{};
 $('#dcorpo').innerHTML=`
 <fieldset><legend>Canal</legend><div class="campos"><label style="grid-column:span 2">Nome<input name="nome" value="${esc(c.nome)}" required></label>
 <label class="chk"><input type="checkbox" name="ativo" ${c.ativo!==false?'checked':''}> Ativo (inicia automaticamente)</label></div></fieldset>
 <fieldset><legend>Entrada</legend><div class="campos"><label>Tipo<select name="e_tipo" id="etipo">
 ${Object.entries(NOMES_E).map(([k,n])=>{const off=(k==='dektec'&&INFO.dektec===false)||(k==='linuxasi'&&INFO.plataforma&&!/x86|amd64|i.86/.test(INFO.plataforma));
  return`<option value="${k}" ${e.tipo===k?'selected':''} ${off&&e.tipo!==k?'disabled':''}>${n}${off?' — indisponível neste hardware':''}</option>`}).join('')}</select></label>
 ${CE[e.tipo].map(f=>campo('e.',f,e)).join('')}</div></fieldset>
 <fieldset><legend>Processamento</legend><div class="campos">
 <label class="chk"><input type="checkbox" name="p.analise_tr101290" ${p.analise_tr101290!==false?'checked':''}> Análise TR 101 290</label>
 <label class="chk" title="Só vale para as saídas"><input type="checkbox" name="p.remover_nulos" ${p.remover_nulos!==false?'checked':''}> Remover nulos nas saídas</label>
 <label>PIDs a remover (ex.: 0x12, 0x14)<input name="p.remover_pids" value="${esc((p.remover_pids||[]).join(', '))}"></label>
 <label>Alarme bitrate mín. (Mb/s)<input type="number" step="0.1" name="p.bitrate_min" value="${(p.bitrate_min||0)/1e6}"></label>
 <label>Alarme bitrate máx. (Mb/s)<input type="number" step="0.1" name="p.bitrate_max" value="${(p.bitrate_max||0)/1e6}"></label></div></fieldset>
 <fieldset><legend>Saídas (opcional)</legend>
 ${c.saidas.length?'':'<p class="msg" style="white-space:normal;margin:0 0 10px">Sem saídas: o canal só recebe e analisa o sinal. Adicione uma saída apenas se quiser retransmitir.</p>'}
 ${c.saidas.map((s,i)=>`<div class="saida"><div class="sh"><span>${i===0?'Principal':'Cópia '+i} — ${s.tipo.toUpperCase()}</span>
  <button type="button" class="mini perigo" data-rm="${i}">Remover</button></div>
  <input type="hidden" name="s.${i}.tipo" value="${s.tipo}"><div class="campos">${CS[s.tipo].map(f=>campo(`s.${i}.`,f,s)).join('')}</div></div>`).join('')}
 <button type="button" data-add="srt">+ Saída SRT</button> <button type="button" data-add="udp">+ Saída UDP</button>
 <button type="button" data-add="rtp">+ Saída RTP</button> <button type="button" data-add="rtmp">+ Saída RTMP</button>
 ${c.saidas.some(s=>s.tipo==='rtmp')?'<p class="msg" style="white-space:normal;margin:8px 0 0">RTMP aceita H.264 + AAC. Padrão: copia o vídeo e converte o áudio para AAC (ISDB-Tb usa AAC LATM). Escolha "h264" no vídeo se a origem for MPEG-2 ou HEVC (usa mais CPU).</p>':''}</fieldset>`;
 $('#etipo').onchange=()=>{coletar();editando.entrada.tipo=$('#etipo').value;desenhar()};
 $('#dcorpo').querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{coletar();editando.saidas.splice(+b.dataset.rm,1);desenhar()});
 $('#dcorpo').querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{coletar();editando.saidas.push({tipo:b.dataset.add});desenhar()})}

function val(el,t){if(!el)return undefined;if(el.type==='checkbox')return el.checked;if(el.type==='number'||t==='number')return el.value===''?0:Number(el.value);return el.value.trim()}
function coletar(){const f=$('#frm'),c=editando,g=n=>f.elements[n];
 c.nome=g('nome').value.trim();c.ativo=g('ativo').checked;
 const tipo=g('e_tipo').value,ent={...c.entrada,tipo};
 CE[tipo].forEach(([k,,t])=>{const v=val(g('e.'+k),t);if(v!==undefined)ent[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});
 if(CE[tipo].some(f=>f[2]==='nic')&&(ent.interface||!g('e.interface')||g('e.interface').selectedOptions[0]?.textContent.indexOf('configurado à mão')<0))delete ent.interface_local;c.entrada=ent;
 c.processamento={...(c.processamento||{}),remover_nulos:g('p.remover_nulos').checked,analise_tr101290:g('p.analise_tr101290').checked,
  remover_pids:g('p.remover_pids').value.split(/[,\s]+/).filter(Boolean),
  bitrate_min:Math.round(Number(g('p.bitrate_min').value||0)*1e6),bitrate_max:Math.round(Number(g('p.bitrate_max').value||0)*1e6)};
 c.saidas=c.saidas.map((s,i)=>{const o={tipo:s.tipo};CS[s.tipo].forEach(([k,,t])=>{
  if(t==='serv'||t==='serv1'){const n=`s.${i}.${k}`,cb=[...f.querySelectorAll(`[name="${n}.cb"]:checked`)].map(x=>x.value).filter(Boolean),
   tx=((f.elements[n+'.txt']||{}).value||'').split(',').map(x=>x.trim()).filter(Boolean);o[k]=[...new Set([...cb,...tx])];if(t==='serv1')o[k]=o[k].slice(0,1);return}
  if(t==='nic'){const el=g(`s.${i}.${k}`);o.interface=el?el.value:'';if(!o.interface&&s.interface_local&&el&&el.selectedOptions[0]?.textContent.indexOf('configurado à mão')>=0)o.interface_local=s.interface_local;return}
  const v=val(g(`s.${i}.${k}`),t);
  if(v!==undefined)o[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});return o})}

async function carregarInterfaces(){try{INTERFACES=await api('GET','/api/interfaces')}catch(e){INTERFACES=[]}}
async function abrir(id){await carregarInterfaces();const c=id?dados.find(x=>x.id===id):null;
 servConhecidos=(c&&c.status&&(c.status.servicos||[]).length?c.status.servicos:(c&&c.ultimo_relatorio&&c.ultimo_relatorio.servicos_lista)||[]);
 editando=c?JSON.parse(JSON.stringify(c)):{nome:'Novo canal',ativo:true,entrada:{tipo:'srt',modo:'caller'},
  processamento:{remover_nulos:true,analise_tr101290:true,intervalo_relatorio_s:5},saidas:[]};
 delete editando.status;$('#dtit').textContent=c?'Editar canal':'Novo canal';desenhar();$('#dlg').showModal()}

$('#frm').onsubmit=async ev=>{ev.preventDefault();coletar();
 try{if(editando.id)await api('PUT','/api/canais/'+editando.id,editando);else await api('POST','/api/canais',editando);
  $('#dlg').close();toast('Canal salvo e aplicado',1);atualizar()}catch(e){toast(e.message)}};

// ---------- análise ----------
const fmtB=b=>{b=+b||0;return b>=1e9?(b/1e9).toFixed(2)+' GB':b>=1e6?(b/1e6).toFixed(1)+' MB':(b/1e3).toFixed(0)+' kB'};
const fmtR=b=>b>=1e6?(b/1e6).toFixed(2)+' Mb/s':(b/1e3).toFixed(b<1e4?1:0)+' kb/s';
const fmtHa=s=>s<60?Math.round(s)+'s':s<3600?Math.round(s/60)+'min':(s/3600).toFixed(1)+'h';
let sessSel=null;
async function carregarSess(){if(!anaId)return;try{const l=await api('GET',`/api/canais/${anaId}/relatorios`),
 c=dados.find(x=>x.id===anaId),vivo=c&&c.status.estado!=='parado';
 const ops=(vivo?['<option value="">Ao vivo</option>']:[]).concat(l.map(r=>`<option value="${r.sessao}">${fmtDH(r.inicio)} → ${fmtH(r.fim)} (${dur(r.duracao_s)})${r.motivo==='em andamento'?' · em andamento':''}</option>`));
 const s=$('#asess');s.innerHTML=ops.join('')||'<option>sem sessões salvas</option>';s.value=sessSel==null?(vivo?'':'0'):String(sessSel)}catch(e){}}
$('#asess').onchange=e=>{const v=e.target.value;sessSel=v===''?null:+v;carregarAna()};
function rota(){const m=location.hash.match(/^#\/analise\/(\w+)/);anaId=m?m[1]:null;ana=null;sessSel=null;if(anaId)carregarSess();
 $('#vana').hidden=!anaId;$('#vlista').hidden=!!anaId;window.scrollTo(0,0);
 if(anaId){$('#akpi').innerHTML=$('#atr').innerHTML=$('#aserv').innerHTML=$('#apids').innerHTML='';carregarAna()}else render()}
async function carregarAna(){if(!anaId)return;const c=dados.find(x=>x.id===anaId);$('#anome').textContent=c?c.nome:'';
 const qs=sessSel==null?'':`?sessao=${sessSel}`;['pdf','html','txt','json'].forEach(f=>{$('#abx_'+f).href=`/api/canais/${anaId}/analise?baixar=1&formato=${f}`+(sessSel==null?'':`&sessao=${sessSel}`)});
 try{ana=await api('GET',`/api/canais/${anaId}/analise${qs}`);rAna()}catch(e){$('#aest').className='pill parado';$('#aest').textContent=e.message;}}
function kpi(r,v,t){return`<div class="kpi" title="${esc(t||'')}"><small>${r}</small><b>${v}</b></div>`}
function rAna(){const a=ana,r=a.resumo,t=a.tr101290,S=a.salvo,p=a.periodo||{},ts=a.taxa_sessao;
 $('#aest').className='pill '+(S?'parado':a.estado);$('#aest').textContent=S?'relatório salvo':(r.sinal?a.estado:'sem sinal').replace('_',' ');
 $('#azerar').hidden=!!S;$('#aban').hidden=!S;
 if(S)$('#aban').innerHTML=`Análise ${esc(MOTIVO_L[p.motivo]||p.motivo)} — resumo salvo de <b>${fmtDH(p.inicio)}</b> até <b>${fmtDH(p.fim)}</b> (${dur(p.duracao_s)}).`
  +(p.motivo==='em andamento'?' Sessão ainda em andamento: este é o último salvamento automático.':'');
 $('#akpi').innerHTML=(S?kpi('Duração',dur(p.duracao_s))+kpi('Bitrate médio',fmtR(ts.media))+kpi('Bitrate mínimo',fmtR(ts.min),'menor média de 5 s da sessão')+kpi('Bitrate máximo',fmtR(ts.max),'maior média de 5 s da sessão (rajadas isoladas não contam)')
  :kpi('Bitrate TS',fmtR(r.taxa_total))+kpi('Bitrate útil',fmtR(r.taxa_util),'Sem pacotes nulos')+kpi('Sessão',dur(p.duracao_s)))
  +kpi('Taxa do TS',r.modo_taxa&&r.modo_taxa.modo?`${r.modo_taxa.modo} <small style="font-size:12px;color:var(--mut)">±${r.modo_taxa.variacao_pct}%</small>`:'medindo…',
     r.modo_taxa&&r.modo_taxa.modo?`variação da taxa total nos últimos ${r.modo_taxa.amostras} s (mín ${fmtR(r.modo_taxa.min)}, máx ${fmtR(r.modo_taxa.max)}). CBR abaixo de 2%.`:'')+kpi('TS ID',r.tsid??'—')+kpi('Rede',esc(r.rede||'—'),'Nome da rede na tabela NIT: '+(r.rede||'este sinal não envia NIT (comum em sinal por IP)'))
  +(ts&&ts.rajadas?kpi('Rajadas',ts.rajadas,'segundos em que os pacotes chegaram acumulados (a rede ou o SRT segurou e soltou de uma vez). Costumam vir junto com perda/retransmissão.'):'')
  +(ts&&ts.perda_interna?kpi('Perda interna',`<span style="color:var(--warn)">${ts.perda_interna.toLocaleString('pt-BR')}</span>`,'datagramas descartados pelo próprio servidor antes da análise (CPU sobrecarregada ou buffer pequeno). Nesses momentos os erros de CC podem não estar no sinal.'):'')
  +kpi('Serviços',r.n_servicos)+kpi('PIDs',r.n_pids)+kpi('Hora do TS',esc((r.hora_ts||'—').split(' ').pop()),'Data/hora da TDT/TOT: '+(r.hora_ts||'não recebida'))
  +kpi('Pacotes',r.pacotes.toLocaleString('pt-BR'));
 $('#adesde').textContent='desde '+new Date(r.desde*1000).toLocaleTimeString('pt-BR');
 $('#atr').innerHTML=[1,2,3].map(p=>`<div class="trg"><h4>Prioridade ${p}</h4>${TRI.filter(x=>x[1]===p).map(([k,,cod,nome,desc])=>{
  const v=t.contadores[k]||0,rec=t.recentes.includes(k),na=t.nao_aplicavel.includes(k),ha=S?null:(t.ha_s||{})[k],em=S?(t.ultimo_em||{})[k]:null;
  const c=rec?'e':(na&&!v)?'x':v?'w':'';
  return`<div class="trl ${c}" title="${esc(desc)}${na?' — não aplicável a este TS':''}"><i></i><span class="cod">${cod}</span><span>${nome}</span>
  <span class="ha">${na&&!v?'n/a':ha!=null?'há '+fmtHa(ha):em!=null?'às '+fmtH(em):''}</span><b>${v}</b></div>`}).join('')}</div>`).join('');
 // gráfico
 const h=a.historico,W=600,H=150;if(h.length>1){const mx=Math.max(...h)*1.15||1,
  pts=h.map((v,i)=>`${(i/(h.length-1)*W).toFixed(1)},${(H-v/mx*(H-14)).toFixed(1)}`).join(' ');
  const fs=[.25,.5,.75,1],gl=fs.map(f=>{const y=(H-f*(H-14)).toFixed(1);return`<line class="gl" x1="0" x2="${W}" y1="${y}" y2="${y}"/>`}).join('');
  const rot=fs.map(f=>`<span style="top:${((1-f*(H-14)/H)*100).toFixed(1)}%">${(mx*f/1e6).toFixed(1)} Mb/s</span>`).join('');
  $('#agraf').innerHTML=`<div class="gw"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${gl}<polygon points="0,${H} ${pts} ${W},${H}"/><polyline points="${pts}"/></svg>${rot}</div>`}
 else $('#agraf').innerHTML='<div class="msg">aguardando dados…</div>';
 // saídas e conexões
 const ch=dados.find(x=>x.id===anaId),cfgS=a.saidas_cfg||((ch&&ch.saidas)||[]),pss=a.saidas||((ch&&ch.status&&ch.status.saidas)||[]);
 const itens=[];if((a.srt||{}).SRTIN)itens.push({rot:'Entrada',x:(ch&&ch.entrada)||{tipo:'srt',modo:a.srt.SRTIN.modo,endereco:a.srt.SRTIN.endereco},e:a.srt.SRTIN,ps:null});
 cfgS.forEach((x,i)=>itens.push({rot:'Saída '+(i+1),x,e:(a.srt||{})['SRTOUT'+i]||null,ps:pss[i]||null}));
 $('#psrt').hidden=!itens.length;$('#asrtn').textContent=itens.length?(()=>{const n=itens.filter(it=>it.ps?it.ps.estado==='rodando':it.e&&it.e.estado==='conectado').length;return a.salvo?itens.length+' item(ns)':`${n} de ${itens.length} no ar`})():'';
 $('#asrt').innerHTML=itens.map(({rot,x,e,ps})=>{const K=(r,v,cl)=>`<div><small>${r}</small><b class="${cl||''}">${v}</b></div>`,W=600,H=90;
  const srtOk=e&&e.estado==='conectado'&&!a.salvo,psOk=ps&&ps.estado==='rodando'&&!a.salvo,noAr=srtOk||psOk||(!ps&&srtOk);
  const tp=TIPO_TXT[x.tipo]||x.tipo,cl=x.tipo==='udp'?'udp':(x.tipo==='dektec'||x.tipo==='linuxasi')?'asi':'';
  const estTxt=a.salvo?'encerrada':noAr?(e?'conectado':'no ar'):ps?({conectando:x.tipo==='srt'&&x.modo==='listener'?'aguardando conexão':'conectando…',reiniciando:'tentando de novo',erro:'erro',parado:'parado',iniciando:'iniciando'}[ps.estado]||ps.estado):(e?e.estado:'');
  const peer=e&&e.peer&&!a.salvo?` · <code>${esc(e.peer)}</code>`:'';
  const uptime=psOk&&ps.uptime_s!=null?ps.uptime_s:(srtOk?e.conectado_s:0);
  const conx=ps?ps.conexoes:(e?e.conexoes:0);
  // gráfico: SRT (taxa+RTT+perda) ou taxa da saída
  let g='';const h=e&&e.hist&&e.hist.length>1?e.hist:null,hp=!h&&ps&&ps.hist&&ps.hist.length>1?ps.hist:null;
  if(h){const mx=Math.max(...h.map(v=>v[0]))*1.2||1,mr=Math.max(...h.map(v=>v[2]))*1.2||1,X=i=>(i/(h.length-1)*W).toFixed(1);
   g=`<div class="leg"><span style="color:var(--acc)">— taxa (máx ${(mx/1.2).toFixed(2)} Mb/s)</span><span style="color:#d2a8ff">— RTT (máx ${(mr/1.2).toFixed(0)} ms)</span><span style="color:var(--err)">▮ perda</span></div>
   <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${h.map((v,i)=>v[1]>0?`<rect x="${X(i)}" y="${H-Math.min(H,v[1]*20+4)}" width="3" height="${Math.min(H,v[1]*20+4)}" fill="var(--err)"/>`:'').join('')}
   <polyline points="${h.map((v,i)=>`${X(i)},${(H-v[0]/mx*H).toFixed(1)}`).join(' ')}" style="fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke"/>
   <polyline points="${h.map((v,i)=>`${X(i)},${(H-v[2]/mr*H).toFixed(1)}`).join(' ')}" style="fill:none;stroke:#d2a8ff;stroke-width:1.2;vector-effect:non-scaling-stroke;opacity:.8"/></svg>`}
  else if(hp){const mx=Math.max(...hp)*1.2||1,X=i=>(i/(hp.length-1)*W).toFixed(1);
   g=`<div class="leg"><span style="color:var(--acc)">— taxa de saída (máx ${(mx/1.2/1000).toFixed(2)} Mb/s)</span></div>
   <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${hp.map((v,i)=>`${X(i)},${(H-v/mx*H).toFixed(1)}`).join(' ')}" style="fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke"/></svg>`}
  const tt=(e&&e.totais)||{},info=(ps&&ps.info)||{};
  let k='';
  if(noAr){const taxa=srtOk?(e.taxa_mbps||0):((info.taxa_kbps||0)/1000);
   k+=K('Taxa',taxa.toFixed(2)+' Mb/s')+K('No ar há',dur(uptime));
   if(ps)k+=K('Enviado nesta conexão',fmtB(ps.enviado_conexao_bytes));
   if(srtOk)k+=K('RTT',e.rtt_ms+' ms')+K('Perda agora',(e.perda_pct||0).toFixed(2)+'%',e.perda_pct>1?'e':e.perda_pct>0?'w':'')
    +K('Retrans/s',e.retransmitidos,e.retransmitidos?'w':'')+K('Buffer',e.buffer_ms+' ms')+K('Latência',e.latencia_ms+' ms')+(e.banda_mbps&&e.papel!=='entrada'?K('Banda do link',e.banda_mbps+' Mb/s'):'')
    +(e.papel==='entrada'?K('Não recuperados/s',e.descartados,e.descartados?'e':''):'');
   if(info.velocidade)k+=K('Velocidade',esc(info.velocidade),/^0\.|^0x/.test(info.velocidade)?'w':'')}
  if(ps)k+=K('Enviado na sessão',fmtB(ps.enviado_bytes))+K('Tempo no ar',dur(ps.tempo_no_ar_s))+K('Conexões',conx)+K('Quedas',ps.reinicios,ps.reinicios?'w':'');
  if(e)k+=K('RTT médio / máx',e.rtt_medio_ms+' / '+e.rtt_max_ms+' ms')+K('Pacotes',(tt.pacotes||0).toLocaleString('pt-BR'))
   +K('Perdidos',(tt.perdidos||0).toLocaleString('pt-BR'),tt.perdidos?'w':'')+K('Retransmitidos',(tt.retransmitidos||0).toLocaleString('pt-BR'))
   +K('Descartados',(tt.descartados||0).toLocaleString('pt-BR'),tt.descartados?'e':'');
  const stc=a.salvo?'parado':noAr?'conectado':(ps&&ps.estado==='parado')?'parado':'aguardando';
  return`<div class="srtl"><div class="sh"><b>${esc(rot)}</b><span class="tp ${cl}">${tp}</span><code>${esc(endTxt(x))}</code>
  <span class="st ${stc}"><i></i>${estTxt}${peer}</span>
  <span style="margin-left:auto;color:var(--mut);font-size:12px">${conx} conexão(ões)${noAr&&uptime?' · há '+dur(uptime):''}</span></div>
  ${!noAr&&!a.salvo&&ps&&ps.ultima_msg?`<div style="padding:7px 11px;color:var(--warn);font-size:12.5px;border-bottom:1px solid var(--line)">⚠ ${esc(msgCurta(ps.ultima_msg))}${ps.reinicios?` — ${ps.reinicios} tentativa(s), tentando de novo sozinho`:''}</div>`:''}
  <div class="sk">${k}</div>${g?`<div class="g">${g}</div>`:''}</div>`}).join('');
 // serviços
 $('#anserv').textContent=a.servicos.length+' serviço(s)';
 prevAlvos=new Set();const chA=dados.find(x=>x.id===anaId)||{},cfgA=a.saidas_cfg||chA.saidas||[];
 $('#aserv').innerHTML=a.servicos.map(s=>{const vid=s.componentes.some(c=>c.tipo==='video');
  const dest=cfgA.map((x,i)=>{const r=servRotulo(x,a.servicos.map(q=>({numero:q.numero,nome:q.nome})));return(!r||r.some(q=>String(q.numero)===String(s.numero)))?`<b>Destino ${i+1}</b> (${esc(resS(x))})`:null}).filter(Boolean);
  return`<div class="serv">${!a.salvo&&vid?`<div style="display:flex;gap:12px;padding:10px 11px 0">${prevBox(anaId,{numero:s.numero,nome:s.nome,video:true},'g')}</div>`:''}
  <div class="sh"><b>${esc(s.nome)}</b><span class="m">${esc(s.provedor||'')}</span>
  ${s.tipo?`<span class="badge">${esc(s.tipo)}</span>`:''}${s.embaralhado?'<span class="badge r">embaralhado</span>':''}${seloModo(s.modo)}<span class="tx">${fmtR(s.taxa)}</span></div>
  ${cfgA.length?`<div class="saiem">${dest.length?'Sai em: '+dest.join(' · '):'Não sai em nenhum destino'}</div>`:''}
  <div class="meta"><span>Programa <b>${s.numero}</b></span><span>PMT <b>${s.pmt}</b></span><span>PCR <b>${s.pcr_pid||'—'}</b></span>
  ${s.pcr?`<span>Intervalo PCR <b>${s.pcr.intervalo_ms} ms</b></span><span>Jitter PCR <b>${s.pcr.jitter_ns} ns</b></span>`:''}</div>
  <div class="tabw"><table class="t"><tr><th>PID</th><th>Componente</th><th>stream_type</th><th class="n">Bitrate</th></tr>
  ${s.componentes.map(c=>`<tr><td class="mono">${c.hex}</td><td>${esc(c.desc)}${c.emb?' <span class="badge r">emb</span>':''}</td><td class="mono">${c.stream_type}</td><td class="n">${fmtR(c.taxa)}</td></tr>`).join('')}</table></div></div>`}).join('')
  ||'<div class="msg">nenhum serviço (aguardando PAT/PMT)</div>';
 rPids()}
const COLS=[['pid','PID',1],['pid','Dec',1],['desc','Descrição'],['servico','Serviço'],['taxa','Bitrate',1],['pct','%',1],['pacotes','Pacotes',1],['cc','Erros CC',1],['f','Flags']];
function rPids(){if(!ana)return;const l=[...ana.pids].sort((x,y)=>{const k=ordem.k,a=x[k],b=y[k];return(typeof a==='string'?a.localeCompare(b):a-b)*ordem.d});
 $('#anpid').textContent=l.length+' PIDs';
 $('#apids').innerHTML=`<tr>${COLS.map(([k,n,num])=>`<th class="${num?'n':''}" data-ord="${k}">${n}${ordem.k===k&&n!=='Dec'?(ordem.d>0?' ▲':' ▼'):''}</th>`).join('')}</tr>`
 +l.map(p=>`<tr class="${p.tipo==='desconhecido'?'unref':p.tipo==='ignorado'?'ign':''} ${p.ausente?'aus':''}" title="${p.tipo==='ignorado'?'PID fora do pacote: aparece, mas não gera erro (lista em ⚙)':''}"><td class="mono n">${p.hex}</td><td class="n">${p.pid}</td><td>${esc(p.desc)}</td>
 <td>${esc(p.servico)}</td><td class="n">${fmtR(p.taxa)}</td><td class="n"><span class="bar" style="width:${Math.max(1,Math.min(60,p.pct*0.6))}px"></span>${p.pct.toFixed(1)}</td>
 <td class="n">${p.pacotes.toLocaleString('pt-BR')}</td><td class="n ${p.cc?'ruim':''}">${p.cc}</td>
 <td>${p.pcr?'<span class="badge">PCR</span> ':''}${p.emb?'<span class="badge r">emb</span>':''}${p.ausente?'<span class="badge r">ausente</span>':''}</td></tr>`).join('')}
$('#apids').addEventListener('click',e=>{const th=e.target.closest('th');if(!th)return;const k=th.dataset.ord;ordem={k,d:ordem.k===k?-ordem.d:(k==='taxa'||k==='pct'||k==='cc'||k==='pacotes'?-1:1)};rPids()});
$('#voltar').onclick=()=>{location.hash=''};
$('#azerar').onclick=async()=>{try{await api('POST',`/api/canais/${anaId}/zerar`);carregarAna()}catch(e){toast(e.message)}};
window.addEventListener('hashchange',rota);

// ---------- log ----------
async function carregarLog(){if(!logId)return;try{const l=await api('GET',`/api/canais/${logId}/log`),p=$('#logtxt'),
 fim=p.scrollTop+p.clientHeight>=p.scrollHeight-20;p.textContent=l.join('\n')||'(sem mensagens — canal parado ou recém-iniciado)';if(fim)p.scrollTop=p.scrollHeight}catch(e){}}

document.addEventListener('click',ev=>{const l=ev.target.closest('[data-ana]');if(l)location.hash='#/analise/'+l.dataset.ana});
document.addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-fechar')){b.closest('dialog').close();return}
 const a=b.dataset.a,id=b.dataset.id;if(!a)return;const c=dados.find(x=>x.id===id);
 try{if(a==='editar')return abrir(id);
  if(a==='analise'){location.hash='#/analise/'+id;return}
  if(a==='log'){logId=id;$('#ltit').textContent='Log — '+c.nome;$('#logtxt').textContent='';$('#dlglog').showModal();return carregarLog()}
  if(a==='excluir'){if(!confirm(`Excluir o canal "${c.nome}"?`))return;await api('DELETE','/api/canais/'+id)}
  else{b.disabled=true;await api('POST',`/api/canais/${id}/${a}`)}
  atualizar()}catch(e){toast(e.message);b.disabled=false}});
$('#dlglog').addEventListener('close',()=>logId=null);
$('#novo').onclick=()=>abrir();

function marca(ap){if(!ap)return;$('#mtit').textContent=ap.titulo;document.title=ap.titulo;const im=$('#mlogo');
 const fv=document.getElementById('fav');if(fv)fv.href='/favicon.ico?v='+(ap.logo?ap.logo_v:0);
 if(ap.logo){im.src='/logo?v='+ap.logo_v;im.hidden=false}else{im.hidden=true;im.removeAttribute('src')}}
let apNovo=null,apRem=false;
$('#bap').onclick=()=>{const ap=INFO.aparencia||{};$('#frmap').elements.titulo.value=ap.titulo||'';$('#frmap').elements.pids_ign.value=(ap.pids_ignorados||[]).join(', ');$('#frmap').elements.pcr_ms.value=String(ap.pcr_repeticao_ms||100);apNovo=null;apRem=false;
 $('#apprev').innerHTML=ap.logo?`<img src="/logo?v=${ap.logo_v}"><span>logo atual</span>`:'<span>sem logo</span>';$('#dlgap').showModal()};
$('#aplogo').onchange=e=>{const f=e.target.files[0];if(!f)return;if(f.size>2*1024*1024){toast('O logo deve ter no máximo 2 MB');return}
 const r=new FileReader();r.onload=()=>{apNovo={tipo:f.type,b64:String(r.result).split(',')[1]};apRem=false;
  $('#apprev').innerHTML=`<img src="${r.result}"><span>${esc(f.name)} — será salvo ao clicar em Salvar</span>`};r.readAsDataURL(f)};
$('#aprem').onclick=()=>{apNovo=null;apRem=true;$('#apprev').innerHTML='<span>o logo será removido ao salvar</span>'};
$('#frmap').onsubmit=async ev=>{ev.preventDefault();const b={titulo:$('#frmap').elements.titulo.value.trim(),pids_ignorados:$('#frmap').elements.pids_ign.value,pcr_repeticao_ms:+$('#frmap').elements.pcr_ms.value};
 if(apNovo){b.logo_base64=apNovo.b64;b.logo_tipo=apNovo.tipo}if(apRem)b.remover_logo=true;
 try{INFO.aparencia=await api('PUT','/api/aparencia',b);marca(INFO.aparencia);$('#dlgap').close();toast('Configurações salvas',1);
  api('GET','/api/info').then(i=>{INFO=i;TRI=i.tr101290||[];render()})}catch(e){toast(e.message)}};
let REDE_T=null;
const fbps=b=>b==null?'—':b>=1e9?(b/1e9).toFixed(2)+' Gb/s':b>=1e6?(b/1e6).toFixed(2)+' Mb/s':(b/1e3).toFixed(0)+' kb/s';
const TIPO_NIC={fisica:'física',wifi:'Wi-Fi',bond:'bond',vlan:'VLAN',bridge:'bridge',vpn:'VPN',virtual:'virtual',interna:'interna'};
async function carregarRede(){try{const r=await api('GET','/api/rede'+($('#redetodas').checked?'?todas=1':''));
 const fis=r.interfaces.filter(i=>!i.virtual),lig=r.interfaces.filter(i=>i.link&&i.ips.length);
 $('#redecorpo').innerHTML=`<p class="msg" style="white-space:normal;margin:0 0 10px">${r.interfaces.length} placa(s) reconhecida(s) · ${fis.length} física(s) · ${lig.length} com link e IP. A lista é lida do sistema na hora: placa nova ou cabo reconectado aparecem sozinhos (atualiza a cada 2 s).</p>
 ${(r.sem_driver||[]).map(p=>`<div class="aviso">Placa detectada no barramento PCI <code>${esc(p.pci)}</code>${p.modelo?` — ${esc(p.modelo)}`:''}, mas <b>sem driver</b>: o sistema não consegue usá-la. Instale o driver/firmware do fabricante e reinicie.</div>`).join('')}
 <div class="nics">${r.interfaces.map(i=>{const uso=r.uso[i.nome]||[];
  return`<div class="nic ${i.link?'':'off'}"><div class="nh"><span class="lk ${i.link?'on':i.estado==='down'?'dn':''}" title="${i.link?'link OK':'sem link'}"></span><b>${esc(i.nome)}</b>
   <span class="tpn ${esc(i.tipo)}">${esc(TIPO_NIC[i.tipo]||i.tipo)}</span><span class="sp" style="flex:1"></span>
   <span style="font-size:12px;color:${i.link?'#a6e3a1':'var(--mut)'}">${i.link?(i.velocidade_mbps?(i.velocidade_mbps>=1000?i.velocidade_mbps/1000+' Gb/s':i.velocidade_mbps+' Mb/s'):'link OK')+(i.duplex&&i.duplex!=='unknown'?' · '+esc(i.duplex):''):'sem link'}</span></div>
   <div class="md">${esc(i.modelo||'')}${i.driver?` <span style="opacity:.8">· driver ${esc(i.driver)}</span>`:''}</div>
   <dl><dt>IPv4</dt><dd>${esc(i.ips.join(', ')||'sem IP')}</dd>${i.ipv6.length?`<dt>IPv6</dt><dd>${esc(i.ipv6.join(', '))}</dd>`:''}
   <dt>Gateway</dt><dd>${esc(i.gateway||'—')}</dd><dt>MAC</dt><dd>${esc(i.mac||'—')}</dd>${i.mtu?`<dt>MTU</dt><dd>${i.mtu}</dd>`:''}
   ${i.pci?`<dt>PCI</dt><dd>${esc(i.pci)}</dd>`:''}${i.membros?`<dt>Membros</dt><dd>${esc(i.membros.join(', '))}</dd>`:''}${i.mestre?`<dt>Faz parte de</dt><dd>${esc(i.mestre)}</dd>`:''}</dl>
   <div class="ntr"><span>↓ recebendo <b>${fbps(i.rx_bps)}</b></span><span>↑ enviando <b>${fbps(i.tx_bps)}</b></span></div>
   ${uso.length?`<div class="uso">${uso.map(u=>`<span class="${u.papel==='reserva'?'r':''}" title="${u.papel==='reserva'?'placa reserva':'placa principal'}">${esc(u.canal)} · ${esc(u.onde)}${u.papel==='reserva'?' (reserva)':''}</span>`).join('')}</div>`:''}</div>`}).join('')}</div>`}catch(e){$('#redecorpo').innerHTML=`<div class="aviso">${esc(e.message)}</div>`}}
$('#brede').onclick=async()=>{$('#redecorpo').innerHTML='<p class="msg">lendo as placas…</p>';$('#dlgrede').showModal();await carregarRede();clearInterval(REDE_T);REDE_T=setInterval(carregarRede,2000)};
$('#dlgrede').addEventListener('close',()=>{clearInterval(REDE_T);REDE_T=null});$('#redetodas').onchange=carregarRede;
let EU=null;
const ini=n=>String(n||'?').trim().split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
function desenharUsr(){if(!EU)return;document.body.classList.toggle('op',EU.papel!=='admin');
 $('#usr').innerHTML=`<button id="busr" title="${esc(EU.usuario)}"><span class="av">${esc(ini(EU.nome))}</span><span class="un">${esc(EU.nome)}<small>${esc(EU.papel_nome)}</small></span> ▾</button>
 <div class="mn" id="mnusr" hidden><div class="q"><b>${esc(EU.nome)}</b>${esc(EU.usuario)} · ${esc(EU.papel_nome)}</div>
 <button data-u="senha">Trocar minha senha</button>${EU.papel==='admin'?'<button data-u="usuarios">Usuários e permissões</button>':''}<button data-u="sair">Sair</button></div>`;
 const av=$('#avisos');const id='avsenha';if(EU.senha_padrao&&!document.getElementById(id))av.insertAdjacentHTML('afterbegin',
  `<div class="aviso" id="${id}">Você está usando a senha padrão <b>admin</b>. <button class="mini" data-u="senha">Trocar agora</button></div>`);
 else if(!EU.senha_padrao&&document.getElementById(id))document.getElementById(id).remove()}
async function carregarEu(){try{EU=await api('GET','/api/eu');desenharUsr()}catch(e){}}
document.addEventListener('click',async e=>{const m=$('#mnusr');const b=e.target.closest('#busr');
 if(b){m.hidden=!m.hidden;return}if(m&&!e.target.closest('#mnusr'))m.hidden=true;
 const u=e.target.closest('[data-u]');if(!u)return;if(m)m.hidden=true;const a=u.dataset.u;
 if(a==='sair'){try{await api('POST','/api/logout')}catch(x){}location.href='/login';return}
 if(a==='senha'){$('#frmsenha').reset();$('#dlgsenha').showModal();return}
 if(a==='usuarios'){usEdit(null);await listarUs();$('#dlgus').showModal();return}
 if(a==='ued'){usEdit(US.find(x=>x.usuario===u.dataset.n));return}
 if(a==='uex'){if(!confirm(`Excluir o usuário "${u.dataset.n}"?`))return;
  try{await api('DELETE','/api/usuarios/'+encodeURIComponent(u.dataset.n));toast('Usuário excluído',1);listarUs()}catch(x){toast(x.message)}}});
$('#frmsenha').onsubmit=async ev=>{ev.preventDefault();const f=ev.target.elements;
 if(f.nova.value!==f.nova2.value){toast('As senhas novas não conferem');return}
 try{await api('PUT','/api/eu/senha',{atual:f.atual.value,nova:f.nova.value});$('#dlgsenha').close();toast('Senha alterada',1);carregarEu()}catch(x){toast(x.message)}};
let US=[],usAlvo=null;
const fmtD=t=>t?new Date(t*1000).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
async function listarUs(){US=await api('GET','/api/usuarios');
 $('#tus').innerHTML='<tr><th>Usuário</th><th>Nome</th><th>Perfil</th><th>Último acesso</th><th></th></tr>'+US.map(x=>`<tr><td><code>${esc(x.usuario)}</code>${x.usuario===EU.usuario?' <small style="color:var(--mut)">(você)</small>':''}</td>
 <td>${esc(x.nome)}</td><td><span class="papel ${esc(x.papel)}">${esc(x.papel_nome)}</span></td><td>${fmtD(x.ultimo_acesso)}</td>
 <td><button class="mini" data-u="ued" data-n="${esc(x.usuario)}">Editar</button>${x.usuario===EU.usuario?'':` <button class="mini perigo" data-u="uex" data-n="${esc(x.usuario)}">Excluir</button>`}</td></tr>`).join('')}
function usEdit(x){usAlvo=x?x.usuario:null;const f=$('#frmus');f.reset();const el=f.elements;
 el.usuario.disabled=!!x;el.usuario.value=x?x.usuario:'';el.nome.value=x?x.nome:'';el.papel.value=x?x.papel:'operador';
 el.senha.required=!x;el.senha.placeholder=x?'deixe vazio para manter':'mínimo 6 caracteres';
 $('#uslg').textContent=x?`Editar ${x.usuario}`:'Novo usuário';$('#ussub').textContent=x?'Salvar alterações':'Adicionar usuário';$('#uscanc').hidden=!x}
$('#uscanc').onclick=()=>usEdit(null);
$('#frmus').onsubmit=async ev=>{ev.preventDefault();const el=ev.target.elements;
 const b={nome:el.nome.value.trim(),papel:el.papel.value};if(el.senha.value)b.senha=el.senha.value;
 try{if(usAlvo){await api('PUT','/api/usuarios/'+encodeURIComponent(usAlvo),b);toast('Usuário atualizado',1)}
  else{b.usuario=el.usuario.value.trim();await api('POST','/api/usuarios',b);toast('Usuário criado',1)}
  usEdit(null);await listarUs();carregarEu()}catch(x){toast(x.message)}};
carregarEu();
api('GET','/api/info').then(i=>{INFO=i;TRI=i.tr101290||[];marca(i.aparencia);render();if(!i.persistente)$('#avisos').innerHTML='<div class="aviso">Modo linha de comando: alterações feitas aqui não são salvas em arquivo. Inicie o serviço com um arquivo de configuração para salvar as alterações.</div>'}).catch(()=>{});
atualizar();setInterval(atualizar,2000);setInterval(carregarLog,2000);setInterval(()=>{if(!(ana&&ana.salvo&&sessSel!=null))carregarAna()},1000);setInterval(carregarSess,10000);rota();
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
        p.add_argument("--senha-painel", default="", help="senha do painel (padrão: admin)")
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
    p.add_argument("--compilar", action="store_true", help="compila do código-fonte (sem pacote pronto)")
    p.set_defaults(func=cmd_instalar_tsduck)

    p = sub.add_parser("instalar-dektec", help="instala o driver da placa DekTec (root)")
    p.add_argument("--sdk", help="caminho ou URL do LinuxSDK_v*.tar.gz da DekTec")
    p.add_argument("--se-houver-placa", action="store_true", help="só instala se detectar placa DekTec")
    p.add_argument("--forcar", action="store_true", help="reinstala mesmo com o driver carregado")
    p.set_defaults(func=cmd_instalar_dektec)

    p = sub.add_parser("rotas-por-placa", help="cada placa de rede sai pelo próprio gateway (root; para placa reserva)")
    p.set_defaults(func=lambda a: print("\n".join(aplicar_rotas_por_placa()) or "nada a fazer (ou sem permissão)"))
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
