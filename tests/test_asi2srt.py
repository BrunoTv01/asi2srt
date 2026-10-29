import base64
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import asi2srt as a  # noqa: E402

PROG = os.path.join(os.path.dirname(__file__), "..", "asi2srt.py")


# ---------------- atalhos de linha de comando ----------------
def test_entrada_dektec():
    e = a.entrada_de_texto("dektec:1:2")
    assert e == {"tipo": "dektec", "dispositivo": 1, "canal": 2, "timeout_ms": 5000}


def test_entrada_udp_com_nic():
    assert a.entrada_de_texto("udp:239.1.1.1:1234@10.0.0.5")["interface_local"] == "10.0.0.5"
    assert a.entrada_de_texto("udp://239.1.1.1:1234?local=10.0.0.5")["endereco"] == "239.1.1.1:1234"


def test_entrada_srt_listener():
    e = a.entrada_de_texto("srt://:9000?mode=listener&passphrase=1234567890&latency=500")
    assert e["tipo"] == "srt" and e["modo"] == "listener" and e["endereco"] == "9000"
    assert e["latencia_ms"] == 500


def test_saida_srt_caller():
    s = a.saida_de_url("srt://203.0.113.10:9000?latency=300&streamid=abc")
    assert s["tipo"] == "srt" and s["modo"] == "caller" and s["endereco"] == "203.0.113.10:9000"
    assert s["latencia_ms"] == 300 and s["stream_id"] == "abc"


def test_saida_udp():
    s = a.saida_de_url("udp://239.1.1.1:1234?ttl=8&local=10.0.0.9")
    assert s == {"tipo": "udp", "endereco": "239.1.1.1:1234", "ttl": 8, "interface_local": "10.0.0.9"}


# ---------------- montagem do comando ----------------
def test_comando_dektec_srt():
    canal = {"entrada": a.entrada_de_texto("dektec:0:0"),
             "saidas": [a.saida_de_url("srt://h:1?passphrase=senhasecreta1")],
             "processamento": {"remover_pids": ["0x12"], "bitrate_min": 1000}}
    cmd = a.montar_comando(canal)
    linha = " ".join(cmd)
    assert "-I dektec --device 0 --channel 0" in linha
    assert "--pid 0x0012 --pid 0x1FFF" in linha
    assert "-O srt --caller h:1" in linha and "--min 1000" in linha
    assert "senhasecreta1" not in a.comando_para_log(cmd)


def test_comando_com_analise():
    canal = {"entrada": a.entrada_de_texto("dektec:0:0"), "saidas": [a.saida_de_url("udp://1.2.3.4:5")]}
    cmd = a.montar_comando(canal, porta_analise=40000)
    i = cmd.index("fork")
    assert cmd.index("fork") < cmd.index("filter")          # analisa o TS bruto, antes do filtro
    assert "-O ip 127.0.0.1:40000" in cmd[i + 4]
    canal["processamento"] = {"analise_tr101290": False}
    assert "fork" not in a.montar_comando(canal, porta_analise=40000)


def test_comando_srt_para_udp_e_srt():
    canal = {"entrada": a.entrada_de_texto("srt://:9000?mode=listener"),
             "saidas": [a.saida_de_url("udp://239.1.1.1:1234?ttl=4"),
                        a.saida_de_url("srt://h:2?passphrase=outrasenha1")]}
    cmd = a.montar_comando(canal)
    linha = " ".join(cmd)
    assert "-I srt --listener 9000" in linha
    assert "-O ip 239.1.1.1:1234 --packet-burst 7 --ttl 4" in linha
    i = cmd.index("fork")
    assert "-O srt --caller h:2" in cmd[i + 2]
    log = a.comando_para_log(cmd)
    assert "outrasenha1" not in log and "********" in log


def test_formato_antigo_compativel():
    antigo = {"nome": "x", "entrada": {"tipo": "dektec", "dektec": {"dispositivo": 2, "canal": 1},
                                       "udp": {"endereco": "1.1.1.1:1"}},
              "saida": {"modo": "caller", "endereco": "h:9"}}
    c = a.normalizar_canal(antigo)
    assert c["entrada"] == {"dispositivo": 2, "canal": 1, "tipo": "dektec"}
    assert c["saidas"][0]["tipo"] == "srt"
    assert "--device 2 --channel 1" in " ".join(a.montar_comando(antigo))


@pytest.mark.parametrize("canal,erro", [
    ({"entrada": {"tipo": "arquivo", "caminho": "x.ts"}, "saidas": []}, "pelo menos uma saída"),
    ({"entrada": {"tipo": "arquivo", "caminho": "x.ts"},
      "saidas": [{"tipo": "srt", "endereco": "h:1", "senha": "curta"}]}, "10 e 79"),
    ({"entrada": {"tipo": "arquivo", "caminho": "x.ts"},
      "saidas": [{"tipo": "udp", "endereco": "sem-porta"}]}, "ip:porta"),
])
def test_validacoes(canal, erro):
    with pytest.raises(ValueError, match=erro):
        a.montar_comando(canal)


def test_config_exemplo_valida():
    import yaml
    cfg = yaml.safe_load(a.CONFIG_EXEMPLO)
    a.montar_comando(cfg["canais"][0])


# ---------------- gateway + painel/API com tsp falso ----------------
def _porta_livre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def gateway():
    d = tempfile.mkdtemp()
    falso = os.path.join(d, "tsp")
    with open(falso, "w") as f:
        f.write("#!/bin/sh\n"
                "while true; do\n"
                "echo '* bitrate_monitor: 2026/01/01 00:00:00, TS bitrate: 8,000,000 bits/s'\n"
                "echo '* continuity: PID: 0x0100, missing 2 packets'\n"
                "sleep 1; done\n")
    os.chmod(falso, os.stat(falso).st_mode | stat.S_IEXEC)
    cfg = os.path.join(d, "cfg.json")
    porta = _porta_livre()
    with open(cfg, "w") as f:
        json.dump({"canais": [], "supervisor": {"tsp": falso, "status_http": f"127.0.0.1:{porta}",
                                                "usuario": "admin", "senha_painel": "segredo"}}, f)
    p = subprocess.Popen([sys.executable, PROG, "rodar", "-c", cfg],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1)
    yield f"http://127.0.0.1:{porta}", cfg
    p.terminate()
    p.wait(10)


def _req(base, metodo, caminho, corpo=None, auth=("admin", "segredo"), csrf=True):
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-Asi2srt"] = "1"
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(":".join(auth).encode()).decode()
    r = urllib.request.Request(base + caminho, method=metodo, headers=h,
                               data=json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        corpo = e.read()
        return e.code, (json.loads(corpo) if corpo else None)


def test_api_completa(gateway):
    base, cfg = gateway
    assert _req(base, "GET", "/api/canais", auth=None)[0] == 401
    assert _req(base, "GET", "/api/canais", auth=("admin", "errada"))[0] == 401
    assert _req(base, "POST", "/api/canais", {}, csrf=False)[0] == 403

    canal = {"nome": "Teste", "entrada": {"tipo": "srt", "modo": "listener", "endereco": "9000"},
             "saidas": [{"tipo": "udp", "endereco": "239.1.1.1:1234"},
                        {"tipo": "srt", "modo": "caller", "endereco": "h:9000"}]}
    cod, criado = _req(base, "POST", "/api/canais", canal)
    assert cod == 201
    cid = criado["id"]

    time.sleep(2.5)
    _, lista = _req(base, "GET", "/api/canais")
    st = lista[0]["status"]
    assert st["estado"] == "rodando" and st["bitrate_bps"] == 8000000
    assert set(st["tr101290"]["contadores"]) == {"sync_loss", "sync_byte", "cc", "pat", "pmt", "pid"}
    assert _req(base, "POST", f"/api/canais/{cid}/zerar")[0] == 200
    assert len(_req(base, "GET", f"/api/canais/{cid}/log")[1]) >= 1

    # inválido é rejeitado
    ruim = dict(canal, saidas=[{"tipo": "srt", "endereco": "h:1", "senha": "x"}])
    assert _req(base, "PUT", f"/api/canais/{cid}", ruim)[0] == 400

    assert _req(base, "POST", f"/api/canais/{cid}/parar")[0] == 200
    _, lista = _req(base, "GET", "/api/canais")
    assert lista[0]["status"]["estado"] == "parado" and lista[0]["ativo"] is False

    # persistido no arquivo
    salvo = json.load(open(cfg))
    assert salvo["canais"][0]["nome"] == "Teste" and salvo["canais"][0]["ativo"] is False

    assert _req(base, "DELETE", f"/api/canais/{cid}")[0] == 200
    assert _req(base, "GET", "/api/canais")[1] == []


def test_painel_html(gateway):
    base, _ = gateway
    r = urllib.request.Request(base + "/", headers={
        "Authorization": "Basic " + base64.b64encode(b"admin:segredo").decode()})
    with urllib.request.urlopen(r) as resp:
        html = resp.read().decode()
    assert "Novo canal" in html and a.__version__ in html


# Opções reais do TSDuck (extraídas do código-fonte: tsSRTArgs.cpp, tsTSDatagramOutput.cpp,
# tsUDPReceiverArgs.cpp, tsForkPacketPlugin.cpp, tsBitrateMonitorPlugin.cpp, tsTSProcessorArgs.cpp,
# tsDektecInputPlugin.cpp, tsTSFileInputArgs.cpp). Garante que nenhuma opção inventada chegue ao tsp.
OPCOES_TSDUCK = {
    "tsp": {"--realtime", "--buffer-size-mb", "--max-flushed-packets"},
    "srt": {"--caller", "--listener", "--transtype", "--latency", "--ohead-bw", "--passphrase",
            "--pbkeylen", "--streamid"},
    "ip": {"--packet-burst", "--ttl", "--local-address", "--receive-timeout"},
    "dektec": {"--device", "--channel", "--receive-timeout"},
    "file": {"--infinite"},
    "fork": {"--nowait", "--buffered-packets"},
    "filter": {"--negate", "--pid"},
    "bitrate_monitor": {"--periodic-bitrate", "--time-interval", "--min", "--max"},
}


def _verificar_opcoes(cmd):
    plugin = "tsp"
    for arg in cmd[1:]:
        if arg in ("-I", "-P", "-O"):
            plugin = None
            continue
        if plugin is None:
            plugin = arg
            continue
        if arg.startswith("--"):
            assert arg in OPCOES_TSDUCK[plugin], f"opção {arg} não existe no plugin {plugin}"
        if plugin == "fork" and arg.startswith(("/", "tsp")) or " -O " in arg:
            _verificar_opcoes(shlex.split(arg))


import shlex  # noqa: E402


@pytest.mark.parametrize("entrada", ["dektec:0:1", "linuxasi:/dev/asirx0", "udp://239.1.1.1:1234?local=10.0.0.1",
                                     "srt://:9000?mode=listener&passphrase=1234567890&streamid=x",
                                     "srt://h:9000?latency=500", "arquivo:x.ts"])
def test_opcoes_existem_no_tsduck(entrada):
    canal = {"entrada": a.entrada_de_texto(entrada),
             "saidas": [a.saida_de_url("srt://h:1?passphrase=1234567890&streamid=s&oheadbw=30"),
                        a.saida_de_url("udp://239.1.1.1:1?ttl=4&local=10.0.0.2"),
                        a.saida_de_url("srt://:9001?mode=listener")],
             "processamento": {"remover_pids": ["0x20"], "bitrate_min": 1, "bitrate_max": 9}}
    _verificar_opcoes(a.montar_comando(canal, porta_analise=40000))


def test_linhas_reais_do_bitrate_monitor():
    real = "* bitrate_monitor: 2026/10/29 12:00:00, TS bitrate: 8,123,456 bits/s, net bitrate: 7,900,000 bits/s"
    alarme = "* bitrate_monitor: TS bitrate (512,000 bits/s) is lower than allowed minimum (1,000,000 bits/s)"
    m = a.RE_BITRATE.search(real)
    assert m and int(m.group(1).replace(",", "")) == 8123456
    assert a.RE_ALARME.search(alarme) and not a.RE_ALARME.search(real)
