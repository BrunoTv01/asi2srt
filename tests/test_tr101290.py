"""Testes do analisador TR 101 290 P1 com pacotes TS sintéticos."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from asi2srt import AnalisadorTS  # noqa: E402

PMT_PID, VIDEO, AUDIO = 0x100, 0x101, 0x102


def secao(tid, extensao, corpo):
    dados = struct.pack(">H", extensao) + b"\xc1\x00\x00" + corpo + b"\x00\x00\x00\x00"
    return bytes([tid, 0xB0 | (len(dados) >> 8), len(dados) & 0xFF]) + dados


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
