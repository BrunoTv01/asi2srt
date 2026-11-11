import io
import base64
import shutil
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
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
    cmd = a.montar_comando(canal, porta_dist=41000)
    linha = " ".join(cmd)
    assert "-I dektec --device 0 --channel 0" in linha
    assert "--pid 0x0012 --pid 0x1FFF" in linha and "--min 1000" in linha
    assert cmd[-4:] == ["ip", "127.0.0.1:41000", "--packet-burst", str(a.LOTE_INTERNO)]   # entrega ao distribuidor local
    assert "srt" not in cmd                                                # a saída não fica neste processo
    sai = a.montar_saida(canal["saidas"][0], 0, porta_entrada=42000)
    assert "-I ip 42000 --local-address 127.0.0.1" in " ".join(sai) and "-O srt --caller h:1" in " ".join(sai)
    _verificar_opcoes(sai)
    assert "senhasecreta1" not in a.comando_para_log(sai)


def test_comando_com_analise():
    canal = {"entrada": a.entrada_de_texto("dektec:0:0"), "saidas": [a.saida_de_url("udp://1.2.3.4:5")]}
    cmd = a.montar_comando(canal, porta_analise=40000)
    i = cmd.index("fork")
    assert cmd.index("fork") < cmd.index("filter")          # analisa o TS bruto, antes do filtro
    assert any("-O ip 127.0.0.1:40000" in x for x in cmd[i:i + 6])
    canal["processamento"] = {"analise_tr101290": False}
    assert "fork" not in a.montar_comando(canal, porta_analise=40000)


def test_comando_srt_para_udp_e_srt():
    canal = {"entrada": a.entrada_de_texto("srt://:9000?mode=listener"),
             "saidas": [a.saida_de_url("udp://239.1.1.1:1234?ttl=4"),
                        a.saida_de_url("srt://h:2?passphrase=outrasenha1")]}
    cmd = a.montar_comando(canal)
    assert "-I srt --listener 9000" in " ".join(cmd)
    s0 = " ".join(a.montar_saida(canal["saidas"][0], 0))
    s1 = a.montar_saida(canal["saidas"][1], 1)
    assert "-O ip 239.1.1.1:1234 --packet-burst 7 --ttl 4" in s0
    assert "-O srt --caller h:2" in " ".join(s1) and "--json-line=SRTOUT1:" in s1
    log = a.comando_para_log(s1)
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
    assert set(st["tr101290"]["contadores"]) == set(a.TIPOS_TR101290)
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
    "tsp": {"--realtime", "--buffer-size-mb", "--max-flushed-packets", "--verbose"},
    "srt": {"--caller", "--listener", "--transtype", "--latency", "--ohead-bw", "--passphrase",
            "--pbkeylen", "--streamid", "--statistics-interval", "--json-line", "--local-interface"},
    "ip": {"--packet-burst", "--ttl", "--local-address", "--receive-timeout", "--buffer-size",
           "--rtp", "--payload-type", "--ssrc-identifier"},
    "dektec": {"--device", "--channel", "--receive-timeout"},
    "file": {"--infinite"},
    "fork": {"--nowait", "--ignore-abort", "--buffered-packets"},
    "drop": set(),
    "filter": {"--negate", "--pid"},
    "bitrate_monitor": {"--periodic-bitrate", "--time-interval", "--min", "--max"},
}


def _verificar_opcoes(cmd):
    """Confere opções contra as do TSDuck e aplica a regra da entrada 'ip' (tsUDPReceiverArgs.cpp):
    se o parâmetro tiver endereço, ele precisa ser multicast."""
    import ipaddress
    for i, x in enumerate(cmd[:-2]):
        if x == "-I" and cmd[i + 1] == "ip":
            dest = cmd[i + 2]
            host = dest.rpartition(":")[0].split("@")[-1]
            assert not host or ipaddress.ip_address(host).is_multicast, \
                f"TSDuck recusaria: address {dest} is not multicast"
    plugin = "tsp"
    for arg in cmd[1:]:
        if arg in ("-I", "-P", "-O"):
            plugin = None
            continue
        if plugin is None:
            plugin = arg
            continue
        if arg.startswith("--"):
            nome = arg.split("=", 1)[0]
            assert nome in OPCOES_TSDUCK[plugin], f"opção {nome} não existe no plugin {plugin}"
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
                        a.saida_de_url("srt://:9001?mode=listener"),
                        a.saida_de_url("rtp://239.2.2.2:5004?ttl=8&pt=96&ssrc=0x1234")],
             "processamento": {"remover_pids": ["0x20"], "bitrate_min": 1, "bitrate_max": 9}}
    _verificar_opcoes(a.montar_comando(canal, porta_analise=40000, porta_dist=41000))
    for i, s in enumerate(canal["saidas"]):
        _verificar_opcoes(a.montar_saida(s, i, porta_entrada=42000 + i))


def test_linhas_reais_do_bitrate_monitor():
    real = "* bitrate_monitor: 2026/10/29 12:00:00, TS bitrate: 8,123,456 bits/s, net bitrate: 7,900,000 bits/s"
    alarme = "* bitrate_monitor: TS bitrate (512,000 bits/s) is lower than allowed minimum (1,000,000 bits/s)"
    m = a.RE_BITRATE.search(real)
    assert m and int(m.group(1).replace(",", "")) == 8123456
    assert a.RE_ALARME.search(alarme) and not a.RE_ALARME.search(real)


@pytest.mark.parametrize("end,modo", [("187.60.226.82?6001", "caller"), ("187.60.226.82", "caller"),
                                      ("9000x", "listener")])
def test_endereco_srt_invalido(end, modo):
    canal = {"entrada": {"tipo": "srt", "modo": modo, "endereco": end},
             "saidas": [{"tipo": "udp", "endereco": "127.0.0.1:5000"}]}
    with pytest.raises(ValueError, match="inválido"):
        a.montar_comando(canal)


def test_canal_so_analise():
    for ent in ("srt://h:6001?latency=2000", "udp://239.1.1.1:1234", "dektec:0:0", "linuxasi:/dev/asirx0"):
        cmd = a.montar_comando({"entrada": a.entrada_de_texto(ent), "saidas": []}, porta_analise=40000)
        assert cmd[-2:] == ["-O", "drop"]
        assert "filter" not in cmd           # sem saída, mantém o TS completo (com nulos)
        assert "fork" in cmd and "bitrate_monitor" in cmd
        _verificar_opcoes(cmd)


# ---------------- relatório salvo ao parar ----------------
TSP_COM_TS = r'''#!/usr/bin/env python3
import sys, re, socket, time, struct, zlib
porta = int(re.search(r"127\.0\.0\.1:(\d+)", " ".join(sys.argv)).group(1))
INV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
def crc(d):
    c = zlib.crc32(d.translate(INV)) ^ 0xFFFFFFFF
    return int(f"{c:032b}"[::-1], 2)
def sec(tid, ext, corpo):
    d = struct.pack(">H", ext) + b"\xc1\x00\x00" + corpo
    s = bytes([tid, 0xB0 | ((len(d) + 4) >> 8), (len(d) + 4) & 0xFF]) + d
    return s + struct.pack(">I", crc(s))
pat = sec(0, 7, struct.pack(">HH", 1, 0xE100))
pmt = sec(2, 1, struct.pack(">HH", 0xE101, 0xF000) + struct.pack(">BHH", 0x1B, 0xE101, 0xF000))
cc = {}
def pkt(pid, carga=b"", pusi=False):
    cc[pid] = (cc.get(pid, -1) + 1) & 15
    return (bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, 0x10 | cc[pid]]) + carga).ljust(188, b"\xff")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
n = 0
while True:
    d = pkt(0, b"\x00" + pat, True) + pkt(0x100, b"\x00" + pmt, True) + b"".join(pkt(0x101) for _ in range(5))
    s.sendto(d, ("127.0.0.1", porta)); n += 1
    if n % 20 == 0: print("* bitrate_monitor: TS bitrate: 1,000,000 bits/s", flush=True)
    time.sleep(0.05)
'''


def _subir(cfg_path, porta):
    p = subprocess.Popen([sys.executable, PROG, "rodar", "-c", cfg_path],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", porta), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    return p


def test_relatorio_salvo_ao_parar_e_persistente():
    d = tempfile.mkdtemp()
    tsp = os.path.join(d, "tsp")
    with open(tsp, "w") as f:
        f.write(TSP_COM_TS)
    os.chmod(tsp, 0o755)
    porta = _porta_livre()
    cfg = os.path.join(d, "cfg.json")
    with open(cfg, "w") as f:
        json.dump({"canais": [{"id": "c1", "nome": "Sinal", "entrada": {"tipo": "udp", "endereco": "1234"},
                               "saidas": []}],
                   "supervisor": {"tsp": tsp, "status_http": f"127.0.0.1:{porta}",
                                  "usuario": "admin", "senha_painel": "segredo"}}, f)
    base = f"http://127.0.0.1:{porta}"
    p = _subir(cfg, porta)
    try:
        time.sleep(3.5)
        assert _req(base, "GET", "/api/canais/c1/analise")[1]["salvo"] is False
        assert _req(base, "POST", "/api/canais/c1/parar")[0] == 200
        _, lista = _req(base, "GET", "/api/canais")
        r = lista[0]["ultimo_relatorio"]
        assert r and r["motivo"] == "parado" and r["n_servicos"] == 1 and r["duracao_s"] >= 3
        assert r["taxa"]["media"] > 0 and set(r["totais"]) == {"p1", "p2", "p3"}
        cod, a = _req(base, "GET", "/api/canais/c1/analise")
        assert cod == 200 and a["salvo"] is True and a["resumo"]["tsid"] == 7
        assert a["servicos"][0]["componentes"][0]["desc"] == "H.264/AVC vídeo"
    finally:
        p.terminate()
        p.wait(10)
    assert os.path.exists(os.path.join(d, "cfg-relatorios.json"))
    # depois de reiniciar o programa, o resumo continua lá
    p = _subir(cfg, porta)
    try:
        _, lista = _req(base, "GET", "/api/canais")
        assert lista[0]["ultimo_relatorio"]["n_servicos"] == 1
        assert len(_req(base, "GET", "/api/canais/c1/relatorios")[1]) == 1
        r = urllib.request.Request(base + "/api/canais/c1/analise?sessao=0&baixar=1", headers={
            "Authorization": "Basic " + base64.b64encode(b"admin:segredo").decode()})
        with urllib.request.urlopen(r) as resp:
            assert "attachment" in resp.headers["Content-Disposition"]
            assert json.loads(resp.read())["salvo"] is True
    finally:
        p.terminate()
        p.wait(10)


# ---------------- escolha do pacote do TSDuck (Raspberry Pi e outros) ----------------
PACOTES = [
    "tsduck_3.45-4798.ubuntu24_amd64.deb", "tsduck_3.45-4798.ubuntu24_arm64.deb",
    "tsduck-dev_3.45-4798.ubuntu24_arm64.deb", "tsduck_3.45-4798.ubuntu25_amd64.deb",
    "tsduck_3.45-4798.debian12_amd64.deb", "tsduck_3.45-4798.debian12_arm64.deb",
    "tsduck_3.45-4798.debian13_arm64.deb", "tsduck-dev_3.45-4798.debian13_arm64.deb",
    "tsduck_3.45-4798.raspbian12_armhf.deb", "tsduck-dev_3.45-4798.raspbian12_armhf.deb",
    "tsduck-3.45-4798.el9.x86_64.rpm", "tsduck-3.45-4798.el9.aarch64.rpm",
    "tsduck-3.45-4798.fc42.aarch64.rpm", "tsduck-devel-3.45-4798.fc42.aarch64.rpm",
]


@pytest.mark.parametrize("info,arq,deb,esperado", [
    # Raspberry Pi OS 64 bits (bookworm)
    ({"ID": "debian", "VERSION_ID": "12"}, "arm64", True, "tsduck_3.45-4798.debian12_arm64.deb"),
    # Raspberry Pi OS 64 bits (trixie)
    ({"ID": "debian", "VERSION_ID": "13"}, "arm64", True, "tsduck_3.45-4798.debian13_arm64.deb"),
    # Raspberry Pi OS 32 bits
    ({"ID": "raspbian", "VERSION_ID": "12", "ID_LIKE": "debian"}, "armhf", True,
     "tsduck_3.45-4798.raspbian12_armhf.deb"),
    # Raspberry Pi OS 32 bits mais novo que o pacote: usa o raspbian mais recente
    ({"ID": "raspbian", "VERSION_ID": "13", "ID_LIKE": "debian"}, "armhf", True,
     "tsduck_3.45-4798.raspbian12_armhf.deb"),
    # Ubuntu no Raspberry
    ({"ID": "ubuntu", "VERSION_ID": "24.04", "ID_LIKE": "debian"}, "arm64", True,
     "tsduck_3.45-4798.ubuntu24_arm64.deb"),
    # Ubuntu de PC (a VM)
    ({"ID": "ubuntu", "VERSION_ID": "24.04", "ID_LIKE": "debian"}, "amd64", True,
     "tsduck_3.45-4798.ubuntu24_amd64.deb"),
    # derivado de Debian sem pacote próprio: cai no Debian
    ({"ID": "linuxmint", "VERSION_ID": "22", "ID_LIKE": "ubuntu debian"}, "arm64", True,
     "tsduck_3.45-4798.ubuntu24_arm64.deb"),
    ({"ID": "fedora", "VERSION_ID": "42"}, "aarch64", False, "tsduck-3.45-4798.fc42.aarch64.rpm"),
    ({"ID": "rocky", "VERSION_ID": "9.4"}, "x86_64", False, "tsduck-3.45-4798.el9.x86_64.rpm"),
    # 32 bits sem pacote raspbian: não instala pacote errado
    ({"ID": "debian", "VERSION_ID": "12"}, "armhf", True, None),
])
def test_escolher_pacote_tsduck(info, arq, deb, esperado):
    assert a.escolher_pacote_tsduck(PACOTES, info, arq, deb) == esperado


# ---------------- DekTec: detecção de placa e driver ----------------
def _sysfs(raiz, barramento, nome, arquivos):
    d = os.path.join(raiz, barramento, "devices", nome)
    os.makedirs(d)
    for k, v in arquivos.items():
        with open(os.path.join(d, k), "w") as f:
            f.write(v + "\n")


def test_detectar_dektec_pci_e_usb():
    raiz = tempfile.mkdtemp()
    _sysfs(raiz, "pci", "0000:03:00.0", {"vendor": "0x1a0e", "device": "0x0891"})      # DekTec PCIe
    _sysfs(raiz, "pci", "0000:00:02.0", {"vendor": "0x8086", "device": "0x3e92"})      # Intel (ignorar)
    _sysfs(raiz, "usb", "2-1", {"idVendor": "1a0e", "idProduct": "00f5"})              # DekTec USB
    _sysfs(raiz, "usb", "1-1", {"idVendor": "046d", "idProduct": "c52b"})              # mouse (ignorar)
    achados = a.detectar_dektec(raiz)
    assert [(p["barramento"], p["produto"]) for p in achados] == [("PCI", "0891"), ("USB", "00f5")]
    assert a.detectar_dektec(tempfile.mkdtemp()) == []


def test_driver_dektec_carregado():
    d = tempfile.mkdtemp()
    m = os.path.join(d, "modules")
    with open(m, "w") as f:
        f.write("snd_hda_intel 61440 3 - Live 0x0\nDtPcie 1871872 0 - Live 0x0\n")
    assert a.driver_dektec_carregado(m)
    with open(m, "w") as f:
        f.write("snd_hda_intel 61440 3 - Live 0x0\nDtPcieX 1 0 - Live 0x0\n")
    assert not a.driver_dektec_carregado(m)


def test_procurar_sdk_dektec_pega_o_mais_recente():
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    velho, novo = os.path.join(d1, "LinuxSDK_v2024.06.0.tar.gz"), os.path.join(d2, "LinuxSDK_v2025.03.0.tar.gz")
    for arq, t in ((velho, 1000), (novo, 2000)):
        open(arq, "w").close()
        os.utime(arq, (t, t))
    open(os.path.join(d1, "outro.tar.gz"), "w").close()
    assert a.procurar_sdk_dektec([d1, d2]) == novo
    assert a.procurar_sdk_dektec([tempfile.mkdtemp()]) is None


# ---------------- estatísticas SRT (formato do TSDuck: tsSRTSocket.cpp, --json-line) ----------------
def _linha_srt(prefixo, rx=True, taxa=12.0, pac=1100, perd=0, retr=0, rtt=14.5):
    j = {"global": {"instant": {"rtt-ms": rtt}}}
    if rx:
        j["receive"] = {"interval": {"rate-mbps": taxa, "packets": pac, "lost-packets": perd, "dropped-packets": 0,
                                     "retransmitted-packets": retr},
                        "instant": {"buffer-ack-ms": 1980, "delivery-delay-ms": 2000}}
        j["send"] = {"instant": {"estimated-link-bandwidth-mbps": 0}}
    else:
        j["send"] = {"interval": {"send-rate-mbps": taxa, "packets": pac, "lost-packets": perd, "dropped-packets": 0,
                                  "retransmit-packets": retr},
                     "instant": {"snd-buffer-ms": 120, "delivery-delay-ms": 200,
                                 "estimated-link-bandwidth-mbps": 87.4}}
    return f"* srt: {prefixo}:" + json.dumps(j)


def test_estatisticas_srt_entrada_e_saida():
    canal = {"nome": "x", "entrada": a.entrada_de_texto("srt://187.60.226.82:6001?latency=2000"),
             "saidas": [a.saida_de_url("udp://127.0.0.1:5000"), a.saida_de_url("srt://:9000?mode=listener")]}
    c = a.Canal(canal, {"tsp": "tsp"})
    try:
        assert set(c.srt) == {"SRTIN", "SRTOUT1"}
        assert "--json-line=SRTIN:" in c.cmd and "--json-line=SRTOUT1:" in c.procs_saida[1].cmd
        linhas = ["* srt: connected from 187.60.226.82:6001 (local: 172.20.15.119:40122)",
                  _linha_srt("SRTIN", perd=11, pac=1089, retr=5),
                  _linha_srt("SRTOUT1", rx=False, taxa=11.8, retr=2),
                  "* bitrate_monitor: 2026/10/29 15:00:00, TS bitrate: 12,000,000 bits/s"]
        import io
        c._ler_saida(io.StringIO("\n".join(linhas) + "\n"))
        st = c.srt_status()
        e = st["SRTIN"]
        assert e["estado"] == "conectado" and e["peer"] == "187.60.226.82:6001"
        assert e["rtt_ms"] == 14.5 and e["taxa_mbps"] == 12.0 and e["perda_pct"] == 1.0
        assert e["retransmitidos"] == 5 and e["latencia_ms"] == 2000 and e["totais"]["perdidos"] == 11
        s = st["SRTOUT1"]
        assert s["papel"] == "saida" and s["banda_mbps"] == 87.4 and s["retransmitidos"] == 2
        assert c.status()["bitrate_bps"] == 12_000_000          # outras linhas continuam sendo lidas
        assert not any("SRTIN:{" in l for l in c.log)            # estatísticas não poluem o log
        # sem novas estatísticas por mais de 3 s: aguardando
        est = c.srt["SRTIN"]
        assert est.status(agora=est.ultimo + 5)["estado"] == "aguardando"
    finally:
        c.analisador.fechar()



# ---------------- saídas independentes ----------------
def test_distribuidor_entrega_copia_a_cada_saida():
    import socket
    dist = a.Distribuidor()
    receptores = []
    for _ in range(3):
        r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        r.bind(("127.0.0.1", 0))
        r.settimeout(2)
        receptores.append(r)
    dist.iniciar([r.getsockname()[1] for r in receptores])
    try:
        pacote = bytes([0x47, 0x01, 0x00, 0x10]).ljust(188, b"\xff") * 7
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(pacote, ("127.0.0.1", dist.porta))
        assert all(r.recv(2048) == pacote for r in receptores)
    finally:
        dist.fechar()


TSP_SAIDA_FALHA = r"""#!/bin/sh
case "$*" in
  *"-I ip 127.0.0.1:"*) echo "* Error: ip: address 127.0.0.1 is not multicast"; exit 1;;
  *"-O srt"*) echo "* Error: srt: error during srt_connect: Connection setup failure: connection timed out"; exit 1;;
  *"--local-address 127.0.0.1"*) while true; do echo "* bitrate_monitor: TS bitrate: 9,000,000 bits/s"; sleep 0.5; done;;
  *) while true; do echo "* bitrate_monitor: TS bitrate: 9,000,000 bits/s"; sleep 0.5; done;;
esac
"""


def test_saida_com_falha_nao_derruba_a_entrada():
    d = tempfile.mkdtemp()
    tsp = os.path.join(d, "tsp")
    with open(tsp, "w") as f:
        f.write(TSP_SAIDA_FALHA)
    os.chmod(tsp, 0o755)
    canal = {"nome": "x", "entrada": a.entrada_de_texto("udp://239.1.1.1:1234"),
             "saidas": [a.saida_de_url("srt://200.136.26.171:8099"), a.saida_de_url("udp://127.0.0.1:5000")]}
    c = a.Canal(canal, {"tsp": tsp, "reinicio_min_s": 1, "reinicio_max_s": 2})
    c.start()
    try:
        time.sleep(4)
        st = c.status()
        assert st["estado"] == "rodando" and st["bitrate_bps"] == 9_000_000 and st["reinicios"] == 0
        srt_out, udp_out = st["saidas"]
        assert srt_out["reinicios"] >= 1 and srt_out["estado"] in ("reiniciando", "conectando")
        assert "connection timed out" in srt_out["ultima_msg"]
        assert udp_out["estado"] == "rodando" and udp_out["reinicios"] == 0
        assert udp_out["info"]["taxa_kbps"] == 9000 and udp_out["enviado_bytes"] > 0 and udp_out["conexoes"] == 1
        assert srt_out["conexoes"] == 0 and srt_out["enviado_bytes"] == 0
        assert st["srt"]["SRTOUT0"]["estado"] == "aguardando"
    finally:
        c.encerrar()



# ---------------- saídas RTP e RTMP ----------------
def test_saida_rtp():
    s = a.saida_de_url("rtp://239.2.2.2:5004?ttl=8&local=10.0.0.5&pt=96&ssrc=0x1234")
    cmd = " ".join(a.montar_saida(s, 0, porta_entrada=42000))
    assert "-O ip 239.2.2.2:5004 --packet-burst 7 --ttl 8 --local-address 10.0.0.5 --rtp --payload-type 96 --ssrc-identifier 4660" in cmd
    padrao = " ".join(a.montar_saida(a.saida_de_url("rtp://10.0.0.9:5004"), 0))
    assert "--rtp" in padrao and "--payload-type" not in padrao     # 33 (MP2T) é o padrão do TSDuck
    with pytest.raises(ValueError, match="payload type"):
        a.montar_saida({"tipo": "rtp", "endereco": "1.2.3.4:5", "payload_type": 200}, 0)


def test_saida_rtmp_comando_e_chave_mascarada():
    s = a.saida_de_url("rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl")
    assert s["video"] == "copiar" and s["audio"] == "aac"
    cmd = a.montar_saida(s, 0, porta_entrada=42000)
    assert cmd[0] == "ffmpeg" and "udp://127.0.0.1:42000" in cmd[cmd.index("-i") + 1]  # ffmpeg aceita unicast
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[-3:] == ["-flvflags", "no_duration_filesize", "rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl"]
    erro = "[out#0/flv @ 0x5] Error opening output rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl: Connection timed out"
    assert "abcd-efgh-ijkl" not in a.mascarar(erro) and "Connection timed out" in a.mascarar(erro)
    assert "segredo123" not in a.mascarar("srt://h:1?mode=caller&passphrase=segredo123&latency=200")
    assert "abcd-efgh-ijkl" not in a.comando_para_log(cmd) and "live2/********" in a.comando_para_log(cmd)
    # servidor e chave em campos separados (como o painel salva)
    sep = a.montar_saida({"tipo": "rtmp", "url": "rtmps://live-api-s.facebook.com:443/rtmp/", "chave": "FB-123-abc"}, 0)
    assert sep[-1] == "rtmps://live-api-s.facebook.com:443/rtmp/FB-123-abc"
    assert "FB-123-abc" not in a.comando_para_log(sep)
    # recodificando vídeo e escolhendo o programa (serviço) num MPTS
    cmd2 = a.montar_saida({**s, "video": "h264", "video_kbps": 3000, "programa": "257"}, 0)
    assert "libx264" in cmd2 and "3000k" in cmd2 and "0:p:257:v:0" in cmd2


@pytest.mark.parametrize("cfg,erro", [
    ({"tipo": "rtmp", "url": "http://x/y"}, "URL completa"),
    ({"tipo": "rtmp", "url": "rtmp://servidor"}, "URL completa"),
    ({"tipo": "rtmp", "url": "rtmp://s/app/k", "video": "hevc"}, "copiar"),
    ({"tipo": "rtmp", "url": "rtmp://s/app/k", "programa": "abc"}, "número do serviço"),
    ({"tipo": "rtmp", "url": "rtmp://s/app/k", "servicos": ["257", "258"]}, "um serviço só"),
])
def test_saida_rtmp_validacao(cfg, erro):
    with pytest.raises(ValueError, match=erro):
        a.montar_comando({"entrada": {"tipo": "udp", "endereco": "1234"}, "saidas": [cfg]})


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="precisa do ffmpeg")
def test_rtmp_ponta_a_ponta_com_audio_latm():
    """TS no padrão ISDB-Tb (H.264 + AAC LATM) -> saída RTMP do asi2srt -> servidor RTMP local."""
    d = tempfile.mkdtemp()
    ts = os.path.join(d, "entrada.ts")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", "15",
                    "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1M", "-g", "30",
                    "-c:a", "aac", "-mpegts_flags", "latm", "-f", "mpegts", ts], check=True)
    tsp = os.path.join(d, "tsp")
    with open(tsp, "w") as f:
        f.write("#!/usr/bin/env python3\n"
                "import sys,re,socket,time\n"
                "p=(re.findall(r'-O ip 127\\.0\\.0\\.1:(\\d+)',' '.join(sys.argv)) or [None])[-1]\n"
                "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);d=open(%r,'rb').read();t0=time.time();n=0\n"
                "while True:\n"
                "  for i in range(0,len(d)-1316,1316):\n"
                "    s.sendto(d[i:i+1316],('127.0.0.1',int(p)));n+=1316\n"
                "    w=n/150000-(time.time()-t0)\n"
                "    if w>0: time.sleep(w)\n" % ts)
    os.chmod(tsp, 0o755)
    porta = _porta_livre()
    flv = os.path.join(d, "recebido.flv")
    srv = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-listen", "1", "-i",
                            f"rtmp://127.0.0.1:{porta}/live/chave", "-c", "copy", "-f", "flv", "-y", flv])
    time.sleep(1.5)
    c = a.Canal({"nome": "rtmp", "entrada": a.entrada_de_texto("udp://239.1.1.1:1234"),
                 "saidas": [a.saida_de_url(f"rtmp://127.0.0.1:{porta}/live/chave")]},
                {"tsp": tsp, "reinicio_min_s": 1})
    c.start()
    try:
        time.sleep(9)
        st = c.status()["saidas"][0]
        assert st["estado"] == "rodando" and st["info"]["taxa_kbps"] > 0
    finally:
        c.encerrar()
        time.sleep(1)
        srv.terminate()
        srv.wait(10)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                           "-of", "csv=p=0", flv], capture_output=True, text=True).stdout.split()
    assert info == ["h264", "aac"]



# ---------------- entrada 'ip' do TSDuck: unicast x multicast ----------------
@pytest.mark.parametrize("endereco,interface,esperado", [
    ("239.1.1.1:1234", "", ["239.1.1.1:1234"]),
    ("239.1.1.1:1234", "10.0.0.5", ["239.1.1.1:1234", "--local-address", "10.0.0.5"]),
    ("1234", "", ["1234"]),
    (":1234", "", ["1234"]),
    ("0.0.0.0:1234", "", ["1234"]),
    ("192.168.0.10:1234", "", ["1234", "--local-address", "192.168.0.10"]),   # unicast: só porta + IP local
    ("127.0.0.1:5000", "", ["5000", "--local-address", "127.0.0.1"]),
])
def test_args_ip_entrada(endereco, interface, esperado):
    assert a._args_ip_entrada(endereco, interface) == esperado


def test_entrada_udp_unicast_aceita_pelo_tsduck():
    cmd = a.montar_comando({"entrada": a.entrada_de_texto("udp://192.168.0.10:1234"), "saidas": []})
    assert "-I ip 1234 --local-address 192.168.0.10" in " ".join(cmd)
    _verificar_opcoes(cmd)
    with pytest.raises(ValueError, match="IP numérico"):
        a.montar_comando({"entrada": {"tipo": "udp", "endereco": "servidor.local:1234"}, "saidas": []})


def test_processo_de_saida_aceito_pelo_tsduck():
    for i, s in enumerate([a.saida_de_url("srt://172.22.130.200:9090"), a.saida_de_url("udp://10.0.0.9:5000"),
                           a.saida_de_url("rtp://239.2.2.2:5004")]):
        _verificar_opcoes(a.montar_saida(s, i, porta_entrada=43000 + i))



def test_metricas_de_envio_da_saida():
    canal = a.Canal({"nome": "x", "entrada": {"tipo": "udp", "endereco": "1234"},
                     "saidas": [a.saida_de_url("udp://10.0.0.9:5000")]}, {"tsp": "tsp"})
    try:
        ps = canal.procs_saida[0]
        assert "bitrate_monitor" in ps.cmd and ps.cmd.index("bitrate_monitor") < ps.cmd.index("-O")
        ps._taxa(8000, agora=100.0)
        ps._no_ar()
        ps._taxa(8000, agora=102.0)              # 2 s a 8 Mb/s = 2 MB
        st = ps.status()
        assert st["estado"] == "rodando" and st["conexoes"] == 1
        assert st["enviado_bytes"] == 2_000_000 and st["hist"] == [8000, 8000]
        ps._fora_do_ar("reiniciando")
        ps._no_ar()
        assert ps.status()["conexoes"] == 2 and ps.status()["enviado_conexao_bytes"] == 0
    finally:
        canal.analisador.fechar()


def test_saidas_publicas_sem_segredos():
    s = a.saidas_publicas([{"tipo": "rtmp", "url": "rtmp://s/live/chave123", "chave": "xyz"},
                           {"tipo": "srt", "modo": "caller", "endereco": "h:1", "senha": "segredo1234"}])
    assert "chave" not in s[0] and "chave123" not in s[0]["url"] and "senha" not in s[1]



def test_procurar_sdk_dektec_nomes_alternativos():
    d = tempfile.mkdtemp()
    for n in ("DTAPI-TS_v1.12.1.tar.gz", "DtapiArm_2026.09.tar.gz", "Dtapi_v2026.09.0.tar.gz"):
        open(os.path.join(d, n), "w").close()
    assert a.procurar_sdk_dektec([d]).endswith("Dtapi_v2026.09.0.tar.gz")
    open(os.path.join(d, "LinuxSDK_v2026.09.0.tar.gz"), "w").close()
    assert a.procurar_sdk_dektec([d]).endswith("LinuxSDK_v2026.09.0.tar.gz")   # nome oficial tem preferência


# ---------------- aparência: nome e logo ----------------
def test_aparencia_nome_e_logo(gateway):
    base, cfg = gateway
    cod, ap = _req(base, "GET", "/api/aparencia")
    assert cod == 200 and ap["titulo"] == "Engenharia TV Cultura" and ap["logo"] is False
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    cod, ap = _req(base, "PUT", "/api/aparencia", {"titulo": "Engenharia TV Cultura - Sumaré",
                                                  "logo_base64": base64.b64encode(png).decode(), "logo_tipo": "image/png"})
    assert cod == 200 and ap["logo"] is True and ap["titulo"].endswith("Sumaré")
    r = urllib.request.Request(base + "/logo", headers={
        "Authorization": "Basic " + base64.b64encode(b"admin:segredo").decode()})
    with urllib.request.urlopen(r) as resp:
        assert resp.read() == png and resp.headers["Content-Type"] == "image/png"
        assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    with urllib.request.urlopen(urllib.request.Request(base + "/", headers=r.headers)) as resp:
        assert "<title>Engenharia TV Cultura - Sumaré</title>" in resp.read().decode()
    salvo = json.load(open(cfg))["supervisor"]
    assert salvo["titulo"].endswith("Sumaré") and salvo["logo"] == "logo-painel.png"
    assert os.path.exists(os.path.join(os.path.dirname(cfg), "logo-painel.png"))
    assert _req(base, "PUT", "/api/aparencia", {"logo_base64": "AAAA", "logo_tipo": "text/html"})[0] == 400
    assert _req(base, "PUT", "/api/aparencia", {"titulo": ""})[0] == 400
    cod, ap = _req(base, "PUT", "/api/aparencia", {"remover_logo": True})
    assert ap["logo"] is False and not os.path.exists(os.path.join(os.path.dirname(cfg), "logo-painel.png"))
    assert _req(base, "GET", "/logo")[0] == 404
    assert json.load(open(cfg))["supervisor"]["logo"] == "-"


def test_logo_padrao_junto_da_configuracao():
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "config.json")
    open(os.path.join(d, "logo-painel.png"), "wb").write(b"\x89PNG")
    g = a.Gerenciador({"canais": [], "supervisor": {}}, cfg)
    assert g.logo_arquivo().endswith("logo-painel.png") and g.aparencia()["logo"] is True
    g.sup["logo"] = "-"
    assert g.logo_arquivo() is None



# ---------------- seleção de serviços por saída ----------------
def test_saida_com_servicos_usa_zap():
    s = a.saida_de_url("srt://172.22.130.200:9090?servico=257,0x103")
    assert s["servicos"] == ["257", "0x103"]
    cmd = a.montar_saida(s, 0, porta_entrada=42000)
    i = cmd.index("zap")
    assert cmd[i:i + 5] == ["zap", "--ignore-absent", "--eit", "257", "259"]
    assert i < cmd.index("bitrate_monitor") < cmd.index("-O")
    todos = a.montar_saida(a.saida_de_url("udp://239.1.1.1:1234"), 0)
    assert "zap" not in todos                                   # sem escolha = TS completo
    por_nome = a.montar_saida({"tipo": "udp", "endereco": "239.1.1.1:1", "servicos": ["REDE ALESP HD"]}, 0)
    assert por_nome[por_nome.index("zap") + 3] == "REDE ALESP HD"
    with pytest.raises(ValueError, match="serviço inválido"):
        a.montar_saida({"tipo": "udp", "endereco": "239.1.1.1:1", "servicos": ["; rm -rf /"]}, 0)


def test_rtmp_com_servico_e_url_limpa():
    s = a.saida_de_url("rtmp://servidor/live/chave?servico=258")
    assert s["url"] == "rtmp://servidor/live/chave" and s["servicos"] == ["258"]
    cmd = a.montar_saida(s, 0)
    assert "0:p:258:v:0" in cmd and cmd[-1] == "rtmp://servidor/live/chave"


def test_opcoes_zap_existem_no_tsduck():
    OPCOES_TSDUCK["zap"] = {"--ignore-absent", "--eit"}
    _verificar_opcoes(a.montar_saida({"tipo": "srt", "modo": "caller", "endereco": "h:1", "servicos": ["257"]}, 0,
                                     porta_entrada=42000))


def test_canal_so_analise_alimenta_distribuidor():
    cmd = a.montar_comando({"entrada": a.entrada_de_texto("srt://h:6001"), "saidas": []}, porta_dist=41000)
    assert cmd[-4:] == ["ip", "127.0.0.1:41000", "--packet-burst", str(a.LOTE_INTERNO)] and "filter" not in cmd


# ---------------- janela de imagem (miniatura por serviço) ----------------
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="precisa do ffmpeg")
def test_previa_gera_miniatura_por_servico():
    import socket
    d = tempfile.mkdtemp()
    ts = os.path.join(d, "mpts.ts")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "smptehdbars=size=640x360:rate=25", "-t", "10",
                    "-map", "0:v", "-map", "1:v", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
                    "-program", "program_num=257:st=0", "-program", "program_num=258:st=1",
                    "-f", "mpegts", ts], check=True)

    class AnalisadorFalso:
        def servicos_resumo(self):
            return [{"numero": 257, "nome": "TV1", "video": True}, {"numero": 258, "nome": "TV2", "video": True},
                    {"numero": 300, "nome": "Rádio", "video": False}]

    class CanalFalso:
        name = "teste"
        analisador = AnalisadorFalso()

    pv = a.Previa(CanalFalso())
    assert pv.imagem(257) is None
    pv.pedir()
    pv.start()
    parar = threading.Event()

    def enviar():
        dados = open(ts, "rb").read()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0, n = time.time(), 0
        while not parar.is_set():
            for i in range(0, len(dados) - 1316, 1316):
                if parar.is_set():
                    break
                s.sendto(dados[i:i + 1316], ("127.0.0.1", pv.porta))
                n += 1316
                w = n / 400000 - (time.time() - t0)
                if w > 0:
                    time.sleep(w)

    th = threading.Thread(target=enviar, daemon=True)
    th.start()
    try:
        for _ in range(40):
            time.sleep(0.5)
            if pv.imagem(257) and pv.imagem(258):
                break
        img = pv.imagem(257)
        assert img and img[:2] == b"\xff\xd8" and pv.imagem(258)
        assert pv.servs == (257, 258)                         # rádio (sem vídeo) fica de fora
        cmd = " ".join(pv.comando(pv.servs))
        assert "-skip_frame nokey" in cmd                      # decodifica só quadros-chave
    finally:
        parar.set()
        pv.encerrar()
        pv.join(10)



# ---------------- placa de rede de entrada e saída ----------------
@pytest.fixture
def placas(monkeypatch):
    lista = [{"nome": "enp1s0", "ips": ["10.10.0.5"], "estado": "up", "mac": "", "velocidade_mbps": 1000, "virtual": False},
             {"nome": "enp2s0", "ips": ["172.20.15.119"], "estado": "up", "mac": "", "velocidade_mbps": 1000, "virtual": False},
             {"nome": "enp3s0", "ips": [], "estado": "down", "mac": "", "velocidade_mbps": None, "virtual": False}]
    monkeypatch.setattr(a, "listar_interfaces", lambda todas=False: lista)
    return lista


def test_interface_nas_entradas(placas):
    udp = a.montar_comando({"entrada": {"tipo": "udp", "endereco": "239.1.1.1:1234", "interface": "enp1s0"}, "saidas": []})
    assert "-I ip 239.1.1.1:1234 --local-address 10.10.0.5" in " ".join(udp)
    srt = a.montar_comando({"entrada": a.entrada_de_texto("srt://187.60.226.82:6001?interface=enp2s0"), "saidas": []})
    assert "--caller 187.60.226.82:6001" in " ".join(srt) and "--local-interface 172.20.15.119" in " ".join(srt)
    lst = a.montar_comando({"entrada": {"tipo": "srt", "modo": "listener", "endereco": "9000", "interface": "enp1s0"},
                            "saidas": []})
    assert "--listener 10.10.0.5:9000" in " ".join(lst) and "--local-interface" not in lst
    for cmd in (udp, srt, lst):
        _verificar_opcoes(cmd)


def test_interface_nas_saidas(placas):
    for i, (s, esperado) in enumerate([
            ({"tipo": "udp", "endereco": "239.1.1.1:1234", "interface": "enp2s0"}, "--local-address 172.20.15.119"),
            ({"tipo": "rtp", "endereco": "239.2.2.2:5004", "interface": "enp1s0"}, "--local-address 10.10.0.5 --rtp"),
            ({"tipo": "srt", "modo": "caller", "endereco": "h:9090", "interface": "enp1s0"}, "--local-interface 10.10.0.5")]):
        cmd = a.montar_saida(s, i, porta_entrada=42000)
        assert esperado in " ".join(cmd)
        _verificar_opcoes(cmd)


def test_interface_invalida_ou_sem_ip(placas):
    with pytest.raises(ValueError, match="não tem endereço IPv4"):
        a.montar_saida({"tipo": "udp", "endereco": "239.1.1.1:1", "interface": "enp3s0"}, 0)
    with pytest.raises(ValueError, match="não encontrada"):
        a.montar_saida({"tipo": "udp", "endereco": "239.1.1.1:1", "interface": "wlan9"}, 0)
    with pytest.raises(ValueError, match="inválido"):
        a.montar_saida({"tipo": "udp", "endereco": "239.1.1.1:1", "interface": "eth0; rm"}, 0)


def test_listar_interfaces_real():
    lista = a.listar_interfaces()
    assert all({"nome", "ips", "estado", "virtual"} <= set(i) for i in lista)
    assert all(i["nome"] != "lo" for i in lista)


def test_api_interfaces(gateway):
    base, _ = gateway
    cod, lista = _req(base, "GET", "/api/interfaces")
    assert cod == 200 and isinstance(lista, list)


# ---------------- relatório em HTML e TXT ----------------
def _dados_falsos():
    return {"salvo": True, "periodo": {"inicio": 1793300000, "fim": 1793303600, "duracao_s": 3600, "motivo": "parado"},
            "taxa_sessao": {"media": 4_900_000, "min": 4_700_000, "max": 5_100_000},
            "resumo": {"tsid": 1105, "rede": "Rede <Teste>", "n_pids": 3, "pacotes": 123456, "taxa_util": 0, "taxa_total": 0,
                       "modo_taxa": {"modo": "CBR", "variacao_pct": 0.3}},
            "tr101290": {"contadores": {**{k: 0 for k in a.TIPOS_TR101290}, "cc": 7, "pcr_rep": 120},
                         "ultimo_em": {"cc": 1793301000}, "nao_aplicavel": ["nit", "cat"]},
            "servicos": [{"numero": 257, "nome": "REDE ALESP HD", "provedor": "ALESP", "tipo": "TV HD H.264", "taxa": 4_800_000,
                          "pmt": "0x0100", "pcr_pid": "0x0101", "pcr": {"intervalo_ms": 38, "jitter_ns": 120},
                          "modo": {"modo": "VBR"},
                          "componentes": [{"hex": "0x0101", "desc": "H.264/AVC vídeo", "stream_type": "0x1B", "taxa": 4_500_000}]}],
            "pids": [{"pid": 257, "hex": "0x0101", "desc": "H.264/AVC vídeo", "servico": "REDE ALESP HD", "tipo": "video",
                      "taxa": 4_500_000, "pct": 91.0, "pacotes": 100000, "cc": 7}],
            "srt": {"SRTIN": {"rotulo": "Entrada", "modo": "caller", "endereco": "187.60.226.82:6001", "conexoes": 1,
                              "rtt_medio_ms": 12.1, "rtt_max_ms": 30, "totais": {"pacotes": 1, "perdidos": 2, "retransmitidos": 2, "descartados": 0}}},
            "saidas": [{"enviado_bytes": 2_000_000_000, "tempo_no_ar_s": 3500, "conexoes": 1, "reinicios": 0}],
            "saidas_cfg": a.saidas_publicas([{"tipo": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2", "chave": "abc-def",
                                              "servicos": ["257"]}])}


def test_relatorio_txt_e_html():
    canal = {"nome": "Canal <A>", "entrada": {"tipo": "srt", "modo": "caller", "endereco": "187.60.226.82:6001",
                                            "senha": "segredo123456", "interface": "enp1s0"}}
    d = a.dados_relatorio(_dados_falsos(), canal, "Engenharia TV Cultura")
    txt = a.relatorio_txt(d)
    assert "Engenharia TV Cultura" in txt and "Canal <A>" in txt and "1h00min00s" in txt
    assert "Média 4.90 Mb/s" in txt and "CBR (variação 0.3%)" in txt
    assert "1.4   Continuity count error" in txt and "2.3a  PCR repetition error" in txt
    assert "n/a" in txt and "REDE ALESP HD" in txt and "segredo123456" not in txt and "(placa enp1s0)" in txt
    h = a.relatorio_html(d, ("image/png", b"\x89PNG"))
    assert h.startswith("<!doctype html>") and "data:image/png;base64," in h
    assert "Canal &lt;A&gt;" in h and "Rede &lt;Teste&gt;" in h and "<A>" not in h       # tudo escapado
    assert "ocorrência(s)" not in h and "segredo123456" not in h
    assert "RTMP rtmp://a.rtmp.youtube.com/live2/******** — serviços 257" in h and "abc-def" not in h


def test_relatorio_pdf():
    pytest.importorskip("reportlab")
    canal = {"nome": "Canal <A> & ç", "entrada": {"tipo": "srt", "modo": "caller", "endereco": "187.60.226.82:6001",
                                                 "senha": "segredo123456"}}
    d = a.dados_relatorio(_dados_falsos(), canal, "Engenharia TV Cultura")
    logo = open(os.path.join(os.path.dirname(__file__), "..", "marca", os.listdir(os.path.join(
        os.path.dirname(__file__), "..", "marca"))[0]), "rb").read() if os.path.isdir(os.path.join(
        os.path.dirname(__file__), "..", "marca")) else None
    pdf = a.relatorio_pdf(d, ("image/png", logo) if logo else None)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert b"segredo123456" not in pdf
    a.relatorio_pdf(d, ("image/png", b"lixo"))     # logo inválido não impede o PDF


def test_api_baixar_relatorio_formatos(gateway):
    base, _ = gateway
    canal = {"nome": "R", "entrada": {"tipo": "udp", "endereco": "1234"}, "saidas": []}
    cid = _req(base, "POST", "/api/canais", canal)[1]["id"]
    time.sleep(2.5)
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:segredo").decode()}
    for fmt, tipo, ext in (("html", "text/html", ".html"), ("txt", "text/plain", ".txt"), ("json", "application/json", ".json"),
                           ("pdf", "application/pdf", ".pdf")):
        r = urllib.request.Request(f"{base}/api/canais/{cid}/analise?baixar=1&formato={fmt}", headers=hdr)
        with urllib.request.urlopen(r) as resp:
            assert resp.headers["Content-Type"].startswith(tipo)
            assert resp.headers["Content-Disposition"].endswith(ext + '"')
            assert len(resp.read()) > 200


# ---------------- robustez (2.17) ----------------
def test_fork_do_analisador_nao_derruba_o_canal():
    canal = {"entrada": a.entrada_de_texto("udp://239.1.1.1:1234"), "saidas": [a.saida_de_url("udp://1.2.3.4:5")]}
    cmd = a.montar_comando(canal, porta_analise=40000, porta_dist=40001)
    i = cmd.index("fork")
    assert "--ignore-abort" in cmd[i:i + 5]
    j = cmd.index("ip")
    assert cmd[cmd.index("--buffer-size", j) + 1] == str(a.BUFFER_UDP)
    _verificar_opcoes(cmd)
    _verificar_opcoes(a.montar_saida(canal["saidas"][0], 0, porta_entrada=40002))


def test_udp_descartes_ve_perda_no_kernel():
    import socket
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    r.bind(("127.0.0.1", 0))
    porta = r.getsockname()[1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(400):
        s.sendto(b"\x47" * 1316, ("127.0.0.1", porta))
    time.sleep(0.2)
    assert a.udp_descartes().get(porta, 0) > 0
    r.close(); s.close()


def _alimentar(an, taxas_bps):
    """Simula segundos com a taxa útil dada (pacotes com PID 0x100)."""
    pkt = bytes([0x47, 0x01, 0x00, 0x10]) + bytes(184)
    ag = 100.0
    an.verificar(ag)
    for bps in taxas_bps:
        n = int(bps / 1504)
        for k in range(0, n, 7):
            an.processar(pkt * min(7, n - k), ag + 0.05 + 0.9 * k / max(n, 1))
        ag += 1.0
        an.verificar(ag)


def test_rajada_nao_vira_bitrate_maximo():
    an = a.AnalisadorTS()
    seq = [5e6] * 20 + [0.2e6, 40e6] + [5e6] * 20     # rede segurou e soltou de uma vez
    _alimentar(an, seq)
    ts = an.taxa_sessao()
    assert 4.5e6 < ts["media"] < 6.5e6
    assert ts["max"] < 15e6, ts            # sem o filtro daria ~40 Mb/s
    assert ts["min"] > 1e6, ts
    assert ts["rajadas"] >= 1


def test_analisador_sobrevive_a_excecao(monkeypatch):
    import socket
    an = a.AnalisadorTS()
    porta = an.abrir()
    original = an.processar
    chamadas = {"n": 0}

    def falha_uma_vez(d, agora):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise IndexError("seção malformada")
        return original(d, agora)
    an.processar = falha_uma_vez
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = bytes([0x47, 0x01, 0x00, 0x10]) + bytes(184)
    for _ in range(50):
        s.sendto(pkt * 7, ("127.0.0.1", porta))
        time.sleep(0.005)
    time.sleep(0.3)
    assert chamadas["n"] > 1 and an.pacotes > 0     # a thread continuou depois do erro
    an.fechar(); s.close()


TSP_PARA_DE_ENVIAR = """#!/usr/bin/env python3
import sys, re, socket, time
arg = " ".join(sys.argv)
if "--local-address 127.0.0.1" in arg:      # processo de saída: só fica vivo
    while True: time.sleep(1)
portas = [int(p) for p in re.findall(r"127\\.0\\.0\\.1:(\\d+)", arg)]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
pkt = bytes([0x47, 0x00, 0x00, 0x10]) + bytes(184)
t0 = time.time()
while time.time() - t0 < 2:                  # envia 2 s e depois fica mudo, mas vivo (como um SRT sem dados)
    for p in portas: s.sendto(pkt * 7, ("127.0.0.1", p))
    time.sleep(0.01)
print("* bitrate_monitor: TS bitrate: 1,000,000 bits/s", flush=True)
while True: time.sleep(1)
"""


def test_vigia_reinicia_entrada_muda():
    d = tempfile.mkdtemp()
    tsp = os.path.join(d, "tsp")
    with open(tsp, "w") as f:
        f.write(TSP_PARA_DE_ENVIAR)
    os.chmod(tsp, 0o755)
    canal = {"nome": "mudo", "entrada": a.entrada_de_texto("udp://239.1.1.1:1234"), "saidas": [],
             "processamento": {"reiniciar_sem_sinal_s": 3}}
    c = a.Canal(canal, {"tsp": tsp, "reinicio_min_s": 1, "reinicio_max_s": 1})
    c.start()
    try:
        fim = time.time() + 20
        while time.time() < fim and c.reinicios_vigia < 1:
            time.sleep(0.5)
        assert c.reinicios_vigia >= 1
        time.sleep(2.5)
        assert c.status()["reinicios"] >= 1
        assert any("sem sinal" in x for x in c.log)
    finally:
        c.encerrar()


def test_unidade_systemd_sem_tempo_real():
    u = a.UNIDADE_SYSTEMD
    assert "CPUSchedulingPolicy=fifo" not in u and "StartLimitIntervalSec=0" in u and "Restart=always" in u
    assert "net.core.rmem_max" in a.SYSCTL_ASI2SRT


# ---------------- tela de login, usuários e perfis (2.18) ----------------
def _http(base, metodo, caminho, corpo=None, cookie=None, csrf=True):
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-Asi2srt"] = "1"
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(base + caminho, method=metodo, headers=h,
                               data=json.dumps(corpo).encode() if corpo is not None else None)

    class SemRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(SemRedir)
    try:
        with op.open(r) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _login(base, u, s):
    cod, hd, corpo = _http(base, "POST", "/api/login", {"usuario": u, "senha": s})
    ck = hd.get("Set-Cookie", "") or ""
    return cod, ck.split(";")[0], json.loads(corpo or b"{}")


def test_login_sessao_e_perfis(gateway):
    base, cfg = gateway
    cod, hd, _ = _http(base, "GET", "/analise/x")
    assert cod == 302 and hd["Location"].startswith("/login?volta=")
    cod, _, pag = _http(base, "GET", "/login")
    assert cod == 200 and b"Entrar" in pag and b"Engenharia TV Cultura" in pag
    assert _login(base, "admin", "errada")[0] == 401
    cod, ck, eu = _login(base, "admin", "segredo")
    assert cod == 200 and ck.startswith("asi2srt_sessao=") and eu["papel"] == "admin"
    assert _http(base, "GET", "/", cookie=ck)[0] == 200
    assert json.loads(_http(base, "GET", "/api/eu", cookie=ck)[2])["usuario"] == "admin"
    # senha nunca em texto no arquivo
    salvo = json.load(open(cfg))["supervisor"]
    assert "senha_painel" not in salvo and salvo["usuarios"][0]["senha"].startswith("pbkdf2_sha256$")
    assert oct(os.stat(cfg).st_mode & 0o777) == "0o600"
    # operador
    cod, _, _ = _http(base, "POST", "/api/usuarios", {"usuario": "op1", "nome": "Operador 1", "papel": "operador",
                                                     "senha": "senha-op1"}, cookie=ck)
    assert cod == 201
    assert _http(base, "POST", "/api/usuarios", {"usuario": "op2", "papel": "operador", "senha": "123"}, cookie=ck)[0] == 400
    canal = {"nome": "C", "entrada": {"tipo": "udp", "endereco": "1234"}, "saidas": []}
    cid = json.loads(_http(base, "POST", "/api/canais", canal, cookie=ck)[2])["id"]
    _, cko, eu = _login(base, "op1", "senha-op1")
    assert eu["papel"] == "operador"
    assert _http(base, "GET", "/api/canais", cookie=cko)[0] == 200
    assert _http(base, "POST", f"/api/canais/{cid}/parar", cookie=cko)[0] == 200       # operar pode
    assert _http(base, "POST", "/api/canais", canal, cookie=cko)[0] == 403             # criar não
    assert _http(base, "PUT", f"/api/canais/{cid}", canal, cookie=cko)[0] == 403
    assert _http(base, "DELETE", f"/api/canais/{cid}", cookie=cko)[0] == 403
    assert _http(base, "PUT", "/api/aparencia", {"titulo": "x"}, cookie=cko)[0] == 403
    assert _http(base, "GET", "/api/usuarios", cookie=cko)[0] == 403
    # trocar a própria senha encerra as outras sessões dele
    _, cko2, _ = _login(base, "op1", "senha-op1")
    assert _http(base, "PUT", "/api/eu/senha", {"atual": "errada", "nova": "nova-senha"}, cookie=cko)[0] == 400
    assert _http(base, "PUT", "/api/eu/senha", {"atual": "senha-op1", "nova": "nova-senha"}, cookie=cko)[0] == 200
    assert _http(base, "GET", "/api/canais", cookie=cko)[0] == 200 and _http(base, "GET", "/api/canais", cookie=cko2)[0] == 401
    # regras de administrador
    assert _http(base, "DELETE", "/api/usuarios/admin", cookie=ck)[0] == 400          # não exclui a si mesmo
    assert _http(base, "PUT", "/api/usuarios/admin", {"papel": "operador"}, cookie=ck)[0] == 400   # último admin
    assert _http(base, "DELETE", "/api/usuarios/op1", cookie=ck)[0] == 200
    assert _http(base, "GET", "/api/canais", cookie=cko)[0] == 401                     # excluído: sessão cai
    # sair
    assert _http(base, "POST", "/api/logout", cookie=ck)[0] == 200
    assert _http(base, "GET", "/api/canais", cookie=ck)[0] == 401
    assert _http(base, "POST", "/api/login", {"usuario": "admin", "senha": "segredo"}, csrf=False)[0] == 403


def test_login_bloqueia_apos_tentativas(gateway):
    base, _ = gateway
    cods = [_login(base, "admin", f"errada{i}")[0] for i in range(6)]
    assert cods[:4] == [401] * 4 and cods[4] == 429 and cods[5] == 429
    assert _login(base, "admin", "segredo")[0] == 429        # nem a senha certa entra durante o bloqueio


def test_hash_de_senha():
    h = a.hash_senha("abc123")
    assert h.startswith("pbkdf2_sha256$") and "abc123" not in h
    assert a.conferir_senha("abc123", h) and not a.conferir_senha("abc124", h) and not a.conferir_senha("x", "lixo")


# ---------------- placa de rede reserva (2.20) ----------------
def _nics(monkeypatch, estado):
    """estado: {nome: (ligada, ip)}"""
    monkeypatch.setattr(a, "estado_nics", lambda cache_s=1.5: {n: {"ligada": l, "ip": ip, "estado": "up" if l else "down"}
                                                               for n, (l, ip) in estado.items()})
    monkeypatch.setattr(a, "ip_da_interface", lambda n: estado[n][1] or (_ for _ in ()).throw(ValueError("sem IP")))


def test_failover_nic_link_e_volta(monkeypatch):
    est = {"eth0": (True, "10.0.0.5"), "eth1": (True, "192.168.1.5")}
    _nics(monkeypatch, est)
    fo = a.FailoverNic({"interface": "eth0", "interface_reserva": "eth1"}, "Saída 1", estavel_s=30, sem_sinal_s=15)
    assert fo.ativa == "eth0" and fo.avaliar(100) is None
    est["eth0"] = (False, "10.0.0.5")                       # cabo da principal desconectado
    para, motivo = fo.avaliar(101)
    assert para == "eth1" and "sem link" in motivo
    fo.trocar(para, motivo, 101)
    assert fo.aplicar({"interface": "eth0", "interface_local": "1.1.1.1"})["interface"] == "eth1"
    est["eth0"] = (True, "10.0.0.5")                        # voltou: só retorna depois de 30 s estável
    assert fo.avaliar(110) is None and fo.avaliar(130) is None
    para, motivo = fo.avaliar(141)
    assert para == "eth0" and "estável" in motivo
    fo.trocar(para, motivo, 141)
    st = fo.status()
    assert st["ativa"] == "eth0" and not st["em_reserva"] and st["trocas"] == 2


def test_failover_nic_sem_conexao_nao_volta_sozinho(monkeypatch):
    _nics(monkeypatch, {"eth0": (True, "10.0.0.5"), "eth1": (True, "192.168.1.5")})
    fo = a.FailoverNic({"interface": "eth0", "interface_reserva": "eth1"}, "Entrada", estavel_s=5, sem_sinal_s=15)
    t0 = fo.desde
    assert fo.avaliar(t0 + 10, sem_sinal_ha=10) is None          # ainda dentro do prazo
    para, motivo = fo.avaliar(t0 + 20, sem_sinal_ha=16)
    assert para == "eth1" and "sem conexão" in motivo
    fo.trocar(para, motivo, t0 + 20)
    assert fo.avaliar(t0 + 100) is None                          # funcionando pela reserva: fica nela
    # se a reserva também não conectar, tenta a principal de novo, com espera maior (2x)
    assert fo.avaliar(t0 + 45, sem_sinal_ha=20) is None
    assert fo.avaliar(t0 + 60, sem_sinal_ha=31)[0] == "eth0"


def test_failover_validacao():
    base = {"tipo": "udp", "endereco": "239.1.1.1:1234"}
    for item, erro in (({**base, "interface_reserva": "eth1"}, "principal"),
                       ({**base, "interface": "eth1", "interface_reserva": "eth1"}, "diferente")):
        with pytest.raises(ValueError, match=erro):
            a.montar_comando({"entrada": item, "saidas": []})
        with pytest.raises(ValueError, match=erro):
            a.montar_saida(item, 0, porta_entrada=1)


TSP_REGISTRA = """#!/usr/bin/env python3
import sys, re, socket, time, os
arg = " ".join(sys.argv)
with open(os.environ["REG_TSP"], "a") as f:
    f.write(arg + "\\n")
if "--local-address 127.0.0.1" in arg:
    while True: time.sleep(1)
portas = [int(p) for p in re.findall(r"127\\\\.0\\\\.0\\\\.1:(\\\\d+)", arg)]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
pkt = bytes([0x47, 0x00, 0x00, 0x10]) + bytes(184)
while True:
    for p in portas: s.sendto(pkt * 7, ("127.0.0.1", p))
    time.sleep(0.02)
"""


def test_canal_troca_placa_da_entrada_quando_cai(monkeypatch):
    est = {"eth0": (True, "10.0.0.5"), "eth1": (True, "192.168.1.5")}
    _nics(monkeypatch, est)
    d = tempfile.mkdtemp()
    tsp, reg = os.path.join(d, "tsp"), os.path.join(d, "reg.txt")
    with open(tsp, "w") as f:
        f.write(TSP_REGISTRA)
    os.chmod(tsp, 0o755)
    monkeypatch.setenv("REG_TSP", reg)
    canal = {"nome": "fo", "entrada": {"tipo": "udp", "endereco": "239.1.1.1:1234", "interface": "eth0",
                                       "interface_reserva": "eth1"}, "saidas": []}
    c = a.Canal(canal, {"tsp": tsp, "reinicio_min_s": 1, "reinicio_max_s": 1})
    c.start()
    try:
        time.sleep(3)
        assert "--local-address 10.0.0.5" in open(reg).read()
        est["eth0"] = (False, "10.0.0.5")
        fim = time.time() + 12
        while time.time() < fim and "--local-address 192.168.1.5" not in open(reg).read():
            time.sleep(0.5)
        assert "--local-address 192.168.1.5" in open(reg).read()
        st = c.status()
        assert st["rede"]["entrada"]["ativa"] == "eth1" and st["rede"]["entrada"]["em_reserva"]
        assert st["reinicios"] == 0                          # troca de placa não conta como queda
        assert any("passou da placa eth0 para eth1" in x for x in c.log)
    finally:
        c.encerrar()


def test_rotas_por_placa_comandos(monkeypatch):
    chamadas = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    def falso(cmd, **k):
        chamadas.append(cmd)
        if cmd[:5] == ["ip", "-j", "-4", "route", "show"]:
            return R(json.dumps([{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0"},
                                 {"dst": "default", "gateway": "192.168.1.1", "dev": "eth1"}]))
        if cmd[:4] == ["ip", "-j", "-4", "rule"]:
            return R("[]")
        return R()
    _nics(monkeypatch, {"eth0": (True, "10.0.0.5"), "eth1": (True, "192.168.1.5")})
    monkeypatch.setattr(a.subprocess, "run", falso)
    monkeypatch.setattr(a.shutil, "which", lambda x: "/sbin/ip")
    reais = {"eth0": "2", "eth1": "3"}
    orig_open = open
    monkeypatch.setattr("builtins.open", lambda f, *x, **k: io.StringIO(reais[f.split("/")[4]]) if str(f).endswith("/ifindex")
                        else orig_open(f, *x, **k))
    feitos = a.aplicar_rotas_por_placa()
    assert "ip -4 route replace default via 192.168.1.1 dev eth1 table 203" in feitos
    assert "ip -4 rule add from 192.168.1.5/32 table 203 priority 1003" in feitos


def test_listar_interfaces_detalhes():
    lst = a.listar_interfaces(todas=True)
    for i in lst:
        assert {"nome", "ips", "link", "tipo", "modelo", "driver", "gateway", "rx_bps", "virtual"} <= set(i)
        assert i["tipo"] in ("fisica", "wifi", "bond", "vlan", "bridge", "vpn", "virtual", "interna")
    assert all(i["tipo"] != "interna" for i in a.listar_interfaces())
    assert isinstance(a.placas_sem_driver(), list)


def test_api_rede(gateway):
    base, _ = gateway
    cod, r = _req(base, "GET", "/api/rede")
    assert cod == 200 and {"interfaces", "sem_driver", "uso"} <= set(r)


def test_favicon_publico(gateway):
    base, _ = gateway
    cod, hd, corpo = _http(base, "GET", "/favicon.ico", csrf=False)
    assert cod == 200 and hd["Content-Type"].startswith("image/") and len(corpo) > 50
    cod, _, pag = _http(base, "GET", "/login")
    assert b'rel="icon"' in pag


def test_monitor_maquina():
    m = a.MonitorMaquina()
    m._amostrar()
    time.sleep(0.3)
    m._amostrar()
    s = m.status()
    assert s["cpu"]["nucleos"] >= 1 and s["cpu"]["uso"] is not None and 0 <= s["cpu"]["uso"] <= 100
    assert s["memoria"]["total"] > 0 and 0 <= s["memoria"]["pct"] <= 100
    assert s["discos"] and s["sistema"]["versao"] == a.__version__
    assert "asi2srt" in s["processos"] and len(s["historico"]) >= 1


def test_api_sistema(gateway):
    base, _ = gateway
    cod, r = _req(base, "GET", "/api/sistema")
    assert cod == 200 and {"cpu", "memoria", "discos", "temperatura", "processos", "sistema", "historico"} <= set(r)


def test_explicar_erro_tsp():
    cfg = {"entrada": {"tipo": "udp", "endereco": "226.0.0.1:1234", "interface": "enp9s0"}, "saidas": []}
    m = a.explicar_erro_tsp("* Error: ip: error receiving from UDP socket: Resource temporarily unavailable", cfg)
    assert "nenhum pacote em 5 s" in m and "enp9s0" in m
    assert "outro canal" in a.explicar_erro_tsp("error attaching ... DTAPI_E_IN_USE (DTAPI status 4104)")
    assert a.explicar_erro_tsp("qualquer coisa") == "qualquer coisa"
    assert "rp_filter = 2" in a.SYSCTL_ASI2SRT


# ---------------- 2.27: proteção contra sobrecarga em máquinas fracas ----------------
def _mem(total, livre, swap=0):
    return {"total": total, "usada": total - livre, "swap_usada": swap}


def test_protecao_liga_com_pouca_memoria_e_desliga(monkeypatch):
    a.PROTECAO.update(ativa=False, motivo="", desde=None)
    m = a.MonitorMaquina({})
    t = [1000.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: t[0])
    m._protecao(40, _mem(1.6e9, 100e6))
    assert a.PROTECAO["ativa"] and "memória" in a.PROTECAO["motivo"]
    m._protecao(40, _mem(1.6e9, 800e6))
    assert a.PROTECAO["ativa"]            # precisa ficar normal por 60 s
    t[0] += 61
    m._protecao(40, _mem(1.6e9, 800e6))
    assert not a.PROTECAO["ativa"]


def test_protecao_cpu_alta_so_depois_de_30s(monkeypatch):
    a.PROTECAO.update(ativa=False, motivo="", desde=None)
    m = a.MonitorMaquina({})
    t = [0.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: t[0])
    m._protecao(95, _mem(4e9, 2e9))
    assert not a.PROTECAO["ativa"]
    t[0] = 31
    m._protecao(95, _mem(4e9, 2e9))
    assert a.PROTECAO["ativa"] and "CPU" in a.PROTECAO["motivo"]
    a.PROTECAO.update(ativa=False, motivo="", desde=None)


def test_protecao_swap_crescendo(monkeypatch):
    a.PROTECAO.update(ativa=False, motivo="", desde=None)
    m = a.MonitorMaquina({})
    t = [0.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: t[0])
    m._protecao(30, _mem(4e9, 2e9, 50e6))
    t[0] = 30
    m._protecao(30, _mem(4e9, 2e9, 200e6))
    assert a.PROTECAO["ativa"] and "swap" in a.PROTECAO["motivo"]
    a.PROTECAO.update(ativa=False, motivo="", desde=None)


def test_protecao_desligada_na_config():
    a.PROTECAO.update(ativa=True, motivo="x", desde=1)
    m = a.MonitorMaquina({"protecao_automatica": False})
    m._protecao(99, _mem(1e9, 10e6))
    assert not a.PROTECAO["ativa"]


def test_sistema_ajustes_maquina_fraca():
    assert "vm.swappiness = 10" in a.SYSCTL_ASI2SRT
    assert "OOMScoreAdjust=-500" in a.UNIDADE_SYSTEMD
    assert callable(a.ajustar_cpu_desempenho)
