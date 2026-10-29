"""Testes do analisador TR 101 290 P1 com pacotes TS sintéticos."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from asi2srt import AnalisadorTS, crc32_mpeg  # noqa: E402

PMT_PID, VIDEO, AUDIO = 0x100, 0x101, 0x102


def secao(tid, extensao, corpo, crc_ok=True):
    dados = struct.pack(">H", extensao) + b"\xc1\x00\x00" + corpo
    tam = len(dados) + 4
    s = bytes([tid, 0xB0 | (tam >> 8), tam & 0xFF]) + dados
    crc = crc32_mpeg(s) ^ (0 if crc_ok else 1)
    return s + struct.pack(">I", crc)


def pat(tid=0x00):
    return secao(tid, 1, struct.pack(">HH", 1, 0xE000 | PMT_PID))


def pmt(tid=0x02, pids=(VIDEO, AUDIO)):
    es = b"".join(struct.pack(">BHH", 0x1B, 0xE000 | p, 0xF000) for p in pids)
    return secao(tid, 1, struct.pack(">HH", 0xE000 | VIDEO, 0xF000) + es)


class Gerador:
    def __init__(self):
        self.cc = {}

    def pkt(self, pid, carga=b"", pusi=False, scr=0, pular_cc=False, sync=0x47):
        cc = (self.cc.get(pid, -1) + (2 if pular_cc else 1)) & 0x0F
        self.cc[pid] = cc
        h = bytes([sync, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, (scr << 6) | 0x10 | cc])
        return (h + carga).ljust(188, b"\xff")

    def psi(self, pid, sec, **kw):
        return self.pkt(pid, b"\x00" + sec, pusi=True, **kw)


def rodar(an, g, inicio, duracao, passo=0.1, sem=(), extra=None):
    """Alimenta o analisador com PAT+PMT+vídeo+áudio a cada 'passo' segundos."""
    t = inicio
    while t < inicio + duracao - 1e-9:
        pacotes = []
        if 0 not in sem:
            pacotes.append(g.psi(0, pat()))
        if PMT_PID not in sem:
            pacotes.append(g.psi(PMT_PID, pmt()))
        for pid in (VIDEO, AUDIO):
            if pid not in sem:
                pacotes.append(g.pkt(pid))
        if extra:
            pacotes += extra(t)
        an.processar(b"".join(pacotes), t)
        an.verificar(t)
        t += passo
    return t


def test_fluxo_limpo():
    an, g = AnalisadorTS(), Gerador()
    rodar(an, g, 0, 8)
    st = an.status(8)
    assert all(v == 0 for v in st["contadores"].values()), st["contadores"]
    assert st["programas"] == 1 and st["pids_referenciados"] == 2 and st["sinal"]


def test_erro_de_continuidade():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    an.processar(g.pkt(VIDEO, pular_cc=True), t)
    assert an.cont["cc"] == 1


def test_duplicado_unico_permitido():
    an, g = AnalisadorTS(), Gerador()
    p = g.pkt(VIDEO)
    an.processar(p + p, 0)          # 1 duplicado: permitido
    assert an.cont["cc"] == 0
    an.processar(p, 0.01)           # 2º duplicado: erro
    assert an.cont["cc"] == 1


def test_sync_byte_e_sync_loss():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    an.processar(g.pkt(VIDEO, sync=0x00), t)
    assert an.cont["sync_byte"] == 1 and an.cont["sync_loss"] == 0
    an.processar(g.pkt(VIDEO, sync=0x00) + g.pkt(VIDEO, sync=0x12), t)
    assert an.cont["sync_byte"] == 3 and an.cont["sync_loss"] == 1 and not an.em_sync
    an.processar(b"".join(g.pkt(AUDIO) for _ in range(5)), t + 0.01)
    assert an.em_sync


def test_perda_de_sinal_conta_sync_loss():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    an.verificar(t + 1.5)
    assert an.cont["sync_loss"] == 1
    assert an.status(t + 1.5)["sinal"] is False
    # sem sinal não gera erros de PAT/PMT/PID em cascata
    for k in range(20):
        an.verificar(t + 2 + k)
    assert an.cont["pat"] == an.cont["pmt"] == an.cont["pid"] == 0
    # retomada não gera CC falso
    rodar(an, g, t + 30, 2)
    assert an.cont["cc"] == 0


def test_pat_ausente():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    rodar(an, g, t, 2, sem=(0,))
    assert an.cont["pat"] >= 2 and an.cont["pmt"] == 0


def test_pat_table_id_errado_e_embaralhada():
    an, g = AnalisadorTS(), Gerador()
    an.processar(g.psi(0, pat(tid=0x42)), 0)
    an.processar(g.psi(0, pat(), scr=2), 0.01)
    assert an.cont["pat"] == 2


def test_pmt_ausente_e_table_id_errado():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    t = rodar(an, g, t, 2, sem=(PMT_PID,))
    assert an.cont["pmt"] >= 2
    an.processar(g.psi(PMT_PID, pmt(tid=0x03)), t)
    assert an.cont["pmt"] >= 3


def test_pid_ausente():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1)
    t = rodar(an, g, t, 7, sem=(AUDIO,))
    st = an.status(t)
    assert an.cont["pid"] >= 1 and an.cont["pat"] == an.cont["pmt"] == 0
    assert st["pids_ausentes"] == ["0x0102"]
    assert "pid" in st["recentes"]


def test_pid_nunca_enviado_tem_tolerancia_inicial():
    an, g = AnalisadorTS(), Gerador()
    rodar(an, g, 0, 4.5, sem=(AUDIO,))
    assert an.cont["pid"] == 0
    rodar(an, g, 4.5, 1.5, sem=(AUDIO,))
    assert an.cont["pid"] == 1


def test_zerar():
    an, g = AnalisadorTS(), Gerador()
    an.processar(g.pkt(VIDEO) + g.pkt(VIDEO, pular_cc=True), 0)
    assert an.cont["cc"] == 1
    an.zerar()
    assert an.cont["cc"] == 0 and an.status(0)["recentes"] == []


def test_desempenho():
    """~40 Mb/s: 26600 pacotes/s precisam ser processados com folga."""
    import time
    an, g = AnalisadorTS(), Gerador()
    bloco = b"".join(g.pkt(VIDEO + (i % 4)) for i in range(7))
    t0 = time.perf_counter()
    for i in range(3800):  # 26600 pacotes
        an.processar(bloco, i * 0.0003)
    gasto = time.perf_counter() - t0
    assert gasto < 0.5, f"lento demais: {gasto:.2f}s para 1 s de TS a 40 Mb/s"


# ---------------- Prioridade 2, Prioridade 3, PIDs e serviços ----------------
def pkt_pcr(g, pid, pcr, disc=False):
    """Pacote só com adaptation field carregando PCR (27 MHz)."""
    base, ext = divmod(pcr, 300)
    af = bytes([0x80 if disc else 0x00 | 0x10]) if False else bytes([(0x80 if disc else 0) | 0x10])
    pcrb = struct.pack(">IH", base >> 1, ((base & 1) << 15) | 0x7E00 | ext)
    af = af + pcrb
    cc = g.cc.get(pid, 0)  # sem carga útil: CC não avança
    h = bytes([0x47, pid >> 8, pid & 0xFF, 0x20 | cc])
    return (h + bytes([len(af)]) + af).ljust(188, b"\xff")


def pkt_pes(g, pid, sid=0xE0, pts=True):
    pes = b"\x00\x00\x01" + bytes([sid]) + b"\x00\x00\x80" + (b"\x80\x05" + b"\x21\x00\x01\x00\x01" if pts else b"\x00\x00")
    return g.pkt(pid, pes, pusi=True)


def sdt(nome=b"Canal Teste", prov=b"Emissora"):
    d = bytes([0x19, len(prov)]) + prov + bytes([len(nome)]) + nome
    desc = bytes([0x48, len(d)]) + d
    serv = struct.pack(">HBH", 1, 0xFC, 0x8000 | len(desc)) + desc
    return secao(0x42, 0x0001, struct.pack(">HB", 0x0001, 0xFF) + serv)


def test_crc_invalido():
    an, g = AnalisadorTS(), Gerador()
    an.processar(g.psi(0, secao(0x00, 1, struct.pack(">HH", 1, 0xE000 | PMT_PID), crc_ok=False)), 0)
    assert an.cont["crc"] == 1 and an.pmt_pids == {}


def test_transport_error_indicator():
    an, g = AnalisadorTS(), Gerador()
    p = bytearray(g.pkt(VIDEO))
    p[1] |= 0x80
    an.processar(bytes(p), 0)
    assert an.cont["transport"] == 1


def test_pcr_repeticao_descontinuidade_e_jitter():
    an, g = AnalisadorTS(), Gerador()
    clk = 27_000_000
    # PCR a cada 30 ms com 100 pacotes entre eles (CBR perfeito) -> sem erros, jitter ~0
    blocos = []
    for k in range(80):
        blocos.append(pkt_pcr(g, VIDEO, k * clk * 30 // 1000) + b"".join(g.pkt(0x1FFF) for _ in range(99)))
    for k, b in enumerate(blocos):
        an.processar(b, k * 0.03)
        an.verificar(k * 0.03)
    assert an.cont["pcr_rep"] == an.cont["pcr_disc"] == an.cont["pcr_acc"] == 0
    # intervalo de 60 ms -> repetition error
    ult = 79 * clk * 30 // 1000
    an.processar(pkt_pcr(g, VIDEO, ult + clk * 60 // 1000), 2.5)
    assert an.cont["pcr_rep"] == 1
    # salto de 500 ms sem discontinuity_indicator -> discontinuity error
    an.processar(pkt_pcr(g, VIDEO, ult + clk * 560 // 1000), 2.6)
    assert an.cont["pcr_disc"] == 1
    # com discontinuity_indicator não conta
    an.processar(pkt_pcr(g, VIDEO, 5, disc=True), 2.7)
    assert an.cont["pcr_disc"] == 1


def test_pcr_accuracy():
    an, g = AnalisadorTS(), Gerador()
    clk = 27_000_000
    for k in range(60):
        jitter = 2700 if k == 55 else 0  # 100 µs de erro num PCR
        an.processar(pkt_pcr(g, VIDEO, k * clk * 30 // 1000 + jitter)
                     + b"".join(g.pkt(0x1FFF) for _ in range(99)), k * 0.03)
        an.verificar(k * 0.03)
    assert an.cont["pcr_acc"] >= 1


def test_pts_ausente():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 1, extra=lambda t: [pkt_pes(g, VIDEO)])
    assert an.cont["pts"] == 0
    rodar(an, g, t, 1.5)  # vídeo continua chegando, mas sem PTS
    assert an.cont["pts"] >= 1 and an.cont["pid"] == 0


def test_cat_ausente_com_embaralhamento():
    an, g = AnalisadorTS(), Gerador()
    rodar(an, g, 0, 3, extra=lambda t: [g.pkt(VIDEO, scr=2)])
    assert an.cont["cat"] >= 1


def test_si_e_servicos():
    an, g = AnalisadorTS(), Gerador()
    t = rodar(an, g, 0, 2, extra=lambda t: [g.psi(0x11, sdt())])
    a = an.analise(t)
    serv = a["servicos"][0]
    assert serv["nome"] == "Canal Teste" and serv["provedor"] == "Emissora" and serv["tipo"] == "TV HD H.264"
    assert [c["desc"] for c in serv["componentes"]] == ["H.264/AVC vídeo", "H.264/AVC vídeo"]
    assert "sdt" not in a["tr101290"]["nao_aplicavel"]
    assert "nit" in a["tr101290"]["nao_aplicavel"]  # nunca vista: não se aplica
    # SDT some por mais de 2 s -> erro
    rodar(an, g, t, 3)
    assert an.cont["sdt"] >= 1


def test_table_id_errado_em_pid_si():
    an, g = AnalisadorTS(), Gerador()
    an.processar(g.psi(0x11, secao(0x99, 1, b"\x00\x01\xff")), 0)
    an.processar(g.psi(0x14, secao(0x99, 1, b"\x00\x01\xff")), 0)
    assert an.cont["sdt"] == 1 and an.cont["tdt"] == 1


def test_pid_nao_referenciado_e_taxas():
    an, g = AnalisadorTS(), Gerador()
    rodar(an, g, 0, 4, extra=lambda t: [g.pkt(0x0777)])
    assert an.cont["unref"] == 1  # conta uma vez por PID
    a = an.analise(4)
    pids = {p["pid"]: p for p in a["pids"]}
    assert pids[0x0777]["desc"] == "Não referenciado"
    assert pids[PMT_PID]["desc"] == "PMT (programa 1)"
    assert pids[VIDEO]["taxa"] > 0 and abs(sum(p["pct"] for p in a["pids"]) - 100) < 1
    assert a["resumo"]["tsid"] == 1 and a["resumo"]["n_servicos"] == 1


def test_varias_secoes_no_mesmo_pacote():
    an, g = AnalisadorTS(), Gerador()
    an.processar(g.pkt(0x11, b"\x00" + sdt(b"A") + sdt(b"B"), pusi=True), 0)
    assert an.si_visto.get("sdt") == 0 and an.cont["crc"] == 0


def test_desempenho_com_pcr_e_psi():
    """Fluxo realista a ~40 Mb/s (PCR, PSI, PES) precisa ser processado com folga em tempo real."""
    import time
    an, g = AnalisadorTS(), Gerador()
    clk = 27_000_000
    blocos = []
    for k in range(100):
        pk = [g.psi(0, pat()), g.psi(PMT_PID, pmt()), pkt_pcr(g, VIDEO, k * clk // 100), pkt_pes(g, VIDEO)]
        pk += [g.pkt(VIDEO) for _ in range(220)] + [g.pkt(AUDIO) for _ in range(30)] + [g.pkt(0x1FFF) for _ in range(14)]
        blocos.append(b"".join(pk))
    n_pacotes = sum(len(b) // 188 for b in blocos)
    t0 = time.perf_counter()
    for k, b in enumerate(blocos):
        for j in range(0, len(b), 1316):
            an.processar(b[j:j + 1316], k * 0.01)
        an.verificar(k * 0.01)
    gasto = time.perf_counter() - t0
    taxa_mbps = n_pacotes * 1504 / gasto / 1e6
    assert taxa_mbps > 60, f"processa só {taxa_mbps:.0f} Mb/s"
