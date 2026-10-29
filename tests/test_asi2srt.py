import base64
import shutil
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
    cmd = a.montar_comando(canal, porta_dist=41000)
    linha = " ".join(cmd)
    assert "-I dektec --device 0 --channel 0" in linha
    assert "--pid 0x0012 --pid 0x1FFF" in linha and "--min 1000" in linha
    assert cmd[-4:] == ["ip", "127.0.0.1:41000", "--packet-burst", "7"]   # entrega ao distribuidor local
    assert "srt" not in cmd                                                # a saída não fica neste processo
    sai = a.montar_saida(canal["saidas"][0], 0, porta_entrada=42000)
    assert "-I ip 127.0.0.1:42000" in " ".join(sai) and "-O srt --caller h:1" in " ".join(sai)
    assert "senhasecreta1" not in a.comando_para_log(sai)


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
            "--pbkeylen", "--streamid", "--statistics-interval", "--json-line"},
    "ip": {"--packet-burst", "--ttl", "--local-address", "--receive-timeout", "--buffer-size",
           "--rtp", "--payload-type", "--ssrc-identifier"},
    "dektec": {"--device", "--channel", "--receive-timeout"},
    "file": {"--infinite"},
    "fork": {"--nowait", "--buffered-packets"},
    "drop": set(),
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
  *"-O srt"*) echo "* Error: srt: error during srt_connect: Connection setup failure: connection timed out"; exit 1;;
  *"-I ip 127.0.0.1"*) sleep 30;;
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
    assert cmd[0] == "ffmpeg" and "udp://127.0.0.1:42000" in cmd[cmd.index("-i") + 1]
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
    ({"tipo": "rtmp", "url": "rtmp://s/app/k", "programa": "abc"}, "programa"),
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
