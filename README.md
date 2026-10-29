# asi2srt — Gateway ASI / UDP / SRT → SRT / UDP

Um único programa que recebe um transport stream (**placa DVB-ASI**, **UDP/multicast**,
**SRT** ou arquivo) e o reenvia para uma ou mais saídas **SRT** e/ou **UDP**.
Usa o [TSDuck](https://tsduck.io) como motor e inclui **painel web** para gerenciar
os canais, supervisão com reinício automático e monitoração.

```
 Entradas                                                          Saídas (uma ou várias)
 ─────────                                                         ──────────────────────
 DekTec ASI ─┐                                                  ┌─► SRT caller / listener (AES)
 DVEO ASI ───┤                                                  │
 UDP/multic. ┼─► tsp: continuity ─► filter ─► bitrate_monitor ──┼─► UDP unicast / multicast
 SRT ────────┤                                                  │
 Arquivo .ts ┘        painel web :8088 · reinício automático     └─► ...
```

## Instalação

**Binário pronto (não precisa de Python):** baixe em [Releases](../../releases).

```bash
chmod +x asi2srt-linux-amd64 && sudo mv asi2srt-linux-amd64 /usr/local/bin/asi2srt
```

**Ou script Python (3.8+):**

```bash
curl -LO https://raw.githubusercontent.com/BrunoTv01/asi2srt/main/asi2srt.py
chmod +x asi2srt.py && sudo mv asi2srt.py /usr/local/bin/asi2srt
pip install pyyaml   # opcional; sem ele a configuração é salva em JSON
```

Depois:

```bash
sudo asi2srt instalar-tsduck
asi2srt verificar
```

## Painel web

```bash
asi2srt rodar
```

Abra `http://IP-DO-SERVIDOR:8088` e entre com **admin / admin**. Para trocar, altere
`usuario` e `senha_painel` no arquivo de configuração e reinicie o serviço.

![Painel](docs/painel.png)

![Editar canal](docs/editar.png)

Pelo painel você:
- cria, edita e exclui canais
- escolhe a entrada (DekTec, Linux ASI, UDP, SRT, arquivo)
- adiciona várias saídas SRT e UDP por canal
- inicia, para e reinicia cada canal
- acompanha bitrate (com gráfico), erros de continuidade, reinícios e uptime
- vê o log de cada canal

Tudo que é alterado no painel é salvo no arquivo de configuração e sobrevive a reinícios.

## Linha de comando (sem painel nem arquivo)

```bash
# DekTec 0, porta 0 -> SRT caller
asi2srt rodar -e dektec:0:0 -s "srt://203.0.113.10:9000?latency=300"

# ASI (DVEO) -> SRT listener com AES + cópia em multicast
asi2srt rodar -e linuxasi:/dev/asirx0 \
  -s "srt://:9000?mode=listener&passphrase=minhasenha123" \
  -s "udp://239.1.1.1:1234?ttl=16"

# SRT recebido -> UDP multicast (retorno / recepção)
asi2srt rodar -e "srt://:9000?mode=listener" -s "udp://239.10.0.1:5000?local=10.0.0.5"

# UDP multicast -> SRT para um servidor com stream id
asi2srt rodar -e "udp://239.1.1.1:1234?local=10.0.0.5" \
  -s "srt://srt.exemplo.com:8080?streamid=publish/live/canal1"
```

| Entrada | Formato |
|---|---|
| DekTec (DTA-2145, DTA-2138, DTU-245…) | `dektec:PLACA:CANAL` |
| Driver Linux ASI (DVEO / Computer Modules) | `linuxasi:/dev/asirxN` |
| UDP / multicast | `udp://IP:PORTA?local=IP_DA_NIC` |
| SRT listener / caller | `srt://:PORTA?mode=listener` · `srt://HOST:PORTA` |
| Arquivo .ts em loop | `arquivo:caminho.ts` |

| Saída | Formato |
|---|---|
| SRT | `srt://HOST:PORTA?mode=caller\|listener&latency=MS&passphrase=SENHA&pbkeylen=16&streamid=ID` |
| UDP | `udp://IP:PORTA?ttl=N&local=IP_DA_NIC` |

Repita `-s` para várias saídas. A primeira é a principal; as demais recebem uma cópia.

## Arquivo de configuração

```bash
asi2srt init                     # cria asi2srt.yaml comentado
asi2srt comando -c asi2srt.yaml  # mostra os comandos tsp sem executar
asi2srt rodar -c asi2srt.yaml
```

## Serviço (inicia no boot)

```bash
sudo asi2srt instalar-servico
sudo systemctl enable --now asi2srt
# painel: http://IP:8088  (admin / admin)
```

## Monitoração externa

`http://IP:8088/status.json` (autenticação básica) — para Zabbix, Grafana etc.

## API

Todas as rotas usam autenticação básica. Rotas que alteram algo exigem o cabeçalho `X-Asi2srt: 1`.

| Método | Rota | Função |
|---|---|---|
| GET | `/api/canais` | lista canais e status |
| POST | `/api/canais` | cria canal |
| PUT / DELETE | `/api/canais/{id}` | edita / exclui |
| POST | `/api/canais/{id}/iniciar` · `parar` · `reiniciar` | controle |
| GET | `/api/canais/{id}/log` · `comando` | log / comando tsp |

## Teste sem hardware

```bash
asi2srt teste
```

Gera um TS com ffmpeg e o envia em SRT (porta 9000) e UDP (127.0.0.1:5000).
Painel em `http://127.0.0.1:8088` (admin / admin).

## Dicas

- **Latência SRT:** 3–4× o RTT. Internet pública: 500–2000 ms. LAN/MPLS: 120–200 ms.
- **Pacotes nulos:** removidos por padrão para economizar banda. Desligue se o receptor exigir CBR.
- **Multicast:** informe o IP da placa de rede (`local=` / "IP da interface") em servidores com várias NICs.
- **DekTec:** precisa do driver DekTec e do TSDuck com plugin `dektec`.
- **DVEO:** configure o driver para pacotes de 188 bytes.
- **Segurança:** o painel controla o gateway. Não exponha a porta 8088 na internet; use VPN ou firewall.

## Gerar uma versão (binário)

```bash
git tag v2.0.0 && git push origin v2.0.0
```

## Licença

MIT
