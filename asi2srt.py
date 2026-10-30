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

__version__ = "2.9.1"

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
  senha_painel: admin         # troque se o painel ficar acessível fora da rede local
  log_arquivo: ""
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
        if s.get("interface_local"):
            a += ["--local-address", s["interface_local"]]
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
    prog = str(s.get("programa") or "").strip()
    if prog and not prog.isdigit():
        raise ValueError("programa deve ser o número do serviço (ex.: 257) ou vazio")
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


def montar_comando(canal: dict, tsp_bin: str = "tsp", porta_analise=None, porta_dist=None) -> list:
    """porta_analise: porta UDP local onde o analisador TR 101 290 recebe uma cópia do TS."""
    canal = normalizar_canal(canal)
    ent = canal["entrada"]
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
        cmd += ["-I", "ip"] + _args_ip_entrada(ent["endereco"], ent.get("interface_local", ""))
        cmd += ["--receive-timeout", "5000"]
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
        cmd += ["-P", "fork", "--nowait", "--buffered-packets", "7",
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
    if so_analise:
        cmd += ["-O", "drop"]
    else:
        cmd += ["-O", "ip", f"127.0.0.1:{porta_dist or 'PORTA_DIST'}", "--packet-burst", "7"]
    return cmd


def montar_saida(s: dict, indice: int, tsp_bin: str = "tsp", porta_entrada=None) -> list:
    """Processo de uma saída: recebe o TS do distribuidor local e envia por SRT, UDP, RTP ou RTMP."""
    if s.get("tipo") == "rtmp":
        return _args_ffmpeg_rtmp(s, porta_entrada)
    cmd = [tsp_bin, "--realtime"]
    if s.get("tipo", "srt") == "srt":
        cmd.append("--verbose")  # mostra "connected to ..." com o endereço do outro lado
    cmd += ["-I", "ip", str(porta_entrada or "PORTA_SAIDA"), "--local-address", "127.0.0.1", "--buffer-size", "4000000",
            "-O"] + _args_saida(s, prefixo=f"SRTOUT{indice}")
    return cmd


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
        self.lock = threading.Lock()
        self.zerar()
        self.reiniciar_estado()
        self._sock = None
        self._parar = threading.Event()
        self.porta = None

    # --- estado -------------------------------------------------------
    def zerar(self):
        self.est_taxa = [0, 0.0, None, None]   # n, soma, mín, máx (bitrate total por segundo)
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
            if afc & 1:
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
            return
        ult, uidx, apcr, aidx, taxa = st
        dif = pcr - ult
        if dif < 0 or dif > self.PCR_DISC:
            self._erro("pcr_disc", agora)
            self.pcr[pid] = [pcr, idx, pcr, idx, None]
            return
        if dif > self.PCR_REP:
            self._erro("pcr_rep", agora)
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
            if self.taxa_total > 0 and self.ultimo_pacote is not None and agora - self.ultimo_pacote < self.LIM_SINAL:
                e = self.est_taxa
                e[0] += 1
                e[1] += self.taxa_total
                e[2] = self.taxa_total if e[2] is None else min(e[2], self.taxa_total)
                e[3] = self.taxa_total if e[3] is None else max(e[3], self.taxa_total)
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
        refs = set(range(0x20)) | {0x1FFF} | set(self.pmt_pids) | set(self.es_pids) | self.ca_pids
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
            }

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
        if pid < 0x20:
            return "SI reservado", "psi"
        return "Não referenciado", "desconhecido"

    def relatorio(self, agora_mono=None, agora_wall=None):
        """Análise congelada para guardar: horários absolutos no lugar de 'há N s'."""
        agora_mono = time.monotonic() if agora_mono is None else agora_mono
        agora_wall = time.time() if agora_wall is None else agora_wall
        a = self.analise(agora_mono)
        tr = a["tr101290"]
        with self.lock:
            tr["ultimo_em"] = {k: agora_wall - (agora_mono - v) for k, v in self.ultimo_erro.items()}
            n, soma, mn, mx = self.est_taxa
        tr.pop("ha_s", None)
        tr["recentes"] = []
        a["resumo"]["sinal"] = False
        a["taxa_sessao"] = {"media": int(soma / n) if n else 0, "min": int(mn or 0), "max": int(mx or 0),
                            "segundos_com_sinal": n}
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
                if pcr_pid is not None and pcr_pid not in self.es_pids:
                    tx += int(self.taxa.get(pcr_pid, 0)) if fluindo else 0
                servicos.append({
                    "numero": num, "nome": sdt[0] or f"Programa {num}", "provedor": sdt[1],
                    "tipo": TIPOS_SERVICO.get(sdt[2], f"0x{sdt[2]:02X}" if sdt[2] is not None else ""),
                    "pmt": f"0x{ppid:04X}", "pcr_pid": f"0x{pcr_pid:04X}" if pcr_pid is not None else "",
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
                    "cbr": self.taxa.get(0x1FFF, 0) > 0, "pacotes": self.pacotes, "desde": self.desde,
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
                "historico": list(self.hist_total),
            }

    # --- recepção pela rede local -------------------------------------
    def abrir(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
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
        while not self._parar.is_set():
            try:
                dados = self._sock.recv(65536)
                agora = time.monotonic()
                with self.lock:
                    self.processar(dados, agora)
            except OSError:
                agora = time.monotonic()
            if agora - ultima >= 0.25:
                ultima = agora
                with self.lock:
                    self.verificar(agora)

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
        self.cmd = montar_saida(cfg, indice, canal.sup.get("tsp", "tsp"), self.porta)
        self.proc = None
        self.parar = threading.Event()
        self.st = {"estado": "iniciando", "ultima_msg": "", "reinicios": 0, "desde": None, "info": {}}
        self._prog = {}

    def status(self):
        s = dict(self.st)
        s["info"] = dict(self.st.get("info") or {})
        if s["desde"] and s["estado"] == "rodando":
            s["uptime_s"] = int(time.time() - s["desde"])
        return s

    def _ler(self, stream):
        est = self.canal.srt.get(self.chave)
        for linha in iter(stream.readline, ""):
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
                        self.st["estado"] = "rodando"
                        self.st["desde"] = self.st["desde"] or time.time()
                    except ValueError:
                        pass
                continue
            if self.cfg.get("tipo") == "rtmp" and re.match(r"^[a-z_0-9]+=", linha):
                k, _, v = linha.partition("=")   # progresso do ffmpeg (-progress pipe:1)
                self._prog[k] = v.strip()
                if k == "progress":
                    br = re.match(r"([\d.]+)kbits/s", self._prog.get("bitrate", ""))
                    self.st["info"] = {"taxa_kbps": float(br.group(1)) if br else 0,
                                       "velocidade": self._prog.get("speed", "").strip(),
                                       "quadros": int(self._prog.get("frame", 0) or 0),
                                       "fps": float(self._prog.get("fps", 0) or 0)}
                    if self.st["estado"] != "rodando":
                        self.st.update(estado="rodando", desde=time.time())
                continue
            mp = RE_SRT_PEER_SAIDA.search(linha)
            if mp and est:
                est.peer = mp.group(1)
                self.st.update(estado="rodando", desde=time.time())
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
            # UDP não tem "conexão": está no ar assim que o processo sobe
            udp = self.cfg.get("tipo") in ("udp", "rtp")
            self.st.update(estado="rodando" if udp else "conectando", desde=t0 if udp else None, info={})
            self._ler(self.proc.stdout)
            rc = self.proc.wait()
            if self.parar.is_set():
                break
            if time.time() - t0 > 60:
                espera = espera_min
            self.st["reinicios"] += 1
            self.st.update(estado="reiniciando", desde=None)
            self.canal._registrar(logging.ERROR, f"[saída {self.indice + 1}] {os.path.basename(self.cmd[0])} saiu (código {rc}). "
                                  f"Tentando de novo em {espera}s.")
            self.parar.wait(espera)
            espera = min(espera * 2, espera_max)
        self.st.update(estado="parado", desde=None)

    def encerrar(self):
        self.parar.set()
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


class Distribuidor:
    """Recebe o TS do processo de entrada e entrega uma cópia a cada saída (UDP local)."""

    def __init__(self):
        import socket
        self._s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
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
                except OSError:
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
        montar_comando(cfg, sup.get("tsp", "tsp"))  # valida antes de abrir recursos
        self.analisador = AnalisadorTS()
        self.inicio_sessao = time.time()
        porta = self.analisador.abrir()
        c0 = normalizar_canal(cfg)
        self.distribuidor = Distribuidor() if c0["saidas"] else None
        self.cmd = montar_comando(cfg, sup.get("tsp", "tsp"), porta_analise=porta,
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
            s["historico"] = list(self.hist)
        if s["inicio_execucao"]:
            s["uptime_s"] = int(time.time() - s["inicio_execucao"])
        intervalo = (self.cfg.get("processamento") or {}).get("intervalo_relatorio_s", 5)
        if s["estado"] == "rodando" and s["ultimo_bitrate_em"] \
                and time.time() - s["ultimo_bitrate_em"] > 3 * intervalo:
            s["estado"] = "sem_dados"
        s["tr101290"] = self.analisador.status()
        s["erros_cc"] = s["tr101290"]["contadores"]["cc"]
        s["srt"] = self.srt_status()
        s["saidas"] = [p.status() for p in self.procs_saida]
        return s

    def relatorio(self, motivo):
        agora_w = time.time()
        a = self.analisador.relatorio(time.monotonic(), agora_w)
        prio = {i[0]: i[1] for i in TR_ITENS}
        cont = a["tr101290"]["contadores"]
        totais = {f"p{p}": sum(v for k, v in cont.items() if prio[k] == p) for p in (1, 2, 3)}
        return {"inicio": self.inicio_sessao, "fim": agora_w, "duracao_s": int(agora_w - self.inicio_sessao),
                "motivo": motivo, "reinicios": self.st.get("reinicios", 0),
                "taxa": a.pop("taxa_sessao"), "totais": totais, "analise": a, "srt": self.srt_status()}

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
            self._set(ultima_msg=linha[-300:])

    def run(self):
        espera_min = self.sup.get("reinicio_min_s", 2)
        espera_max = self.sup.get("reinicio_max_s", 60)
        espera = espera_min
        if self.distribuidor:
            self.distribuidor.iniciar([p.porta for p in self.procs_saida])
            for p in self.procs_saida:
                p.start()
        while not self.parar.is_set():
            self._registrar(logging.INFO, "iniciando: " + comando_para_log(self.cmd))
            with self.analisador.lock:
                self.analisador.reiniciar_estado()
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
        for ps in self.procs_saida:
            ps.encerrar()
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
                "n_servicos": a["resumo"]["n_servicos"], "servicos": [s["nome"] for s in a["servicos"]]}

    def relatorio_salvo(self, cid, idx=0):
        lista = self.relatorios.get(cid) or []
        if not 0 <= idx < len(lista):
            return None
        rel = lista[idx]
        return {**rel["analise"], "estado": "parado", "salvo": True, "sessao": idx,
                "periodo": {k: rel[k] for k in ("inicio", "fim", "duracao_s", "motivo")},
                "taxa_sessao": rel["taxa"], "totais": rel["totais"], "srt": rel.get("srt") or {}}

    # --- persistência ------------------------------------------------
    def persistir(self):
        if not self.caminho:
            return
        with self.lock:
            cfg = {"canais": list(self.defs.values()), "supervisor": self.sup}
            salvar_config(self.caminho, cfg)

    def garantir_senha(self):
        self.sup.setdefault("usuario", "admin")
        if not self.sup.get("senha_painel"):
            self.sup["senha_painel"] = "admin"
            self.persistir()
        if self.sup["usuario"] == "admin" and self.sup["senha_painel"] == "admin":
            log.warning("painel com login padrão admin/admin - troque 'senha_painel' em %s "
                        "se o servidor for acessível fora da rede local", self.caminho or "--senha-painel")

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

        def _resp(self, codigo, corpo, tipo="application/json", baixar=None):
            if not isinstance(corpo, bytes):
                corpo = json.dumps(corpo, ensure_ascii=False, indent=2 if baixar else None).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            if baixar:
                self.send_header("Content-Disposition", f'attachment; filename="{baixar}"')
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
            url = urlparse(self.path)
            caminho = url.path.rstrip("/") or "/"
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            partes = caminho.strip("/").split("/")
            try:
                if metodo == "GET" and (caminho == "/" or caminho.startswith("/analise")):
                    return self._resp(200, PAINEL_HTML.replace("__VERSAO__", __version__).encode(),
                                      "text/html; charset=utf-8")
                if metodo == "GET" and caminho == "/status.json":
                    return self._resp(200, [{"nome": c["nome"], "id": c["id"],
                                             **{k: v for k, v in c["status"].items() if k != "historico"}}
                                            for c in ger.listar()])
                if metodo == "GET" and caminho == "/api/info":
                    return self._resp(200, {"versao": __version__, "config": ger.caminho,
                                            "persistente": bool(ger.caminho), "tr101290": TR_ITENS,
                                            "plataforma": platform.machine(), "dektec": DEKTEC_DISPONIVEL})
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
                                         "salvo": False, "periodo": {"inicio": t.inicio_sessao,
                                                                     "duracao_s": int(agora_w - t.inicio_sessao)}}
                            if q.get("baixar"):
                                nome = re.sub(r"[^\w.-]+", "_", ger.defs[cid]["nome"])
                                data = time.strftime("%Y%m%d-%H%M", time.localtime(dados["periodo"]["inicio"]))
                                return self._resp(200, dados, baixar=f"asi2srt-{nome}-{data}.json")
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
              + "".join(f"\n  {p['barramento']} {p['endereco']} (produto 0x{p['produto']})" for p in placas))
        if placas:
            print("Driver DekTec: " + ("carregado ✓" if driver_dektec_carregado()
                                       else "NÃO carregado ✗  ->  sudo asi2srt instalar-dektec"))
    if shutil.which("tsdektec"):
        r = subprocess.run(["tsdektec", "-a"], capture_output=True, text=True)
        print("Placas vistas pelo TSDuck:\n" + ((r.stdout or r.stderr).strip() or "  nenhuma encontrada"))
    asi = sorted(f for f in os.listdir("/dev") if f.startswith("asirx")) if os.path.isdir("/dev") else []
    print("\nDispositivos Linux ASI: " + (", ".join("/dev/" + a for a in asi) or "nenhum"))
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
    achados = []
    for d in pastas:
        achados += glob.glob(os.path.join(d, "LinuxSDK*.tar.gz")) + glob.glob(os.path.join(d, "LinuxSDK*.tgz"))
    return max(set(achados), key=os.path.getmtime) if achados else None


def cmd_instalar_dektec(args):
    if os.geteuid() != 0:
        raise SystemExit("rode como root: sudo asi2srt instalar-dektec")
    if not DEKTEC_DISPONIVEL:
        print(f"placas DekTec não são suportadas em {platform.machine()} (o driver só existe para PC Intel/AMD).")
        return 0 if args.se_houver_placa else 1
    placas = detectar_dektec()
    if placas:
        for p in placas:
            print(f"placa DekTec encontrada: {p['barramento']} {p['endereco']} (produto 0x{p['produto']})")
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
              "  1. baixe 'LinuxSDK' em https://www.dektec.com/downloads/SDK/  (arquivo LinuxSDK_v*.tar.gz)\n"
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
            f.write(CONFIG_INICIAL)
        os.chmod(args.config, 0o600)
        print(f"configuração criada em {args.config} (sem canais: crie pelo painel)")
    with open("/etc/systemd/system/asi2srt.service", "w") as f:
        f.write(UNIDADE_SYSTEMD.format(exe=exe, config=args.config))
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print("serviço instalado.\n"
          "  1. sudo systemctl enable --now asi2srt\n"
          "  2. login do painel: admin / admin  (troque em senha_painel no arquivo de configuração)\n"
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
.met{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:6px;align-items:end}
.met div{display:flex;flex-direction:column}.met small{color:var(--mut);font-size:11px}
.met b{font-size:15px;font-variant-numeric:tabular-nums}.met .big{font-size:22px}
.spark{width:100%;height:38px;display:block}.spark polyline{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}
.spark .area{fill:var(--acc);opacity:.12;stroke:none}
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
table.t tr.unref td{color:var(--err)}table.t tr.aus td{opacity:.45}table.t td.ruim{color:var(--err);font-weight:600}
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
.no{display:grid;grid-template-columns:86px minmax(170px,280px) 1fr auto;gap:10px;align-items:center;padding:6px 0;border-bottom:1px dashed #20262f;font-size:13px}
.no:last-child{border-bottom:0}.no .pp{color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
.no .ed{display:flex;align-items:center;gap:7px;min-width:0}.no .ed code{font-family:ui-monospace,monospace;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.no .tp{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:4px;background:#1f2a3a;color:#9ec5ff;letter-spacing:.03em}
.no .tp.udp{background:#22301f;color:#a6e3a1}.no .tp.asi{background:#3a2a1f;color:#f5c48a}
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
<header><h1>asi2srt</h1><span class="v">v__VERSAO__</span><span class="sp"></span>
<div class="resumo" id="resumo"></div><div class="vis" id="vis"><button data-vis="rotas">Rotas</button><button data-vis="cards">Cards</button></div>
<button class="pri" id="novo">+ Novo canal</button></header>
<main id="vlista"><div id="avisos"></div><div class="grid" id="grid"></div></main>
<section id="vana" hidden>
 <div class="atop"><button id="voltar">← Canais</button><h2 id="anome"></h2><span class="pill" id="aest"></span>
  <span class="sp" style="flex:1"></span><select id="asess" title="Sessões de análise salvas"></select>
  <a class="btn" id="abaixar" href="#">Baixar relatório</a><button id="azerar">Zerar contadores</button></div>
 <div class="aban" id="aban" hidden></div>
 <div class="kpis" id="akpi"></div>
 <div class="agrid"><div class="painel"><h3>TR 101 290<span id="adesde"></span></h3><div id="atr"></div></div>
  <div class="acol"><div class="painel" id="psrt" hidden><h3>Conexões SRT<span id="asrtn"></span></h3><div id="asrt"></div></div>
  <div class="painel"><h3>Bitrate do TS<span>últimos 5 min</span></h3><div class="graf" id="agraf"></div></div>
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
 srt:[['modo','Modo','sel:caller,listener','caller'],['endereco','Endereço (caller: IP:porta · listener: porta)','text',''],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],['stream_id','Stream ID','text','']],
 arquivo:[['caminho','Arquivo .ts','text',''],['loop','Repetir em loop','chk',true]]};
const CS={
 srt:[['modo','Modo','sel:caller,listener','caller'],['endereco','Destino (host:porta) / porta','text',''],
      ['latencia_ms','Latência (ms)','number',200],['senha','Senha AES (10-79)','password',''],['tamanho_chave','Chave','sel:16,24,32','16'],
      ['stream_id','Stream ID','text',''],['overhead_pct','Overhead (%)','number',25]],
 udp:[['endereco','Destino (ip:porta)','text','239.1.1.1:1234'],['ttl','TTL','number',16],['interface_local','IP da interface de saída','text','']],
 rtp:[['endereco','Destino (ip:porta)','text','239.1.1.1:5004'],['ttl','TTL','number',16],['interface_local','IP da interface de saída','text',''],
      ['payload_type','Payload type','number',33],['ssrc','SSRC (opcional)','text','']],
 rtmp:[['url','Servidor (ex.: rtmp://a.rtmp.youtube.com/live2)','text',''],['chave','Chave de transmissão','password',''],['video','Vídeo','sel:copiar,h264','copiar'],['audio','Áudio','sel:aac,copiar','aac'],
      ['video_kbps','Vídeo kb/s (se h264)','number',4000],['audio_kbps','Áudio kb/s','number',128],['programa','Programa/serviço (vazio = 1º)','text','']]};
const NOMES_E={srt:'SRT',udp:'UDP / Multicast',dektec:'ASI — placa DekTec',linuxasi:'ASI — driver Linux (DVEO)',arquivo:'Arquivo .ts'};

function resE(e){switch(e.tipo){case'dektec':return`DekTec ${e.dispositivo??0}:${e.canal??0}`;case'linuxasi':return`ASI ${e.dispositivo}`;
 case'udp':return`UDP ${e.endereco}`;case'srt':return`SRT ${e.modo} ${e.endereco}${e.senha?' 🔒':''}`;case'arquivo':return`Arquivo ${(e.caminho||'').split('/').pop()}`}return e.tipo}
const urlSemChave=u=>String(u||'').replace(/^(rtmps?:\/\/[^?#]+\/)[^/?#]+/,'$1••••');
const resS=s=>s.tipo==='udp'?`UDP ${s.endereco}`:s.tipo==='rtp'?`RTP ${s.endereco}`:s.tipo==='rtmp'?(s.chave?String(s.url||'').replace(/\/+$/,'')+'/••••':urlSemChave(s.url)).replace(/^rtmp/,'RTMP').replace(/^RTMPs/,'RTMPS'):`SRT ${s.modo} ${s.endereco}${s.senha?' 🔒':''}`;

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
function tr(s){const t=s.tr101290;if(!t)return'';
 const info=t.sinal?`${t.programas} prog · ${t.pids_referenciados} PIDs`:'<span class="semsinal">sem sinal</span>';
 return`<div class="tr"><div class="trh"><span>TR 101 290 · Prioridade 1</span><span>${info}</span></div><div class="lamps">
 ${TR.map(([k,n,d])=>{const v=t.contadores[k]||0,c=t.recentes.includes(k)?'e':v?'w':(t.sinal?'':'x');
  return`<div class="lamp ${c}" title="${esc(d)}"><i></i><small>${n}</small><b>${v}</b></div>`}).join('')}</div>
 ${t.pids_ausentes.length?`<div class="aus">PIDs ausentes: ${t.pids_ausentes.map(esc).join(', ')}</div>`:''}
 ${TRI.length?(()=>{const soma=p=>TRI.filter(x=>x[1]===p).reduce((a,x)=>a+(t.contadores[x[0]]||0),0),
  rec=p=>TRI.some(x=>x[1]===p&&t.recentes.includes(x[0])),cl=p=>rec(p)?'e':soma(p)?'w':'';
  return`<div class="trp"><span class="${cl(2)}">P2 · ${soma(2)}</span><span class="${cl(3)}">P3 · ${soma(3)}</span><a data-ana="${s._id}">Análise completa →</a></div>`})():''}</div>`}
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
function card(c){const s=c.status||{},ativo=s.estado!=='parado';s._id=c.id;
 return`<div class="card"><div class="top"><h2 title="${esc(c.nome)}">${esc(c.nome)}</h2><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span></div>
 <div class="fluxo"><div><span class="l">Entrada</span><span class="tag">${esc(resE(c.entrada))}</span></div>
 <div><span class="l">Saídas</span>${c.saidas.length?c.saidas.map(x=>`<span class="tag">${esc(resS(x))}</span>`).join(''):'<span class="tag" style="color:var(--mut)">somente análise</span>'}</div></div>
 ${!ativo&&c.ultimo_relatorio?relCard(c):`${spark(s.historico)}
 <div class="met"><div><small>Bitrate</small><b class="big">${mb(s.bitrate_bps||0)}<small> Mb/s</small></b></div>
 <div><small>Reinícios</small><b>${s.reinicios||0}</b></div><div><small>Uptime</small><b>${dur(s.uptime_s)}</b></div></div>`}
 ${tr(s)}
 <div class="msg" title="${esc(s.ultima_msg)}">${esc(s.ultima_msg||'')}</div>
 <div class="acoes">${ativo?`<button data-a="parar" data-id="${c.id}">■ Parar</button><button data-a="reiniciar" data-id="${c.id}">↻ Reiniciar</button><button data-a="zerar" data-id="${c.id}" title="Zerar contadores">Zerar</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}">▶ Iniciar</button>`}
 ${ativo?`<button data-a="analise" data-id="${c.id}">Análise</button>`:c.ultimo_relatorio?`<button data-a="analise" data-id="${c.id}">Relatório</button>`:''}<button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button>
 <button class="perigo" data-a="excluir" data-id="${c.id}">Excluir</button></div></div>`}

let VIS='rotas';try{VIS=localStorage.getItem('asi2srt.vis')||'rotas'}catch(e){}
const TIPO_TXT={srt:'SRT',udp:'UDP',rtp:'RTP',rtmp:'RTMP',dektec:'ASI',linuxasi:'ASI',arquivo:'ARQ'};
function endTxt(x){return x.tipo==='rtmp'?(x.chave?String(x.url||'').replace(/\/+$/,'')+'/••••':urlSemChave(x.url))+(x.video==='h264'?' · vídeo→H.264':'')+(x.audio==='aac'?' · áudio→AAC':''):x.tipo==='rtp'?x.endereco:x.tipo==='srt'?`${x.modo==='listener'?'listener :':'caller → '}${x.endereco}`:x.tipo==='udp'?x.endereco:x.tipo==='dektec'?`placa ${x.dispositivo??0} · porta ${x.canal??0}`:x.tipo==='linuxasi'?x.dispositivo:(x.caminho||'').split('/').pop()}
function mspark(h){if(!h||h.length<2)return'<svg class="mspark"></svg>';const v=h.map(x=>x[0]),mx=Math.max(...v)*1.2||1,mxp=Math.max(...h.map(x=>x[1]),1),W=90,H=22;
 const p=v.map((y,i)=>`${(i/(v.length-1)*W).toFixed(1)},${(H-y/mx*H).toFixed(1)}`).join(' ');
 const lp=h.some(x=>x[1]>0)?`<polyline class="lp" points="${h.map((x,i)=>`${(i/(h.length-1)*W).toFixed(1)},${(H-x[1]/mxp*H*.9).toFixed(1)}`).join(' ')}"/>`:'';
 return`<svg class="mspark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${p}"/>${lp}</svg>`}
function chipsSRT(e){if(e.estado!=='conectado')return'';const lat=e.latencia_ms||0;
 const cp=e.perda_pct>1?'e':e.perda_pct>0?'w':'',cr=lat&&e.rtt_ms>lat/4?'w':'';
 return`<span class="chip ${cr}"><small>RTT</small>${e.rtt_ms} ms</span><span class="chip ${cp}"><small>Perda</small>${(e.perda_pct||0).toFixed(2)}%</span>
 <span class="chip ${e.retransmitidos?'w':''}"><small>Retrans</small>${e.retransmitidos}/s</span><span class="chip"><small>Taxa</small>${(e.taxa_mbps||0).toFixed(2)} Mb/s</span>
 ${e.banda_mbps?`<span class="chip"><small>Banda</small>${e.banda_mbps} Mb/s</span>`:''}<span class="chip"><small>Buffer</small>${e.buffer_ms} ms</span>`}
const msgCurta=m=>String(m||'').replace(/^\*\s*(Error|Warning):\s*/i,'').replace(/^srt:\s*/,'').replace(/^\[saída \d+\]\s*/,'').slice(0,90);
function no(rot,x,e,ativo,ps){const tp=TIPO_TXT[x.tipo]||x.tipo,cl=x.tipo==='udp'?'udp':(x.tipo==='dektec'||x.tipo==='linuxasi')?'asi':'';
 const falha=ativo&&ps&&ps.estado!=='rodando'&&ps.ultima_msg&&(!e||e.estado!=='conectado')?`<span class="chip w" title="${esc(ps.ultima_msg)}">⚠ ${esc(msgCurta(ps.ultima_msg))}${ps.reinicios?` · ${ps.reinicios} tentativa(s)`:''}</span>`:'';
 let st;if(e){const tx={conectado:'conectado',aguardando:x.modo==='listener'?'aguardando conexão':'conectando…',parado:'parado'}[e.estado];
  st=`<span class="st ${e.estado}"><i></i>${tx}${e.peer?` · <code>${esc(e.peer)}</code>`:''}${e.estado==='conectado'&&e.conectado_s?` · ${dur(e.conectado_s)}`:''}</span>`}
 else if(ps&&ativo){const ok=ps.estado==='rodando';st=`<span class="st ${ok?'ativo':'aguardando'}"><i></i>${ok?'ativo':ps.estado}</span>`}
 else st=`<span class="st ${ativo?'ativo':'parado'}"><i></i>${ativo?'ativo':'parado'}</span>`;
 return`<div class="no"><span class="pp">${rot}</span><span class="ed"><span class="tp ${cl}">${tp}</span><code title="${esc(endTxt(x))}">${esc(endTxt(x))}</code></span>
 <span class="chips">${st}${e?chipsSRT(e):''}${ps&&ps.info&&ps.info.taxa_kbps&&ps.estado==='rodando'?`<span class="chip"><small>Taxa</small>${(ps.info.taxa_kbps/1000).toFixed(2)} Mb/s</span>${ps.info.velocidade?`<span class="chip"><small>Veloc.</small>${esc(ps.info.velocidade)}</span>`:''}`:''}${falha}</span>${e&&e.estado==='conectado'?mspark(e.hist):'<span></span>'}</div>`}
function linha(c){const s=c.status||{},ativo=s.estado!=='parado',srt=s.srt||{},t=s.tr101290,r=c.ultimo_relatorio;
 const dots=t?`<span class="dots" title="TR 101 290 · prioridade 1">${TR.map(([k,n,d])=>{const v=t.contadores[k]||0;return`<i class="${t.recentes.includes(k)?'e':v?'w':t.sinal?'':'x'}" title="${esc(n)}: ${v}"></i>`}).join('')}
  <small>${(()=>{const p2=TRI.filter(x=>x[1]===2).reduce((a,x)=>a+(t.contadores[x[0]]||0),0),p3=TRI.filter(x=>x[1]===3).reduce((a,x)=>a+(t.contadores[x[0]]||0),0);return`P2 ${p2} · P3 ${p3}`})()}</small></span>`
  :r?`<span class="dots">${TR.map(([k,n])=>`<i class="${(r.contadores[k]||0)?'w':''}" title="${esc(n)}: ${r.contadores[k]||0}"></i>`).join('')}<small>última sessão</small></span>`:'';
 const acs=(ativo?`<button data-a="parar" data-id="${c.id}">■ Parar</button><button data-a="reiniciar" data-id="${c.id}">↻</button><button data-a="analise" data-id="${c.id}">Análise</button>`
  :`<button class="ok" data-a="iniciar" data-id="${c.id}">▶ Iniciar</button>${r?`<button data-a="analise" data-id="${c.id}">Relatório</button>`:''}`)
  +`<button data-a="editar" data-id="${c.id}">Editar</button><button data-a="log" data-id="${c.id}">Log</button><button class="perigo" data-a="excluir" data-id="${c.id}">✕</button>`;
 const destinos=c.saidas.length?c.saidas.map((x,i)=>no('Destino '+(i+1),x,srt['SRTOUT'+i],ativo,(s.saidas||[])[i])).join(''):'<div class="no"><span class="pp">Destino</span><span class="ed" style="color:var(--mut)">somente análise</span><span></span><span></span></div>';
 return`<div class="rota e-${esc(s.estado)}"><div class="rh"><b class="rn" data-ana="${c.id}">${esc(c.nome)}</b><span class="pill ${esc(s.estado)}">${esc((s.estado||'').replace('_',' '))}</span>
 ${dots}<span class="rb">${ativo?`${mb(s.bitrate_bps||0)} <small>Mb/s</small>`:r?`<small>méd.</small> ${mb(r.taxa.media)} <small>Mb/s</small>`:''}</span><span class="acs">${acs}</span></div>
 <div class="rl">${no('Origem',c.entrada,srt.SRTIN,ativo)}${destinos}
 ${!ativo&&r?`<div class="rrel">Última análise ${esc(MOTIVO[r.motivo]||r.motivo)}: <b>${fmtDH(r.inicio)} → ${fmtH(r.fim)}</b> · ${dur(r.duracao_s)} · ${r.n_servicos} serviço(s)${r.servicos.length?': '+esc(r.servicos.join(', ')):''}</div>`:''}
 ${ativo&&s.estado!=='rodando'&&s.ultima_msg?`<div class="rrel" style="color:var(--warn)">${esc(s.ultima_msg)}</div>`:''}</div></div>`}
document.addEventListener('click',e=>{const b=e.target.closest('[data-vis]');if(!b)return;VIS=b.dataset.vis;try{localStorage.setItem('asi2srt.vis',VIS)}catch(x){}render()});
function render(){if(anaId)return;document.querySelectorAll('[data-vis]').forEach(b=>b.classList.toggle('on',b.dataset.vis===VIS));
 $('#grid').className=VIS==='rotas'?'rotas':'grid';
 $('#grid').innerHTML=dados.length?dados.map(VIS==='rotas'?linha:card).join(''):
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
 CE[tipo].forEach(([k,,t])=>{const v=val(g('e.'+k),t);if(v!==undefined)ent[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});c.entrada=ent;
 c.processamento={...(c.processamento||{}),remover_nulos:g('p.remover_nulos').checked,analise_tr101290:g('p.analise_tr101290').checked,
  remover_pids:g('p.remover_pids').value.split(/[,\s]+/).filter(Boolean),
  bitrate_min:Math.round(Number(g('p.bitrate_min').value||0)*1e6),bitrate_max:Math.round(Number(g('p.bitrate_max').value||0)*1e6)};
 c.saidas=c.saidas.map((s,i)=>{const o={tipo:s.tipo};CS[s.tipo].forEach(([k,,t])=>{const v=val(g(`s.${i}.${k}`),t);
  if(v!==undefined)o[k]=t.startsWith('sel:')&&/^\d+$/.test(v)?Number(v):v});return o})}

function abrir(id){const c=id?dados.find(x=>x.id===id):null;
 editando=c?JSON.parse(JSON.stringify(c)):{nome:'Novo canal',ativo:true,entrada:{tipo:'srt',modo:'caller'},
  processamento:{remover_nulos:true,analise_tr101290:true,intervalo_relatorio_s:5},saidas:[]};
 delete editando.status;$('#dtit').textContent=c?'Editar canal':'Novo canal';desenhar();$('#dlg').showModal()}

$('#frm').onsubmit=async ev=>{ev.preventDefault();coletar();
 try{if(editando.id)await api('PUT','/api/canais/'+editando.id,editando);else await api('POST','/api/canais',editando);
  $('#dlg').close();toast('Canal salvo e aplicado',1);atualizar()}catch(e){toast(e.message)}};

// ---------- análise ----------
const fmtR=b=>b>=1e6?(b/1e6).toFixed(2)+' Mb/s':(b/1e3).toFixed(1)+' kb/s';
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
 const qs=sessSel==null?'':`?sessao=${sessSel}`;$('#abaixar').href=`/api/canais/${anaId}/analise?baixar=1`+(sessSel==null?'':`&sessao=${sessSel}`);
 try{ana=await api('GET',`/api/canais/${anaId}/analise${qs}`);rAna()}catch(e){$('#aest').className='pill parado';$('#aest').textContent=e.message;}}
function kpi(r,v,t){return`<div class="kpi" title="${esc(t||'')}"><small>${r}</small><b>${v}</b></div>`}
function rAna(){const a=ana,r=a.resumo,t=a.tr101290,S=a.salvo,p=a.periodo||{},ts=a.taxa_sessao;
 $('#aest').className='pill '+(S?'parado':a.estado);$('#aest').textContent=S?'relatório salvo':(r.sinal?a.estado:'sem sinal').replace('_',' ');
 $('#azerar').hidden=!!S;$('#aban').hidden=!S;
 if(S)$('#aban').innerHTML=`Análise ${esc(MOTIVO_L[p.motivo]||p.motivo)} — resumo salvo de <b>${fmtDH(p.inicio)}</b> até <b>${fmtDH(p.fim)}</b> (${dur(p.duracao_s)}).`
  +(p.motivo==='em andamento'?' Sessão ainda em andamento: este é o último salvamento automático.':'');
 $('#akpi').innerHTML=(S?kpi('Duração',dur(p.duracao_s))+kpi('Bitrate médio',fmtR(ts.media))+kpi('Bitrate mínimo',fmtR(ts.min))+kpi('Bitrate máximo',fmtR(ts.max))
  :kpi('Bitrate TS',fmtR(r.taxa_total))+kpi('Bitrate útil',fmtR(r.taxa_util),'Sem pacotes nulos')+kpi('Sessão',dur(p.duracao_s)))
  +kpi('Taxa',r.cbr?'CBR':'VBR','CBR = TS com pacotes nulos (taxa constante)')+kpi('TS ID',r.tsid??'—')+kpi('Rede',esc(r.rede||'—'))
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
 // conexões SRT
 const ch=dados.find(x=>x.id===anaId),pss=(ch&&ch.status&&ch.status.saidas)||[];
 const ls=Object.entries(a.srt||{}).map(([k,v])=>({...v,_ps:k.startsWith('SRTOUT')?pss[+k.slice(6)]:null}));$('#psrt').hidden=!ls.length;$('#asrtn').textContent=ls.length?ls.length+' conexão(ões)':'';
 $('#asrt').innerHTML=ls.map(e=>{const c=e.estado==='conectado',tt=e.totais||{},W=600,H=90,h=e.hist||[];
  let g='';if(h.length>1){const mx=Math.max(...h.map(x=>x[0]))*1.2||1,mr=Math.max(...h.map(x=>x[2]))*1.2||1,X=i=>(i/(h.length-1)*W).toFixed(1);
   g=`<div class="leg"><span style="color:var(--acc)">— taxa (máx ${(mx/1.2).toFixed(2)} Mb/s)</span><span style="color:#d2a8ff">— RTT (máx ${(mr/1.2).toFixed(0)} ms)</span><span style="color:var(--err)">▮ perda</span></div>
   <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${h.map((x,i)=>x[1]>0?`<rect x="${X(i)}" y="${H-Math.min(H,x[1]*20+4)}" width="3" height="${Math.min(H,x[1]*20+4)}" fill="var(--err)"/>`:'').join('')}
   <polyline points="${h.map((x,i)=>`${X(i)},${(H-x[0]/mx*H).toFixed(1)}`).join(' ')}" style="fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke"/>
   <polyline points="${h.map((x,i)=>`${X(i)},${(H-x[2]/mr*H).toFixed(1)}`).join(' ')}" style="fill:none;stroke:#d2a8ff;stroke-width:1.2;vector-effect:non-scaling-stroke;opacity:.8"/></svg>`}
  const K=(r,v,cl)=>`<div><small>${r}</small><b class="${cl||''}">${v}</b></div>`;
  return`<div class="srtl"><div class="sh"><b>${esc(e.rotulo)}</b><span class="tp">SRT ${esc(e.modo)}</span><code>${esc(e.endereco)}</code>
  <span class="st ${a.salvo?'parado':e.estado}"><i></i>${a.salvo?'encerrada':e.estado}${e.peer?` · <code>${esc(e.peer)}</code>`:''}</span>
  <span style="margin-left:auto;color:var(--mut);font-size:12px">${e.conexoes} conexão(ões)${c&&!a.salvo?' · há '+dur(e.conectado_s):''}</span></div>
  ${!c&&!a.salvo&&e._ps&&e._ps.ultima_msg?`<div style="padding:7px 11px;color:var(--warn);font-size:12.5px;border-bottom:1px solid var(--line)">⚠ ${esc(msgCurta(e._ps.ultima_msg))}${e._ps.reinicios?` — ${e._ps.reinicios} tentativa(s), tentando de novo sozinho`:''}</div>`:''}
  <div class="sk">${c&&!a.salvo?K('Taxa',(e.taxa_mbps||0).toFixed(2)+' Mb/s')+K('RTT',e.rtt_ms+' ms')+K('Perda agora',(e.perda_pct||0).toFixed(2)+'%',e.perda_pct>1?'e':e.perda_pct>0?'w':'')
   +K('Retrans/s',e.retransmitidos,e.retransmitidos?'w':'')+K('Buffer',e.buffer_ms+' ms')+K('Latência',e.latencia_ms+' ms')+(e.banda_mbps?K('Banda do link',e.banda_mbps+' Mb/s'):''):''}
   ${K('RTT médio / máx',e.rtt_medio_ms+' / '+e.rtt_max_ms+' ms')}${K('Pacotes',(tt.pacotes||0).toLocaleString('pt-BR'))}
   ${K('Perdidos',(tt.perdidos||0).toLocaleString('pt-BR'),tt.perdidos?'w':'')}${K('Retransmitidos',(tt.retransmitidos||0).toLocaleString('pt-BR'))}
   ${K('Descartados',(tt.descartados||0).toLocaleString('pt-BR'),tt.descartados?'e':'')}</div>${g?`<div class="g">${g}</div>`:''}</div>`}).join('');
 // serviços
 $('#anserv').textContent=a.servicos.length+' serviço(s)';
 $('#aserv').innerHTML=a.servicos.map(s=>`<div class="serv"><div class="sh"><b>${esc(s.nome)}</b><span class="m">${esc(s.provedor||'')}</span>
  ${s.tipo?`<span class="badge">${esc(s.tipo)}</span>`:''}${s.embaralhado?'<span class="badge r">embaralhado</span>':''}<span class="tx">${fmtR(s.taxa)}</span></div>
  <div class="meta"><span>Programa <b>${s.numero}</b></span><span>PMT <b>${s.pmt}</b></span><span>PCR <b>${s.pcr_pid||'—'}</b></span>
  ${s.pcr?`<span>Intervalo PCR <b>${s.pcr.intervalo_ms} ms</b></span><span>Jitter PCR <b>${s.pcr.jitter_ns} ns</b></span>`:''}</div>
  <div class="tabw"><table class="t"><tr><th>PID</th><th>Componente</th><th>stream_type</th><th class="n">Bitrate</th></tr>
  ${s.componentes.map(c=>`<tr><td class="mono">${c.hex}</td><td>${esc(c.desc)}${c.emb?' <span class="badge r">emb</span>':''}</td><td class="mono">${c.stream_type}</td><td class="n">${fmtR(c.taxa)}</td></tr>`).join('')}</table></div></div>`).join('')
  ||'<div class="msg">nenhum serviço (aguardando PAT/PMT)</div>';
 rPids()}
const COLS=[['pid','PID',1],['pid','Dec',1],['desc','Descrição'],['servico','Serviço'],['taxa','Bitrate',1],['pct','%',1],['pacotes','Pacotes',1],['cc','Erros CC',1],['f','Flags']];
function rPids(){if(!ana)return;const l=[...ana.pids].sort((x,y)=>{const k=ordem.k,a=x[k],b=y[k];return(typeof a==='string'?a.localeCompare(b):a-b)*ordem.d});
 $('#anpid').textContent=l.length+' PIDs';
 $('#apids').innerHTML=`<tr>${COLS.map(([k,n,num])=>`<th class="${num?'n':''}" data-ord="${k}">${n}${ordem.k===k&&n!=='Dec'?(ordem.d>0?' ▲':' ▼'):''}</th>`).join('')}</tr>`
 +l.map(p=>`<tr class="${p.tipo==='desconhecido'?'unref':''} ${p.ausente?'aus':''}"><td class="mono n">${p.hex}</td><td class="n">${p.pid}</td><td>${esc(p.desc)}</td>
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

api('GET','/api/info').then(i=>{INFO=i;TRI=i.tr101290||[];render();if(!i.persistente)$('#avisos').innerHTML='<div class="aviso">Modo linha de comando: alterações feitas aqui não são salvas em arquivo. Use <code>asi2srt rodar -c arquivo.yaml</code> para persistir.</div>'}).catch(()=>{});
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
