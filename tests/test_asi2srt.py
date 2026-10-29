import os
import stat
import subprocess
import sys
import tempfile
import time
import json
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import asi2srt as a  # noqa: E402

PROG = os.path.join(os.path.dirname(__file__), "..", "asi2srt.py")


def test_entrada_dektec():
    e = a.entrada_de_texto("dektec:1:2")
    assert e["dektec"]["dispositivo"] == 1 and e["dektec"]["canal"] == 2


def test_entrada_udp_com_nic():
    e = a.entrada_de_texto("udp:239.1.1.1:1234@10.0.0.5")
    assert e["udp"] == {"endereco": "239.1.1.1:1234", "interface_local": "10.0.0.5"}


def test_saida_caller():
    s = a.saida_de_url("srt://203.0.113.10:9000?latency=300&streamid=abc")
    assert s["modo"] == "caller" and s["endereco"] == "203.0.113.10:9000"
    assert s["latencia_ms"] == 300 and s["stream_id"] == "abc"


def test_saida_listener():
    s = a.saida_de_url("srt://:9000?mode=listener&passphrase=1234567890")
    assert s == {**s, "modo": "listener", "endereco": "9000", "senha": "1234567890"}


def test_comando_completo():
    canal = {"entrada": a.entrada_de_texto("dektec:0:0"),
             "saida": a.saida_de_url("srt://h:1?passphrase=senhasecreta1"),
             "processamento": {"remover_pids": ["0x12"], "bitrate_min": 1000}}
    cmd = a.montar_comando(canal)
    linha = " ".join(cmd)
    assert "-I dektec --device 0 --channel 0" in linha
    assert "--pid 0x0012 --pid 0x1FFF" in linha
    assert "-O srt --caller h:1" in linha
    assert "--min 1000" in linha
    assert "********" in a.comando_para_log(cmd) and "senhasecreta1" not in a.comando_para_log(cmd)


def test_senha_curta_rejeitada():
    canal = {"entrada": a.entrada_de_texto("arquivo:x.ts"),
             "saida": a.saida_de_url("srt://h:1?passphrase=curta")}
    try:
        a.montar_comando(canal)
        assert False
    except ValueError:
        pass


def test_config_exemplo_valida():
    import yaml
    cfg = yaml.safe_load(a.CONFIG_EXEMPLO)
    a.montar_comando(cfg["canais"][0])


def test_supervisor_com_tsp_falso():
    """Roda o gateway com um tsp falso: lê bitrate, conta erro CC e reinicia."""
    d = tempfile.mkdtemp()
    falso = os.path.join(d, "tsp")
    with open(falso, "w") as f:
        f.write("#!/bin/sh\n"
                "echo '* bitrate_monitor: 2026/01/01 00:00:00, TS bitrate: 8,000,000 bits/s'\n"
                "echo '* continuity: PID: 0x0100, missing 2 packets'\n"
                "sleep 1; exit 1\n")
    os.chmod(falso, os.stat(falso).st_mode | stat.S_IEXEC)
    p = subprocess.Popen([sys.executable, PROG, "rodar", "-e", "dektec:0:0",
                          "-s", "srt://h:9000", "--tsp", falso, "--status", "127.0.0.1:18088"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        time.sleep(4)
        with urllib.request.urlopen("http://127.0.0.1:18088/status.json") as r:
            st = json.load(r)[0]
        assert st["erros_cc"] >= 1
        assert st["reinicios"] >= 1
    finally:
        p.terminate()
        p.wait(10)
